import os
import requests
import polyline
import json
from flask import Flask, redirect, request, jsonify, session, render_template, url_for
from dotenv import load_dotenv
from datetime import datetime
from shapely.geometry import Point, Polygon, shape
from shapely.prepared import prep
from sqlalchemy import create_engine, text

# 1. Configuration initiale
load_dotenv()
app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', 'dev_secret_key_123')

# --- CONFIGURATION DATABASE (SUPABASE) ---
# Sur Vercel, cette variable doit être définie dans les Settings
# ON FORCE L'ADRESSE ICI DIRECTEMENT
DB_URL = "postgresql://postgres:R61KlIcfrFKqADHK@db.jsfouzzuekmdyegslhmf.supabase.co:5432/postgres"

# Configuration Strava
CLIENT_ID = os.getenv('STRAVA_CLIENT_ID')
CLIENT_SECRET = os.getenv('STRAVA_CLIENT_SECRET')
REDIRECT_URI = os.getenv('STRAVA_REDIRECT_URI', 'http://localhost:5000/callback')

SPORT_TRANSLATIONS = {
    'Run': 'Course à pied', 'Ride': 'Vélo', 'Hike': 'Randonnée', 'Walk': 'Marche',
    'AlpineSki': 'Ski Alpin', 'BackcountrySki': 'Ski de Rando', 'VirtualRide': 'Vélo Virtuel',
    'VirtualRun': 'Course Virtuelle', 'GravelRide': 'Gravel', 'TrailRun': 'Trail',
    'E-BikeRide': 'Vélo Électrique', 'Velomobile': 'Vélomobile', 'NordicSki': 'Ski de Fond',
    'Snowshoe': 'Raquettes'
}
GPS_SPORTS = list(SPORT_TRANSLATIONS.keys())

# --- CACHES EN MÉMOIRE (RAM uniquement, reset à chaque redémarrage sur Vercel) ---
# Sur Vercel, le serveur redémarre souvent, donc on utilise la DB pour le durable.
# Ces caches servent juste à accélérer la navigation immédiate de l'utilisateur.
RAW_DATA_CACHE = {}
API_RESULT_CACHE = {}

# --- FONCTIONS UTILITAIRES BDD ---

def get_city_from_db(lat, lon):
    """
    Interroge Supabase pour trouver la ville correspondant au point GPS.
    Retourne le nom, la surface et le contour (GeoJSON).
    """
    if not DB_URL:
        return None

    try:
        engine = create_engine(DB_URL)
        with engine.connect() as conn:
            # On demande : Nom, Surface, et Géométrie (au format JSON)
            # ST_Area retourne des m², conversion auto gérée par PostGIS si projection correcte
            # Ici on utilise ::geography pour avoir des mètres carrés précis
            query = text("""
                SELECT 
                    nom_commune, 
                    ST_Area(geometry::geography) as area_m2, 
                    ST_AsGeoJSON(geometry) as outline
                FROM communes
                WHERE ST_Contains(
                    geometry, 
                    ST_SetSRID(ST_Point(:lon, :lat), 4326)
                )
                LIMIT 1;
            """)
            
            result = conn.execute(query, {"lat": lat, "lon": lon}).fetchone()
            
            if result:
                # PostGIS renvoie le GeoJSON sous forme de string, on le parse
                geojson_geom = json.loads(result.outline)
                
                # IMPORTANT : PostGIS renvoie [Lon, Lat], mais ton code et Leaflet veulent souvent [Lat, Lon]
                # On doit inverser les coordonnées pour ton frontend
                if geojson_geom['type'] == 'Polygon':
                    raw_coords = geojson_geom['coordinates'][0]
                    inverted_outline = [[p[1], p[0]] for p in raw_coords]
                elif geojson_geom['type'] == 'MultiPolygon':
                    # On prend le plus grand polygone (souvent le principal)
                    raw_coords = geojson_geom['coordinates'][0][0]
                    inverted_outline = [[p[1], p[0]] for p in raw_coords]
                else:
                    inverted_outline = []

                return {
                    "name": result.nom_commune,
                    "area_m2": result.area_m2,
                    "outline": inverted_outline
                }
            return None
    except Exception as e:
        print(f"⚠️ Erreur DB: {e}")
        return None

