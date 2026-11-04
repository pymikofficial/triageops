"""``POST /api/webhooks/rmm``: signed RMM alerts from the n8n relay.

NinjaOne and Datto RMM do not sign their webhooks, so they post to the n8n
relay, which checks a static header secret and signs the body for TriageOps.
This route accepts only that timestamped HMAC (``X-TriageOps-Timestamp`` and
``X-TriageOps-Signature``), with ``RMM_WEBHOOK_SECRET`` or else
``WEBHOOK_SECRET``. There is deliberately no static token or query token mode.

The signature is checked over the raw bytes before anything is parsed. Every
handled alert answers 200, whatever the action, because RMMs and the relay
retry any other status.
"""

import json
import logging
from typing import Any
from urllib.parse import parse_qsl

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from .. import db, store
from ..config import settings
from ..security import (
    SignatureError,
    client_ip,
    hash_client,
    verify_timestamped_signature,
)
from . import intake
from .config import signing_secret
from .parsers import RmmIgnored, RmmPayloadError, parse_alert

logger = logging.getLogger(__name__)

router = APIRouter()

MAX_BODY_BYTES = 64 * 1024
SOURCE_MAX_CHARS = 20
_FORM_CONTENT_TYPE = "application/x-www-form-urlencoded"


async def rmm_raw_body(request: Request) -> bytes:
    """The exact signed bytes. The middleware refuses a large declared length, this catches chunked bodies."""
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        raise HTTPException(413, "The request body is too large.")
    return body


def _verify(request: Request, raw: bytes) -> None:
    try:
        verify_timestamped_signature(
            signing_secret(),
            request.headers.get("x-triageops-timestamp"),
            request.headers.get("x-triageops-signature"),
            raw,
        )
    except SignatureError as exc:
        logger.warning("signature rejected path=%s reason=%s", request.url.path, exc)
        raise HTTPException(401, "Invalid or expired signature.") from exc


def _decode(raw: bytes, content_type: str | None) -> Any:
    """A JSON body, or a form encoded body when the content type says so. Never raises anything else."""
    if (content_type or "").split(";", 1)[0].strip().lower() == _FORM_CONTENT_TYPE:
        return dict(parse_qsl(raw.decode("utf-8", "replace"), keep_blank_values=True))
    try:
        return json.loads(raw)
    except (ValueError, RecursionError) as exc:
        raise HTTPException(400, "The request body must be valid JSON.") from exc


def _client_hash(request: Request) -> str:
    peer = request.client.host if request.client else None
    return hash_client(client_ip(request.headers, peer), settings.ip_hash_salt)


@router.post("/api/webhooks/rmm")
def rmm_webhook(
    request: Request,
    raw: bytes = Depends(rmm_raw_body),
    source: str | None = Query(default=None, max_length=SOURCE_MAX_CHARS),
) -> dict[str, Any]:
    _verify(request, raw)
    payload = _decode(raw, request.headers.get("content-type"))
    try:
        alert = parse_alert(payload, source)
    except RmmIgnored as exc:
        logger.info("rmm alert vendor=%s action=ignored reason=%s", exc.vendor, exc.reason)
        return {
            "action": "ignored",
            "ticket": None,
            "occurrences": 0,
            "priority_hint": None,
            "vendor": exc.vendor,
            "reason": exc.reason,
        }
    except RmmPayloadError as exc:
        logger.info("rmm payload rejected reason=%s", exc)
        raise HTTPException(422, str(exc)) from exc

    with db.connect() as conn:
        outcome = intake.handle_alert(conn, alert, client_hash=_client_hash(request))
    body: dict[str, Any] = {
        "action": outcome.action,
        "ticket": store.to_json(outcome.ticket) if outcome.ticket else None,
        "occurrences": outcome.occurrences,
        "priority_hint": outcome.priority_hint,
        "vendor": alert.vendor,
    }
    if outcome.reason:
        body["reason"] = outcome.reason
    return body
