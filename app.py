import os
import requests
import polyline
import json
from flask import Flask, redirect, request, jsonify, session, render_template, url_for
from dotenv import load_dotenv
from datetime import datetime
from shapely.geometry import Point, Polygon
from shapely.prepared import prep
from collections import defaultdict
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool  # <--- IMPORT INDISPENSABLE POUR VERCEL


# ============================================================
# 1. CONFIGURATION INITIALE
# ============================================================

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', 'dev_secret_key_123')


# ============================================================
# 2. CONFIGURATION DATABASE (SUPABASE)
# ============================================================

DB_URL = os.getenv('DATABASE_URL')


# ============================================================
# 3. CONFIGURATION STRAVA
# ============================================================

CLIENT_ID = os.getenv('STRAVA_CLIENT_ID')
CLIENT_SECRET = os.getenv('STRAVA_CLIENT_SECRET')
REDIRECT_URI = os.getenv(
    'STRAVA_REDIRECT_URI',
    'http://localhost:5000/callback'
)


# ============================================================
# 4. SPORTS
# ============================================================

SPORT_TRANSLATIONS = {
    'Run': 'Course à pied',
    'Ride': 'Vélo',
    'Hike': 'Randonnée',
    'Walk': 'Marche',
    'AlpineSki': 'Ski Alpin',
    'BackcountrySki': 'Ski de Rando',
    'VirtualRide': 'Vélo Virtuel',
    'VirtualRun': 'Course Virtuelle',
    'GravelRide': 'Gravel',
    'TrailRun': 'Trail',
    'E-BikeRide': 'Vélo Électrique',
    'Velomobile': 'Vélomobile',
    'NordicSki': 'Ski de Fond',
    'Snowshoe': 'Raquettes'
}

GPS_SPORTS = list(SPORT_TRANSLATIONS.keys())


# ============================================================
# 5. CACHES EN MÉMOIRE
# ============================================================

RAW_DATA_CACHE = {}
API_RESULT_CACHE = {}


# ============================================================
# 6. FONCTIONS UTILITAIRES
# ============================================================

def get_cells_from_polyline(pts, grid_size_deg):
    cells = set()

    if not pts:
        return cells

    prev_lat, prev_lon = pts[0]

    def to_key(lat, lon):
        return (
            round(round(lat / grid_size_deg) * grid_size_deg, 6),
            round(round(lon / grid_size_deg) * grid_size_deg, 6)
        )

    cells.add(to_key(prev_lat, prev_lon))

    for i in range(1, len(pts)):
        curr_lat, curr_lon = pts[i]

        dist = (
            (curr_lat - prev_lat) ** 2 +
            (curr_lon - prev_lon) ** 2
        ) ** 0.5

        if dist > grid_size_deg * 0.7:
            num_steps = int(dist / (grid_size_deg * 0.5))

            for j in range(1, num_steps + 1):
                frac = j / (num_steps + 1)

                cells.add(
                    to_key(
                        prev_lat + (curr_lat - prev_lat) * frac,
                        prev_lon + (curr_lon - prev_lon) * frac
                    )
                )

        cells.add(to_key(curr_lat, curr_lon))

        prev_lat, prev_lon = curr_lat, curr_lon

    return cells


def get_current_iso_period():
    """
    Retourne la période ISO courante sous la forme YYYY-Wxx.
    """
    current_year, current_week, _ = datetime.now().isocalendar()
    return f"{current_year}-W{current_week:02d}"


def get_current_week_display():
    """
    Retourne la semaine courante sous la forme :
    DD/MM/YYYY - DD/MM/YYYY

    Semaine ISO : lundi -> dimanche.
    """
    today = datetime.now()
    iso_year, iso_week, _ = today.isocalendar()

    week_start = datetime.fromisocalendar(
        iso_year,
        iso_week,
        1
    )

    week_end = datetime.fromisocalendar(
        iso_year,
        iso_week,
        7
    )

    return f"{week_start:%d/%m/%Y} - {week_end:%d/%m/%Y}"


