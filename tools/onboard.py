"""Create and configure the y1-community firmware mirror repositories.

The source list comes from ``slidia_manifest.xml``.  Each unique
``type="img"`` repository becomes one mirror repository named
``<org>/<source-name>-mirror``.  The mirror gets a generated
``mirror.config.json`` and a small caller workflow; the actual packaging code
stays in ``ryan-specter/inni-packager`` and is used through a reusable
workflow.

Run without ``--apply`` first.  It is a read-only plan.  ``--apply`` needs a
token with organisation repository administration, contents write on the
mirrors, and Actions write to trigger the first run.
"""

from __future__ import annotations

import argparse
import fnmatch
import html
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from gh import ApiError, GitHub, GitHubError
from manifest import (
    MANIFEST_URL,
    Product,
    caller_workflow,
    config_text,
    fetch_manifest,
    group_products,
    parse_manifest,
)

SITE_REPO = "y1-community/Innioasis-Updater"
SITE_MANIFEST = "slidia_manifest.xml"
PACKAGE_TAG_RE = re.compile(r"<package\b[^>]*?/>")
ATTRIBUTE_RE = re.compile(r'([A-Za-z_][\w:.-]*)\s*=\s*"([^"]*)"')
PORTABLE_ATTR_RE = re.compile(r"(\bportable_zips\s*=\s*)([\"'])(.*?)\2")


def parse_csv(value: Optional[str]) -> List[str]:
    if not value:
        return []
    return [item.strip().lower() for item in value.split(",") if item.strip()]


def selected_products(
    products: Sequence[Product], only: Sequence[str], skip: Sequence[str]
) -> List[Product]:
    result: List[Product] = []
    for product in products:
        haystack = f"{product.source_repo} {product.mirror_repo}".lower()
        if only and not any(fnmatch.fnmatch(haystack, f"*{pat}*") for pat in only):
            continue
        if skip and any(fnmatch.fnmatch(haystack, f"*{pat}*") for pat in skip):
            continue
        result.append(product)
    return result


def source_info(gh: GitHub, product: Product, check: bool) -> Dict[str, Any]:
    """Collect release and zip diagnostics used by the plan."""

    info: Dict[str, Any] = {
        "release_count": None,
        "zip_assets": [],
        "source_error": "",
        "source_missing": False,
    }
    if not check:
        return info
    try:
        releases = gh.list_releases(product.source_repo)
    except ApiError as exc:
        info["source_error"] = str(exc)
        info["source_missing"] = exc.status == 404
        return info
    except GitHubError as exc:
        info["source_error"] = str(exc)
        return info

    assets = sorted(
        {
            str(asset.get("name") or "")
            for release in releases
            for asset in (release.get("assets") or [])
            if str(asset.get("name") or "").lower().endswith(".zip")
        }
    )
    info["release_count"] = len(releases)
    info["zip_assets"] = assets
    return info


def _put_action(existing: bool, changed: bool) -> str:
    if changed:
        return "updated" if existing else "created"
    return "unchanged"


def set_portable_zips(text: str, mirrors: Mapping[str, str]) -> tuple[str, int]:
    """Set ``portable_zips`` on matching ``type="img"`` manifest rows.

    The site manifest is human-maintained and contains comments and irregular
    whitespace, so this is a tag-level text update rather than an ElementTree
    round trip.  Only rows whose source repository is in ``mirrors`` change.
    """

    changed = 0

    def replace_tag(match: re.Match[str]) -> str:
        nonlocal changed
        tag = match.group(0)
        attrs = dict(ATTRIBUTE_RE.findall(tag))
        if (attrs.get("type") or "").lower() != "img":
            return tag
        mirror = mirrors.get((attrs.get("repo") or "").strip().lower())
        if not mirror:
            return tag
        value = html.escape(mirror, quote=True)
        if PORTABLE_ATTR_RE.search(tag):
            updated = PORTABLE_ATTR_RE.sub(
                lambda item: item.group(1) + '"' + value + '"', tag, count=1
            )
        else:
            updated = re.sub(
                r"\s*/>$", ' portable_zips="' + value + '" />', tag
            )
        if updated != tag:
            changed += 1
        return updated

    return PACKAGE_TAG_RE.sub(replace_tag, text), changed


