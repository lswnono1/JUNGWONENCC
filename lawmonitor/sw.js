const CACHE_PREFIX='jungwon-lawmonitor-pwa-';
const CACHE_NAME=CACHE_PREFIX+'v1.5.4';
const APP_SHELL=['./','./index.html','./recover-v1.5.4.html','./app-v1.5.4.js','./app.js','./app.js?v=1.4.0','./app.js?v=1.5.0','./app.js?v=1.5.1','./app.js?v=1.5.2','./styles.css','./manifest.webmanifest','./icon.svg','./maskable.svg','./release.json'];
self.addEventListener('install',e=>e.waitUntil(caches.open(CACHE_NAME).then(c=>c.addAll(APP_SHELL)).then(()=>self.skipWaiting())));
self.addEventListener('activate',e=>e.waitUntil(caches.keys().then(keys=>Promise.all(keys.filter(key=>key.startsWith(CACHE_PREFIX)&&key!==CACHE_NAME).map(k=>caches.delete(k)))).then(()=>self.clients.claim())));
self.addEventListener('fetch',event=>{
 const request=event.request,u=new URL(request.url),scope=new URL(self.registration.scope);
 if(request.method!=='GET'||u.origin!==scope.origin||!u.pathname.startsWith(scope.pathname))return;
 event.respondWith((async()=>{
  const c=await caches.open(CACHE_NAME);
  try{const r=await fetch(request);if(r.ok){const copy=r.clone();event.waitUntil(c.put(request,copy).catch(()=>{}));}return r;}
  catch(error){const stored=await c.match(request);if(stored)return stored;if(request.mode==='navigate'){const page=await c.match('./index.html');if(page)return page;}throw error;}
 })());
});