def get_strava_activities_cached(token):
    """
    Charge les activités Strava une seule fois par token.
    """

    if token in RAW_DATA_CACHE:
        return RAW_DATA_CACHE[token]

    all_activities = []

    headers = {
        'Authorization': f'Bearer {token}'
    }

    page = 1

    while True:
        try:
            r = requests.get(
                "https://www.strava.com/api/v3/athlete/activities",
                headers=headers,
                params={
                    'per_page': 200,
                    'page': page
                },
                timeout=10
            )

            if r.status_code != 200:
                break

            data = r.json()

            if not data:
                break

            all_activities.extend(data)

            page += 1

            if page > 10:
                break

        except Exception:
            break

    cleaned_data = []

    for act in all_activities:

        if (
            act.get('type') in GPS_SPORTS
            and act.get('map', {}).get('summary_polyline')
        ):
            cleaned_data.append({
                'id': act.get('id'),
                'type': act['type'],
                'start_date_local': act['start_date_local'],
                'polyline': act['map']['summary_polyline'],
                'distance': act.get('distance', 0)
            })

    RAW_DATA_CACHE[token] = cleaned_data

    return cleaned_data


# ============================================================
# 7. ROUTES STANDARD
# ============================================================

@app.route('/')
def index():

    if 'access_token' not in session:
        return redirect(url_for('login_page'))

    return render_template('index.html')


@app.route('/login')
def login_page():
    return render_template('login.html')


@app.route('/auth')
def auth():

    return redirect(
        f"https://www.strava.com/oauth/authorize"
        f"?client_id={CLIENT_ID}"
        f"&response_type=code"
        f"&redirect_uri={REDIRECT_URI}"
        f"&approval_prompt=auto"
        f"&scope=activity:read_all"
    )


@app.route('/logout')
def logout():

    token = session.get('access_token')

    if token:

        try:
            requests.post(
                "https://www.strava.com/api/v3/oauth/deauthorize",
                headers={
                    'Authorization': f'Bearer {token}'
                },
                timeout=5
            )

        except Exception as e:
            print(
                f"Erreur lors de la révocation Strava : {e}"
            )

        RAW_DATA_CACHE.pop(token, None)
        API_RESULT_CACHE.pop(token, None)

    session.clear()

    return redirect(url_for('login_page'))


@app.route('/callback')
def callback():

    code = request.args.get('code')

    res = requests.post(
        "https://www.strava.com/oauth/token",
        data={
            'client_id': CLIENT_ID,
            'client_secret': CLIENT_SECRET,
            'code': code,
            'grant_type': 'authorization_code'
        }
    )

    if res.status_code == 200:

        data = res.json()

        token = data.get('access_token')

        athlete_info = data.get('athlete', {})

        athlete_id = athlete_info.get('id')

        firstname = athlete_info.get(
            'firstname',
            ''
        )

        lastname = athlete_info.get(
            'lastname',
            ''
        )

        athlete_name = f"{firstname} {lastname}".strip()

        session['access_token'] = token

        if athlete_id and token and DB_URL:

            try:

                engine = create_engine(
                    DB_URL,
                    poolclass=NullPool
                )

                with engine.connect() as conn:

                    query = text("""
                        INSERT INTO strava_users (
                            athlete_id,
                            access_token,
                            login_count,
                            first_login_date,
                            last_login_date,
                            athlete_name
                        )
                        VALUES (
                            :ath_id,
                            :token,
                            1,
                            CURRENT_TIMESTAMP,
                            CURRENT_TIMESTAMP,
                            :ath_name
                        )
                        ON CONFLICT (athlete_id) DO UPDATE
                        SET access_token = EXCLUDED.access_token,
                            login_count = strava_users.login_count + 1,
                            last_login_date = CURRENT_TIMESTAMP,
                            athlete_name = EXCLUDED.athlete_name;
                    """)

                    conn.execute(
                        query,
                        {
                            "ath_id": athlete_id,
                            "token": token,
                            "ath_name": athlete_name
                        }
                    )

                    conn.commit()

            except Exception as e:

                print(
                    f"Erreur d'enregistrement utilisateur: {e}"
                )

        return redirect('/')

    return "Erreur lors de l'authentification avec Strava"


@app.route('/stats')
def stats_page():

    if 'access_token' not in session:
        return redirect(url_for('login_page'))

    return render_template('stats.html')


@app.route('/story')
def story_page():

    if 'access_token' not in session:
        return redirect(url_for('login_page'))

    return render_template('story.html')


@app.route('/timelapse')
def timelapse_page():

    if 'access_token' not in session:
        return redirect(url_for('login_page'))

    return render_template('timelapse.html')


# ============================================================
# 8. API : HISTORIQUE DES STATS
# ============================================================

