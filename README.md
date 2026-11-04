# inni-packager

Portable Flashing Zip Packager for Innioasis firmware. The canonical repository
lives at [`ryan-specter/inni-packager`](https://github.com/ryan-specter/inni-packager).
It creates and drives one thin mirror repository per unique firmware source
repository under the `y1-community` organisation.

Each mirror repackages firmware ZIPs into ready-to-run
[Innioasis Updater](https://github.com/y1-community/innioasis-updater) bundles.
Windows and Linux use the portable apps from
[updater-ce-neo](https://github.com/y1-community/updater-ce-neo); macOS uses
the latest `Innioasis.Updater.for.Mac.dmg`. Old releases are backfilled,
new releases are picked up automatically, and every mirror release keeps its
original tag and title while gaining portable downloads and a generated
image-button install/download header above the original notes. The source
repository's README is copied into the mirror, and the public index carries
the original titles and notes so the website can build its own release pages.

The output is what `innioasis.app/firmware.html` cannot safely build in a
browser: the workstation app plus the firmware payload, zipped once,
server-side, with maximum deflate.

For macOS users, the bundled `README.txt` explains the exact flow: open the
DMG, install the app, choose **Install from file...**, select the extracted
`firmware` folder (the original software download's Firmware folder), and
click **Install**. It also covers **System Settings → Privacy & Security →
Open Anyway**.

## What a mirrored release looks like

For a source release tagged `3.1.2` with `rom.zip`:

| Asset | Platform | Layout |
| --- | --- | --- |
| `rom.zip` | original | copied unchanged (configurable) |
| `rom-windows.zip` | Windows x86-64 | portable app + `SP_Flash_Tool/` firmware + `fw_package.ini` |
| `rom-windows-arm64.zip` | Windows arm64 | same, built automatically once upstream publishes the arm64 app |
| `rom-linux.zip` | Linux x86-64 | portable app + `SP_Flash_Tool/` firmware + `fw_package.ini` |
| `rom-linux-arm64.zip` | Linux arm64 | same, automatic once upstream publishes the arm64 app |
| `rom-macos.zip` | macOS (universal) | signed DMG copied as `1. Install App and Firmware.dmg` + lowercase `firmware/` folder with the images and `fw_package.ini` |
| `stats.txt` | all | cumulative source and mirror download figures |

Release title: `Portable Flashing Package: <original title>`. Each body starts
with generated image buttons: **Get it on Updater CE**, then the available
Windows, Linux and macOS portable ZIPs, grouped by device model and type when
a release contains more than one. The original notes follow unchanged. The
same original title and notes are stored in `index.json` as `source_title` and
`source_notes`.

Each mirror also copies the source repository's files, excluding `.github`
workflows and actions. The mirror keeps its own `mirror.config.json`, generated
`index.json`, and caller workflow, so upstream automation is never copied. The
source README is part of that copy; if the source has no README, a minimal
README is generated so the mirror always has a landing page.

Because `fw_package.ini` sits next to the single `*scatter*.txt`, Updater CE
detects the release on launch, fills in the software name, version, model and
type, and primes the **Install Software** screen. The user never has to pick
files or enter firmware details.

## How it works

1. The workflow lists every release in `source_repo` (newest first, drafts
   excluded).
2. Zips that look like SP Flash Tool packages are selected. Anything without a
   `*scatter*.txt` is skipped, so Rockbox `update.zip`-style SD-card updates
   are not mistaken for flashable firmware.
3. Windows and Linux pick the newest `updater-ce-neo` release that ships their
   portable ZIP. macOS uses the newest DMG from `innioasis-updater`.
4. The portable ZIP is streamed into a new ZIP at deflate level 9. For macOS,
   the official `Innioasis.Updater.for.Mac.dmg` is copied byte-for-byte and
   named `1. Install App and Firmware.dmg`; the firmware goes into a
   neighbouring lowercase `firmware/` folder. On Windows/Linux the portable
   app stays at the root and firmware is merged into `SP_Flash_Tool/`.
   `history.ini` and `fw_package.ini` are written so SP Flash Tool and Updater
   CE pick the right scatter and DA. The history entries use bare filenames,
   so launching `SP_Flash_Tool/flash_tool` directly works without the updater.
5. A release with the same tag is created or updated in the mirror repository,
   and the assets are uploaded.
6. `stats.txt` is written to every mirrored release, and the source README is
   copied into the mirror repository.
7. `index.json` is regenerated and committed. It includes `source_title`,
   `source_notes`, per-asset model/type metadata and a top-level
   `source_readme` URL in addition to the portable assets. The website reads
   the index from `raw.githubusercontent.com`, avoiding GitHub API rate limits.

Every mirror release body starts with generated image-button links and ends
with an invisible `<!-- portable-mirror:{...} -->` record with a fingerprint of
its inputs. The small button SVGs are published to `.mirror-assets/` on the
mirror branch, where `raw.githubusercontent.com` serves them as SVG; GitHub
release assets are downloaded as `application/octet-stream` and cannot be used
as rendered images. The notes therefore do not depend on a site deploy. A
later run skips the release when the source assets, config and Updater CE
build are unchanged, so scheduled runs are cheap and idempotent. The moment Updater CE
publishes a new portable build for a platform, the fingerprint changes and
every affected release is rebuilt around the new app version.

`stats.txt` records GitHub download events for the source assets and for every
mirror asset. Counts are snapshotted into a baseline before an asset is
replaced, so rebuilding with a new Updater CE build does not reset history.
It reports per-platform portable downloads and a combined estimate covering
manual SP Flash Tool downloads, Shared/Universal Flash Tool, Updater CE and
Firmware Archive links, which all fetch one of those two asset sets.

See [docs/how-it-works.md](docs/how-it-works.md) for the full pipeline,
[docs/website-integration.md](docs/website-integration.md) for the download
resolver, and [docs/static-release-pages.md](docs/static-release-pages.md) for
the site's release-note, install-guide and post-install page contract.

## Quick start: central onboarding

1. Create the public repository `ryan-specter/inni-packager` and push this
   folder to it.
2. Create a `MIRROR_TOKEN` secret with organisation repository administration,
   contents read/write on the source and mirror repositories, and Actions
   write. The token needs access to `y1-community`.
3. Run **Actions → Onboard y1-community firmware mirrors** once with
   `apply: false` and review `onboarding-report.json`.
4. Run it again with `apply: true`. It creates one
   `y1-community/<source-name>-mirror` per unique `type="img"` repository in
   `slidia_manifest.xml`, generates its `mirror.config.json`, and installs a
   thin caller workflow.
5. Optionally enable `trigger` to dispatch the first backfill in every mirror.
   Otherwise wait for each mirror's six-hour schedule or run it manually.

Each mirror keeps only its config, `index.json`, and a small workflow that
calls `ryan-specter/inni-packager/.github/workflows/reusable-mirror.yml@main`.
Packager updates therefore reach every mirror without copying code.

See [docs/onboarding.md](docs/onboarding.md) for the runbook, token scopes,
naming, overrides and recovery.

## Standalone clone (optional)

One product can still be mirrored on its own: create a repository, upload this
folder, edit `mirror.config.json` (`source_repo`, `mirror_repo`, `defaults`,
`rules`), allow workflow write access, and run **Actions → Portable flashing
packages (standalone)** manually. [docs/setup.md](docs/setup.md) covers this
configuration in detail.

## Requirements

- A public `ryan-specter/inni-packager` repository and a `MIRROR_TOKEN` with
  organisation administration plus contents/Actions write on the mirrors.
- Public firmware repositories with GitHub releases (private works when the
  token can read them).
- GitHub Actions enabled. The workflow uses the preinstalled `gh` CLI and
  Python 3.12; no third-party packages are needed.
- An Updater CE release that publishes the portable ZIP for each platform you
  enable.

## Local usage

```bash
python3 -m unittest discover -s tests -v

# Read-only onboarding plan for every manifest product
python3 tools/onboard.py --out onboarding-plan.json

# What would be mirrored, and is it current?
python3 tools/portable_mirror.py status --limit 10

# Build one release without uploading (downloads the app + firmware)
python3 tools/portable_mirror.py build --tag 3.1.2 --platforms windows --out dist

# Full sync; add --dry-run to build everything without publishing
python3 tools/portable_mirror.py sync --dry-run --limit 1

# Backfill every mirror from this machine, in resumable batches
tools/local-backfill.sh --limit 5
```

`tools/local-backfill.sh` publishes `index.json` through the contents API and
deletes each mirror's downloads and builds once its releases are on GitHub, so
a long backfill does not accumulate files locally.

`gh` is only needed for uploading and for private-repository downloads. Public
repositories can be read without it.

## Desktop apps

### Mirror Watch (native menu bar)

`tools/build_mirror_watch.sh` builds `dist/Mirror Watch.app`, a self-contained
SwiftUI menu-bar app for the current Mac architecture. It does not invoke
Python or dispatch GitHub Actions. All GitHub access, release creation, asset
uploading and index publishing happen directly on the Mac through the REST API.

The app reads `slidia_manifest.xml`, groups its `type="img"` rows into logical
products, creates missing `y1-community/<source>-mirror` repositories plus
their generated `mirror.config.json` files, and upgrades legacy configs with
the platform definitions needed by the native packager. It caches the newest portable app
assets for Windows and Linux from `y1-community/updater-ce-neo` and the newest
macOS DMG from `y1-community/innioasis-updater`, then downloads source
firmware, detects the scatter, builds `1. Install App and Firmware.dmg` +
`firmware/` on macOS and `SP_Flash_Tool/` packages on Windows/Linux, uploads
the release assets, and updates `index.json`. Releases keep their original
titles and notes. The shared sync is the path that adds the generated
release-note button header.

The menu-bar panel is deliberately just a live activity feed: checking the
manifest, creating a repository, downloading a portable app, packing each
platform, uploading each asset, and updating the index. Settings holds the
direct GitHub token (stored in Keychain), organization, manifest URL, polling
interval and auto-run toggle. A token needs repository contents write and
organization administration for automatic mirror creation.

```bash
# Build, self-test the native packager, and package the app
tools/build_mirror_watch.sh
open "dist/Mirror Watch.app"

# Read-only live check (also reports manifest products and missing mirrors)
dist/Mirror\ Watch.app/Contents/MacOS/MirrorWatch --check

# Pre-download the newest portable app for every published OS/architecture
dist/Mirror\ Watch.app/Contents/MacOS/MirrorWatch --cache-apps

# Replace every macOS package with the latest Innioasis DMG + firmware layout
dist/Mirror\ Watch.app/Contents/MacOS/MirrorWatch --replace-macos

# Repair and fully audit every mirror, packing and uploading missing assets
dist/Mirror\ Watch.app/Contents/MacOS/MirrorWatch --sync-all
```

### Mirror Manager (Tkinter control panel)

`tools/build_app.sh` builds `dist/Mirror Manager.app`, a universal2 (Intel and
Apple silicon) Tkinter control panel for the mirrors. It reviews every mirror
(source versus mirrored releases, new releases, changed files), dispatches
`mirror.yml` on GitHub Actions, or runs the same packaging locally.

Both refresh paths are non-destructive: existing releases are kept, missing
assets are added, and a changed file replaces only that asset. Files whose size
and SHA-256 already match are skipped, so repeated runs cost API calls rather
than uploads. Local runs publish `index.json` and delete each mirror's
downloads and builds when it finishes.

The app needs the `gh` CLI, authenticated with push access to the mirrors. The
build uses a universal2 interpreter (`/usr/bin/python3`, or set
`PYTHON=/path/to/python3`), installs PyInstaller into `.build/venv-app`, and
needs no administrator rights.

## Repository layout

```
mirror.config.json               # standalone config; mirrors get generated configs
.github/workflows/onboard.yml    # central repo onboarding workflow
.github/workflows/reusable-mirror.yml  # called by every y1-community mirror
.github/workflows/mirror.yml     # optional manual standalone runner
macos/MirrorWatch/              # native SwiftUI menu-bar mirror app
tools/build_mirror_watch.sh      # build and self-test the native app
tools/portable_mirror.py         # sync / build / status CLI
tools/onboard.py                 # create/configure y1-community mirrors
tools/manifest.py                # slidia_manifest.xml parser + config generator
tools/packager.py                # streaming repackager
tools/mirrorspec.py              # config, naming, fw_package.ini, fingerprints
tools/stats.py                   # cumulative stats.txt ledger and rendering
tools/gh.py                      # GitHub API + gh CLI
tests/                           # stdlib unittest, run in CI before every sync
docs/                            # pipeline, onboarding, website plan, troubleshooting
examples/                        # ready-made configs for other products
web/                             # optional site helper + download proxy worker
index.json                       # generated in each mirror by CI
```

## Limits and caveats

- **2 GiB per asset.** GitHub rejects larger release assets; the workflow
  reports an oversized package instead of uploading a broken release.
- **CORS.** Browsers cannot fetch GitHub release assets cross-origin, so a
  page cannot rename a download. Links work; the saved name is the portable
  ZIP's name unless you deploy the optional same-origin proxy in
  `web/download-proxy-worker.js`.
- **Storage.** `copy_original_assets` defaults to `true` so releases look like
  the originals. Set it to `false` for a lean mirror with only the portable
  packages.
- **Download counts.** `stats.txt` counts download events, not unique people;
  GitHub may lag, and a manual asset replacement outside the workflow cannot
  be reconstructed. Source counts cover every tool that fetches the original
  asset, so the combined figure is an activity estimate, not unique installs.
- **Large archives.** Use `--offset` / `--limit` (or the workflow inputs) to
  backfill in windows. Reruns are incremental: current releases are skipped
  without downloading anything.
- **Schedules.** GitHub disables scheduled workflows after 60 days without
  repository activity; a manual run or a commit re-enables it. Release
  creation also counts as activity.
- **Release events.** Releases created with `GITHUB_TOKEN` do not trigger other
  workflows. That is deliberate here: it prevents loops.

## License

MIT. See [LICENSE](LICENSE).