def get_cells_from_polyline(pts, grid_size_deg):
    cells = set()
    if not pts: return cells
    prev_lat, prev_lon = pts[0]
    
    def to_key(lat, lon):
        return (round(round(lat/grid_size_deg)*grid_size_deg, 6), 
                round(round(lon/grid_size_deg)*grid_size_deg, 6))

    cells.add(to_key(prev_lat, prev_lon))

    for i in range(1, len(pts)):
        curr_lat, curr_lon = pts[i]
        dist = ((curr_lat - prev_lat)**2 + (curr_lon - prev_lon)**2)**0.5
        if dist > grid_size_deg * 0.7:
            num_steps = int(dist / (grid_size_deg * 0.5))
            for j in range(1, num_steps + 1):
                frac = j / (num_steps + 1)
                cells.add(to_key(prev_lat + (curr_lat - prev_lat) * frac, prev_lon + (curr_lon - prev_lon) * frac))
        cells.add(to_key(curr_lat, curr_lon))
        prev_lat, prev_lon = curr_lat, curr_lon
    return cells

def get_strava_activities_cached(token):
    """Charge les activités une seule fois."""
    if token in RAW_DATA_CACHE: return RAW_DATA_CACHE[token]
    
    all_activities = []
    headers = {'Authorization': f'Bearer {token}'}
    page = 1
    
    while True:
        try:
            r = requests.get("https://www.strava.com/api/v3/athlete/activities", headers=headers, params={'per_page': 200, 'page': page}, timeout=10)
            if r.status_code != 200: break
            data = r.json()
            if not data: break
            all_activities.extend(data)
            page += 1
            if page > 10: break
        except: break
    
    cleaned_data = []
    for act in all_activities:
        if act.get('type') in GPS_SPORTS and act.get('map', {}).get('summary_polyline'):
            cleaned_data.append({
                'type': act['type'],
                'start_date_local': act['start_date_local'],
                'polyline': act['map']['summary_polyline'],
                'distance': act.get('distance', 0)
            })

    RAW_DATA_CACHE[token] = cleaned_data
    return cleaned_data

# --- ROUTES ---

@app.route('/')
def index():
    if 'access_token' not in session: return redirect(url_for('login_page'))
    return render_template('index.html')

@app.route('/login')
def login_page(): return render_template('login.html')

@app.route('/auth')
def auth():
    return redirect(f"https://www.strava.com/oauth/authorize?client_id={CLIENT_ID}&response_type=code&redirect_uri={REDIRECT_URI}&approval_prompt=auto&scope=activity:read_all")

@app.route('/logout')
def logout():
    token = session.get('access_token')
    if token:
        RAW_DATA_CACHE.pop(token, None)
        API_RESULT_CACHE.pop(token, None)
    session.clear()
    return redirect(url_for('login_page'))

@app.route('/callback')
def callback():
    code = request.args.get('code')
    res = requests.post("https://www.strava.com/oauth/token", data={'client_id': CLIENT_ID, 'client_secret': CLIENT_SECRET, 'code': code, 'grant_type': 'authorization_code'})
    if res.status_code == 200:
        session['access_token'] = res.json().get('access_token')
        return redirect('/')
    return "Erreur auth"

@app.route('/stats')
def stats_page(): return render_template('stats.html') if 'access_token' in session else redirect(url_for('login_page'))

@app.route('/story')
def story_page(): return render_template('story.html') if 'access_token' in session else redirect(url_for('login_page'))

@app.route('/timelapse')
def timelapse_page(): return render_template('timelapse.html') if 'access_token' in session else redirect(url_for('login_page'))

# --- API ---

@app.route('/api/stats_history')
def get_stats_history():
    token = session.get('access_token')
    if not token: return jsonify({"error": "Login required"}), 401

    grid_meters = int(request.args.get('grid_size', 100))
    sel_year = request.args.get('year', 'all')
    sel_sport = request.args.get('sport_type', 'all')

    cache_key = f"stats_{grid_meters}_{sel_year}_{sel_sport}"
    
    if token not in API_RESULT_CACHE: API_RESULT_CACHE[token] = {}
    if cache_key in API_RESULT_CACHE[token]:
        return jsonify(API_RESULT_CACHE[token][cache_key])

    activities = get_strava_activities_cached(token)
    grid_size_deg = grid_meters / 111320
    activities.sort(key=lambda x: x['start_date_local'])

    monthly_data = {}
    global_seen = set()
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
        "available_sports": sorted(list(available_sports))
    }

    API_RESULT_CACHE[token][cache_key] = result
    return jsonify(result)