@app.route('/api/stats_history')
def get_stats_history():

    token = session.get('access_token')

    if not token:
        return jsonify({
            "error": "Login required"
        }), 401

    grid_meters = int(
        request.args.get(
            'grid_size',
            100
        )
    )

    sel_year = request.args.get(
        'year',
        'all'
    )

    sel_sport = request.args.get(
        'sport_type',
        'all'
    )

    cache_key = (
        f"stats_{grid_meters}_"
        f"{sel_year}_"
        f"{sel_sport}"
    )

    if token not in API_RESULT_CACHE:
        API_RESULT_CACHE[token] = {}

    if cache_key in API_RESULT_CACHE[token]:

        return jsonify(
            API_RESULT_CACHE[token][cache_key]
        )

    activities = get_strava_activities_cached(token)

    grid_size_deg = grid_meters / 111320

    activities.sort(
        key=lambda x: x['start_date_local']
    )

    monthly_data = {}

    global_seen = set()

    available_years = set()

    available_sports = set()

    total_blocks = 0

    for act in activities:

        dt = datetime.strptime(
            act['start_date_local'],
            "%Y-%m-%dT%H:%M:%SZ"
        )

        y_str = str(dt.year)

        m_key = dt.strftime(
            "%Y-%m"
        )

        sport = act['type']

        available_years.add(y_str)

        available_sports.add(sport)

        if (
            sel_year != 'all'
            and y_str != sel_year
        ):
            continue

        if (
            sel_sport != 'all'
            and sport != sel_sport
        ):
            continue

        if m_key not in monthly_data:
            monthly_data[m_key] = {
                'new': 0,
                'routine': 0
            }

        pts = polyline.decode(
            act['polyline']
        )

        blocks = get_cells_from_polyline(
            pts,
            grid_size_deg
        )

        for b in blocks:

            if b not in global_seen:

                global_seen.add(b)

                monthly_data[m_key]['new'] += 1

                total_blocks += 1

            else:

                monthly_data[m_key]['routine'] += 1

    labels = sorted(
        monthly_data.keys()
    )

    conquest = []
    explore = []
    routine = []

    running = 0

    for m in labels:

        running += monthly_data[m]['new']

        conquest.append(running)

        explore.append(
            monthly_data[m]['new']
        )

        routine.append(
            monthly_data[m]['routine']
        )

    result = {
        "labels": labels,
        "conquest": conquest,
        "exploration": explore,
        "routine": routine,
        "total_blocks": total_blocks,
        "available_years": sorted(
            list(available_years),
            reverse=True
        ),
        "available_sports": sorted(
            list(available_sports)
        )
    }

    API_RESULT_CACHE[token][cache_key] = result

    return jsonify(result)


# ============================================================
# 9. API ACTIVITÉS / CARTE / COMMUNES
# ============================================================

