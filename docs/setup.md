# Setting up a standalone mirror

> The recommended setup is central onboarding: see
> [onboarding.md](onboarding.md). It creates every
> `y1-community/<source>-mirror` and keeps the packaging code in
> `ryan-specter/inni-packager`. This page describes the optional standalone
> clone, where one repository owns both the code and one product.

This repository can be used as a template for one firmware product. Each
product gets its own clone:

- `y1-community/y1-stock-rom` → `you/y1-stock-rom-portable`
- `rockbox-y1/rockbox` → `you/rockbox-y1-portable`
- `y1-community/y1-ata-rom` → `you/y1-ata-rom-portable`

The clone owns one `mirror.config.json` and one Actions workflow. No secrets
are required for public repositories.

## 1. Create the repository

Create an empty public repository. Upload the contents of this folder (drag and
drop works, or `git init` + `git add .` + `git push`). Keep `.github/`,
`tools/`, `tests/` and `mirror.config.json`; `index.json` is generated on the
first run.

## 2. Point it at a product

Edit `mirror.config.json`:

```json
{
  "source_repo": "y1-community/y1-stock-rom",
  "mirror_repo": "your-name/y1-stock-rom-portable"
}
```

`mirror_repo` can be left empty locally; in Actions it falls back to
`GITHUB_REPOSITORY`. Set it explicitly if you run the CLI by hand.

The source repository's README is copied into the mirror on every run. The
default target is `README.md`, so the mirror's landing page matches the
firmware project. If this mirror's own README should be kept, set
`source_readme.path` to another file (for example `README.source.md`).

```json
"source_readme": {
  "enabled": true,
  "path": "README.md",
  "commit_message": "Mirror source README"
}
```

Then describe the firmware for `fw_package.ini`:

```json
"defaults": {
  "software_name": "Original Software",
  "target_model": "Y1",
  "target_type": "Type A",
  "description": ""
},
"rules": [
  { "match_asset": "(?i)(^|[-_.])y2([-_.]|$)", "target_model": "Y2", "target_type": "Stock" },
  { "match_asset": "(?i)type[-_ ]?b", "target_model": "Y1", "target_type": "Type B" }
]
```

Rules are matched against each firmware asset name, in order, and only the
first match applies. Supported values are the ones Updater CE documents:
`target_model` is `Y1`, `Y2` or `Generic MTK`; `target_type` is `Type A`,
`Type B`, `Stock`, `Native`, `360p` or `240p`.

## 3. Allow the workflow to publish

**Settings → Actions → General → Workflow permissions** must permit read and
write. The workflow also declares `permissions: contents: write`, but an
organisation policy can still override it.

For a private source repository, or when the default token cannot read the
source, create a fine-grained PAT with:

- **Contents: read** on the source repository
- **Contents: write** on the mirror repository

and save it as the `MIRROR_TOKEN` secret. The workflow uses it for both the
API and `gh` when present:

```yaml
GH_TOKEN: ${{ secrets.MIRROR_TOKEN || github.token }}
```

## 4. Run it

**Actions → Portable flashing packages (standalone) → Run workflow.**

Inputs:

| Input | Meaning |
| --- | --- |
| `source_repo` | temporary override of `source_repo` |
| `tags` | comma-separated globs, e.g. `3.1.2,type-b-*` |
| `platforms` | `windows,linux,linux-arm64,macos` |
| `limit` | only the N newest releases (useful for a first test) |
| `offset` | skip the N newest releases (batch backfill for large archives) |
| `force` | rebuild even when the fingerprint says current |
| `dry_run` | build without publishing or committing the index |

Start with `limit: 1` and `dry_run: true` to confirm the pipeline, then run it
for real. For an archive with hundreds of releases, backfill in windows with
`offset`: run `limit: 20, offset: 0`, then `limit: 20, offset: 20`, and so on.
Each run is incremental; already-current releases are skipped.

The first scheduled run backfills every historical release, oldest first, so
GitHub's "latest release" ends up pointing at the newest tag.

## 5. Verify a release

Open any mirrored release and check:

- the tag matches the source
- the title starts with `Portable Flashing Package: `
- the original notes are intact below the generated button header
- the buttons render and the Updater CE button opens the install guide
- `README.md` matches the source repository's README (or the configured path)
- the assets include `rom-windows.zip`, `rom-linux.zip` and `rom-macos.zip`;
  the Updater CE SVGs live under `.mirror-assets/` on the branch