def sync_site_manifest(
    gh: GitHub,
    products: Sequence[Product],
    *,
    site_repo: str = SITE_REPO,
    site_manifest: str = SITE_MANIFEST,
    branch: str = "main",
    log=print,
) -> Dict[str, Any]:
    """Publish the created mirror names into the website manifest."""

    mirrors = {
        product.source_repo.lower(): product.mirror_repo for product in products
    }
    existing = gh.get_file(site_repo, site_manifest, ref=branch)
    if not existing:
        raise GitHubError(
            f"site manifest not found: {site_repo}/{site_manifest}@{branch}"
        )
    original = str(existing.get("text") or "")
    updated, rows = set_portable_zips(original, mirrors)
    action = "unchanged"
    if rows:
        committed = gh.put_file(
            site_repo,
            site_manifest,
            updated,
            "Define portable mirror repositories in the firmware manifest",
            branch,
        )
        action = "updated" if committed else "unchanged"
        log(f"  site manifest: {action} ({rows} rows)")
    return {
        "repo": site_repo,
        "path": site_manifest,
        "branch": branch,
        "action": action,
        "rows": rows,
    }


def apply_product(
    gh: GitHub,
    product: Product,
    *,
    branch: str = "",
    update_config: bool = False,
    trigger: bool = False,
    packager_repo: str = "ryan-specter/inni-packager",
    packager_ref: str = "main",
    org: str = "y1-community",
    log=print,
) -> Dict[str, Any]:
    """Create one mirror and seed its managed files.  Returns an action report."""

    existing = gh.get_repo(product.mirror_repo)
    created = False
    if not existing:
        name = product.mirror_repo.split("/", 1)[1]
        description = (
            f"Portable flashing packages for {product.source_repo} "
            f"({', '.join(product.devices) or 'firmware'})"
        )
        log(f"  creating {product.mirror_repo}")
        existing = gh.create_repo(
            org,
            name,
            description=description,
            private=False,
            auto_init=True,
        )
        created = True
    if existing.get("archived"):
        return {
            "mirror_repo": product.mirror_repo,
            "action": "skipped",
            "error": "repository is archived",
        }

    actual_branch = branch or str(existing.get("default_branch") or "main")
    config = config_text(
        product,
        branch=actual_branch,
        updater_repo="y1-community/updater-ce-neo",
    )
    config_path = "mirror.config.json"
    old_config = gh.get_file(product.mirror_repo, config_path, ref=actual_branch)
    if old_config and not update_config:
        config_action = "kept"
    else:
        changed = gh.put_file(
            product.mirror_repo,
            config_path,
            config,
            "Configure portable firmware packages",
            actual_branch,
        )
        config_action = _put_action(bool(old_config), changed)
        log(f"  config: {config_action}")

    workflow_path = ".github/workflows/mirror.yml"
    workflow = caller_workflow(packager_repo, packager_ref)
    old_workflow = gh.get_file(product.mirror_repo, workflow_path, ref=actual_branch)
    changed = gh.put_file(
        product.mirror_repo,
        workflow_path,
        workflow,
        "Use the central portable packaging workflow",
        actual_branch,
    )
    workflow_action = _put_action(bool(old_workflow), changed)
    log(f"  workflow: {workflow_action}")

    triggered = False
    trigger_error = ""
    if trigger:
        for attempt in range(5):
            try:
                gh.dispatch_workflow(product.mirror_repo, "mirror.yml", actual_branch)
                triggered = True
                break
            except ApiError as exc:
                if exc.status in (404, 409, 422) and attempt < 4:
                    time.sleep(2 * (attempt + 1))
                    continue
                trigger_error = str(exc)
                break
            except GitHubError as exc:
                trigger_error = str(exc)
                break
        log(f"  trigger: {'started' if triggered else trigger_error or 'not started'}")

    if created:
        action = "created"
    elif config_action in ("created", "updated") or workflow_action in ("created", "updated"):
        action = "updated"
    else:
        action = "unchanged"

    return {
        "action": action,
        "mirror_repo": product.mirror_repo,
        "created": created,
        "branch": actual_branch,
        "config": config_action,
        "workflow": workflow_action,
        "triggered": triggered,
        "trigger_error": trigger_error,
    }


