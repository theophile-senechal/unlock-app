import os
import re
import time
import secrets
import requests
from urllib.parse import urlencode
import polyline
import json
from flask import Flask, redirect, request, jsonify, session, render_template, url_for, send_from_directory, abort, make_response
from dotenv import load_dotenv
from datetime import datetime, timedelta
import math
import numpy as np
import acces   # grille des blocs, marge GPS, blocs inaccessibles (données OpenStreetMap)
import lieux   # lieux historiques et culturels à collectionner (données OpenStreetMap)
import icons   # icônes au trait (pages et JavaScript)
import shapely
from shapely.geometry import Point, Polygon, shape as shp_shape
from shapely.ops import transform as shp_transform
from shapely.prepared import prep
from collections import defaultdict
from sqlalchemy import create_engine, text, table as sa_table, column as sa_column
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.pool import NullPool

# ============================================================
# 1. CONFIGURATION INITIALE
# ============================================================
load_dotenv()
app = Flask(__name__)
app.jinja_env.globals['icon'] = icons.icon
ICONS_JSON = json.dumps(icons.ICONS, separators=(',', ':'))
app.secret_key = os.getenv('SECRET_KEY', 'dev_secret_key_123')
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=os.getenv('VERCEL') is not None or os.getenv('SESSION_COOKIE_SECURE') == '1',
    # Connexion gardée 30 jours après la dernière visite (le délai est prolongé à chaque visite).
    # Sans cela, le cookie disparaît à la fermeture du navigateur et il faut repasser par Strava.
    # 30 jours < 31 jours : un compte inactif est révoqué après la fin de sa session (INACTIVE_DAYS).
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
    SESSION_REFRESH_EACH_REQUEST=True,
)

# ------------------------------------------------------------
# Fonctionnalités / infos légales
# ------------------------------------------------------------
# Classement désactivé par défaut : il affiche les données Strava d'autres
# utilisateurs et les agrège par commune, ce que la Strava API Policy (juin 2026)
# interdit (§2.3, §5.4, §5.7, §6.2). Ne le réactiver qu'après accord écrit de Strava.
ENABLE_LEADERBOARD = os.getenv('ENABLE_LEADERBOARD', 'false').lower() == 'true'

SITE_OWNER = os.getenv('SITE_OWNER', 'Théophile Sénéchal')
LINKEDIN_URL = 'https://www.linkedin.com/in/th%C3%A9ophile-s%C3%A9n%C3%A9chal-5119181a9/'
# Dons (facultatif) : lien de la page Ko-fi. Vide = rien ne s'affiche dans l'app.
KOFI_URL = os.getenv('KOFI_URL', '').strip()
if KOFI_URL and not KOFI_URL.startswith('https://'): KOFI_URL = ''
ASSET_VERSION = '2026.11.06'

# Utilisateurs actifs = au moins une connexion sur cette période (affiché sur la page de connexion)
ACTIVE_WINDOW_DAYS = 31
# Sans connexion pendant ce délai, l'accès Strava est révoqué et les données effacées :
# ça libère une place sur le quota d'athlètes autorisé par Strava.
INACTIVE_DAYS = int(os.getenv('INACTIVE_DAYS', '31'))
# En dessous de ce nombre, le compteur n'est pas affiché (évite « 2 explorateurs actifs »)
MIN_ACTIVE_DISPLAY = int(os.getenv('MIN_ACTIVE_DISPLAY', '5'))
# Quota d'athlètes accordé par Strava, et identifiants Strava des admins (séparés par des virgules)
STRAVA_ATHLETE_CAPACITY = int(os.getenv('STRAVA_ATHLETE_CAPACITY', '999'))
ADMIN_ATHLETE_IDS = {x.strip() for x in os.getenv('ADMIN_ATHLETE_IDS', '').split(',') if x.strip()}

def is_admin():
    return bool(ADMIN_ATHLETE_IDS) and str(session.get('athlete_id')) in ADMIN_ATHLETE_IDS
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static')

ALLOWED_GRID_SIZES = (100, 250)

def _grid_size(default=250):
    """Taille de bloc demandée, limitée à 100 m ou 250 m."""
    value = request.args.get('grid_size', default, type=int)
    return value if value in ALLOWED_GRID_SIZES else default

NAV_ITEMS = [
    ('map', '/', 'map', 'Carte'),
    ('lieux', '/lieux', 'landmark', 'Lieux'),
    ('timelapse', '/timelapse', 'play', 'Timelapse'),
    ('story', '/story', 'story', 'Story'),
    ('stats', '/stats', 'stats', 'Stats'),
]

# Version des CGU et de la politique de confidentialité : la changer redemande l'accord de chacun
CGU_VERSION = '2026-11-02'
_LEGAL_PATHS = ('/cgu', '/confidentialite', '/mentions-legales', '/conditions/accepter', '/logout', '/delete-account')

def _save_cgu(athlete_id):
    """Garde la preuve de l'accord (version et date) avec le compte. Sans la migration, l'accord reste dans la session."""
    if not (DB_URL and athlete_id): return
    try:
        with _engine().begin() as conn:
            conn.execute(text("UPDATE strava_users SET cgu_version = :v, cgu_accepted_at = NOW() WHERE athlete_id = :a"),
                         {"v": CGU_VERSION, "a": athlete_id})
    except Exception as e:
        print(f"Accord CGU (migration 2026-11-02_cgu.sql faite ?) : {e}")

@app.before_request
def keep_session():
    """Sessions ouvertes avant cette mise à jour : elles deviennent durables à la visite suivante."""
    if 'access_token' in session and not session.permanent:
        session.permanent = True

@app.before_request
def backfill_athlete_id():
    """Sessions ouvertes avant la mise à jour : elles n'ont pas l'identifiant Strava.
    On le retrouve (base, sinon API Strava) une seule fois, pour l'afficher et activer le cache."""
    if 'access_token' not in session or session.get('athlete_id') or session.get('aid_checked'):
        return
    if request.path.startswith('/static') or request.path in ('/sw.js', '/manifest.webmanifest', '/favicon.ico'):
        return
    session['aid_checked'] = True
    token, aid = session['access_token'], None
    if DB_URL:
        try:
            with _engine().begin() as conn:
                row = conn.execute(text("SELECT athlete_id FROM strava_users WHERE access_token = :t"), {"t": token}).fetchone()
            aid = row[0] if row else None
        except Exception as e:
            print(f"Recherche de l'identifiant : {e}")
    if not aid:
        try:
            r = requests.get("https://www.strava.com/api/v3/athlete",
                             headers={'Authorization': f'Bearer {token}'}, timeout=5)
            if r.status_code == 200:
                aid = r.json().get('id')
        except Exception as e:
            print(f"Identifiant via Strava : {e}")
    if aid:
        session['athlete_id'] = aid

@app.context_processor
def inject_globals():
    if 'csrf' not in session:
        session['csrf'] = secrets.token_urlsafe(24)
    return {
        'nav_items': NAV_ITEMS,
        'linkedin_url': LINKEDIN_URL,
        'donations_enabled': bool(KOFI_URL),
        'asset_v': ASSET_VERSION,
        'leaderboard_enabled': ENABLE_LEADERBOARD,
        'session_user': 'access_token' in session,
        'csrf_token': session['csrf'],
        'site_owner': SITE_OWNER,
        'legal_updated': '2 novembre 2026',
        'inactive_days': INACTIVE_DAYS,
        'is_admin': is_admin(),
        'carto_key': os.getenv('CARTO_API_KEY', ''),
        'my_athlete_id': session.get('athlete_id'),
        'sync_state': sync_state(),
        # Utilisateur connecté qui n'a pas encore accepté cette version des CGU : fenêtre d'accord (sauf pages légales)
        'cgu_needed': 'access_token' in session and session.get('cgu') != CGU_VERSION and request.path not in _LEGAL_PATHS,
        'cgu_version': CGU_VERSION,
        'icons_json': ICONS_JSON,
        # Bilan de l'année : entrée du menu et carte d'annonce selon le calendrier (aperçu pour l'admin)
        'annee': annee_context(),
    }

def sync_state():
    """Durée probable du prochain chargement, pour adapter les messages d'attente :
    'first' (jamais synchronisé : tout l'historique Strava est lu, quelques minutes),
    'refresh' (cache de 47 h expiré : relecture complète, une à deux minutes), 'fast' (quelques secondes)."""
    if 'access_token' not in session: return 'fast'
    if _user_key() in RAW_DATA_CACHE: return 'fast'
    aid = session.get('athlete_id')
    if not (DB_URL and aid): return 'first'
    try:
        with _engine().connect() as conn:
            row = conn.execute(text("""SELECT full_sync_at IS NULL AS never,
                                              full_sync_at > NOW() - make_interval(hours => :h) AS fresh
                                       FROM strava_users WHERE athlete_id = :a"""),
                               {"h": DB_CACHE_MAX_AGE_HOURS, "a": aid}).fetchone()
    except Exception:
        return 'fast'
    if not row or row.never: return 'first'
    return 'fast' if row.fresh else 'refresh'

# ============================================================
# 2. CONFIGURATION DATABASE
# ============================================================
DB_URL = os.getenv('DATABASE_URL')

# ============================================================
# 3. CONFIGURATION STRAVA
# ============================================================
CLIENT_ID = os.getenv('STRAVA_CLIENT_ID')
CLIENT_SECRET = os.getenv('STRAVA_CLIENT_SECRET')
REDIRECT_URI = os.getenv('STRAVA_REDIRECT_URI', 'http://localhost:5000/callback')

# ============================================================
# 4. SPORTS
# ============================================================
# Sports suivis : à pied ou à vélo uniquement (ski, natation et sports virtuels ne débloquent rien)
SPORT_TRANSLATIONS = {
    'Run': 'Course à pied', 'TrailRun': 'Trail', 'Hike': 'Randonnée', 'Walk': 'Marche', 'Snowshoe': 'Raquettes',
    'Ride': 'Vélo', 'GravelRide': 'Gravel', 'E-BikeRide': 'Vélo Électrique', 'Velomobile': 'Vélomobile'
}
GPS_SPORTS = list(SPORT_TRANSLATIONS.keys())

# ============================================================
# 5. CACHES
# ============================================================
# Strava API Policy :
#   §6.2 : pas plus de 7 jours de cache ;
#   §6.3 : une activité supprimée sur Strava doit disparaître sous 48 h.
# → Les activités sont gardées en base (table `activities`) au maximum 47 h,
#   puis rechargées entièrement. Entre-temps, seules les nouvelles sont demandées.
DB_CACHE_MAX_AGE_HOURS = 47
MEMORY_TTL_SECONDS = 600          # petit cache mémoire (même instance serveur)
INCREMENTAL_OVERLAP_SECONDS = 3 * 86400   # rattrape les activités envoyées en retard

RAW_DATA_CACHE = {}     # user_key -> (timestamp, activities)
API_RESULT_CACHE = {}   # user_key -> {"_ts": timestamp, cache_key: data}

def _user_key():
    return str(session.get('athlete_id') or session.get('access_token'))

def _purge_expired_caches():
    now = time.time()
    for k in [k for k, (ts, _) in RAW_DATA_CACHE.items() if now - ts > MEMORY_TTL_SECONDS]:
        RAW_DATA_CACHE.pop(k, None)
    for k in [k for k, v in API_RESULT_CACHE.items() if now - v.get('_ts', 0) > MEMORY_TTL_SECONDS]:
        API_RESULT_CACHE.pop(k, None)

def _result_cache(user_key):
    _purge_expired_caches()
    if user_key not in API_RESULT_CACHE:
        API_RESULT_CACHE[user_key] = {'_ts': time.time()}
    return API_RESULT_CACHE[user_key]

def _data_version(activities):
    """Change dès qu'une activité arrive : invalide les résultats calculés."""
    if not activities: return "0"
    return f"{len(activities)}-{max(str(a['id']) for a in activities)}"

# ============================================================
# 6. FONCTIONS UTILITAIRES
# ============================================================
M_PER_DEG = 111320.0
MAX_COMMUNES = 300   # communes analysées au maximum (les plus fréquentées d'abord)

def _dlon(lat_c, dlat):
    """Largeur d'un bloc en degrés de longitude à cette latitude, pour des blocs vraiment carrés
    (un degré de longitude rétrécit avec la latitude : ~76 km en Bretagne, contre 111 km pour la latitude)."""
    return dlat / max(0.05, math.cos(math.radians(lat_c)))

def cell_key(lat, lon, dlat):
    lat_c = round(lat / dlat) * dlat
    dl = _dlon(lat_c, dlat)
    return (round(lat_c, 6), round(round(lon / dl) * dl, 6))

def get_cells_from_polyline(pts, grid_size_deg):
    """Blocs carrés (côté = grid_size_deg × 111,32 km) traversés par une trace, ou longés à moins de
    20 m de leur bord : marge d'imprécision du GPS, la même que pour les chemins (voir acces.py)."""
    return acces.trace_cells(pts, grid_size_deg)

def commune_cells(geom, dlat):
    """Tous les blocs dont le centre est dans la commune (géométrie en (lat, lon), toutes ses parties)."""
    min_lat, min_lon, max_lat, max_lon = geom.bounds
    lats, lons = [], []
    for i in range(math.floor(min_lat / dlat), math.ceil(max_lat / dlat) + 1):
        lat_c = i * dlat
        dl = _dlon(lat_c, dlat)
        js = range(math.floor(min_lon / dl), math.ceil(max_lon / dl) + 1)
        lats.extend([lat_c] * len(js)); lons.extend(j * dl for j in js)
    if not lats: return set()
    la, lo = np.array(lats), np.array(lons)
    inside = shapely.contains_xy(geom, la, lo)
    return {(round(float(x), 6), round(float(y), 6)) for x, y in zip(la[inside], lo[inside])}

def get_current_iso_period():
    year, week, _ = datetime.now().isocalendar()
    return f"{year}-W{week:02d}"

def get_current_week_display():
    today = datetime.now()
    iso_year, iso_week, _ = today.isocalendar()
    week_start = datetime.fromisocalendar(iso_year, iso_week, 1)
    week_end = datetime.fromisocalendar(iso_year, iso_week, 7)
    return f"{week_start:%d/%m/%Y} - {week_end:%d/%m/%Y}"

# ============================================================
# 7. ACTIVITÉS STRAVA
# ============================================================
class StravaAuthError(Exception):
    """Le jeton Strava n'est plus valide et n'a pas pu être renouvelé."""

def _valid_token():
    """Renvoie un jeton d'accès valide, en le renouvelant si besoin (ils expirent après 6 h)."""
    token = session.get('access_token')
    expires_at = session.get('expires_at') or 0
    refresh = session.get('refresh_token')
    if not refresh or expires_at > time.time() + 120:
        return token
    try:
        r = requests.post("https://www.strava.com/oauth/token", data={
            'client_id': CLIENT_ID, 'client_secret': CLIENT_SECRET,
            'grant_type': 'refresh_token', 'refresh_token': refresh}, timeout=10)
    except Exception:
        return token
    if r.status_code != 200:
        raise StravaAuthError()
    d = r.json()
    session['access_token'] = d['access_token']
    session['refresh_token'] = d.get('refresh_token', refresh)
    session['expires_at'] = d.get('expires_at', 0)
    if DB_URL and session.get('athlete_id'):
        try:
            with _engine().begin() as conn:
                conn.execute(text("UPDATE strava_users SET access_token = :t, refresh_token = :r WHERE athlete_id = :a"),
                             {"t": d['access_token'], "r": session['refresh_token'], "a": session['athlete_id']})
        except Exception as e:
            print(f"Mise à jour du jeton : {e}")
    return d['access_token']

def _clean_activity(act):
    start_utc = act.get('start_date') or act['start_date_local']
    return {
        'id': str(act.get('id')),
        'type': act['type'],
        'start_date_local': act['start_date_local'],
        'start_epoch': int(datetime.fromisoformat(start_utc.replace('Z', '+00:00')).timestamp()),
        'polyline': act['map']['summary_polyline'],
        'distance': act.get('distance', 0) or 0,
    }

def _fetch_strava(token, after=None):
    """Toutes les activités GPS (ou celles après `after`). None si Strava ne répond pas correctement :
    on ne veut jamais enregistrer un historique incomplet."""
    out, page = [], 1
    while page <= 10:
        params = {'per_page': 200, 'page': page}
        if after: params['after'] = int(after)
        try:
            r = requests.get("https://www.strava.com/api/v3/athlete/activities",
                             headers={'Authorization': f'Bearer {token}'}, params=params, timeout=15)
        except Exception:
            return None
        _record_rate_limit(r)
        if r.status_code == 401:
            raise StravaAuthError()
        if r.status_code != 200:
            return None
        data = r.json()
        if not data: break
        out.extend(data)
        if len(data) < 200: break
        page += 1
    return [_clean_activity(a) for a in out
            if a.get('type') in GPS_SPORTS and (a.get('map') or {}).get('summary_polyline')]

