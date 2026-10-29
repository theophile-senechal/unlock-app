-- =====================================================================
-- Unlock : cache d'activités en base (48 h max) + mise en conformité
-- À exécuter UNE fois dans Supabase (SQL Editor), avant de déployer.
-- =====================================================================

BEGIN;

-- 1. Purge des données Strava dont l'âge est inconnu
--    (Strava API Policy §6.2 : pas plus de 7 jours ; §2.3/§5.4 : pas de classement)
DELETE FROM public.activities;         -- ancienne table non datée, réutilisée comme cache
DELETE FROM public.api_cache;          -- résultats calculés (données géographiques dérivées)
DELETE FROM public.city_scores;        -- ancien classement
UPDATE public.strava_users SET athlete_name = '';

-- 2. Colonnes nécessaires au cache
ALTER TABLE public.activities   ADD COLUMN IF NOT EXISTS start_epoch bigint;
ALTER TABLE public.activities   ADD COLUMN IF NOT EXISTS fetched_at timestamptz NOT NULL DEFAULT now();
ALTER TABLE public.strava_users ADD COLUMN IF NOT EXISTS full_sync_at timestamptz;
ALTER TABLE public.strava_users ADD COLUMN IF NOT EXISTS refresh_token text;

-- 3. Index pour lire vite les activités d'un athlète
CREATE INDEX IF NOT EXISTS activities_athlete_epoch_idx ON public.activities (athlete_id, start_epoch);

COMMIT;

-- 4. (Recommandé) La table `profiles` n'est plus utilisée par l'app.
--    Elle contient prénoms et refresh tokens d'anciens utilisateurs : à vider si tu ne t'en sers pas.
-- DELETE FROM public.profiles;
