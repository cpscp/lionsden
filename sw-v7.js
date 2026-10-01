const CACHE_NAME = "lions-den-pwa-v7";
const APP_SHELL = [
  "./",
  "./index.html",
  "./manifest.json",
  "./icons/lions-den-final.svg",
  "./data/fixtures.json"
];

self.addEventListener("install", event => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then(cache => cache.addAll(APP_SHELL))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", event => {
  event.waitUntil(
    caches.keys()
      .then(keys =>
        Promise.all(
          keys
            .filter(key => key.startsWith("lions-den-pwa-") && key !== CACHE_NAME)
            .map(key => caches.delete(key))
        )
      )
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", event => {
  const request = event.request;
  if (request.method !== "GET") return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  // Data JSON is always fetched directly from the network and is NEVER
  // written to the Service Worker cache. The GitHub Actions pipeline
  // refreshes these files every 30 minutes, so stale data must not survive.
  if (url.pathname.includes("/data/") && url.pathname.endsWith(".json")) {
    event.respondWith(fetch(request, { cache: "no-store" }));
    return;
  }

  event.respondWith(
    fetch(request, { cache: "no-store" })
      .then(response => {
        if (response && response.ok) {
          const copy = response.clone();
          caches.open(CACHE_NAME).then(cache => cache.put(request, copy)).catch(() => {});
        }
        return response;
      })
      .catch(() =>
        caches.match(request).then(cached =>
          cached || caches.match("./index.html")
        )
      )
  );
});
