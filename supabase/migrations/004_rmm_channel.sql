-- RMM alert intake (NinjaOne and Datto RMM through /api/webhooks/rmm) and PSA sync.
-- Not applied automatically. Apply it before deploying the code that sends channel 'rmm',
-- otherwise RMM alerts fail the channel check and the endpoint returns 503.

-- 1. A new channel for tickets raised by RMM alerts.
alter table public.tickets drop constraint if exists tickets_channel_check;
alter table public.tickets
  add constraint tickets_channel_check check (channel in ('email', 'whatsapp', 'webhook', 'portal', 'rmm'));

-- 2. Repeat alerts for the same device and condition are collapsed into one ticket.
-- The de-duplication key lives in the event detail, so this partial index keeps the 30 minute lookup cheap.
create index if not exists ticket_events_rmm_key_idx
  on public.ticket_events ((detail->>'rmm_key'), created_at desc)
  where type in ('received', 'rmm_repeat', 'rmm_reset');

-- 3. Retried RMM deliveries are matched on their alert id, in the first alert or in a repeat.
create index if not exists ticket_events_source_id_idx
  on public.ticket_events ((detail->>'source_id'))
  where type in ('received', 'rmm_repeat');

-- No new grants. triage_api already has select, insert and update on tickets and ticket_events,
-- and PSA sync results are stored as ticket_events rows (psa_synced, psa_failed, psa_skipped).
