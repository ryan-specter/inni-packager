# Static release pages and post-install viewer

This is the implementation contract for the separate `innioasis-updater` website
repository. The packager in this repository supplies the data; the site owns the
templates, styling, preferences and deployment.

The goal is to stop sending users to GitHub for release notes and post-install
instructions. Every firmware release gets a crawlable page with the site's own
layout and portable download buttons, while the GitHub release remains one
click away for provenance.

## 1. What the mirror now provides

Every mirror `index.json` contains:

```json
{
  "source_repo": "y1-community/y1-stock-rom",
  "mirror_repo": "y1-community/y1-stock-rom-mirror",
  "source_readme": {
    "path": "README.md",
    "source_path": "README.md",
    "source_url": "https://github.com/y1-community/y1-stock-rom/blob/main/README.md",
    "url": "https://raw.githubusercontent.com/y1-community/y1-stock-rom-mirror/main/README.md"
  },
  "releases": [
    {
      "tag": "3.1.2",
      "title": "Portable Flashing Package: Original System Software 3.1.2",
      "source_title": "Original System Software 3.1.2",
      "source_notes": "<the source release body, unmodified>",
      "source_url": "https://github.com/y1-community/y1-stock-rom/releases/tag/3.1.2",
      "published_at": "2025-06-01T12:00:00Z",
      "prerelease": false,
      "assets": [
        {
          "name": "rom-windows.zip",
          "source_asset": "rom.zip",
          "platform": "windows",
          "size": 423456789,
          "sha256": "…",
          "url": "https://github.com/…/releases/download/3.1.2/rom-windows.zip"
        }
      ],
      "original_assets": ["rom.zip", "updater.jpg"],
      "stats_url": "https://github.com/…/releases/download/3.1.2/stats.txt"
    }
  ]
}
```

Use `source_title` and `source_notes`. Do not parse the mirror release body: it
starts with generated image buttons and ends with an invisible
`portable-mirror` block around the original notes. Older index entries created
before this field existed may lack it; fetch the source release from
`source_url` until the mirror backfill refreshes them.

The site also keeps its existing `firmware-catalog.js` / manifest entries
(`repo`, `packageName`, `post_install`, `post_install2`, model, version). The
index is the join key: match its `tag` and `source_asset` to the catalog entry.

## 2. Page types and layouts

Three page types deliberately do different jobs.

### Release notes page

Route: `/firmware/{product}/{tag}/release-notes/`

Existing query-style equivalent:
`release_notes.html?repo={owner/repo}&tag={tag}`

Layout:

1. Product name, model, firmware version, release date and prerelease badge.
2. Markdown body from `source_notes` using the site's normal article styles:
   headings, tables, code, images, blockquotes and links.
3. A compact “See more on GitHub” link to `source_url`.
4. Portable download buttons for the detected platform, with the normal
   fallback to the original `packageName` asset when no mirror asset matches.
5. A link to the install guide (and, when applicable, the post-install page).

The page is a **download and install** page. It should look like a normal
release/article page, not a full-screen notice. It replaces the GitHub button
with the site's own page while retaining the GitHub link as secondary
provenance.

### Install guide page

Route: `/firmware/{product}/{tag}/install/`

Keep the existing step-by-step guide. If the manifest entry has
`post_install`, append one final step:

> **After installing:** view the post-install instructions

That button links to the branded post-install page. Do not add this step for
`post_install2`; it is already an Innioasis-controlled channel and does not need
the generic viewer.

The install guide is where Updater CE guidance belongs. Release notes pages can
link to it, but should not repeat the whole pitch.

### Post-install page

Route: `/firmware/{product}/{tag}/post-install/`

Existing query-style equivalent:
`post_install.html?repo={owner/repo}&tag={tag}&src={encoded URL}`

Layout:

- The same article/notice typography as `notices.html`, with the site header,
  footer, theme and user preferences.
