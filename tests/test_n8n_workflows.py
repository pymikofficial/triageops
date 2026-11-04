"""The n8n workflows are well formed, and the RMM relay signs exactly like the server.

The Code node JavaScript runs in Node (skipped when ``node`` is not on PATH)
with stubbed ``$input``, ``$env``, ``$execution`` and ``require``, so the test
proves the signature byte for byte without n8n or the network.
"""

import importlib.util
import json
import re
import shutil
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from triageops import db
from triageops.security import sign_timestamped, verify_timestamped_signature

N8N_DIR = Path(__file__).resolve().parent.parent / "n8n"
WORKFLOW_FILES = sorted(N8N_DIR.glob("*.json"))
RMM_FILE = N8N_DIR / "rmm-to-triageops.json"
TRIAGEOPS_HOST = "triageops.vercel.app"
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not on PATH")

# Stable typeVersions from the research, see spec_teams_n8n.md section B0.
STABLE_VERSIONS = {
    "n8n-nodes-base.stickyNote": 1,
    "n8n-nodes-base.webhook": 2,
    "n8n-nodes-base.code": 2,
    "n8n-nodes-base.httpRequest": 4.2,
    "n8n-nodes-base.if": 2,
    "n8n-nodes-base.gmailTrigger": 1.2,
    "n8n-nodes-base.slack": 2.2,
}
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
ENV_REFERENCE = re.compile(r"\$env\.([A-Z][A-Z0-9_]*)")
FORBIDDEN_COPY = {"em dash": "—", "en dash": "–", "semicolon": ";"}
CONTRACTION = re.compile(r"\b\w+(?:n't|'re|'ve|'ll|'m|'d)\b", re.IGNORECASE)

SECRET = "relay-test-secret"
SHARED_SECRET = "shared-test-secret"

NINJA_TRIGGERED: dict[str, Any] = {
    "id": 48213377, "activityTime": 1793805137.418226, "deviceId": 1042, "severity": "MAJOR",
    "priority": "HIGH", "seriesUid": "3f6c2a9e-8b1d-4c57-9e2a-7d0b6f41c8a3", "activityType": "CONDITION",
    "statusCode": "TRIGGERED", "status": "Triggered", "activityResult": "SUCCESS",
    "sourceConfigUid": "b7e4d1c0-2f9a-4e6b-8d35-91a0c7f2e614", "sourceName": "Disk Free Space < 10%",
    "subject": "Disk free space low", "message": "Disk C: free space is 6.2% on ACME-FS01 at Café Ltd",
    "type": "Condition", "data": {"message": {"code": "condition_triggered", "params": {"volume": "C:"}}},
    "device": {
        "id": 1042, "organizationId": 17, "displayName": "ACME-FS01", "systemName": "ACME-FS01",
        "references": {"organization": {"id": 17, "name": "Acme Ltd"}},
    },
}  # fmt: skip

DATTO_TEMPLATE: dict[str, Any] = {
    "source": "datto_rmm", "event": "triggered", "alert_uid": "6f1c2b9e-3a4d-4e8f-9b21-7c5d0a3e8f41",
    "priority": "Critical", "alert_type": "Disk Usage", "alert_category": "Performance",
    "alert": "Disk usage on C: above 90%", "alert_message": "Disk C: has 4.2 GB free (95.8% used)",
    "device_uid": "0d8e5c1a-77b2-4f0e-a6c4-2b9f31d4e7aa", "device_id": "1874523",
    "device_hostname": "ACME-FS01", "site_name": "Acme Ltd - Head Office", "platform": "merlot",
}  # fmt: skip

# Datto fills [alert_message] without JSON escaping, so the quotes break the JSON.
DATTO_BROKEN_TEXT = (
    '{"source":"datto_rmm","event":"triggered","alert_uid":"0b9d8c7e-1111-4a2b-9c3d-5e6f7a8b9c0d",'
    '"priority":"High","alert_type":"Service Status","alert_category":"Service",'
    '"alert_message":"Service "Print Spooler" stopped on ACME-FS01",'
    '"device_uid":"0d8e5c1a-77b2-4f0e-a6c4-2b9f31d4e7aa","device_hostname":"ACME-FS01",'
    '"site_name":"Acme Ltd - Head Office","platform":"merlot"}'
)

