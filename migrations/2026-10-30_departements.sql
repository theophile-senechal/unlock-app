-- Mode « Communes » de la carte : recherche rapide des communes d'un département.
-- Le département se déduit du code INSEE (2 premiers caractères, 3 pour l'outre-mer) :
-- aucune nouvelle colonne, juste un index pour les recherches « code_insee LIKE '56%' ».
-- Sans risque : peut être relancé plusieurs fois.
CREATE INDEX IF NOT EXISTS communes_code_insee_prefix_idx
  ON public.communes (code_insee text_pattern_ops);

-- Vérification (doit afficher une centaine de lignes, ex. 56 | 249) :
-- SELECT CASE WHEN code_insee LIKE '97%' THEN left(code_insee, 3) ELSE left(code_insee, 2) END AS dep, count(*)
-- FROM public.communes GROUP BY 1 ORDER BY 1;
