"""NinjaOne and Datto RMM parsing, severity mapping, de-duplication keys and ticket text."""

import json
import re
from datetime import UTC, datetime
from typing import Any

import pytest
from fakes_integrations import hashed_key, load_rmm_fixture

from triageops.rmm.models import (
    SEVERITY_TO_PRIORITY,
    RmmAlert,
    priority_hint,
    priority_hint_reason,
)
from triageops.rmm.parsers import (
    MESSAGE_MAX_CHARS,
    RmmIgnored,
    RmmPayloadError,
    detect_vendor,
    parse_alert,
    parse_datto,
    parse_ninjaone,
    parse_time,
)
from triageops.rmm.tickets import (
    BODY_MAX_CHARS,
    SUBJECT_MAX_CHARS,
    build_subject,
    to_inbound,
)


def payload(name: str, **overrides: Any) -> dict[str, Any]:
    """A fixture with fields replaced. An override of None removes the key."""
    data = load_rmm_fixture(name)
    for key, value in overrides.items():
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
    return data


def make_alert(**overrides: Any) -> RmmAlert:
    fields: dict[str, Any] = {
        "vendor": "ninjaone",
        "alert_id": "1:TRIGGERED",
        "device_id": "1042",
        "device_name": "ACME-FS01",
        "customer": "Acme Ltd",
        "condition": "b7e4d1c0",
        "condition_name": "Disk Free Space < 10%",
        "severity": "major",
        "state": "triggered",
        "message": "Disk C: free space is 6.2% on ACME-FS01",
        "occurred_at": datetime(2026, 11, 4, 15, 12, tzinfo=UTC),
    }
    return RmmAlert(**{**fields, **overrides})


# NinjaOne -------------------------------------------------------------------------

def test_ninjaone_triggered_activity() -> None:
    alert = parse_ninjaone(load_rmm_fixture("ninjaone_triggered"))
    assert alert.vendor == "ninjaone"
    assert alert.state == "triggered"
    assert alert.alert_id == "48213377:TRIGGERED"
    assert alert.source_id == "rmm:ninjaone:48213377:TRIGGERED"
    assert (alert.device_id, alert.device_name, alert.customer) == ("1042", "ACME-FS01", "Acme Ltd")
    assert alert.condition == "b7e4d1c0-2f9a-4e6b-8d35-91a0c7f2e614"
    assert alert.condition_name == "Disk Free Space < 10%"
    assert alert.message.startswith("Disk C: free space is 6.2%")
    assert (alert.severity, priority_hint(alert)) == ("major", "P2")
    assert priority_hint_reason(alert) == "NinjaOne severity Major"
    assert alert.occurred_at == datetime(2026, 11, 4, 15, 12, 17, 418226, tzinfo=UTC)
    assert alert.dedupe_key == hashed_key("ninjaone:1042:b7e4d1c0-2f9a-4e6b-8d35-91a0c7f2e614")


def test_ninjaone_reset_shares_the_key_but_not_the_delivery_id() -> None:
    triggered = parse_ninjaone(load_rmm_fixture("ninjaone_triggered"))
    reset = parse_ninjaone(load_rmm_fixture("ninjaone_reset"))
    assert reset.state == "reset"
    assert reset.alert_id == "48219902:RESET"
    assert reset.dedupe_key == triggered.dedupe_key
    assert reset.customer is None, "the reset fixture has no organisation reference"


def test_ninjaone_console_channel_minimal_shape() -> None:
    alert = parse_ninjaone(load_rmm_fixture("ninjaone_console_minimal"))
    assert alert.device_name == "device 67890"
    assert alert.customer is None
    assert (alert.severity, priority_hint(alert)) == ("critical", "P1")
    assert alert.occurred_at == datetime(2023, 12, 21, 1, 50, 56, tzinfo=UTC)
    assert alert.condition_name == "Device Offline"


