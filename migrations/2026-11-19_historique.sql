-- ============================================================
-- Historique long (v57)
-- La carte s'affiche d'abord avec les 2 000 activités les plus récentes,
-- puis le reste de l'historique est lu en arrière-plan, par morceaux.
-- hist_before : NULL = historique complet ; sinon, date (en secondes) avant laquelle il reste des activités à lire.
-- Sans cette colonne, l'appli fonctionne comme avant (2 000 activités au plus).
-- À lancer une fois dans Supabase (SQL Editor). Sans danger si relancé.
-- ============================================================
ALTER TABLE public.strava_users ADD COLUMN IF NOT EXISTS hist_before bigint;
