"""Fusionne les calculs par département et écrit les fichiers de l'app.

    python tools/acces/fusion.py <dossier des .npz> [data/acces]

Un bloc reste inaccessible seulement si AUCUN fichier (département ou voisin) n'y trouve une voie
à moins de la marge. Les départements absents de ce calcul gardent leur fichier précédent.
"""
import os, sys, glob, json, datetime
import numpy as np

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))
import acces  # noqa: E402

SIZES = (100, 250)


def main(src, out):
    parts = {os.path.basename(p)[:-4]: dict(np.load(p)) for p in sorted(glob.glob(os.path.join(src, '**', '*.npz'), recursive=True))}
    if not parts: sys.exit('Aucun calcul trouvé')
    os.makedirs(out, exist_ok=True)
    index_path = os.path.join(out, 'index.json')
    try: index = json.load(open(index_path))
    except Exception: index = {'departements': {}}
    for size in SIZES:
        ext = {p: np.unique(np.concatenate([d[f'ext_{p}_{size}'] for d in parts.values()])) for p in ('pied', 'velo')}
        for dep, d in parts.items():
            pied = np.setdiff1d(d[f'pied_{size}'], ext['pied'], assume_unique=True)
            velo = np.setdiff1d(d[f'velo_{size}'], ext['velo'], assume_unique=True)
            acces.write_file(os.path.join(out, f'{dep}_{size}.bin'), pied, velo)
            e = index['departements'].setdefault(dep, {})
            e[str(size)] = {'pied': int(len(pied)), 'velo': int(len(velo))}
            e['communes'] = int(d['communes'])
    index.update({'maj': datetime.date.today().isoformat(), 'marge_m': acces.MARGE_M,
                  'source': '© contributeurs OpenStreetMap (ODbL)'})
    json.dump(index, open(index_path, 'w'), ensure_ascii=False, indent=1, sort_keys=True)
    total = sum(os.path.getsize(p) for p in glob.glob(os.path.join(out, '*.bin')))
    print(f'{len(parts)} départements mis à jour, {total / 1e6:.1f} Mo au total')


if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else 'data/acces')