@pytest.mark.parametrize("code", ["RESET", "RESET_BY_PSA_TICKET_CALLBACK", "reset"])
def test_ninjaone_clear_codes(code: str) -> None:
    assert parse_ninjaone(payload("ninjaone_triggered", statusCode=code)).state == "reset"


def test_ninjaone_acknowledged_is_ignored_not_an_error() -> None:
    with pytest.raises(RmmIgnored) as caught:
        parse_alert(load_rmm_fixture("ninjaone_acknowledged"))
    assert caught.value.vendor == "ninjaone"
    assert caught.value.reason == "NinjaOne status ACKNOWLEDGED does not raise or clear an alert"


@pytest.mark.parametrize("code", ["DISABLED", "EVALUATION_FAILURE", "SOMETHING_NEW"])
def test_other_ninjaone_status_codes_are_ignored(code: str) -> None:
    with pytest.raises(RmmIgnored):
        parse_ninjaone(payload("ninjaone_triggered", statusCode=code))


def test_ninjaone_odd_status_text_is_not_echoed() -> None:
    with pytest.raises(RmmIgnored) as caught:
        parse_ninjaone(payload("ninjaone_triggered", statusCode="<script>alert(1)</script>"))
    assert "script" not in caught.value.reason


def test_ninjaone_status_label_is_a_fallback_for_a_missing_status_code() -> None:
    assert parse_ninjaone(payload("ninjaone_triggered", statusCode=None, status="Triggered")).state == "triggered"
    with pytest.raises(RmmIgnored, match="no status"):
        parse_ninjaone(payload("ninjaone_triggered", statusCode=None, status=None))


@pytest.mark.parametrize("activity", ["CONDITION_ACTION", "CONDITION_ACTIONSET", "condition"])
def test_ninjaone_condition_activities_are_alerts(activity: str) -> None:
    assert parse_ninjaone(payload("ninjaone_triggered", activityType=activity)).state == "triggered"


def test_ninjaone_non_condition_activity_is_ignored_before_any_field_check() -> None:
    system = {"id": 9, "activityType": "SYSTEM", "statusCode": "USER_LOGGED_IN", "message": "A user signed in"}
    with pytest.raises(RmmIgnored, match="not an alert condition"):
        parse_alert(system)


@pytest.mark.parametrize(
    ("severity", "priority", "expected", "hint", "reason"),
    [
        ("CRITICAL", "LOW", "critical", "P1", "NinjaOne severity Critical"),
        ("MAJOR", None, "major", "P2", "NinjaOne severity Major"),
        ("MODERATE", "HIGH", "moderate", "P3", "NinjaOne severity Moderate"),
        ("MINOR", "HIGH", "minor", "P4", "NinjaOne severity Minor"),
        ("NONE", "HIGH", "major", "P2", "NinjaOne priority High"),
        (None, "MEDIUM", "moderate", "P3", "NinjaOne priority Medium"),
        (None, "LOW", "minor", "P4", "NinjaOne priority Low"),
        ("NONE", "NONE", "info", "P4", "NinjaOne severity None"),
        ("NONE", None, "info", "P4", "NinjaOne severity None"),
        (None, "NONE", "info", "P4", "NinjaOne priority None"),
        ("critical", None, "critical", "P1", "NinjaOne severity Critical"),
        (None, None, "unknown", None, None),
        ("SEVERE", 7, "unknown", None, None),
    ],
)
def test_ninjaone_severity_then_priority(
    severity: str | None, priority: Any, expected: str, hint: str | None, reason: str | None
) -> None:
    alert = parse_ninjaone(payload("ninjaone_triggered", severity=severity, priority=priority))
    assert (alert.severity, priority_hint(alert), priority_hint_reason(alert)) == (expected, hint, reason)


def test_ninjaone_condition_identity_falls_back() -> None:
    base = payload("ninjaone_triggered", sourceConfigUid=None)
    assert parse_ninjaone(base).condition == "Disk Free Space < 10%"
    base.pop("sourceName")
    assert parse_ninjaone(base).condition == "Disk free space low"
    base.pop("subject")
    from_message = parse_ninjaone(base)
    assert from_message.condition == "disk c: free space is .% (below % threshold) on acme-fs"
    assert from_message.condition_name is None
    base.pop("message")
    with pytest.raises(RmmPayloadError, match="sourceConfigUid"):
        parse_ninjaone(base)