_ENGINE = None
def _engine():
    global _ENGINE
    if _ENGINE is None:
        # SQLAlchemy ≥ 2.1 choisit le pilote "psycopg" (v3) par défaut ; on force psycopg2 (requirements.txt)
        url = DB_URL
        for prefix in ('postgres://', 'postgresql://'):
            if url.startswith(prefix):
                url = 'postgresql+psycopg2://' + url[len(prefix):]
        _ENGINE = create_engine(url, poolclass=NullPool)
    return _ENGINE

# ---------- Journal anonyme pour le tableau de bord admin ----------
# Aucun identifiant d'utilisateur : seulement un type d'événement et une date.
EVENT_KINDS = ('signup', 'login', 'visit', 'logout', 'delete', 'revoke_inactive')

def log_event(kind):
    if not DB_URL: return
    try:
        with _engine().begin() as conn:
            conn.execute(text("INSERT INTO app_events (kind) VALUES (:k)"), {"k": kind})
    except Exception as e:
        print(f"Journal ({kind}) : {e}")

def kv_set(key, value):
    if not DB_URL: return
    try:
        with _engine().begin() as conn:
            conn.execute(text("""INSERT INTO app_kv (key, value, updated_at) VALUES (:k, CAST(:v AS jsonb), NOW())
                                 ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()"""),
                         {"k": key, "v": json.dumps(value)})
    except Exception as e:
        print(f"app_kv ({key}) : {e}")

_RATE = {'ts': 0}
def _record_rate_limit(resp):
    """Mémorise l'utilisation des quotas Strava (en-têtes X-RateLimit / X-ReadRateLimit), au plus 1 fois/min."""
    h = resp.headers if hasattr(resp, 'headers') else {}
    if not h or 'X-RateLimit-Usage' not in h: return
    def pair(name):
        try: return [int(x) for x in h.get(name, '').split(',')[:2]]
        except ValueError: return None
    data = {'usage': pair('X-RateLimit-Usage'), 'limit': pair('X-RateLimit-Limit'),
            'read_usage': pair('X-ReadRateLimit-Usage'), 'read_limit': pair('X-ReadRateLimit-Limit'),
            'at': datetime.utcnow().isoformat() + 'Z'}
    _RATE.update(data)
    if time.time() - _RATE['ts'] > 60:
        _RATE['ts'] = time.time()
        kv_set('strava_rate_limit', data)

NAME_DAYS = 7   # nom et prénom gardés 7 jours au plus après leur lecture (Strava API Policy §6.2)

def _save_name(athlete_id, info):
    """Nom et prénom Strava, visibles uniquement dans /admin (§5.13 : le développeur seul)."""
    if not (DB_URL and athlete_id and info): return
    try:
        with _engine().begin() as conn:
            conn.execute(text("""UPDATE strava_users SET first_name = :f, last_name = :l, name_at = NOW()
                                 WHERE athlete_id = :a"""),
                         {"f": (info.get('firstname') or '').strip()[:80] or None,
                          "l": (info.get('lastname') or '').strip()[:80] or None, "a": athlete_id})
    except Exception as e:
        print(f"Nom de l'athlète (migration 2026-11-01_noms_admin.sql faite ?) : {e}")

def _refresh_name(token):
    """Relit le nom au plus une fois par jour et par session : il reste à jour et n'a jamais plus de 7 jours."""
    aid = session.get('athlete_id')
    if not (DB_URL and aid) or time.time() - (session.get('name_at') or 0) < 86400: return
    session['name_at'] = time.time()
    try:
        r = requests.get("https://www.strava.com/api/v3/athlete", headers={'Authorization': f'Bearer {token}'}, timeout=8)
        _record_rate_limit(r)
        if r.status_code == 200: _save_name(aid, r.json())
    except Exception as e:
        print(f"Lecture du nom : {e}")

def _touch_visit(old_last_login):
    """Compte une visite unique par utilisateur et par jour."""
    if old_last_login is None or old_last_login.date() < datetime.now().date():
        log_event('visit')

_ACT_TABLE = sa_table('activities', sa_column('activity_id'), sa_column('athlete_id'), sa_column('start_date_local'),
                      sa_column('type'), sa_column('distance'), sa_column('polyline'), sa_column('start_epoch'),
                      sa_column('fetched_at'))

def _db_sync(token, athlete_id):
    """Synchronise la table `activities` puis la renvoie. None si impossible (on passe alors sans base)."""
    with _engine().begin() as conn:
        row = conn.execute(text("""SELECT full_sync_at > NOW() - make_interval(hours => :h) AS fresh
                                   FROM strava_users WHERE athlete_id = :a"""),
                           {"h": DB_CACHE_MAX_AGE_HOURS, "a": athlete_id}).fetchone()
        fresh = bool(row and row.fresh)
        if fresh:
            last = conn.execute(text("SELECT COALESCE(MAX(start_epoch), 0) FROM activities WHERE athlete_id = :a"),
                                {"a": athlete_id}).scalar() or 0
            new = _fetch_strava(token, after=max(0, last - INCREMENTAL_OVERLAP_SECONDS)) if last else _fetch_strava(token)
            new = new or []          # Strava indisponible : on sert le cache, encore valide
        else:
            new = _fetch_strava(token)
            if new is None:
                return None
            conn.execute(text("DELETE FROM activities WHERE athlete_id = :a"), {"a": athlete_id})
            conn.execute(text("""INSERT INTO strava_users (athlete_id, access_token, full_sync_at)
                                 VALUES (:a, :t, NOW())
                                 ON CONFLICT (athlete_id) DO UPDATE SET full_sync_at = NOW()"""),
                         {"a": athlete_id, "t": token})
        for i in range(0, len(new), 500):
            chunk = [{**{k: v for k, v in x.items() if k != 'id'}, 'activity_id': x['id'], 'athlete_id': athlete_id}
                     for x in new[i:i + 500]]
            stmt = pg_insert(_ACT_TABLE).values(chunk)
            stmt = stmt.on_conflict_do_update(index_elements=['activity_id'], set_={
                'type': stmt.excluded.type, 'distance': stmt.excluded.distance, 'polyline': stmt.excluded.polyline,
                'start_date_local': stmt.excluded.start_date_local, 'start_epoch': stmt.excluded.start_epoch,
                'athlete_id': stmt.excluded.athlete_id})
            conn.execute(stmt)
        rows = conn.execute(text("""SELECT activity_id, type, start_date_local, polyline, distance
                                    FROM activities WHERE athlete_id = :a ORDER BY start_epoch"""),
                            {"a": athlete_id}).fetchall()
    return [{'id': r.activity_id, 'type': r.type, 'start_date_local': r.start_date_local,
             'polyline': r.polyline, 'distance': r.distance or 0} for r in rows]

def get_strava_activities_cached(_token=None):
    """Activités GPS de l'utilisateur connecté : mémoire (10 min) → base (47 h, + nouveautés) → Strava."""
    _purge_expired_caches()
    key = _user_key()
    if key in RAW_DATA_CACHE:
        return RAW_DATA_CACHE[key][1]
    token = _valid_token()
    athlete_id = session.get('athlete_id')
    acts = None
    if DB_URL and athlete_id:
        try:
            acts = _db_sync(token, athlete_id)
        except StravaAuthError:
            raise
        except Exception as e:
            print(f"Cache base indisponible, lecture directe Strava : {e}")
    if acts is None:
        acts = _fetch_strava(token) or []
        acts = [{k: v for k, v in a.items() if k != 'start_epoch'} for a in sorted(acts, key=lambda a: a['start_epoch'])]
    # Sports non suivis (le cache en base peut encore en contenir pendant 47 h)
    acts = [a for a in acts if a.get('type') in SPORT_TRANSLATIONS]
    RAW_DATA_CACHE[key] = (time.time(), acts)
    return acts

@app.errorhandler(StravaAuthError)
def strava_auth_error(e):
    session.clear()
    if request.path.startswith('/api/'):
        return jsonify({"error": "Session Strava expirée"}), 401
    return redirect(url_for('login_page', error='expired'))

# ============================================================
# 8. ROUTES STANDARD
# ============================================================
def _require_login():
    return 'access_token' not in session

def _deauthorize(token):
    """Révoque l'accès d'Unlock au compte Strava (libère une place sur le quota d'athlètes).
    NB : Strava remplace /oauth/deauthorize par /oauth/revoke le 1er juin 2027."""
    if not token: return False
    try:
        r = requests.post("https://www.strava.com/oauth/deauthorize",
                          headers={'Authorization': f'Bearer {token}'}, timeout=5)
        return r.status_code == 200
    except Exception as e:
        print(f"Erreur lors de la révocation Strava : {e}")
        return False

def _deauthorize_stored(access_token, refresh_token):
    """Révocation depuis le cron : le jeton d'accès enregistré a expiré (6 h),
    on en redemande un avec le refresh token avant de révoquer."""
    if refresh_token:
        try:
            r = requests.post("https://www.strava.com/oauth/token", data={
                'client_id': CLIENT_ID, 'client_secret': CLIENT_SECRET,
                'grant_type': 'refresh_token', 'refresh_token': refresh_token}, timeout=10)
            if r.status_code == 200:
                return _deauthorize(r.json().get('access_token'))
            return False   # accès déjà retiré par l'utilisateur côté Strava : place déjà libérée
        except Exception as e:
            print(f"Renouvellement avant révocation : {e}")
    return _deauthorize(access_token)

_ACTIVE_CACHE = {'ts': 0, 'n': None}
def active_users_count():
    """Nombre d'utilisateurs connectés au moins une fois sur les 31 derniers jours (mis en cache 10 min)."""
    if not DB_URL: return None
    if time.time() - _ACTIVE_CACHE['ts'] < 600:
        return _ACTIVE_CACHE['n']
    try:
        with _engine().begin() as conn:
            n = conn.execute(text("SELECT COUNT(*) FROM strava_users WHERE last_login_date > NOW() - make_interval(days => :d)"),
                             {"d": ACTIVE_WINDOW_DAYS}).scalar()
        _ACTIVE_CACHE.update(ts=time.time(), n=int(n or 0))
    except Exception as e:
        print(f"Compteur d'utilisateurs actifs : {e}")
        _ACTIVE_CACHE.update(ts=time.time(), n=None)
    return _ACTIVE_CACHE['n']

# Tables contenant des données d'un utilisateur (ordre : dépendances d'abord)
USER_TABLES = (('city_scores', 'athlete_id'), ('activities', 'athlete_id'), ('api_cache', 'athlete_id'),
               ('profiles', 'id'), ('strava_users', 'athlete_id'))

def _delete_user_rows(athlete_id):
    """Supprime l'utilisateur de chaque table, chaque suppression dans sa propre transaction.
    Renvoie False si une table existante n'a pas pu être vidée."""
    ok = True
    for table, col in USER_TABLES:
        try:
            with _engine().begin() as conn:
                conn.execute(text(f"DELETE FROM {table} WHERE {col} = :id"), {"id": athlete_id})
        except Exception as e:
            if 'does not exist' not in str(e):
                print(f"Suppression {table} : {e}")
                ok = False
    return ok

def _purge_user(token, athlete_id):
    """Efface toutes les données d'un utilisateur (caches + base). Strava API Policy §2.5 / §7.4."""
    for key in (str(athlete_id) if athlete_id else None, token):
        if key:
            RAW_DATA_CACHE.pop(key, None)
            API_RESULT_CACHE.pop(key, None)
    if not DB_URL or not (athlete_id or token):
        return True
    try:
        if not athlete_id and token:
            with _engine().begin() as conn:
                row = conn.execute(text("SELECT athlete_id FROM strava_users WHERE access_token = :t"), {"t": token}).fetchone()
            athlete_id = row[0] if row else None
        return _delete_user_rows(athlete_id) if athlete_id else True
    except Exception as e:
        print(f"Erreur purge utilisateur : {e}")
        return False

@app.route('/')
def index():
    if _require_login(): return redirect(url_for('login_page'))
    return render_template('index.html', active='map')

@app.route('/login')
def login_page():
    if 'access_token' in session: return redirect('/')
    n = active_users_count()
    return render_template('login.html', error=request.args.get('error'), bye=request.args.get('bye'),
                           active_users=n if n is not None and n >= MIN_ACTIVE_DISPLAY else None,
                           active_days=ACTIVE_WINDOW_DAYS)

@app.route('/auth')
def auth():
    # Accord explicite des CGU et de la politique de confidentialité avant toute connexion
    if request.args.get('cgu') != '1':
        return redirect(url_for('login_page', error='cgu'))
    session['cgu'] = CGU_VERSION
    state = secrets.token_urlsafe(16)
    session['oauth_state'] = state
    params = {
        'client_id': CLIENT_ID, 'response_type': 'code', 'redirect_uri': REDIRECT_URI,
        'approval_prompt': 'auto', 'scope': 'activity:read_all', 'state': state,
    }
    return redirect("https://www.strava.com/oauth/authorize?" + urlencode(params))

@app.route('/conditions/accepter', methods=['POST'])
def accepter_cgu():
    """Accord des CGU par un utilisateur déjà connecté (inscrit avant la case à cocher, ou CGU modifiées)."""
    if 'access_token' not in session: return redirect(url_for('login_page'))
    if not session.get('csrf') or request.form.get('csrf') != session.get('csrf') or request.form.get('cgu') != '1':
        abort(400)
    session['cgu'] = CGU_VERSION
    _save_cgu(session.get('athlete_id'))
    nxt = request.form.get('next') or '/'
    return redirect(nxt if nxt.startswith('/') and not nxt.startswith('//') else '/')

@app.route('/logout')
def logout():
    # Se déconnecter révoque l'accès Strava : on efface donc aussi les données (Policy §7.4).
    token = session.get('access_token')
    _deauthorize(token)
    _purge_user(token, session.get('athlete_id'))
    if token: log_event('logout')
    session.clear()
    return redirect(url_for('login_page', bye=1))

@app.route('/delete-account', methods=['POST'])
def delete_account():
    if request.form.get('csrf') != session.get('csrf'):
        abort(400)
    token = session.get('access_token')
    _deauthorize(token)
    ok = _purge_user(token, session.get('athlete_id'))
    log_event('delete')
    session.clear()
    # Confirmation écrite de la suppression (Policy §2.5)
    return render_template('deleted.html', ok=ok, deleted_at=datetime.now().strftime('%d/%m/%Y à %H:%M'))

@app.route('/callback')
def callback():
    if request.args.get('error'):
        return redirect(url_for('login_page', error='denied'))
    expected_state = session.pop('oauth_state', None)
    if expected_state and request.args.get('state') != expected_state:
        return redirect(url_for('login_page', error='state'))
    granted = request.args.get('scope', '')
    if granted and 'activity:read' not in granted:
        return redirect(url_for('login_page', error='scope'))

    code = request.args.get('code')
    try:
        res = requests.post(
            "https://www.strava.com/oauth/token",
            data={
                'client_id': CLIENT_ID,
                'client_secret': CLIENT_SECRET,
                'code': code,
                'grant_type': 'authorization_code'
            },
            timeout=10
        )
    except Exception:
        return redirect(url_for('login_page', error='network'))
    if res.status_code == 200:
        data = res.json()
        token = data.get('access_token')
        athlete_info = data.get('athlete', {})
        athlete_id = athlete_info.get('id')
        # Le nom n'est conservé que si le classement est actif (minimisation des données).
        athlete_name = f"{athlete_info.get('firstname', '')} {athlete_info.get('lastname', '')}".strip() if ENABLE_LEADERBOARD else ''

        session.permanent = True          # reste connecté 30 jours, même après fermeture du navigateur
        session['access_token'] = token
        session['athlete_id'] = athlete_id
        session['refresh_token'] = data.get('refresh_token')
        session['expires_at'] = data.get('expires_at', 0)

        if athlete_id and token and DB_URL:
            try:
                engine = _engine()
                with engine.connect() as conn:
                    prev = conn.execute(text("SELECT last_login_date FROM strava_users WHERE athlete_id = :a"),
                                        {"a": athlete_id}).fetchone()
                    # Le nom n'est écrit que si le classement est actif (la colonne peut avoir été supprimée)
                    name_col = ", athlete_name" if ENABLE_LEADERBOARD else ""
                    name_val = ", :ath_name" if ENABLE_LEADERBOARD else ""
                    name_upd = ", athlete_name = EXCLUDED.athlete_name" if ENABLE_LEADERBOARD else ""
                    query = text(f"""
                        INSERT INTO strava_users (athlete_id, access_token, login_count, first_login_date, last_login_date{name_col})
                        VALUES (:ath_id, :token, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP{name_val})
                        ON CONFLICT (athlete_id) DO UPDATE SET
                            access_token = EXCLUDED.access_token,
                            login_count = strava_users.login_count + 1,
                            last_login_date = CURRENT_TIMESTAMP{name_upd};
                    """)
                    conn.execute(query, {"ath_id": athlete_id, "token": token, "ath_name": athlete_name})
                    conn.commit()
                log_event('login' if prev else 'signup')
                _touch_visit(prev[0] if prev else None)
            except Exception as e:
                print(f"Erreur d'enregistrement utilisateur: {e}")
            _save_name(athlete_id, athlete_info)
            if session.get('cgu') == CGU_VERSION: _save_cgu(athlete_id)
            session['name_at'] = time.time()
            try:
                with _engine().begin() as conn:
                    conn.execute(text("UPDATE strava_users SET refresh_token = :r WHERE athlete_id = :a"),
                                 {"r": data.get('refresh_token'), "a": athlete_id})
            except Exception as e:
                print(f"Enregistrement du refresh token (colonne manquante ?) : {e}")

        return redirect('/')
    return redirect(url_for('login_page', error='token'))

