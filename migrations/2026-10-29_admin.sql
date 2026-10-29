-- Tableau de bord admin (à lancer si tu as déjà exécuté tout_en_un.sql avant cette version)
BEGIN;
ALTER TABLE public.strava_users ADD COLUMN IF NOT EXISTS refresh_token text;

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
