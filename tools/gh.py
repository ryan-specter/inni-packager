"""Small GitHub client used by the mirror.

Reads use the REST API with ``GITHUB_TOKEN`` when available.  Release asset
downloads and uploads shell out to the GitHub CLI (``gh``) because it handles
large multipart uploads and private repositories well; downloads fall back to
plain HTTP for public repositories so local runs work without ``gh``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

API_ROOT = "https://api.github.com"
USER_AGENT = "portable-flashing-package/1.0"
CHUNK = 1024 * 1024


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def asset_matches(path: Path, asset: Mapping[str, Any]) -> bool:
    """Return True when a local file already equals a release asset.

    GitHub reports a ``sha256:`` digest for uploaded assets; when it is
    missing only the size can be compared, which is still enough to skip the
    obvious no-op without downloading the asset again.
    """

    if int(asset.get("size") or 0) != path.stat().st_size:
        return False
    digest = str(asset.get("digest") or "")
    if digest.startswith("sha256:"):
        return sha256_file(path) == digest.split(":", 1)[1]
    return True


class GitHubError(RuntimeError):
    pass


class ApiError(GitHubError):
    def __init__(self, status: int, message: str, url: str = "") -> None:
        super().__init__(f"GitHub API {status}: {message}" + (f" ({url})" if url else ""))
        self.status = status
        self.url = url


class GitHub:
    def __init__(self, token: Optional[str] = None, gh_bin: str = "gh") -> None:
        self.token = token or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
        self.gh_bin = gh_bin
        self._gh_path: Optional[str] = None
        self._gh_token_loaded = False

    # ------------------------------------------------------------------ HTTP
    def _auth_token(self) -> str:
        """Use gh's stored token when the environment has no token.

        This keeps local `gh auth login` usable for the README Contents API
        while Actions continues to pass GH_TOKEN/GITHUB_TOKEN explicitly.
        """

        if self.token or self._gh_token_loaded:
            return self.token
        self._gh_token_loaded = True
        if not self.gh_available():
            return ""
        try:
            result = subprocess.run(
                [self._gh_path or self.gh_bin, "auth", "token"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                check=False,
            )
        except OSError:
            return ""
        if result.returncode == 0:
            self.token = (result.stdout or "").strip()
        return self.token

    def _headers(self, accept: str) -> Dict[str, str]:
        headers = {
            "Accept": accept,
            "User-Agent": USER_AGENT,
            "X-GitHub-Api-Version": "2022-11-28",
        }
        token = self._auth_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def request_json(
        self,
        path: str,
        params: Optional[Mapping[str, Any]] = None,
        method: str = "GET",
        data: Optional[Mapping[str, Any]] = None,
    ) -> Any:
        url = path if path.startswith("http") else API_ROOT + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        delay = 2.0
        for attempt in range(5):
            headers = self._headers("application/vnd.github+json")
            payload = None
            if data is not None:
                payload = json.dumps(data).encode("utf-8")
                headers["Content-Type"] = "application/json"
            req = urllib.request.Request(
                url, data=payload, headers=headers, method=method
            )
            try:
                with urllib.request.urlopen(req, timeout=90) as response:
                    raw = response.read()
                    return json.loads(raw.decode("utf-8")) if raw else None
            except urllib.error.HTTPError as exc:
                remaining = exc.headers.get("x-ratelimit-remaining")
                reset = exc.headers.get("x-ratelimit-reset")
                if exc.code == 404:
                    raise ApiError(404, "not found", url) from exc
                if exc.code in (403, 429) and remaining == "0" and reset:
                    wait = max(5, min(int(reset) - int(time.time()) + 5, 900))
                    if attempt == 0:
                        time.sleep(wait)
                        continue
                if exc.code in (429, 500, 502, 503, 504) and attempt < 4:
                    time.sleep(delay)
                    delay *= 2
                    continue
                body = exc.read().decode("utf-8", "replace")[:500]
                raise ApiError(exc.code, body or exc.reason, url) from exc
            except urllib.error.URLError as exc:
                if attempt < 4:
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise GitHubError(f"network error for {url}: {exc.reason}") from exc
        raise GitHubError(f"gave up on {url}")

    # -------------------------------------------------------------- releases
    def list_releases(self, repo: str, max_pages: int = 50) -> List[Dict[str, Any]]:
        releases: List[Dict[str, Any]] = []
        for page in range(1, max_pages + 1):
            chunk = self.request_json(
                f"/repos/{repo}/releases", {"per_page": 100, "page": page}
            )
            if not isinstance(chunk, list):
                raise GitHubError(f"unexpected release payload for {repo}: {type(chunk)!r}")
            releases.extend(chunk)
            if len(chunk) < 100:
                break
        return releases

    def get_repo(self, repo: str) -> Optional[Dict[str, Any]]:
        try:
            return self.request_json(f"/repos/{repo}")
        except ApiError as exc:
            if exc.status == 404:
                return None
            raise

    def create_repo(
        self,
        org: str,
        name: str,
        description: str = "",
        private: bool = False,
        auto_init: bool = True,
    ) -> Dict[str, Any]:
        """Create an organisation repository, returning it if it exists."""

        payload = {
            "name": name,
            "description": description,
            "private": private,
            "auto_init": auto_init,
            "has_issues": True,
            "has_wiki": False,
        }
        try:
            created = self.request_json(
                f"/orgs/{urllib.parse.quote(org, safe='')}/repos",
                method="POST",
                data=payload,
            )
            return created if isinstance(created, dict) else {}
        except ApiError as exc:
            if exc.status != 422:
                raise
            existing = self.get_repo(f"{org}/{name}")
            if existing:
                return existing
            raise

    def dispatch_workflow(
        self, repo: str, workflow: str, ref: str, inputs: Optional[Mapping[str, Any]] = None
    ) -> None:
        """Trigger a workflow_dispatch run; a 204 response has no body."""

        self.request_json(
            f"/repos/{repo}/actions/workflows/"
            f"{urllib.parse.quote(workflow, safe='')}/dispatches",
            method="POST",
            data={"ref": ref, "inputs": dict(inputs or {})},
        )

    def get_release_by_tag(self, repo: str, tag: str) -> Optional[Dict[str, Any]]:
        try:
            return self.request_json(
                f"/repos/{repo}/releases/tags/{urllib.parse.quote(tag, safe='')}"
            )
        except ApiError as exc:
            if exc.status == 404:
                return None
            raise

    # ------------------------------------------------------- repository files
    @staticmethod
    def _file_payload(data: Any) -> Optional[Dict[str, Any]]:
        if not isinstance(data, dict) or not data.get("name"):
            return None
        content = data.get("content")
        text = ""
        if content:
            try:
                text = base64.b64decode(content).decode("utf-8")
            except (ValueError, UnicodeDecodeError) as exc:
                raise GitHubError(f"could not decode repository file {data.get('path')}: {exc}") from exc
        return {
            "name": str(data.get("name") or ""),
            "path": str(data.get("path") or ""),
            "sha": data.get("sha"),
            "text": text,
            "html_url": data.get("html_url"),
            "download_url": data.get("download_url"),
        }

    def get_readme(self, repo: str) -> Optional[Dict[str, Any]]:
        """Fetch the repository README selected by GitHub's readme endpoint."""

        try:
            data = self.request_json(f"/repos/{repo}/readme")
        except ApiError as exc:
            if exc.status == 404:
                return None
            raise
        return self._file_payload(data)

    def get_file(
        self, repo: str, path: str, ref: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """Fetch one file from a branch, or None when it is absent."""

        quoted = urllib.parse.quote(path, safe="/")
        params = {"ref": ref} if ref else None
        try:
            data = self.request_json(
                f"/repos/{repo}/contents/{quoted}", params=params
            )
        except ApiError as exc:
            if exc.status == 404:
                return None
            raise
        return self._file_payload(data)

    def put_file(
        self,
        repo: str,
        path: str,
        text: str,
        message: str,
        branch: str,
    ) -> bool:
        """Create or update a repository file.  Return True when committed."""

        existing = self.get_file(repo, path, ref=branch)
        if existing and existing.get("text") == text:
            return False
        payload: Dict[str, Any] = {
            "message": message,
            "content": base64.b64encode(text.encode("utf-8")).decode("ascii"),
            "branch": branch,
        }
        if existing and existing.get("sha"):
            payload["sha"] = existing["sha"]
        quoted = urllib.parse.quote(path, safe="/")
        self.request_json(
            f"/repos/{repo}/contents/{quoted}", method="PUT", data=payload
        )
        return True

    # ------------------------------------------------------------------- gh
    def gh_available(self) -> bool:
        if self._gh_path is None:
            self._gh_path = shutil.which(self.gh_bin) or ""
        return bool(self._gh_path)

    def authenticated(self) -> bool:
        """True when a token is available from the environment or gh."""

        return bool(self._auth_token())

    def _gh_env(self) -> Dict[str, str]:
        env = dict(os.environ)
        if self.token:
            env["GH_TOKEN"] = self.token
            env["GITHUB_TOKEN"] = self.token
        return env

    def _run_gh(self, args: List[str], log=None, stream_stdout: Optional[Path] = None) -> None:
        if not self.gh_available():
            raise GitHubError(
                "the GitHub CLI (gh) is required to upload or download private "
                "release assets; install it from https://cli.github.com/"
            )
        stdout = subprocess.PIPE
        handle = None
        if stream_stdout is not None:
            handle = stream_stdout.open("wb")
            stdout = handle
        try:
            result = subprocess.run(
                [self._gh_path or self.gh_bin, *args],
                stdout=stdout,
                stderr=subprocess.PIPE,
                env=self._gh_env(),
                check=False,
            )
        finally:
            if handle:
                handle.close()
        if result.returncode != 0:
            tail = (result.stderr or b"").decode("utf-8", "replace").strip().splitlines()
            raise GitHubError(
                "gh " + " ".join(args[:3]) + " failed: " + (" | ".join(tail[-4:]) or "unknown error")
            )

    # -------------------------------------------------------------- download
    def download(
        self,
        repo: str,
        asset: Mapping[str, Any],
        dest: Path,
        log=None,
        progress_every: int = 256 * 1024 * 1024,
    ) -> Path:
        """Download one release asset, resuming a partial ``.part`` file."""

        dest.parent.mkdir(parents=True, exist_ok=True)
        expected = int(asset.get("size") or 0)
        if dest.exists() and expected and dest.stat().st_size == expected:
            if log:
                log(f"    cached {dest.name} ({expected / 1024 / 1024:.0f} MiB)")
            return dest

        part = dest.with_name(dest.name + ".part")
        url = str(asset.get("browser_download_url") or "")
        last_error: Optional[Exception] = None
        for attempt in range(3):
            try:
                self._http_download(url, part, expected, log, progress_every)
            except urllib.error.HTTPError as exc:
                last_error = exc
                if exc.code in (401, 403, 404):
                    break
                time.sleep(2 * (attempt + 1))
                continue
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(2 * (attempt + 1))
                continue

            if not expected or (part.exists() and part.stat().st_size == expected):
                break
            # A truncated body can end without a socket error. Restarting is
            # safer than resuming bytes the server may have cut short.
            last_error = GitHubError(
                f"short transfer for {dest.name}: "
                f"{part.stat().st_size if part.exists() else 0} of {expected} bytes"
            )
            if attempt < 2:
                part.unlink(missing_ok=True)
                time.sleep(2 * (attempt + 1))

        size_ok = part.exists() and (not expected or part.stat().st_size == expected)
        if not size_ok and asset.get("id") and self.gh_available():
            if log:
                log(f"    retrying {dest.name} through gh api")
            part.unlink(missing_ok=True)
            self._run_gh(
                [
                    "api",
                    "-H",
                    "Accept: application/octet-stream",
                    f"/repos/{repo}/releases/assets/{asset['id']}",
                ],
                stream_stdout=part,
            )

        if not part.exists():
            raise GitHubError(f"could not download {asset.get('name')}: {last_error}")
        if expected and part.stat().st_size != expected:
            raise GitHubError(
                f"download size mismatch for {asset.get('name')}: "
                f"{part.stat().st_size} != {expected}"
            )
        part.replace(dest)
        if log:
            log(f"    downloaded {dest.name} ({dest.stat().st_size / 1024 / 1024:.0f} MiB)")
        return dest

    def _http_download(
        self,
        url: str,
        part: Path,
        expected: int,
        log,
        progress_every: int,
    ) -> None:
        if not url:
            raise GitHubError("asset has no browser_download_url")
        offset = part.stat().st_size if part.exists() else 0
        headers = {"User-Agent": USER_AGENT}
        if offset:
            headers["Range"] = f"bytes={offset}-"
        request = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(request, timeout=120) as response:
            mode = "ab" if (offset and response.status == 206) else "wb"
            if mode == "wb":
                offset = 0
            total = int(response.headers.get("Content-Length") or 0) + offset
            next_mark = progress_every
            with part.open(mode) as handle:
                while True:
                    chunk = response.read(CHUNK)
                    if not chunk:
                        break
                    handle.write(chunk)
                    offset += len(chunk)
                    if log and total and offset >= next_mark:
                        log(
                            f"    downloading {part.name}: "
                            f"{offset * 100 // max(total, 1)}% "
                            f"({offset / 1024 / 1024:.0f}/{total / 1024 / 1024:.0f} MiB)"
                        )
                        next_mark += progress_every

    # ---------------------------------------------------------------- upload
    def ensure_release(
        self, repo: str, tag: str, title: str, notes_path: Path, log=None
    ) -> Dict[str, Any]:
        existing = self.get_release_by_tag(repo, tag)
        if existing:
            self._run_gh(
                ["release", "edit", tag, "--repo", repo, "--title", title, "--notes-file", str(notes_path)]
            )
            if log:
                log(f"    updated release {tag}")
            return existing
        self._run_gh(
            ["release", "create", tag, "--repo", repo, "--title", title, "--notes-file", str(notes_path)]
        )
        if log:
            log(f"    created release {tag}")
        created = self.get_release_by_tag(repo, tag)
        return created or {}

    def upload(
        self,
        repo: str,
        tag: str,
        paths: List[Path],
        log=None,
        skip_unchanged: bool = True,
    ) -> Dict[str, int]:
        """Upload only assets that differ from the release.

        Existing assets whose size and SHA-256 digest match are skipped; a
        changed asset replaces just the asset with that name.  The release,
        its notes and every other asset are left in place, so refreshing an
        old release never deletes and recreates it.
        """

        stats = {"uploaded": 0, "skipped": 0, "bytes": 0}
        if not paths:
            return stats

        remote: Dict[str, Dict[str, Any]] = {}
        if skip_unchanged:
            release = self.get_release_by_tag(repo, tag) or {}
            remote = {
                str(asset.get("name")): asset
                for asset in release.get("assets") or []
            }

        pending: List[Path] = []
        for path in paths:
            current = remote.get(path.name)
            if current and asset_matches(path, current):
                stats["skipped"] += 1
                if log:
                    log(f"    unchanged {path.name}")
                continue
            pending.append(path)

        for start in range(0, len(pending), 10):
            batch = pending[start : start + 10]
            for attempt in range(3):
                try:
                    self._run_gh(
                        [
                            "release",
                            "upload",
                            tag,
                            "--repo",
                            repo,
                            "--clobber",
                            *[str(path) for path in batch],
                        ]
                    )
                    break
                except GitHubError:
                    if attempt == 2:
                        raise
                    time.sleep(3 * (attempt + 1))
            stats["uploaded"] += len(batch)
            stats["bytes"] += sum(path.stat().st_size for path in batch)
            if log:
                log(
                    f"    uploaded {min(start + 10, len(pending))}/{len(pending)} "
                    "changed assets"
                )
        return stats

    def list_org_repos(self, org: str, max_pages: int = 5) -> List[Dict[str, Any]]:
        """List organisation repositories (used by the desktop review)."""

        repos: List[Dict[str, Any]] = []
        for page in range(1, max_pages + 1):
            chunk = self.request_json(
                f"/orgs/{urllib.parse.quote(org, safe='')}/repos",
                {"per_page": 100, "page": page},
            )
            if not isinstance(chunk, list):
                break
            repos.extend(chunk)
            if len(chunk) < 100:
                break
        return repos

    def list_workflow_runs(
        self, repo: str, workflow: str = "mirror.yml", limit: int = 5
    ) -> List[Dict[str, Any]]:
        """Recent runs for one workflow, newest first."""

        data = self.request_json(
            f"/repos/{repo}/actions/workflows/"
            f"{urllib.parse.quote(workflow, safe='')}/runs",
            {"per_page": max(1, min(limit, 100))},
        )
        if not isinstance(data, dict):
            return []
        return list(data.get("workflow_runs") or [])