@app.route('/api/activities')
def get_activities_route():

    token = session.get('access_token')

    if not token:
        return jsonify({
            "error": "Login required"
        }), 401

    athlete_id = None

    # --------------------------------------------------------
    # Récupération de l'athlete_id
    # --------------------------------------------------------

    if DB_URL:

        try:

            engine = create_engine(
                DB_URL,
                poolclass=NullPool
            )

            with engine.connect() as conn:

                res = conn.execute(
                    text("""
                        UPDATE strava_users
                        SET last_login_date = CURRENT_TIMESTAMP
                        WHERE access_token = :token
                        RETURNING athlete_id
                    """),
                    {
                        "token": token
                    }
                ).fetchone()

                if res:
                    athlete_id = res[0]

                conn.commit()

        except Exception as e:

            print(
                f"Erreur màj silencieuse: {e}"
            )

    # --------------------------------------------------------
    # Filtres
    # --------------------------------------------------------

    sel_year = request.args.get(
        'year',
        'all'
    )

    sel_sport = request.args.get(
        'sport_type',
        'all'
    )

    grid_meters = int(
        request.args.get(
            'grid_size',
            100
        )
    )

    cache_key = (
        f"act_{grid_meters}_"
        f"{sel_year}_"
        f"{sel_sport}"
    )

    if token not in API_RESULT_CACHE:
        API_RESULT_CACHE[token] = {}

    if cache_key in API_RESULT_CACHE[token]:

        return jsonify(
            API_RESULT_CACHE[token][cache_key]
        )

    activities = get_strava_activities_cached(token)

    grid_size_deg = grid_meters / 111320

    data = {
        "coords": [],
        "grid_cells": [],
        "grid_size_used": grid_size_deg,
        "available_years": set(),
        "available_sports": {},
        "top_municipalities": [],
        "stats": {
            "total_distance": 0,
            "activity_count": 0,
            "cells_conquered": 0
        }
    }

    # --------------------------------------------------------
    # IMPORTANT :
    # On conserve le fonctionnement de ta version qui
    # fonctionnait avant les modifications.
    # --------------------------------------------------------

    grid_store = {}

    for act in activities:

        dt = datetime.strptime(
            act['start_date_local'],
            "%Y-%m-%dT%H:%M:%SZ"
        )

        y_str = str(dt.year)

        sport = act['type']

        data["available_years"].add(
            y_str
        )

        if sport not in data["available_sports"]:

            data["available_sports"][sport] = (
                SPORT_TRANSLATIONS.get(
                    sport,
                    sport
                )
            )

        # ----------------------------------------------------
        # On ne met dans grid_store QUE les activités filtrées
        # ----------------------------------------------------

        if (
            sel_year == 'all'
            or sel_year == y_str
        ) and (
            sel_sport == 'all'
            or sel_sport == sport
        ):

            pts = polyline.decode(
                act['polyline']
            )

            data["coords"].append(pts)

            blocks = get_cells_from_polyline(
                pts,
                grid_size_deg
            )

            act_ym = dt.strftime(
                "%Y-%m"
            )

            for b in blocks:

                if b not in grid_store:

                    grid_store[b] = {
                        'cnt': 0,
                        'first': act_ym,
                        'last': act_ym
                    }

                grid_store[b]['cnt'] += 1

                if (
                    act_ym
                    < grid_store[b]['first']
                ):
                    grid_store[b]['first'] = act_ym

                if (
                    act_ym
                    > grid_store[b]['last']
                ):
                    grid_store[b]['last'] = act_ym

            data["stats"]["total_distance"] += (
                act['distance'] / 1000
            )

            data["stats"]["activity_count"] += 1

    # --------------------------------------------------------
    # Cellules de la carte
    # --------------------------------------------------------

    data["grid_cells"] = [
        [
            k[0],
            k[1],
            v['cnt'],
            v['first'],
            v['last']
        ]
        for k, v in grid_store.items()
    ]

    data["stats"]["cells_conquered"] = len(
        grid_store
    )

    data["available_years"] = sorted(
        list(data["available_years"]),
        reverse=True
    )

    data["available_sports"] = dict(
        sorted(
            data["available_sports"].items(),
            key=lambda x: x[1]
        )
    )

    # ========================================================
    # CALCUL DES VILLES
    # ========================================================

    if grid_store and DB_URL:

        identified_cities = {}

        # ----------------------------------------------------
        # 1. Création des sondes
        # ----------------------------------------------------

        probe_points = set()

        for lat, lon in grid_store.keys():

            probe_points.add(
                (
                    round(lat, 2),
                    round(lon, 2)
                )
            )

        probe_list = list(
            probe_points
        )

        batch_size = 50

        # ----------------------------------------------------
        # 2. Recherche des communes
        # ----------------------------------------------------

        try:

            engine = create_engine(
                DB_URL,
                poolclass=NullPool
            )

            with engine.connect() as conn:

                for i in range(
                    0,
                    len(probe_list),
                    batch_size
                ):

                    batch = probe_list[
                        i:i + batch_size
                    ]

                    points_str = ", ".join(
                        [
                            f"{lon} {lat}"
                            for lat, lon in batch
                        ]
                    )

                    wkt_multipoint = (
                        f"MULTIPOINT({points_str})"
                    )

                    query = text("""
                        SELECT DISTINCT
                               nom_commune,
                               ST_Area(
                                   geometry::geography
                               ) as area_m2,
                               ST_AsGeoJSON(
                                   geometry
                               ) as outline
                        FROM communes
                        WHERE ST_Intersects(
                            geometry,
                            ST_GeomFromText(
                                :wkt,
                                4326
                            )
                        )
                    """)

                    result_proxy = conn.execute(
                        query,
                        {
                            "wkt": wkt_multipoint
                        }
                    )

                    # ------------------------------------------------
                    # IMPORTANT :
                    # C'est la logique de ton ancienne version
                    # qui fonctionnait pour les contours.
                    # ------------------------------------------------

                    for row in result_proxy:

                        if (
                            row.nom_commune
                            in identified_cities
                        ):
                            continue

                        try:

                            geojson_geom = json.loads(
                                row.outline
                            )

                            inverted_outline = []

                            # ----------------------------------------
                            # POLYGON
                            # ----------------------------------------

                            if (
                                geojson_geom['type']
                                == 'Polygon'
                            ):

                                inverted_outline = [
                                    [p[1], p[0]]
                                    for p in geojson_geom[
                                        'coordinates'
                                    ][0]
                                ]

                            # ----------------------------------------
                            # MULTIPOLYGON
                            # ----------------------------------------

                            elif (
                                geojson_geom['type']
                                == 'MultiPolygon'
                            ):

                                inverted_outline = [
                                    [p[1], p[0]]
                                    for p in geojson_geom[
                                        'coordinates'
                                    ][0][0]
                                ]

                            else:
                                continue

                            # ----------------------------------------
                            # STOCKAGE
                            # ----------------------------------------

                            identified_cities[
                                row.nom_commune
                            ] = {
                                "name": row.nom_commune,
                                "area_m2": row.area_m2,
                                "outline": inverted_outline,
                                "poly_obj": Polygon(
                                    inverted_outline
                                )
                            }

                        except Exception as e:

                            print(
                                f"Erreur traitement commune "
                                f"{row.nom_commune}: {e}"
                            )

                    if len(
                        identified_cities
                    ) >= 70:
                        break

        except Exception as e:

            print(
                f"⚠️ Erreur Batch DB: {e}"
            )

        # ========================================================
        # 3. CALCUL DES STATISTIQUES DES VILLES
        # ========================================================

        final_cities_list = []

        scores_to_save = []

        for city_name, city_data in identified_cities.items():

            try:

                poly_geom = city_data[
                    'poly_obj'
                ]

                prepared_poly = prep(
                    poly_geom
                )

                min_lat, min_lon, max_lat, max_lon = (
                    poly_geom.bounds
                )

                count_inside = 0

                for (
                    clat,
                    clon
                ) in grid_store.keys():

                    if (
                        min_lat <= clat <= max_lat
                        and
                        min_lon <= clon <= max_lon
                    ):

                        # --------------------------------------------
                        # CONSERVÉ TEL QU'AVANT
                        # --------------------------------------------

                        if prepared_poly.contains(
                            Point(clat, clon)
                        ):

                            count_inside += 1

                if count_inside <= 0:
                    continue

                # ----------------------------------------------------
                # Statistiques ville
                # ----------------------------------------------------

                area_conquered_m2 = (
                    count_inside
                    * (grid_meters ** 2)
                )

                pct = (
                    area_conquered_m2
                    / city_data['area_m2']
                ) * 100

                final_cities_list.append({
                    "name": city_name,
                    "outline": city_data[
                        'outline'
                    ],
                    "stats": {
                        "blocks": count_inside,
                        "percent": round(
                            min(pct, 100),
                            2
                        )
                    }
                })

                # ====================================================
                # CALCUL MULTIDIMENSIONNEL POUR CITY_SCORES
                # ====================================================

                stats_dim = defaultdict(
                    lambda: defaultdict(
                        lambda: {
                            'blocks': set(),
                            'acts': set()
                        }
                    )
                )

                for act in activities:

                    dt_act = datetime.strptime(
                        act['start_date_local'],
                        "%Y-%m-%dT%H:%M:%SZ"
                    )

                    sport = act['type']

                    # --------------------------------------------
                    # ACTIVITÉ FILTRÉE
                    # --------------------------------------------

                    if (
                        sel_year != 'all'
                        and str(dt_act.year)
                        != sel_year
                    ):
                        continue

                    if (
                        sel_sport != 'all'
                        and sport != sel_sport
                    ):
                        continue

                    act_pts = polyline.decode(
                        act['polyline']
                    )

                    act_blocks_all = get_cells_from_polyline(
                        act_pts,
                        grid_size_deg
                    )

                    # ------------------------------------------------
                    # On reprend les blocs réellement présents
                    # dans la commune
                    # ------------------------------------------------

                    city_block_coords = set()

                    for (
                        clat,
                        clon
                    ) in grid_store.keys():

                        if (
                            min_lat <= clat <= max_lat
                            and
                            min_lon <= clon <= max_lon
                        ):

                            if prepared_poly.contains(
                                Point(clat, clon)
                            ):

                                city_block_coords.add(
                                    (
                                        clat,
                                        clon
                                    )
                                )

                    act_blocks_in = (
                        act_blocks_all
                        .intersection(
                            city_block_coords
                        )
                    )

                    if not act_blocks_in:
                        continue

                    # ------------------------------------------------
                    # Période ISO de l'activité
                    # ------------------------------------------------

                    act_year, act_week, _ = (
                        dt_act.isocalendar()
                    )

                    act_period = (
                        f"{act_year}-W{act_week:02d}"
                    )

                    current_period = (
                        get_current_iso_period()
                    )

                    is_current = (
                        act_period
                        == current_period
                    )

                    # ------------------------------------------------
                    # Dimensions disponibles
                    # ------------------------------------------------

                    keys = [
                        ('all', 'all'),
                        (sport, 'all')
                    ]

                    if is_current:

                        keys.extend([
                            (
                                'all',
                                current_period
                            ),
                            (
                                sport,
                                current_period
                            )
                        ])

                    # ------------------------------------------------
                    # Ajout des blocs / activités
                    # ------------------------------------------------

                    for (
                        k_sp,
                        k_per
                    ) in keys:

                        stats_dim[
                            k_sp
                        ][
                            k_per
                        ][
                            'blocks'
                        ].update(
                            act_blocks_in
                        )

                        # ID de l'activité.
                        # Ici on utilise l'ID Strava lorsque disponible,
                        # ce qui évite de dépendre uniquement de l'index.
                        stats_dim[
                            k_sp
                        ][
                            k_per
                        ][
                            'acts'
                        ].add(
                            act.get('id')
                        )

                # ====================================================
                # ENREGISTREMENT DES SCORES
                # ====================================================

                for sp, periods in stats_dim.items():

                    for per, s_data in periods.items():

                        b_cnt = len(
                            s_data['blocks']
                        )

                        p_val = round(
                            min(
                                (
                                    (
                                        b_cnt
                                        * (grid_meters ** 2)
                                    )
                                    / city_data['area_m2']
                                )
                                * 100,
                                100
                            ),
                            2
                        )

                        scores_to_save.append({
                            "ath_id": athlete_id,
                            "c_name": city_name,
                            "g_size": grid_meters,
                            "sport": sp,
                            "period": per,
                            "b_count": b_cnt,
                            "pct": p_val,
                            "act_count": len(
                                s_data['acts']
                            )
                        })

            except Exception as e:

                print(
                    f"Erreur calcul stats ville "
                    f"{city_name}: {e}"
                )

        # --------------------------------------------------------
        # Top communes local
        # --------------------------------------------------------

        data["top_municipalities"] = sorted(
            final_cities_list,
            key=lambda x:
                x['stats']['blocks'],
            reverse=True
        )

        # ========================================================
        # SAUVEGARDE CITY_SCORES
        # ========================================================

        if scores_to_save:

            try:

                engine = create_engine(
                    DB_URL,
                    poolclass=NullPool
                )

                with engine.connect() as conn:

                    upsert_query = text("""
                        INSERT INTO city_scores (
                            athlete_id,
                            city_name,
                            grid_size,
                            sport,
                            period,
                            blocks_count,
                            percent,
                            activities_count,
                            last_updated
                        )
                        VALUES (
                            :ath_id,
                            :c_name,
                            :g_size,
                            :sport,
                            :period,
                            :b_count,
                            :pct,
                            :act_count,
                            CURRENT_TIMESTAMP
                        )
                        ON CONFLICT (
                            athlete_id,
                            city_name,
                            grid_size,
                            sport,
                            period
                        )
                        DO UPDATE SET
                            blocks_count =
                                EXCLUDED.blocks_count,
                            percent =
                                EXCLUDED.percent,
                            activities_count =
                                EXCLUDED.activities_count,
                            last_updated =
                                CURRENT_TIMESTAMP;
                    """)

                    for score in scores_to_save:

                        conn.execute(
                            upsert_query,
                            score
                        )

                    conn.commit()

            except Exception as e:

                print(
                    f"Erreur sauvegarde scores: {e}"
                )

    API_RESULT_CACHE[token][cache_key] = data

    return jsonify(data)


