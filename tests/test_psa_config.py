"""PSA settings: defaults, parsing, picklist id maps, https checks and the missing list."""

import logging

import pytest
from mock_psa import AUTOTASK_ENV, HALO_ENV

from triageops.psa.config import PsaSettings, parse_priority_ids


def test_defaults_leave_psa_sync_off() -> None:
    settings = PsaSettings.from_env({})
    assert settings.provider == ""
    assert not settings.enabled and not settings.ready
    assert settings.missing() == [] and settings.problems() == []
    assert (settings.timeout_seconds, settings.max_attempts, settings.backoff_seconds, settings.deadline_seconds) == (
        8.0, 3, 0.5, 20.0,
    )  # fmt: skip
    assert settings.halopsa_scope == "all"
    assert settings.halopsa_priority_ids == {"P1": 1, "P2": 2, "P3": 3, "P4": 4}
    assert settings.autotask_priority_ids == {"P1": 4, "P2": 1, "P3": 2, "P4": 3}
    assert settings.autotask_status_id == 1


def test_provider_is_case_insensitive() -> None:
    assert PsaSettings.from_env({"PSA_PROVIDER": " HaloPSA "}).provider == "halopsa"


def test_unknown_provider_is_disabled_with_a_warning(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        settings = PsaSettings.from_env({"PSA_PROVIDER": "connectwise"})
    assert settings.provider == "" and not settings.enabled
    assert "not supported" in caplog.text


def test_halopsa_missing_lists_every_required_name() -> None:
    settings = PsaSettings.from_env({"PSA_PROVIDER": "halopsa"})
    assert settings.enabled and not settings.ready
    assert settings.missing() == [
        "HALOPSA_BASE_URL", "HALOPSA_CLIENT_ID", "HALOPSA_CLIENT_SECRET", "HALOPSA_TICKET_TYPE_ID",
    ]  # fmt: skip


def test_halopsa_complete_settings_are_ready() -> None:
    settings = PsaSettings.from_env(
        {**HALO_ENV, "HALOPSA_TENANT": "acme", "HALOPSA_DEFAULT_CLIENT_ID": "12", "HALOPSA_SITE_ID": "0",
         "HALOPSA_TEAM_ID": "3", "HALOPSA_SCOPE": "edit:tickets read:tickets"}
    )  # fmt: skip
    assert settings.ready and settings.missing() == []
    assert settings.halopsa_ticket_type_id == 31
    assert (settings.halopsa_default_client_id, settings.halopsa_site_id, settings.halopsa_team_id) == (12, 0, 3)
    assert settings.halopsa_scope == "edit:tickets read:tickets"
    assert settings.halopsa_token_url == "https://acme.halopsa.com/auth/token"


def test_halopsa_auth_url_override_and_trailing_slashes() -> None:
    settings = PsaSettings.from_env(
        {**HALO_ENV, "HALOPSA_BASE_URL": "https://acme.halopsa.com/", "HALOPSA_AUTH_URL": "https://auth.acme.example/auth/"}
    )
    assert settings.halopsa_base_url == "https://acme.halopsa.com"
    assert settings.halopsa_token_url == "https://auth.acme.example/auth/token"


def test_autotask_missing_lists_every_required_name() -> None:
    settings = PsaSettings.from_env({"PSA_PROVIDER": "autotask"})
    assert settings.missing() == [
        "AUTOTASK_USERNAME", "AUTOTASK_SECRET", "AUTOTASK_INTEGRATION_CODE", "AUTOTASK_COMPANY_ID",
    ]  # fmt: skip


def test_autotask_company_id_zero_is_a_valid_value() -> None:
    settings = PsaSettings.from_env(AUTOTASK_ENV)
    assert settings.autotask_company_id == 0
    assert settings.ready, "company 0 is the MSP itself and must not count as missing"


def test_autotask_optional_ids_and_status() -> None:
    settings = PsaSettings.from_env(
        {**AUTOTASK_ENV, "AUTOTASK_QUEUE_ID": "29683487", "AUTOTASK_TICKET_TYPE_ID": "5", "AUTOTASK_SOURCE_ID": "0",
         "AUTOTASK_STATUS_ID": "8"}
    )  # fmt: skip
    assert (settings.autotask_queue_id, settings.autotask_ticket_type_id, settings.autotask_source_id) == (
        29683487, 5, 0,
    )  # fmt: skip
    assert settings.autotask_status_id == 8


def test_settings_of_the_other_provider_do_not_block() -> None:
    settings = PsaSettings.from_env({**AUTOTASK_ENV, "HALOPSA_BASE_URL": "http://insecure.example"})
    assert settings.ready, "a bad Halo value must not block Autotask"


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("HALOPSA_BASE_URL", "http://acme.halopsa.com"),
        ("HALOPSA_BASE_URL", "acme.halopsa.com"),
        ("HALOPSA_AUTH_URL", "ftp://acme.halopsa.com/auth"),
    ],
)
def test_halopsa_urls_must_use_https(name: str, value: str) -> None:
    settings = PsaSettings.from_env({**HALO_ENV, name: value})
    assert not settings.ready
    assert name in settings.missing()
    assert f"{name} (must be an https URL)" in settings.problems()


