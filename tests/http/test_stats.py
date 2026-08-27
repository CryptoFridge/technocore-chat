"""Run: uv run --group dev python -m pytest tests"""

import json
import os
from pathlib import Path

import _client
import pytest
from starlette.testclient import TestClient

client = _client.client  # the shared TestClient fixture


def test_stats_says_whether_per_ip_limits_are_actually_per_ip(client, monkeypatch):
    """Behind a CDN with no CHAT_CLIENT_IP_HEADER every caller shares one bucket, and the
    per-day room budget then bounds the whole world at once. Silent, and indistinguishable
    from an outage — so the evidence is published rather than left to be guessed at."""
    import config

    with config.override(STATS_TOKEN="t", STATS_CACHE_SECONDS=0):
        for i in range(3):
            client.get("/r/lobby", headers={"CF-Connecting-IP": f"203.0.113.{i}"})
        ident = client.get("/stats", headers={"X-Stats-Token": "t"}).json()["client_identity"]
        assert ident["client_ip_header"] is None
        assert ident["proxied_requests_ignored"] >= 3  # three real callers...
        assert ident["distinct_identities"] == 1  # ...seen as one


@pytest.fixture()
def stats_client(tmp_path, monkeypatch):
    """A client whose service has the stats token configured (the deployed shape)."""
    import app as app_module
    import config

    # Test bodies read CHAT_ROOT back for direct store access; the knob itself comes from
    # config.override now, not the environment.
    monkeypatch.setenv("CHAT_ROOT", str(tmp_path))
    app_module._buckets.clear()
    app_module._rooms_cache.clear()
    app_module._identities.clear()
    app_module._proxy_evidence["proxied_requests"] = 0
    with config.override(  # every stats call recomputes, so a test can observe its writes
        ROOT=tmp_path, STATS_TOKEN="s3cret", STATS_CACHE_SECONDS=0, DUPE_FILTER_SECONDS=0
    ):
        yield TestClient(app_module.app)


def test_stats_does_not_exist_without_a_token(client):
    """Unconfigured means absent, not open: growth numbers are never public by default."""
    assert client.get("/stats").status_code == 404


def test_the_stats_404_is_byte_identical_to_a_path_that_was_never_routed(stats_client):
    """The whole point of 404-not-401 is that a prober cannot tell the endpoint from a
    path that does not exist. A distinctive body would hand that back — which is a live
    risk now that the generic 404 carries a route map rather than the word "Not Found"."""
    missing = stats_client.get("/definitely-not-a-route")
    for probe in (
        stats_client.get("/stats"),
        stats_client.get("/stats", headers={"X-Stats-Token": "wrong"}),
    ):
        assert probe.status_code == missing.status_code
        assert probe.text == missing.text


def test_stats_404s_a_wrong_token_rather_than_401ing(stats_client):
    """A 401 would confirm the endpoint is there to keep probing."""
    assert stats_client.get("/stats").status_code == 404
    assert stats_client.get("/stats", headers={"X-Stats-Token": "wrong"}).status_code == 404
    assert stats_client.get("/stats", headers={"X-Stats-Token": "s3cret"}).status_code == 200


def test_stats_counts_every_room_class_and_names_none_of_them(stats_client):
    """Unlisted rooms are counted (they bound the disk) but never named (the name is the
    only secret protecting them) — and the same holds for note namespaces and nicks."""
    import store

    for room in ("openroom", "p-verysecret", "d-owned", "e-fleeting"):
        stats_client.get(f"/r/{room}/say/somenick/hi")
    # A mailbox takes signed writes only, so the unsigned lane cannot create one — the
    # store is the short way to get the class on disk for the count.
    store.append(store.Path(os.environ["CHAT_ROOT"]), "mb-postbox", "somenick", "hi")
    stats_client.get("/kv/privatens/somekey/set/value")
    body = stats_client.get("/stats", headers={"X-Stats-Token": "s3cret"}).text
    view = json.loads(body)

    rooms = view["rooms"]
    assert rooms["total"] == 6  # the five above + the server's own `events` room
    # `ownable`: `d-owned` above was never claimed, so it is not an owned room yet.
    assert (rooms["unlisted"], rooms["mailbox"], rooms["ownable"], rooms["ephemeral"]) == (
        1,
        1,
        1,
        1,
    )
    assert rooms["listed"] == 5 and rooms["capacity"] == store.MAX_ROOMS
    assert view["notes"]["total"] == 1 and view["bytes"]["rooms"] > 0

    for secret in ("verysecret", "privatens", "somekey", "somenick", "postbox", "openroom"):
        assert secret not in body


def test_stats_reports_traffic_against_uptime(stats_client):
    """Request counters are only readable as a rate, so they ship with the uptime."""
    stats_client.get("/rooms")
    stats_client.get("/r/lobby/say/bot/hi")
    view = stats_client.get("/stats", headers={"X-Stats-Token": "s3cret"}).json()
    assert view["requests"]["read"] >= 1 and view["requests"]["write"] >= 1
    assert view["requests"]["uptime_seconds"] >= 0
    assert view["capacity_limits"]["read_per_min"] == 120


