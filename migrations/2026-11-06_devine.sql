-- « Devine la commune » : une partie par utilisateur et par jour.
-- À lancer une fois dans Supabase > SQL Editor. Sans risque : peut être relancé.
-- Contenu : la commune du jour (code INSEE), la réponse donnée, le résultat et l'usage de l'indice.
-- Visible par l'utilisateur seul ; effacé avec son compte (suppression ou inactivité de 31 jours).
CREATE TABLE IF NOT EXISTS public.devine_parties (
  athlete_id  bigint      NOT NULL,
  jour        date        NOT NULL,          -- jour du défi (heure de Paris)
  code_insee  text        NOT NULL,          -- commune à trouver
  raison      text,                          -- 'nouvelle', 'seconde' ou vide
  indice      boolean     NOT NULL DEFAULT false,
  reponse     text,                          -- code INSEE répondu (vide tant que pas de réponse)
  trouve      boolean,                       -- vide tant que pas de réponse
  created_at  timestamptz NOT NULL DEFAULT now(),
  answered_at timestamptz,
  PRIMARY KEY (athlete_id, jour)
);
-- Comme les autres tables : aucun accès par l'API publique de Supabase, seulement par le serveur d'Unlock.
ALTER TABLE public.devine_parties ENABLE ROW LEVEL SECURITY;
