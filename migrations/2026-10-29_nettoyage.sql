-- =====================================================================
-- Unlock : nettoyage du schéma Supabase
-- À exécuter APRÈS 2026-10-29_cache_activites.sql (SQL Editor).
-- Partie 1 = sans risque. Partie 2 = suppressions de tables : à décommenter
-- une fois que tu as vérifié que tu n'en as plus besoin.
-- =====================================================================

-- ---------------------------------------------------------------------
-- PARTIE 1 : sans risque
-- ---------------------------------------------------------------------

-- 1.1 Retrait du premium (Strava API Policy §5.8 : interdit de faire payer
--     l'accès à des fonctions basées sur les données Strava)
ALTER TABLE public.profiles DROP COLUMN IF EXISTS is_premium;

-- 1.2 Sécurité : bloque l'API publique de Supabase (clé "anon") sur toutes les tables.
--     L'app se connecte directement en Postgres (DATABASE_URL) et n'est pas concernée.
--     Sans ça, quiconque a la clé anon peut lire les jetons Strava et les tracés GPS.
ALTER TABLE public.strava_users ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.activities   ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.communes     ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.profiles     ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.api_cache    ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.city_scores  ENABLE ROW LEVEL SECURITY;

-- 1.3 Performance : index spatial sur les communes (accélère fortement ST_Intersects)
CREATE INDEX IF NOT EXISTS communes_geometry_gist ON public.communes USING GIST (geometry);

-- ---------------------------------------------------------------------
-- PARTIE 2 : tables en double ou inutilisées (à décommenter)
-- ---------------------------------------------------------------------

-- 2.1 profiles : doublon de strava_users (même athlète, autre format), plus utilisée par l'app.
--     Contient prénoms + refresh tokens d'anciens utilisateurs.
--     Si tu veux garder "fondateur" ou les dons, copie-les d'abord :
-- ALTER TABLE public.strava_users ADD COLUMN IF NOT EXISTS is_founder boolean DEFAULT false;
-- UPDATE public.strava_users u SET is_founder = p.is_founder FROM public.profiles p WHERE p.id = u.athlete_id;
-- DROP TABLE IF EXISTS public.profiles;

-- 2.2 api_cache : ancien cache de résultats, plus utilisé (remplacé par le cache d'activités).
-- DROP TABLE IF EXISTS public.api_cache;

-- 2.3 city_scores : ancien classement, désactivé (non conforme Strava §2.3 / §5.4 / §5.7).
-- DROP TABLE IF EXISTS public.city_scores;

-- 2.4 strava_users.athlete_name : toujours vide depuis la désactivation du classement.
--     (à faire seulement avec 2.3)
-- ALTER TABLE public.strava_users DROP COLUMN IF EXISTS athlete_name;

-- NE PAS TOUCHER : spatial_ref_sys (table système de PostGIS), communes, strava_users, activities.
