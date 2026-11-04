"""Authentication, data masking and abuse controls.

* HMAC verification for our signed webhooks and for Meta WhatsApp callbacks,
  always over the raw request bytes and always with a constant time compare.
* Masking of secrets and personal data before anything is stored or sent to
  Claude, plus masking of the requester for display.
* Client IP hashing (raw IPs are never stored), an in-memory sliding window
  limiter, a prompt injection marker check and the HTTP security headers.
"""

import hashlib
import hmac
import re
import threading
import time
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass

REPLAY_WINDOW_SECONDS = 300
SIGNATURE_PREFIX = "sha256="


# Webhook signatures -------------------------------------------------------------

class SignatureError(Exception):
    """A webhook signature is missing, malformed, outside the replay window or wrong."""


def _hmac_hex(secret: str, message: bytes) -> str:
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def sign_timestamped(secret: str, timestamp: str, raw_body: bytes) -> str:
    """Signature for our webhooks: HMAC SHA-256 of ``"{timestamp}.{raw_body}"``."""
    return SIGNATURE_PREFIX + _hmac_hex(secret, timestamp.encode() + b"." + raw_body)


def _matches(expected: str, provided: str) -> bool:
    return hmac.compare_digest(expected.encode(), provided.strip().lower().encode("utf-8", "replace"))


def verify_timestamped_signature(
    secret: str,
    timestamp: str | None,
    signature: str | None,
    raw_body: bytes,
    *,
    now: float | None = None,
    window: int = REPLAY_WINDOW_SECONDS,
) -> None:
    """Raise ``SignatureError`` unless the signature is valid and fresh."""
    if not secret:
        raise SignatureError("webhook secret is not configured")
    if not timestamp or not signature:
        raise SignatureError("signature headers are missing")
    timestamp = timestamp.strip()
    if not timestamp.isdigit():
        raise SignatureError("timestamp is not a unix time in seconds")
    current = time.time() if now is None else now
    if abs(current - int(timestamp)) > window:
        raise SignatureError("timestamp is outside the replay window")
    if not _matches(sign_timestamped(secret, timestamp, raw_body), signature):
        raise SignatureError("signature does not match")


def verify_meta_signature(app_secret: str, signature: str | None, raw_body: bytes) -> None:
    """Verify Meta's ``X-Hub-Signature-256``, an HMAC of the raw body with the app secret."""
    if not app_secret:
        raise SignatureError("WhatsApp app secret is not configured")
    if not signature:
        raise SignatureError("signature header is missing")
    if not _matches(SIGNATURE_PREFIX + _hmac_hex(app_secret, raw_body), signature):
        raise SignatureError("signature does not match")


def tokens_match(provided: str | None, expected: str) -> bool:
    """Constant time comparison that never accepts an unset secret."""
    if not expected or not provided:
        return False
    return hmac.compare_digest(provided.encode(), expected.encode())


def bearer_token_matches(authorization: str | None, expected: str) -> bool:
    """Constant time check of an ``Authorization: Bearer <token>`` header."""
    scheme, _, token = (authorization or "").partition(" ")
    return scheme.lower() == "bearer" and tokens_match(token.strip(), expected)


# Masking ------------------------------------------------------------------------

@dataclass(frozen=True)
class MaskResult:
    text: str
    redactions: tuple[str, ...]


def _placeholder(kind: str) -> str:
    return f"[REDACTED:{kind}]"


# Words that commonly follow "password is" in a ticket without being a password.
_NOT_A_PASSWORD = (
    "not|incorrect|wrong|expired|expiring|invalid|correct|reset|locked|working|being|still|"
    "the|my|a|an|now|changed|right|fine|ok|okay|no|empty|blank|required|needed|too|also|"
    "definitely|probably|case|same|different|new|old|due|asking|rejected|accepted"
)
_PASSWORD = re.compile(
    r"(?i)\b(password|passwd|pwd|passcode|pin)((?:\s*(?:[:=]|\bis\b|\bwas\b))+\s*)"
    rf"(?!(?:{_NOT_A_PASSWORD})\b)(\S+)"
)
_VENDOR_KEYS = (
    re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"),  # Anthropic and OpenAI style keys
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),  # AWS access key ids
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),  # GitHub tokens
    re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"),  # Slack tokens
)
_CARD = re.compile(r"(?<!\d)(?:\d[ \-]?){12,18}\d(?!\d)")
_NI_NUMBER = re.compile(
    r"(?i)\b(?!BG|GB|NK|KN|TN|NT|ZZ)[A-CEGHJ-PR-TW-Z][A-CEGHJ-NPR-TW-Z] ?\d{2} ?\d{2} ?\d{2} ?[A-D]\b"
)
_HEX_TOKEN = re.compile(r"\b[a-fA-F0-9]{32,}\b")
_OPAQUE_TOKEN = re.compile(r"(?<![A-Za-z0-9_\-])[A-Za-z0-9_\-]{40,}(?![A-Za-z0-9_\-])")


def _luhn_valid(digits: str) -> bool:
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = int(char)
        if index % 2 == 1:
            value = value * 2 - 9 if value > 4 else value * 2
        total += value
    return total % 10 == 0


