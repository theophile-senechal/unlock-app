"""Blocs Unlock : grille, marge de tolérance GPS et blocs inaccessibles.

Une seule règle pour les traces et pour les chemins : un bloc est touché si la ligne passe
à l'intérieur ou à moins de MARGE_M mètres de son bord (imprécision d'un GPS : 5 à 15 m).

Grille : ligne i = round(lat / dlat), colonne j = round(lon / dl), dl = dlat / cos(lat de la ligne i).
Bloc « inaccessible » : aucune voie praticable (à pied ou à vélo) à moins de MARGE_M mètres,
d'après OpenStreetMap. Fichiers data/acces/<département>_<taille>.bin, produits par tools/acces.

Ce module est utilisé par l'app ET par l'outil de calcul : toute modification de la règle
doit être suivie d'un nouveau calcul des fichiers (tâche GitHub « Blocs inaccessibles »).
"""
import os, math, struct, zlib, threading
import numpy as np

M_PER_DEG = 111320.0
MARGE_M = 20.0
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'acces')
MAGIC = b'UNA1'

# Profils d'accès par sport (type Strava). Les autres sports ne sont pas suivis par l'app.
PIED = {'Run', 'TrailRun', 'Hike', 'Walk', 'Snowshoe'}
VELO = {'Ride', 'GravelRide', 'E-BikeRide', 'Velomobile', 'MountainBikeRide', 'EMountainBikeRide'}


def profil_of(sport):
    """'pied', 'velo' ou 'tous' (filtre « Tous les sports » : accessible à pied OU à vélo)."""
    if sport in PIED: return 'pied'
    if sport in VELO: return 'velo'
    return 'tous'


# ------------------------------------------------------------------ grille
def dl_of(lat_c, dlat):
    return dlat / np.maximum(0.05, np.cos(np.radians(lat_c)))


def _dl_py(lat_c, dlat):
    """Même calcul en Python pur : identique à celui qui produit les clés des blocs dans l'app."""
    return dlat / max(0.05, math.cos(math.radians(lat_c)))


def densify(lines, step_m):
    """Points le long des lignes (listes de (lat, lon)), espacés d'au plus step_m mètres."""
    lat_parts, lon_parts = [], []
    for pts in lines:
        a = np.asarray(pts, dtype=np.float64)
        if a.ndim != 2 or len(a) == 0: continue
        if len(a) == 1:
            lat_parts.append(a[:, 0]); lon_parts.append(a[:, 1]); continue
        p, q = a[:-1], a[1:]
        dy = (q[:, 0] - p[:, 0]) * M_PER_DEG
        dx = (q[:, 1] - p[:, 1]) * M_PER_DEG * np.cos(np.radians((q[:, 0] + p[:, 0]) / 2))
        n = np.maximum(1, np.ceil(np.hypot(dx, dy) / step_m).astype(np.int64))
        seg = np.repeat(np.arange(len(p)), n)
        frac = (np.arange(n.sum()) - np.repeat(np.cumsum(n) - n, n)) / np.repeat(n, n)
        lat_parts.append(np.append(p[seg, 0] + (q[seg, 0] - p[seg, 0]) * frac, a[-1, 0]))
        lon_parts.append(np.append(p[seg, 1] + (q[seg, 1] - p[seg, 1]) * frac, a[-1, 1]))
    if not lat_parts: return np.empty(0), np.empty(0)
    return np.concatenate(lat_parts), np.concatenate(lon_parts)


