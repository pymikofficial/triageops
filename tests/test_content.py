"""Knowledge base articles and sample tickets follow the taxonomy and the house copy rules."""

import importlib.util
import re
from pathlib import Path

import pytest

from triageops.kb_seed import KB_ARTICLES
from triageops.samples import SAMPLES
from triageops.security import verify_timestamped_signature
from triageops.taxonomy import CATEGORIES, CHANNELS, PRIORITIES

FORBIDDEN = {"em dash": "\u2014", "en dash": "\u2013", "semicolon": ";", "tilde": "~"}
CONTRACTION = re.compile(r"\b\w+(?:n't|'re|'ve|'ll|'m|'d)\b", re.IGNORECASE)
SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "send_test_ticket.py"


def copy_problems(text: str) -> list[str]:
    problems = [name for name, char in FORBIDDEN.items() if char in text]
    problems += [f"contraction {match}" for match in CONTRACTION.findall(text)]
    return problems


def test_knowledge_base_has_twelve_unique_articles() -> None:
    assert len(KB_ARTICLES) == 12
    assert len({article.slug for article in KB_ARTICLES}) == 12


@pytest.mark.parametrize("article", KB_ARTICLES, ids=lambda article: article.slug)
def test_article_is_well_formed(article) -> None:
    assert article.category in CATEGORIES
    assert re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", article.slug)
    assert 120 <= len(article.content.split()) <= 200
    assert re.search(r"^1\. ", article.content, re.MULTILINE), "articles are step by step"
    assert copy_problems(article.title + article.content) == []


@pytest.mark.parametrize("sample", SAMPLES, ids=lambda sample: sample.key)
def test_sample_ticket_is_valid(sample) -> None:
    assert sample.channel in CHANNELS
    assert sample.expected_priority in PRIORITIES
    assert 3 <= len(sample.subject) <= 200
    assert 10 <= len(sample.body) <= 4000
    assert copy_problems(sample.subject + sample.body) == []


def test_samples_cover_the_required_scenarios() -> None:
    assert [sample.key for sample in SAMPLES] == [
        "ransomware", "phishing", "mfa", "printer", "billing", "howto", "outage", "injection",
    ]  # fmt: skip
    assert {sample.channel for sample in SAMPLES} >= {"email", "portal", "whatsapp"}


def test_test_ticket_script_signs_exactly_like_the_server() -> None:
    spec = importlib.util.spec_from_file_location("send_test_ticket", SCRIPT)
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    raw = b'{"subject":"Test","body":"A signed test ticket."}'
    verify_timestamped_signature("secret", "1794000000", script.sign("secret", "1794000000", raw), raw, now=1794000000)
