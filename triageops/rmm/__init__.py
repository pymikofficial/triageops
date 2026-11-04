"""RMM alert intake for NinjaOne and Datto RMM style payloads at /api/webhooks/rmm.

Neither NinjaOne nor Datto RMM signs its webhooks. TriageOps keeps one
authentication scheme for this route: the same timestamped HMAC as
/api/webhooks/inbound. The RMM posts to the n8n relay, which checks a static
header secret and then signs the body for TriageOps.

* ``parsers`` turns a vendor payload into a channel neutral ``RmmAlert``.
* ``intake`` collapses repeats, records resets and triages new alerts.
* ``store`` holds the SQL that finds earlier deliveries and open alert tickets.
* ``routes`` is the FastAPI router included by ``triageops.app``.
"""
