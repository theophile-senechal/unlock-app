/* UNLOCK: shared front-end helpers (loader, sheets, PWA install, toasts) */
(function () {
  'use strict';

  const U = (window.Unlock = window.Unlock || {});

  /* ---------------- Sports ---------------- */
  U.SPORTS = {
    // Sports suivis : à pied ou à vélo (ski, natation et sports virtuels ne débloquent rien)
    Run: ['Course à pied', '🏃'], TrailRun: ['Trail', '⛰️'],
    Ride: ['Vélo', '🚴'], GravelRide: ['Gravel', '🚵'], 'E-BikeRide': ['Vélo électrique', '⚡'],
    Velomobile: ['Vélomobile', '🚲'],
    Hike: ['Randonnée', '🥾'], Walk: ['Marche', '🚶'],
    Snowshoe: ['Raquettes', '❄️']
  };
  U.sportLabel = (code) => (U.SPORTS[code] ? `${U.SPORTS[code][1]}  ${U.SPORTS[code][0]}` : code);

  U.esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  U.fmt = (n, d = 0) => Number(n || 0).toLocaleString('fr-FR', { minimumFractionDigits: d, maximumFractionDigits: d }).replace(/\u202f/g, '\u00a0');

  /* ---------------- Fond de carte ----------------
     CARTO (clair, épuré) si une clé est configurée, sinon OpenStreetMap passé en gris clair. */
  U.baseLayer = function (map) {
    const key = (window.UNLOCK_CONFIG || {}).cartoKey;
    const osm = '© <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a>';
    map.attributionControl.setPrefix(false);
    if (key) {
      return L.tileLayer('https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png?key=' + encodeURIComponent(key), {
        maxZoom: 19, subdomains: 'abcd',
        attribution: osm + ' © <a href="https://carto.com/attributions" target="_blank" rel="noopener">CARTO</a>'
      }).addTo(map);
    }
    map.getContainer().classList.add('map--osm');
    return L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', { maxZoom: 19, attribution: osm }).addTo(map);
  };

  /* Emprise d'un bloc carré : la largeur en longitude dépend de la latitude */
  U.cellBounds = function (lat, lon, size) {
    const w = size / Math.max(0.05, Math.cos(lat * Math.PI / 180));
    return [[lat - size / 2, lon - w / 2], [lat + size / 2, lon + w / 2]];
  };

  /* ---------------- Fetch ---------------- */
  U.getJSON = async function (url) {
    const res = await fetch(url, { credentials: 'same-origin', headers: { Accept: 'application/json' } });
    if (res.status === 401) { window.location.href = '/login'; throw new Error('auth'); }
    if (!res.ok) throw new Error('HTTP ' + res.status);
    return res.json();
  };

  /* ---------------- Full-screen loader ---------------- */
  const loader = () => document.getElementById('loader');
  let msgTimer = null, patienceTimers = [];
  // Messages affichés si le chargement dure (1re connexion : tout l'historique Strava est lu)
  const PATIENCE = [
    [15000, 'Encore un peu de patience ⏳ La première connexion lit tout votre historique Strava.'],
    [45000, 'Ça peut prendre quelques minutes si vous avez beaucoup de sorties. Les prochaines ouvertures seront quasi instantanées 🚀'],
    [120000, 'Toujours en cours… Merci pour votre patience 🙏 Ne fermez pas la page.']
  ];
  function setHint(el, text) {
    let h = el.querySelector('.loader__hint');
    if (!h) { h = document.createElement('p'); h.className = 'loader__hint'; el.querySelector('.loader__msg').after(h); }
    h.classList.add('is-fading');
    setTimeout(() => { h.textContent = text; h.classList.remove('is-fading'); }, 250);
  }
  U.loader = {
    show(messages) {
      const el = loader(); if (!el) return;
      el.classList.remove('is-hidden');
      const msgEl = el.querySelector('.loader__msg');
      clearInterval(msgTimer); patienceTimers.forEach(clearTimeout);
      if (msgEl && messages && messages.length) {
        // Les étapes défilent une fois puis restent sur la dernière (pas de boucle trompeuse)
        let i = 0; msgEl.textContent = messages[0];
        msgTimer = setInterval(() => {
          if (i >= messages.length - 1) { clearInterval(msgTimer); return; }
          i += 1;
          msgEl.classList.add('is-fading');
          setTimeout(() => { msgEl.textContent = messages[i]; msgEl.classList.remove('is-fading'); }, 250);
        }, 4000);
      }
      patienceTimers = PATIENCE.map(([ms, text]) => setTimeout(() => setHint(el, text), ms));
    },
    hide() {
      const el = loader(); if (el) el.classList.add('is-hidden');
      clearInterval(msgTimer); patienceTimers.forEach(clearTimeout);
    }
  };

  /* Busy pill for refreshes that shouldn't block the screen */
  let busyTimer = null;
  let busyWanted = false;   // un « off » très rapide ne doit pas être suivi d'un affichage différé
  U.busy = {
    /* title : action en cours ; sub : précision (filtres…) ; opts.veil : voile flou sur le contenu */
    on(title, sub, opts) {
      const b = document.getElementById('busy'); if (!b) return;
      clearTimeout(busyTimer);
      document.getElementById('busy-title').textContent = title || 'Mise à jour…';
      document.getElementById('busy-sub').textContent = sub || '';
      b.classList.toggle('working--veil', !!(opts && opts.veil));
      busyWanted = true;
      b.hidden = false; requestAnimationFrame(() => { if (busyWanted) b.classList.add('is-on'); });
    },
    off() {
      const b = document.getElementById('busy'); if (!b) return;
      busyWanted = false;
      b.classList.remove('is-on');
      busyTimer = setTimeout(() => { b.hidden = true; }, 260);
    }
  };

  /* ---------------- Toast ---------------- */
  let toastTimer = null;
  U.toast = function (text) {
    let t = document.getElementById('toast');
    if (!t) { t = document.createElement('div'); t.id = 'toast'; t.className = 'toast'; t.setAttribute('role', 'status'); document.body.appendChild(t); }
    t.textContent = text; t.classList.add('is-on');
    clearTimeout(toastTimer); toastTimer = setTimeout(() => t.classList.remove('is-on'), 3200);
  };

  /* ---------------- Sheets ---------------- */
  let lastFocus = null;
  U.openSheet = function (id) {
    const s = document.getElementById(id); if (!s) return;
    lastFocus = document.activeElement;
    s.classList.add('is-open'); s.setAttribute('aria-hidden', 'false');
    // Focus sur un bouton plutôt qu'un champ (évite d'ouvrir le clavier / une liste de recherche à l'ouverture)
    const focusable = s.querySelector('[autofocus]') || s.querySelector('button:not([hidden]), a[href]');
    setTimeout(() => focusable && focusable.focus({ preventScroll: true }), 60);
    const p = s.querySelector('.sheet__panel'); if (p) { p.scrollTop = 0; requestAnimationFrame(() => moreHint(p)); setTimeout(() => moreHint(p), 320); }
  };
  // Fondu en bas d'une fenêtre tant qu'il reste du contenu à faire défiler
  function moreHint(p) { p.classList.toggle('has-more', p.scrollTop + p.clientHeight < p.scrollHeight - 6); }
  document.addEventListener('scroll', (e) => { if (e.target.classList && e.target.classList.contains('sheet__panel')) moreHint(e.target); }, true);
  window.addEventListener('resize', () => document.querySelectorAll('.sheet.is-open .sheet__panel').forEach(moreHint));
  U.closeSheet = function (id) {
    const s = id ? document.getElementById(id) : document.querySelector('.sheet.is-open');
    if (!s) return;
    s.classList.remove('is-open'); s.setAttribute('aria-hidden', 'true');
    if (lastFocus && lastFocus.focus) lastFocus.focus({ preventScroll: true });
  };
  document.addEventListener('click', (e) => {
    const open = e.target.closest('[data-sheet-open]');
    // Fenêtre absente sur cette page : un lien suit simplement son adresse (ex. « Soutenir » -> Ko-fi)
    if (open && document.getElementById(open.dataset.sheetOpen)) { e.preventDefault(); U.openSheet(open.dataset.sheetOpen); return; }
    if (open) return;
    const close = e.target.closest('[data-sheet-close]');
    if (close) { e.preventDefault(); U.closeSheet(close.closest('.sheet') && close.closest('.sheet').id); }
  });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') U.closeSheet(); });

  /* ---------------- Copier dans le presse-papiers ---------------- */
  document.addEventListener('click', async (e) => {
    const b = e.target.closest('[data-copy]'); if (!b) return;
    try {
      await navigator.clipboard.writeText(b.dataset.copy);
      U.toast('📋 Identifiant copié');
      b.classList.add('is-done'); setTimeout(() => b.classList.remove('is-done'), 1500);
    } catch (err) {
      const t = document.getElementById('my-athlete-id');
      if (t) { const r = document.createRange(); r.selectNodeContents(t); const sel = getSelection(); sel.removeAllRanges(); sel.addRange(r); }
      U.toast('Sélectionné : copiez avec Ctrl+C');
    }
  });

  /* ---------------- PWA ---------------- */
  const isStandalone = () => window.matchMedia('(display-mode: standalone)').matches || window.navigator.standalone === true;
  const ua = navigator.userAgent || '';
  const isIOS = /iphone|ipad|ipod/i.test(ua) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
  let deferredPrompt = null;

  function refreshInstallButtons() {
    const canShow = !isStandalone() && (deferredPrompt || isIOS);
    document.querySelectorAll('[data-install]').forEach((b) => { b.hidden = !canShow; });
  }

  window.addEventListener('beforeinstallprompt', (e) => { e.preventDefault(); deferredPrompt = e; refreshInstallButtons(); });
  window.addEventListener('appinstalled', () => { deferredPrompt = null; refreshInstallButtons(); U.toast('🎉 Unlock est installée sur votre appareil'); });

  document.addEventListener('click', async (e) => {
    const btn = e.target.closest('[data-install]'); if (!btn) return;
    e.preventDefault();
    U.closeSheet('sheet-menu');
    if (deferredPrompt) {
      deferredPrompt.prompt();
      const { outcome } = await deferredPrompt.userChoice;
      deferredPrompt = null; refreshInstallButtons();
      if (outcome === 'dismissed') U.toast('Installation annulée. Le bouton reste dans le menu.');
    } else if (isIOS) {
      setTimeout(() => U.openSheet('sheet-ios'), 50);
    }
  });

  if ('serviceWorker' in navigator && window.isSecureContext) {
    window.addEventListener('load', () => { navigator.serviceWorker.register('/sw.js').catch(() => {}); });
  }

  document.addEventListener('DOMContentLoaded', () => {
    refreshInstallButtons();
    if (isStandalone()) document.documentElement.classList.add('is-standalone');
  });
})();