DATTO_ALERTS_PAGE: dict[str, Any] = {
    "pageDetails": {"count": 2, "totalCount": 2, "prevPageUrl": None, "nextPageUrl": None},
    "alerts": [
        {
            "alertUid": "6f1c2b9e-3a4d-4e8f-9b21-7c5d0a3e8f41", "priority": "Moderate",
            "diagnostics": "Service Spooler is STOPPED", "resolved": False, "timestamp": 1762268400000,
            "alertContext": {"@class": "perf_resource_usage_ctx", "processName": "", "sample": 97.5, "type": "CPU"},
            "alertSourceInfo": {
                "deviceUid": "0d8e5c1a-77b2-4f0e-a6c4-2b9f31d4e7aa", "deviceName": "ACME-FS01",
                "siteUid": "9a3b7e2c-1f44-4c6d-8e0a-55d2b1c9f310", "siteName": "Acme Ltd - Head Office",
            },
        },
        {
            "alertUid": "b2e4d6f8-0a1c-4e3f-9d5b-7c9e1a3f5b70", "priority": "Critical",
            "diagnostics": "Device has gone offline", "resolved": False, "timestamp": 1762268400000,
            "alertContext": {"@class": "online_offline_status_ctx", "status": "OFFLINE"},
            "alertSourceInfo": {
                "deviceUid": "3c7a9e1b-5d2f-4a8c-b6e0-1f4d7a2c9e83", "deviceName": "ACME-LT042",
                "siteUid": "9a3b7e2c-1f44-4c6d-8e0a-55d2b1c9f310", "siteName": "Acme Ltd - Head Office",
            },
        },
    ],
}  # fmt: skip

# The n8n Webhook node output: parsed body plus the query string of the n8n URL.
CASES: dict[str, dict[str, Any]] = {
    "ninjaone": {"body": NINJA_TRIGGERED, "query": {"source": "ninjaone"}},
    "ninjaone_by_shape": {"body": NINJA_TRIGGERED, "query": {}},
    "datto": {"body": DATTO_TEMPLATE, "query": {"source": "datto"}},
    "datto_by_shape": {"body": DATTO_TEMPLATE, "query": {}},
    "datto_broken_string": {"body": DATTO_BROKEN_TEXT, "query": {"source": "datto"}},
    "datto_alerts_list": {"body": DATTO_ALERTS_PAGE, "query": {}},
}
EXPECTED_SOURCE = {
    "ninjaone": "ninjaone", "ninjaone_by_shape": "ninjaone", "datto": "datto", "datto_by_shape": "datto",
    "datto_broken_string": "datto", "datto_alerts_list": "datto",
}  # fmt: skip


# Helpers -------------------------------------------------------------------------

