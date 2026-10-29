/* UNLOCK: shared front-end helpers (loader, sheets, PWA install, toasts) */
(function () {
  'use strict';

  const U = (window.Unlock = window.Unlock || {});

  /* ---------------- Sports ---------------- */
  U.SPORTS = {
    Run: ['Course à pied', '🏃'], TrailRun: ['Trail', '⛰️'], VirtualRun: ['Course virtuelle', '🏃'],
    Ride: ['Vélo', '🚴'], GravelRide: ['Gravel', '🚵'], 'E-BikeRide': ['Vélo électrique', '⚡'],
    VirtualRide: ['Vélo virtuel', '🚴'], Velomobile: ['Vélomobile', '🚲'],
    Hike: ['Randonnée', '🥾'], Walk: ['Marche', '🚶'],
    AlpineSki: ['Ski alpin', '⛷️'], BackcountrySki: ['Ski de rando', '🎿'], NordicSki: ['Ski de fond', '🎿'],
    Snowshoe: ['Raquettes', '❄️'], Swim: ['Natation', '🏊']
  };
  U.sportLabel = (code) => (U.SPORTS[code] ? `${U.SPORTS[code][1]}  ${U.SPORTS[code][0]}` : code);

  U.esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  U.fmt = (n, d = 0) => Number(n || 0).toLocaleString('fr-FR', { minimumFractionDigits: d, maximumFractionDigits: d }).replace(/\u202f/g, '\u00a0');

  /* ---------------- Fetch ---------------- */
  U.getJSON = async function (url) {
    const res = await fetch(url, { credentials: 'same-origin', headers: { Accept: 'application/json' } });
    if (res.status === 401) { window.location.href = '/login'; throw new Error('auth'); }
    if (!res.ok) throw new Error('HTTP ' + res.status);
    return res.json();
  };

  /* ---------------- Full-screen loader ---------------- */
  const loader = () => document.getElementById('loader');
  let msgTimer = null;
  U.loader = {
    show(messages) {
      const el = loader(); if (!el) return;
      el.classList.remove('is-hidden');
      const msgEl = el.querySelector('.loader__msg');
      clearInterval(msgTimer);
      if (msgEl && messages && messages.length) {
        let i = 0; msgEl.textContent = messages[0];
        msgTimer = setInterval(() => {
          i = (i + 1) % messages.length;
          msgEl.classList.add('is-fading');
          setTimeout(() => { msgEl.textContent = messages[i]; msgEl.classList.remove('is-fading'); }, 250);
        }, 2200);
      }
    },
    hide() { const el = loader(); if (el) el.classList.add('is-hidden'); clearInterval(msgTimer); }
  };

  /* Busy pill for refreshes that shouldn't block the screen */
  U.busy = {
    on(text) { const b = document.getElementById('busy'); if (!b) return; if (text) b.querySelector('span').textContent = text; b.classList.add('is-on'); },
    off() { const b = document.getElementById('busy'); if (b) b.classList.remove('is-on'); }
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
    const focusable = s.querySelector('[autofocus], button, a, select, input');
    setTimeout(() => focusable && focusable.focus({ preventScroll: true }), 60);
  };
  U.closeSheet = function (id) {
    const s = id ? document.getElementById(id) : document.querySelector('.sheet.is-open');
    if (!s) return;
    s.classList.remove('is-open'); s.setAttribute('aria-hidden', 'true');
    if (lastFocus && lastFocus.focus) lastFocus.focus({ preventScroll: true });
  };
  document.addEventListener('click', (e) => {
    const open = e.target.closest('[data-sheet-open]');
    if (open) { e.preventDefault(); U.openSheet(open.dataset.sheetOpen); return; }
    const close = e.target.closest('[data-sheet-close]');
    if (close) { e.preventDefault(); U.closeSheet(close.closest('.sheet') && close.closest('.sheet').id); }
  });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') U.closeSheet(); });

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