def test_ninjaone_device_identity_is_required() -> None:
    no_device = payload("ninjaone_triggered", deviceId=None, device=None)
    with pytest.raises(RmmPayloadError, match="deviceId"):
        parse_ninjaone(no_device)
    assert parse_ninjaone(payload("ninjaone_triggered", deviceId=None)).device_id == "1042"


def test_ninjaone_wrong_types_are_skipped() -> None:
    alert = parse_ninjaone(
        payload(
            "ninjaone_triggered",
            sourceConfigUid={"nested": True},
            sourceName=["Disk"],
            message=12,
            device="not an object",
            activityTime="soon",
            id=True,
        )
    )
    assert alert.condition == "Disk free space low"
    assert alert.message == "Disk free space low"
    assert alert.device_name == "device 1042"
    assert alert.customer is None
    assert alert.occurred_at is None
    assert alert.alert_id is None and alert.source_id is None
    with pytest.raises(RmmPayloadError):
        parse_ninjaone(payload("ninjaone_triggered", deviceId={"id": 1}, device=None))


def test_ninjaone_text_is_stripped_and_capped() -> None:
    alert = parse_ninjaone(
        payload(
            "ninjaone_triggered",
            message="  " + "x" * 5000 + "  ",
            sourceName="  Disk \n\t Free   Space " + "y" * 400,
            sourceConfigUid="u" * 500,
            deviceId="  " + "9" * 300,
        )
    )
    assert len(alert.message) == MESSAGE_MAX_CHARS
    assert alert.condition_name.startswith("Disk Free Space y") and len(alert.condition_name) == 160
    assert len(alert.condition) == 120
    assert alert.device_id == "9" * 120


def test_ninjaone_numeric_strings_and_device_name_order() -> None:
    data = payload("ninjaone_triggered", deviceId="1042")
    data["device"] = {"systemName": "  ACME-FS01-SYS ", "dnsName": "acme.local"}
    alert = parse_ninjaone(data)
    assert (alert.device_id, alert.device_name) == ("1042", "ACME-FS01-SYS")
    data["device"] = {"dnsName": "acme-fs01.acme.local"}
    assert parse_ninjaone(data).device_name == "acme-fs01.acme.local"


# Datto RMM ------------------------------------------------------------------------

def test_datto_raised_template() -> None:
    alert = parse_datto(load_rmm_fixture("datto_raised"))
    assert alert.vendor == "datto"
    assert alert.state == "triggered"
    assert alert.alert_id == "6f1c2b9e-3a4d-4e8f-9b21-7c5d0a3e8f41:raised"
    assert alert.source_id == "rmm:datto:6f1c2b9e-3a4d-4e8f-9b21-7c5d0a3e8f41:raised"
    assert alert.device_id == "0d8e5c1a-77b2-4f0e-a6c4-2b9f31d4e7aa"
    assert (alert.device_name, alert.customer) == ("ACME-FS01", "Acme Ltd - Head Office")
    assert (alert.condition, alert.condition_name) == ("Disk Usage/Performance", "Disk Usage")
    assert alert.message == "Disk C: has 4.2 GB free (95.8% used)"
    assert (alert.severity, priority_hint(alert)) == ("critical", "P1")
    assert priority_hint_reason(alert) == "Datto RMM priority Critical"
    assert alert.occurred_at is None
    assert alert.dedupe_key == hashed_key("datto:0d8e5c1a-77b2-4f0e-a6c4-2b9f31d4e7aa:disk usage/performance")


def test_datto_resolved_template_is_a_reset_with_its_own_delivery_id() -> None:
    raised = parse_datto(load_rmm_fixture("datto_raised"))
    resolved = parse_datto(load_rmm_fixture("datto_resolved"))
    assert resolved.state == "reset"
    assert resolved.alert_id == "6f1c2b9e-3a4d-4e8f-9b21-7c5d0a3e8f41:resolved"
    assert resolved.dedupe_key == raised.dedupe_key


