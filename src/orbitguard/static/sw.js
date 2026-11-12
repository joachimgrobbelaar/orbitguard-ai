/* OrbitGuard Service Worker — App shell caching only.
 *
 * Scientific honesty constraint:
 * Telemetry, catalog states, environment indices, and tracking-loop endpoints
 * (/api/* and /health) are NEVER cached or served from cache. When the network
 * is unreachable, /api/ requests return HTTP 503 JSON stating that OrbitGuard
 * does not show cached telemetry as live.
 */

const CACHE_NAME = "orbitguard-shell-v1";

const APP_SHELL_URLS = [
  "/",
  "/?source=pwa",
  "/manifest.webmanifest",
  "/apple-touch-icon.png",
  "/icons/icon-192.png",
  "/icons/icon-512.png",
  "/icons/icon-512-maskable.png",
  "/icons/icon.svg"
];

const OPPORTUNISTIC_CDN_URLS = [
  "https://cdn.jsdelivr.net/npm/three@0.128.0/build/three.min.js",
  "https://cdn.jsdelivr.net/npm/three@0.128.0/examples/js/controls/OrbitControls.js"
];

const OFFLINE_API_PAYLOAD = JSON.stringify({
  error: "offline",
  detail: "No network; OrbitGuard does not show cached telemetry as live."
});

function isApiPath(pathname) {
  return pathname === "/api" || pathname.startsWith("/api/") || pathname === "/health";
}

function isStaticOrCdnAsset(url) {
  if (url.origin === self.location.origin) {
    return (
      url.pathname.startsWith("/icons/") ||
      url.pathname === "/apple-touch-icon.png" ||
      url.pathname === "/manifest.webmanifest"
    );
  }
  return OPPORTUNISTIC_CDN_URLS.indexOf(url.href) !== -1;
}

self.addEventListener("install", (event) => {
  self.skipWaiting();
  event.waitUntil(
    caches.open(CACHE_NAME).then(async (cache) => {
      await cache.addAll(APP_SHELL_URLS);
      await Promise.allSettled(
        OPPORTUNISTIC_CDN_URLS.map((cdnUrl) =>
          fetch(cdnUrl, { mode: "cors" })
            .then((response) => {
              if (response && response.ok) {
                return cache.put(cdnUrl, response);
              }
              return null;
            })
            .catch(() => null)
        )
      );
    })
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(
          keys
            .filter((key) => key !== CACHE_NAME)
            .map((oldKey) => caches.delete(oldKey))
        )
      )
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  const url = new URL(request.url);

  // Never cache or serve cached responses for /api/ or /health endpoints.
  if (url.origin === self.location.origin && isApiPath(url.pathname)) {
    event.respondWith(
      fetch(request).catch(
        () =>
          new Response(OFFLINE_API_PAYLOAD, {
            status: 503,
            statusText: "Service Unavailable",
            headers: {
              "Content-Type": "application/json",
              "Cache-Control": "no-store"
            }
          })
      )
    );
    return;
  }

  if (request.method !== "GET") {
    return;
  }

  // Network-first for "/" and HTML navigations; fall back to cached app shell offline.
  if (
    url.origin === self.location.origin &&
    (url.pathname === "/" || request.mode === "navigate")
  ) {
    event.respondWith(
      fetch(request)
        .then((response) => {
          if (response && response.ok) {
            const copyForRoot = response.clone();
            const copyForReq = response.clone();
            caches.open(CACHE_NAME).then((cache) => {
              cache.put("/", copyForRoot);
              cache.put(request, copyForReq);
            });
          }
          return response;
        })
        .catch(async () => {
          const cachedReq = await caches.match(request);
          if (cachedReq) return cachedReq;
          const cachedRoot = await caches.match("/");
          if (cachedRoot) return cachedRoot;
          return new Response("Offline — OrbitGuard app shell unavailable.", {
            status: 503,
            headers: { "Content-Type": "text/plain; charset=utf-8" }
          });
        })
    );
    return;
  }

  // Cache-first for static icons, manifest, and Three.js CDN scripts.
  if (isStaticOrCdnAsset(url)) {
    event.respondWith(
      caches.match(request).then((cached) => {
        if (cached) {
          return cached;
        }
        return fetch(request).then((response) => {
          if (response && response.ok) {
            const copy = response.clone();
            caches.open(CACHE_NAME).then((cache) => cache.put(request, copy));
          }
          return response;
        });
      })
    );
  }
});
