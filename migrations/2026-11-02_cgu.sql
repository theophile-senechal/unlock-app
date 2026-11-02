-- Preuve de l'accord des CGU et de la politique de confidentialité : version acceptée et date.
-- À lancer une fois dans Supabase > SQL Editor. Sans risque : peut être relancé.
ALTER TABLE public.strava_users ADD COLUMN IF NOT EXISTS cgu_version     text;
ALTER TABLE public.strava_users ADD COLUMN IF NOT EXISTS cgu_accepted_at timestamptz;