def test_datto_api_v2_alert_object() -> None:
    alert = parse_alert(load_rmm_fixture("datto_api_alert"))
    assert alert.vendor == "datto"
    assert alert.state == "triggered"
    assert alert.alert_id == "b2e4d6f8-0a1c-4e3f-9d5b-7c9e1a3f5b70:raised"
    assert (alert.device_id, alert.device_name) == ("3c7a9e1b-5d2f-4a8c-b6e0-1f4d7a2c9e83", "ACME-LT042")
    assert alert.customer == "Acme Ltd - Head Office"
    assert alert.condition == "online_offline_status_ctx"
    assert alert.condition_name == "Device has gone offline"
    assert alert.message == "Device has gone offline"
    assert alert.occurred_at == datetime(2026, 11, 4, 15, 0, tzinfo=UTC)
    assert priority_hint(alert) == "P1"
    assert parse_datto(payload("datto_api_alert", resolved=True)).state == "reset"


def test_datto_api_context_discriminators_keep_conditions_apart() -> None:
    cpu = payload("datto_api_alert", alertContext={"@class": "perf_resource_usage_ctx", "processName": "", "sample": 97.5, "type": "CPU"})
    memory = payload("datto_api_alert", alertContext={"@class": "perf_resource_usage_ctx", "sample": 91.0, "type": "MEMORY"})
    assert parse_datto(cpu).condition == "perf_resource_usage_ctx:cpu"
    assert parse_datto(memory).condition == "perf_resource_usage_ctx:memory"
    label_only = payload("datto_api_alert", diagnostics=None, alertContext={"@class": "perf_resource_usage_ctx", "type": "CPU"})
    assert parse_datto(label_only).condition_name == "Resource usage"


def test_datto_test_send_with_unexpanded_placeholders_is_rejected() -> None:
    with pytest.raises(RmmPayloadError, match="Unexpanded placeholders count as missing"):
        parse_alert(load_rmm_fixture("datto_test_send_unexpanded"))


def test_datto_unexpanded_priority_gives_no_hint() -> None:
    alert = parse_alert(load_rmm_fixture("datto_priority_unexpanded"))
    assert alert.severity == "unknown"
    assert priority_hint(alert) is None and priority_hint_reason(alert) is None
    assert alert.customer is None, "an unexpanded [sitename] counts as missing"
    assert alert.condition == "Service Status/Service"


def test_datto_keys_ignore_case_and_aliases() -> None:
    alert = parse_datto(
        {
            "AlertUID": "abc-123",
            "DeviceUid": "dev-9",
            "HostName": "ACME-PC07",
            "SiteName": "Acme Ltd",
            "AlertMessage": "CPU above 95%",
            "Alert_Type": "CPU Usage",
            "PRIORITY": "high",
        }
    )
    assert (alert.alert_id, alert.device_id, alert.device_name, alert.customer) == (
        "abc-123:raised", "dev-9", "ACME-PC07", "Acme Ltd",
    )  # fmt: skip
    assert (alert.condition, alert.message, alert.severity) == ("CPU Usage", "CPU above 95%", "major")


def test_datto_device_identity_order() -> None:
    base = {"alert_type": "Disk Usage", "device_hostname": "ACME-FS01"}
    assert parse_datto({**base, "device_id": "1874523", "device_uid": "uid-1"}).device_id == "uid-1"
    assert parse_datto({**base, "device_id": "1874523"}).device_id == "1874523"
    assert parse_datto({**base, "alertSourceInfo": {"deviceUid": "uid-2"}}).device_id == "uid-2"
    assert parse_datto(base).device_id == "ACME-FS01"
    with pytest.raises(RmmPayloadError, match="device_uid"):
        parse_datto({"alert_type": "Disk Usage", "device_uid": "[device_uid]"})


