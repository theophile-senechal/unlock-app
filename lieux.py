"""Lieux à collectionner : patrimoine (châteaux, églises, mégalithes, musées…) et nature (points de vue, cascades,
parcs et jardins, sommets).

Les lieux viennent d'OpenStreetMap (data/lieux/<dep>.json, calculés chaque mois par tools/acces).
Un lieu est débloqué quand une sortie passe à moins de RAYON_M mètres de lui (ou de son emprise pour
un grand site : château, alignements…). Le calcul se fait à la demande, pour l'utilisateur seul, à partir
de ses propres sorties : rien n'est enregistré (Strava API Policy §6.1 et §6.2).
"""
import os, json, threading
import numpy as np
import acces

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'lieux')
RAYON_M = 60          # distance de passage pour débloquer un lieu (marge GPS comprise)
RAYON_MAX_M = 250     # emprise maximale prise en compte pour un grand site
PAS_M = 15            # pas de densification des traces

# Catégories : code -> (libellé, emoji). L'ordre est celui de l'affichage.
CATEGORIES = {
    'chateau':   ('Châteaux et manoirs', '🏰'),
    'religieux': ('Églises et abbayes', '⛪'),
    'megalithe': ('Mégalithes et sites antiques', '🪨'),
    'fortif':    ('Forts et remparts', '🛡️'),
    'musee':     ('Musées et culture', '🏛️'),
    'monument':  ('Monuments et patrimoine', '🗿'),
    'vue':       ('Points de vue', '🔭'),
    'cascade':   ('Cascades', '💧'),
    'jardin':    ('Parcs et jardins', '🌳'),
    'sommet':    ('Sommets', '⛰️'),
}

_CACHE, _LOCK = {}, threading.Lock()


def departement(dep):
    """Données d'un département (ou None si pas encore calculé). Gardées en mémoire : donnée publique."""
    with _LOCK:
        if dep not in _CACHE:
            try:
                with open(os.path.join(DATA_DIR, f'{dep}.json'), encoding='utf-8') as fh: d = json.load(fh)
                rows = d['lieux']
                d['_lat'] = np.array([r[1] for r in rows], dtype=np.float64)
                d['_lon'] = np.array([r[2] for r in rows], dtype=np.float64)
                d['_ray'] = np.array([RAYON_M + min(RAYON_MAX_M, r[3]) for r in rows], dtype=np.float64)
                _CACHE[dep] = d
            except (OSError, ValueError, KeyError):
                _CACHE[dep] = None
        return _CACHE[dep]


def collecter(traces, deps):
    """traces : [(date 'AAAA-MM-JJ', [(lat, lon), …]), …] triées par date.
    Renvoie {dep: {indice du lieu: date de 1re visite}} pour les départements demandés qui ont des données."""
    data = {dep: departement(dep) for dep in deps}
    data = {dep: d for dep, d in data.items() if d and len(d['lieux'])}
    if not data: return {}
    deps_l = list(data)
    lat = np.concatenate([data[d]['_lat'] for d in deps_l])
    lon = np.concatenate([data[d]['_lon'] for d in deps_l])
    ray = np.concatenate([data[d]['_ray'] for d in deps_l])
    dep_of = np.concatenate([np.full(len(data[d]['_lat']), i) for i, d in enumerate(deps_l)])
    debut = np.concatenate([[0], np.cumsum([len(data[d]['_lat']) for d in deps_l])])
    pris = np.zeros(len(lat), dtype=bool)
    date_de = {}
    m_deg = (RAYON_M + RAYON_MAX_M) / acces.M_PER_DEG
    for date, pts in traces:
        if len(pts) < 1: continue
        a = np.asarray(pts, dtype=np.float64)
        la0, la1 = a[:, 0].min() - m_deg, a[:, 0].max() + m_deg
        mc = m_deg / max(0.2, np.cos(np.radians(a[0, 0])))
        lo0, lo1 = a[:, 1].min() - mc, a[:, 1].max() + mc
        cand = np.nonzero(~pris & (lat >= la0) & (lat <= la1) & (lon >= lo0) & (lon <= lo1))[0]
        if not len(cand): continue
        tla, tlo = acces.densify([pts], PAS_M)
        cosl = np.cos(np.radians(a[0, 0]))
        for k in range(0, len(cand), 200):          # par paquets : matrice lieux × points raisonnable
            c = cand[k:k + 200]
            dy = (tla[None, :] - lat[c, None]) * acces.M_PER_DEG
            dx = (tlo[None, :] - lon[c, None]) * acces.M_PER_DEG * cosl
            d2 = (dy * dy + dx * dx).min(axis=1)
            for i in c[d2 <= ray[c] ** 2]:
                pris[i] = True; date_de[int(i)] = date
    out = {d: {} for d in deps_l}
    for i, date in date_de.items():
        di = int(dep_of[i]); out[deps_l[di]][i - int(debut[di])] = date
    return out


def disponibles():
    """{dep: nombre de lieux} pour les départements déjà calculés."""
    try:
        with open(os.path.join(DATA_DIR, 'index.json'), encoding='utf-8') as fh: return json.load(fh).get('departements', {})
    except (OSError, ValueError):
        return {}


# ---------------------------------------------------------------- Recherche de commune (toute la France)
import unicodedata as _ud

_COMMUNES = {'rows': None}


def _norm(t):
    t = _ud.normalize('NFD', t or '').encode('ascii', 'ignore').decode().lower()
    return ' '.join(''.join(ch if ch.isalnum() else ' ' for ch in t).split())


def _communes():
    """[(code, nom, dep, (sud, ouest, nord, est), nombre de lieux, nom normalisé)] pour toute la France."""
    with _LOCK:
        if _COMMUNES['rows'] is None:
            rows = []
            try:
                with open(os.path.join(DATA_DIR, 'communes.json'), encoding='utf-8') as fh: data = json.load(fh)
                for dep, lst in data.items():
                    for code, nom, s, w, n, e, nb in lst:
                        rows.append((code, nom, dep, (s, w, n, e), nb, _norm(nom)))
            except (OSError, ValueError):
                pass
            _COMMUNES['rows'] = rows
        return _COMMUNES['rows']


def commune(code):
    return next((r for r in _communes() if r[0] == code), None)


def chercher(q, limite=12):
    """Communes dont le nom commence par la saisie (ou dont un mot commence par elle), sans tenir compte
    des accents ni des tirets. Les communes qui ont le plus de lieux passent en premier (souvent les plus connues)."""
    q = _norm(q)
    if len(q) < 2: return []
    res = []
    for r in _communes():
        n = r[5]
        if n.startswith(q): rang = 0 if n == q else 1
        elif (' ' + q) in (' ' + n): rang = 2
        else: continue
        res.append((rang, -r[4], len(n), r))
    res.sort(key=lambda x: x[:3])
    return [x[3] for x in res[:limite]]


def total_france():
    return sum(disponibles().values())
