# How the packaging works

## The upstream landscape

`innioasis.app/slidia_manifest.xml` is the app-market index that Updater CE
reads. It contains 19 active `<package>` entries across 16 GitHub repositories (plus
one commented-out APK example), all `type="img"`, with attributes such as:

```xml
<package name="Original Software" repo="y1-community/y1-stock-rom"
         device="Y1" url="https://github.com/y1-community/y1-stock-rom"
         type="img" handler="Custom Firmware" />
```

Some entries add `derivative` (the stock ROM a custom build is based on),
`post_install` (a page to open afterwards) or `icon`. One APK entry is
commented out, which is why the manifest is effectively image firmware only.

The hosting pattern for every listed project is the same:

- firmware lives in **GitHub Releases**, not in the repository tree
- the release tag is the version (`3.1.2`, `ADB-2.1.9`, `type-b-1.7.6`,
  `nightly-0936ff7…`, `baseline-207`)
- the release carries one or more ZIP assets, named by package role:
  `rom.zip` (Y1 Type A), `rom_type_b.zip` (Y1 Type B), `rom_y2.zip` (Y2), and
  occasionally `update.zip` (an SD-card update, not a flash package)
- the ZIP is a complete SP Flash Tool bundle: `flash_tool.exe`, DA binaries,
  the Qt runtime, `MT6582_Android_scatter_def.txt` or
  `MT6572_Android_scatter.txt`, and the partition images (`boot.img`,
  `system.img`, `lk.bin`, `userdata.img`, …). It is meant to be extracted into
  a folder next to the flashing tool.

The other device pages on `innioasis.app` (G1, G3, G5, Q8, R1, PR1, Timmkoo)
offer direct file downloads rather than repository releases. There is nothing
to mirror until those packages are published as releases.

Updater CE itself adds two relevant behaviours:

1. It reads the same GitHub releases (`repo` in the manifest) and picks the
   asset that matches the selected model and hardware type.
2. It can detect a **self-describing firmware folder**: when a single
   `*scatter*.txt` and a `fw_package.ini` sit in `SP_Flash_Tool/` (Windows and
   Linux) or beside the `.app` (macOS), the app primes the Install Software
   screen with the software name, version, model and type. `fw_package.ini` is
   optional, but it is what makes the portable package show the right details.

`fw_package.ini` is read from the application folder first on Windows/Linux,
then from the package folder. This workflow writes it in both places. On macOS
only the package folder is read, so it is written beside the `.app`.

## The portable app zips

`y1-community/updater-ce-neo` publishes, per `docs/RELEASE_FILES.md`:

| Platform | Asset |
| --- | --- |
| Windows x64 | `UpdaterCE-Windows-Portable.zip` |
| macOS | `Innioasis.Updater.for.Mac.zip` |
| Linux x86-64 | `UpdaterCE-Linux-Portable.zip` |
| Linux arm64 | `UpdaterCE-Linux-Portable-arm64.zip` |
| Windows arm64 | not published yet |

The real archives (checked from the 2.5.1 release) contain:

- Windows: `1. Install Driver First and Reboot PC.exe`, `2. Install Firmware.exe`,
  `SP_Flash_Tool/` (full tool), `_internal/`, `tools/`
- Linux: `InnioasisUpdater`, `SP_Flash_Tool/`, `_internal/`, `adb`, `tools/`
- macOS: `Innioasis Updater.app/`, with many symlinks inside
  (`Contents/Frameworks/QtCore` → `../../Resources/QtCore`, etc.)

Symlinks matter: naive extraction and re-zipping turns them into small text
files and breaks the signed app bundle. `tools/packager.py` never extracts to
disk. It copies entries from one ZIP to the other, carrying `external_attr`
(mode bits, including `S_IFLNK`) and timestamps, and writes the symlink target
as the entry body. Directories and symlinks are stored; everything else is
deflated at level 9.

Note that in the macOS archive the app is named `Innioasis Updater.app`, not
`Updater CE.app` as older `firmware.html` code assumed. The packager detects
any root `.app` bundle instead of hard-coding a name.

## Deployment shapes

- **Central (normal).** `ryan-specter/inni-packager` owns the code. Each
  `y1-community/<source>-mirror` owns a generated config and a small caller
  workflow. [Onboarding](onboarding.md) creates both.
- **Standalone.** One repository can still own the code and one product's
  config. The `Portable flashing packages (standalone)` workflow is manual-only
  in the central repo; a standalone clone can add its own schedule.

## Pipeline

