"""Build the PSA adapter for the configured provider, once per warm instance.

The adapter is cached by provider and settings, so the HaloPSA token and the
discovered Autotask zone survive between requests on a warm serverless
instance. A change to the settings (tests monkeypatch them) builds a new one.
"""

import threading

from .autotask import AutotaskAdapter
from .base import PsaAdapter
from .config import PsaSettings, psa_settings
from .halopsa import HaloPsaAdapter

_lock = threading.Lock()
_cache: dict[tuple[str, tuple[object, ...]], PsaAdapter] = {}


def _build(settings: PsaSettings) -> PsaAdapter | None:
    if settings.provider == "halopsa":
        return HaloPsaAdapter(settings)
    if settings.provider == "autotask":
        return AutotaskAdapter(settings)
    return None


def get_adapter(settings: PsaSettings | None = None) -> PsaAdapter | None:
    """The adapter for ``settings`` (default ``psa_settings``), or None when PSA sync is off or not ready."""
    settings = settings or psa_settings
    if not settings.ready:
        return None
    key = (settings.provider, settings.cache_key())
    with _lock:
        adapter = _cache.get(key)
        if adapter is None:
            adapter = _build(settings)
            if adapter is not None:
                _cache[key] = adapter
        return adapter


def reset_adapter_cache() -> None:
    """Drop every cached adapter and close its HTTP client. For tests."""
    with _lock:
        adapters = list(_cache.values())
        _cache.clear()
    for adapter in adapters:
        client = getattr(adapter, "client", None)
        if client is not None:
            client.close()