def load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def nodes_by_name(workflow: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {node["name"]: node for node in workflow["nodes"]}


def nodes_of_type(workflow: dict[str, Any], node_type: str) -> list[dict[str, Any]]:
    return [node for node in workflow["nodes"] if node["type"] == node_type]


def sticky_text(workflow: dict[str, Any]) -> str:
    return "\n".join(node["parameters"]["content"] for node in nodes_of_type(workflow, "n8n-nodes-base.stickyNote"))


def code_of(workflow: dict[str, Any], name: str) -> str:
    return nodes_by_name(workflow)[name]["parameters"]["jsCode"]


def output_count(node: dict[str, Any]) -> int:
    """Main outputs a node exposes, including the error output when it is switched on."""
    if node["type"] == "n8n-nodes-base.stickyNote":
        return 0
    base = 2 if node["type"] == "n8n-nodes-base.if" else 1
    return base + (1 if node.get("onError") == "continueErrorOutput" else 0)


def copy_problems(text: str) -> list[str]:
    problems = [name for name, char in FORBIDDEN_COPY.items() if char in text]
    return problems + [f"contraction {match}" for match in CONTRACTION.findall(text)]


def js_comments(code: str) -> str:
    return "\n".join(match.group(1) for match in re.finditer(r"^\s*//(.*)$", code, re.MULTILINE))


HARNESS = """
const fs = require('fs');
const crypto = require('crypto');
const spec = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const items = spec.items.map((json) => ({ json }));
const $input = { all: () => items, first: () => items[0] };
const $env = spec.env;
const $execution = { id: spec.executionId };
const allowed = { crypto };
function sandboxRequire(name) {
  if (!(name in allowed)) throw new Error('module ' + name + ' is not allowed');
  return allowed[name];
}
const run = new Function('$input', '$env', '$execution', 'require', 'return (async () => {\\n' + spec.code + '\\n})();');
run($input, $env, $execution, sandboxRequire).then(
  (out) => process.stdout.write(JSON.stringify(out)),
  (error) => { process.stderr.write(String(error && error.message || error)); process.exit(3); },
);
"""


def run_code(tmp_path: Path, code: str, items: list[dict[str, Any]], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Run n8n Code node JavaScript in Node with stubbed globals. Returns the finished process."""
    harness = tmp_path / "harness.js"
    harness.write_text(HARNESS, encoding="utf-8")
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps({"code": code, "items": items, "env": env, "executionId": "4711"}), encoding="utf-8")
    return subprocess.run([NODE, str(harness), str(spec)], capture_output=True, text=True, timeout=30, check=False)


def run_ok(tmp_path: Path, code: str, items: list[dict[str, Any]], env: dict[str, str]) -> list[dict[str, Any]]:
    result = run_code(tmp_path, code, items, env)
    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    assert isinstance(output, list) and all(set(entry) == {"json"} for entry in output)
    return [entry["json"] for entry in output]


def sign_items(tmp_path: Path, item: dict[str, Any], env: dict[str, str] | None = None) -> list[dict[str, Any]]:
    code = code_of(load(RMM_FILE), "Sign RMM alerts")
    return run_ok(tmp_path, code, [{"headers": {}, "params": {}, **item}], env or {"TRIAGEOPS_RMM_WEBHOOK_SECRET": SECRET})


# Structure of both workflows -----------------------------------------------------------

def test_both_workflow_files_exist() -> None:
    assert {path.name for path in WORKFLOW_FILES} >= {"gmail-to-triageops.json", "rmm-to-triageops.json"}


@pytest.mark.parametrize("path", WORKFLOW_FILES, ids=lambda path: path.name)
def test_workflow_parses_with_unique_nodes_and_valid_connections(path: Path) -> None:
    workflow = load(path)
    assert workflow["active"] is False
    assert workflow["settings"]["executionOrder"] == "v1"
    names = [node["name"] for node in workflow["nodes"]]
    assert len(names) == len(set(names)), "node names must be unique"
    ids = [node["id"] for node in workflow["nodes"]]
    assert len(ids) == len(set(ids)) and all(UUID.match(node_id) for node_id in ids)
    nodes = nodes_by_name(workflow)
    for source, outputs in workflow["connections"].items():
        assert source in nodes, f"connection from unknown node {source}"
        assert set(outputs) == {"main"}
        assert len(outputs["main"]) <= output_count(nodes[source]), f"{source} has no output that high"
        for targets in outputs["main"]:
            for target in targets:
                assert target["node"] in nodes, f"connection to unknown node {target['node']}"
                assert target["type"] == "main" and target["index"] == 0


@pytest.mark.parametrize("path", WORKFLOW_FILES, ids=lambda path: path.name)
def test_workflow_uses_stable_node_versions(path: Path) -> None:
    for node in load(path)["nodes"]:
        assert STABLE_VERSIONS.get(node["type"]) == node["typeVersion"], node["name"]


@pytest.mark.parametrize("path", WORKFLOW_FILES, ids=lambda path: path.name)
def test_sticky_note_names_every_environment_variable(path: Path) -> None:
    workflow = load(path)
    notes = sticky_text(workflow)
    used = set(ENV_REFERENCE.findall(json.dumps([node["parameters"] for node in workflow["nodes"]])))
    assert used, "the workflow reads at least one secret from the n8n environment"
    for name in sorted(used):
        assert f"`{name}`" in notes, f"{name} is not explained in the sticky note"
    for node in nodes_of_type(workflow, "n8n-nodes-base.code"):
        if "$env." in node["parameters"]["jsCode"]:
            assert "N8N_BLOCK_ENV_ACCESS_IN_NODE=false" in notes
        if "require('crypto')" in node["parameters"]["jsCode"]:
            assert "NODE_FUNCTION_ALLOW_BUILTIN=crypto" in notes


@pytest.mark.parametrize("path", WORKFLOW_FILES, ids=lambda path: path.name)
def test_workflow_holds_no_secrets(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    for node in load(path)["nodes"]:
        assert not node.get("credentials"), f"{node['name']} carries a credential reference"
    assert not re.search(r"\b[0-9a-fA-F]{32,}\b", text), "a long hex string looks like a secret"
    assert not re.search(r"\b(?:sk-[A-Za-z0-9_\-]{16,}|xox[abprs]-|gh[pousr]_[A-Za-z0-9]{20,})", text)
    assert not re.search(r"(?i)bearer\s+[A-Za-z0-9._\-]{12,}|[?&]sig=", text)


# The RMM relay workflow ----------------------------------------------------------

def test_rmm_webhook_is_post_with_header_auth_and_responds_immediately() -> None:
    workflow = load(RMM_FILE)
    [webhook] = nodes_of_type(workflow, "n8n-nodes-base.webhook")
    params = webhook["parameters"]
    assert params["httpMethod"] == "POST"
    assert params["path"] == "triageops-rmm"
    assert params["authentication"] == "headerAuth"
    assert params["responseMode"] == "onReceived"
    assert UUID.match(webhook["webhookId"])
    assert not nodes_of_type(workflow, "n8n-nodes-base.respondToWebhook"), "onReceived cannot coexist with a Respond node"
    assert workflow["connections"][webhook["name"]]["main"][0][0]["node"] == "Sign RMM alerts"


def test_rmm_http_node_posts_signed_raw_body_with_retries() -> None:
    workflow = load(RMM_FILE)
    node = nodes_by_name(workflow)["POST to TriageOps"]
    params = node["parameters"]
    assert params["method"] == "POST"
    assert params["url"] == f"=https://{TRIAGEOPS_HOST}/api/webhooks/rmm?source={{{{ $json.source }}}}"
    headers = {header["name"]: header["value"] for header in params["headerParameters"]["parameters"]}
    assert params["sendHeaders"] is True
    assert headers == {"X-TriageOps-Timestamp": "={{ $json.timestamp }}", "X-TriageOps-Signature": "={{ $json.signature }}"}
    assert params["sendBody"] is True
    assert (params["contentType"], params["rawContentType"], params["body"]) == ("raw", "application/json", "={{ $json.rawBody }}")
    assert params["options"]["timeout"] == 60000
    assert (node["retryOnFail"], node["maxTries"], node["waitBetweenTries"]) == (True, 3, 5000)
    # Three full timeouts plus two waits stay inside the 300 second replay window.
    assert node["maxTries"] * params["options"]["timeout"] + (node["maxTries"] - 1) * node["waitBetweenTries"] < 300_000


def test_rmm_failures_reach_the_fallback_channel() -> None:
    workflow = load(RMM_FILE)
    nodes = nodes_by_name(workflow)
    connections = workflow["connections"]
    for name in ("Sign RMM alerts", "POST to TriageOps"):
        assert nodes[name]["onError"] == "continueErrorOutput"
        assert [target["node"] for target in connections[name]["main"][1]] == ["Build fallback message"]
    assert connections["POST to TriageOps"]["main"][0] == []
    assert connections["Build fallback message"]["main"][0][0]["node"] == "Post fallback alert"
    fallback = nodes["Post fallback alert"]
    assert fallback["type"] == "n8n-nodes-base.httpRequest"
    assert fallback["parameters"]["method"] == "POST"
    assert fallback["parameters"]["url"] == "={{ $env.TRIAGEOPS_FALLBACK_WEBHOOK_URL }}"
    assert fallback["parameters"]["body"] == "={{ $json.fallbackBody }}"
    assert fallback["retryOnFail"] is True and "onError" not in fallback, "a failed fallback must fail the execution"


def test_rmm_workflow_names_no_real_host_but_triageops() -> None:
    text = RMM_FILE.read_text(encoding="utf-8")
    hosts = set(re.findall(r"(?i)\bhttps?://([^/\s\"'`?#:]+)", text))
    assert hosts <= {TRIAGEOPS_HOST, "adaptivecards.io"}, hosts
    assert not re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", text), "no email addresses"
    # Host names used only to choose the fallback body format.
    allowed = {TRIAGEOPS_HOST, "adaptivecards.io", "hooks.slack.com", "discord.com", "discordapp.com",
               "webhook.office.com", "logic.azure.com", "powerplatform.com"}  # fmt: skip
    found = {match.lower() for match in re.findall(r"\b(?:[a-z0-9-]+\.)+(?:com|net|org|io|app|dev|cloud|uk)\b", text, re.I)}
    assert found <= allowed, found - allowed


def test_rmm_sticky_note_covers_setup_in_house_style() -> None:
    workflow = load(RMM_FILE)
    notes = sticky_text(workflow)
    assert copy_problems(notes) == []
    for phrase in ("Header Auth", "?source=ninjaone", "?source=datto", "PUT /v2/webhook", "Global Webhooks",
                   "text/plain", "event=resolved", "priority=High", "/webhook/triageops-rmm", TRIAGEOPS_HOST):
        assert phrase in notes, phrase  # fmt: skip
    for node in nodes_of_type(workflow, "n8n-nodes-base.code"):
        assert copy_problems(js_comments(node["parameters"]["jsCode"])) == [], node["name"]


parsers_missing = importlib.util.find_spec("triageops.rmm.parsers") is None


# The Code nodes, run in Node ---------------------------------------------------------

@needs_node
@pytest.mark.parametrize("case", list(CASES))
def test_sign_node_signs_exactly_like_the_server(tmp_path: Path, case: str) -> None:
    outputs = sign_items(tmp_path, CASES[case])
    assert outputs
    for out in outputs:
        raw = out["rawBody"].encode("utf-8")
        assert out["signature"] == sign_timestamped(SECRET, out["timestamp"], raw)
        verify_timestamped_signature(SECRET, out["timestamp"], out["signature"], raw)
        assert abs(int(out["timestamp"]) - time.time()) < 60
        assert out["source"] == EXPECTED_SOURCE[case]
        assert isinstance(json.loads(raw), dict)


@needs_node
def test_sign_node_forwards_vendor_bodies_unchanged(tmp_path: Path) -> None:
    [ninja] = sign_items(tmp_path, CASES["ninjaone"])
    assert json.loads(ninja["rawBody"]) == NINJA_TRIGGERED
    assert "Café Ltd" in ninja["rawBody"], "non ASCII text is signed as UTF-8 bytes"
    assert (ninja["device"], ninja["condition"]) == ("ACME-FS01", "Disk Free Space < 10%")
    [datto] = sign_items(tmp_path, CASES["datto"])
    assert json.loads(datto["rawBody"]) == DATTO_TEMPLATE
    assert (datto["device"], datto["condition"]) == ("ACME-FS01", "Disk Usage")


@needs_node
def test_sign_node_rescues_a_datto_body_that_is_not_valid_json(tmp_path: Path) -> None:
    [out] = sign_items(tmp_path, CASES["datto_broken_string"])
    body = json.loads(out["rawBody"])
    assert body["alert_message"] == DATTO_BROKEN_TEXT
    assert body["device_uid"] == "0d8e5c1a-77b2-4f0e-a6c4-2b9f31d4e7aa"
    assert (body["device_hostname"], body["alert_type"], body["priority"], body["event"]) == (
        "ACME-FS01", "Service Status", "High", "triggered",
    )  # fmt: skip
    assert out["device"] == "ACME-FS01"


@needs_node
def test_sign_node_keeps_text_that_has_nothing_to_rescue(tmp_path: Path) -> None:
    [out] = sign_items(tmp_path, {"body": "Disk full on server SRV01", "query": {}})
    assert json.loads(out["rawBody"]) == {"alert_message": "Disk full on server SRV01"}
    assert out["source"] == "datto"
    [parsed] = sign_items(tmp_path, {"body": "\ufeff" + json.dumps(DATTO_TEMPLATE), "query": {}})
    assert json.loads(parsed["rawBody"]) == DATTO_TEMPLATE, "a text/plain body that is valid JSON is parsed"


@needs_node
def test_sign_node_splits_a_datto_alerts_list(tmp_path: Path) -> None:
    outputs = sign_items(tmp_path, {"body": DATTO_ALERTS_PAGE, "query": {"source": "datto", "event": "resolved"}})
    assert [json.loads(out["rawBody"]) for out in outputs] == DATTO_ALERTS_PAGE["alerts"], "resolved flags win over the URL"
    assert [out["device"] for out in outputs] == ["ACME-FS01", "ACME-LT042"]
    assert sign_items(tmp_path, {"body": {"alerts": []}, "query": {"source": "datto"}}) == []


@needs_node
def test_sign_node_merges_datto_query_parameters_only_when_missing(tmp_path: Path) -> None:
    body = {key: value for key, value in DATTO_TEMPLATE.items() if key not in ("event", "priority")}
    body["Priority"] = "[alert_priority]"
    [out] = sign_items(tmp_path, {"body": body, "query": {"source": "datto", "event": "Resolved", "priority": "high"}})
    merged = json.loads(out["rawBody"])
    assert (merged["event"], merged["Priority"]) == ("resolved", "High")
    assert "priority" not in merged, "an unexpanded key is filled in place, not duplicated"
    [kept] = sign_items(tmp_path, {"body": DATTO_TEMPLATE, "query": {"event": "resolved", "priority": "Low"}})
    assert json.loads(kept["rawBody"]) == DATTO_TEMPLATE
    [ignored] = sign_items(tmp_path, {"body": body, "query": {"source": "datto", "event": "bogus", "priority": "urgent"}})
    assert json.loads(ignored["rawBody"]) == body
    [ninja] = sign_items(tmp_path, {"body": NINJA_TRIGGERED, "query": {"event": "resolved"}})
    assert json.loads(ninja["rawBody"]) == NINJA_TRIGGERED, "query merging is for Datto only"


@needs_node
@pytest.mark.parametrize(
    ("query", "body", "expected"),
    [
        ({"source": " Ninja "}, {"foo": 1}, "ninjaone"),
        ({"source": "dattormm"}, NINJA_TRIGGERED, "datto"),
        ({"source": "other"}, NINJA_TRIGGERED, "unknown"),
        ({}, {"statusCode": "TRIGGERED"}, "ninjaone"),
        ({}, {"deviceUid": "x"}, "datto"),
        ({}, {"Alert_UID": "x"}, "datto"),
        ({}, {"source": "datto_rmm"}, ""),
        ({}, {"foo": "bar"}, ""),
    ],
)
def test_sign_node_detects_the_vendor(tmp_path: Path, query: dict[str, str], body: dict[str, Any], expected: str) -> None:
    [out] = sign_items(tmp_path, {"body": body, "query": query})
    assert out["source"] == expected
    if parsers_missing:
        return
    from triageops.rmm.parsers import RmmPayloadError, detect_vendor

    try:
        server = detect_vendor(json.loads(out["rawBody"]), out["source"])
    except RmmPayloadError:
        server = "rejected"
    assert server == (expected or "rejected").replace("unknown", "rejected"), "the relay and the server agree"


@needs_node
def test_sign_node_prefers_the_rmm_secret_and_needs_one(tmp_path: Path) -> None:
    env = {"TRIAGEOPS_RMM_WEBHOOK_SECRET": SECRET, "TRIAGEOPS_WEBHOOK_SECRET": SHARED_SECRET}
    [out] = sign_items(tmp_path, CASES["datto"], env)
    assert out["signature"] == sign_timestamped(SECRET, out["timestamp"], out["rawBody"].encode())
    [shared] = sign_items(tmp_path, CASES["datto"], {"TRIAGEOPS_WEBHOOK_SECRET": SHARED_SECRET})
    assert shared["signature"] == sign_timestamped(SHARED_SECRET, shared["timestamp"], shared["rawBody"].encode())
    code = code_of(load(RMM_FILE), "Sign RMM alerts")
    result = run_code(tmp_path, code, [{"body": DATTO_TEMPLATE, "query": {}}], {})
    assert result.returncode != 0 and "TRIAGEOPS_RMM_WEBHOOK_SECRET" in result.stderr


@needs_node
@pytest.mark.parametrize(
    ("url", "kind"),
    [
        ("https://hooks.slack.com/services/T000/B000/XXXX", "slack"),
        ("https://default0000.00.environment.api.powerplatform.com/powerautomate/automations/direct/workflows/1/triggers/manual/paths/invoke", "teams"),
        ("https://discord.com/api/webhooks/1/abc", "discord"),
        ("https://alerts.example.invalid/hook", "generic"),
    ],
)  # fmt: skip
def test_fallback_node_builds_a_plain_message_for_the_channel(tmp_path: Path, url: str, kind: str) -> None:
    code = code_of(load(RMM_FILE), "Build fallback message")
    failed = {"rawBody": json.dumps(NINJA_TRIGGERED), "timestamp": "1", "signature": "sha256=00", "source": "ninjaone",
              "device": "ACME-FS01", "condition": "Disk Free Space < 10%",
              "error": {"message": "422 - The request body must be valid JSON."}}  # fmt: skip
    [out] = run_ok(tmp_path, code, [failed], {"TRIAGEOPS_FALLBACK_WEBHOOK_URL": url})
    payload = json.loads(out["fallbackBody"])
    if kind == "teams":
        card = payload["attachments"][0]["content"]
        assert payload["type"] == "message" and card["version"] == "1.4"
        text = card["body"][1]["text"]
        assert "Disk Free Space \\< 10%" in text
    elif kind == "slack":
        text = payload["text"]
        assert set(payload) == {"text"} and "Disk Free Space &lt; 10%" in text
    elif kind == "discord":
        text = payload["content"]
        assert set(payload) == {"content"}
    else:
        text = payload["text"]
        assert payload == {"text": text, "content": text}
    assert "NinjaOne alert could not be delivered to TriageOps" in text
    assert "ACME-FS01" in text and "4711" in text and "must be valid JSON" in text
    assert NINJA_TRIGGERED["message"] not in text, "the alert text never goes to the chat channel"
    plain = text.replace("\\", "").replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
    assert copy_problems(plain) == []


@needs_node
def test_fallback_node_reports_a_signing_failure_and_needs_a_url(tmp_path: Path) -> None:
    code = code_of(load(RMM_FILE), "Build fallback message")
    env = {"TRIAGEOPS_FALLBACK_WEBHOOK_URL": "https://alerts.example.invalid/hook"}
    [out] = run_ok(tmp_path, code, [{"error": "TRIAGEOPS_WEBHOOK_SECRET is not set"}], env)
    assert json.loads(out["fallbackBody"])["text"].startswith("An unknown RMM alert could not be signed or sent")
    for bad in ({}, {"TRIAGEOPS_FALLBACK_WEBHOOK_URL": "http://alerts.example.invalid/hook"}):
        result = run_code(tmp_path, code, [{"error": "x"}], bad)
        assert result.returncode != 0 and "TRIAGEOPS_FALLBACK_WEBHOOK_URL" in result.stderr


# End to end through the TriageOps route ----------------------------------------------------

intake_missing = importlib.util.find_spec("triageops.rmm.intake") is None


@needs_node
@pytest.mark.skipif(intake_missing, reason="triageops/rmm/intake.py (Workstream A) does not exist yet")
@pytest.mark.parametrize("case", list(CASES))
def test_signed_output_is_accepted_by_the_rmm_route(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str) -> None:
    from triageops.app import app
    from triageops.rmm import config as rmm_config
    from triageops.rmm import intake, routes

    seen: list[Any] = []

    def fake_handle_alert(conn: object, alert: Any, **kwargs: Any) -> Any:
        seen.append(alert)
        return intake.RmmOutcome(action="ignored", ticket=None, occurrences=0, priority_hint=None)

    @contextmanager
    def fake_connect():
        yield object()

    for module in (intake, routes):
        if hasattr(module, "handle_alert"):
            monkeypatch.setattr(module, "handle_alert", fake_handle_alert)
    monkeypatch.setattr(db, "connect", fake_connect)
    secret = rmm_config.signing_secret()
    assert secret, "the server has a signing secret in tests"

    outputs = sign_items(tmp_path, CASES[case], {"TRIAGEOPS_RMM_WEBHOOK_SECRET": secret})
    client = TestClient(app)
    for out in outputs:
        headers = {"Content-Type": "application/json", "X-TriageOps-Timestamp": out["timestamp"],
                   "X-TriageOps-Signature": out["signature"]}  # fmt: skip
        response = client.post("/api/webhooks/rmm", params={"source": out["source"]}, content=out["rawBody"].encode(), headers=headers)
        assert response.status_code != 401, response.text
        tampered = client.post("/api/webhooks/rmm", params={"source": out["source"]}, content=out["rawBody"].encode() + b" ", headers=headers)
        assert tampered.status_code == 401
        assert response.status_code == 200, response.text
    assert [alert.vendor for alert in seen] == [EXPECTED_SOURCE[case]] * len(outputs)
