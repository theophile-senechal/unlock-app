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

## Lieux à collectionner

La même tâche extrait les lieux historiques et culturels (`lieux_osm.py`) et `fusion.py` écrit
`data/lieux/<dep>.json` (environ 100 Ko par département). Un lieu entre dans la collection s'il a un nom et
qu'il est protégé (monument historique), décrit sur Wikipédia en français, ou d'un type marquant
(château, manoir, musée, fort, abbaye, mégalithe nommé). Nature : points de vue et cascades qui ont un vrai nom,
parcs et jardins à visiter (botaniques, arboretums, protégés ou sur Wikipédia), sommets sur Wikipédia.
Doublons retirés (même nom à moins de 250 m). `communes.json` : toutes les communes de France (nom, emprise,
nombre de lieux) pour la recherche dans l'app.
L'app le débloque quand une sortie passe à moins de 60 m (ou de l'emprise d'un grand site, 250 m au plus).
Rien n'est stocké dans Supabase.

## Calcul d'un seul département (sur un ordinateur)

```
pip install osmium shapely numpy
python tools/acces/calcul.py morbihan-latest.osm.pbf parts
python tools/acces/fusion.py parts data/acces
```

Données © contributeurs OpenStreetMap, licence ODbL (mention dans les mentions légales et la légende de la carte).