def test_datto_condition_from_alert_text_ignores_numbers() -> None:
    first = parse_datto({"device_uid": "d1", "alert": "Disk usage on C: above 90%", "alert_message": "95.8% used"})
    second = parse_datto({"device_uid": "d1", "alert": "Disk usage on C: above 95%", "alert_message": "97.1% used"})
    assert first.condition == second.condition == "disk usage on c: above %"
    assert first.condition_name == "Disk usage on C: above 90%"
    from_message = parse_datto({"device_uid": "d1", "alert_message": "Backup failed 3 times"})
    assert from_message.condition == "backup failed times"
    with pytest.raises(RmmPayloadError, match="alert_type"):
        parse_datto({"device_uid": "d1", "platform": "merlot"})


@pytest.mark.parametrize(
    ("fields", "state"),
    [
        ({"event": "resolved"}, "reset"),
        ({"event": "Raised"}, "triggered"),
        ({"status": "resolved"}, "reset"),
        ({"alertlevel": "Resolved"}, "reset"),
        ({"alert_resolved": "true"}, "reset"),
        ({"alertResolved": True}, "reset"),
        ({"alert_resolved": "false"}, "triggered"),
        ({"resolved": True}, "reset"),
        ({"event": "[event]"}, "triggered"),
        ({}, "triggered"),
    ],
)
def test_datto_raised_or_resolved(fields: dict[str, Any], state: str) -> None:
    assert parse_datto({"device_uid": "d1", "alert_type": "Disk Usage", **fields}).state == state


@pytest.mark.parametrize(
    ("fields", "severity", "hint"),
    [
        ({"priority": "Critical"}, "critical", "P1"),
        ({"priority": "High"}, "major", "P2"),
        ({"priority": "moderate"}, "moderate", "P3"),
        ({"priority": "Low"}, "minor", "P4"),
        ({"priority": "Information"}, "info", "P4"),
        ({"priority": "Unknown"}, "unknown", None),
        ({"priority": ""}, "unknown", None),
        ({"priority": "[alert_priority]"}, "unknown", None),
        ({"priority": 2}, "unknown", None),
        ({"alert_priority": "High"}, "major", "P2"),
        ({"severity": "Critical"}, "critical", "P1"),
        ({"priority": "[alert_priority]", "severity": "Low"}, "minor", "P4"),
        ({}, "unknown", None),
    ],
)
def test_datto_priority_mapping(fields: dict[str, Any], severity: str, hint: str | None) -> None:
    alert = parse_datto({"device_uid": "d1", "alert_type": "Disk Usage", **fields})
    assert (alert.severity, priority_hint(alert)) == (severity, hint)


def test_datto_list_of_alerts_asks_for_one_per_request() -> None:
    with pytest.raises(RmmPayloadError, match="one Datto RMM alert per request"):
        parse_alert({"pageDetails": {"count": 2}, "alerts": [load_rmm_fixture("datto_api_alert")]})


def test_datto_form_style_string_values() -> None:
    form = {"alert_uid": "u1", "device_uid": "d1", "alert_type": "Ping", "alert_resolved": "1", "timestamp": "1793804400000"}
    alert = parse_datto(form)
    assert alert.state == "reset"
    assert alert.occurred_at == datetime(2026, 11, 4, 15, 0, tzinfo=UTC)


# Detection and time ----------------------------------------------------------------

@pytest.mark.parametrize(
    ("source", "vendor"),
    [("ninjaone", "ninjaone"), ("Ninja", "ninjaone"), ("datto", "datto"), (" DattoRMM ", "datto")],
)
def test_explicit_source_wins(source: str, vendor: str) -> None:
    assert detect_vendor(load_rmm_fixture("datto_raised") if vendor == "ninjaone" else {}, source) == vendor


