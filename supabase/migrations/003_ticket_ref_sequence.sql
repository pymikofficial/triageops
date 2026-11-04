-- Ticket refs from a sequence instead of random digits, so refs never collide.
-- Format stays TKT-MMDD-NNNN (London date, then a running number).

create sequence if not exists public.ticket_ref_seq start 1001;

alter table public.tickets
  alter column ref set default (
    'TKT-' || to_char(now() at time zone 'Europe/London', 'MMDD') || '-' || lpad(nextval('public.ticket_ref_seq')::text, 4, '0')
  );

grant usage on sequence public.ticket_ref_seq to triage_api;