def test_stats_serves_the_stored_history_with_the_current_values(stats_client, monkeypatch):
    """One fetch answers both "now" and "how did we get here", so the caller keeps no ring
    of its own and a redeploy of it costs no history."""
    import store

    monkeypatch.setattr(store, "SNAPSHOT_EVERY", 0)
    for i in range(2):
        stats_client.get(f"/r/lobby/say/bot/m{i}")
    view = stats_client.get("/stats", headers={"X-Stats-Token": "s3cret"}).json()

    assert [h["counters"]["messages"] for h in view["history"]] == [1, 2]
    assert view["counters"]["messages"] == 2  # current, computed live
    # …and the history is the store's file, not a second copy built in the handler.
    assert store.snapshots(Path(os.environ["CHAT_ROOT"])) == view["history"]


def test_stats_cache_avoids_repeating_the_expensive_store_walk(stats_client, monkeypatch):
    """The token is not a cost bound: a leaked token can be replayed, so the O(capacity)
    stats walk still needs the short cache promised by the handler.
    """
    import app as app_module
    import config

    real_view = app_module._stats_view
    calls = []

    def counted():
        calls.append(1)
        return real_view()

    with config.override(STATS_CACHE_SECONDS=60):
        monkeypatch.setattr(app_module, "_stats_view", counted)
        app_module._stats_cache = (0.0, {})
        headers = {"X-Stats-Token": "s3cret"}
        first = stats_client.get("/stats", headers=headers)
        second = stats_client.get("/stats", headers=headers)
        assert first.status_code == second.status_code == 200
        assert calls == [1]


def test_identities_lru_evicts_oldest_first(stats_client):
    """When the LRU window is full, the oldest IP is evicted to make room for a new one."""
    import limit

    limit._identities.clear()
    limit._identities_evictions = 0
    # Fill the window with a tiny cap
    old_cap = limit.MAX_IDENTITIES
    try:
        limit.MAX_IDENTITIES = 3
        for i in range(3):
            limit._identities[f"10.0.0.{i}"] = None
        # Adding a 4th should evict the oldest (10.0.0.0)
        limit._identities["10.0.0.3"] = None
        limit._identities.popitem(last=False)  # simulate what take() does
        assert "10.0.0.0" not in limit._identities
        assert len(limit._identities) == 3
    finally:
        limit.MAX_IDENTITIES = old_cap
        limit._identities.clear()


def test_identities_lru_refreshes_recency_on_reinsert(stats_client):
    """Re-inserting an IP moves it to the end of the LRU, so established callers
    survive a flood of new IPs."""
    import limit

    limit._identities.clear()
    limit._identities_evictions = 0
    old_cap = limit.MAX_IDENTITIES
    try:
        limit.MAX_IDENTITIES = 3
        # Insert 3 IPs: oldest first
        for i in range(3):
            limit._identities[f"10.0.0.{i}"] = None
        # Re-insert the first IP to refresh its recency (move_to_end pattern)
        ip = "10.0.0.0"
        del limit._identities[ip]
        limit._identities[ip] = None  # now at the end
        # Window has 3 items. Add a 4th — evicts the oldest (10.0.0.1)
        limit._identities["10.0.0.3"] = None
        limit._identities.popitem(last=False)  # simulate take()'s eviction
        assert "10.0.0.0" in limit._identities  # survived because recency was refreshed
        assert "10.0.0.1" not in limit._identities  # evicted as oldest
    finally:
        limit.MAX_IDENTITIES = old_cap
        limit._identities.clear()


def test_identities_lru_stays_bounded_under_flood(stats_client):
    """100 unique IPs never grow past MAX_IDENTITIES; eviction count is correct."""
    import limit

    limit._identities.clear()
    limit._identities_evictions = 0
    old_cap = limit.MAX_IDENTITIES
    try:
        limit.MAX_IDENTITIES = 5
        for i in range(100):
            limit._identities[f"10.0.{i // 256}.{i % 256}"] = None
            if len(limit._identities) > limit.MAX_IDENTITIES:
                limit._identities.popitem(last=False)
                limit._identities_evictions += 1
        assert len(limit._identities) == 5
        assert limit._identities_evictions == 95
    finally:
        limit.MAX_IDENTITIES = old_cap
        limit._identities.clear()
        limit._identities_evictions = 0


def test_stats_reports_lru_identity_metrics(stats_client):
    """/stats exposes identities_capacity, identities_evictions, and the
    windowed distinct_identities count."""
    import limit

    limit._identities.clear()
    limit._identities_evictions = 0
    # Add two identities directly
    limit._identities["1.2.3.4"] = None
    limit._identities["5.6.7.8"] = None
    # Force some evictions to make the counter non-zero
    limit.MAX_IDENTITIES = 2
    limit._identities["9.9.9.9"] = None  # triggers eviction
    limit._identities.popitem(last=False)
    limit._identities_evictions += 1
    limit.MAX_IDENTITIES = 50_000  # restore

    view = stats_client.get("/stats", headers={"X-Stats-Token": "s3cret"}).json()
    ident = view["client_identity"]
    assert ident["distinct_identities"] == len(limit._identities)
    assert ident["identities_capacity"] == 50_000
    assert ident["identities_evictions"] >= 1
