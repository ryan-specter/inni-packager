#!/usr/bin/env python3
"""Innioasis Mirror Manager: review and refresh the firmware mirrors.

A small desktop control panel around the same code the GitHub Actions workflow
runs.  It can review each mirror, dispatch the workflow, or package locally.

Refreshes are non-destructive: releases are kept and only missing assets are
added, while a changed file replaces just that asset.  Files whose size and
SHA-256 digest already match are skipped without downloading them again.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import shutil
import sys
import tempfile
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import tkinter as tk
from tkinter import messagebox, ttk

sys.path.insert(0, str(Path(__file__).resolve().parent))

import portable_mirror as pm  # noqa: E402
from gh import GitHub  # noqa: E402

ORG = os.environ.get("MIRROR_ORG", "y1-community")
WORKFLOW = "mirror.yml"
BRANCH = "main"

COLUMNS = ("mirror", "source", "releases", "missing", "changed", "run")
HEADINGS = {
    "mirror": "Mirror",
    "source": "Source",
    "releases": "Releases",
    "missing": "New",
    "changed": "Changed files",
    "run": "Last run",
}
WIDTHS = {
    "mirror": 235,
    "source": 245,
    "releases": 90,
    "missing": 70,
    "changed": 110,
    "run": 150,
}


class MirrorManager:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.events: "queue.Queue[tuple]" = queue.Queue()
        self.rows: Dict[str, Dict[str, Any]] = {}
        self.busy = False
        self.stop_event = threading.Event()
        self.work_dir = Path(tempfile.mkdtemp(prefix="mirror-manager-"))
        self.limit_var = tk.StringVar(value="")
        self.force_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value="Ready")

        self._build()
        self.root.after(120, self._poll)
        self.root.protocol("WM_DELETE_WINDOW", self._close)
        self.review()

    # ------------------------------------------------------------------- ui
    def _build(self) -> None:
        self.root.title("Innioasis Mirror Manager")
        self.root.geometry("1120x720")
        self.root.minsize(860, 520)

        style = ttk.Style()
        style.configure("Treeview", rowheight=24)
        style.configure("Title.TLabel", font=("Helvetica", 17, "bold"))
        style.configure("Hint.TLabel", foreground="#666666")

        header = ttk.Frame(self.root, padding=(14, 12, 14, 6))
        header.pack(fill="x")
        titles = ttk.Frame(header)
        titles.pack(side="left")
        ttk.Label(titles, text="Innioasis Mirror Manager", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            titles,
            text=f"{ORG} · review, dispatch and package without replacing releases",
            style="Hint.TLabel",
        ).pack(anchor="w")

        actions = ttk.Frame(header)
        actions.pack(side="right")
        self.review_btn = ttk.Button(actions, text="Review", command=self.review)
        self.review_btn.pack(side="left", padx=(0, 6))
        self.actions_btn = ttk.Button(
            actions, text="Update via GitHub Actions", command=self.update_actions
        )
        self.actions_btn.pack(side="left", padx=(0, 6))
        self.local_btn = ttk.Button(actions, text="Update locally", command=self.update_local)
        self.local_btn.pack(side="left", padx=(0, 6))
        self.stop_btn = ttk.Button(actions, text="Stop", command=self.stop, state="disabled")
        self.stop_btn.pack(side="left")

        options = ttk.Frame(self.root, padding=(14, 0, 14, 8))
        options.pack(fill="x")
        ttk.Label(options, text="Limit releases:").pack(side="left")
        ttk.Entry(options, textvariable=self.limit_var, width=6).pack(side="left", padx=(6, 14))
        ttk.Checkbutton(
            options, text="Rebuild current releases", variable=self.force_var
        ).pack(side="left")
        ttk.Label(
            options,
            text="Leave empty for the full backlog. Existing releases are retained.",
            style="Hint.TLabel",
        ).pack(side="left", padx=(16, 0))

        table = ttk.Frame(self.root, padding=(14, 0, 14, 8))
        table.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(table, columns=COLUMNS, show="headings", selectmode="extended")
        for column in COLUMNS:
            self.tree.heading(column, text=HEADINGS[column])
            self.tree.column(
                column,
                width=WIDTHS[column],
                anchor="w" if column in ("mirror", "source", "run") else "center",
                stretch=column == "source",
            )
        scroll = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        log_frame = ttk.Frame(self.root, padding=(14, 0, 14, 8))
        log_frame.pack(fill="both", expand=False)
        ttk.Label(log_frame, text="Activity").pack(anchor="w")
        self.log = tk.Text(log_frame, height=11, wrap="word", state="disabled", relief="flat")
        log_scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=log_scroll.set)
        self.log.pack(side="left", fill="both", expand=True)
        log_scroll.pack(side="right", fill="y")

        status = ttk.Label(self.root, textvariable=self.status_var, relief="sunken", padding=(10, 4))
        status.pack(fill="x", side="bottom")

    def _append(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        if int(self.log.index("end-1c").split(".")[0]) > 4000:
            self.log.delete("1.0", "1200.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _upsert(self, info: Dict[str, Any]) -> None:
        name = info["name"]
        self.rows[name] = info
        values = tuple(info.get(column, "") for column in COLUMNS)
        if self.tree.exists(name):
            self.tree.item(name, values=values)
        else:
            self.tree.insert("", "end", iid=name, values=values)
            self.tree.selection_add(name)

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        state = "disabled" if busy else "normal"
        for button in (self.review_btn, self.actions_btn, self.local_btn):
            button.configure(state=state)
        self.stop_btn.configure(state="normal" if busy else "disabled")

    def _emit(self, kind: str, payload: Any = None) -> None:
        self.events.put((kind, payload))

    def _poll(self) -> None:
        while True:
            try:
                kind, payload = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "log":
                self._append(str(payload))
            elif kind == "mirror":
                self._upsert(payload)
            elif kind == "status":
                self.status_var.set(str(payload))
            elif kind == "busy":
                self._set_busy(bool(payload))
        self.root.after(120, self._poll)

    # -------------------------------------------------------------- commands
    def review(self) -> None:
        if self.busy:
            return
        self.rows.clear()
        for item in self.tree.get_children():
            self.tree.delete(item)
        self._start(self._review_worker, f"Reviewing {ORG}…")

    def update_actions(self) -> None:
        names = self._selected()
        if not names:
            return
        if not messagebox.askyesno(
            "Update via GitHub Actions",
            f"Dispatch {WORKFLOW} for {len(names)} mirror(s)?\n\n"
            "Existing releases are kept; only missing or changed files are uploaded.",
        ):
            return
        self._start(lambda: self._actions_worker(names), "Dispatching workflows…")

    def update_local(self) -> None:
        names = self._selected()
        if not names:
            return
        if not messagebox.askyesno(
            "Update locally",
            f"Package {len(names)} mirror(s) on this Mac?\n\n"
            "Large releases take several minutes each. Already-current files are skipped.",
        ):
            return
        self._start(lambda: self._local_worker(names), "Packaging locally…")

    def stop(self) -> None:
        if not self.busy:
            return
        self.stop_event.set()
        self._append("Stopping after the current mirror…")
        self.status_var.set("Stopping…")

    def _selected(self) -> List[str]:
        selection = list(self.tree.selection())
        return selection or sorted(self.rows)

    def _start(self, target: Callable[[], None], status: str) -> None:
        self.stop_event.clear()
        self._set_busy(True)
        self.status_var.set(status)
        threading.Thread(target=self._guard, args=(target,), daemon=True).start()

    def _guard(self, target: Callable[[], None]) -> None:
        try:
            target()
        except Exception:
            self._emit("log", traceback.format_exc())
        finally:
            self._emit("busy", False)
            self._emit("status", "Ready")

    # --------------------------------------------------------------- workers
    def _review_worker(self) -> None:
        gh = GitHub()
        if not gh.gh_available():
            self._emit(
                "log",
                "The GitHub CLI (gh) is required. Install it from "
                "https://cli.github.com/ and run `gh auth login`.",
            )
            return
        if not gh.authenticated():
            self._emit(
                "log",
                "The GitHub CLI is not authenticated. Run `gh auth login` and try again.",
            )
            return
        repos = [
            repo for repo in gh.list_org_repos(ORG)
            if str(repo.get("name") or "").endswith("-mirror")
        ]
        self._emit("log", f"Found {len(repos)} mirror repositories in {ORG}.")

        def inspect(repo: Dict[str, Any]) -> Dict[str, Any]:
            name = str(repo["name"])
            config_file = gh.get_file(f"{ORG}/{name}", "mirror.config.json")
            if not config_file:
                return {
                    "name": name,
                    "source": "",
                    "releases": "?",
                    "missing": "no config",
                    "changed": "",
                    "run": "",
                }
            source = str(json.loads(config_file["text"]).get("source_repo") or "")
            source_releases = gh.list_releases(source)
            mirror_releases = gh.list_releases(f"{ORG}/{name}")
            mirrored = {str(r.get("tag_name")): r for r in mirror_releases}
            source_tags = [
                str(r.get("tag_name"))
                for r in source_releases
                if not r.get("draft")
            ]
            new = [tag for tag in source_tags if tag not in mirrored]

            changed = 0
            for release in source_releases:
                if release.get("draft"):
                    continue
                mirror_release = mirrored.get(str(release.get("tag_name")))
                if not mirror_release:
                    continue
                remote = {
                    str(asset.get("name")): asset
                    for asset in mirror_release.get("assets") or []
                }
                for asset in release.get("assets") or []:
                    if str(asset.get("name")) == pm.STATS_ASSET:
                        continue
                    other = remote.get(str(asset.get("name")))
                    if other and pm.assets_differ(asset, other):
                        changed += 1

            runs = gh.list_workflow_runs(f"{ORG}/{name}", WORKFLOW, 1)
            run_text = "never"
            if runs:
                run = runs[0]
                run_text = f"{run.get('status')}/{run.get('conclusion') or '-'}"
            return {
                "name": name,
                "source": source,
                "releases": f"{len(mirrored)}/{len(source_tags)}",
                "missing": str(len(new)),
                "changed": str(changed),
                "run": run_text,
            }

        total_new = 0
        total_changed = 0
        reviewed = 0
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(inspect, repo) for repo in repos]
            for future in as_completed(futures):
                try:
                    row = future.result()
                except Exception as exc:
                    self._emit("log", f"review error: {exc}")
                    continue
                reviewed += 1
                total_new += int(row.get("missing") or 0)
                total_changed += int(row.get("changed") or 0)
                self._emit("mirror", row)

        self._emit(
            "status",
            f"Review complete · {reviewed} mirrors · "
            f"{total_new} new release(s) · {total_changed} changed file(s)",
        )

    def _actions_worker(self, names: List[str]) -> None:
        gh = GitHub()
        limit = self.limit_var.get().strip()
        for name in names:
            if self.stop_event.is_set():
                break
            inputs: Dict[str, Any] = {}
            if limit:
                inputs["limit"] = limit
            if self.force_var.get():
                inputs["force"] = True
            gh.dispatch_workflow(f"{ORG}/{name}", WORKFLOW, BRANCH, inputs)
            self._emit("log", f"Dispatched {name}")
            time.sleep(1)
        self._emit("status", "Dispatched selected mirrors")

    def _local_worker(self, names: List[str]) -> None:
        gh = GitHub()
        limit_text = self.limit_var.get().strip()
        limit = int(limit_text) if limit_text.isdigit() else None
        for name in names:
            if self.stop_event.is_set():
                break
            self._emit("log", f"=== {name} ===")
            self._emit("status", f"Packaging {name}…")
            directory = self.work_dir / name
            directory.mkdir(parents=True, exist_ok=True)
            config_file = gh.get_file(f"{ORG}/{name}", "mirror.config.json")
            if not config_file:
                self._emit("log", f"{name}: mirror.config.json is missing")
                continue
            config_path = directory / "mirror.config.json"
            config_path.write_text(config_file["text"], encoding="utf-8")
            args = argparse.Namespace(
                config=str(config_path),
                source_repo=None,
                mirror_repo=None,
                platforms=None,
                tags=None,
                limit=limit,
                offset=0,
                force=self.force_var.get(),
                dry_run=False,
                work_dir=str(directory / "work"),
                keep_work=False,
                index=str(directory / "index.json"),
                skip_index=False,
                json_report=str(directory / "mirror-report.json"),
                verbose=False,
                publish_index=True,
            )
            try:
                code = pm.sync(args, sink=lambda line: self._emit("log", line))
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
            except Exception:
                self._emit("log", traceback.format_exc())
                code = 1
            shutil.rmtree(directory / "work", ignore_errors=True)
            self._emit("log", f"{name}: finished with exit {code}")

    # ------------------------------------------------------------------ close
    def _close(self) -> None:
        if self.busy and not messagebox.askyesno(
            "Quit", "A job is running. Quit anyway?"
        ):
            return
        self.stop_event.set()
        shutil.rmtree(self.work_dir, ignore_errors=True)
        self.root.destroy()


def main(argv: Optional[List[str]] = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if "--selftest" in args:
        root = tk.Tk()
        root.withdraw()
        root.update()
        root.destroy()
        return 0
    if "--uitest" in args:
        root = tk.Tk()
        root.withdraw()
        manager = MirrorManager(root)
        root.update()
        root.destroy()
        shutil.rmtree(manager.work_dir, ignore_errors=True)
        return 0
    root = tk.Tk()
    MirrorManager(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