- The Markdown document rendered verbatim: headings, paragraphs, lists,
  tables, images, links, code blocks and blockquotes.
- Donation and preference choices remain available, but the page must not
  contain download buttons or an Updater CE call to action. The user has
  already installed the firmware.
- A small attribution/source link if the document came from a GitHub blob.

This page is a **reading** page. It is not a release/download page.

## 3. Reading `post_install` from the manifest

For each `<package>` in `slidia_manifest.xml`:

- If `post_install` is absent, no post-install page is generated.
- If `post_install` is present, it is the source URL for that product's
  post-install page.
- `post_install2` is excluded from this pipeline.
- The URL may point at:
  - `https://github.com/{owner}/{repo}/blob/{ref}/{path}.md`
  - `https://github.com/{owner}/{repo}/raw/{ref}/{path}.md`
  - `https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{path}.md`
  - a Gist or another host that serves Markdown directly
  - a normal HTML page

Normalize and fetch **server-side** during the static build, not in the user's
browser:

1. Convert GitHub `/blob/` URLs to `raw.githubusercontent.com`.
2. Follow redirects.
3. Require HTTPS.
4. Accept only `text/markdown`, `text/plain`, or a `.md`/`.markdown` response.
   If the URL is HTML or an unknown type, leave it as an ordinary external link.
5. Resolve relative images and links against the document's final URL.
6. Cache by final URL plus ETag/Last-Modified. Never fetch the same document
   once per release.
7. Strip YAML front matter from display, but keep the body verbatim otherwise.

If `post_install` changes in the manifest, the next scheduled build regenerates
the page. Historical releases can share the current post-install document;
that is preferable to freezing a stale copy unless the manifest provides
per-release links.

## 4. Updater CE deep link

Updater CE should open the branded viewer for Markdown `post_install` links:

```
https://innioasis.app/post_install.html?src=<encoded absolute Markdown URL>
```

The app only needs to detect that the link is Markdown-like and wrap it. It
must not rewrite `post_install2`.

The viewer page:

- reads `src`, validates `https:` and the site's host allow-list;
- fetches the Markdown through the site's same-origin fetch/render endpoint;
- renders it with the same sanitizer and stylesheet as the static post-install
  page;
- opens non-Markdown URLs normally;
- shows a clear error with a “Open original” fallback if the fetch fails.

The static build and the viewer should share one Markdown normalizer and one
renderer. Do not maintain two divergent implementations.

## 5. Static generation workflow

Add a workflow to `innioasis-updater`, not to this packager repository.

Triggers:

- `schedule` (for example every six hours);
- `workflow_dispatch`;
- `repository_dispatch` (`firmware-release-published`) from a mirror after a
  successful release run, if desired;
- `push` to the site's templates/styles.

Job outline:

1. Check out the site repository.
2. Read `slidia_manifest.xml` and the firmware catalog.
3. Fetch each source repository's release list (or the mirror `index.json` when
   it already has `source_notes`). Exclude drafts; include prereleases according
   to the catalog.
4. For each release, generate:
   - release-notes page from `source_title` + `source_notes`;
   - install-guide page from the existing guide template;
   - post-install page when `post_install` exists.
5. Fetch and normalize the `post_install` Markdown once per product, not once
   per release.
6. Resolve portable downloads from each mirror's `index.json`. Emit the same
   platform detection and original-asset fallback as the runtime helper.
7. Sanitize the rendered HTML and write static files with content hashes.
8. Update the sitemap, RSS/JSON feed, `llms.txt` and search index.
9. Commit the generated output, or deploy it to the site's static host.

The job must be idempotent. If the source notes, post-install document and
mirror index are unchanged, it should not create a new commit or deployment.

### Recommended output paths

```
/firmware/{product}/{tag}/release-notes/index.html
/firmware/{product}/{tag}/install/index.html
/firmware/{product}/{tag}/post-install/index.html
/firmware/{product}/index.html
```