def point_cells(lat, lon, dlat, marge_m=MARGE_M):
    """Blocs (i, j) contenant chaque point ou à moins de marge_m mètres. Renvoie deux tableaux I, J."""
    size_m = dlat * M_PER_DEG
    i0 = np.round(lat / dlat).astype(np.int64)
    fy = lat / dlat - i0                       # position dans la ligne, de -0,5 à 0,5
    out_i, out_j = [], []
    for di in (-1, 0, 1):
        if di == 0:
            sel = np.ones(len(lat), bool); dy = np.zeros(len(lat))
        else:
            dy = (0.5 - di * fy) * size_m        # distance au bord partagé avec la ligne voisine
            sel = dy < marge_m
            dy = np.where(sel, dy, 0)
        if not sel.any(): continue
        I = i0[sel] + di; lo, dyy = lon[sel], dy[sel]
        dl = dl_of(I * dlat, dlat)
        J = np.round(lo / dl).astype(np.int64)
        fx = lo / dl - J
        out_i.append(I); out_j.append(J)
        if marge_m <= 0: continue
        for dj in (-1, 1):
            ok = np.hypot((0.5 - dj * fx) * size_m, dyy) < marge_m
            if ok.any():
                out_i.append(I[ok]); out_j.append(J[ok] + dj)
    if not out_i: return np.empty(0, np.int64), np.empty(0, np.int64)
    return np.concatenate(out_i), np.concatenate(out_j)


def ij_keys(I, J):
    """Clé entière unique et triable d'un bloc."""
    return np.asarray(I, np.int64) * 10_000_000 + (np.asarray(J, np.int64) + 5_000_000)


def keys_ij(k):
    k = np.asarray(k, np.int64)
    return k // 10_000_000, k % 10_000_000 - 5_000_000


def line_keys(lines, dlat, marge_m=MARGE_M, mask=None):
    """Clés (triées, uniques) des blocs touchés par des lignes. mask(lat, lon) -> points à garder."""
    step = min(10.0, dlat * M_PER_DEG * 0.1)
    lat, lon = densify(lines, step)
    if mask is not None and len(lat):
        keep = mask(lat, lon); lat, lon = lat[keep], lon[keep]
    if not len(lat): return np.empty(0, np.int64)
    I, J = point_cells(lat, lon, dlat, marge_m)
    return np.unique(ij_keys(I, J))


def trace_cells(pts, dlat, marge_m=MARGE_M):
    """Blocs d'une trace, au format de l'app : {(lat_centre, lon_centre)} arrondis à 6 décimales."""
    if not pts: return set()
    I, J = keys_ij(line_keys([pts], dlat, marge_m))
    out = set()
    for i, j in zip(I.tolist(), J.tolist()):
        lat_c = i * dlat
        out.add((round(lat_c, 6), round(j * _dl_py(lat_c, dlat), 6)))
    return out


def cell_key_of(lat_c, lon_c, dlat):
    """Bloc de l'app (lat, lon du centre) -> clé entière."""
    i = round(lat_c / dlat)
    return i * 10_000_000 + (round(lon_c / _dl_py(i * dlat, dlat)) + 5_000_000)


# ------------------------------------------------------------------ format des fichiers
def _varints(values, out):
    for v in values:
        v = (v << 1) ^ (v >> 63)                 # zigzag : petits nombres négatifs -> petits positifs
        while True:
            b = v & 0x7F; v >>= 7
            if v: out.append(b | 0x80)
            else: out.append(b); break


def pack_keys(keys):
    """Clés triées -> octets compressés (écarts successifs, varints, zlib)."""
    I, J = keys_ij(np.unique(np.asarray(keys, np.int64)))
    vals = []; pi = pj = 0
    for i, j in zip(I.tolist(), J.tolist()):
        di = i - pi
        vals.append(di); vals.append(j - pj if di == 0 else j)
        pi, pj = i, j
    out = bytearray(); _varints(vals, out)
    return zlib.compress(bytes(out), 9)


def unpack_keys(blob):
    """Inverse de pack_keys, vectorisé (numpy)."""
    b = np.frombuffer(zlib.decompress(blob), dtype=np.uint8)
    if not len(b): return np.empty(0, np.int64)
    end = (b & 0x80) == 0
    grp = np.concatenate(([0], np.cumsum(end)[:-1]))            # numéro du varint de chaque octet
    start = np.concatenate(([0], np.flatnonzero(end)[:-1] + 1))
    pos = np.arange(len(b)) - start[grp]
    # valeurs < 2**53 : la somme en flottants est exacte, et bincount est bien plus rapide que add.at
    v = np.bincount(grp, weights=((b & 0x7F).astype(np.int64) << (7 * pos)).astype(np.float64)).astype(np.int64)
    v = (v >> 1) ^ -(v & 1)
    di, dj = v[0::2], v[1::2]
    I = np.cumsum(di)
    # j : valeur absolue au début de chaque ligne, puis écarts successifs sur la même ligne
    starts = di != 0
    starts[0] = True
    run = np.cumsum(starts) - 1
    s = np.flatnonzero(starts)[run]
    cs = np.cumsum(dj)
    J = cs - (cs[s] - dj[s])
    return ij_keys(I, J)


