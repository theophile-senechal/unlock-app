/* UNLOCK : mesure d'usage (comment l'appli est utilisée, jamais de donnée Strava).
   Chargé seulement pour une personne connectée qui n'a pas refusé la mesure, jamais pour l'admin.
   - une session = un onglet, jusqu'à 30 minutes sans activité (sessionStorage, rien de durable)
   - temps actif : onglet visible et une interaction dans la dernière minute (ou une animation regardée)
   - envoi groupé toutes les 20 s, et à la fermeture de la page (sendBeacon) */
(function () {
  'use strict';
  const U = (window.Unlock = window.Unlock || {});
  const CFG = window.UNLOCK_TRACK;
  if (!CFG || !window.JSON) return;

  const PAGES = { '/': 'carte', '/lieux': 'lieux', '/timelapse': 'timelapse', '/story': 'story', '/stats': 'stats',
    '/devine': 'devine', '/annee': 'annee', '/cgu': 'cgu', '/confidentialite': 'confidentialite', '/mentions-legales': 'mentions' };
  const PAGE = PAGES[location.pathname] || 'autre';
  const IDLE = 60000, GAP = 30 * 60000, EVERY = 15000;

  /* ---------- Session ---------- */
  const store = {
    get(k) { try { return sessionStorage.getItem(k); } catch (e) { return null; } },
    set(k, v) { try { sessionStorage.setItem(k, v); } catch (e) {} }
  };
  function newSid() {
    try { const a = crypto.getRandomValues(new Uint32Array(2)); return (a[0] & 0x1fffff) * 4294967296 + a[1] + 1; }
    catch (e) { return Math.floor(Math.random() * 9e15) + 1; }
  }
  let sid = +store.get('u.sid') || 0;
  const last = +store.get('u.last') || 0;
  const fresh = !sid || Date.now() - last > GAP;
  if (fresh) { sid = newSid(); store.set('u.sid', String(sid)); }
  const touch = () => store.set('u.last', String(Date.now()));
  touch();

  /* ---------- Appareil ---------- */
  const ua = navigator.userAgent || '';
  const touchMac = navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1;
  const os = /iphone|ipad|ipod/i.test(ua) || touchMac ? 'ios' : /android/i.test(ua) ? 'android' : /cros/i.test(ua) ? 'chromeos'
    : /windows/i.test(ua) ? 'windows' : /mac os x|macintosh/i.test(ua) ? 'macos' : /linux/i.test(ua) ? 'linux' : 'autre';
  const tablet = /ipad|tablet/i.test(ua) || touchMac || (/android/i.test(ua) && !/mobile/i.test(ua));
  const dev = tablet ? 'tablette' : /mobi|iphone|ipod|android/i.test(ua) ? 'telephone' : 'ordinateur';
  const nav = /instagram/i.test(ua) ? 'instagram' : /fban|fbav|fb_iab/i.test(ua) ? 'facebook' : /samsungbrowser/i.test(ua) ? 'samsung'
    : /edg\//i.test(ua) ? 'edge' : /opr\/|opera/i.test(ua) ? 'opera' : /firefox|fxios/i.test(ua) ? 'firefox'
    : /chrome|crios|chromium/i.test(ua) ? 'chrome' : /safari/i.test(ua) ? 'safari' : 'autre';
  const pwa = window.matchMedia('(display-mode: standalone)').matches || navigator.standalone === true;
  // Site d'où vient la personne (premier passage, gardé par app.js avant la connexion Strava)
  const ref = store.get('u.ref') || '';

  /* ---------- File d'envoi ---------- */
  let queue = [], active = 0, ctxSent = false, watching = 0;
  const clean = (d) => (d == null ? null : String(d).toLowerCase().normalize('NFD').replace(/[̀-ͯ]/g, '').replace(/[^a-z0-9_-]+/g, '_').slice(0, 24) || null);
  function track(ev, opt) {
    opt = opt || {};
    if (queue.length >= 80) return;
    queue.push([ev, clean(opt.d), opt.v == null || !isFinite(opt.v) ? null : Math.round(opt.v), performance.now()]);
    touch();
  }
  function payload() {
    const now = performance.now();
    const body = { c: CFG.csrf, s: sid, p: PAGE, a: active, e: queue.map((e) => [e[0], e[1], e[2], Math.max(0, Math.round(now - e[3]))]) };
    if (!ctxSent) body.x = { dev, os, nav, pwa, w: Math.round(window.innerWidth), ref };
    return body;
  }
  function flush(beacon) {
    if (!queue.length && active < 1) return;
    const body = JSON.stringify(payload());
    queue = []; active = 0; ctxSent = true;
    try {
      // text/plain : seul type accepté par tous les navigateurs pour sendBeacon (le serveur lit le JSON quand même)
      if (beacon && navigator.sendBeacon && navigator.sendBeacon('/api/u', new Blob([body], { type: 'text/plain;charset=UTF-8' }))) return;
      fetch('/api/u', { method: 'POST', body, keepalive: true, credentials: 'same-origin', headers: { 'Content-Type': 'application/json' } }).catch(() => {});
    } catch (e) {}
  }

  /* ---------- Temps actif ---------- */
  let lastInput = Date.now();
  const awake = () => { lastInput = Date.now(); };
  ['pointerdown', 'keydown', 'wheel', 'touchstart', 'scroll', 'mousemove'].forEach((t) => addEventListener(t, awake, { passive: true, capture: true }));
  setInterval(() => {
    if (document.visibilityState === 'visible' && (watching > 0 || Date.now() - lastInput < IDLE)) { active += 1; touch(); }
  }, 1000);
  setInterval(() => { if (queue.length || active >= EVERY / 1000) flush(false); }, EVERY);
  // Lien interne cliqué : envoi avant de quitter la page (plus sûr que pendant le changement de page)
  document.addEventListener('click', (e) => {
    const a = e.target.closest && e.target.closest('a[href]');
    if (!a || a.target === '_blank' || e.defaultPrevented || e.metaKey || e.ctrlKey) return;
    try { if (new URL(a.href, location.href).origin === location.origin) flush(true); } catch (err) {}
  });
  document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'hidden') flush(true); else awake(); });
  addEventListener('pagehide', () => flush(true));

  /* ---------- Vue de page (avec le temps d'affichage) ----------
     Temps jusqu'à la page affichée et prête (DOMContentLoaded), sans attendre les images de carte. */
  function pageView() {
    let ms = null;
    try { const n = performance.getEntriesByType('navigation')[0]; if (n && n.domContentLoadedEventEnd > 0) ms = n.domContentLoadedEventEnd; } catch (e) {}
    track('vue', { v: ms && ms > 0 ? Math.min(ms, 600000) : null });
    // Envoi rapide de la vue : l'envoi à la fermeture de la page n'est pas garanti par tous les navigateurs
    setTimeout(() => flush(false), 1200);
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', () => setTimeout(pageView, 0), { once: true });
  else setTimeout(pageView, 0);

  /* ---------- Fenêtres, erreurs, appels lents ---------- */
  const openSheet = U.openSheet;
  if (openSheet) U.openSheet = function (id) { track('fenetre', { d: String(id || '').replace(/^sheet-/, '') }); return openSheet.apply(this, arguments); };
  let errs = 0;
  addEventListener('error', (e) => { if (e && e.message && errs++ < 5) track('erreur_js', { d: PAGE }); });
  addEventListener('unhandledrejection', () => { if (errs++ < 5) track('erreur_js', { d: PAGE }); });
  const endpoint = (url) => { try { const p = new URL(url, location.href).pathname.split('/').filter(Boolean); return p[0] === 'api' ? p[1] : p[0]; } catch (e) { return 'autre'; } };
  const getJSON = U.getJSON;
  if (getJSON) U.getJSON = async function (url) {
    const t0 = performance.now();
    try {
      const r = await getJSON.apply(this, arguments);
      const ms = performance.now() - t0;
      if (ms > 3000) track('api_lente', { d: endpoint(url), v: ms });
      return r;
    } catch (e) {
      if (e && e.message !== 'auth') track('erreur_api', { d: endpoint(url), v: parseInt(String(e.message).replace(/\D/g, ''), 10) || 0 });
      throw e;
    }
  };

  /* ---------- Clics simples : attribut data-track="nom" (et data-track-d) ---------- */
  document.addEventListener('click', (e) => {
    const el = e.target.closest && e.target.closest('[data-track]');
    if (el) track(el.dataset.track, { d: el.dataset.trackD });
  }, true);

  U.track = track;
  /* Animation regardée sans toucher l'écran (timelapse, bilan de l'année) : le temps compte comme actif */
  U.track.watching = (on) => { watching = Math.max(0, watching + (on ? 1 : -1)); if (!on) awake(); };
  (U._trackQueue || []).forEach((a) => track(a[0], a[1]));
  U._trackQueue = [];
})();