def test_autotask_base_url_must_use_https() -> None:
    settings = PsaSettings.from_env({**AUTOTASK_ENV, "AUTOTASK_BASE_URL": "http://webservices15.autotask.net/atservicesrest"})
    assert settings.missing() == ["AUTOTASK_BASE_URL"]


def test_invalid_integers_are_reported_by_name() -> None:
    settings = PsaSettings.from_env({**AUTOTASK_ENV, "AUTOTASK_COMPANY_ID": "acme", "AUTOTASK_QUEUE_ID": "8x"})
    assert settings.autotask_company_id is None
    assert settings.missing() == ["AUTOTASK_COMPANY_ID", "AUTOTASK_QUEUE_ID"]
    assert settings.problems() == ["AUTOTASK_COMPANY_ID (must be a whole number)", "AUTOTASK_QUEUE_ID (must be a whole number)"]


@pytest.mark.parametrize(("raw", "expected"), [("0", 1), ("1", 1), ("4", 4), ("5", 5), ("99", 5), ("-3", 1)])
def test_max_attempts_is_clamped(raw: str, expected: int) -> None:
    assert PsaSettings.from_env({"PSA_MAX_ATTEMPTS": raw}).max_attempts == expected


def test_timing_values_parse_and_reject_nonsense() -> None:
    settings = PsaSettings.from_env(
        {**HALO_ENV, "PSA_TIMEOUT_SECONDS": "4.5", "PSA_BACKOFF_SECONDS": "0.25", "PSA_DEADLINE_SECONDS": "12"}
    )
    assert (settings.timeout_seconds, settings.backoff_seconds, settings.deadline_seconds) == (4.5, 0.25, 12.0)
    broken = PsaSettings.from_env({**HALO_ENV, "PSA_DEADLINE_SECONDS": "soon", "PSA_TIMEOUT_SECONDS": "-1"})
    assert broken.deadline_seconds == 20.0 and broken.timeout_seconds == 8.0
    assert set(broken.missing()) == {"PSA_DEADLINE_SECONDS", "PSA_TIMEOUT_SECONDS"}


# Picklist id maps --------------------------------------------------------------------

def test_priority_ids_parse_with_spaces_and_lower_case() -> None:
    assert parse_priority_ids(" p1 : 10, P2:20 ,P3:30,P4:40 ", "P1:1,P2:2,P3:3,P4:4") == {
        "P1": 10, "P2": 20, "P3": 30, "P4": 40,
    }  # fmt: skip


def test_priority_ids_given_override_the_defaults() -> None:
    settings = PsaSettings.from_env({**AUTOTASK_ENV, "AUTOTASK_PRIORITY_IDS": "P1:7"})
    assert settings.autotask_priority_ids == {"P1": 7, "P2": 1, "P3": 2, "P4": 3}


@pytest.mark.parametrize("raw", ["P1=1", "P5:1", "P1:high", "critical:4"])
def test_bad_priority_ids_are_a_problem_and_keep_the_defaults(raw: str) -> None:
    settings = PsaSettings.from_env({**HALO_ENV, "HALOPSA_PRIORITY_IDS": raw})
    assert settings.halopsa_priority_ids == {"P1": 1, "P2": 2, "P3": 3, "P4": 4}
    assert settings.problems() == ["HALOPSA_PRIORITY_IDS (must look like P1:1,P2:2,P3:3,P4:4)"]


def test_cache_key_changes_with_any_value() -> None:
    first, second = PsaSettings.from_env(HALO_ENV), PsaSettings.from_env(HALO_ENV)
    assert first.cache_key() == second.cache_key()
    second.halopsa_team_id = 9
    assert first.cache_key() != second.cache_key()
