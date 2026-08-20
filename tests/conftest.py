"""Shared fixtures for the technocore-chat test suite.

The ``reset_globals`` autouse fixture clears every module-level mutable in ``app``
before every test, so tests are order-independent and never leak rate-limiter state,
room-cache hits, or waiter counts into one another.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Ensure ``import app`` / ``import store`` resolve to the src/ tree, matching the
# runtime layout where they are top-level modules shipped into the image.
_src = str(Path(__file__).resolve().parents[1] / "src")
if _src not in sys.path:
    sys.path.insert(0, _src)


@pytest.fixture(autouse=True)
def _reset_globals(monkeypatch, tmp_path):
    """Reset every module-level mutable in ``app`` so tests are order-independent.

    Called before every test. Also points ``CHAT_ROOT`` at ``tmp_path`` and
    forces a fresh import of ``app`` and ``store`` so the constants read from
    the environment are predictable.
    """
    os.environ["CHAT_ROOT"] = str(tmp_path)
    for mod in ("app", "store"):
        sys.modules.pop(mod, None)

    import app as app_module

    # Rate-limiter buckets and bookkeeping
    monkeypatch.setattr(app_module, "_buckets", app_module.OrderedDict(), raising=False)
    monkeypatch.setattr(
        app_module, "_requests", {"read": 0, "write": 0, "rate_limited": 0}, raising=False
    )
    monkeypatch.setattr(app_module, "_proxy_evidence", {"proxied_requests": 0}, raising=False)
    monkeypatch.setattr(app_module, "_identities", app_module.OrderedDict(), raising=False)

    # Room cache
    monkeypatch.setattr(app_module, "_rooms_cache", app_module.OrderedDict(), raising=False)

    # Long-poll waiter tracking
    monkeypatch.setattr(app_module, "_waiters_total", 0, raising=False)
    monkeypatch.setattr(app_module, "_waiters_by_ip", {}, raising=False)

    # Stats cache
    monkeypatch.setattr(app_module, "_stats_cache", (0.0, {}), raising=False)
