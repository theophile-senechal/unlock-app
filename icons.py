"""Jeu d'icônes d'Unlock : icônes au trait (24 × 24, trait 1,75, bouts arrondis), dessinées pour l'app.
Une seule source pour les pages (fonction Jinja `icon`) et le JavaScript (`Unlock.icon`, via window.UNLOCK_ICONS).
Elles prennent la couleur du texte (currentColor) : grises au repos, bordeaux quand l'élément est actif."""
from markupsafe import Markup

ICONS = {
    # Navigation
    'map': '<path d="M9 4 3.5 6v14L9 18l6 2 5.5-2V4L15 6 9 4Z"/><path d="M9 4v14M15 6v14"/>',
    'landmark': '<path d="M3.5 20.5h17M5.5 20.5v-9M18.5 20.5v-9M9.5 20.5v-6M14.5 20.5v-6M3.5 10 12 4l8.5 6Z"/>',
    'play': '<circle cx="12" cy="12" r="8.5"/><path d="m10.2 8.8 5 3.2-5 3.2Z"/>',
    'story': '<rect x="6.5" y="3" width="11" height="18" rx="2.5"/><path d="M10 6h4"/><path d="m9 15 2-2 1.5 1.5L15 12"/>',
    'stats': '<path d="M4 20h16"/><path d="M7.5 16.5v-5M12 16.5v-10M16.5 16.5v-7"/>',
    # Interface
    'search': '<circle cx="11" cy="11" r="6"/><path d="m20 20-4.5-4.5"/>',
    'pin': '<path d="M12 20.5s-6.5-5.8-6.5-11a6.5 6.5 0 0 1 13 0c0 5.2-6.5 11-6.5 11Z"/><circle cx="12" cy="9.5" r="2.3"/>',
    'sliders': '<path d="M4 7h9M17 7h3M4 17h3M11 17h9"/><circle cx="15" cy="7" r="2"/><circle cx="9" cy="17" r="2"/>',
    'x': '<path d="M6 6l12 12M18 6 6 18"/>',
    'menu': '<path d="M4 7h16M4 12h16M4 17h10"/>',
    'chev': '<path d="m9.5 6 6 6-6 6"/>',
    'copy': '<rect x="8.5" y="8.5" width="11" height="11" rx="2"/><path d="M15.5 8.5V6a1.5 1.5 0 0 0-1.5-1.5H6A1.5 1.5 0 0 0 4.5 6v8A1.5 1.5 0 0 0 6 15.5h2.5"/>',
    'lock': '<rect x="5" y="10.5" width="14" height="10" rx="2"/><path d="M8.5 10.5V7.5a3.5 3.5 0 0 1 7 0v3"/>',
    'unlock': '<rect x="5" y="10.5" width="14" height="10" rx="2"/><path d="M8.5 10.5V7.5a3.5 3.5 0 0 1 6.8-1.2"/>',
    'shield': '<path d="M12 3 5.5 5.8v5c0 4.3 2.8 7.6 6.5 9.7 3.7-2.1 6.5-5.4 6.5-9.7v-5L12 3Z"/><path d="m9.3 12 1.9 1.9 3.6-3.8"/>',
    'msg': '<path d="M4.5 5.5h15v10.5H10l-5.5 4v-14.5Z"/>',
    'logout': '<path d="M14 4h3.5A2.5 2.5 0 0 1 20 6.5v11a2.5 2.5 0 0 1-2.5 2.5H14"/><path d="M10 16.5 5.5 12 10 7.5M5.5 12H15"/>',
    'trash': '<path d="M4.5 7h15M10 11v6M14 11v6"/><path d="M6.5 7l.8 12.5a1.5 1.5 0 0 0 1.5 1.5h6.4a1.5 1.5 0 0 0 1.5-1.5L17.5 7M9.5 7V4.5h5V7"/>',
    'install': '<rect x="6.5" y="2.5" width="11" height="19" rx="2.5"/><path d="M12 7v7M9 11.5l3 3 3-3M10.5 18.5h3"/>',
    'download': '<path d="M12 4v11M7.5 10.5 12 15l4.5-4.5M5 19.5h14"/>',
    'gauge': '<path d="M4 17a8 8 0 1 1 16 0"/><path d="m12 17 4-5.5"/><circle cx="12" cy="17" r="1.2"/>',
    'trophy': '<path d="M8 4.5h8v5a4 4 0 0 1-8 0v-5Z"/><path d="M8 6.5H5v1.5a3 3 0 0 0 3 3M16 6.5h3v1.5a3 3 0 0 1-3 3M12 13.5v3M8.5 20h7M10 16.5h4"/>',
    'heart': '<path d="M12 19.5s-7.5-4.3-7.5-9.8A4.2 4.2 0 0 1 12 7.2a4.2 4.2 0 0 1 7.5 2.5c0 5.5-7.5 9.8-7.5 9.8Z"/>',
    'doc': '<path d="M14 3.5H7.5A1.5 1.5 0 0 0 6 5v14a1.5 1.5 0 0 0 1.5 1.5h9A1.5 1.5 0 0 0 18 19V7.5l-4-4Z"/><path d="M14 3.5v4h4M9 12h6M9 15.5h6"/>',
    'building': '<path d="M4.5 20.5h15M6.5 20.5V5.5a1 1 0 0 1 1-1h9a1 1 0 0 1 1 1v15M10 8h1M13 8h1M10 11.5h1M13 11.5h1M10.5 20.5v-4h3v4"/>',
    'key': '<circle cx="8" cy="15" r="3.5"/><path d="m10.5 12.5 8-8M16 7l2.5 2.5M14 9l2 2"/>',
    'globe': '<circle cx="12" cy="12" r="8.5"/><path d="M3.5 12h17M12 3.5c2.3 2.4 3.5 5.3 3.5 8.5s-1.2 6.1-3.5 8.5c-2.3-2.4-3.5-5.3-3.5-8.5s1.2-6.1 3.5-8.5Z"/>',
    'info': '<circle cx="12" cy="12" r="8.5"/><path d="M12 11v5M12 8h.01"/>',
    'alert': '<path d="M10.3 4.6 3.4 17a2 2 0 0 0 1.7 3h13.8a2 2 0 0 0 1.7-3L13.7 4.6a2 2 0 0 0-3.4 0Z"/><path d="M12 9.5v4M12 16.5h.01"/>',
    'check': '<path d="m5 12.5 4.5 4.5L19 7.5"/>',
    'check-circle': '<circle cx="12" cy="12" r="8.5"/><path d="m8.5 12.3 2.5 2.5 4.5-5"/>',
    'clock': '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/>',
    'calendar': '<rect x="4" y="5.5" width="16" height="14.5" rx="2"/><path d="M4 10h16M8.5 3.5v4M15.5 3.5v4"/>',
    'grid': '<rect x="4" y="4" width="6.5" height="6.5" rx="1.5"/><rect x="13.5" y="4" width="6.5" height="6.5" rx="1.5"/><rect x="4" y="13.5" width="6.5" height="6.5" rx="1.5"/><rect x="13.5" y="13.5" width="6.5" height="6.5" rx="1.5"/>',
    'link': '<path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1"/><path d="M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1"/>',
    'wifi-off': '<path d="M3.5 3.5l17 17M8.5 16a5 5 0 0 1 7 0M5 12.5a10 10 0 0 1 4.2-2.4M19 12.5a10 10 0 0 0-3.2-2.1M2 9a14.5 14.5 0 0 1 5-3M22 9a14.5 14.5 0 0 0-10-4M12 19.5h.01"/>',
    'compass': '<circle cx="12" cy="12" r="8.5"/><path d="m15.5 8.5-2 5-5 2 2-5 5-2Z"/>',
    'users': '<circle cx="9" cy="8.5" r="3.5"/><path d="M3 19.5a6 6 0 0 1 12 0M16 5.5a3.5 3.5 0 0 1 0 6.5M17.5 14.5a5.5 5.5 0 0 1 3.5 5"/>',
    'trend': '<path d="M3.5 17 9 11.5l4 4 7.5-8"/><path d="M15 7.5h5.5V13"/>',
    'scale': '<path d="M12 4v16M7.5 20h9M5 7.5h14M5 7.5 2.5 13a3 3 0 0 0 5 0L5 7.5ZM19 7.5 16.5 13a3 3 0 0 0 5 0L19 7.5Z"/>',
    'broom': '<path d="M14.5 3.5 11 10M8 10.5h7l1.5 3.5-2 6.5H7.5L6 14l2-3.5Z"/><path d="M9.5 15v3.5M12.5 15v3.5"/>',
    'external': '<path d="M14 4.5h5.5V10M19.5 4.5 11 13M17.5 14v4a1.5 1.5 0 0 1-1.5 1.5H6A1.5 1.5 0 0 1 4.5 18V8A1.5 1.5 0 0 1 6 6.5h4"/>',
    'sparkle': '<path d="M12 4v4M12 16v4M4 12h4M16 12h4M7 7l2 2M15 15l2 2M17 7l-2 2M9 15l-2 2"/>',
    # Catégories de lieux
    'chateau': '<path d="M3.5 20.5h17M5 20.5V8h2v2h2.5V8h2v2H14V8h2v2h2.5V8H20v12.5"/><path d="M10 20.5v-3.5a2 2 0 0 1 4 0v3.5"/>',
    'religieux': '<path d="M12 2.5v4M10 4.5h4"/><path d="M6 20.5V11l6-4.5 6 4.5v9.5"/><path d="M10 20.5v-3.5a2 2 0 0 1 4 0v3.5M3.5 20.5h17"/>',
    'megalithe': '<path d="M8 20.5c-.6-4.6-.4-9.6 1.4-13.4.9-2 2.6-3 3.6-3 1.6 0 2.8 1.6 3.2 4.6.5 3.7.2 8-.6 11.8"/><path d="M4 20.5h16"/>',
    'fortif': '<path d="M12 3 5.5 5.8v5c0 4.3 2.8 7.6 6.5 9.7 3.7-2.1 6.5-5.4 6.5-9.7v-5L12 3Z"/>',
    'musee': '<path d="M3.5 20.5h17M5.5 20.5v-9M18.5 20.5v-9M9.5 20.5v-9M14.5 20.5v-9M3.5 9.5 12 4l8.5 5.5Z"/>',
    'monument': '<path d="M10 20.5 11 6l1-2.5L13 6l1 14.5"/><path d="M6.5 20.5h11"/>',
    'vue': '<circle cx="7" cy="15.5" r="3.5"/><circle cx="17" cy="15.5" r="3.5"/><path d="M7 12V6.5h3V12M17 12V6.5h-3V12M10 9h4"/>',
    'cascade': '<path d="M12 3.5s-5.5 6.2-5.5 10a5.5 5.5 0 0 0 11 0c0-3.8-5.5-10-5.5-10Z"/>',
    'jardin': '<path d="M12 20.5v-6"/><path d="M12 14.5c-3.6 0-6-2.3-6-5.2 0-2.6 2-4.8 4.3-5.2A4 4 0 0 1 12 3.5a4 4 0 0 1 1.7.6c2.3.4 4.3 2.6 4.3 5.2 0 2.9-2.4 5.2-6 5.2Z"/><path d="M8.5 20.5h7"/>',
    'sommet': '<path d="M3 19.5 9.5 8l3.5 6 2.5-4L21 19.5H3Z"/><path d="m8 10.8 1.5 1.2 1.5-1.2"/>',
}


def svg(name, cls='i', label=None):
    """Code SVG d'une icône. Sans libellé, elle est décorative (cachée aux lecteurs d'écran)."""
    a11y = f' role="img" aria-label="{label}"' if label else ' aria-hidden="true"'
    return (f'<svg class="{cls}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" '
            f'stroke-linecap="round" stroke-linejoin="round"{a11y}>{ICONS.get(name, "")}</svg>')


def icon(name, cls='i', label=None):
    return Markup(svg(name, cls, label))
