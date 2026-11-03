# Website integration plan

This is the plan for `innioasis.app/firmware.html` (the `Innioasis-Updater`
site). It replaces the in-browser JSZip packaging attempt, which cannot work
on a real web server because GitHub release assets are cross-origin and do not
send `Access-Control-Allow-Origin`.

The mirror repository does the packaging once, server-side. The site only has
to choose the right asset and hand it to the browser.

## 1. Add a mirror to the catalog

`firmware-catalog.js` entries currently carry `repo` (the source firmware
repository) and `packageName` (`rom.zip`, `rom_y2.zip`, …). Add one field:

```js
{
  "slug": "original-y1",
  "name": "Original Software",
  "model": "Y1",
  "repo": "y1-community/y1-stock-rom",
  "packageName": "rom.zip",
  "portable_zips": "you/y1-stock-rom-portable",
  ...
}
```

`portable_zips` is a comma-separated list so a project can have more than one
mirror (or a fallback mirror operated by someone else). The first mirror that
has a matching release and asset wins.

Keeping this per-catalog-entry means one mirror repository can serve
`rom.zip`, `rom_type_b.zip` and `rom_y2.zip` (they are all assets of the same
source release), while a different product points at a different mirror.

The existing `slidia_manifest.xml` does not need `portable_zips`; it is used by
the app, not the download buttons. If the app should also prefer portable
packages later, add the same attribute there and teach the app to read it.

## 2. Read the mirror index, not the GitHub API

Each mirror commits `index.json`:

```
https://raw.githubusercontent.com/<mirror>/<branch>/index.json
```

It lists every mirrored tag and, for each, the assets with `source_asset`,
`platform`, `size`, `sha256` and `url`. It also carries `source_title` and
`source_notes` (the unmodified GitHub release title and Markdown body), which
are the fields the static release-note pages should render. Fetch it directly;
do not call the GitHub API per page view (60 requests/hour per IP, and it would
need CORS preflights). Entries also carry `stats_url` for the cumulative
`stats.txt`; link it directly if the page wants to show download figures. It
is a release asset, so reading it cross-origin for display needs the proxy
described below. The top-level `source_readme` object points at the copied
source README and its raw URL.

Cache it in memory for five minutes and in `sessionStorage` for the tab's
lifetime. The raw CDN caches for a few minutes anyway.

Reference implementation: `web/portable-download.js` exposes
`window.PortablePackages`.

## 3. Detect the user's platform

Use `navigator.userAgentData.getHighEntropyValues(["architecture", "bitness"])`
when available (Chromium), then fall back to the user agent and
`navigator.platform`:

| OS | Arch | Platform key |
| --- | --- | --- |
| Windows | x86-64 | `windows` |
| Windows | arm64 | `windows-arm64` |
| Linux | x86-64 | `linux` |
| Linux | arm64 | `linux-arm64` |
| macOS | any | `macos` |

The module already does this and maps macOS to the universal key. Treat
unknown OS or architecture as "no portable package" and fall back.

Also accept a `?platform=windows` style override for support and testing.

## 4. Change the ZIP download button

Today `startSmartZipDownload(zipUrl, zipName, itemName, itemModel, releaseTag)`
downloads the source ZIP and, when running locally, merges it with an app ZIP
via JSZip. Replace that merge path with:

```
on click(zipUrl, zipName, item, releaseTag):
    repos = parseRepos(item.portable_zips || button.getAttribute("portable_zips"))
    if repos:
        match = PortablePackages.resolve({
            repos, tag: releaseTag,
            sourceAsset: zipName,      // e.g. "rom.zip"
            platform: await detectPlatform()
        })
        if match:
            PortablePackages.download(match.asset.url, zipName)
            return
    // fallback: current behaviour, direct download of the original zip
    downloadOriginal(zipUrl, zipName)
```

`resolve` should:

1. For each mirror in order, load its index.
2. Find `releases[]` with `tag === releaseTag` (exact match; tags are mirrored
   verbatim).
3. Find an asset with `source_asset === zipName` and `platform` equal to the
   detected platform key.
4. Return the asset or `null`.

If no mirror matches, the existing direct link stays the source of truth. No
new failure mode: the fallback is exactly what the page does today.

### Wiring example

```js
async function startSmartZipDownload(zipUrl, zipName, item, releaseTag) {
  const repos = window.PortablePackages.parseRepos(
    item && item.portable_zips
  );
  if (repos.length) {
    const match = await window.PortablePackages.resolve({
      repos,
      tag: releaseTag,
      sourceAsset: zipName
    });
    if (match) {
      window.PortablePackages.download(match.asset.url, zipName);
      return;
    }
  }
  // existing progress UI + direct download
  ...
}
```

