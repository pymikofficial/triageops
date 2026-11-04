-- Dedicated least-privilege role for the TriageOps backend.
-- The real password is set at deploy time and stored only as a server environment variable.
-- No DELETE is granted anywhere, so a compromised backend cannot wipe the queue.

do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'triage_api') then
    create role triage_api login password 'SET_AT_DEPLOY_TIME' noinherit;
  end if;
end
$$;

alter role triage_api set search_path = public, extensions;
alter role triage_api set statement_timeout = '15s';

grant usage on schema public, extensions to triage_api;
grant select, insert, update on public.tickets, public.ticket_events to triage_api;
grant select, insert on public.kb_articles to triage_api;
grant execute on function public.match_kb(extensions.vector, int) to triage_api;

create policy triage_api_tickets on public.tickets for all to triage_api using (true) with check (true);
create policy triage_api_events on public.ticket_events for all to triage_api using (true) with check (true);
create policy triage_api_kb on public.kb_articles for all to triage_api using (true) with check (true);
