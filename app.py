import os
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
app.secret_key = os.getenv('SECRET_KEY', 'dev_secret_key_123')
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=os.getenv('VERCEL') is not None or os.getenv('SESSION_COOKIE_SECURE') == '1',
)

# ------------------------------------------------------------
# Fonctionnalités / infos légales
# ------------------------------------------------------------
# ⚠️ Classement désactivé par défaut : il affiche les données Strava d'autres
# utilisateurs et les agrège par commune, ce que la Strava API Policy (juin 2026)
# interdit (§2.3, §5.4, §5.7, §6.2). Ne le réactiver qu'après accord écrit de Strava.
ENABLE_LEADERBOARD = os.getenv('ENABLE_LEADERBOARD', 'false').lower() == 'true'

SITE_OWNER = os.getenv('SITE_OWNER', 'Théophile Sénéchal')
LINKEDIN_URL = 'https://www.linkedin.com/in/th%C3%A9ophile-s%C3%A9n%C3%A9chal-5119181a9/'
# Dons (facultatif) : lien de la page Ko-fi. Vide = rien ne s'affiche dans l'app.
KOFI_URL = os.getenv('KOFI_URL', '').strip()
if KOFI_URL and not KOFI_URL.startswith('https://'): KOFI_URL = ''
ASSET_VERSION = '2026.10.21'

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
    ('map', '/', '🗺️', 'Carte'),
    ('timelapse', '/timelapse', '⏱️', 'Timelapse'),
    ('story', '/story', '📸', 'Story'),
    ('stats', '/stats', '📊', 'Stats'),
]

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
        'legal_updated': '29 octobre 2026',
        'inactive_days': INACTIVE_DAYS,
        'is_admin': is_admin(),
        'carto_key': os.getenv('CARTO_API_KEY', ''),
        'my_athlete_id': session.get('athlete_id'),
    }

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
SPORT_TRANSLATIONS = {
    'Run': 'Course à pied', 'Ride': 'Vélo', 'Hike': 'Randonnée', 'Walk': 'Marche',
    'AlpineSki': 'Ski Alpin', 'BackcountrySki': 'Ski de Rando', 'VirtualRide': 'Vélo Virtuel',
    'VirtualRun': 'Course Virtuelle', 'GravelRide': 'Gravel', 'TrailRun': 'Trail',
    'E-BikeRide': 'Vélo Électrique', 'Velomobile': 'Vélomobile', 'NordicSki': 'Ski de Fond',
    'Snowshoe': 'Raquettes'
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
    """Blocs carrés (côté = grid_size_deg × 111,32 km) traversés par une trace."""
    cells = set()
    if not pts: return cells
    grid_m = grid_size_deg * M_PER_DEG
    prev_lat, prev_lon = pts[0]
    cells.add(cell_key(prev_lat, prev_lon, grid_size_deg))
    for curr_lat, curr_lon in pts[1:]:
        dy = (curr_lat - prev_lat) * M_PER_DEG
        dx = (curr_lon - prev_lon) * M_PER_DEG * math.cos(math.radians((curr_lat + prev_lat) / 2))
        dist_m = math.hypot(dx, dy)
        if dist_m > grid_m * 0.7:
            num_steps = int(dist_m / (grid_m * 0.5))
            for j in range(1, num_steps + 1):
                frac = j / (num_steps + 1)
                cells.add(cell_key(prev_lat + (curr_lat - prev_lat) * frac,
                                   prev_lon + (curr_lon - prev_lon) * frac, grid_size_deg))
        cells.add(cell_key(curr_lat, curr_lon, grid_size_deg))
        prev_lat, prev_lon = curr_lat, curr_lon
    return cells

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
    state = secrets.token_urlsafe(16)
    session['oauth_state'] = state
    params = {
        'client_id': CLIENT_ID, 'response_type': 'code', 'redirect_uri': REDIRECT_URI,
        'approval_prompt': 'auto', 'scope': 'activity:read_all', 'state': state,
    }
    return redirect("https://www.strava.com/oauth/authorize?" + urlencode(params))

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

    # 🚀 OPTIMISATION N°1: Utilisation d'un Dictionnaire au lieu d'une Liste
    # pour garder un index parfait avec grid_store_db
    activity_cache = {} 
    grid_store = {}
    grid_store_db = defaultdict(set)
    filtered_cell_acts = defaultdict(set)   # blocs des activités filtrées -> activités
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
            print(f"⚠️ Erreur Batch DB: {e}")

        visited = set(filtered_cell_acts.keys())
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
                pct = round(min(100.0, len(seen) / max(1, len(all_cells)) * 100), 2)
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
                    "stats": {"blocks": len(seen), "total_blocks": len(all_cells), "percent": pct, "activities": len(acts),
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

@app.errorhandler(404)
def not_found(e):
    if request.path.startswith('/api/'):
        return jsonify({"error": "Introuvable"}), 404
    return render_template('error.html', code=404, title="Page introuvable",
                           message="Cette page n'existe pas ou n'est plus disponible."), 404

if __name__ == '__main__':
    app.run(debug=True, port=5000)