def write_report(report: Mapping[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Create and configure y1-community firmware mirrors."
    )
    parser.add_argument("--manifest", default=MANIFEST_URL)
    parser.add_argument("--org", default="y1-community")
    parser.add_argument("--suffix", default="-mirror")
    parser.add_argument(
        "--packager-repo", default="ryan-specter/inni-packager"
    )
    parser.add_argument("--packager-ref", default="main")
    parser.add_argument("--site-repo", default=SITE_REPO, help="repository containing the website manifest")
    parser.add_argument("--site-manifest", default=SITE_MANIFEST, help="path to the website manifest")
    parser.add_argument("--no-site-manifest", action="store_true", help="do not update the website manifest")
    parser.add_argument("--only", default="", help="comma-separated source/mirror text filters")
    parser.add_argument("--skip", default="")
    parser.add_argument("--apply", action="store_true", help="create repositories and seed files")
    parser.add_argument("--trigger", action="store_true", help="dispatch each mirror workflow")
    parser.add_argument(
        "--update-config",
        action="store_true",
        help="overwrite existing mirror.config.json files",
    )
    parser.add_argument(
        "--include-missing-sources",
        action="store_true",
        help="create mirrors even when the source repository is missing",
    )
    parser.add_argument(
        "--no-check-releases",
        action="store_true",
        help="skip source release/asset checks",
    )
    parser.add_argument("--out", default="onboarding-report.json")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    only = parse_csv(args.only)
    skip = parse_csv(args.skip)
    try:
        packages = parse_manifest(fetch_manifest(args.manifest))
        products = group_products(packages, org=args.org, suffix=args.suffix)
    except (OSError, ValueError, GitHubError) as exc:
        print(f"error: could not read manifest: {exc}")
        return 2

    products = selected_products(products, only, skip)
    if not products:
        print("No products selected.")
        return 2

    gh = GitHub()
    report: Dict[str, Any] = {
        "manifest": args.manifest,
        "org": args.org,
        "packager_repo": args.packager_repo,
        "packager_ref": args.packager_ref,
        "apply": args.apply,
        "trigger": args.trigger,
        "site_manifest": None,
        "products": [],
        "failures": [],
    }
    applied_products: List[Product] = []

    for product in products:
        print(f"{product.source_repo} -> {product.mirror_repo}")
        info = source_info(gh, product, not args.no_check_releases)
        entry: Dict[str, Any] = {
            "source_repo": product.source_repo,
            "mirror_repo": product.mirror_repo,
            "software_name": product.software_name,
            "devices": product.devices,
            "post_installs": product.post_installs,
            **info,
        }
        if args.apply:
            if info["source_error"] and not args.include_missing_sources:
                entry["action"] = "skipped"
                entry["error"] = f"source unavailable: {info['source_error']}"
            else:
                try:
                    entry.update(
                        apply_product(
                            gh,
                            product,
                            update_config=args.update_config,
                            trigger=args.trigger,
                            packager_repo=args.packager_repo,
                            packager_ref=args.packager_ref,
                            org=args.org,
                        )
                    )
                except (GitHubError, ApiError, OSError, ValueError) as exc:
                    entry["action"] = "failed"
                    entry["error"] = str(exc)
                    report["failures"].append(
                        {"mirror_repo": product.mirror_repo, "error": str(exc)}
                    )
        else:
            entry["mirror_exists"] = gh.get_repo(product.mirror_repo) is not None
            entry["action"] = "planned"
        report["products"].append(entry)
        if args.apply and entry.get("action") in ("created", "updated", "unchanged"):
            applied_products.append(product)
        print(
            f"  action={entry.get('action')} "
            f"releases={entry.get('release_count')} "
            f"zips={len(entry.get('zip_assets') or [])}"
        )

    if args.apply and not args.no_site_manifest and applied_products:
        try:
            report["site_manifest"] = sync_site_manifest(
                gh,
                applied_products,
                site_repo=args.site_repo,
                site_manifest=args.site_manifest,
            )
        except (GitHubError, ApiError, OSError, ValueError) as exc:
            report["site_manifest"] = {"action": "failed", "error": str(exc)}
            report["failures"].append(
                {"mirror_repo": args.site_repo, "error": str(exc)}
            )
            print(f"error: could not update the site manifest: {exc}")

    write_report(report, Path(args.out))
    print(f"wrote {args.out}")
    if report["failures"]:
        print(f"{len(report['failures'])} failure(s)")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