@app.route('/stats')
def stats_page():
    if _require_login(): return redirect(url_for('login_page'))
    return render_template('stats.html', active='stats')

@app.route('/story')
def story_page():
    if _require_login(): return redirect(url_for('login_page'))
    return render_template('story.html', active='story')

@app.route('/lieux')
def lieux_page():
    if _require_login(): return redirect(url_for('login_page'))
    return render_template('lieux.html', active='lieux', categories=lieux.CATEGORIES)

@app.route('/timelapse')
def timelapse_page():
    if _require_login(): return redirect(url_for('login_page'))
    return render_template('timelapse.html', active='timelapse')

# ---------- Pages légales ----------
@app.route('/soutenir/go')
def support_go():
    """Bouton « Faire un don » : compte le clic (anonyme) puis redirige vers Ko-fi.
    Le paiement se fait entièrement sur Ko-fi : Unlock ne voit aucune donnée de paiement."""
    if not KOFI_URL: return redirect('/')
    log_event('kofi_click')
    return redirect(KOFI_URL)

@app.route('/cgu')
def cgu_page():
    return render_template('legal/cgu.html', legal='cgu')

@app.route('/confidentialite')
def privacy_page():
    return render_template('legal/confidentialite.html', legal='privacy')

@app.route('/mentions-legales')
def mentions_page():
    return render_template('legal/mentions.html', legal='mentions')

# ---------- PWA ----------
@app.route('/manifest.webmanifest')
def manifest():
    resp = send_from_directory(STATIC_DIR, 'manifest.webmanifest', mimetype='application/manifest+json')
    resp.headers['Cache-Control'] = 'no-cache'
    return resp

@app.route('/sw.js')
def service_worker():
    resp = send_from_directory(STATIC_DIR, 'sw.js', mimetype='application/javascript')
    resp.headers['Cache-Control'] = 'no-cache'
    resp.headers['Service-Worker-Allowed'] = '/'
    return resp

@app.route('/offline')
def offline_page():
    return render_template('offline.html')

@app.route('/favicon.ico')
def favicon():
    return send_from_directory(os.path.join(STATIC_DIR, 'icons'), 'favicon-32.png', mimetype='image/png')

# ============================================================
# 9. API HISTORIQUE
# ============================================================
@app.route('/api/stats_history')
def get_stats_history():
    token = session.get('access_token')
    if not token: return jsonify({"error": "Login required"}), 401

    grid_meters = _grid_size()
    sel_year = request.args.get('year', 'all')
    sel_sport = request.args.get('sport_type', 'all')

    activities = get_strava_activities_cached()
    cache_key = f"stats_{grid_meters}_{sel_year}_{sel_sport}_{_data_version(activities)}"
    cache = _result_cache(_user_key())
    if cache_key in cache: return jsonify(cache[cache_key])
    activities = list(activities)
    grid_size_deg = grid_meters / 111320
    activities.sort(key=lambda x: x['start_date_local'])

    monthly_data = {}
    global_seen = set()
    habit_times = []          # (date/heure locale, distance) des sorties filtrées
    available_years = set()
    available_sports = set()
    total_blocks = 0

    for act in activities:
        dt = datetime.strptime(act['start_date_local'], "%Y-%m-%dT%H:%M:%SZ")
        y_str = str(dt.year)
        m_key = dt.strftime("%Y-%m")
        sport = act['type']

        available_years.add(y_str)
        available_sports.add(sport)

        if sel_year != 'all' and y_str != sel_year: continue
        if sel_sport != 'all' and sport != sel_sport: continue

        if m_key not in monthly_data: monthly_data[m_key] = {'new': 0, 'routine': 0}
        habit_times.append((dt, act.get('distance', 0) or 0))

        pts = polyline.decode(act['polyline'])
        blocks = get_cells_from_polyline(pts, grid_size_deg)

        for b in blocks:
            if b not in global_seen:
                global_seen.add(b)
                monthly_data[m_key]['new'] += 1
                total_blocks += 1
            else:
                monthly_data[m_key]['routine'] += 1

    labels = sorted(monthly_data.keys())
    conquest, explore, routine = [], [], []
    running = 0

    for m in labels:
        running += monthly_data[m]['new']
        conquest.append(running)
        explore.append(monthly_data[m]['new'])
        routine.append(monthly_data[m]['routine'])

    result = {
        "labels": labels, "conquest": conquest, "exploration": explore, "routine": routine,
        "total_blocks": total_blocks,
        "available_years": sorted(list(available_years), reverse=True),
        "available_sports": sorted(list(available_sports)),
        "habits": compute_habits(habit_times, sel_year),
    }

    cache[cache_key] = result
    return jsonify(result)

# Créneaux horaires (heure locale de départ de la sortie)
HABIT_SLOTS = [
    ('Avant 8 h', 5, 8), ('8 h – 12 h', 8, 12), ('12 h – 14 h', 12, 14),
    ('14 h – 18 h', 14, 18), ('18 h – 21 h', 18, 21), ('Après 21 h', 21, 29),
]
WEEKDAYS_FR = ['Lundi', 'Mardi', 'Mercredi', 'Jeudi', 'Vendredi', 'Samedi', 'Dimanche']

def compute_habits(times, sel_year):
    """Habitudes de l'utilisateur (ses propres sorties) : créneau, jour, sorties par semaine."""
    if not times:
        return None
    slots = [0] * len(HABIT_SLOTS)
    days = [0] * 7
    for dt, _ in times:
        h = dt.hour if dt.hour >= 5 else dt.hour + 24          # 0 h – 5 h compte comme « après 21 h »
        for i, (_, a, b) in enumerate(HABIT_SLOTS):
            if a <= h < b:
                slots[i] += 1; break
        days[dt.weekday()] += 1
    # Moyennes calculées sur des SEMAINES COMPLÈTES (lundi → dimanche) : la semaine en cours,
    # pas encore finie, ferait baisser artificiellement la moyenne.
    today = datetime.now()
    this_monday = datetime(today.year, today.month, today.day) - timedelta(days=today.weekday())
    first = min(dt for dt, _ in times)
    if sel_year != 'all':
        y = int(sel_year)
        start = datetime(y, 1, 1)
        end = min(this_monday, datetime(y + 1, 1, 1))
    else:
        start = datetime(first.year, first.month, first.day) - timedelta(days=first.weekday())
        end = this_monday
    done = [(dt, d) for dt, d in times if start <= dt < end]
    weeks = (end - start).days / 7
    if weeks < 1 or not done:            # moins d'une semaine complète : on garde tout, sur 1 semaine
        done, weeks = list(times), 1.0
    is_current = sel_year == 'all' or int(sel_year) == today.year
    recent = [dt for dt, _ in times if this_monday - timedelta(days=28) <= dt < this_monday]
    total_km = sum(d for _, d in done) / 1000
    return {
        "count": len(times),
        "slots": [{"label": l, "count": c} for (l, _, _), c in zip(HABIT_SLOTS, slots)],
        "weekdays": [{"label": l, "count": c} for l, c in zip(WEEKDAYS_FR, days)],
        "per_week": round(len(done) / weeks, 2),
        "per_week_recent": round(len(recent) / 4, 2) if is_current else None,
        "km_per_week": round(total_km / weeks, 1),
        "full_weeks": int(round(weeks)),
    }

# ============================================================
# DÉPARTEMENTS (mode « Communes » de la carte)
# ============================================================
DEPARTEMENTS = {
    '01': 'Ain', '02': 'Aisne', '03': 'Allier', '04': 'Alpes-de-Haute-Provence', '05': 'Hautes-Alpes', '06': 'Alpes-Maritimes',
    '07': 'Ardèche', '08': 'Ardennes', '09': 'Ariège', '10': 'Aube', '11': 'Aude', '12': 'Aveyron', '13': 'Bouches-du-Rhône',
    '14': 'Calvados', '15': 'Cantal', '16': 'Charente', '17': 'Charente-Maritime', '18': 'Cher', '19': 'Corrèze',
    '2A': 'Corse-du-Sud', '2B': 'Haute-Corse', '21': "Côte-d'Or", '22': "Côtes-d'Armor", '23': 'Creuse', '24': 'Dordogne',
    '25': 'Doubs', '26': 'Drôme', '27': 'Eure', '28': 'Eure-et-Loir', '29': 'Finistère', '30': 'Gard', '31': 'Haute-Garonne',
    '32': 'Gers', '33': 'Gironde', '34': 'Hérault', '35': 'Ille-et-Vilaine', '36': 'Indre', '37': 'Indre-et-Loire', '38': 'Isère',
    '39': 'Jura', '40': 'Landes', '41': 'Loir-et-Cher', '42': 'Loire', '43': 'Haute-Loire', '44': 'Loire-Atlantique', '45': 'Loiret',
    '46': 'Lot', '47': 'Lot-et-Garonne', '48': 'Lozère', '49': 'Maine-et-Loire', '50': 'Manche', '51': 'Marne', '52': 'Haute-Marne',
    '53': 'Mayenne', '54': 'Meurthe-et-Moselle', '55': 'Meuse', '56': 'Morbihan', '57': 'Moselle', '58': 'Nièvre', '59': 'Nord',
    '60': 'Oise', '61': 'Orne', '62': 'Pas-de-Calais', '63': 'Puy-de-Dôme', '64': 'Pyrénées-Atlantiques', '65': 'Hautes-Pyrénées',
    '66': 'Pyrénées-Orientales', '67': 'Bas-Rhin', '68': 'Haut-Rhin', '69': 'Rhône', '70': 'Haute-Saône', '71': 'Saône-et-Loire',
    '72': 'Sarthe', '73': 'Savoie', '74': 'Haute-Savoie', '75': 'Paris', '76': 'Seine-Maritime', '77': 'Seine-et-Marne',
    '78': 'Yvelines', '79': 'Deux-Sèvres', '80': 'Somme', '81': 'Tarn', '82': 'Tarn-et-Garonne', '83': 'Var', '84': 'Vaucluse',
    '85': 'Vendée', '86': 'Vienne', '87': 'Haute-Vienne', '88': 'Vosges', '89': 'Yonne', '90': 'Territoire de Belfort',
    '91': 'Essonne', '92': 'Hauts-de-Seine', '93': 'Seine-Saint-Denis', '94': 'Val-de-Marne', '95': "Val-d'Oise",
    '971': 'Guadeloupe', '972': 'Martinique', '973': 'Guyane', '974': 'La Réunion', '976': 'Mayotte',
}
DEP_MIN_COMMUNES = 5          # minimum de communes visitées pour être classé au % (évite « Paris = 100 % »)
_DEP_TOTALS = {}              # nombre de communes par département (donnée publique, gardée en mémoire)
_DEP_GEO = {}                 # contours simplifiés par département (donnée publique)

def dep_of(code):
    if not code: return None
    code = str(code)
    return code[:3] if code.startswith('97') else code[:2]

def dep_totals():
    if not _DEP_TOTALS and DB_URL:
        try:
            with _engine().connect() as conn:
                rows = conn.execute(text("""
                    SELECT CASE WHEN code_insee LIKE '97%' THEN left(code_insee, 3) ELSE left(code_insee, 2) END AS dep, count(*) AS n
                    FROM communes WHERE code_insee IS NOT NULL GROUP BY 1"""))
                _DEP_TOTALS.update({r.dep: int(r.n) for r in rows})
        except Exception as e:
            print(f"Erreur comptage départements: {e}")
    return _DEP_TOTALS

def departements_summary(cities):
    """Vos départements : communes visitées / total. Classement : meilleur % parmi ceux où au moins
    DEP_MIN_COMMUNES communes sont visitées, puis les autres par nombre de communes visitées."""
    totals = dep_totals()
    by_dep = defaultdict(list)
    for c in cities:
        if c.get('dep'): by_dep[c['dep']].append(c)
    out = []
    for dep, cs in by_dep.items():
        total = max(totals.get(dep, 0), len(cs))
        out.append({"code": dep, "name": DEPARTEMENTS.get(dep, dep), "visited": len(cs), "total": total,
                    "percent": round(len(cs) / total * 100, 1),
                    "at100": sum(1 for c in cs if c['stats']['percent'] >= 100),
                    "over50": sum(1 for c in cs if c['stats']['percent'] >= 50)})
    out.sort(key=lambda d: (d['visited'] >= DEP_MIN_COMMUNES, d['percent'] if d['visited'] >= DEP_MIN_COMMUNES else d['visited'], d['visited']), reverse=True)
    return out

def _rings_latlon(geojson):
    """GeoJSON (lon, lat) -> liste de polygones [[anneau extérieur, trous...], ...] en (lat, lon) pour Leaflet."""
    g = json.loads(geojson) if isinstance(geojson, str) else geojson
    polys = g['coordinates'] if g['type'] == 'MultiPolygon' else [g['coordinates']] if g['type'] == 'Polygon' else []
    return [[[[round(y, 5), round(x, 5)] for x, y in ring] for ring in poly] for poly in polys]

@app.route('/api/departement/<dep>/geo')
def departement_geo(dep):
    """Contours simplifiés de toutes les communes d'un département. Donnée publique (geo.api.gouv.fr) :
    aucune donnée Strava, donc mise en cache longue durée par le CDN de Vercel."""
    dep = dep.upper()
    if dep not in DEPARTEMENTS: return jsonify({"error": "Département inconnu"}), 404
    if dep not in _DEP_GEO:
        if not DB_URL: return jsonify({"error": "Base indisponible"}), 503
        try:
            with _engine().connect() as conn:
                rows = conn.execute(text("""
                    SELECT code_insee, nom_commune, ST_AsGeoJSON(ST_SimplifyPreserveTopology(geometry, 0.0004), 5) AS g
                    FROM communes WHERE code_insee LIKE :p"""), {"p": dep + '%'}).fetchall()
                outline = conn.execute(text("""
                    SELECT ST_AsGeoJSON(ST_SimplifyPreserveTopology(ST_Union(geometry), 0.0008), 5) AS g
                    FROM communes WHERE code_insee LIKE :p"""), {"p": dep + '%'}).scalar()
        except Exception as e:
            print(f"Erreur contours département {dep}: {e}")
            return jsonify({"error": "Contours indisponibles"}), 500
        _DEP_GEO[dep] = {"code": dep, "name": DEPARTEMENTS[dep],
                         "communes": [{"code": r.code_insee, "name": r.nom_commune, "shape": _rings_latlon(r.g)} for r in rows if r.g],
                         "outline": _rings_latlon(outline) if outline else []}
    resp = make_response(jsonify(_DEP_GEO[dep]))
    resp.headers['Cache-Control'] = 'public, max-age=86400, s-maxage=604800, stale-while-revalidate=86400'
    return resp

_INACC_CACHE = {}