Keep the existing progress panel for the fallback. For the portable path the
browser shows its own download UI; a short "Preparing portable package…"
message before navigation is enough. If changing the function signature is
awkward, leave `startSmartZipDownload` as it is and read the mirror list from
the button instead: `PortablePackages.reposFromElement(button)` checks both
`portable_zips` and `data-portable-zips`.

### Where to get `releaseTag`

The site already resolves releases per repository and renders a row per tag.
Pass the row's `tag_name` through to the click handler (it is currently passed
as `releaseTag`). If it is missing, skip the mirror lookup and use the direct
link.

## 5. The filename problem (important)

GitHub release assets are served from `release-assets.githubusercontent.com`
with `Content-Disposition: attachment; filename=<asset name>`. They do **not**
send `Access-Control-Allow-Origin`, so JavaScript cannot fetch the bytes and
save them under a different name. The HTML `download` attribute is also ignored
for cross-origin URLs.

Consequences:

- Without extra infrastructure, clicking a portable link saves
  `rom-windows.zip`, not `rom.zip`. The package is still correct; only the
  local filename differs.
- If the original filename is required (so users can drop the file where the
  updater expects `rom.zip`), deploy the optional same-origin proxy in
  `web/download-proxy-worker.js`. It streams the asset from GitHub with
  `Content-Disposition: attachment; filename="<original>"`, restricted to an
  allow-list of mirror repositories and site origins.

The helper supports both: pass `proxyUrl` to `PortablePackages.download` and it
will route through the proxy; omit it and it links directly.

Recommendation: start without the proxy. The portable ZIP is a self-contained
folder; its name does not matter to Updater CE. Add the proxy only if the
support load from "the file is called rom-windows.zip" becomes real.

## 6. Release titles and notes

Mirror release titles are prefixed with `Portable Flashing Package: `. The
site should use `source_title` from the index (or the catalog title), not the
mirror title, so the table does not change. The mirror title exists for people
browsing GitHub.

Use `source_notes` for the site's own release-note page. Do not scrape the
mirror release body: it has a provenance footer and a hidden build block after
the original content. Link “See more on GitHub” to `source_url`.

The site-side static page builder and the Updater CE post-install viewer are
specified in [static-release-pages.md](static-release-pages.md).

## 7. Testing checklist

- Catalog entry with no `portable_zips`: unchanged behaviour.
- Catalog entry with a mirror that has no matching tag: falls back to the
  original link.
- Catalog entry with a matching tag but no asset for the detected platform:
  falls back.
- Windows x64, Windows arm64, Linux x64, Linux arm64 and macOS each resolve to
  the right suffix.
- macOS user agent resolves to `macos` even though the architecture reports
  `arm64`.
- `?platform=windows` override works.
- Index fetch failure (offline, 404) logs once and falls back without blocking
  the download.

## 8. Rollout

1. Create and backfill one mirror (`y1-stock-rom`).
2. Add `portable_zips` to the matching catalog entries only.
3. Ship the helper and the button change behind a query parameter
   (`?portable=1`) for a day of testing.
4. Enable for everyone; keep the fallback permanently.
5. Add mirrors product by product.

The runtime download path requires no build step or new dependency: the helper
is plain ES2017 and the mirror index is static JSON. The crawlable pages are a
separate build-time concern, covered by section 9 and
[static-release-pages.md](static-release-pages.md).

## 9. Static pages, install guides and post-install viewers

The site also needs crawlable pages, not only the JavaScript download table:

- **Release notes pages** render `source_notes` with the site's Markdown
  styling and put portable download buttons beside the notes. They are the
  replacement for “View on GitHub”; a small “See more on GitHub” link still
  points at `source_url`.
- **Install guide pages** keep the existing guide layout. If the manifest entry
  has `post_install`, the final step links to the branded post-install page.
  Do not add that step for `post_install2`; it is already an Innioasis channel.
- **Post-install pages** have a notice-style layout, not a download layout.
  They render the Markdown file verbatim (tables, images, links, headings) with
  the site's styling and preferences. They deliberately do not push Updater CE;
  the user has already installed the firmware.
- **Updater CE viewer** accepts a Markdown URL from `post_install` and opens the
  branded viewer. Non-Markdown links open normally. The app does not need a
  manifest edit for each new document.

The separate `innioasis-updater` workflow, routes, Markdown normalization and
SEO requirements are in [static-release-pages.md](static-release-pages.md).
