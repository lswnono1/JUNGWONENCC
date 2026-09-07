const CACHE_PREFIX = 'jungwon-lawmonitor-pwa-';
const CACHE_NAME = `${CACHE_PREFIX}v1.5.1`;
const APP_SHELL = [
  './', './index.html', './styles.css', './app.js',
  // Existing index.html references 1.4.0. Keep that entry URL available offline.
  './app.js?v=1.4.0', './app.js?v=1.5.0', './app.js?v=1.5.1',
  './manifest.webmanifest', './icon.svg', './maskable.svg',
  ...Array.from({length:8}, (_, index) => `./app-core-${index + 1}.txt?v=1.5.1`),
  // core-7 retains this feed URL; preserve the existing notice relay contract.
  './data/notices.json?v=1.5.0'
];
self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(CACHE_NAME).then((cache) => cache.addAll(APP_SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', (event) => {
  event.waitUntil(caches.keys().then((keys) => Promise.all(
    keys.filter((key) => key.startsWith(CACHE_PREFIX) && key !== CACHE_NAME).map((key) => caches.delete(key))
  )).then(() => self.clients.claim()));
});
self.addEventListener('fetch', (event) => {
  const request = event.request;
  if (request.method !== 'GET') return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  const networkFirst = request.mode === 'navigate' || /\.(?:js|txt|css|webmanifest|json)$/i.test(url.pathname);
  if (networkFirst) {
    event.respondWith(fetch(request).then((response) => {
      if (response.ok) {
        const copy = response.clone();
        event.waitUntil(caches.open(CACHE_NAME).then((cache) => cache.put(request, copy)).catch(() => {}));
      }
      return response;
    }).catch(async () => {
      const cache = await caches.open(CACHE_NAME);
      const cached = await cache.match(request);
      if (cached) return cached;
      if (request.mode === 'navigate') return cache.match('./index.html');
      throw new Error('offline');
    }));
    return;
  }
  event.respondWith(caches.open(CACHE_NAME).then(async (cache) => {
    const cached = await cache.match(request);
    if (cached) return cached;
    const response = await fetch(request);
    if (response.ok) {
      const copy = response.clone();
      event.waitUntil(cache.put(request, copy).catch(() => {}));
    }
    return response;
  }));
});