@app.route('/api/commune/<code>/inaccessibles')
def commune_inaccessibles(code):
    """Blocs d'une commune sans aucun chemin à moins de 20 m (centres des blocs). Donnée publique
    calculée depuis OpenStreetMap, sans donnée Strava : mise en cache longue par le CDN."""
    grid = request.args.get('grid', type=int)
    profil = request.args.get('profil', 'tous')
    if grid not in ALLOWED_GRID_SIZES or profil not in ('pied', 'velo', 'tous') or not re.fullmatch(r'[0-9AB]{5}', code or ''):
        return jsonify({"error": "Paramètres invalides"}), 400
    key = (code, grid, profil)
    if key not in _INACC_CACHE:
        dlat = grid / 111320
        cells = []
        if acces.inaccessibles(dep_of(code), grid) and DB_URL:
            try:
                with _engine().connect() as conn:
                    g = conn.execute(text("SELECT ST_AsGeoJSON(geometry) FROM communes WHERE code_insee = :c LIMIT 1"),
                                     {"c": code}).scalar()
                if g:
                    geom = shp_transform(lambda x, y, z=None: (y, x), shp_shape(json.loads(g))).buffer(0)
                    keys = acces.filtre_inaccessibles(acces.keys_of_cells(commune_cells(geom, dlat), dlat),
                                                      dep_of(code), grid, profil)
                    cells = [[a, b] for a, b in acces.cells_of_keys(keys, dlat)]
            except Exception as e:
                print(f"Erreur blocs inaccessibles {code}: {e}")
                return jsonify({"error": "Indisponible"}), 500
        if len(_INACC_CACHE) > 2000: _INACC_CACHE.clear()
        _INACC_CACHE[key] = {"code": code, "grid": grid, "profil": profil, "cells": cells,
                             "source": "© contributeurs OpenStreetMap"}
    resp = make_response(jsonify(_INACC_CACHE[key]))
    resp.headers['Cache-Control'] = 'public, max-age=86400, s-maxage=604800, stale-while-revalidate=86400'
    return resp

# ============================================================
# 10. API ACTIVITÉS / CARTE / COMMUNES
# ============================================================
@app.route('/api/activities')
def get_activities_route():
    token = session.get('access_token')
    if not token: return jsonify({"error": "Login required"}), 401

    athlete_id = session.get('athlete_id')

    if DB_URL:
        try:
            engine = _engine()
            with engine.connect() as conn:
                res = conn.execute(
                    text("""
                        WITH prev AS (SELECT athlete_id, last_login_date AS old FROM strava_users
                                      WHERE athlete_id = :aid OR access_token = :token LIMIT 1)
                        UPDATE strava_users u SET last_login_date = CURRENT_TIMESTAMP
                        FROM prev WHERE u.athlete_id = prev.athlete_id
                        RETURNING u.athlete_id, prev.old
                    """), {"token": token, "aid": athlete_id or -1}
                ).fetchone()
                if res and not athlete_id: athlete_id = res[0]
                conn.commit()
            if res: _touch_visit(res[1])
        except Exception as e:
            print(f"Erreur màj silencieuse: {e}")

    _refresh_name(token)
    sel_year = request.args.get('year', 'all')
    sel_sport = request.args.get('sport_type', 'all')
    return jsonify(_activities_data(sel_year, sel_sport, _grid_size()))

@app.route('/api/communes')
def get_communes_route():
    """Tableau « Mes communes » (Stats) : mêmes calculs que la carte, sans les contours."""
    if not session.get('access_token'): return jsonify({"error": "Login required"}), 401
    data = _activities_data(request.args.get('year', 'all'), request.args.get('sport_type', 'all'), _grid_size())
    rows = [{"name": c["name"], "code": c.get("code"), "dep": c.get("dep"), **c["stats"]} for c in data.get("top_municipalities", [])]
    return jsonify({"communes": rows, "departements": data.get("departements", []), "db": bool(DB_URL)})

_COMMUNE_GEOM = {}   # code INSEE -> géométrie (lat, lon). Donnée publique, gardée en mémoire

def _commune_geom(code):
    if code not in _COMMUNE_GEOM:
        with _engine().connect() as conn:
            row = conn.execute(text("SELECT nom_commune, ST_AsGeoJSON(geometry) AS g FROM communes WHERE code_insee = :c LIMIT 1"),
                               {"c": code}).fetchone()
        if not row or not row.g: return None, None
        if len(_COMMUNE_GEOM) > 500: _COMMUNE_GEOM.clear()
        _COMMUNE_GEOM[code] = (row.nom_commune, shp_transform(lambda x, y, z=None: (y, x), shp_shape(json.loads(row.g))).buffer(0))
    return _COMMUNE_GEOM[code]

@app.route('/api/commune_history')
def commune_history():
    """% exploré d'UNE commune, mois après mois (graphique de la page Stats). Même calcul que la carte :
    blocs de la commune traversés / blocs accessibles, avec les mêmes filtres."""
    if not session.get('access_token'): return jsonify({"error": "Login required"}), 401
    code = request.args.get('code', '')
    if not re.fullmatch(r'[0-9AB]{5}', code): return jsonify({"error": "Commune invalide"}), 400
    if not DB_URL: return jsonify({"error": "Base indisponible"}), 503
    grid_meters = _grid_size()
    sel_year, sel_sport = request.args.get('year', 'all'), request.args.get('sport_type', 'all')
    activities = get_strava_activities_cached()
    cache_key = f"ch_{code}_{grid_meters}_{sel_year}_{sel_sport}_{_data_version(activities)}"
    cache = _result_cache(_user_key())
    if cache_key in cache: return jsonify(cache[cache_key])
    try:
        name, geom = _commune_geom(code)
    except Exception as e:
        print(f"Erreur contour commune {code}: {e}")
        return jsonify({"error": "Indisponible"}), 500
    if geom is None: return jsonify({"error": "Commune inconnue"}), 404

    dlat = grid_meters / 111320
    acts = [a for a in activities
            if (sel_year == 'all' or a['start_date_local'][:4] == sel_year) and (sel_sport == 'all' or a['type'] == sel_sport)]
    # Profil d'accès identique à la carte : celui de toutes les sorties affichées
    profils = {acces.profil_of(a['type']) for a in acts}
    profil = profils.pop() if len(profils) == 1 else 'tous'
    all_cells = commune_cells(geom, dlat)
    hors = set(acces.filtre_inaccessibles(acces.keys_of_cells(all_cells, dlat), dep_of(code), grid_meters, profil).tolist()) if all_cells else set()
    a0, b0, a1, b1 = geom.bounds
    m = 2 * dlat   # marge : un bloc touché peut déborder un peu de l'emprise
    par_mois = defaultdict(set)
    for a in sorted(acts, key=lambda a: a['start_date_local']):
        pts = polyline.decode(a['polyline'])
        if len(pts) < 2: continue
        arr = np.asarray(pts)
        if arr[:, 0].max() < a0 - m or arr[:, 0].min() > a1 + m or arr[:, 1].max() < b0 - m * 2 or arr[:, 1].min() > b1 + m * 2:
            continue
        dedans = get_cells_from_polyline(pts, dlat) & all_cells
        if dedans: par_mois[a['start_date_local'][:7]] |= dedans
    if not par_mois:
        res = {"code": code, "name": name, "labels": [], "percent": [], "blocks": [], "new": [], "total_blocks": 0}
        cache[cache_key] = res
        return jsonify(res)

    # Mois continus, de la 1re visite jusqu'à aujourd'hui (ou fin de l'année choisie)
    now = datetime.now()
    fin = f"{now:%Y-%m}" if sel_year == 'all' or int(sel_year) >= now.year else f"{sel_year}-12"
    mois, k = [], min(par_mois)
    while k <= fin:
        mois.append(k)
        y, mo = int(k[:4]), int(k[5:]) + 1
        k = f"{y + (mo > 12)}-{(mo - 1) % 12 + 1:02d}"
    vus, n_hors_vus = set(), 0
    pct, blocs, nouveaux, totaux = [], [], [], []
    for k in mois:
        neufs = par_mois.get(k, set()) - vus
        if neufs:
            cles = acces.keys_of_cells(neufs, dlat).tolist()
            n_hors_vus += sum(1 for c in cles if c in hors)
            vus |= neufs
        total = max(len(vus), len(all_cells) - (len(hors) - n_hors_vus))
        pct.append(round(min(100.0, len(vus) / max(1, total) * 100), 2))
        blocs.append(len(vus)); nouveaux.append(len(neufs)); totaux.append(total)
    res = {"code": code, "name": name, "labels": mois, "percent": pct, "blocks": blocs, "new": nouveaux,
           "totals": totaux, "total_blocks": totaux[-1]}
    cache[cache_key] = res
    return jsonify(res)

def _lieux_base():
    """Sorties décodées de l'utilisateur et départements où il a des communes explorées (cache 10 min)."""
    activities = get_strava_activities_cached()
    cache = _result_cache(_user_key())
    key = f"lieux_{_data_version(activities)}"
    if key not in cache:
        deps = [d['code'] for d in _activities_data('all', 'all', 250).get('departements', [])]
        traces = []
        for a in sorted(activities, key=lambda a: a['start_date_local']):
            pts = polyline.decode(a['polyline'])
            if pts: traces.append((a['start_date_local'][:10], pts))
        cache[key] = {"deps": deps, "traces": traces, "pris": {}}
    return cache[key]

def _lieux_pris(base, dep):
    """{indice du lieu: date de 1re visite} pour un département (calculé une fois, puis gardé 10 min)."""
    if dep not in base['pris']:
        base['pris'][dep] = lieux.collecter(base['traces'], [dep]).get(dep, {}) if lieux.departement(dep) else {}
    return base['pris'][dep]

@app.route('/api/lieux')
def api_lieux():
    """Lieux d'un département (n'importe lequel en France), avec ceux que l'utilisateur a débloqués.
    Toutes ses sorties comptent (sans filtre d'année ni de sport : c'est une collection).
    ?commune=<code INSEE> : centre la vue sur cette commune (son département est chargé)."""
    if not session.get('access_token'): return jsonify({"error": "Login required"}), 401
    base = _lieux_base()
    resume = []
    for dep in base['deps']:
        d = lieux.departement(dep)
        if d: resume.append({"code": dep, "name": DEPARTEMENTS.get(dep, dep), "total": len(d['lieux']), "collected": len(_lieux_pris(base, dep))})
    resume.sort(key=lambda r: (r['collected'], r['total']), reverse=True)

    com = lieux.commune(request.args.get('commune', ''))
    dep = com[2] if com else request.args.get('dep')
    absent = None
    if dep not in DEPARTEMENTS or not lieux.departement(dep):
        if dep in DEPARTEMENTS: absent = {"code": dep, "name": DEPARTEMENTS[dep]}   # pas encore calculé
        dep, com = (resume[0]['code'] if resume else None), None
    out = {"departements": resume,
           "sans_donnees": [{"code": c, "name": DEPARTEMENTS.get(c, c)} for c in base['deps'] if not lieux.departement(c)],
           "categories": {k: {"label": v[0], "short": v[1], "icon": k} for k, v in lieux.CATEGORIES.items()},
           "ordre": list(lieux.CATEGORIES),    # ordre d'affichage (les clés d'un objet JSON sont triées)
           "rayon_m": lieux.RAYON_M, "source": "© contributeurs OpenStreetMap (ODbL)", "dep": None, "commune": None}
    out["absent"] = absent
    # Tous les départements déjà calculés, pour le menu (y compris ceux sans sortie)
    dispo = lieux.disponibles()
    out["tous"] = [{"code": c, "name": DEPARTEMENTS[c], "total": dispo[c]} for c in DEPARTEMENTS if c in dispo]
    vus = set(base['deps']) | ({dep} if dep else set())
    out["france"] = {"total": lieux.total_france(), "collected": sum(len(_lieux_pris(base, x)) for x in vus)}
    if dep:
        d, pris = lieux.departement(dep), _lieux_pris(base, dep)
        out["dep"] = {"code": dep, "name": DEPARTEMENTS.get(dep, dep), "communes": d['communes'],
                      "lieux": [[r[0], r[1], r[2], r[4], r[5], r[6], r[7], r[8], pris.get(i)] for i, r in enumerate(d['lieux'])]}
        if com: out["commune"] = {"code": com[0], "name": com[1], "bbox": com[3]}
    return jsonify(out)

@app.route('/api/lieux/communes')
def api_lieux_communes():
    """Recherche de commune sur toute la France (donnée publique OpenStreetMap, sans donnée Strava)."""
    if not session.get('access_token'): return jsonify({"error": "Login required"}), 401
    res = [{"code": c, "name": n, "dep": d, "dep_name": DEPARTEMENTS.get(d, d), "lieux": nb}
           for c, n, d, _, nb, _n in lieux.chercher(request.args.get('q', ''))]
    return jsonify({"communes": res})

@app.route('/api/commune/<code>/contour')
def commune_contour(code):
    """Contour simplifié d'une commune (donnée publique) : mis en cache longue durée par le CDN."""
    if not re.fullmatch(r'[0-9AB]{5}', code or ''): return jsonify({"error": "Paramètres invalides"}), 400
    if not DB_URL: return jsonify({"code": code, "shape": []})
    try:
        with _engine().connect() as conn:
            g = conn.execute(text("""SELECT ST_AsGeoJSON(ST_SimplifyPreserveTopology(geometry, 0.0002), 5)
                                     FROM communes WHERE code_insee = :c LIMIT 1"""), {"c": code}).scalar()
    except Exception as e:
        print(f"Contour {code}: {e}")
        return jsonify({"error": "Indisponible"}), 500
    resp = make_response(jsonify({"code": code, "shape": _rings_latlon(g) if g else []}))
    resp.headers['Cache-Control'] = 'public, max-age=86400, s-maxage=604800, stale-while-revalidate=86400'
    return resp

