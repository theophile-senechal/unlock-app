# Blocs inaccessibles

Un bloc est **inaccessible** quand aucune voie praticable ne passe à moins de 20 m de son bord
(routes ouvertes, rues, chemins, sentiers, chemins forestiers, pistes cyclables). Il n'est pas compté
dans le % de la commune, sauf si l'utilisateur y est passé. Terrains militaires et aérodromes : fermés.

Deux profils : **à pied** (course, trail, rando, marche, raquettes) et **vélo** (route, gravel, électrique).
Avec plusieurs sports affichés, un bloc est accessible s'il l'est à pied ou à vélo.

La même marge de 20 m s'applique aux traces (`acces.py`, à la racine) : un bloc est débloqué si la trace
passe à moins de 20 m de son bord. Toute modification de cette règle impose un nouveau calcul.

## Calcul (automatique)

Tâche GitHub `.github/workflows/blocs-inaccessibles.yml`, le 3 de chaque mois, ou à la main :
onglet **Actions** > **Blocs inaccessibles** > **Run workflow**. Environ 30 minutes.

1. `sources.py` : liste des fichiers par département sur download.openstreetmap.fr.
2. `calcul.py` : un département (téléchargement, lecture des voies, blocs à 100 m et 250 m).
3. `fusion.py` : corrige les bords entre départements, écrit `data/acces/<dep>_<taille>.bin` et `index.json`.

Le résultat (environ 10 Mo pour la France) est ajouté au dépôt seulement s'il a changé ; Vercel redéploie seul.
Rien n'est stocké dans Supabase.

## Calcul d'un seul département (sur un ordinateur)

```
pip install osmium shapely numpy
python tools/acces/calcul.py morbihan-latest.osm.pbf parts
python tools/acces/fusion.py parts data/acces
```

Données © contributeurs OpenStreetMap, licence ODbL (mention dans les mentions légales et la légende de la carte).
