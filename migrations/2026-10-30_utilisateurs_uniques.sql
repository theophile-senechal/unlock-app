-- Tableau de bord admin : « Utilisateurs depuis le lancement ».
-- Ajoute un compteur de nouveaux utilisateurs par jour dans les relevés quotidiens (un nombre, rien d'autre).
-- Ces relevés sont gardés sans limite, contrairement au journal d'événements (purgé après 400 jours) :
-- le total ne redescend donc jamais. Sans risque : peut être relancé plusieurs fois.
ALTER TABLE public.app_daily_stats ADD COLUMN IF NOT EXISTS new_users integer;