def test_vendor_detection_by_shape() -> None:
    for name in ("ninjaone_triggered", "ninjaone_console_minimal", "ninjaone_acknowledged"):
        assert detect_vendor(load_rmm_fixture(name)) == "ninjaone"
    for name in ("datto_raised", "datto_api_alert", "datto_test_send_unexpanded", "datto_priority_unexpanded"):
        assert detect_vendor(load_rmm_fixture(name)) == "datto"
    assert detect_vendor({"alerts": []}) == "datto"
    assert detect_vendor({"activityType": "CONDITION"}, source="  ") == "ninjaone"


@pytest.mark.parametrize(
    ("body", "source", "message"),
    [
        ({"text": "hello"}, None, "not recognised"),
        ({}, None, "not recognised"),
        ([{"activityType": "CONDITION"}], None, "JSON object"),
        ("CONDITION", None, "JSON object"),
        ({"activityType": "CONDITION"}, "connectwise", "Unknown source"),
    ],
)
def test_unknown_vendor_or_shape(body: Any, source: str | None, message: str) -> None:
    with pytest.raises(RmmPayloadError, match=message):
        parse_alert(body, source)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (1793805137.5, datetime(2026, 11, 4, 15, 12, 17, 500000, tzinfo=UTC)),
        (1793805137, datetime(2026, 11, 4, 15, 12, 17, tzinfo=UTC)),
        (1793805137000, datetime(2026, 11, 4, 15, 12, 17, tzinfo=UTC)),
        ("1793805137", datetime(2026, 11, 4, 15, 12, 17, tzinfo=UTC)),
        ("2026-11-04T15:12:17Z", datetime(2026, 11, 4, 15, 12, 17, tzinfo=UTC)),
        ("2026-11-04T15:12:17", datetime(2026, 11, 4, 15, 12, 17, tzinfo=UTC)),
        ("2026-11-04T16:12:17+01:00", datetime(2026, 11, 4, 15, 12, 17, tzinfo=UTC)),
        (None, None),
        (True, None),
        ("soon", None),
        (float("nan"), None),
        ("inf", None),
        (1e30, None),
        (-5, None),
        ({}, None),
        ("2026-11-04T15:12:17Z" * 10, None),
    ],
)
def test_parse_time(value: Any, expected: datetime | None) -> None:
    assert parse_time(value) == expected


# Model ----------------------------------------------------------------------------

def test_severity_to_priority_table() -> None:
    assert SEVERITY_TO_PRIORITY == {"critical": "P1", "major": "P2", "moderate": "P3", "minor": "P4", "info": "P4"}
    assert priority_hint(make_alert(severity="unknown")) is None
    assert priority_hint_reason(make_alert(severity="critical")) == "NinjaOne severity Critical"
    assert priority_hint_reason(make_alert(vendor="datto", severity="major", severity_reason="priority High")) == (
        "Datto RMM priority High"
    )


def test_dedupe_key_is_normalised() -> None:
    alert = make_alert(device_id="ACME-FS01", condition="  Disk \t Free\n\nSpace  ")
    assert alert.dedupe_key == hashed_key("ninjaone:acme-fs01: disk free space")
    assert make_alert(condition="DISK free SPACE").dedupe_key == make_alert(condition="disk  free space").dedupe_key


def test_long_dedupe_keys_are_hashed() -> None:
    long_one = make_alert(condition="c" * 300)
    other = make_alert(condition="c" * 299 + "d")
    assert re.fullmatch(r"ninjaone:sha256:[0-9a-f]{40}", long_one.dedupe_key)
    assert long_one.dedupe_key == make_alert(condition="C" * 300).dedupe_key
    assert long_one.dedupe_key != other.dedupe_key
    assert len(make_alert(condition="c" * 180).dedupe_key) <= 200


def test_dedupe_key_never_holds_alert_text() -> None:
    secret_text = "Backup failed password=Sup3rS3cretPw! token ghp_abcdefghijklmnopqrstuvwxyz0123456789AB"
    key = make_alert(condition=secret_text.lower()).dedupe_key
    assert re.fullmatch(r"ninjaone:sha256:[0-9a-f]{40}", key)
    assert "sup3r" not in key and "ghp_" not in key


