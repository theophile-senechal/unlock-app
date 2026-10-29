# Unlock

Vos sorties Strava transformées en blocs débloqués sur la carte, commune par commune.

## Structure

```
app.py                  Flask (routes, API, conformité Strava)
templates/
  base.html, _ui.html   Gabarit commun (barre du haut, navigation mobile, menu, chargement, PWA)
  index.html            Carte (tracés / blocs / communes)
  stats.html            Statistiques
  timelapse.html        Animation mois par mois
  story.html            Générateur d'image story
  login.html            Connexion
  legal/                CGU, confidentialité, mentions légales
  deleted.html          Confirmation écrite de suppression des données
  offline.html, error.html
  leaderboard.html      Classement (DÉSACTIVÉ, voir plus bas)
static/
  css/app.css           Design system
  js/app.js             Chargement, menus, installation PWA, toasts
  img/logo.svg          Logo (+ variantes maskable / carrée)
  icons/                Icônes PNG de l'app
  manifest.webmanifest, sw.js
  vendor/               Leaflet, Chart.js, dom-to-image-more (servis en local)
  fonts/                Sora (servie en local, pas de Google Fonts)
```

Les anciens fichiers HTML à la racine (versions plus récentes que celles de `templates/`) ont été fusionnés dans `templates/`. `leaderboard.html` n'existait qu'à la racine : `/leaderboard` plantait en production.

## Variables d'environnement

Voir `.env.example`. Nouvelles : `SITE_OWNER`, `ENABLE_LEADERBOARD` (false par défaut).

## Conformité Strava (API Policy du 1er juin 2026)

- **Classement désactivé** : il affichait les noms et scores d'autres athlètes (§2.3), agrégeait les données de plusieurs utilisateurs (§5.4) et stockait durablement des données géographiques dérivées (§5.7, §6.2). Les routes `/leaderboard` et `/api/*leaderboard` renvoient 404 et la table `city_scores` n'est plus alimentée. Le nom de l'athlète n'est plus enregistré.
- **Cache d'activités en base (table `activities`) : 47 h maximum.** Au-delà, rechargement complet ; entre-temps, seules les nouvelles activités sont demandées à Strava (paramètre `after`, avec 3 jours de chevauchement pour les synchros tardives). Une activité supprimée sur Strava disparaît donc en moins de 48 h (§6.3). Le cron quotidien efface le cache expiré.
- **Comptes inactifs** : sans connexion pendant `INACTIVE_DAYS` (31 j par défaut), le cron renouvelle le jeton (refresh token), révoque l'accès Strava (place libérée sur le quota) et efface les données.
- **Compteur d'utilisateurs actifs** (≥ 1 connexion sur 31 jours) sur la page de connexion, masqué sous `MIN_ACTIVE_DISPLAY`.
- **Tableau de bord admin** (`/admin`, menu ☰) : places occupées sur le quota, actifs, arrivées/départs, dernière visite, santé (cron, quota de requêtes Strava, configuration). Accès réservé aux identifiants Strava de `ADMIN_ATHLETE_IDS`. Données 100 % anonymes (tables `app_events`, `app_daily_stats`, `app_kv`).
- **Jetons renouvelés automatiquement** (ils expirent après 6 h) : plus de carte vide après quelques heures.
- **Suppression** : « Se déconnecter » et « Supprimer mes données » révoquent l'accès et effacent `strava_users` + `city_scores` ; une confirmation écrite s'affiche (§2.5, §7.4).
- **Consentement** affiché sur la page de connexion (§2.1), politique de confidentialité RGPD (§7.3), clause d'exonération des prestataires tiers dans les CGU (§9.2).
- **OAuth** : paramètre `state` (anti-CSRF), vérification du scope accordé.
- **Marque Strava** : aucun logo officiel, uniquement des mentions texte (« Se connecter avec Strava », « Powered by Strava »), ce qui est permis par les Brand Guidelines.
- À prévoir : `/oauth/deauthorize` sera remplacé par `/oauth/revoke` le 1er juin 2027.

## Base de données

Exécuter une fois `migrations/2026-10-29_cache_activites.sql` dans Supabase (SQL Editor) **avant** de déployer :
purge des anciennes données Strava, colonnes `activities.start_epoch` / `fetched_at`, `strava_users.full_sync_at`, index.

Tables utilisées : `strava_users`, `activities` (cache 47 h), `communes`. `city_scores` et `api_cache` sont vidées et
nettoyées mais plus alimentées ; `profiles` n'est plus utilisée (à vider, voir fin de la migration).
