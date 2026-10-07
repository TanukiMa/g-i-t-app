/* G医t service worker.
 *
 * Goal: installable app + a readable offline copy, WITHOUT ever hiding a newer version when online.
 *   - HTML pages ... network first (a short timeout when a copy exists), the copy is only a fallback.
 *   - CSS / JS / icons ... cached copy first, refreshed in the background (stale-while-revalidate).
 *   - Everything else (Atom feeds, diff pages, other domains such as Google / Cloudflare / Wayback) is not touched.
 * __BUILD_ID__ is replaced at build time, so every deployment installs a fresh worker and a fresh asset cache.
 */
const BUILD = "__BUILD_ID__";
const STATIC_CACHE = "git-static-" + BUILD;   // replaced on every deployment
const PAGE_CACHE = "git-pages-v2";            // offline copies of pages the reader opened (kept across deployments; v2: keys without index.html)
const PAGE_LIMIT = 60;                        // most recent pages kept
const WAIT_FOR_NETWORK_MS = 4000;             // with a copy available, fall back to it after this long

const ASSET_VERSION = "__ASSET_VERSION__";   // the pages ask for assets/xxx?v=<this>: same URL, same cache entry
const SHELL = [
  "offline.html",
  "assets/github.css?v=" + ASSET_VERSION, "assets/dashboard.css?v=" + ASSET_VERSION,
  "assets/minimal.css?v=" + ASSET_VERSION, "assets/common.css?v=" + ASSET_VERSION,
  "assets/app.js?v=" + ASSET_VERSION, "assets/pwa.js?v=" + ASSET_VERSION,
  "assets/icons/icon-192.png",
];
const PRECACHE_PAGES = [""];   // "" = the scope itself (the timeline)

const scopeUrl = (path) => new URL(path, self.registration.scope).href;

// The offline copy is keyed by path only: a shared "?follow=..." list must never be stored.
// "/index.html" and "/" are the same page: one key (a directory URL ends with "/").
const pageKey = (url) => { const u = new URL(url); return u.origin + u.pathname.replace(/index\.html$/, ""); };

self.addEventListener("install", (event) => {
  event.waitUntil((async () => {
    const statics = await caches.open(STATIC_CACHE);
    await Promise.allSettled(SHELL.map((p) => statics.add(scopeUrl(p))));
    const pages = await caches.open(PAGE_CACHE);
    await Promise.allSettled(PRECACHE_PAGES.map((p) => pages.add(new Request(scopeUrl(p)))));
    await self.skipWaiting();
  })());
});

self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    const names = await caches.keys();
    await Promise.all(names
      .filter((n) => (n.startsWith("git-static-") && n !== STATIC_CACHE) || (n.startsWith("git-pages-") && n !== PAGE_CACHE))
      .map((n) => caches.delete(n)));
    await self.clients.claim();
  })());
});

async function trim(cache) {
  const keys = await cache.keys();                       // oldest first
  await Promise.all(keys.slice(0, Math.max(0, keys.length - PAGE_LIMIT)).map((k) => cache.delete(k)));
}

function withTimeout(promise, ms) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("timeout")), ms);
    promise.then((v) => { clearTimeout(timer); resolve(v); }, (e) => { clearTimeout(timer); reject(e); });
  });
}

async function pageNetworkFirst(request) {
  const cache = await caches.open(PAGE_CACHE);
  const key = pageKey(request.url);
  const cached = await cache.match(key);
  const network = fetch(request).then((response) => {
    if (response.ok && response.type === "basic") {
      cache.put(key, response.clone()).then(() => trim(cache));
    }
    return response;
  });
  try {
    // Without a copy there is nothing to fall back to, so wait for the network as long as it takes.
    return await (cached ? withTimeout(network, WAIT_FOR_NETWORK_MS) : network);
  } catch (e) {
    network.catch(() => {});                              // a late response still refreshes the copy
    if (cached) return cached;
    const offline = await (await caches.open(STATIC_CACHE)).match(scopeUrl("offline.html"));
    return offline || Response.error();
  }
}

async function assetStaleWhileRevalidate(request) {
  const cache = await caches.open(STATIC_CACHE);
  const cached = await cache.match(request);
  const refresh = fetch(request).then((response) => {
    if (response.ok && response.type === "basic") cache.put(request, response.clone());
    return response;
  });
  if (cached) { refresh.catch(() => {}); return cached; }
  return refresh;
}

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET" || request.headers.has("range")) return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;       // never touch other domains
  if (!url.href.startsWith(self.registration.scope)) return;
  const path = url.pathname;

  if (path.includes("/feeds/") || /\/diff_[^/]*\.html$/.test(path)) return;   // feeds: always live; diffs: large

  if (request.mode === "navigate") {
    event.respondWith(pageNetworkFirst(request));
  } else if (path.includes("/assets/")) {
    event.respondWith(assetStaleWhileRevalidate(request));
  }
});