```
source release ──► select *.zip assets ──► download once
                                          │
updater release ─► newest per platform ──►│
                                          ▼
                       stream-repack at deflate 9
                                          │
             ┌────────────────────────────┼───────────────────────────┐
             ▼                            ▼                           ▼
   SP_Flash_Tool merge            SP_Flash_Tool merge        .app + files beside it
   (Windows x64/arm64)            (Linux x64/arm64)          (macOS)
             └────────────────────────────┼───────────────────────────┘
                                          ▼
                     create/update release with the same tag
                                          ▼
                    upload variants + original assets
                                          ▼
                         rewrite index.json, commit
```

Before release processing, `sync` fetches the source repository's README and
writes it to `source_readme.path` in the mirror (`README.md` by default). This
is a repository-file update, not a release asset, and does not change the
package fingerprint.

The workflow processes releases **oldest first**. GitHub decides the "latest"
release by creation time, so creating the oldest mirror release first leaves
the newest tag as latest when the run finishes.

### Firmware selection

A source asset is repackaged when:

- it ends in `.zip`
- it is not one of this project's outputs (`*-windows.zip`, `*-macos.zip`, …)
- it does not match the platform/installer/archive excludes
- it is at most `max_asset_bytes` (2 GiB)
- it contains a `*scatter*.txt` (when `require_scatter` is true)

The scatter check is what keeps Rockbox's `update.zip` out. It is an SD-card
update with no scatter and no partition images; wrapping it would produce a
useless flashing package.

If a release has no eligible ZIP after filtering, the release is skipped, and
any stale index entry for that tag is removed. The original release is still
reachable on the source repository, which is what the website falls back to.

### Updater selection

Platforms are staggered upstream: a new release may ship Windows and macOS but
not yet Linux arm64. For each platform, the workflow walks the updater
repository newest-first and uses the first release that contains the exact
asset name. If none exists and the platform is `optional`, it is skipped (and
its variants are simply absent); otherwise the release fails with a clear
message.

The arm64 platforms are enabled by default with `optional: true`. The day
`UpdaterCE-Linux-Portable-arm64.zip` appears upstream, the next scheduled run
notices it, the fingerprint changes, and every release gains
`-linux-arm64.zip`. No config edit is needed.

When Updater CE publishes a new portable build for any enabled platform, the
fingerprint changes too, so existing releases are repackaged around the newest
app version rather than keeping the copy they were first built with.

### The repack

Windows/Linux (`sp_flash_tool`):

1. Copy every portable entry, except files that the firmware will replace.
2. Copy the firmware's scatter folder contents into `SP_Flash_Tool/`,
   flattening the scatter folder itself but keeping any sub-folders below it.
3. Write `SP_Flash_Tool/fw_package.ini` and `fw_package.ini` at the portable
   root.
4. Replace `SP_Flash_Tool/history.ini` with one whose `scatterHistory` points
   at the packaged scatter, so SP Flash Tool opens with it loaded.
5. Upstream `history.ini` from the firmware zip is dropped for the same reason.

macOS (`beside_app`):

1. Copy every entry of the portable zip, preserving the `.app` bundle and its
   symlinks.
2. Copy the firmware's scatter folder contents to the ZIP root, beside the
   `.app`.
3. Write `fw_package.ini` and `README.txt` at the root.

Firmware entries that collide with tool files overwrite them. That is
intentional: `rom.zip` is itself a working SP Flash Tool bundle, so the merged
result is a single self-contained folder.

Path traversal (`../`, absolute paths, drive letters) is rejected and logged.

### README and release-note mirroring

The mirror owns two representations of the source material:

- the source README copied to `README.md` (configurable via `source_readme`)
- each release body, which keeps the original notes above a provenance footer

`index.json` also stores the unmodified `source_title` and `source_notes` for
every release, plus a top-level `source_readme` object with the copied path and
its raw URL. The website generator should use those fields rather than parsing
the mirror release body: the body's footer and hidden `portable-mirror` block
are implementation details.

A stale README does not force a package rebuild. The README and index are
refreshed independently, so changing upstream documentation does not cause
hundreds of megabytes of firmware to be repackaged.

### Release metadata and fingerprint

Each mirrored release body is:

```
<original notes verbatim>

---

Portable flashing packages ... were added by [`owner/mirror`](...) from
`source@tag` using Innioasis Updater CE. The original release notes are
unchanged above.

<!-- portable-mirror:{"v":1,"fingerprint":"…","assets":[…],"skipped":[…],"stats":{…}} -->
```

