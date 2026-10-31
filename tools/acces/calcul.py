"""Blocs inaccessibles d'un département, à partir d'un fichier OpenStreetMap (.osm.pbf).

    python tools/acces/calcul.py <fichier.osm.pbf ou URL> <dossier_sortie>

Produit <dossier_sortie>/<dep>.npz :
  - pour chaque taille (100, 250 m) et profil (pied, velo) : blocs inaccessibles des communes du département ;
  - les blocs accessibles HORS du département (bordure couverte par le fichier), pour corriger
    les départements voisins lors de la fusion (un sentier peut passer juste de l'autre côté de la limite).
"""
import os, sys, time, tempfile, urllib.request, shutil
from collections import Counter
import numpy as np
import osmium
import shapely, shapely.wkb

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))
import acces  # noqa: E402  (même grille et même marge que l'app)

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


def dep_of(code):
    return code[:3] if code.startswith('97') else code[:2]


def lire(path):
    pied, velo, communes, fermees = [], [], {}, []
    wkb = osmium.geom.WKBFactory()
    for o in osmium.FileProcessor(path).with_locations().with_areas():
        if o.is_way():
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
                try: communes[t.get('ref:INSEE')] = shapely.wkb.loads(wkb.create_multipolygon(o), hex=True)
                except Exception: pass
    return pied, velo, communes, fermees


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
        pied, velo, communes, fermees = lire(path)
    finally:
        if tmp: os.unlink(tmp.name)
    if not communes:
        print(f'{src} : aucune commune, ignoré'); return
    dep = Counter(dep_of(c) for c in communes).most_common(1)[0][0]
    propres = [g for c, g in communes.items() if dep_of(c) == dep]
    fermees = shapely.union_all([g.buffer(0) for g in fermees]) if fermees else None
    if fermees is not None: shapely.prepare(fermees)
    # Les voies sont en (lat, lon) ; les géométries OSM en (lon, lat)
    masque = (lambda la, lo: ~shapely.contains_xy(fermees, lo, la)) if fermees is not None else None

    res = {}
    for size in SIZES:
        dlat = size / acces.M_PER_DEG
        mine = cles_communes(propres, dlat)
        for nom, voies in (('pied', pied), ('velo', velo)):
            acc = acces.line_keys(voies, dlat, acces.MARGE_M, masque)
            res[f'{nom}_{size}'] = np.setdiff1d(mine, acc, assume_unique=True)     # inaccessibles du département
            res[f'ext_{nom}_{size}'] = np.setdiff1d(acc, mine, assume_unique=True)  # accessibles hors département
        print(f'  {size} m : {len(mine)} blocs, {len(res[f"pied_{size}"])} inaccessibles à pied')
    os.makedirs(outdir, exist_ok=True)
    np.savez_compressed(os.path.join(outdir, f'{dep}.npz'), communes=len(propres), **res)
    print(f'{dep} : {len(propres)} communes, {len(pied)} voies à pied, {time.time() - t0:.0f} s')


if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2])