def test_source_id_needs_an_alert_id() -> None:
    assert make_alert().source_id == "rmm:ninjaone:1:TRIGGERED"
    assert make_alert(alert_id=None).source_id is None


# Ticket text ----------------------------------------------------------------------

def test_inbound_ticket_from_a_ninjaone_alert() -> None:
    inbound = to_inbound(parse_ninjaone(load_rmm_fixture("ninjaone_triggered")), client_hash="abc")
    assert inbound.channel == "rmm"
    assert inbound.subject == "Disk Free Space < 10% on ACME-FS01"
    assert inbound.customer == "Acme Ltd"
    assert inbound.requester is None
    assert inbound.client_hash == "abc"
    assert inbound.source_id == "rmm:ninjaone:48213377:TRIGGERED"
    assert inbound.priority_hint == "P2"
    assert inbound.priority_hint_reason == "NinjaOne severity Major"
    assert inbound.source_detail == {
        "rmm_key": hashed_key("ninjaone:1042:b7e4d1c0-2f9a-4e6b-8d35-91a0c7f2e614"),
        "vendor": "ninjaone",
        "device": "ACME-FS01",
        "condition": "Disk Free Space < 10%",
        "severity": "major",
        "occurrences": 1,
    }
    lines = inbound.body.splitlines()
    assert lines[0] == "Disk C: free space is 6.2% (below 10% threshold) on ACME-FS01"
    assert "Device: ACME-FS01 (device id 1042)" in lines
    assert "Customer: Acme Ltd" in lines
    assert "Condition: Disk Free Space < 10%" in lines
    assert "Severity: Major" in lines
    assert "Source: NinjaOne alert" in lines
    assert "Time: 04 Nov 2026 15:12 UTC" in lines


def test_inbound_ticket_without_a_priority_says_so() -> None:
    inbound = to_inbound(parse_alert(load_rmm_fixture("datto_priority_unexpanded")), client_hash=None)
    assert inbound.priority_hint is None and inbound.priority_hint_reason is None
    assert "Severity: Not supplied. The RMM did not supply a priority" in inbound.body
    assert "Customer: not supplied by the RMM" in inbound.body
    assert "Time: not supplied by the RMM" in inbound.body
    assert inbound.source_detail["severity"] == "unknown"


def test_subject_and_body_limits() -> None:
    long_alert = make_alert(condition_name="Very long condition " * 30, device_name="D" * 150, message="m" * 9000)
    inbound = to_inbound(long_alert, client_hash=None)
    assert len(inbound.subject) <= SUBJECT_MAX_CHARS
    assert inbound.subject.endswith(" on " + "D" * 80)
    assert "..." in inbound.subject
    assert len(inbound.body) <= BODY_MAX_CHARS
    assert inbound.body.startswith("m" * 100)
    assert "Device: " in inbound.body, "the facts survive a long message"

    bare = to_inbound(make_alert(condition_name=None, device_name=None, message="", customer=None), client_hash=None)
    assert bare.subject == "RMM alert on device 1042"
    assert bare.body.startswith("The RMM did not include an alert message.")
    assert len(bare.body) >= 10
    assert build_subject(make_alert(condition_name="  Disk \n full  ")) == "Disk full on ACME-FS01"


def test_secrets_in_alert_text_never_reach_the_event_detail() -> None:
    """A Datto alert with no type or category takes its condition from the message text."""
    alert = parse_datto(
        {
            "alert_uid": "u1",
            "device_uid": "abc-123",
            "device_hostname": "SRV-FS01",
            "alert_message": "Backup job failed: password=Sup3rS3cretPw! token ghp_abcdefghijklmnopqrstuvwxyz0123456789AB",
        }
    )
    inbound = to_inbound(alert, client_hash=None)
    detail = json.dumps(inbound.source_detail)
    assert "Sup3rS3cretPw" not in detail and "ghp_abcdef" not in detail
    assert "[REDACTED:password]" in inbound.source_detail["condition"]