The hidden block records:

- `fingerprint`: hash of builder version, relevant config, every source asset
  (name/size/id) and the exact updater asset per platform
- `assets`: output name, source asset, platform, size, SHA-256
- `original_assets`: copied source asset names
- `skipped`: firmware candidates rejected by the scatter check
- `stats`: cumulative download ledger (`baseline`, `retired`)

A run skips a release when the fingerprint matches and every recorded asset is
still present at the recorded size. This makes reruns cheap and makes partial
uploads visible: a missing or truncated asset forces a rebuild.

`index.json` is generated from these blocks, so it can be rebuilt from the
releases alone if it is ever lost.

## index.json

Committed at the repository root and served by
`https://raw.githubusercontent.com/<mirror>/<branch>/index.json`.

```json
{
  "schema": 1,
  "generated_at": "2026-11-03T09:00:00Z",
  "source_repo": "y1-community/y1-stock-rom",
  "mirror_repo": "you/y1-stock-rom-portable",
  "title_prefix": "Portable Flashing Package: ",
  "updater_repo": "y1-community/updater-ce-neo",
  "source_readme": {
    "path": "README.md",
    "source_url": "https://github.com/y1-community/y1-stock-rom/blob/main/README.md",
    "url": "https://raw.githubusercontent.com/you/y1-stock-rom-portable/main/README.md"
  },
  "platforms": [
    { "key": "windows", "os": "windows", "arch": "x86-64",
      "suffix": "-windows.zip", "layout": "sp_flash_tool" }
  ],
  "releases": [
    {
      "tag": "3.1.2",
      "title": "Portable Flashing Package: Original System Software 3.1.2",
      "source_url": "https://github.com/y1-community/y1-stock-rom/releases/tag/3.1.2",
      "source_title": "Original System Software 3.1.2",
      "source_notes": "Original release notes in Markdown…",
      "published_at": "2025-06-01T12:00:00Z",
      "assets": [
        { "name": "rom-windows.zip", "source_asset": "rom.zip",
          "platform": "windows", "size": 423456789,
          "sha256": "…", "url": "https://github.com/…" }
      ],
      "original_assets": ["rom.zip", "updater.jpg"],
      "stats_url": "https://github.com/…/releases/download/3.1.2/stats.txt"
    }
  ]
}
```

The website matches on `source_asset` (the file the existing download button
already knows) plus `platform`, so no filename arithmetic is needed on the
client.

## Download statistics (`stats.txt`)

Every mirrored release carries a `stats.txt` asset with source downloads,
mirror downloads, per-platform portable totals and a `[Totals]` block.

GitHub's `download_count` is per asset and resets when `gh release upload
--clobber` replaces it. The hidden block keeps a ledger:

```json
"stats": { "baseline": { "rom-windows.zip": 250 }, "retired": {}, "generated_at": "…" }
```

Before a rebuild, current counts for assets that will be replaced are folded
into `baseline`; assets that remain keep their live count; vanished assets are
folded into `retired`. `stats.txt` then reports `baseline + live count`, so a
new Updater CE build does not erase history. Current releases only regenerate
and re-upload `stats.txt`.

Source counts include every route that fetches the original asset (manual SP
Flash Tool, Shared/Universal tool, Updater CE, Firmware Archive, direct
links). `combined_download_events` adds them to the mirror total; it is a
download-event estimate, not unique installs.

## Edge cases

- **Multiple scatters in one zip.** All are copied. `history.ini` points at the
  best one: non-`preserve_data` first, then a unique `*ndroid_scatter*.txt`,
  then the only candidate.
- **Scatter inside a folder.** The folder prefix is stripped, so the package
  always has the scatter at the top of `SP_Flash_Tool/`.
- **Duplicate destinations.** First firmware entry wins; later duplicates are
  ignored, keeping ZIPs unambiguous.
- **A source release with several firmware zips.** Each becomes its own set of
  platform variants, e.g. `rom-windows.zip` and `rom_type_b-windows.zip`.
- **An Updater CE release without a platform.** Optional platforms are skipped;
  required ones abort that release with an explanatory report entry.
- **Firmware bigger than 2 GiB after repacking.** The build is rejected before
  upload instead of failing halfway through a release.
- **Original asset bigger than 2 GiB.** Copied only if under the limit;
  otherwise it is left on the source repository and noted in the log.
- **`stats.txt` counts.** A manual replacement of an asset outside the
  workflow cannot be reconstructed. Legacy releases without a `stats` ledger
  start from their currently visible counts on the first run.