`{product}` should be the catalog slug, not the raw repository name. Keep the
existing `release_notes.html` and `post_install.html` URLs as redirects or
client-side entry points for links already in the wild.

## 6. Download buttons on static release pages

The static pages must not hard-code GitHub asset names. Reuse the resolver in
`web/portable-download.js` or port its rules into the build:

1. detect OS and architecture (macOS always `macos`; arm64 keys where present);
2. find the mirror release by exact tag;
3. find an asset with matching `source_asset` and `platform`;
4. fall back to the original `packageName` asset URL.

The release page should include:

- a primary “Download for {detected platform}” button;
- a small platform picker for the other available variants;
- an “Original {packageName}” fallback link;
- the file size and SHA-256 from the index, when available.

Do not show a portable button for a platform that has no asset. Do not make the
user leave the release page to discover the download.

## 7. SEO and RAG output

Every release page should have:

- a unique `<title>` and meta description built from product, model, version
  and a plain-text excerpt of the notes;
- a canonical URL;
- Open Graph and Twitter card metadata;
- JSON-LD (`SoftwareApplication` or `TechArticle`, matching the page type);
- visible publication and update dates;
- stable static HTML, no client-side-only content for the core notes.

Add:

- `/sitemap.xml` including every generated release, install and post-install
  page;
- a JSON feed of releases with `title`, `url`, `source_url`, `tag`, `product`,
  `published_at` and plain-text notes;
- `llms.txt` pointing at the feed and the canonical page patterns;
- `robots.txt` allowing the generated pages;
- a `noindex` policy for duplicate query-string entry points, if both query and
  static URLs remain.

Do not duplicate the same notes across several routes without canonical URLs.
Do not put raw HTML from GitHub into the page without sanitizing it.

## 8. Markdown rendering requirements

Use the site's existing notice renderer where possible. It must support:

- headings and anchors;
- paragraphs, emphasis, strong text and inline code;
- ordered/unordered lists and nested lists;
- fenced code blocks with language classes;
- tables;
- blockquotes;
- images with alt text, lazy loading and responsive sizing;
- absolute and relative links;
- GitHub-flavored line breaks where the source uses them.

Normalize:

- GitHub `/blob/` links to their rendered page or raw content as appropriate;
- relative links and images against the final document URL;
- protocol-relative URLs to HTTPS;
- `target="_blank"` plus `rel="noopener noreferrer"` for external links;
- image URLs through the site's image proxy/cache if one exists.

Reject or strip:

- `<script>`, `<iframe>`, `<object>`, event-handler attributes and inline
  JavaScript URLs;
- unknown HTML that the sanitizer does not explicitly allow.

## 9. Acceptance checklist

- A release with `source_notes` renders the original Markdown, not the mirror's
  hidden build block.
- “See more on GitHub” opens `source_url` in a new tab.
- Windows x64, Windows arm64, Linux x64, Linux arm64 and macOS each resolve to
  the right portable asset; an unavailable platform falls back cleanly.
- A release with no mirror match still offers the original firmware ZIP.
- An install guide with `post_install` gets a final post-install button; one
  with only `post_install2` does not.
- Post-install pages use the notice layout and contain no download/Updater CE
  call to action.
- GitHub blob, raw, relative-image and table examples render correctly.
- Non-Markdown `post_install` links open normally.
- Re-running the generator with unchanged inputs produces no diff.
- Every generated page has canonical metadata and appears in the sitemap.
- Generated HTML contains no unsanitized script from a source document.

## 10. Rollout

1. Point the generator at one product with a known `post_install` (for example
   Rondo) and one release.
2. Compare the rendered page against the source Markdown and the existing
   notices page.
3. Add the portable download resolver and verify every platform.
4. Generate history for that product, then add products one at a time.
5. Add the Updater CE deep link only after the viewer and sanitizer are shared
   with the static page implementation.
6. Enable `repository_dispatch` from mirror runs once the scheduled build is
   stable.
