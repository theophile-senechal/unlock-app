-- Nom et prénom Strava des personnes connectées ces 7 derniers jours, visibles seulement dans /admin.
-- Mis à jour à chaque connexion (au plus une fois par jour) et effacés par le nettoyage quotidien
-- 7 jours après leur dernière lecture (Strava API Policy §6.2). À lancer une fois dans Supabase > SQL Editor.
ALTER TABLE public.strava_users ADD COLUMN IF NOT EXISTS first_name text;
ALTER TABLE public.strava_users ADD COLUMN IF NOT EXISTS last_name  text;
ALTER TABLE public.strava_users ADD COLUMN IF NOT EXISTS name_at    timestamptz;