def _activities_data(sel_year, sel_sport, grid_meters):
    activities = get_strava_activities_cached()
    cache_key = f"act_{grid_meters}_{sel_year}_{sel_sport}_{_data_version(activities)}"
    cache = _result_cache(_user_key())
    if cache_key in cache: return cache[cache_key]
    grid_size_deg = grid_meters / 111320

    data = {
        "coords": [], "grid_cells": [], "grid_size_used": grid_size_deg,
        "available_years": set(), "available_sports": {}, "top_municipalities": [], "departements": [],
        "stats": { "total_distance": 0, "activity_count": 0, "cells_conquered": 0 }
    }

    # Dictionnaire plutôt que liste
    # pour garder un index parfait avec grid_store_db
    activity_cache = {} 
    grid_store = {}
    grid_store_db = defaultdict(set)
    filtered_cell_acts = defaultdict(set)   # blocs des activités filtrées -> activités
    filtered_sports = set()
    seg_lat, seg_lon, seg_len, seg_act = [], [], [], []   # milieux des segments des traces filtrées
    
    current_year, current_week, _ = datetime.now().isocalendar()
    current_period = f"{current_year}-W{current_week:02d}"

    for idx, act in enumerate(activities):
        dt = datetime.strptime(act['start_date_local'], "%Y-%m-%dT%H:%M:%SZ")
        y_str = str(dt.year)
        sport = act['type']

        data["available_years"].add(y_str)
        if sport not in data["available_sports"]:
            data["available_sports"][sport] = SPORT_TRANSLATIONS.get(sport, sport)

        pts = polyline.decode(act['polyline'])
        if len(pts) < 2: continue

        blocks = get_cells_from_polyline(pts, grid_size_deg)
        act_ym = dt.strftime("%Y-%m")

        activity_cache[idx] = {
            "act": act, "dt": dt, "year": y_str, "sport": sport,
            "pts": pts, "blocks": blocks,
            "period": f"{dt.isocalendar().year}-W{dt.isocalendar().week:02d}"
        }

        # CARTE : uniquement selon les filtres
        if (sel_year == 'all' or sel_year == y_str) and (sel_sport == 'all' or sel_sport == sport):
            data["coords"].append(pts)
            for b in blocks:
                if b not in grid_store:
                    grid_store[b] = {'cnt': 0, 'first': act_ym, 'last': act_ym}
                
                grid_store[b]['cnt'] += 1
                if act_ym < grid_store[b]['first']: grid_store[b]['first'] = act_ym
                if act_ym > grid_store[b]['last']: grid_store[b]['last'] = act_ym
            
            filtered_sports.add(sport)
            data["stats"]["total_distance"] += act['distance'] / 1000
            data["stats"]["activity_count"] += 1
            for b in blocks:
                filtered_cell_acts[b].add(idx)
            arr = np.asarray(pts, dtype=float)
            mid = (arr[1:] + arr[:-1]) / 2
            d_lat = (arr[1:, 0] - arr[:-1, 0]) * M_PER_DEG
            d_lon = (arr[1:, 1] - arr[:-1, 1]) * M_PER_DEG * np.cos(np.radians(mid[:, 0]))
            seg_lat.append(mid[:, 0]); seg_lon.append(mid[:, 1])
            seg_len.append(np.hypot(d_lat, d_lon)); seg_act.append(np.full(len(mid), idx))
            
        # BASE DE DONNEES : on enregistre tous les blocks
        for b in blocks:
            grid_store_db[b].add(idx)

    data["grid_cells"] = [[k[0], k[1], v['cnt'], v['first'], v['last']] for k, v in grid_store.items()]
    profils = {acces.profil_of(sp) for sp in filtered_sports}
    data["acces_profil"] = profils.pop() if len(profils) == 1 else 'tous'
    data["stats"]["cells_conquered"] = len(grid_store)
    data["available_years"] = sorted(list(data["available_years"]), reverse=True)
    data["available_sports"] = dict(sorted(data["available_sports"].items(), key=lambda x: x[1]))

    # ========================================================
    # CALCUL DES VILLES
    # ========================================================
    if filtered_cell_acts and DB_URL:
        identified_cities = {}
        probe_weight = defaultdict(int)
        for (lat, lon), acts_in in filtered_cell_acts.items():
            probe_weight[(round(lat, 2), round(lon, 2))] += len(acts_in)
        probe_list = sorted(probe_weight, key=probe_weight.get, reverse=True)
        batch_size = 500
        swap = lambda x, y, z=None: (y, x)   # GeoJSON (lon, lat) -> (lat, lon), comme les blocs

        try:
            with _engine().connect() as conn:
                for i in range(0, len(probe_list), batch_size):
                    batch = probe_list[i:i + batch_size]
                    wkt_multipoint = "MULTIPOINT(" + ", ".join(f"{lon} {lat}" for lat, lon in batch) + ")"
                    rows = conn.execute(text("""
                        SELECT DISTINCT nom_commune, code_insee, ST_AsGeoJSON(geometry) AS outline
                        FROM communes
                        WHERE ST_Intersects(geometry, ST_GeomFromText(:wkt, 4326))
                    """), {"wkt": wkt_multipoint})
                    for row in rows:
                        # Clé = code INSEE : des dizaines de communes portent le même nom en France
                        key = row.code_insee or row.nom_commune
                        if key in identified_cities: continue
                        try:
                            geom = shp_transform(swap, shp_shape(json.loads(row.outline))).buffer(0)
                            if geom.is_empty: continue
                            # Contour affiché : la plus grande partie de la commune
                            parts = list(geom.geoms) if geom.geom_type == 'MultiPolygon' else [geom]
                            main = max(parts, key=lambda g: g.area)
                            identified_cities[key] = {
                                "name": row.nom_commune, "code": row.code_insee, "dep": dep_of(row.code_insee),
                                "geom": geom,
                                "outline": [[round(y, 6), round(x, 6)] for y, x in main.exterior.coords],
                            }
                        except Exception as e:
                            print(f"Erreur traitement commune {row.nom_commune}: {e}")
                    if len(identified_cities) >= MAX_COMMUNES: break
        except Exception as e:
            print(f"Erreur Batch DB: {e}")

        visited = set(filtered_cell_acts.keys())
        profil = data["acces_profil"]   # à pied si toutes les sorties affichées le sont, vélo idem, sinon les deux
        if seg_lat:
            S_lat, S_lon = np.concatenate(seg_lat), np.concatenate(seg_lon)
            S_len, S_act = np.concatenate(seg_len), np.concatenate(seg_act)
        final_cities_list = []
        scores_to_save = []   # classement désactivé (non conforme Strava) : rien n'est enregistré
        for city_key, city in identified_cities.items():
            city_name = city['name']
            try:
                all_cells = commune_cells(city['geom'], grid_size_deg)
                seen = all_cells & visited
                if not seen: continue
                acts = set()
                for c in seen: acts |= filtered_cell_acts[c]
                # Blocs sans aucun chemin à moins de 20 m : non comptés, sauf s'ils ont été visités
                hors = acces.filtre_inaccessibles(acces.keys_of_cells(all_cells, grid_size_deg),
                                                  city['dep'], grid_meters, profil)
                if len(hors):
                    hors = np.setdiff1d(hors, acces.keys_of_cells(seen, grid_size_deg))
                total = max(len(seen), len(all_cells) - len(hors))
                pct = round(min(100.0, len(seen) / max(1, total) * 100), 2)
                # Distance parcourue DANS la commune : segments dont le milieu est à l'intérieur
                km = 0.0
                if seg_lat:
                    g = city['geom']; a0, b0, a1, b1 = g.bounds
                    box = (S_lat >= a0) & (S_lat <= a1) & (S_lon >= b0) & (S_lon <= b1)
                    if box.any():
                        inside = shapely.contains_xy(g, S_lat[box], S_lon[box])
                        km = float(S_len[box][inside].sum()) / 1000
                days = sorted(activity_cache[i]['dt'].strftime('%Y-%m-%d') for i in acts)
                final_cities_list.append({
                    "name": city_name, "code": city['code'], "dep": city['dep'],
                    "outline": city['outline'],
                    "stats": {"blocks": len(seen), "total_blocks": total, "inaccessible": int(len(hors)), "percent": pct, "activities": len(acts),
                              "km": round(km, 1), "first": days[0] if days else None, "last": days[-1] if days else None},
                })
            except Exception as e:
                print(f"Erreur calcul stats ville {city_name}: {e}")

        # Homonymes (ex. plusieurs « Saint-Martin ») : on précise le département dans le nom affiché
        seen_names = defaultdict(int)
        for c in final_cities_list: seen_names[c['name']] += 1
        for c in final_cities_list:
            if seen_names[c['name']] > 1 and c['dep']: c['name'] = f"{c['name']} ({c['dep']})"
        data["top_municipalities"] = sorted(final_cities_list, key=lambda x: x['stats']['blocks'], reverse=True)
        data["departements"] = departements_summary(final_cities_list)

        if scores_to_save and ENABLE_LEADERBOARD:
            try:
                engine = _engine()
                with engine.connect() as conn:
                    upsert_query = text("""
                        INSERT INTO city_scores (
                            athlete_id, city_name, grid_size, sport, period, blocks_count, percent, activities_count, last_updated
                        ) VALUES (
                            :ath_id, :c_name, :g_size, :sport, :period, :b_count, :pct, :act_count, CURRENT_TIMESTAMP
                        ) ON CONFLICT (athlete_id, city_name, grid_size, sport, period) DO UPDATE SET
                            blocks_count = EXCLUDED.blocks_count,
                            percent = EXCLUDED.percent,
                            activities_count = EXCLUDED.activities_count,
                            last_updated = CURRENT_TIMESTAMP;
                    """)
                    conn.execute(upsert_query, scores_to_save)
                    conn.commit()
            except Exception as e:
                print(f"Erreur sauvegarde scores: {e}")

    cache[cache_key] = data
    return data

