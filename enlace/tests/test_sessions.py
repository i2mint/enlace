"""SessionStore roundtrip over a dict backend and file backend."""

from enlace.auth import SessionStore
from enlace.stores.backends import make_file_store_factory


def test_create_get_delete_dict():
    store = SessionStore({})
    sid = store.create("alice@example.com", "alice@example.com")
    got = store.get(sid)
    assert got is not None
    assert got["user_id"] == "alice@example.com"
    assert "created_at" in got
    assert store.delete(sid) is True
    assert store.get(sid) is None


def test_unknown_session_returns_none():
    store = SessionStore({})
    assert store.get("nope") is None
    assert store.delete("nope") is False


def test_list_all_multiple():
    store = SessionStore({})
    sid1 = store.create("a", "a@x")
    sid2 = store.create("b", "b@x")
    pairs = dict(store.list_all())
    assert sid1 in pairs and sid2 in pairs


def test_session_ids_are_unique():
    store = SessionStore({})
    ids = {store.create("u", None) for _ in range(20)}
    assert len(ids) == 20


def test_file_backend_persists(tmp_path):
    factory = make_file_store_factory(str(tmp_path))
    store = SessionStore(factory("sessions"))
    sid = store.create("alice", "alice@x")
    # Rebuild — the store should reload the same data.
    factory2 = make_file_store_factory(str(tmp_path))
    store2 = SessionStore(factory2("sessions"))
    got = store2.get(sid)
    assert got is not None and got["user_id"] == "alice"
