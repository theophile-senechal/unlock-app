"""Liste des fichiers OpenStreetMap par département (download.openstreetmap.fr), répartis en lots.

    python tools/acces/sources.py [nombre_de_lots]   -> JSON pour la matrice GitHub Actions

Lit la page d'index de chaque région française et garde les fichiers « <nom>-latest.osm.pbf ».
"""
import json, re, sys, urllib.request

BASE = 'https://download.openstreetmap.fr/extracts/europe/france/'
UA = {'User-Agent': 'unlock-app (calcul mensuel des blocs inaccessibles)'}


def lister(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60) as r:
        html = r.read().decode('utf-8', 'replace')
    return re.findall(r'href="([^"?#]+)"', html)


def sources():
    urls = []
    for href in lister(BASE):
        if not href.endswith('/') or href.startswith(('/', '.', 'http')): continue
        region = BASE + href
        try:
            urls += [region + f for f in lister(region) if f.endswith('-latest.osm.pbf') and '/' not in f]
        except Exception as e:
            print(f'Région illisible {region}: {e}', file=sys.stderr)
    return sorted(set(urls))


if __name__ == '__main__':
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 12
    urls = sources()
    if len(urls) < 90:
        sys.exit(f'Seulement {len(urls)} départements trouvés : la structure du site a peut-être changé')
    lots = [urls[i::n] for i in range(n)]
    print(json.dumps({'lot': [{'id': i, 'urls': ' '.join(l)} for i, l in enumerate(lots) if l]}))
