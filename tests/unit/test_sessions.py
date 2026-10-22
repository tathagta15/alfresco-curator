from __future__ import annotations

import pytest

from alfresco_curator.web.sessions import SessionStore


class FakeClient:
    """Stands in for AlfrescoClient; only close() matters to the store."""

    def __init__(self, name: str = "admin") -> None:
        self.username = name
        self.base_url = "http://acs.test/alfresco"
        self.closed = False

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch):
    now = {"value": 1000.0}
    monkeypatch.setattr("alfresco_curator.web.sessions.time.monotonic", lambda: now["value"])
    return now


def test_attach_creates_a_session_and_returns_an_id():
    store = SessionStore()
    session_id = store.attach(None, "search", FakeClient())

    assert session_id
    assert len(store) == 1
    assert store.get(session_id).clients["search"].username == "admin"


def test_second_role_reuses_the_same_session():
    store = SessionStore()
    first = store.attach(None, "search", FakeClient("analyst"))
    second = store.attach(first, "inspect", FakeClient("admin"))

    assert first == second
    assert len(store) == 1
    assert set(store.get(first).clients) == {"search", "inspect"}


def test_unknown_session_id_starts_a_fresh_session():
    store = SessionStore()
    session_id = store.attach("does-not-exist", "search", FakeClient())

    assert session_id != "does-not-exist"
    assert store.get("does-not-exist") is None


def test_reconnecting_a_role_closes_the_previous_client():
    store = SessionStore()
    first_client = FakeClient("admin")
    session_id = store.attach(None, "search", first_client)
    store.attach(session_id, "search", FakeClient("analyst"))

    assert first_client.closed is True
    assert store.get(session_id).clients["search"].username == "analyst"


def test_discard_closes_every_client():
    store = SessionStore()
    search, inspect = FakeClient("analyst"), FakeClient("admin")
    session_id = store.attach(None, "search", search)
    store.attach(session_id, "inspect", inspect)

    store.discard(session_id)

    assert search.closed and inspect.closed
    assert store.get(session_id) is None
    assert len(store) == 0


def test_idle_sessions_expire_and_are_closed(clock):
    store = SessionStore(ttl_seconds=60)
    client = FakeClient()
    session_id = store.attach(None, "search", client)

    clock["value"] += 61
    assert store.get(session_id) is None
    assert client.closed is True


def test_activity_postpones_expiry(clock):
    store = SessionStore(ttl_seconds=60)
    session_id = store.attach(None, "search", FakeClient())

    clock["value"] += 50
    assert store.get(session_id) is not None  # refreshes last_seen_at
    clock["value"] += 50
    assert store.get(session_id) is not None


def test_capacity_evicts_the_least_recently_used():
    store = SessionStore(max_sessions=2)
    oldest_client = FakeClient()
    oldest = store.attach(None, "search", oldest_client)
    middle = store.attach(None, "search", FakeClient())
    newest = store.attach(None, "search", FakeClient())

    assert len(store) == 2
    assert store.get(oldest) is None
    assert oldest_client.closed is True
    assert store.get(middle) is not None
    assert store.get(newest) is not None


def test_clear_releases_everything():
    store = SessionStore()
    client = FakeClient()
    store.attach(None, "search", client)

    store.clear()

    assert len(store) == 0
    assert client.closed is True


def test_get_with_no_session_id_returns_none():
    assert SessionStore().get(None) is None
