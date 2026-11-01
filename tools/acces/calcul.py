"""Blocs inaccessibles d'un département, à partir d'un fichier OpenStreetMap (.osm.pbf).

    python tools/acces/calcul.py <fichier.osm.pbf ou URL> <dossier_sortie>

Produit <dossier_sortie>/<nom du fichier>.pkl, pour chaque taille (100, 250 m) :
  - pour CHAQUE département dont le fichier contient des communes complètes : ses blocs, et ceux sans
    voie à pied / à vélo d'après ce fichier ;
  - les blocs accessibles hors de ces communes (bordure), pour les départements voisins.
La fusion combine tous les fichiers : un bloc n'est inaccessible que si aucun ne trouve de voie.
Il contient aussi les lieux historiques et culturels à collectionner (voir lieux_osm.py) et le nom des communes.
"""
import os, sys, time, tempfile, urllib.request, shutil, pickle
from collections import Counter
import numpy as np
import osmium
import shapely, shapely.wkb

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))
import acces  # noqa: E402  (même grille et même marge que l'app)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lieux_osm  # noqa: E402  (lieux historiques et culturels à collectionner)

SIZES = (100, 250)
ROADS = {'primary', 'primary_link', 'secondary', 'secondary_link', 'tertiary', 'tertiary_link',
         'unclassified', 'residential', 'living_street', 'service', 'road', 'trunk', 'trunk_link'}
FOOT_ONLY = {'footway', 'pedestrian', 'steps', 'corridor'}
NEVER = {'motorway', 'motorway_link', 'construction', 'proposed', 'abandoned', 'raceway',
         'bus_guideway', 'razed', 'busway', 'escape', 'platform', 'disused'}
YES = {'yes', 'designated', 'permissive', 'official'}
CLOSED = {'private', 'no'}


def profils(t):
    """(praticable à pied, praticable à vélo) d'après les attributs OpenStreetMap d'une voie."""
    hw = t.get('highway')
    if not hw or hw in NEVER: return False, False
    access = t.get('access', '')
    foot_t, bike_t = t.get('foot', ''), t.get('bicycle', '')
    rapide = t.get('motorroad') == 'yes' or (hw in ('trunk', 'trunk_link') and t.get('expressway') == 'yes')

    foot = hw in ROADS or hw in FOOT_ONLY or hw in ('path', 'track', 'cycleway', 'bridleway')
    if rapide or access in CLOSED or foot_t in ('no', 'private'): foot = False
    if foot_t in YES: foot = True

    if hw in FOOT_ONLY or hw == 'bridleway': bike = bike_t in YES
    else: bike = hw in ROADS or hw in ('path', 'track', 'cycleway')
    if rapide or (access in CLOSED and bike_t not in YES) or bike_t in ('no', 'private', 'use_sidepath'): bike = False
    if bike_t in YES: bike = True
    return foot, bike


def zone_fermee(t):
    """Terrains militaires et aérodromes : les voies qui s'y trouvent ne comptent pas."""
    return t.get('landuse') == 'military' or t.get('aeroway') == 'aerodrome' or \
        t.get('military') in ('base', 'range', 'training_area', 'danger_area')


def _norm(t):
    import unicodedata
    t = unicodedata.normalize('NFD', t).encode('ascii', 'ignore').decode().lower()
    return ''.join(ch for ch in t if ch.isalnum())


def _noms_departements():
    """Nom normalisé -> code, lu dans app.py (DEPARTEMENTS) : « val_de_marne » et « Val-de-Marne » -> 94."""
    import ast
    src = os.path.join(os.path.dirname(__file__), '..', '..', 'app.py')
    for n in ast.parse(open(src, encoding='utf-8').read()).body:
        if isinstance(n, ast.Assign) and getattr(n.targets[0], 'id', '') == 'DEPARTEMENTS':
            return {_norm(nom): code for code, nom in ast.literal_eval(n.value).items()}
    return {}


def dep_of(code):
    return code[:3] if code.startswith('97') else code[:2]