- `stats.txt` is present and shows source, mirror, per-platform and combined
  download figures
- `index.json` at the repository root lists the same assets with sizes and
  SHA-256 hashes, plus `stats_url`; it also carries `source_title`,
  `source_notes` and a top-level `source_readme` URL

`rom-macos.zip` should contain `1. Install App and Firmware.dmg`, a lowercase
`firmware/` folder, `README.txt` and the post-install shortcut. `rom-windows.zip`
should contain `SP_Flash_Tool/` with the scatter, images and `fw_package.ini`.

## Config reference

| Key | Default | Notes |
| --- | --- | --- |
| `source_repo` | – | Repository to mirror. Required. |
| `mirror_repo` | `GITHUB_REPOSITORY` | Repository that receives releases. |
| `branch` | `main` | Branch where `index.json` is committed. |
| `updater_repo` | `y1-community/updater-ce-neo` | Source of the Windows/Linux portable app zips. |
| `title_prefix` | `Portable Flashing Package: ` | Applied to release titles. |
| `copy_original_assets` | `true` | Copy every original asset; source zips are repacked at maximum deflate. |
| `source_readme.enabled` | `true` | Copy the source repository README into the mirror. |
| `source_readme.path` | `README.md` | Target path. Use another path to keep the mirror's operator README. |
| `source_readme.commit_message` | `Mirror source README` | Commit message for the README update. |
| `build_workers` | `2` | Parallel platform builds. zlib releases the GIL. |
| `max_asset_bytes` | `2147483648` | GitHub's 2 GiB per-asset ceiling. |
| `firmware.include` | `["*.zip"]` | Assets considered firmware. |
| `firmware.exclude` | long default list | Portable zips, APKs, DMGs, tarballs, etc. |
| `firmware.require_scatter` | `true` | Only repackage zips with a `*scatter*.txt`. |
| `releases.include_prereleases` | `true` | Mirror pre-releases too. |
| `releases.tags` | `[]` | Optional glob allow-list. |
| `releases.exclude_tags` | `[]` | Optional glob deny-list. |
| `platforms.<key>.updater_asset` | per platform | Exact asset name in `updater_repo`. |
| `platforms.<key>.updater_repo` | top-level `updater_repo` | Per-platform override; macOS uses `y1-community/innioasis-updater`. |
| `platforms.<key>.suffix` | per platform | Appended to the firmware asset base name. |
| `platforms.<key>.layout` | `sp_flash_tool` | `beside_dmg` for macOS. |
| `platforms.<key>.optional` | arm64 only | Skip silently until upstream publishes it. |
| `platforms.<key>.tag` | – | Pin a specific Updater CE release. |
| `platforms.<key>.enabled` | `true` | Set `false` to disable. |

## Product examples

**Original Software (`y1-community/y1-stock-rom`)** — the default config. One
repository publishes `rom.zip` (Y1 Type A), `rom_type_b.zip` (Y1 Type B) and
`rom_y2.zip` (Y2). The rules select model and type per asset.

**Rockbox Y1 (`rockbox-y1/rockbox`)** — `examples/rockbox-y1.json`. Releases
also carry `update.zip`, an SD-card update with no scatter; the workflow skips
it automatically and only packages `rom.zip` and `rom_type_b.zip`.

**Android/ATA Y1 (`y1-community/y1-ata-rom`)** — `examples/y1-ata-rom.json`.
Two firmware zips, same Type A/Type B split.

**Projects with a single model** — set `defaults.target_model` and drop the
rules. For example, a Y2-only repository uses `target_model: "Y2"`,
`target_type: "Stock"`.

## Updater CE version refresh

No extra configuration is needed. The per-release fingerprint includes the
exact portable app asset chosen for each platform, so a newly published
Updater CE build changes the fingerprint and causes a repackage. The newest
release containing that platform's asset wins; a release that only ships
Windows does not roll Linux or macOS backwards.

## When to re-run

- After the source repository publishes a release: the central watcher picks
  it up within 15 minutes, or the mirror's hourly fallback does. You can also
  run the workflow manually.
- After Updater CE or the flash-tool release publishes a new seed: the watcher
  dispatches every mirror within 15 minutes. The fingerprint includes the
  updater asset, so the next run rebuilds every release with the new app.
- After changing `mirror.config.json`: the config fingerprint changes and
  affected releases are rebuilt.

Rebuilding downloads the source firmware and the app again. Use `--tags` or
the workflow `tags` input to limit the blast radius.
