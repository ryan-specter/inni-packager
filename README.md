# inni-packager

Mirrors releases of Innioasis and Timmkoo firmwares and provides pre-packaged
zips with portable flashing application binaries for various OSes and
architectures.

| System | Package |
| --- | --- |
| Windows 10/11 (64-bit, x86-64 or ARM64) | Innioasis Updater CE |
| Windows XP–8.1 and 32-bit Windows | SP Flash Tool only |
| Linux 64-bit (x86-64, ARM64) | Innioasis Updater CE, MTKclient and SP Flash Tool |
| 32-bit Linux (pre-AppImage) | SP Flash Tool only |
| macOS 12+ | Innioasis Updater CE |

## Automatic package rebuilds

`.github/workflows/onboard.yml` polls every 15 minutes. It fingerprints the
release assets of every source repository in `slidia_manifest.xml`, plus the
portable application seed releases in `y1-community/updater-ce-neo` and
`y1-community/Innioasis-Updater`. A new release, a replaced asset, a changed
seed, or a changed manifest row dispatches only the mirrors that need work.
The first run dispatches every mirror. State is stored in
`.github/mirror-watch-state.json` only after a clean apply.

Onboarding also adds or refreshes the `portable_zips` attribute on matching
`type="img"` manifest rows, so a new firmware only needs its line in
`slidia_manifest.xml`. Each mirror also keeps an hourly schedule as a fallback
if the central watcher is delayed or disabled.

## Legacy packages

Windows systems that cannot run Updater CE get `<rom>-windows-legacy.zip`;
32-bit Linux gets `<rom>-linux-legacy.zip`. Both are the same firmware packed
beside the standalone SP Flash Tool, without the app. They are built from the
`flash_tool_windows.zip` and `flash_tool_linux.zip` assets on
`y1-community/Innioasis-Updater`'s `flash_tool` release, and carry `donate.txt`
plus a `README.txt` guide.

The guide follows the flow those systems need:

1. extract the zip, install the driver from the package, and reboot
2. open the Flash Tool folder and launch the tool
3. keep the player powered off and disconnected, click Download, then connect it
4. wait for the completion message before unplugging

The guide is generated per package, so the brand, model, hardware type, firmware
file and scatter named in it are the ones in that package. The same text powers
the static pages under `docs/guides/legacy`; regenerate them with:

```bash
python tools/install_guides.py
```

The website links a detected legacy system to its own guide only. Windows 7
users are not shown the Updater CE guide, and Updater CE users are not shown the
legacy guide.

## Dynamic download links

Every download link is resolved at runtime from the mirror `index.json`, never
hard-coded per operating system. The site recommends the package that matches
the detected system but keeps every option visible:

- a browser that reports Windows XP/Vista/7/8/8.1 or 32-bit Windows is
  recommended `windows-legacy` and has Updater CE hidden behind its guide link
- a 32-bit Linux browser is recommended `linux-legacy`
- an unknown or unsupported user agent leaves the recommendation empty and
  expands the full list instead of hiding it

`web/portable-download.js` implements this. `resolve()` returns `recommended`
plus `options`; the page must render both, with “Show all options” available
even when a recommendation exists.