def lire(path):
    pied, velo, communes, fermees, noms, lieux = [], [], {}, [], {}, []
    wkb = osmium.geom.WKBFactory()
    for o in osmium.FileProcessor(path).with_locations().with_areas():
        if o.is_node():
            if 'name' in o.tags:
                c = lieux_osm.classer(o.tags)
                if c: lieux.append(_lieu('n%d' % o.id, o.tags, c, o.location.lat, o.location.lon, 0))
        elif o.is_way():
            # Lieu dessiné comme une ligne ouverte (cascade le long d'un ruisseau…) : point au milieu de la ligne.
            # Les lignes fermées sont traitées comme des surfaces plus bas.
            if 'name' in o.tags and not o.is_closed():
                c = lieux_osm.classer(o.tags)
                if c:
                    try:
                        pts = [(n.lat, n.lon) for n in o.nodes]
                        la, lo = pts[len(pts) // 2]
                        r = int(max(abs(pts[0][0] - pts[-1][0]), abs(pts[0][1] - pts[-1][1]) * np.cos(np.radians(la))) * acces.M_PER_DEG / 2)
                        lieux.append(_lieu('w%d' % o.id, o.tags, c, la, lo, min(250, r)))
                    except osmium.InvalidLocationError:
                        pass
            f, b = profils(o.tags)
            if not (f or b): continue
            try:
                a = np.array([(n.lat, n.lon) for n in o.nodes], dtype=np.float64)
            except osmium.InvalidLocationError:
                continue
            if f: pied.append(a)
            if b: velo.append(a)
        elif o.is_area():
            t = o.tags
            if zone_fermee(t):
                try: fermees.append(shapely.wkb.loads(wkb.create_multipolygon(o), hex=True))
                except Exception: pass
            elif t.get('boundary') == 'administrative' and t.get('admin_level') == '8' and t.get('ref:INSEE'):
                try:
                    communes[t.get('ref:INSEE')] = shapely.wkb.loads(wkb.create_multipolygon(o), hex=True)
                    noms[t.get('ref:INSEE')] = t.get('name') or t.get('ref:INSEE')
                except Exception: pass
            if 'name' in t and not t.get('boundary'):
                c = lieux_osm.classer(t)
                if c:
                    try: g = shapely.wkb.loads(wkb.create_multipolygon(o), hex=True)
                    except Exception: continue
                    p = g.representative_point(); x0, y0, x1, y1 = g.bounds
                    # Rayon du lieu (grand château, site mégalithique…) : on le débloque en passant à côté de son emprise
                    r = min(250, int(max((x1 - x0) * np.cos(np.radians(p.y)), y1 - y0) * acces.M_PER_DEG / 2))
                    oid = ('w%d' if o.from_way() else 'r%d') % o.orig_id()
                    lieux.append(_lieu(oid, t, c, p.y, p.x, r))
    return pied, velo, communes, fermees, noms, lieux


def _lieu(oid, t, c, lat, lon, r):
    return {'id': oid, 'lat': round(lat, 5), 'lon': round(lon, 5), 'r': int(r), 'cat': c[0], 'mh': bool(c[1]),
            'nom': t['name'].strip()[:120], 'wiki': lieux_osm.wiki_of(t)}


def lieux_par_commune(lieux, communes):
    """Rattache chaque lieu à sa commune (code INSEE) et retire les doublons (même nom à moins de 250 m :
    un nœud posé sur le bâtiment déjà décrit, par exemple). On garde la version la plus complète."""
    from shapely import STRtree
    codes = list(communes); arbre = STRtree([communes[c] for c in codes])
    score = lambda l: (l['mh'], bool(l['wiki']), l['r'] > 0)
    garde = []
    for l in sorted(lieux, key=score, reverse=True):
        idx = arbre.query(shapely.Point(l['lon'], l['lat']), predicate='intersects')
        if not len(idx): continue
        l['insee'] = codes[int(idx[0])]
        cle = lieux_osm.re.sub(r'\W+', '', l['nom'].lower())
        if any(g['_cle'] == cle and abs(g['lat'] - l['lat']) < .0023 and abs(g['lon'] - l['lon']) < .0034 for g in garde):
            continue
        l['_cle'] = cle; garde.append(l)
    for l in garde: del l['_cle']
    return garde


def cles_communes(geoms, dlat):
    """Clés des blocs dont le centre est dans l'une des communes (géométries en lon, lat)."""
    out = []
    for g in geoms:
        b0, a0, b1, a1 = g.bounds
        I, J = [], []
        for i in range(int(np.floor(a0 / dlat)), int(np.ceil(a1 / dlat)) + 1):
            dl = float(acces.dl_of(i * dlat, dlat))
            js = np.arange(int(np.floor(b0 / dl)), int(np.ceil(b1 / dl)) + 1)
            I.append(np.full(len(js), i)); J.append(js)
        I = np.concatenate(I); J = np.concatenate(J)
        lat = I * dlat; lon = J * acces.dl_of(lat, dlat)
        inside = shapely.contains_xy(g, lon, lat)
        out.append(acces.ij_keys(I[inside], J[inside]))
    return np.unique(np.concatenate(out)) if out else np.empty(0, np.int64)


def main(src, outdir):
    t0 = time.time()
    tmp = None
    if src.startswith('http'):
        tmp = tempfile.NamedTemporaryFile(suffix='.osm.pbf', delete=False); tmp.close()
        req = urllib.request.Request(src, headers={'User-Agent': 'unlock-app (calcul mensuel des blocs inaccessibles)'})
        with urllib.request.urlopen(req, timeout=600) as r, open(tmp.name, 'wb') as fh:
            shutil.copyfileobj(r, fh, 1 << 20)
        path = tmp.name
    else:
        path = src
    try:
        pied, velo, communes, fermees, noms, lieux = lire(path)
    finally:
        if tmp: os.unlink(tmp.name)
    if not communes:
        print(f'{src} : aucune commune, ignoré'); return
    lieux = lieux_par_commune(lieux, communes)
    fermees = shapely.union_all([g.buffer(0) for g in fermees]) if fermees else None
    if fermees is not None: shapely.prepare(fermees)
    # Les voies sont en (lat, lon) ; les géométries OSM en (lon, lat)
    masque = (lambda la, lo: ~shapely.contains_xy(fermees, lo, la)) if fermees is not None else None

    # Toutes les communes complètes du fichier, regroupées par département. Un fichier de département
    # contient aussi quelques communes voisines : la fusion combine tous les fichiers qui couvrent un bloc.
    par_dep = {}
    for c, g in communes.items(): par_dep.setdefault(dep_of(c), []).append(g)
    # Département du fichier : d'après son nom (« paris-latest » -> 75), sinon la plus grande surface
    nom_src = os.path.basename(src).split('-latest')[0].split('.osm')[0]
    principal = _noms_departements().get(_norm(nom_src))
    if principal not in par_dep:
        principal = max(par_dep, key=lambda d: sum(g.area for g in par_dep[d]))
    res = {}
    for size in SIZES:
        dlat = size / acces.M_PER_DEG
        acc = {nom: acces.line_keys(voies, dlat, acces.MARGE_M, masque) for nom, voies in (('pied', pied), ('velo', velo))}
        tout = []
        for dep, geoms in par_dep.items():
            mine = cles_communes(geoms, dlat); tout.append(mine)
            res[f'{dep}|{size}|blocs'] = acces.pack_keys(mine)
            for nom in ('pied', 'velo'):
                res[f'{dep}|{size}|{nom}'] = acces.pack_keys(np.setdiff1d(mine, acc[nom], assume_unique=True))
        dehors = np.unique(np.concatenate(tout))
        for nom in ('pied', 'velo'):   # voies trouvées hors des communes complètes du fichier (bordure)
            res[f'*|{size}|{nom}'] = acces.pack_keys(np.setdiff1d(acc[nom], dehors, assume_unique=True))
    os.makedirs(outdir, exist_ok=True)
    nom_fichier = os.path.basename(src).replace('-latest.osm.pbf', '').replace('.osm.pbf', '') or principal
    with open(os.path.join(outdir, f'{nom_fichier}.pkl'), 'wb') as fh:
        pickle.dump({'source': src, 'principal': principal, 'communes': {d: len(g) for d, g in par_dep.items()}, 'cles': res,
                     'noms': noms, 'lieux': lieux,
                     # Emprise de chaque commune (sud, ouest, nord, est) pour centrer la carte après une recherche
                     'emprises': {c: tuple(round(v, 4) for v in (g.bounds[1], g.bounds[0], g.bounds[3], g.bounds[2]))
                                  for c, g in communes.items()}}, fh)
    print(f'{nom_fichier} : département principal {principal}, communes {dict((d, len(g)) for d, g in par_dep.items())}, '
          f'{len(lieux)} lieux, {time.time() - t0:.0f} s')


if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2])