# ============================================================
# 11. KEEP ALIVE
# ============================================================
@app.route('/api/keep-alive')
def keep_alive():
    if not DB_URL: return jsonify({"error": "Database URL non configurée"}), 500
    try:
        engine = _engine()
        with engine.connect() as conn: conn.execute(text("SELECT 1"))
        return jsonify({"status": "Supabase is awake!"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ============================================================
# 12. NETTOYAGE UTILISATEURS
# ============================================================
@app.route('/api/cleanup-users')
def cleanup_users():
    cron_secret = os.getenv('CRON_SECRET')
    auth_header = request.headers.get('Authorization')
    if auth_header != f"Bearer {cron_secret}": return jsonify({"error": "Non autorisé"}), 401
    if not DB_URL: return jsonify({"error": "Database URL non configurée"}), 500

    try:
        revoked_count = 0
        with _engine().begin() as conn:
            inactive_users = conn.execute(text("""
                SELECT athlete_id, access_token, refresh_token FROM strava_users
                WHERE last_login_date < NOW() - make_interval(days => :d)
            """), {"d": INACTIVE_DAYS}).fetchall()
        for row in inactive_users:
            _deauthorize_stored(row.access_token, row.refresh_token)
            _delete_user_rows(row.athlete_id)
            log_event('revoke_inactive')
            revoked_count += 1

        # Cache d'activités expiré (> 47 h) : effacé (Strava API Policy §6.2 / §6.3)
        expired = 0
        with _engine().begin() as conn:
            expired = conn.execute(text("""
                DELETE FROM activities a WHERE NOT EXISTS (
                    SELECT 1 FROM strava_users u WHERE u.athlete_id = a.athlete_id
                    AND u.full_sync_at > NOW() - make_interval(hours => :h))
            """), {"h": DB_CACHE_MAX_AGE_HOURS}).rowcount
        try:   # Noms lus il y a plus de 7 jours : effacés (relus à la prochaine visite)
            with _engine().begin() as conn:
                conn.execute(text("""UPDATE strava_users SET first_name = NULL, last_name = NULL, name_at = NULL
                                     WHERE name_at < NOW() - make_interval(days => :d)"""), {"d": NAME_DAYS})
        except Exception as e:
            print(f"Purge des noms : {e}")
        try:
            with _engine().begin() as conn:
                conn.execute(text("DELETE FROM api_cache WHERE created_at < NOW() - make_interval(hours => :h)"),
                             {"h": DB_CACHE_MAX_AGE_HOURS})
        except Exception:
            pass

        _daily_snapshot()
        result = {"status": "Nettoyage terminé", "comptes_supprimes": revoked_count, "activites_expirees": expired}
        kv_set('last_cron', {**result, 'at': datetime.utcnow().isoformat() + 'Z'})
        return jsonify(result), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ============================================================
# 13. PAGE LEADERBOARD
# ============================================================
@app.route('/leaderboard')
def leaderboard_page():
    if not ENABLE_LEADERBOARD: abort(404)
    if 'access_token' not in session: return redirect(url_for('login_page'))
    return render_template('leaderboard.html', active='leaderboard')

# ============================================================
# 14. LEADERBOARD GLOBAL
# ============================================================
@app.route('/api/global_stats_leaderboard')
def get_global_stats_leaderboard():
    if not ENABLE_LEADERBOARD: return jsonify({"error": "Fonctionnalité désactivée"}), 404
    token = session.get('access_token')
    if not token: return jsonify({"error": "Login required"}), 401

    grid_size = _grid_size()
    sport_filter = request.args.get('sport', 'all')
    period_filter = request.args.get('period', 'all')
    period_display = None

    if period_filter == 'current_week':
        period_filter = get_current_iso_period()
        period_display = get_current_week_display()

    if not DB_URL: return jsonify({"error": "Base de données non configurée"}), 500

    try:
        engine = _engine()
        with engine.connect() as conn:
            query = text("""
                SELECT city_name, COUNT(DISTINCT athlete_id) AS users_count, SUM(blocks_count) AS total_blocks, SUM(activities_count) AS total_acts
                FROM city_scores
                WHERE grid_size = :grid_size AND sport = :sport AND period = :period
                GROUP BY city_name
                ORDER BY users_count DESC, total_blocks DESC, city_name ASC
            """)
            res = conn.execute(query, {"grid_size": grid_size, "sport": sport_filter, "period": period_filter}).fetchall()

            cities_data = [{
                "name": row.city_name,
                "users": int(row.users_count) if row.users_count else 0,
                "blocks": int(row.total_blocks) if row.total_blocks else 0,
                "activities": int(row.total_acts) if row.total_acts else 0
            } for row in res]

            sports_res = conn.execute(text("SELECT DISTINCT sport FROM city_scores WHERE sport != 'all' ORDER BY sport")).fetchall()
            available_sports = [r.sport for r in sports_res]

            return jsonify({"cities": cities_data, "available_sports": available_sports, "period_display": period_display}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ============================================================
# 15. LEADERBOARD D'UNE VILLE
# ============================================================
@app.route('/api/city_leaderboard')
def get_city_leaderboard():
    if not ENABLE_LEADERBOARD: return jsonify({"error": "Fonctionnalité désactivée"}), 404
    token = session.get('access_token')
    if not token: return jsonify({"error": "Login required"}), 401

    city = request.args.get('city')
    grid_size = _grid_size()
    sport_filter = request.args.get('sport', 'all')
    period_filter = request.args.get('period', 'all')
    period_display = None

    if period_filter == 'current_week':
        period_filter = get_current_iso_period()
        period_display = get_current_week_display()

    if not city: return jsonify({"error": "Nom de la ville manquant"}), 400
    if not DB_URL: return jsonify({"error": "Base de données non configurée"}), 500

    try:
        engine = _engine()
        with engine.connect() as conn:
            stats_res = conn.execute(text("""
                SELECT COUNT(DISTINCT athlete_id) AS total_users, SUM(activities_count) AS total_activities
                FROM city_scores
                WHERE city_name = :city AND grid_size = :grid_size AND sport = :sport AND period = :period
            """), {"city": city, "grid_size": grid_size, "sport": sport_filter, "period": period_filter}).fetchone()

            total_users = stats_res.total_users if stats_res and stats_res.total_users else 0
            total_activities = int(stats_res.total_activities) if stats_res and stats_res.total_activities else 0

            board_res = conn.execute(text("""
                SELECT u.athlete_name, c.percent, c.blocks_count, c.activities_count
                FROM city_scores c
                JOIN strava_users u ON c.athlete_id = u.athlete_id
                WHERE c.city_name = :city AND c.grid_size = :grid_size AND c.sport = :sport AND c.period = :period
                ORDER BY c.percent DESC, c.blocks_count DESC, u.athlete_name ASC
            """), {"city": city, "grid_size": grid_size, "sport": sport_filter, "period": period_filter}).fetchall()

            leaderboard = [{
                "name": row.athlete_name if row.athlete_name else "Explorateur Anonyme",
                "percent": row.percent,
                "blocks": row.blocks_count,
                "activities": row.activities_count
            } for row in board_res]

            return jsonify({
                "city": city, "grid_size": grid_size,
                "global_stats": {"total_explorers": int(total_users), "total_activities": total_activities},
                "period_display": period_display, "leaderboard": leaderboard
            }), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ============================================================
# 16. LANCEMENT
# ============================================================
# ============================================================
# 17. TABLEAU DE BORD ADMIN
# ============================================================
def _unique_users(conn, series):
    """Utilisateurs uniques depuis le lancement = comptes présents avant le début du suivi (figé une fois)
    + nouvelles inscriptions jour par jour. Limite connue : quelqu'un qui supprime ses données puis revient
    est compté deux fois (on n'en garde aucune trace, c'est voulu)."""
    base = series("SELECT value FROM app_kv WHERE key = 'unique_baseline'")
    if base:
        baseline = int(base[0]["value"].get("count", 0)); base_day = base[0]["value"].get("day")
    else:
        first = series("SELECT MIN(created_at) AS t FROM app_events")
        t0 = first[0]["t"] if first and first[0]["t"] else None
        n = series("SELECT COUNT(*) AS n FROM strava_users WHERE first_login_date < COALESCE(:t, NOW())", t=t0)
        baseline = int(n[0]["n"]) if n else 0
        base_day = (t0 or datetime.utcnow()).strftime('%Y-%m-%d')
        kv_set('unique_baseline', {"count": baseline, "day": base_day})
    per_day = {}
    for r in series("SELECT day, new_users FROM app_daily_stats WHERE new_users IS NOT NULL"):
        per_day[str(r["day"])] = int(r["new_users"])
    ev = series("SELECT created_at::date AS day, COUNT(*) AS n FROM app_events WHERE kind = 'signup' GROUP BY 1")
    # Pour chaque jour : la plus grande des deux valeurs. Le relevé peut être en retard (jour en cours),
    # le journal peut être incomplet (jour à cheval sur la purge des 400 jours).
    for r in ev: per_day[str(r["day"])] = max(per_day.get(str(r["day"]), 0), int(r["n"]))
    today = datetime.utcnow().date()
    days = sorted(per_day)
    start = min([datetime.strptime(base_day, '%Y-%m-%d').date()] + [datetime.strptime(d, '%Y-%m-%d').date() for d in days])
    out, total, d = [], baseline, start
    while d <= today:
        k = d.strftime('%Y-%m-%d'); new = per_day.get(k, 0); total += new
        out.append({"day": k, "new": new, "total": total}); d += timedelta(days=1)
    last7 = sum(r["new"] for r in out[-7:]); prev7 = sum(r["new"] for r in out[-14:-7])
    return {"total": total, "baseline": baseline, "since": start.strftime('%Y-%m-%d'), "new_7": last7, "new_prev_7": prev7, "series": out}

def _daily_snapshot():
    """Relevé quotidien anonyme (comptes connectés, actifs 7 j / 31 j) + purge du journal > 400 jours."""
    try:
        with _engine().begin() as conn:
            conn.execute(text("""
                INSERT INTO app_daily_stats (day, connected, active_7, active_31)
                SELECT CURRENT_DATE, COUNT(*),
                       COUNT(*) FILTER (WHERE last_login_date > NOW() - INTERVAL '7 days'),
                       COUNT(*) FILTER (WHERE last_login_date > NOW() - INTERVAL '31 days')
                FROM strava_users
                ON CONFLICT (day) DO UPDATE SET connected = EXCLUDED.connected,
                    active_7 = EXCLUDED.active_7, active_31 = EXCLUDED.active_31"""))
            conn.execute(text("DELETE FROM app_events WHERE created_at < NOW() - INTERVAL '400 days'"))
    except Exception as e:
        print(f"Relevé quotidien : {e}")
    # Nouveaux utilisateurs par jour, recopiés dans app_daily_stats (conservé sans limite) : le total
    # « depuis le lancement » reste juste même après la purge du journal (400 jours). Un nombre par jour, rien d'autre.
    try:
        with _engine().begin() as conn:
            conn.execute(text("""
                INSERT INTO app_daily_stats (day, new_users)
                SELECT created_at::date, COUNT(*) FROM app_events WHERE kind = 'signup' GROUP BY 1
                ON CONFLICT (day) DO UPDATE SET new_users = GREATEST(COALESCE(app_daily_stats.new_users, 0), EXCLUDED.new_users)"""))
            # GREATEST : un jour ne peut que gagner des inscriptions ; après la purge du journal,
            # un recalcul partiel ne doit jamais écraser la valeur complète déjà relevée.
    except Exception as e:
        print(f"Relevé des nouveaux utilisateurs (migration 2026-10-30_utilisateurs_uniques.sql lancée ?) : {e}")

@app.route('/admin')
def admin_page():
    if not is_admin(): abort(404)
    return render_template('admin.html', active='admin')

@app.route('/api/admin/stats')
def admin_stats():
    if not is_admin(): return jsonify({"error": "Introuvable"}), 404
    if not DB_URL: return jsonify({"error": "Base de données non configurée"}), 500
    _daily_snapshot()   # garde la courbe à jour même si le cron n'est pas encore passé aujourd'hui
    out = {"capacity": STRAVA_ATHLETE_CAPACITY, "inactive_days": INACTIVE_DAYS, "generated_at": datetime.utcnow().isoformat() + 'Z'}
    with _engine().begin() as conn:
        u = conn.execute(text("""
            SELECT COUNT(*) AS connected,
              COUNT(*) FILTER (WHERE last_login_date > NOW() - INTERVAL '1 day')   AS active_1,
              COUNT(*) FILTER (WHERE last_login_date > NOW() - INTERVAL '7 days')  AS active_7,
              COUNT(*) FILTER (WHERE last_login_date > NOW() - INTERVAL '31 days') AS active_31,
              COUNT(*) FILTER (WHERE first_login_date > NOW() - INTERVAL '7 days') AS new_7,
              COUNT(*) FILTER (WHERE first_login_date <= NOW() - INTERVAL '7 days'
                               AND first_login_date > NOW() - INTERVAL '14 days')  AS new_prev_7,
              COUNT(*) FILTER (WHERE first_login_date < NOW() - INTERVAL '1 day')  AS older_than_1d,
              COUNT(*) FILTER (WHERE first_login_date < NOW() - INTERVAL '1 day'
                               AND last_login_date::date > first_login_date::date) AS returned,
              COUNT(*) FILTER (WHERE last_login_date::date = CURRENT_DATE)                         AS seen_today,
              COUNT(*) FILTER (WHERE last_login_date::date < CURRENT_DATE
                               AND last_login_date > NOW() - INTERVAL '7 days')                    AS seen_week,
              COUNT(*) FILTER (WHERE last_login_date <= NOW() - INTERVAL '7 days'
                               AND last_login_date > NOW() - make_interval(days => :d - 7))         AS seen_month,
              COUNT(*) FILTER (WHERE last_login_date <= NOW() - make_interval(days => :d - 7))      AS at_risk
            FROM strava_users"""), {"d": INACTIVE_DAYS}).mappings().one()
        out["users"] = {k: int(v or 0) for k, v in u.items()}

        def series(sql, **kw):
            try: return [dict(r) for r in conn.execute(text(sql), kw).mappings().all()]
            except Exception as e:
                print(f"Admin : {e}"); conn.rollback(); return []
        out["daily"] = series("""
            SELECT d::date AS day,
                   COUNT(e.*) FILTER (WHERE e.kind = 'visit')  AS visits,
                   COUNT(e.*) FILTER (WHERE e.kind = 'signup') AS signups
            FROM generate_series(CURRENT_DATE - 59, CURRENT_DATE, INTERVAL '1 day') d
            LEFT JOIN app_events e ON e.created_at::date = d::date
            GROUP BY d ORDER BY d""")
        out["weekly"] = series("""
            SELECT w::date AS week,
                   COUNT(e.*) FILTER (WHERE e.kind = 'signup') AS arrivals,
                   COUNT(e.*) FILTER (WHERE e.kind IN ('revoke_inactive', 'delete', 'logout')) AS departures,
                   COUNT(e.*) FILTER (WHERE e.kind = 'revoke_inactive') AS auto_revoked
            FROM generate_series(date_trunc('week', CURRENT_DATE) - INTERVAL '11 weeks', date_trunc('week', CURRENT_DATE), INTERVAL '1 week') w
            LEFT JOIN app_events e ON date_trunc('week', e.created_at) = w
            GROUP BY w ORDER BY w""")
        out["snapshots"] = series("""SELECT day, connected, active_7, active_31 FROM app_daily_stats
                                     WHERE day > CURRENT_DATE - 90 AND connected IS NOT NULL ORDER BY day""")
        out["unique"] = _unique_users(conn, series)
        try:
            out["people"] = [dict(r) for r in conn.execute(text("""
                SELECT athlete_id, first_name, last_name, last_login_date FROM strava_users
                WHERE last_login_date > NOW() - make_interval(days => :d)
                ORDER BY last_login_date DESC LIMIT 500"""), {"d": NAME_DAYS}).mappings().all()]
            for r in out["people"]:   # dates enregistrées en UTC sans fuseau : on le précise pour le navigateur
                t = r["last_login_date"]
                if t is not None: r["last_login_date"] = t.isoformat() + ('Z' if t.tzinfo is None else '')
        except Exception as e:
            print(f"Admin, liste des personnes : {e}"); conn.rollback()
            out["people"] = None   # colonnes absentes : migration 2026-11-01_noms_admin.sql à lancer
        out["people_days"] = NAME_DAYS
        out["recent"] = series("""SELECT kind, created_at FROM app_events
                                  WHERE kind IN ('signup', 'logout', 'delete', 'revoke_inactive')
                                  ORDER BY created_at DESC LIMIT 10""")
        flow = series("""SELECT COUNT(*) FILTER (WHERE kind = 'signup') AS arrivals,
                                COUNT(*) FILTER (WHERE kind IN ('revoke_inactive','delete','logout')) AS departures,
                                MIN(created_at) AS since
                         FROM app_events WHERE created_at > NOW() - INTERVAL '30 days'""")
        out["flow_30"] = flow[0] if flow else {"arrivals": 0, "departures": 0, "since": None}
        kc = series("""SELECT COUNT(*) FILTER (WHERE created_at > NOW() - INTERVAL '30 days') AS d30, COUNT(*) AS total
                       FROM app_events WHERE kind = 'kofi_click'""")
        out["kofi"] = {"enabled": bool(KOFI_URL), **(kc[0] if kc else {"d30": 0, "total": 0})}
        first = series("SELECT MIN(created_at) AS first_event FROM app_events")
        out["tracking_since"] = first[0]["first_event"] if first else None
        kv = {r["key"]: {"value": r["value"], "updated_at": r["updated_at"]} for r in series("SELECT key, value, updated_at FROM app_kv")}
        out["last_cron"] = kv.get("last_cron")
        out["rate_limit"] = kv.get("strava_rate_limit")

    out["health"] = [
        {"label": "Clé secrète Flask personnalisée", "ok": app.secret_key != 'dev_secret_key_123',
         "help": "Définir SECRET_KEY dans Vercel (longue chaîne aléatoire)."},
        {"label": "Secret du cron défini", "ok": bool(os.getenv('CRON_SECRET')),
         "help": "Définir CRON_SECRET dans Vercel, sinon le nettoyage quotidien ne tourne pas."},
        {"label": "Classement désactivé", "ok": not ENABLE_LEADERBOARD,
         "help": "ENABLE_LEADERBOARD doit rester à false (règles Strava)."},
    ]
    return app.response_class(json.dumps(out, default=str), mimetype='application/json')

# ============================================================
# 18. BILAN DE L'ANNÉE (« Votre année Unlock »)
# ============================================================
# Calendrier (heure de Paris) :
#   1er → 15 décembre  : carte d'annonce sur la carte + entrée du menu avec le badge « Nouveau »
#   16 → 31 décembre   : carte d'annonce + entrée du menu
#   1er → 31 janvier   : entrée du menu seulement (« Votre bilan AAAA complet »)
#   à partir du 1er février : retiré complètement.
# L'admin le voit toute l'année (aperçu), en choisissant la période simulée.
# Tout est calculé à la demande pour l'utilisateur seul, rien n'est enregistré (règles Strava).
try:
    from zoneinfo import ZoneInfo
    PARIS = ZoneInfo('Europe/Paris')
except Exception:   # serveur sans base des fuseaux : heure d'hiver de Paris (décembre et janvier)
    from datetime import timezone
    PARIS = timezone(timedelta(hours=1))
ANNEE_PHASES = ('new', 'card', 'menu')
MOIS_FR = ['janvier', 'février', 'mars', 'avril', 'mai', 'juin', 'juillet', 'août', 'septembre', 'octobre', 'novembre', 'décembre']

def _today_paris():
    return datetime.now(PARIS).date()

def annee_state():
    """{'year', 'phase', 'preview'} si le bilan est visible pour l'utilisateur connecté, sinon None.
    phase : 'new' (1er-15 déc.), 'card' (16-31 déc.), 'menu' (janvier)."""
    if 'access_token' not in session: return None
    d = _today_paris()
    if d.month == 12: state = {'year': d.year, 'phase': 'new' if d.day <= 15 else 'card', 'preview': False}
    elif d.month == 1: state = {'year': d.year - 1, 'phase': 'menu', 'preview': False}
    else: state = None
    if is_admin():
        forced = session.get('annee_apercu')
        if state is None:
            state = {'year': d.year, 'phase': forced if forced in ANNEE_PHASES else 'new', 'preview': True}
        elif forced in ANNEE_PHASES:
            state = {**state, 'phase': forced, 'preview': True}
    return state

def annee_context():
    st = annee_state()
    if not st: return None
    y, ph = st['year'], st['phase']
    return {**st,
            'title': f"Votre bilan {y} complet" if ph == 'menu' else f"Votre année {y}",
            'sub': "Disponible jusqu'au 31 janvier",
            'badge': ph == 'new', 'card': ph in ('new', 'card')}

def _fr_date(d):
    return f"{d.day}{'er' if d.day == 1 else ''} {MOIS_FR[d.month - 1]}"

SPORT_TITRES = {'Run': 'La course à pied', 'TrailRun': 'Le trail', 'Hike': 'La randonnée', 'Walk': 'La marche',
                'Snowshoe': 'Les raquettes', 'Ride': 'Le vélo', 'GravelRide': 'Le gravel', 'E-BikeRide': 'Le vélo électrique',
                'Velomobile': 'Le vélomobile'}
MOMENTS = [  # (début, fin, titre, libellé) : heure locale de départ
    (5, 11, 'Plutôt du matin', 'le matin, entre 5 h et 11 h'),
    (11, 14, 'Plutôt à midi', 'entre 11 h et 14 h'),
    (14, 18, "Plutôt l'après-midi", "l'après-midi, entre 14 h et 18 h"),
    (18, 22, 'Plutôt du soir', 'le soir, entre 18 h et 22 h'),
    (22, 29, 'Plutôt de nuit', 'la nuit, après 22 h'),
]

def _evol(cur, prev):
    """Évolution en % (None si pas de référence)."""
    if not prev: return None
    return round((cur - prev) / prev * 100)

ZONE_KM = 20          # taille des carrés utilisés pour regrouper les blocs en zones
ZONE_MIN_BLOCS = 8    # une zone plus petite ne compte pas comme « autre zone » (simple passage)

def _zones(cells, dlat):
    """Regroupe des blocs en zones : carrés de ~20 km voisins réunis. Renvoie une liste d'ensembles, la plus grande en premier."""
    q = ZONE_KM * 1000 / 111320
    buckets = defaultdict(set)
    for c in cells:
        buckets[(int(c[0] // q), int(c[1] // (q / max(.2, math.cos(math.radians(c[0]))))))].add(c)
    seen, zones = set(), []
    for start in buckets:
        if start in seen: continue
        stack, comp = [start], set()
        seen.add(start)
        while stack:
            b0 = stack.pop(); comp |= buckets[b0]
            for di in (-1, 0, 1):
                for dj in (-1, 0, 1):
                    nb = (b0[0] + di, b0[1] + dj)
                    if nb in buckets and nb not in seen: seen.add(nb); stack.append(nb)
        zones.append(comp)
    zones.sort(key=len, reverse=True)
    return zones

def _carte_blocs(cells_upto, cells_new, cells_touched, dlat, max_cells=2600):
    """Mosaïque de la zone principale de l'année (celle où l'on a débloqué le plus de blocs), pour qu'elle
    reste lisible même quand on a exploré plusieurs villes éloignées.
    Renvoie des indices de grille relatifs [ligne, colonne, débloqué cette année] + le nombre d'autres zones."""
    ref = cells_new or cells_touched or cells_upto
    if not ref: return {"rows": 0, "cols": 0, "cells": [], "other_zones": 0, "zone": set()}
    zones = _zones(ref, dlat)
    main = zones[0]
    others = sum(1 for z in zones[1:] if len(z) >= ZONE_MIN_BLOCS)
    lats = np.array([c[0] for c in main]); lons = np.array([c[1] for c in main])
    lat0, lat1 = np.percentile(lats, [3, 97]); lon0, lon1 = np.percentile(lons, [3, 97])
    pad_lat = max(dlat * 3, (lat1 - lat0) * .1); pad_lon = max(dlat * 4, (lon1 - lon0) * .1)
    lat0, lat1, lon0, lon1 = lat0 - pad_lat, lat1 + pad_lat, lon0 - pad_lon, lon1 + pad_lon
    dl = _dlon((lat0 + lat1) / 2, dlat)
    rows = int(round((lat1 - lat0) / dlat)) + 1; cols = int(round((lon1 - lon0) / dl)) + 1
    # Zone encore très grande : on regroupe les blocs affichés pour rester lisible
    step = 1
    while rows * cols / (step * step) > 90 * 90: step += 1
    out = {}
    for c in cells_upto:
        if lat0 <= c[0] <= lat1 and lon0 <= c[1] <= lon1:
            # arrondi (et non troncature) : sinon des lignes de blocs se dédoublent ou disparaissent
            r = int(round((lat1 - c[0]) / dlat)) // step; k = int(round((c[1] - lon0) / dl)) // step
            out[(r, k)] = out.get((r, k), False) or (c in cells_new)
    cells = [[r, k, 1 if v else 0] for (r, k), v in out.items()]
    if len(cells) > max_cells: cells = cells[:max_cells]
    return {"rows": rows // step + 1, "cols": cols // step + 1, "cells": cells, "other_zones": others, "zone": main}

def compute_annee(year, as_of):
    """Bilan de l'année `year` pour l'utilisateur connecté (ses données seulement)."""
    grid_m = 250
    dlat = grid_m / 111320
    activities = sorted(get_strava_activities_cached(), key=lambda a: a['start_date_local'])
    ys, ye = f"{year}-01-01", f"{year + 1}-01-01"
    pys = f"{year - 1}-01-01"
    before_prev, before, seen = set(), set(), set()
    acts_y, acts_prev = [], []
    cells_y_all = set()
    month_new = [0] * 12
    best = None
    for a in activities:
        d = a['start_date_local']
        if d >= ye: break
        pts = polyline.decode(a['polyline'])
        if len(pts) < 2: continue
        cells = get_cells_from_polyline(pts, dlat)
        if d < ys:
            if d < pys: before_prev |= cells
            else: acts_prev.append(a)
            before |= cells
            continue
        new = cells - before - seen
        seen |= cells
        cells_y_all |= cells
        acts_y.append(a)
        month_new[int(d[5:7]) - 1] += len(new)
        if new and (best is None or len(new) > best[1]):
            best = (a, len(new), pts, new)
    new_y = len((before | seen)) - len(before)
    new_prev = len(before) - len(before_prev)
    upto = before | seen
    km_y = sum(a.get('distance', 0) or 0 for a in acts_y) / 1000
    km_prev = sum(a.get('distance', 0) or 0 for a in acts_prev) / 1000

    res = {"year": year, "as_of": _fr_date(as_of) if as_of.year == year else None,
           "complete": as_of.year > year, "empty": not acts_y}
    res["chiffres"] = {
        "new_blocks": new_y, "new_blocks_prev": new_prev, "new_blocks_evol": _evol(new_y, new_prev),
        "total_before": len(before), "total_after": len(upto),
        "outings": len(acts_y), "outings_prev": len(acts_prev), "outings_diff": len(acts_y) - len(acts_prev) if acts_prev else None,
        "km": round(km_y), "km_evol": _evol(km_y, km_prev),
        "first_year": not before,
    }
    # Mosaïque (couverture et story)
    res["mosaique"] = _carte_blocs(upto, seen - before, seen, dlat)
    zone_cells = res["mosaique"].pop("zone")
    res["mosaique"]["name"] = None

    # Sortie la plus marquante : la plus riche en blocs jamais traversés auparavant
    if best:
        a, n, pts, new = best
        step = max(1, len(pts) // 260)
        tr = pts[::step] + ([pts[-1]] if (len(pts) - 1) % step else [])
        dt = datetime.strptime(a['start_date_local'], "%Y-%m-%dT%H:%M:%SZ")
        res["sortie"] = {"new_blocks": n, "date": f"{['Lundi','Mardi','Mercredi','Jeudi','Vendredi','Samedi','Dimanche'][dt.weekday()]} {_fr_date(dt)}",
                         "sport": SPORT_TRANSLATIONS.get(a['type'], a['type']), "km": round((a.get('distance', 0) or 0) / 1000, 1),
                         "trace": [[round(p[0], 5), round(p[1], 5)] for p in tr],
                         "cells": [[round(c[0], 6), round(c[1], 6)] for c in list(new)[:400]], "dlat": dlat}
    else:
        res["sortie"] = None

    # Habitudes : heures et jours de départ, sports, nouveaux blocs par mois
    hours, days, sports = [0] * 24, [0] * 7, defaultdict(int)
    for a in acts_y:
        dt = datetime.strptime(a['start_date_local'], "%Y-%m-%dT%H:%M:%SZ")
        hours[dt.hour] += 1; days[dt.weekday()] += 1; sports[a['type']] += 1
    moment = None
    if acts_y:
        counts = [sum(hours[h % 24] for h in range(a0, b0)) for a0, b0, _, _ in MOMENTS]
        i = max(range(len(MOMENTS)), key=lambda k: counts[k])
        a0, b0, titre, lib = MOMENTS[i]
        moment = {"title": titre, "label": lib, "percent": round(counts[i] / len(acts_y) * 100), "from": a0, "to": b0}
    top_day = max(range(7), key=lambda k: days[k]) if acts_y else None
    sp = sorted(sports.items(), key=lambda kv: kv[1], reverse=True)
    split = [{"code": k, "label": SPORT_TRANSLATIONS.get(k, k), "percent": round(v / len(acts_y) * 100)} for k, v in sp[:3]] if acts_y else []
    autres = 100 - sum(s['percent'] for s in split)
    if len(sp) > 3 and autres > 0: split.append({"code": "autres", "label": "Autres", "percent": autres})
    elif split: split[0]['percent'] += autres   # arrondis : le total fait toujours 100 %
    if sp:
        main, share = sp[0][0], sp[0][1] / len(acts_y)
        sport_title = f"{SPORT_TITRES.get(main, SPORT_TRANSLATIONS.get(main, main))} avant tout" if share >= .5 else "Plusieurs sports au programme"
    else:
        sport_title = None
    last_month = 12 if as_of.year > year else as_of.month
    rec = max(range(last_month), key=lambda k: month_new[k]) if any(month_new) else None
    res["habitudes"] = {"hours": hours, "days": days, "moment": moment,
                        "top_day": WEEKDAYS_FR[top_day] if top_day is not None else None, "top_day_index": top_day,
                        "sports": split, "sport_title": sport_title,
                        "months": month_new, "last_month": last_month,
                        "record": {"month": MOIS_FR[rec], "index": rec, "blocks": month_new[rec]} if rec is not None and month_new[rec] else None}

    # Communes et départements de l'année
    res["communes"], res["departements"], res["chiffres"]["new_communes"] = [], [], 0
    if DB_URL and acts_y:
        try:
            _annee_territoires(res, year, before, upto, seen, dlat, grid_m, zone_cells)
        except Exception as e:
            print(f"Bilan : communes indisponibles ({e})")
    return res

def _annee_territoires(res, year, before, upto, cells_y, dlat, grid_m, zone_cells=frozenset()):
    """Top 5 des communes (points gagnés dans l'année) et départements explorés dans l'année."""
    data = _activities_data('all', 'all', grid_m)
    ys, ye = f"{year}-01-01", f"{year}-12-31"
    cities = data.get('top_municipalities', [])
    # Nouvelles communes de l'année : 1re sortie dans la commune pendant l'année
    res["chiffres"]["new_communes"] = sum(1 for c in cities if c['stats'].get('first') and ys <= c['stats']['first'] <= ye)
    cand = [c for c in cities if c.get('code') and c['stats'].get('first') and c['stats']['first'] <= ye and c['stats'].get('last', '') >= ys]
    if not cand: return
    with _engine().connect() as conn:
        rows = conn.execute(text("SELECT code_insee, ST_AsGeoJSON(geometry) AS g FROM communes WHERE code_insee = ANY(:c)"),
                            {"c": [c['code'] for c in cand]}).fetchall()
    geoms = {}
    for r in rows:
        if r.g and r.code_insee not in geoms:
            geoms[r.code_insee] = shp_transform(lambda x, y, z=None: (y, x), shp_shape(json.loads(r.g))).buffer(0)
    profil = data.get('acces_profil', 'tous')
    communes, active_deps = [], set()
    best_zone = [0, None]   # commune qui concentre le plus de blocs de l'année dans la zone de la mosaïque
    zb = (min(k[0] for k in zone_cells), max(k[0] for k in zone_cells), min(k[1] for k in zone_cells), max(k[1] for k in zone_cells)) if zone_cells else None
    for c in cand:
        g = geoms.get(c['code'])
        if g is None or g.is_empty: continue
        all_cells = commune_cells(g, dlat)
        if not all_cells or not (all_cells & cells_y): continue
        active_deps.add(c['dep'])
        dans_zone = sum(1 for k in (all_cells & cells_y) if zb and zb[0] <= k[0] <= zb[1] and zb[2] <= k[1] <= zb[3])
        if dans_zone and dans_zone > best_zone[0]: best_zone[0], best_zone[1] = dans_zone, c['name']
        hors = acces.filtre_inaccessibles(acces.keys_of_cells(all_cells, dlat), c['dep'], grid_m, profil)
        hors = set(hors.tolist()) if len(hors) else set()
        def pct(vus):
            vus = all_cells & vus
            n_hors_vus = sum(1 for k in acces.keys_of_cells(vus, dlat).tolist() if k in hors) if vus and hors else 0
            total = max(len(vus), len(all_cells) - (len(hors) - n_hors_vus))
            return min(100.0, len(vus) / max(1, total) * 100)
        p1, p0 = pct(upto), pct(before)
        communes.append({"name": c['name'], "code": c['code'], "percent": round(p1), "points": round(p1 - p0),
                         "new": not (all_cells & before)})
    res["mosaique"]["name"] = best_zone[1]
    communes.sort(key=lambda x: (x['points'], x['percent']), reverse=True)
    res["communes"] = [c for c in communes if c['points'] > 0][:5] or communes[:5]
    totals = dep_totals()
    deps = []
    for dep in active_deps:
        cs = [c for c in cities if c.get('dep') == dep and c['stats'].get('first')]
        n1 = sum(1 for c in cs if c['stats']['first'] <= ye)
        n0 = sum(1 for c in cs if c['stats']['first'] < ys)
        total = max(totals.get(dep, 0), n1, 1)
        deps.append({"code": dep, "name": DEPARTEMENTS.get(dep, dep), "percent": round(n1 / total * 100, 1),
                     "points": round((n1 - n0) / total * 100, 1), "communes": n1})
    deps.sort(key=lambda d: (d['percent'], d['communes']), reverse=True)
    res["departements_total"] = len(deps)
    res["departements"] = deps[:4]

@app.route('/annee')
def annee_page():
    if not session.get('access_token'): return redirect(url_for('login_page'))
    st = annee_context()
    if not st: abort(404)
    return render_template('annee.html', active=None, annee=st)

@app.route('/annee/apercu', methods=['POST'])
def annee_apercu():
    """Admin : choisir la période simulée du bilan (pour vérifier la carte, le menu et les textes)."""
    if not is_admin() or request.form.get('csrf') != session.get('csrf'): abort(404)
    ph = request.form.get('phase')
    if ph in ANNEE_PHASES: session['annee_apercu'] = ph
    else: session.pop('annee_apercu', None)
    nxt = request.form.get('next', '/annee')
    return redirect(nxt if nxt.startswith('/') and not nxt.startswith('//') else '/annee')

@app.route('/api/annee')
def api_annee():
    if not session.get('access_token'): return jsonify({"error": "Login required"}), 401
    st = annee_state()
    if not st: return jsonify({"error": "Introuvable"}), 404
    activities = get_strava_activities_cached()
    as_of = _today_paris()
    key = f"annee_{st['year']}_{as_of}_{_data_version(activities)}"
    cache = _result_cache(_user_key())
    if key not in cache:
        cache[key] = compute_annee(st['year'], as_of)
    return jsonify(cache[key])


# ============================================================
# 19. EXPORT DE MES CHIFFRES (zip de fichiers CSV + GeoJSON)
# ============================================================
# Uniquement les chiffres de l'utilisateur connecté, calculés à la demande par Unlock (rien n'est stocké en plus).
# Pas de traces GPS ni de données brutes Strava (Strava a déjà son propre export).
import io, csv, zipfile
EXPORT_MIN_PCT = 5            # évolution par commune : communes explorées à 5 % ou plus
JOURS_FR = ['lundi', 'mardi', 'mercredi', 'jeudi', 'vendredi', 'samedi', 'dimanche']

def _fr(x, nd=1):
    """Nombre au format français (virgule décimale) pour Excel."""
    if x is None or x == '': return ''
    if isinstance(x, int): return str(x)
    return f"{x:.{nd}f}".replace('.', ',')

def _csv(rows, header):
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=';', lineterminator='\r\n')
    w.writerow(header); w.writerows(rows)
    return '﻿' + buf.getvalue()          # BOM : Excel ouvre les accents correctement

def _mois_suivant(k):
    y, m = int(k[:4]), int(k[5:]) + 1
    return f"{y + (m > 12)}-{(m - 1) % 12 + 1:02d}"

def _dt_fr(v):
    if not v: return ''
    try:
        from datetime import timezone
        if not getattr(v, 'tzinfo', None): v = v.replace(tzinfo=timezone.utc)   # colonnes sans fuseau : enregistrées en UTC
        return v.astimezone(PARIS).strftime('%d/%m/%Y %H:%M')
    except Exception: return str(v)

def _d_fr(iso):
    return f"{iso[8:10]}/{iso[5:7]}/{iso[:4]}" if iso else ''

def _wiki_url(w):
    """Même lien que la fiche lieu : article Wikipédia en français, sinon fiche Wikidata."""
    if not w: return ''
    if w.startswith('fr:'):
        from urllib.parse import quote
        return 'https://fr.wikipedia.org/wiki/' + quote(w[3:].replace(' ', '_'))
    return 'https://www.wikidata.org/wiki/' + w

def compute_export():
    grid_m = 250
    dlat = grid_m / 111320
    today = _today_paris()
    activities = sorted(get_strava_activities_cached(), key=lambda a: a['start_date_local'])
    files = {}

    # --- Sorties décodées, blocs nouveaux par sortie (global et par sport) ---
    first_cell, cell_sports = {}, defaultdict(set)
    sport_seen = defaultdict(set)
    acts = []
    for a in activities:
        pts = polyline.decode(a['polyline']) if a.get('polyline') else []
        cells = get_cells_from_polyline(pts, dlat) if len(pts) >= 2 else set()
        d = a['start_date_local']
        new = {c for c in cells if c not in first_cell}
        for c in new: first_cell[c] = d[:10]
        for c in cells: cell_sports[c].add(a['type'])
        new_sport = cells - sport_seen[a['type']]; sport_seen[a['type']] |= cells
        acts.append({"a": a, "d": d, "dt": datetime.strptime(d, "%Y-%m-%dT%H:%M:%SZ"), "cells": cells,
                     "new": len(new), "new_sport": len(new_sport), "km": (a.get('distance', 0) or 0) / 1000})

    data = _activities_data('all', 'all', grid_m) if activities else {}
    cities = [c for c in data.get('top_municipalities', []) if c.get('code')]
    profil = data.get('acces_profil', 'tous')

    # --- Contours des communes visitées (une seule requête) ---
    geoms = {}
    if DB_URL and cities:
        try:
            with _engine().connect() as conn:
                for r in conn.execute(text("SELECT code_insee, ST_AsGeoJSON(geometry) AS g FROM communes WHERE code_insee = ANY(:c)"),
                                      {"c": [c['code'] for c in cities]}):
                    if r.g and r.code_insee not in geoms:
                        geoms[r.code_insee] = shp_transform(lambda x, y, z=None: (y, x), shp_shape(json.loads(r.g))).buffer(0)
        except Exception as e:
            print(f"Export : contours indisponibles ({e})")
    com_cells, cell_com = {}, {}
    for c in cities:
        g = geoms.get(c['code'])
        if g is None or g.is_empty: continue
        cc = commune_cells(g, dlat); com_cells[c['code']] = cc
        for cell in cc:
            if cell in first_cell and cell not in cell_com: cell_com[cell] = c['code']
    name_of = {c['code']: c['name'] for c in cities}
    first_com = {c['code']: c['stats'].get('first') for c in cities}

    # --- Lieux débloqués ---
    lieux_rows, lieux_par_jour = [], defaultdict(list)
    try:
        base = _lieux_base() if activities else {'deps': []}
        for dep in base['deps']:
            d = lieux.departement(dep)
            if not d: continue
            pris = _lieux_pris(base, dep)
            for i, date in pris.items():
                r = d['lieux'][i]
                lieux_rows.append([r[6], lieux.CATEGORIES.get(r[4], (r[4],))[0], 'oui' if r[5] else 'non',
                                   d['communes'].get(r[7], ''), DEPARTEMENTS.get(dep, dep), _d_fr(date),
                                   _fr(r[1], 5), _fr(r[2], 5), _wiki_url(r[8])])
                lieux_par_jour[date].append(r[6])
    except Exception as e:
        print(f"Export : lieux indisponibles ({e})")
    lieux_rows.sort(key=lambda r: (r[5][6:] + r[5][3:5] + r[5][:2], r[0]))

    # ============ Photos du jour ============
    files['communes.csv'] = _csv([[c['name'], c['code'], DEPARTEMENTS.get(c['dep'], c['dep']), _fr(c['stats']['percent'], 1),
                                   c['stats']['blocks'], c['stats']['total_blocks'], _fr(c['stats'].get('km', 0), 1),
                                   c['stats'].get('activities', 0), _d_fr(c['stats'].get('first')), _d_fr(c['stats'].get('last'))]
                                  for c in sorted(cities, key=lambda c: c['stats']['percent'], reverse=True)],
                                 ['Commune', 'Code INSEE', 'Département', '% exploré', 'Blocs explorés', 'Blocs au total',
                                  'Km dans la commune', 'Sorties', 'Première sortie', 'Dernière sortie'])
    files['departements.csv'] = _csv([[d['name'], d['code'], d['visited'], d['total'], _fr(d['percent'], 1), d['at100'], d['over50']]
                                      for d in data.get('departements', [])],
                                     ['Département', 'Code', 'Communes visitées', 'Communes au total', '% des communes visitées',
                                      'Communes à 100 %', 'Communes à 50 % ou plus'])
    files['lieux.csv'] = _csv(lieux_rows, ['Lieu', 'Catégorie', 'Monument historique', 'Commune', 'Département',
                                           'Date de déblocage', 'Latitude', 'Longitude', 'Wikipédia ou Wikidata'])
    sp = defaultdict(lambda: {"n": 0, "km": 0.0})
    for x in acts: sp[x['a']['type']]['n'] += 1; sp[x['a']['type']]['km'] += x['km']
    files['sports.csv'] = _csv([[SPORT_TRANSLATIONS.get(k, k), v['n'], _fr(v['km'], 1), len(sport_seen[k]),
                                 len({cell_com[c] for c in sport_seen[k] if c in cell_com})]
                                for k, v in sorted(sp.items(), key=lambda kv: kv[1]['n'], reverse=True)],
                               ['Sport', 'Sorties', 'Km', 'Blocs parcourus', 'Communes visitées'])

    # ============ Évolution ============
    mois = []
    if acts:
        k, fin = acts[0]['d'][:7], f"{today:%Y-%m}"
        while k <= fin: mois.append(k); k = _mois_suivant(k)
    par_mois = defaultdict(lambda: {"n": 0, "km": 0.0, "new": 0})
    par_mois_sport = defaultdict(lambda: {"n": 0, "km": 0.0, "new": 0})
    for x in acts:
        m = x['d'][:7]; s = x['a']['type']
        par_mois[m]['n'] += 1; par_mois[m]['km'] += x['km']; par_mois[m]['new'] += x['new']
        ms = par_mois_sport[(m, s)]; ms['n'] += 1; ms['km'] += x['km']; ms['new'] += x['new_sport']
    com_mois = defaultdict(int)
    for code, f in first_com.items():
        if f: com_mois[f[:7]] += 1
    lieux_mois = defaultdict(int)
    for date, noms in lieux_par_jour.items(): lieux_mois[date[:7]] += len(noms)
    rows, cb, cc, cl = [], 0, 0, 0
    for m in mois:
        p = par_mois[m]; cb += p['new']; cc += com_mois[m]; cl += lieux_mois[m]
        rows.append([m, p['n'], _fr(p['km'], 1), p['new'], cb, com_mois[m], cc, lieux_mois[m], cl])
    files['evolution_mensuelle.csv'] = _csv(rows, ['Mois', 'Sorties', 'Km', 'Nouveaux blocs', 'Blocs cumulés',
                                                   'Nouvelles communes', 'Communes cumulées', 'Lieux débloqués', 'Lieux cumulés'])
    files['evolution_par_sport.csv'] = _csv([[m, SPORT_TRANSLATIONS.get(s, s), v['n'], _fr(v['km'], 1), v['new']]
                                             for (m, s), v in sorted(par_mois_sport.items())],
                                            ['Mois', 'Sport', 'Sorties', 'Km', 'Nouveaux blocs pour ce sport'])
    totals = dep_totals()
    rows = []
    for dep in sorted({c['dep'] for c in cities if c.get('dep')}):
        firsts = sorted(f[:7] for code, f in first_com.items() if f and dep_of(code) == dep)
        if not firsts: continue
        total = max(totals.get(dep, 0), len(firsts))
        n = 0
        for m in mois:
            if m < firsts[0]: continue
            n = sum(1 for f in firsts if f <= m)
            rows.append([m, DEPARTEMENTS.get(dep, dep), dep, n, total, _fr(n / total * 100, 1)])
    files['evolution_departements.csv'] = _csv(rows, ['Mois', 'Département', 'Code', 'Communes visitées (cumul)',
                                                      'Communes au total', '% des communes visitées'])

    # % par commune, fin de chaque mois (communes explorées à 5 % ou plus)
    cell_mois = {c: d[:7] for c, d in first_cell.items()}
    rows, cent_pour_cent = [], []
    for c in sorted(cities, key=lambda c: c['stats']['percent'], reverse=True):
        if c['stats']['percent'] < EXPORT_MIN_PCT or c['code'] not in com_cells: continue
        allc = com_cells[c['code']]
        try:
            hors = set(acces.filtre_inaccessibles(acces.keys_of_cells(allc, dlat), c['dep'], grid_m, profil).tolist()) if allc else set()
        except Exception:
            hors = set()
        vus_par_mois = defaultdict(set)
        for cell in allc:
            if cell in cell_mois: vus_par_mois[cell_mois[cell]].add(cell)
        if not vus_par_mois: continue
        vus, n_hors_vus, atteint = set(), 0, None
        for m in mois:
            if m < min(vus_par_mois): continue
            neufs = vus_par_mois.get(m, set())
            if neufs and hors:
                n_hors_vus += sum(1 for k in acces.keys_of_cells(neufs, dlat).tolist() if k in hors)
            vus |= neufs
            total = max(len(vus), len(allc) - (len(hors) - n_hors_vus))
            pct = min(100.0, len(vus) / max(1, total) * 100)
            rows.append([m, c['name'], c['code'], _fr(pct, 1), len(vus), total])
            if pct >= 100 and not atteint: atteint = m
        if atteint: cent_pour_cent.append((atteint, c['name']))
    files['evolution_communes.csv'] = _csv(rows, ['Mois', 'Commune', 'Code INSEE', '% exploré en fin de mois', 'Blocs explorés', 'Blocs au total'])

    # Résumé annuel : global puis par sport, écart avec l'année précédente
    annees = sorted({x['d'][:4] for x in acts})
    def bilan(sel):
        out = {}
        for y in annees:
            xs = [x for x in acts if x['d'][:4] == y and (sel is None or x['a']['type'] == sel)]
            out[y] = {"n": len(xs), "km": sum(x['km'] for x in xs),
                      "new": sum(x['new'] if sel is None else x['new_sport'] for x in xs)}
        return out
    def ecart(cur, prev):
        return '' if not prev else _fr((cur - prev) / prev * 100, 0)
    rows = []
    g = bilan(None)
    for i, y in enumerate(annees):
        p = g[annees[i - 1]] if i else None
        nc = sum(1 for f in first_com.values() if f and f[:4] == y)
        nl = sum(len(v) for k, v in lieux_par_jour.items() if k[:4] == y)
        rows.append([y, 'Tous', g[y]['n'], _fr(g[y]['km'], 1), g[y]['new'], nc, nl,
                     '' if not p else g[y]['n'] - p['n'], ecart(g[y]['km'], p and p['km']), ecart(g[y]['new'], p and p['new'])])
    for s in sorted(sp, key=lambda k: sp[k]['n'], reverse=True):
        b = bilan(s)
        for i, y in enumerate(annees):
            if not b[y]['n']: continue
            p = b[annees[i - 1]] if i else None
            rows.append([y, SPORT_TRANSLATIONS.get(s, s), b[y]['n'], _fr(b[y]['km'], 1), b[y]['new'], '', '',
                         '' if not p or not p['n'] else b[y]['n'] - p['n'], ecart(b[y]['km'], p and p['km']), ecart(b[y]['new'], p and p['new'])])
    files['resume_annuel.csv'] = _csv(rows, ['Année', 'Sport', 'Sorties', 'Km', 'Nouveaux blocs', 'Nouvelles communes',
                                             'Lieux débloqués', 'Écart sorties', 'Écart km (%)', 'Écart nouveaux blocs (%)'])

    # ============ Détail par sortie ============
    files['sorties.csv'] = _csv([[_d_fr(x['d'][:10]), x['d'][11:16], SPORT_TRANSLATIONS.get(x['a']['type'], x['a']['type']), _fr(x['km'], 2),
                                  x['new'], ' | '.join(sorted({name_of[cell_com[c]] for c in x['cells'] if c in cell_com})),
                                  ' | '.join(lieux_par_jour.get(x['d'][:10], []))] for x in reversed(acts)],
                                ['Date', 'Heure', 'Sport', 'Km', 'Nouveaux blocs', 'Communes traversées', 'Lieux débloqués ce jour-là'])

    # ============ Records ============
    rec = []
    if acts:
        b = max(acts, key=lambda x: x['new'])
        rec.append(['Sortie avec le plus de nouveaux blocs', b['new'], _d_fr(b['d'][:10]), f"{SPORT_TRANSLATIONS.get(b['a']['type'], b['a']['type'])}, {_fr(b['km'], 1)} km"])
        l = max(acts, key=lambda x: x['km'])
        rec.append(['Plus longue sortie (km)', _fr(l['km'], 1), _d_fr(l['d'][:10]), SPORT_TRANSLATIONS.get(l['a']['type'], l['a']['type'])])
        mm = max(mois, key=lambda m: par_mois[m]['new'])
        rec.append(['Mois avec le plus de nouveaux blocs', par_mois[mm]['new'], mm, ''])
        yy = max(annees, key=lambda y: g[y]['n'])
        rec.append(["Année avec le plus de sorties", g[yy]['n'], yy, ''])
        semaines = sorted({x['dt'].isocalendar()[:2] for x in acts})
        best, run, start, best_start = 0, 0, None, None
        prev = None
        for wk in semaines:
            d0 = datetime.fromisocalendar(wk[0], wk[1], 1)
            run = run + 1 if prev and (d0 - prev).days == 7 else 1
            if run == 1: start = d0
            if run > best: best, best_start = run, start
            prev = d0
        rec.append(['Plus longue série de semaines avec au moins une sortie', best, f"à partir du {best_start:%d/%m/%Y}" if best_start else '', ''])
        if cent_pour_cent:
            m, n = min(cent_pour_cent)
            rec.append(['Première commune explorée à 100 %', n, m, ''])
        zones = _zones(set(first_cell), dlat)
        if zones and geoms:
            z = zones[0]; zlat = sum(c[0] for c in z) / len(z); zlon = sum(c[1] for c in z) / len(z)
            def dist(code):
                ct = geoms[code].centroid
                return math.hypot((ct.x - zlat) * 111.32, (ct.y - zlon) * 111.32 * math.cos(math.radians(zlat)))
            far = max(geoms, key=dist)
            rec.append(['Commune explorée la plus éloignée de votre zone principale', name_of.get(far, far), '', f"{_fr(dist(far), 0)} km"])
    files['records.csv'] = _csv(rec, ['Record', 'Valeur', 'Date', 'Détail'])

    # ============ Habitudes ============
    rows = []
    for y in annees:
        xs = [x for x in acts if x['d'][:4] == y]
        for h in range(24):
            n = sum(1 for x in xs if x['dt'].hour == h)
            if n: rows.append([y, 'Heure de départ', f"{h} h", n])
        for j in range(7):
            n = sum(1 for x in xs if x['dt'].weekday() == j)
            if n: rows.append([y, 'Jour de la semaine', JOURS_FR[j], n])
    files['habitudes.csv'] = _csv(rows, ['Année', 'Type', 'Valeur', 'Sorties'])

    # ============ Cartes (GeoJSON) ============
    feats = []
    for (la, lo), d in sorted(first_cell.items(), key=lambda kv: kv[1]):
        dl = _dlon(la, dlat); h, w = dlat / 2, dl / 2
        ring = [[round(lo - w, 6), round(la - h, 6)], [round(lo + w, 6), round(la - h, 6)], [round(lo + w, 6), round(la + h, 6)],
                [round(lo - w, 6), round(la + h, 6)], [round(lo - w, 6), round(la - h, 6)]]
        feats.append({"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [ring]},
                      "properties": {"premier_passage": d, "sports": ', '.join(sorted(SPORT_TRANSLATIONS.get(s, s) for s in cell_sports[(la, lo)]))}})
    files['blocs.geojson'] = json.dumps({"type": "FeatureCollection", "features": feats}, ensure_ascii=False, separators=(',', ':'))
    feats = []
    for c in cities:
        gg = geoms.get(c['code'])
        if gg is None: continue
        simp = shp_transform(lambda x, y, z=None: (round(y, 5), round(x, 5)), gg.simplify(0.0002, preserve_topology=True))
        feats.append({"type": "Feature", "geometry": simp.__geo_interface__,
                      "properties": {"nom": c['name'], "code_insee": c['code'], "departement": DEPARTEMENTS.get(c['dep'], c['dep']),
                                     "pourcentage": c['stats']['percent'], "blocs": c['stats']['blocks'], "blocs_total": c['stats']['total_blocks']}})
    files['communes.geojson'] = json.dumps({"type": "FeatureCollection", "features": feats}, ensure_ascii=False, separators=(',', ':'))

    # ============ Mon compte (droit d'accès RGPD) ============
    rows = [['Identifiant Strava', session.get('athlete_id', '')]]
    if DB_URL and session.get('athlete_id'):
        try:
            with _engine().connect() as conn:
                u = conn.execute(text("""SELECT first_name, last_name, name_at, first_login_date, last_login_date, login_count,
                                                cgu_version, cgu_accepted_at, full_sync_at
                                         FROM strava_users WHERE athlete_id = :a"""), {"a": session['athlete_id']}).mappings().first()
                n_cache = conn.execute(text("SELECT COUNT(*) FROM activities WHERE athlete_id = :a"), {"a": session['athlete_id']}).scalar()
            if u:
                rows += [['Prénom (copie Strava, 7 jours au plus)', u['first_name'] or ''], ['Nom (copie Strava, 7 jours au plus)', u['last_name'] or ''],
                         ['Nom mis à jour le', _dt_fr(u['name_at'])], ['Première connexion', _dt_fr(u['first_login_date'])],
                         ['Dernière connexion', _dt_fr(u['last_login_date'])], ['Nombre de connexions', u['login_count'] or 0],
                         ['Version des CGU acceptée', u['cgu_version'] or ''], ['CGU acceptées le', _dt_fr(u['cgu_accepted_at'])],
                         ['Dernière synchronisation complète avec Strava', _dt_fr(u['full_sync_at'])],
                         ['Sorties gardées en cache (47 heures au plus)', n_cache or 0]]
        except Exception as e:
            print(f"Export : compte indisponible ({e})")
    rows.append(["Jetons d'accès Strava", 'Enregistrés pour la connexion, non exportés (ce sont des secrets)'])
    files['mon_compte.csv'] = _csv(rows, ['Information', 'Valeur'])

    files['lisez-moi.txt'] = EXPORT_LISEZ_MOI.format(date=today.strftime('%d/%m/%Y'), pct=EXPORT_MIN_PCT, maxc=MAX_COMMUNES)
    return files

EXPORT_LISEZ_MOI = """Unlock · vos chiffres au {date}
==============================

Ce dossier contient uniquement vos chiffres, calculés par Unlock à partir de vos sorties Strava.
Les traces GPS et le détail Strava de vos activités n'y sont pas. Strava propose son propre export
(Paramètres du compte > Mon compte > Télécharger ou supprimer votre compte).

Fichiers CSV : séparateur point-virgule, virgule décimale, encodage UTF-8. Ils s'ouvrent dans Excel,
LibreOffice ou Numbers. Blocs de 250 m, comme la carte par défaut.

À la date du téléchargement
- mon_compte.csv : ce qu'Unlock enregistre sur vous (droit d'accès RGPD).
- communes.csv : chaque commune visitée, % exploré, blocs, km dans la commune, première et dernière sortie
  ({maxc} communes au plus).
- departements.csv : part des communes visitées par département.
- lieux.csv : lieux débloqués (monuments, points de vue...), avec la date de déblocage.
- sports.csv : sorties, km, blocs parcourus et communes visitées par sport.
- records.csv : vos records personnels.

Dans le temps
- evolution_mensuelle.csv : chaque mois, sorties, km, nouveaux blocs, communes et lieux, avec les cumuls.
- evolution_par_sport.csv : la même chose par sport. Un bloc parcouru en course puis à vélo compte comme
  nouveau pour chacun des deux sports. L'addition des sports dépasse donc le total.
- evolution_departements.csv : communes visitées par département, mois après mois.
- evolution_communes.csv : % exploré en fin de mois, pour les communes explorées à {pct} % ou plus.
- resume_annuel.csv : chaque année, au total puis par sport, avec l'écart sur l'année précédente.
- habitudes.csv : heures et jours de départ de vos sorties, année par année.

Détail
- sorties.csv : chaque sortie avec ses nouveaux blocs, les communes traversées et les lieux débloqués ce jour-là.

Cartes (GeoJSON, à ouvrir dans uMap, QGIS ou Google My Maps)
- blocs.geojson : vos blocs explorés, avec la date du premier passage et les sports.
- communes.geojson : le contour de vos communes avec leur pourcentage.
Ces deux cartes montrent précisément où vous vous déplacez, notamment autour de chez vous.
Réfléchissez avant de les partager.

Sources. Vos sorties Strava (via l'API Strava). Contours des communes et lieux © contributeurs OpenStreetMap (ODbL).
"""

@app.route('/api/export')
def api_export():
    """Télécharge un zip avec tous les chiffres de l'utilisateur connecté (et seulement les siens)."""
    if not session.get('access_token'): return jsonify({"error": "Login required"}), 401
    try:
        files = compute_export()
    except Exception as e:
        print(f"Export impossible : {e}")
        return jsonify({"error": "Export indisponible"}), 500
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        for name, content in files.items():
            z.writestr(name, content.encode('utf-8'))
    name = f"unlock-chiffres-{_today_paris():%Y-%m-%d}.zip"
    resp = make_response(buf.getvalue())
    resp.headers['Content-Type'] = 'application/zip'
    resp.headers['Content-Disposition'] = f'attachment; filename="{name}"'
    resp.headers['Cache-Control'] = 'no-store'
    return resp


@app.errorhandler(404)
def not_found(e):
    if request.path.startswith('/api/'):
        return jsonify({"error": "Introuvable"}), 404
    return render_template('error.html', code=404, title="Page introuvable",
                           message="Cette page n'existe pas ou n'est plus disponible."), 404

if __name__ == '__main__':
    app.run(debug=True, port=5000)