# ============================================================
# 10. KEEP-ALIVE
# ============================================================

@app.route('/api/keep-alive')
def keep_alive():

    if not DB_URL:

        return jsonify({
            "error": "Database URL non configurée"
        }), 500

    try:

        engine = create_engine(
            DB_URL,
            poolclass=NullPool
        )

        with engine.connect() as conn:

            conn.execute(
                text("SELECT 1")
            )

        return jsonify({
            "status": "Supabase is awake!"
        }), 200

    except Exception as e:

        print(
            f"Erreur Keep-Alive: {e}"
        )

        return jsonify({
            "error": str(e)
        }), 500


# ============================================================
# 11. NETTOYAGE DES UTILISATEURS INACTIFS
# ============================================================

@app.route('/api/cleanup-users')
def cleanup_users():

    cron_secret = os.getenv(
        'CRON_SECRET'
    )

    auth_header = request.headers.get(
        'Authorization'
    )

    if auth_header != f"Bearer {cron_secret}":

        return jsonify({
            "error": "Non autorisé"
        }), 401

    if not DB_URL:

        return jsonify({
            "error": "Database URL non configurée"
        }), 500

    try:

        engine = create_engine(
            DB_URL,
            poolclass=NullPool
        )

        revoked_count = 0

        with engine.connect() as conn:

            query = text("""
                SELECT
                    athlete_id,
                    access_token
                FROM strava_users
                WHERE last_login_date <
                      NOW() - INTERVAL '60 days'
            """)

            inactive_users = conn.execute(
                query
            ).fetchall()

            for row in inactive_users:

                try:

                    requests.post(
                        "https://www.strava.com/api/v3/oauth/deauthorize",
                        headers={
                            'Authorization':
                                f'Bearer {row.access_token}'
                        },
                        timeout=5
                    )

                except Exception:
                    pass

                conn.execute(
                    text("""
                        DELETE FROM strava_users
                        WHERE athlete_id = :id
                    """),
                    {
                        "id": row.athlete_id
                    }
                )

                conn.commit()

                revoked_count += 1

        return jsonify({
            "status": "Nettoyage terminé",
            "comptes_supprimes":
                revoked_count
        }), 200

    except Exception as e:

        return jsonify({
            "error": str(e)
        }), 500


