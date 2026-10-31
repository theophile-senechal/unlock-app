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
acces.py               Grille des blocs, marge GPS de 20 m, blocs sans chemin
data/acces/            Blocs sans chemin par département (calcul mensuel, voir tools/acces)
tools/acces/           Calcul OpenStreetMap + .github/workflows/blocs-inaccessibles.yml
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

Dans Supabase (SQL Editor), **avant** de déployer :
- première fois : `migrations/2026-10-29_tout_en_un.sql` (nettoyage, cache d'activités, sécurité, tableau de bord) ;
- mode « Communes » : `migrations/2026-10-30_departements.sql` (un index, sans risque, relançable) ;
- utilisateurs depuis le lancement : `migrations/2026-10-30_utilisateurs_uniques.sql` (une colonne, sans risque, relançable) ;
- si `tout_en_un.sql` a déjà été lancé avant le tableau de bord : `migrations/2026-10-29_admin.sql`.

Tables utilisées : `strava_users`, `activities` (cache 47 h), `communes`, `app_events`, `app_daily_stats`, `app_kv`.

## Prochaines étapes

### Avant / pendant la mise en ligne
- [ ] Supabase : exécuter `migrations/2026-10-29_tout_en_un.sql`.
- [ ] Vercel : `SECRET_KEY`, `CRON_SECRET`, `CARTO_API_KEY` (clé gratuite : carto.com/basemaps/apikey), puis `ADMIN_ATHLETE_IDS` une fois l'identifiant Strava connu (+ redéploiement). Optionnel : `MIN_ACTIVE_DISPLAY=1`.
- [ ] Pousser sur GitHub (supprimer les anciens HTML de la racine).
- [ ] Blocs sans chemin : après avoir poussé, lancer une fois la tâche GitHub « Blocs inaccessibles » (onglet Actions > Run workflow) pour calculer toute la France. Le Morbihan est déjà inclus.
- [ ] Vérifier sur le site : fond de carte, pourcentages des communes, tableau de bord admin (« Santé de l'app »).
- [ ] Demande en cours chez Strava : confirmer qu'une révocation libère une place sur le quota, et l'attribution Garmin (§4.4).

### Prochaines fonctionnalités (dans l'ordre)
1. **Améliorations de l'interface** : à préciser à partir des retours d'usage.
2. **Unlock Rewind** (bilan de l'année) : à préparer vers mi-novembre pour décembre.
3. **Lieux à collectionner** : châteaux et monuments « débloqués » quand une trace passe à proximité. Source : base Mérimée (data.culture.gouv.fr) et/ou OpenStreetMap. Recalcul à la volée, sans stockage durable (Strava §5.7 / §6.2).

### Fait récemment
- **Blocs sans chemin et marge GPS** : un bloc sans aucune voie praticable à moins de 20 m (champ, marais, terrain militaire, aérodrome…) n'est plus compté dans le % de la commune, sauf s'il a été visité. Hachures grises sur la commune choisie (mode Blocs). La même marge de 20 m s'applique aux traces (imprécision du GPS) : 100 % devient atteignable partout. Profils à pied / vélo selon les sports affichés. Données OpenStreetMap calculées chaque mois par une tâche GitHub (`tools/acces`, environ 10 Mo pour la France dans `data/acces`, rien dans Supabase). Ski, natation et sports virtuels ne sont plus suivis.
- **Story « Département »** : choix Commune / Département dans l'éditeur Story. Carte de toutes les communes du département (dessinée en SVG, sans fond de carte), communes explorées colorées selon leur %, nom du département, % du département et communes explorées / total. Couleurs, fond transparent ou coloré, nom et chiffres masquables, « unlock » toujours en bas.
- **Utilisateurs depuis le lancement** (`/admin`) : total d'utilisateurs uniques et courbe cumulée jour par jour (nouveaux du jour au survol). Le total ne redescend jamais : il continue de monter quand des comptes inactifs sont révoqués, et survit à la purge du journal (400 jours) grâce à un relevé quotidien `app_daily_stats.new_users` (un nombre par jour, jamais revu à la baisse). Point de départ figé une fois (`app_kv.unique_baseline`) pour les comptes antérieurs au suivi. Limite assumée : une personne qui supprime ses données puis revient est comptée deux fois.
- **Dons Ko-fi** : « Soutenir Unlock » dans le menu ☰ et en pied de page, fenêtre d'explication dans l'app puis page Ko-fi dans un nouvel onglet (via `/soutenir/go`, qui compte les clics de façon anonyme, visibles dans `/admin`). Activé uniquement si `KOFI_URL` est renseignée dans Vercel. Dons libres, sans contrepartie (Strava §5.8), minimum 2 € réglé côté Ko-fi. Paragraphes ajoutés aux CGU et à la politique de confidentialité.
- **Mode « Communes » de la carte** (3e mode après Tracés et Blocs) : toutes les communes d'un département, colorées selon le % exploré (0 % en gris clair), contours de toutes les communes et du département. Panneau : communes explorées / total, à 100 %, plus de 50 %, top 5 départements, top 5 communes, légende. Infobulle au clic (ordinateur, placée à droite / gauche / au-dessus / en dessous selon la place, jamais sous le panneau) ; fiche dans le panneau du bas (mobile). Département par défaut : meilleur % de communes visitées parmi ceux où au moins 5 communes sont visitées (`DEP_MIN_COMMUNES`), sinon le plus de communes visitées. Contours servis par `/api/departement/<code>/geo` (donnée publique, cache CDN 7 jours). Communes identifiées par code INSEE (homonymes distingués), jusqu'à 300 communes analysées.
- **Mes communes** (Stats, `/api/communes`) : km parcourus *dans* chaque commune, sorties, % exploré, blocs, 1re et dernière visite ; tri par colonne (ordinateur) ou menu « Trier par » (mobile, en cartes) ; recherche ; 10 premières puis « Voir tout ». Filtres année/sport/taille appliqués. Jusqu'à 100 communes, les plus fréquentées d'abord (`MAX_COMMUNES`).
- **Vos habitudes** (Stats) : sorties par semaine (comparées aux 4 dernières semaines), km par semaine, créneau horaire favori, jour favori, deux graphiques. Les filtres année/sport s'appliquent.

### Boîte à idées (à trancher)
Pas de champ libre public (risque de contenu n'importe quoi). Deux options sans modération :
- **Vote fermé** : l'admin propose 3 à 5 idées, les utilisateurs votent seulement (1 vote par compte, résultats dans `/admin`).
- **Retour privé** : message court (280 caractères) visible uniquement dans `/admin`, jamais affiché publiquement, limité à 1 envoi par jour et par compte.

### Idées en réserve
Séries de semaines avec nouveaux blocs, « ville de cœur » déclarée (compteur d'explorateurs par ville, affiché à partir de 5), trous à combler, plus grand carré exploré, communes voisines jamais visitées, journal des premières fois, calendrier d'exploration, exploration par sport, bilan hebdo en notification, « Où aller pour débloquer ? », objectifs par commune, badges, toi contre l'an dernier, timelapse en vidéo, carte de France des communes, poster HD, import GPX/FIT, masquer le domicile dans la Story, tests automatiques sur GitHub, contours de communes simplifiés (performance).

### Écarté
- **Météo** : absente de l'API Strava ; un service externe impliquerait de partager lieu et heure des sorties avec un tiers (Strava §6.2).
- **Statistiques globales entre utilisateurs** : interdites par la Strava API Policy (§5.4, §5.7).

### En attente d'un accord de Strava
- **Multijoueur / classement entre amis** : à demander à Strava (developers@strava.com) sous forme de dérogation écrite : opt-in explicite de chaque participant, amis uniquement, aucune donnée stockée au-delà du cache. Le code du classement est conservé (désactivé, `ENABLE_LEADERBOARD=false`).
