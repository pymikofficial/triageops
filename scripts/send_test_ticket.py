"""Send a signed test ticket to the TriageOps inbound webhook.

Shows the HMAC authentication end to end. The body is signed with
WEBHOOK_SECRET as HMAC SHA-256 over "{timestamp}.{raw_body}" and sent with the
X-TriageOps-Timestamp and X-TriageOps-Signature headers. Standard library only.

Examples
    python scripts/send_test_ticket.py
    python scripts/send_test_ticket.py --sample 8 --url https://triageops.example.com
    python scripts/send_test_ticket.py --subject "VPN error 809" --body "The VPN fails with error 809 since this morning."
    python scripts/send_test_ticket.py --list
"""

import argparse
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from triageops.samples import SAMPLES  # noqa: E402  (standard library only)

WEBHOOK_PATH = "/api/webhooks/inbound"
TIMEOUT_SECONDS = 60


def sign(secret: str, timestamp: str, raw_body: bytes) -> str:
    digest = hmac.new(secret.encode(), timestamp.encode() + b"." + raw_body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def build_payload(args: argparse.Namespace) -> dict[str, str]:
    if args.subject or args.body:
        if not (args.subject and args.body):
            raise SystemExit("Give both --subject and --body, or use --sample.")
        payload = {"channel": args.channel, "subject": args.subject, "body": args.body}
        if args.customer:
            payload["customer"] = args.customer
        return payload
    if not 1 <= args.sample <= len(SAMPLES):
        raise SystemExit(f"--sample must be between 1 and {len(SAMPLES)}. Use --list to see them.")
    return SAMPLES[args.sample - 1].payload()


def post(url: str, secret: str, payload: dict[str, str]) -> tuple[int, str]:
    raw_body = json.dumps(payload, separators=(",", ":")).encode()
    timestamp = str(int(time.time()))
    request = urllib.request.Request(
        url,
        data=raw_body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-TriageOps-Timestamp": timestamp,
            "X-TriageOps-Signature": sign(secret, timestamp, raw_body),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def main() -> int:
    parser = argparse.ArgumentParser(description="Sign and send a test ticket to TriageOps.")
    parser.add_argument("--url", default="http://localhost:8000", help="Base URL of the deployment.")
    parser.add_argument("--sample", type=int, default=1, help=f"Sample ticket number, 1 to {len(SAMPLES)}.")
    parser.add_argument("--subject", help="Custom ticket subject, used with --body.")
    parser.add_argument("--body", help="Custom ticket body, used with --subject.")
    parser.add_argument("--customer", help="Client company for a custom ticket.")
    parser.add_argument("--channel", default="webhook", choices=["webhook", "email", "whatsapp", "portal"])
    parser.add_argument("--list", action="store_true", help="List the sample tickets and exit.")
    args = parser.parse_args()

    if args.list:
        for number, sample in enumerate(SAMPLES, start=1):
            print(f"{number}. {sample.label} ({sample.channel}, {sample.customer})")
        return 0

    secret = os.environ.get("WEBHOOK_SECRET", "").strip()
    if not secret:
        print("Set WEBHOOK_SECRET in the environment first.", file=sys.stderr)
        return 2

    url = args.url.rstrip("/") + WEBHOOK_PATH
    try:
        status, text = post(url, secret, build_payload(args))
    except urllib.error.URLError as exc:
        print(f"Could not reach {url} ({exc.reason}).", file=sys.stderr)
        return 1
    try:
        print(json.dumps(json.loads(text), indent=2))
    except ValueError:
        print(text)
    print(f"HTTP {status} from {url}", file=sys.stderr)
    return 0 if 200 <= status < 300 else 1


if __name__ == "__main__":
    sys.exit(main())
