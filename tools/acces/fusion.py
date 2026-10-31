"""Fusionne les calculs par fichier et écrit les fichiers de l'app.

    python tools/acces/fusion.py <dossier des .pkl> [data/acces]

Pour chaque département : ses blocs viennent de tous les fichiers qui contiennent ses communes.
Un bloc est accessible si AU MOINS UN fichier y trouve une voie à moins de la marge (le fichier du
département, ou celui d'un voisin pour un sentier qui longe la limite). Sinon il est inaccessible.
Les départements absents de ce calcul gardent leur fichier précédent.
"""
import os, sys, glob, json, pickle, datetime
from collections import defaultdict
import numpy as np

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))
import acces  # noqa: E402

SIZES = (100, 250)
PROFILS = ('pied', 'velo')


def main(src, out):
    paths = sorted(glob.glob(os.path.join(src, '**', '*.pkl'), recursive=True))
    if not paths: sys.exit('Aucun calcul trouvé')
    os.makedirs(out, exist_ok=True)
    index_path = os.path.join(out, 'index.json')
    try: index = json.load(open(index_path))
    except Exception: index = {'departements': {}}

    # Département -> nombre de communes, d'après le fichier qui en contient le plus
    nb_communes = defaultdict(int)
    parts = []
    for p in paths:
        d = pickle.load(open(p, 'rb'))
        parts.append(d)
        for dep, n in d['communes'].items(): nb_communes[dep] = max(nb_communes[dep], n)
    # On n'écrit que les départements dont le fichier a été calculé : les communes voisines présentes
    # dans un autre fichier servent seulement à compléter (sinon un département incomplet écraserait l'ancien)
    deps = sorted({d['principal'] for d in parts})
    absents = sorted(set(nb_communes) - set(deps))
    if absents: print('Vus seulement comme voisins (fichier précédent conservé) :', ' '.join(absents))

    for size in SIZES:
        # Blocs accessibles connus, tous fichiers confondus (par profil)
        acc = {nom: [] for nom in PROFILS}
        blocs = defaultdict(list)
        for d in parts:
            k = d['cles']
            for dep in d['communes']:
                b = acces.unpack_keys(k[f'{dep}|{size}|blocs']); blocs[dep].append(b)
                for nom in PROFILS:
                    acc[nom].append(np.setdiff1d(b, acces.unpack_keys(k[f'{dep}|{size}|{nom}']), assume_unique=True))
            for nom in PROFILS:
                acc[nom].append(acces.unpack_keys(k[f'*|{size}|{nom}']))
        acc = {nom: np.unique(np.concatenate(v)) for nom, v in acc.items()}
        for dep in deps:
            tous = np.unique(np.concatenate(blocs[dep]))
            res = {nom: np.setdiff1d(tous, acc[nom], assume_unique=True) for nom in PROFILS}
            acces.write_file(os.path.join(out, f'{dep}_{size}.bin'), res['pied'], res['velo'])
            e = index['departements'].setdefault(dep, {})
            e[str(size)] = {'blocs': int(len(tous)), 'pied': int(len(res['pied'])), 'velo': int(len(res['velo']))}
            e['communes'] = int(next(d['communes'][dep] for d in parts if d['principal'] == dep))
    index.update({'maj': datetime.date.today().isoformat(), 'marge_m': acces.MARGE_M,
                  'fichiers': len(paths), 'source': '© contributeurs OpenStreetMap (ODbL)'})
    json.dump(index, open(index_path, 'w'), ensure_ascii=False, indent=1, sort_keys=True)
    total = sum(os.path.getsize(p) for p in glob.glob(os.path.join(out, '*.bin')))
    print(f'{len(paths)} fichiers, {len(deps)} départements mis à jour, {total / 1e6:.1f} Mo au total')


if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else 'data/acces')
