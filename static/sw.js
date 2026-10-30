/* Unlock service worker
   - Met en cache uniquement l'interface (CSS, JS, polices, icônes).
   - Ne met JAMAIS en cache les données Strava (/api/*) : la Strava API Policy
     limite leur conservation et elles doivent pouvoir être supprimées à tout moment. */
const VERSION = 'unlock-2026.10.21';
const SHELL = [
  '/offline',
  '/static/css/app.css?v=2026.10.21',
  '/static/js/app.js?v=2026.10.21',
  '/static/fonts/sora.woff2',
  '/static/img/logo.svg',
  '/static/icons/icon-192.png',
  '/static/vendor/leaflet/leaflet.css',
  '/static/vendor/leaflet/leaflet.js'
];

self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(VERSION).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== VERSION).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;           // tuiles de carte, Strava…
  if (url.pathname.startsWith('/api/')) return;               // données : toujours le réseau
  if (['/auth', '/callback', '/logout'].includes(url.pathname)) return;

  // Pages : réseau d'abord, page hors-ligne en secours
  if (req.mode === 'navigate') {
    event.respondWith(fetch(req).catch(() => caches.match('/offline')));
    return;
  }

  // Fichiers statiques : cache d'abord, mis à jour en arrière-plan
  if (url.pathname.startsWith('/static/')) {
    event.respondWith(
      caches.open(VERSION).then(async (cache) => {
        const hit = await cache.match(req);
        const net = fetch(req).then((res) => { if (res.ok) cache.put(req, res.clone()); return res; }).catch(() => hit);
        return hit || net;
      })
    );
  }
});
