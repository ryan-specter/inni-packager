# Troubleshooting

## The workflow fails immediately with "mirror_repo is not set"

Set `mirror_repo` in `mirror.config.json`, or run the workflow from the mirror
repository itself (there it falls back to `GITHUB_REPOSITORY`).

## "The GitHub CLI (gh) is required for publishing"

`gh` is preinstalled on GitHub Actions runners. Locally, install it from
<https://cli.github.com/> and run `gh auth login`, or set `GH_TOKEN`. Public
repositories can be built and inspected with `build` without `gh`.

## The source README was not updated

The README is committed through the Contents API, not through the workflow's
`git push`. Check `mirror-report.json` for a `(readme)` failure.

- The token needs **Contents: read** on the source and **Contents: write** on
  the mirror. `github.token` normally works for public repositories; use
  `MIRROR_TOKEN` for a private source.
- A protected `README.md` can reject the bot commit. Either allow the Actions
  bot to bypass the rule or set `source_readme.path` to a file the bot may
  write.
- If the source repository has no README, the run logs a warning and continues;
  packages are not blocked.
- Set `source_readme.enabled` to `false` only when the mirror README is managed
  by hand.

## The mirror README was replaced unexpectedly

`source_readme.path` defaults to `README.md`, so the upstream README becomes the
mirror's landing page. To keep the packager's own README, set it to another
file, for example:

```json
"source_readme": { "enabled": true, "path": "README.source.md" }
```

## "updater ... has no asset named ..."

The configured `updater_asset` does not exist in any Updater CE release.

- Check the exact asset name on the latest
  [updater-ce-neo release](https://github.com/y1-community/updater-ce-neo/releases).
- If the platform is not supported yet, mark it `"optional": true` (the arm64
  entries already are) or remove it from the `platforms` object.
- If you pinned `platforms.<key>.tag`, confirm that release has the asset.

## A release says "skipping ... no *scatter*.txt"

The ZIP is not a full SP Flash Tool package (for example Rockbox's
`update.zip`, which is an SD-card update). This is intentional. If a genuine
firmware package has a differently named scatter, make sure it is a `.txt`
whose name contains `scatter`; otherwise set `firmware.require_scatter` to
`false` (the package will be built but Updater CE will not auto-detect it).

## The mirror release exists but is missing assets

Rerun the workflow. A release whose recorded assets are missing or the wrong
size fails the "is current" check and is rebuilt. To force it:

```bash
python tools/portable_mirror.py sync --tags 3.1.2 --force
```

If uploads keep failing halfway, inspect the artifact `mirror-report.json`
from the failed run. The report lists the exact tag and error.

## "over the 2147483648 byte GitHub asset limit"

GitHub rejects release assets above 2 GiB. The workflow stops before uploading
that package. Options:

- split the firmware at the source (the upstream release would need smaller
  zips),
- raise `max_asset_bytes` only if GitHub changes its limit (the check exists to
  fail early),
- disable the platform that produced the oversized package.

## The run is very slow or runs out of disk

Each release downloads the original firmware (often 250–600 MB), the portable
apps (180–390 MB each), and writes one output per platform. The workflow
deletes a release's working directory as soon as it is uploaded, so disk peaks
at roughly one release. If you also copy originals, expect roughly:

```
originals + one output per platform + inputs
```

A 600 MB firmware with three platforms can peak around 4 GB. GitHub's runner
has enough for typical packages. Use `--tags` or the workflow `tags` input to
process one release at a time if you are close to the limit.

## "Resource not accessible by integration" when creating a release

The workflow token does not have write access:

- **Settings → Actions → General → Workflow permissions** → *Read and write*.
- Organisation policy can override repository settings.
- If you use `MIRROR_TOKEN`, the PAT needs **Contents: write** on the mirror.

## The scheduled workflow stopped running

GitHub disables schedules after 60 days without repository activity. Open the
Actions tab and run the workflow once, or push any commit. Release creation by
the workflow itself usually keeps the repository active.

## index.json is stale or missing

The workflow commits `index.json` after every run. If the commit step failed
(for example because the branch is protected), the releases are still correct;
only the website index is behind. Regenerate it locally and commit:

```bash
python tools/portable_mirror.py sync --tags 3.1.2   # writes index.json
git add index.json && git commit -m "Update index" && git push
```

Protecting the branch against direct pushes will also block this step; allow
the Actions bot or disable the protection for `index.json`.

## stats.txt is missing or counts dropped

- `stats.txt` is refreshed on every run, including releases whose packages are
  current. If it is absent, the release predates the feature or the stats
  upload failed; rerun the workflow and check the report.
- Counts dropping to zero usually means an asset was replaced without the
  ledger being read first. Rerunning the workflow restores the baseline only
  for counts still visible; a manually deleted/recreated asset's old count
  cannot be recovered.
- The source repository's own counts can reset if the publisher replaces an
  asset in place. The mirror cannot see the previous value.
- `stats.txt` counts download events, not unique people. Use it for trends and
  combined activity, not as an exact install count.

## macOS app is damaged after extraction

That happens when a repackager loses symlinks. This tool copies ZIP entries
without extracting them and preserves `external_attr`, so the `.app` keeps its
links. If you modify the packager, keep that property and run
`tests/test_packager.py::test_macos_layout_keeps_symlinks`.

## Website still downloads the original zip

- Confirm the catalog entry has `portable_zips`.
- Confirm the mirror has a release with exactly the same tag.
- Confirm the asset name's `source_asset` is the same string as the catalog's
  `packageName` (for example `rom_y2.zip`, not `rom-y2.zip`).
- Check the platform key in `index.json` matches the detected OS/arch. macOS is
  `macos` regardless of Intel or Apple Silicon.
- Check the browser console for a failed `raw.githubusercontent.com` fetch
  (offline, ad-blocker, or an incorrect branch in the URL).

The fallback is intentional; a failure to resolve a portable asset must never
block the download.

## Rebuilding changes nothing

The fingerprint includes the source asset IDs, sizes, the updater asset and the
relevant config. If none changed, the release is skipped. Use `--force` to
rebuild anyway.

## Private source repository

Create a fine-grained PAT with **Contents: read** on the source and
**Contents: write** on the mirror, save it as `MIRROR_TOKEN`, and let the
workflow use it. The same token is used for API reads, `gh` downloads and
uploads.
