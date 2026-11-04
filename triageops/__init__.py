"""TriageOps backend: an AI service desk for an IT managed service provider.

Tickets arrive from email, WhatsApp, a web portal and signed webhooks. Claude
triages each one, grounded in a knowledge base, and deterministic business
rules in Python have the final say on priority, escalation and SLA.

This package deliberately has no import side effects, so lightweight modules
such as ``triageops.samples`` can be used by standalone scripts.
"""
