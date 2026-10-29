-- =====================================================================
-- Unlock : nettoyage + cache d'activités (tout en un)
-- À coller dans Supabase > SQL Editor, puis "Run".
-- =====================================================================
BEGIN;

-- 1. Garder l'info "fondateur" avant de supprimer profiles
ALTER TABLE public.strava_users ADD COLUMN IF NOT EXISTS is_founder boolean DEFAULT false;
DO $$ BEGIN
  IF to_regclass('public.profiles') IS NOT NULL THEN
    UPDATE public.strava_users u SET is_founder = COALESCE(p.is_founder, false)
    FROM public.profiles p WHERE p.id = u.athlete_id;
  END IF;
END $$;

-- 2. Suppression des tables en double ou inutilisées
DROP TABLE IF EXISTS public.city_scores;   -- ancien classement (non conforme Strava)
DROP TABLE IF EXISTS public.api_cache;     -- ancien cache de résultats
DROP TABLE IF EXISTS public.profiles;      -- doublon de strava_users (+ premium)
ALTER TABLE public.strava_users DROP COLUMN IF EXISTS athlete_name;

-- 3. Cache d'activités : purge des anciennes données + nouvelles colonnes
DELETE FROM public.activities;
ALTER TABLE public.activities   ADD COLUMN IF NOT EXISTS start_epoch bigint;
ALTER TABLE public.activities   ADD COLUMN IF NOT EXISTS fetched_at timestamptz NOT NULL DEFAULT now();
ALTER TABLE public.strava_users ADD COLUMN IF NOT EXISTS full_sync_at timestamptz;
ALTER TABLE public.strava_users ADD COLUMN IF NOT EXISTS refresh_token text;   -- pour révoquer les comptes inactifs
CREATE INDEX IF NOT EXISTS activities_athlete_epoch_idx ON public.activities (athlete_id, start_epoch);

-- 4. Index spatial sur les communes (recherche des communes traversées)
CREATE INDEX IF NOT EXISTS communes_geometry_gist ON public.communes USING GIST (geometry);

-- 5. Sécurité : bloque l'API publique Supabase (l'app passe par DATABASE_URL, non concernée)
ALTER TABLE public.strava_users ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.activities   ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.communes     ENABLE ROW LEVEL SECURITY;

-- 6. Tableau de bord admin : journal ANONYME (aucun identifiant d'utilisateur)
CREATE TABLE IF NOT EXISTS public.app_events (
  id bigserial PRIMARY KEY,
  kind text NOT NULL,                       -- signup, login, visit, logout, delete, revoke_inactive
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS app_events_created_idx ON public.app_events (created_at);
CREATE TABLE IF NOT EXISTS public.app_daily_stats (
  day date PRIMARY KEY, connected integer, active_7 integer, active_31 integer
);
CREATE TABLE IF NOT EXISTS public.app_kv (
  key text PRIMARY KEY, value jsonb, updated_at timestamptz DEFAULT now()
);
ALTER TABLE public.app_events      ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.app_daily_stats ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.app_kv          ENABLE ROW LEVEL SECURITY;

COMMIT;
