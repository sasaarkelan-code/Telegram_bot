// Only public presentation assets can be cached. API, images and account data: network only.
const CACHE='studio-public-v2.6.0';
const ASSETS=['/assets/app.js','/assets/theme-init.js','/assets/style.css','/assets/aurel.css','/assets/icon.svg','/assets/icon-192.png','/assets/icon-512.png'];
self.addEventListener('install',event=>event.waitUntil(caches.open(CACHE).then(cache=>cache.addAll(ASSETS))));
self.addEventListener('activate',event=>event.waitUntil(caches.keys().then(keys=>Promise.all(keys.filter(k=>k.startsWith('studio-public-')&&k!==CACHE).map(k=>caches.delete(k))))));
self.addEventListener('fetch',event=>{
 const url=new URL(event.request.url);
 if(url.origin!==self.location.origin||event.request.method!=='GET'||url.pathname.startsWith('/api/'))return;
 if(ASSETS.includes(url.pathname)){
  event.respondWith(fetch(event.request).then(response=>{if(response.ok){const copy=response.clone();event.waitUntil(caches.open(CACHE).then(cache=>cache.put(event.request,copy)));}return response;}).catch(()=>caches.match(event.request)));return;
 }
 if(event.request.mode==='navigate')event.respondWith(fetch(event.request).catch(()=>new Response('<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Студия · нет соединения</title><body><h1>Нет соединения</h1><p>Подключись к интернету и открой Студию снова. Очередь и рассылки продолжают работать на сервере, если он доступен.</p></body></html>',{headers:{'Content-Type':'text/html; charset=utf-8','Cache-Control':'no-store'}})));
});
