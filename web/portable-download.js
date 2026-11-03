/*
 * PortablePackages — resolve an Innioasis firmware download to its portable
 * flashing package on a mirror repository.
 *
 * Plain ES2017, no dependencies. Exposes window.PortablePackages.
 *
 * The mirror's index.json is served from raw.githubusercontent.com and lists:
 *   releases[].tag
 *   releases[].assets[].source_asset  (the original filename, e.g. "rom.zip")
 *   releases[].assets[].platform      (e.g. "windows-arm64")
 *   releases[].assets[].url
 *
 * Typical use:
 *
 *   var match = await PortablePackages.resolve({
 *     repos: PortablePackages.reposFromElement(button),
 *     tag: releaseTag,
 *     sourceAsset: zipName
 *   });
 *   if (match) PortablePackages.download(match.asset.url, zipName);
 *   else       fallbackToOriginalDownload();
 */
(function (global) {
    "use strict";

    var DEFAULT_BRANCH = "main";
    var INDEX_TTL_MS = 5 * 60 * 1000;
    var indexCache = Object.create(null);

    function normalizeRepo(value) {
        return String(value || "")
            .trim()
            .replace(/^https?:\/\/github\.com\//i, "")
            .replace(/\.git$/i, "")
            .replace(/^\/+|\/+$/g, "");
    }

    function parseRepos(value) {
        if (Array.isArray(value)) {
            return value.map(normalizeRepo).filter(Boolean);
        }
        return String(value || "")
            .split(/[\s,]+/)
            .map(normalizeRepo)
            .filter(Boolean);
    }

    function reposFromElement(element) {
        if (!element) return [];
        var raw =
            element.getAttribute("portable_zips") ||
            element.getAttribute("data-portable-zips") ||
            (element.dataset && element.dataset.portableZips) ||
            "";
        return parseRepos(raw);
    }

    function detectOS(userAgent, platform) {
        var ua = String(userAgent || "").toLowerCase();
        var plat = String(platform || "").toLowerCase();
        if (/iphone|ipad|ipod/.test(ua)) return "ios";
        if (/android/.test(ua)) return "android";
        if (plat.indexOf("mac") !== -1 || /mac os x|macintosh/.test(ua)) return "macos";
        if (plat.indexOf("win") !== -1 || /windows/.test(ua)) return "windows";
        if (plat.indexOf("linux") !== -1 || /linux|x11|cros/.test(ua)) return "linux";
        return "unknown";
    }

    function detectArch(userAgent, platform) {
        var ua = String(userAgent || "").toLowerCase();
        var plat = String(platform || "").toLowerCase();
        if (/arm64|aarch64|armv8/.test(ua) || /arm64|aarch64/.test(plat)) return "arm64";
        if (/riscv64|riscv/.test(ua)) return "riscv";
        return "x86-64";
    }

    function parsePlatformOverride(value) {
        var key = String(value || "").trim().toLowerCase();
        var table = {
            windows: { os: "windows", arch: "x86-64" },
            "windows-arm64": { os: "windows", arch: "arm64" },
            linux: { os: "linux", arch: "x86-64" },
            "linux-arm64": { os: "linux", arch: "arm64" },
            macos: { os: "macos", arch: "universal" },
            mac: { os: "macos", arch: "universal" },
            "macos-arm64": { os: "macos", arch: "universal" }
        };
        return table[key] || (key ? { os: key, arch: "x86-64" } : null);
    }

    async function detectPlatform() {
        var nav = global.navigator || {};
        var os = detectOS(nav.userAgent, nav.platform);
        var arch = detectArch(nav.userAgent, nav.platform);

        if (nav.userAgentData && typeof nav.userAgentData.getHighEntropyValues === "function") {
            try {
                var values = await nav.userAgentData.getHighEntropyValues(["architecture", "bitness"]);
                var reported = String((values && values.architecture) || "").toLowerCase();
                if (reported.indexOf("arm") !== -1) arch = "arm64";
                else if (reported.indexOf("x86") !== -1) arch = "x86-64";
            } catch (_) {
                /* high-entropy hints are optional */
            }
        }

        if (os === "macos") arch = "universal";
        return { os: os, arch: arch };
    }

    function platformOverride() {
        try {
            var params = new URLSearchParams(global.location.search || "");
            return parsePlatformOverride(params.get("platform"));
        } catch (_) {
            return null;
        }
    }

    function indexUrl(repo, branch) {
        return (
            "https://raw.githubusercontent.com/" +
            normalizeRepo(repo) +
            "/" +
            (branch || DEFAULT_BRANCH) +
            "/index.json"
        );
    }

    async function loadIndex(repo, branch, options) {
        options = options || {};
        var clean = normalizeRepo(repo);
        if (!clean) return null;
        var key = clean + "@" + (branch || DEFAULT_BRANCH);
        var now = Date.now();
        var cached = indexCache[key];
        if (!options.force && cached && now - cached.at < INDEX_TTL_MS) {
            return cached.data;
        }

        var fetchImpl = options.fetchImpl || global.fetch;
        if (typeof fetchImpl !== "function") return cached ? cached.data : null;

        var controller = null;
        var timer = null;
        var requestOptions = { credentials: "omit" };
        if (typeof global.AbortController === "function") {
            controller = new global.AbortController();
            requestOptions.signal = controller.signal;
            timer = global.setTimeout(function () {
                controller.abort();
            }, options.timeoutMs || 8000);
        }

        try {
            var response = await fetchImpl(indexUrl(clean, branch), requestOptions);
            if (!response || !response.ok) return cached ? cached.data : null;
            var data = await response.json();
            if (!data || data.schema !== 1 || !Array.isArray(data.releases)) {
                return cached ? cached.data : null;
            }
            indexCache[key] = { at: now, data: data };
            return data;
        } catch (_) {
            return cached ? cached.data : null;
        } finally {
            if (timer) global.clearTimeout(timer);
        }
    }

    function platformEntries(index, platform) {
        var list = Array.isArray(index.platforms) ? index.platforms : [];
        var matches = list.filter(function (entry) {
            if (!entry || entry.os !== platform.os) return false;
            return (
                entry.arch === platform.arch ||
                entry.arch === "universal" ||
                platform.arch === "universal"
            );
        });
        matches.sort(function (a, b) {
            if (a.arch === platform.arch && b.arch !== platform.arch) return -1;
            if (b.arch === platform.arch && a.arch !== platform.arch) return 1;
            return 0;
        });
        return matches;
    }

    function assetFor(release, platformEntry, sourceAsset) {
        var assets = Array.isArray(release.assets) ? release.assets : [];
        var i;
        for (i = 0; i < assets.length; i += 1) {
            if (
                assets[i] &&
                assets[i].platform === platformEntry.key &&
                assets[i].source_asset === sourceAsset
            ) {
                return assets[i];
            }
        }
        // Older indexes may omit source_asset; fall back to the naming rule.
        var base = String(sourceAsset || "").replace(/\.zip$/i, "");
        var expected = base + (platformEntry.suffix || "");
        for (i = 0; i < assets.length; i += 1) {
            if (assets[i] && assets[i].name === expected) return assets[i];
        }
        return null;
    }

    async function resolve(options) {
        options = options || {};
        var repos = parseRepos(options.repos);
        if (!repos.length || !options.tag || !options.sourceAsset) return null;

        var platform = options.platform || platformOverride() || (await detectPlatform());
        if (!platform || platform.os === "unknown") return null;

        for (var r = 0; r < repos.length; r += 1) {
            var index = await loadIndex(repos[r], options.branch, options);
            if (!index) continue;
            var entries = platformEntries(index, platform);
            if (!entries.length) continue;

            var releases = index.releases;
            for (var i = 0; i < releases.length; i += 1) {
                if (!releases[i] || releases[i].tag !== options.tag) continue;
                for (var e = 0; e < entries.length; e += 1) {
                    var asset = assetFor(releases[i], entries[e], options.sourceAsset);
                    if (asset) {
                        return {
                            repo: normalizeRepo(repos[r]),
                            index: index,
                            release: releases[i],
                            platformKey: entries[e].key,
                            asset: asset
                        };
                    }
                }
            }
        }
        return null;
    }

    function proxyUrlFor(proxyUrl, assetUrl, filename) {
        if (!proxyUrl) return assetUrl;
        var separator = proxyUrl.indexOf("?") === -1 ? "?" : "&";
        return (
            proxyUrl +
            separator +
            "url=" +
            encodeURIComponent(assetUrl) +
            "&filename=" +
            encodeURIComponent(filename)
        );
    }

    function download(assetUrl, filename, options) {
        options = options || {};
        var href = proxyUrlFor(options.proxyUrl, assetUrl, filename);
        var anchor = global.document.createElement("a");
        anchor.href = href;
        anchor.download = filename || "firmware.zip";
        anchor.rel = "noopener";
        if (options.target) anchor.target = options.target;
        global.document.body.appendChild(anchor);
        anchor.click();
        global.setTimeout(function () {
            if (anchor.parentNode) anchor.parentNode.removeChild(anchor);
        }, 1000);
        return href;
    }

    async function resolveAndDownload(options) {
        var match = await resolve(options);
        if (!match) return null;
        download(match.asset.url, options.sourceAsset, options);
        return match;
    }

    global.PortablePackages = {
        parseRepos: parseRepos,
        reposFromElement: reposFromElement,
        detectOS: detectOS,
        detectArch: detectArch,
        detectPlatform: detectPlatform,
        loadIndex: loadIndex,
        resolve: resolve,
        download: download,
        resolveAndDownload: resolveAndDownload,
        indexUrl: indexUrl
    };
})(typeof window !== "undefined" ? window : this);