# ============================================================
# 12. LEADERBOARD COMMUNAUTAIRE
# ============================================================

@app.route('/leaderboard')
def leaderboard_page():

    if 'access_token' not in session:

        return redirect(
            url_for('login_page')
        )

    return render_template(
        'leaderboard.html'
    )


# ============================================================
# 13. LEADERBOARD GLOBAL
# ============================================================

@app.route('/api/global_stats_leaderboard')
def get_global_stats_leaderboard():

    token = session.get(
        'access_token'
    )

    if not token:

        return jsonify({
            "error": "Login required"
        }), 401

    grid_size = request.args.get(
        'grid_size',
        250,
        type=int
    )

    sport_filter = request.args.get(
        'sport',
        'all'
    )

    period_filter = request.args.get(
        'period',
        'all'
    )

    # --------------------------------------------------------
    # Gestion semaine en cours
    # --------------------------------------------------------

    period_display = None

    if period_filter == 'current_week':

        period_filter = (
            get_current_iso_period()
        )

        period_display = (
            get_current_week_display()
        )

    if not DB_URL:

        return jsonify({
            "error":
                "Base de données non configurée"
        }), 500

    try:

        engine = create_engine(
            DB_URL,
            poolclass=NullPool
        )

        with engine.connect() as conn:

            # ------------------------------------------------
            # IMPORTANT :
            # users_count = nombre d'athlètes
            #
            # total_blocks = somme des blocs explorés
            # par les athlètes de la ville
            #
            # Tri :
            # 1. athlètes DESC
            # 2. blocs cumulés DESC
            # 3. nom ASC uniquement si tout est égal
            # ------------------------------------------------

            query = text("""
                SELECT
                    city_name,
                    COUNT(DISTINCT athlete_id)
                        AS users_count,
                    SUM(blocks_count)
                        AS total_blocks,
                    SUM(activities_count)
                        AS total_acts
                FROM city_scores
                WHERE grid_size = :grid_size
                  AND sport = :sport
                  AND period = :period
                GROUP BY city_name
                ORDER BY
                    users_count DESC,
                    total_blocks DESC,
                    city_name ASC
            """)

            res = conn.execute(
                query,
                {
                    "grid_size":
                        grid_size,

                    "sport":
                        sport_filter,

                    "period":
                        period_filter
                }
            ).fetchall()

            cities_data = []

            for row in res:

                cities_data.append({
                    "name":
                        row.city_name,

                    "users":
                        int(row.users_count)
                        if row.users_count
                        else 0,

                    "blocks":
                        int(row.total_blocks)
                        if row.total_blocks
                        else 0,

                    "activities":
                        int(row.total_acts)
                        if row.total_acts
                        else 0
                })

            # ------------------------------------------------
            # Sports disponibles
            # ------------------------------------------------

            sport_query = text("""
                SELECT DISTINCT sport
                FROM city_scores
                WHERE sport != 'all'
                ORDER BY sport
            """)

            sports_res = conn.execute(
                sport_query
            ).fetchall()

            available_sports = [
                r.sport
                for r in sports_res
            ]

            # ------------------------------------------------
            # Réponse
            # ------------------------------------------------

            return jsonify({

                "cities":
                    cities_data,

                "available_sports":
                    available_sports,

                "period_display":
                    period_display

            }), 200

    except Exception as e:

        print(
            f"Erreur global_stats_leaderboard: {e}"
        )

        return jsonify({
            "error": str(e)
        }), 500


