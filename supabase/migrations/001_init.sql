-- TriageOps schema. Supabase Postgres 17, London (eu-west-2).
-- The backend connects as a dedicated least-privilege role (triage_api), created in 002_role.sql
-- with a password that lives only in the server environment.

create extension if not exists vector with schema extensions;
create extension if not exists pgcrypto with schema extensions;

create table if not exists public.tickets (
  id uuid primary key default gen_random_uuid(),
  ref text unique not null default ('TKT-' || to_char(now() at time zone 'Europe/London', 'MMDD') || '-' || lpad((floor(random() * 10000))::int::text, 4, '0')),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  channel text not null check (channel in ('email', 'whatsapp', 'webhook', 'portal')),
  customer text,
  requester text,
  subject text not null check (char_length(subject) between 1 and 300),
  body text not null check (char_length(body) between 1 and 6000),
  status text not null default 'new' check (status in ('new', 'triaged', 'escalated', 'resolved', 'failed')),
  category text check (category in ('Network', 'Hardware', 'Software', 'Access & Identity', 'Security', 'Email & M365', 'Backup & Recovery', 'Billing & Account', 'Other')),
  priority text check (priority in ('P1', 'P2', 'P3', 'P4')),
  sentiment text check (sentiment in ('positive', 'neutral', 'frustrated', 'angry')),
  team text check (team in ('Service Desk L1', 'Infrastructure L2', 'Security Ops', 'Cloud & M365', 'Account Management')),
  summary text,
  suggested_reply text,
  next_steps jsonb not null default '[]'::jsonb,
  kb_refs jsonb not null default '[]'::jsonb,
  confidence numeric(3, 2) check (confidence between 0 and 1),
  injection_suspected boolean not null default false,
  pii_masked boolean not null default false,
  escalation_reason text,
  sla_due_at timestamptz,
  sla_breached boolean not null default false,
  ai_model text,
  ai_latency_ms integer,
  input_tokens integer,
  output_tokens integer,
  client_hash text,
  is_demo boolean not null default false
);

create index if not exists tickets_created_at_idx on public.tickets (created_at desc);
create index if not exists tickets_open_sla_idx on public.tickets (sla_due_at) where status in ('new', 'triaged', 'escalated') and sla_breached = false;
create index if not exists tickets_client_hash_idx on public.tickets (client_hash, created_at desc);

create table if not exists public.ticket_events (
  id bigint generated always as identity primary key,
  ticket_id uuid not null references public.tickets (id) on delete cascade,
  created_at timestamptz not null default now(),
  type text not null,
  detail jsonb not null default '{}'::jsonb
);

create index if not exists ticket_events_ticket_idx on public.ticket_events (ticket_id, created_at);

create table if not exists public.kb_articles (
  id bigint generated always as identity primary key,
  slug text unique not null,
  title text not null,
  category text not null,
  content text not null,
  embedding extensions.vector(384),
  created_at timestamptz not null default now()
);

create index if not exists kb_articles_embedding_idx on public.kb_articles using hnsw (embedding extensions.vector_cosine_ops);

-- Keep updated_at honest without trusting the application.
create or replace function public.touch_updated_at() returns trigger
language plpgsql set search_path = '' as $$
begin
  new.updated_at := now();
  return new;
end;
$$;

drop trigger if exists tickets_touch_updated_at on public.tickets;
create trigger tickets_touch_updated_at before update on public.tickets
  for each row execute function public.touch_updated_at();

-- Cosine similarity search over the knowledge base (RAG retrieval).
create or replace function public.match_kb(query_embedding extensions.vector(384), match_count int default 3)
returns table (id bigint, slug text, title text, category text, content text, similarity float)
language sql stable set search_path = public, extensions as $$
  select k.id, k.slug, k.title, k.category, k.content,
         1 - (k.embedding <=> query_embedding) as similarity
  from public.kb_articles k
  where k.embedding is not null
  order by k.embedding <=> query_embedding
  limit least(greatest(match_count, 1), 5);
$$;

-- Row level security on, no access for the public API roles.
alter table public.tickets enable row level security;
alter table public.ticket_events enable row level security;
alter table public.kb_articles enable row level security;

revoke all on public.tickets, public.ticket_events, public.kb_articles from anon, authenticated;
revoke execute on function public.match_kb(extensions.vector, int) from public, anon, authenticated;
