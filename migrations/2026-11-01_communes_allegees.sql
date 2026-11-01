-- Allège les contours des communes (59 Mo, la plus grosse table de la base).
-- On retire seulement les points superflus des contours : écart maximal d'environ 2 m,
-- invisible avec des blocs de 100 ou 250 m. À lancer dans Supabase > SQL Editor, ÉTAPE PAR ÉTAPE.

-- ÉTAPE 1 (lecture seule) : gain attendu. Ne modifie rien.
SELECT count(*) AS communes,
       sum(ST_NPoints(geometry)) AS points_avant,
       sum(ST_NPoints(ST_SimplifyPreserveTopology(geometry, 0.00002))) AS points_apres,
       pg_size_pretty(pg_total_relation_size('public.communes')) AS taille_table_avant
FROM public.communes;

-- ÉTAPE 2 : copie de sécurité (environ 60 Mo de plus pendant quelques minutes, la limite est à 500 Mo).
CREATE TABLE IF NOT EXISTS public.communes_sauvegarde AS TABLE public.communes;

-- ÉTAPE 3 : simplification. Une commune n'est modifiée que si son nouveau contour reste valide.
UPDATE public.communes
SET geometry = ST_SimplifyPreserveTopology(geometry, 0.00002)
WHERE ST_NPoints(geometry) > 50
  AND ST_IsValid(ST_SimplifyPreserveTopology(geometry, 0.00002));

-- ÉTAPE 4 : à lancer SEULE (une requête à la fois) pour rendre la place libérée.
VACUUM FULL public.communes;

-- ÉTAPE 5 : à lancer seule aussi.
ANALYZE public.communes;

-- ÉTAPE 6 : vérification puis suppression de la copie, une fois l'app testée (carte, % d'une commune).
SELECT pg_size_pretty(pg_total_relation_size('public.communes')) AS taille_table_apres,
       count(*) FILTER (WHERE NOT ST_IsValid(geometry)) AS contours_invalides
FROM public.communes;
-- DROP TABLE public.communes_sauvegarde;
