-- Hybrid knowledge base retrieval: Postgres full text search plus pgvector similarity,
-- merged with reciprocal rank fusion (RRF).
--
-- Safe to apply on a live database. The backend keeps calling the vector only match_kb
-- until match_kb_hybrid exists, so nothing breaks before or after this runs.
-- triageops/hybrid.py holds a Python mirror of the fusion step that the offline tests use.

-- 1. A generated tsvector. Titles weigh more than body text, and Postgres keeps it in step.
alter table public.kb_articles
  add column if not exists search_tsv tsvector
  generated always as (
    setweight(to_tsvector('english', coalesce(title, '')), 'A') ||
    setweight(to_tsvector('english', coalesce(category, '')), 'B') ||
    setweight(to_tsvector('english', coalesce(content, '')), 'C')
  ) stored;

-- 2. GIN index for the @@ match.
create index if not exists kb_articles_search_tsv_idx on public.kb_articles using gin (search_tsv);

-- 3. Hybrid match.
-- Each retriever ranks up to candidate_count articles on its own. An article scores
-- 1 / (rrf_k + rank) from every list it appears in, and the scores are summed. RRF only
-- uses ranks, so it needs no tuning between cosine distances and ts_rank values.
-- Ticket text is long and conversational, so the keyword query ORs every term
-- (plainto_tsquery ANDs them, which would almost never match a whole ticket).
create or replace function public.match_kb_hybrid(
  query_embedding extensions.vector(384),
  query_text text,
  match_count int default 3,
  rrf_k int default 60,
  candidate_count int default 20
)
returns table (
  id bigint,
  slug text,
  title text,
  category text,
  content text,
  similarity float,
  text_rank float,
  rrf_score float
)
language sql stable set search_path = public, extensions as $$
  with terms as (
    select nullif(replace(plainto_tsquery('english', left(coalesce(query_text, ''), 2000))::text, ' & ', ' | '), '')::tsquery as q
  ),
  vector_hits as (
    select k.id,
           row_number() over (order by k.embedding <=> query_embedding, k.id) as rank
    from public.kb_articles k
    where k.embedding is not null
    order by k.embedding <=> query_embedding, k.id
    limit least(greatest(candidate_count, 1), 50)
  ),
  text_hits as (
    select k.id,
           row_number() over (order by ts_rank_cd(k.search_tsv, t.q) desc, k.id) as rank
    from public.kb_articles k
    cross join terms t
    where t.q is not null and k.search_tsv @@ t.q
    order by ts_rank_cd(k.search_tsv, t.q) desc, k.id
    limit least(greatest(candidate_count, 1), 50)
  ),
  fused as (
    select coalesce(v.id, x.id) as id,
           coalesce(1.0 / (rrf_k + v.rank), 0) + coalesce(1.0 / (rrf_k + x.rank), 0) as score
    from vector_hits v
    full outer join text_hits x on x.id = v.id
  )
  select k.id, k.slug, k.title, k.category, k.content,
         coalesce(1 - (k.embedding <=> query_embedding), 0)::float as similarity,
         coalesce(ts_rank_cd(k.search_tsv, t.q), 0)::float as text_rank,
         f.score::float as rrf_score
  from fused f
  join public.kb_articles k on k.id = f.id
  cross join terms t
  order by f.score desc, similarity desc, k.slug
  limit least(greatest(match_count, 1), 5);
$$;

-- Same access model as match_kb: only the backend role may call it.
revoke execute on function public.match_kb_hybrid(extensions.vector, text, int, int, int) from public, anon, authenticated;
grant execute on function public.match_kb_hybrid(extensions.vector, text, int, int, int) to triage_api;