# ============================================================
# 14. LEADERBOARD D'UNE VILLE
# ============================================================

@app.route('/api/city_leaderboard')
def get_city_leaderboard():

    token = session.get(
        'access_token'
    )

    if not token:

        return jsonify({
            "error": "Login required"
        }), 401

    city = request.args.get(
        'city'
    )

    grid_size = request.args.get(
        'grid_size',
        250,
        type=int
    )

    sport_filter = request.args.get(
        'sport',
        'all'
    )

    period_filter = request.args.get(
        'period',
        'all'
    )

    # --------------------------------------------------------
    # Gestion semaine en cours
    # --------------------------------------------------------

    period_display = None

    if period_filter == 'current_week':

        period_filter = (
            get_current_iso_period()
        )

        period_display = (
            get_current_week_display()
        )

    if not city:

        return jsonify({
            "error":
                "Nom de la ville manquant"
        }), 400

    if not DB_URL:

        return jsonify({
            "error":
                "Base de données non configurée"
        }), 500

    try:

        engine = create_engine(
            DB_URL,
            poolclass=NullPool
        )

        with engine.connect() as conn:

            # ------------------------------------------------
            # KPIs de la ville
            # ------------------------------------------------

            stats_query = text("""
                SELECT
                    COUNT(DISTINCT athlete_id)
                        AS total_users,
                    SUM(activities_count)
                        AS total_activities
                FROM city_scores
                WHERE city_name = :city
                  AND grid_size = :grid_size
                  AND sport = :sport
                  AND period = :period
            """)

            stats_res = conn.execute(
                stats_query,
                {
                    "city":
                        city,

                    "grid_size":
                        grid_size,

                    "sport":
                        sport_filter,

                    "period":
                        period_filter
                }
            ).fetchone()

            total_users = (
                stats_res.total_users
                if stats_res
                and stats_res.total_users
                else 0
            )

            total_activities = (
                int(
                    stats_res.total_activities
                )
                if stats_res
                and stats_res.total_activities
                else 0
            )

            # ------------------------------------------------
            # Classement des athlètes de la ville
            # ------------------------------------------------

            board_query = text("""
                SELECT
                    u.athlete_name,
                    c.percent,
                    c.blocks_count,
                    c.activities_count
                FROM city_scores c
                JOIN strava_users u
                    ON c.athlete_id =
                       u.athlete_id
                WHERE c.city_name = :city
                  AND c.grid_size = :grid_size
                  AND c.sport = :sport
                  AND c.period = :period
                ORDER BY
                    c.percent DESC,
                    c.blocks_count DESC,
                    u.athlete_name ASC
            """)

            board_res = conn.execute(
                board_query,
                {
                    "city":
                        city,

                    "grid_size":
                        grid_size,

                    "sport":
                        sport_filter,

                    "period":
                        period_filter
                }
            ).fetchall()

            leaderboard = []

            for row in board_res:

                name = (
                    row.athlete_name
                    if row.athlete_name
                    else "Explorateur Anonyme"
                )

                leaderboard.append({

                    "name":
                        name,

                    "percent":
                        row.percent,

                    "blocks":
                        row.blocks_count,

                    "activities":
                        row.activities_count

                })

            # ------------------------------------------------
            # Réponse
            # ------------------------------------------------

            return jsonify({

                "city":
                    city,

                "grid_size":
                    grid_size,

                "global_stats": {

                    "total_explorers":
                        int(total_users),

                    "total_activities":
                        total_activities
                },

                "period_display":
                    period_display,

                "leaderboard":
                    leaderboard

            }), 200

    except Exception as e:

        print(
            f"Erreur city_leaderboard: {e}"
        )

        return jsonify({
            "error": str(e)
        }), 500


# ============================================================
# 15. LANCEMENT
# ============================================================

if __name__ == '__main__':

    app.run(
        debug=True,
        port=5000
    )