def mask_text(text: str) -> MaskResult:
    """Replace secrets and personal data with ``[REDACTED:type]`` placeholders."""
    found: list[str] = []

    def note(kind: str) -> str:
        if kind not in found:
            found.append(kind)
        return _placeholder(kind)

    text = _PASSWORD.sub(lambda m: m.group(1) + m.group(2) + note("password"), text)
    for pattern in _VENDOR_KEYS:
        text = pattern.sub(lambda _: note("api_key"), text)

    def card(match: re.Match[str]) -> str:
        digits = re.sub(r"\D", "", match.group(0))
        # Luhn keeps order numbers and other long references readable.
        return note("card") if 13 <= len(digits) <= 19 and _luhn_valid(digits) else match.group(0)

    text = _CARD.sub(card, text)
    text = _NI_NUMBER.sub(lambda _: note("ni_number"), text)
    text = _HEX_TOKEN.sub(lambda _: note("token"), text)

    def opaque(match: re.Match[str]) -> str:
        value = match.group(0)
        looks_random = any(c.isdigit() for c in value) and any(c.isalpha() for c in value)
        return note("token") if looks_random else value

    text = _OPAQUE_TOKEN.sub(opaque, text)
    return MaskResult(text=text, redactions=tuple(found))


_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+$")


def _mask_email(address: str) -> str:
    local, _, domain = address.partition("@")
    return f"{local[:3]}***@{domain[:4]}***"


def _mask_phone(digits: str) -> str:
    if digits.startswith("44") and len(digits) >= 11:
        return f"+44 {digits[2]}*** ***{digits[-3:]}"
    if digits.startswith("0"):
        return f"{digits[:2]}*** ***{digits[-3:]}"
    return f"+{digits[:2]}*** ***{digits[-3:]}"


def _mask_name(name: str) -> str:
    parts = name.split()
    if len(parts) == 1:
        return parts[0]
    return f"{parts[0]} {parts[-1][0]}."


def _mask_contact(value: str) -> str:
    value = value.strip()
    if _EMAIL.match(value):
        return _mask_email(value)
    digits = re.sub(r"\D", "", value)
    if len(digits) >= 7 and re.fullmatch(r"[\d\s()+\-.]+", value):
        return _mask_phone(digits)
    return _mask_name(value)


def mask_requester(contact: str | None, name: str | None = None) -> str | None:
    """Display form of a requester, for example ``Priya S. (pri***@nort***)``."""
    contact = (contact or "").strip()
    name = (name or "").strip()
    masked_contact = _mask_contact(contact) if contact else ""
    masked_name = _mask_name(name) if name else ""
    if masked_name and masked_contact and masked_name != masked_contact:
        return f"{masked_name} ({masked_contact})"
    return masked_contact or masked_name or None


# Prompt injection markers ----------------------------------------------------------

_INJECTION_MARKERS = re.compile(
    r"(?i)\b(?:ignore|disregard|forget|override)\b[^.\n]{0,40}?"
    r"\b(?:previous|prior|above|earlier|all|your|system)\b[^.\n]{0,20}?"
    r"\b(?:instructions?|prompts?|rules|directions)\b"
    r"|\b(?:reveal|print|show|repeat|output)\b[^.\n]{0,30}?\b(?:system prompt|your prompt|your instructions)\b"
    r"|\bsystem prompt\b|\bdeveloper mode\b|\bjailbreak\b"
)


def find_injection_marker(text: str) -> str | None:
    """Return the first obvious instruction override phrase, if any.

    Claude is the primary detector. This deterministic check is a second line
    of defence that still works when the model misses a case or is unavailable.
    """
    match = _INJECTION_MARKERS.search(text)
    return match.group(0) if match else None


# Client identity and rate limiting -------------------------------------------------

def client_ip(headers: Mapping[str, str], fallback: str | None) -> str:
    """The first hop of ``x-forwarded-for`` (set by Vercel), else the socket peer."""
    forwarded = headers.get("x-forwarded-for", "")
    first_hop = forwarded.split(",")[0].strip()
    return first_hop or fallback or "unknown"


def hash_client(ip: str, salt: str) -> str:
    """Keyed SHA-256 of the client IP, so rate limits work without storing IPs."""
    return hmac.new(salt.encode(), ip.encode(), hashlib.sha256).hexdigest()


class SlidingWindowLimiter:
    """Per key request limiter held in memory, so it is per serverless instance."""

    _MAX_KEYS = 10_000

    def __init__(self, limit: int, window_seconds: float) -> None:
        self.limit = limit
        self.window = window_seconds
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, now: float | None = None) -> bool:
        current = time.monotonic() if now is None else now
        cutoff = current - self.window
        with self._lock:
            if len(self._hits) > self._MAX_KEYS:
                self._drop_idle(cutoff)
            hits = self._hits.setdefault(key, deque())
            while hits and hits[0] <= cutoff:
                hits.popleft()
            if len(hits) >= self.limit:
                return False
            hits.append(current)
            return True

    def _drop_idle(self, cutoff: float) -> None:
        for key in [k for k, hits in self._hits.items() if not hits or hits[-1] <= cutoff]:
            del self._hits[key]


# HTTP headers -------------------------------------------------------------------

SECURITY_HEADERS: dict[str, str] = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; object-src 'none'; base-uri 'self'; "
        "frame-ancestors 'none'; form-action 'self'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "X-Frame-Options": "DENY",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
    "Strict-Transport-Security": "max-age=63072000; includeSubDomains",
    "Cache-Control": "no-store",
}

# MCP clients run anywhere. Auth is a bearer token, never a cookie, so any origin is safe.
MCP_CORS_HEADERS: dict[str, str] = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "POST, GET, OPTIONS",
    "Access-Control-Allow-Headers": "Authorization, Content-Type, Accept, Mcp-Protocol-Version, Mcp-Session-Id",
    "Access-Control-Max-Age": "600",
}