@app.route('/api/activities')
def get_activities_route():
    token = session.get('access_token')
    if not token: return jsonify({"error": "Login required"}), 401

    sel_year = request.args.get('year', 'all')
    sel_sport = request.args.get('sport_type', 'all')
    grid_meters = int(request.args.get('grid_size', 100))
    
    cache_key = f"act_{grid_meters}_{sel_year}_{sel_sport}"
    if token not in API_RESULT_CACHE: API_RESULT_CACHE[token] = {}
    
    if cache_key in API_RESULT_CACHE[token]:
        return jsonify(API_RESULT_CACHE[token][cache_key])

    activities = get_strava_activities_cached(token)
    grid_size_deg = grid_meters / 111320

    data = {
        "coords": [], "grid_cells": [], "grid_size_used": grid_size_deg,
        "available_years": set(), "available_sports": {}, "top_municipalities": [],
        "stats": { "total_distance": 0, "activity_count": 0, "cells_conquered": 0 }
    }
    
    grid_store = {}

    for act in activities:
        dt = datetime.strptime(act['start_date_local'], "%Y-%m-%dT%H:%M:%SZ")
        y_str = str(dt.year)
        sport = act['type']
        
        data["available_years"].add(y_str)
        if sport not in data["available_sports"]:
            data["available_sports"][sport] = SPORT_TRANSLATIONS.get(sport, sport)

        if (sel_year == 'all' or sel_year == y_str) and (sel_sport == 'all' or sel_sport == sport):
            pts = polyline.decode(act['polyline'])
            data["coords"].append(pts)
            
            blocks = get_cells_from_polyline(pts, grid_size_deg)
            act_ym = dt.strftime("%Y-%m")

            for b in blocks:
                if b not in grid_store:
                    grid_store[b] = {'cnt': 0, 'first': act_ym, 'last': act_ym}
                
                grid_store[b]['cnt'] += 1
                if act_ym < grid_store[b]['first']: grid_store[b]['first'] = act_ym
                if act_ym > grid_store[b]['last']: grid_store[b]['last'] = act_ym
            
            data["stats"]["total_distance"] += act['distance'] / 1000
            data["stats"]["activity_count"] += 1

    data["grid_cells"] = [[k[0], k[1], v['cnt'], v['first'], v['last']] for k, v in grid_store.items()]
    data["stats"]["cells_conquered"] = len(grid_store)
    data["available_years"] = sorted(list(data["available_years"]), reverse=True)
    data["available_sports"] = dict(sorted(data["available_sports"].items(), key=lambda x: x[1]))

    # --- CALCUL DES VILLES (VERSION BDD SUPABASE) ---
    # Cette partie a été réécrite pour ne plus utiliser le fichier cache ni l'API Gouv.
    # On interroge la base de données pour les points les plus fréquentés.
    
    if grid_store and DB_URL:
        # On prend les 400 points les plus visités pour identifier les villes principales
        sorted_locs = sorted(grid_store.items(), key=lambda x: x[1]['cnt'], reverse=True)
        scan_points = [k for k, v in sorted_locs[:400]] # Limit 400 pour aller vite

        identified_cities = {}
        
        # Pour éviter de requêter la même ville 50 fois, on fait un petit cache local temporaire
        local_city_cache = {}

        for lat, lon in scan_points:
            # On cherche la ville pour ce point
            # Idéalement on ferait un "Batch Query" mais on reste simple ici pour commencer
            
            # Vérif cache local (si on a déjà vu ce point proche)
            # Astuce : on arrondit fort pour éviter de spammer la BDD
            approx_key = (round(lat, 2), round(lon, 2))
            
            muni = None
            if approx_key in local_city_cache:
                muni = local_city_cache[approx_key]
            else:
                muni = get_city_from_db(lat, lon)
                if muni: local_city_cache[approx_key] = muni

            if muni:
                name = muni['name']
                if name not in identified_cities and len(identified_cities) < 50:
                    try:
                        # Calcul du % exploré (inchangé pour compatibilité)
                        # On utilise shapely pour vérifier quels carrés sont dans la ville
                        poly_geom = Polygon(muni['outline'])
                        prepared_poly = prep(poly_geom)
                        count_inside = 0
                        
                        # Optimisation: on ne teste que les points qui sont proches du centre de la ville
                        # (bounding box sommaire)
                        min_lat, min_lon, max_lat, max_lon = poly_geom.bounds
                        
                        for (clat, clon) in grid_store.keys():
                            if min_lat <= clat <= max_lat and min_lon <= clon <= max_lon:
                                if prepared_poly.contains(Point(clat, clon)):
                                    count_inside += 1
                        
                        if count_inside > 0:
                            # Calcul du pourcentage basé sur la surface réelle de la BDD
                            area_conquered_m2 = count_inside * (grid_meters**2)
                            pct = (area_conquered_m2 / muni['area_m2']) * 100
                            
                            identified_cities[name] = {
                                "name": name, 
                                "outline": muni['outline'],
                                "stats": {
                                    "blocks": count_inside, 
                                    "percent": round(min(pct, 100), 2)
                                }
                            }
                    except Exception as e:
                        print(f"Erreur calcul ville {name}: {e}")

        data["top_municipalities"] = sorted(list(identified_cities.values()), key=lambda x: x['stats']['blocks'], reverse=True)

    API_RESULT_CACHE[token][cache_key] = data
    return jsonify(data)

if __name__ == '__main__':

    app.run(debug=True, port=5000)
