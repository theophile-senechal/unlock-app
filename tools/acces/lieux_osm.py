"""Lieux à collectionner, d'après les attributs OpenStreetMap.

Patrimoine : un lieu entre dans la collection s'il a un nom ET qu'il est
  - protégé au titre des monuments historiques (ref:mhs, heritage:operator=mhs),
  - ou décrit par un article Wikipédia en français,
  - ou d'un type marquant par nature (château, manoir, musée, fort, abbaye, mégalithe nommé…).
Les croix de chemin, monuments aux morts, chapelles ordinaires, bunkers… ne comptent que s'ils sont protégés
ou décrits sur Wikipédia : sinon il y en aurait des dizaines de milliers et la collection perdrait son sens.

Nature :
  - points de vue et cascades : seulement s'ils ont un vrai nom (pas « Point de vue », « Cascade »…) ;
  - parcs et jardins : jardins botaniques, arboretums, jardins ouverts à la visite, parcs protégés ou décrits
    sur Wikipédia (pas les espaces verts de quartier) ;
  - sommets : seulement ceux qui ont un article Wikipédia (il y en a des milliers en montagne).
"""
import os, re, sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))
from lieux import CATEGORIES  # noqa: E402,F401  (mêmes catégories que l'app)

_GENERIQUE = re.compile(
    r'^(chapelle|église|eglise|calvaire|croix|moulin|lavoir|fontaine|monument|ruines?|tumulus|menhir|dolmen|four à pain|manoir|'
    r'château|chateau|musée|musee|stèle|stele|statue|puits|pont|tour|batterie|fort|porte|cimetière|cimetiere|'
    r'point de vue|points de vue|panorama|belvédère|belvedere|vue|viewpoint|table d.orientation|cascade|chute|chute d.eau|'
    r'jardin|jardins|parc|square|espace vert|sommet|pic|mont|observatoire|arboretum|échauguette|echauguette)s?$', re.I)

_HORS = ('', 'memorial', 'wayside_cross', 'bunker', 'wreck', 'aircraft', 'ship', 'tank', 'cannon', 'bomb_crater',
         'boundary_stone', 'milestone')


def est_mh(t):
    return 'ref:mhs' in t or 'mhs' in t.get('heritage:operator', '')


def wiki_of(t):
    """'fr:Titre' (Wikipédia en français) ou 'Q123' (Wikidata), sinon ''."""
    w = t.get('wikipedia', '')
    if w.startswith('fr:'): return w[:120]
    q = t.get('wikidata', '')
    return q if re.fullmatch(r'Q\d+', q or '') else ''


def classer(t):
    """(catégorie, monument historique ?) ou None si le lieu n'entre pas dans la collection."""
    nom = (t.get('name') or '').strip()
    if not nom or _GENERIQUE.match(nom): return None
    h, tour, am, le, nat = t.get('historic', ''), t.get('tourism', ''), t.get('amenity', ''), t.get('leisure', ''), t.get('natural', '')
    mh = est_mh(t)
    wp = t.get('wikipedia', '').startswith('fr:')
    # Protégé, ou article Wikipédia en français (un simple identifiant Wikidata vient souvent d'imports automatiques)
    notable = mh or wp

    # --- Patrimoine ---
    if h in ('castle', 'manor', 'palace') or t.get('castle_type'):
        return 'chateau', mh
    if tour == 'museum' or (am in ('theatre', 'arts_centre') and notable):   # salles municipales ordinaires exclues
        return 'musee', mh
    if h in ('fort', 'battery', 'city_gate', 'citywalls', 'tower') or t.get('defensive_works'):
        return 'fortif', mh
    if h in ('archaeological_site', 'menhir', 'dolmen', 'megalith', 'tumulus', 'stone', 'rune_stone', 'roman_road', 'aqueduct') \
            or t.get('megalith_type') or t.get('site_type') == 'megalith':
        return 'megalithe', mh
    if h in ('monastery', 'abbey') or t.get('building') == 'cathedral':
        return 'religieux', mh
    if am == 'place_of_worship' or h in ('church', 'chapel', 'wayside_shrine') or t.get('building') in ('church', 'chapel', 'basilica'):
        return ('religieux', mh) if notable else None
    if h == 'monument':
        return 'monument', mh

    # --- Nature ---
    if tour == 'viewpoint':
        return 'vue', mh
    if t.get('waterway') == 'waterfall' or nat == 'waterfall':
        return 'cascade', mh
    if le in ('park', 'garden') or tour == 'garden':
        visite = t.get('garden:type') in ('botanical', 'arboretum') or tour in ('attraction', 'garden') \
            or re.search(r'jardin botanique|arboretum|jardin des plantes|conservatoire botanique', nom, re.I)
        return ('jardin', mh) if (notable or visite) else None
    if nat in ('peak', 'volcano'):
        return ('sommet', mh) if wp else None

    # --- Autres monuments protégés ou décrits sur Wikipédia ---
    if notable and (h not in _HORS or t.get('man_made') in ('lighthouse', 'windmill', 'watermill') or tour in ('attraction', 'artwork')):
        return 'monument', mh
    if mh and h in ('memorial', 'wayside_cross'):          # calvaires et monuments protégés
        return 'monument', mh
    return None
