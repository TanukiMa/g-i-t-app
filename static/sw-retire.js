/* G醫t: the installable app (PWA) is switched off (PWA_ENABLED=0). This file is published as /sw.js in its place.
 *
 * Browsers that installed the app earlier still have the old service worker. They look for a new /sw.js whenever they open a
 * page of the site, find this one, and it removes everything the old worker kept: the cached copies, and itself.
 * There is no fetch handler, so after that every request simply goes to the network, as on any ordinary web page.
 * (An app icon the visitor added to a home screen or a start menu stays until they remove it; it just opens the site.)
 */
self.addEventListener("install", () => { self.skipWaiting(); });

self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    try {
      for (const name of await caches.keys()) await caches.delete(name);
    } catch (e) { /* nothing to clean */ }
    try {
      await self.registration.unregister();
      const windows = await self.clients.matchAll({ type: "window" });
      windows.forEach((client) => { try { client.navigate(client.url); } catch (e) { /* the page is simply not reloaded */ } });
    } catch (e) { /* ignore */ }
  })());
});