def write_file(path, pied, velo):
    """Fichier d'un département pour une taille : couche à pied + différences vélo."""
    pied = np.unique(pied); velo = np.unique(velo)
    blobs = [pack_keys(pied), pack_keys(np.setdiff1d(velo, pied)), pack_keys(np.setdiff1d(pied, velo))]
    with open(path, 'wb') as fh:
        fh.write(MAGIC + struct.pack('<4I', 3, *[len(x) for x in blobs]))
        for x in blobs: fh.write(x)


def read_file(path):
    with open(path, 'rb') as fh: raw = fh.read()
    if raw[:4] != MAGIC: raise ValueError('format inconnu')
    n, *lens = struct.unpack('<4I', raw[4:20])
    pos = 20; blobs = []
    for L in lens[:n]:
        blobs.append(raw[pos:pos + L]); pos += L
    pied, plus, moins = (unpack_keys(x) for x in blobs)
    velo = np.sort(np.concatenate((pied[~_in_sorted(pied, moins)], plus)))
    return {'pied': pied, 'velo': velo, 'tous': pied[_in_sorted(pied, velo)]}


def _in_sorted(a, ref):
    """Pour chaque élément de a : présent dans ref (tableau trié) ? Plus rapide que np.isin ici."""
    if not len(ref): return np.zeros(len(a), bool)
    pos = np.searchsorted(ref, a)
    pos[pos == len(ref)] = 0
    return ref[pos] == a


# ------------------------------------------------------------------ lecture côté app
_CACHE = {}
_LOCK = threading.Lock()


def inaccessibles(dep, grid_m):
    """{'pied', 'velo', 'tous'} -> clés triées des blocs inaccessibles du département (vide si inconnu)."""
    k = (dep, int(grid_m))
    if k not in _CACHE:
        with _LOCK:
            if k not in _CACHE:
                path = os.path.join(DATA_DIR, f'{dep}_{int(grid_m)}.bin')
                try:
                    _CACHE[k] = read_file(path) if os.path.exists(path) else None
                except Exception as e:
                    print(f'Fichier blocs inaccessibles illisible {path}: {e}')
                    _CACHE[k] = None
    return _CACHE[k]


def filtre_inaccessibles(keys, dep, grid_m, profil):
    """Parmi ces clés (triées ou non), celles des blocs inaccessibles. Tableau vide si pas de données."""
    d = inaccessibles(dep, grid_m)
    keys = np.asarray(keys, np.int64)
    if not d or not len(keys): return np.empty(0, np.int64)
    return keys[_in_sorted(keys, d[profil])]


def keys_of_cells(cells, dlat):
    """Blocs de l'app [(lat_centre, lon_centre), ...] -> clés entières (tableau numpy)."""
    if not cells: return np.empty(0, np.int64)
    a = np.asarray(list(cells), dtype=np.float64)
    I = np.round(a[:, 0] / dlat).astype(np.int64)
    J = np.round(a[:, 1] / dl_of(I * dlat, dlat)).astype(np.int64)
    return ij_keys(I, J)


def cells_of_keys(keys, dlat):
    """Clés -> centres des blocs [(lat, lon), ...] arrondis comme dans l'app."""
    I, J = keys_ij(keys)
    out = []
    for i, j in zip(I.tolist(), J.tolist()):
        lat_c = i * dlat
        out.append((round(lat_c, 6), round(j * _dl_py(lat_c, dlat), 6)))
    return out
