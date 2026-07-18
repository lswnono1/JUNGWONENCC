const CACHE_NAME = 'jungwon-lawmonitor-pwa-v1.4.0';
const APP_SHELL = [
  './', './index.html', './styles.css', './app.js?v=1.4.0',
  './manifest.webmanifest', './icon.svg', './maskable.svg',
  './app-core-1.txt?v=1.4.0', './app-core-2.txt?v=1.4.0', './app-core-3.txt?v=1.4.0',
  './app-core-4.txt?v=1.4.0', './app-core-5.txt?v=1.4.0', './app-core-6.txt?v=1.4.0'
];
self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(CACHE_NAME).then((cache) => cache.addAll(APP_SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', (event) => {
  event.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key)))).then(() => self.clients.claim()));
});
self.addEventListener('fetch', (event) => {
  const request = event.request;
  if (request.method !== 'GET') return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  const networkFirst = request.mode === 'navigate' || /\.(?:js|txt|css|webmanifest)$/i.test(url.pathname);
  if (networkFirst) {
    event.respondWith(fetch(request).then((response) => {
      if (response.ok) caches.open(CACHE_NAME).then((cache) => cache.put(request, response.clone()));
      return response;
    }).catch(async () => {
      const cached = await caches.match(request);
      if (cached) return cached;
      if (request.mode === 'navigate') return caches.match('./index.html');
      throw new Error('offline');
    }));
    return;
  }

  event.respondWith(caches.match(request).then((cached) => cached || fetch(request).then((response) => {
    if (response.ok) caches.open(CACHE_NAME).then((cache) => cache.put(request, response.clone()));
    return response;
  })));
});
