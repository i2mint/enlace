"""The launcher and the auth gate share one visibility predicate (issue #35).

A user holding a runtime grant for a ``protected:user`` app could open it (the
gate unions static ``allowed_users`` with live grants) but never see it in
``/_apps`` (the launcher did not). These tests pin the property that matters —
the launcher's verdict equals the gate's predicate for every combination of
static-only, grant-only, both and neither — not merely "a granted user sees it",
which a hard-coded ``True`` would pass.
"""

import itertools

import pytest
from starlette.testclient import TestClient

from enlace import access
from enlace.base import AppConfig, PlatformConfig
from enlace.compose import build_backend

STATIC = "static@x.com"
GRANTED = "granted@x.com"
BOTH = "both@x.com"
NEITHER = "neither@x.com"


class _FakeSession:
    """Stand-in for the auth plugin's session middleware: identity from a header."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            email = dict(scope["headers"]).get(b"x-test-user")
            state = scope.setdefault("state", {})
            state["user_id"] = email.decode() if email else None
            state["user_email"] = email.decode() if email else None
        await self.app(scope, receive, send)


def _gated_app(allowed_users=(STATIC, BOTH)):
    return AppConfig(
        name="gated",
        route_prefix="/api/gated",
        app_type="frontend_only",
        access="protected:user",
        allowed_users=list(allowed_users),
    )


def _client(apps, grants):
    """A backend whose injected grants resolver reads the mutable ``grants`` dict."""
    backend = build_backend(PlatformConfig(apps=apps))
    backend.add_middleware(_FakeSession)
    setattr(
        backend.state, access.GRANTS_STATE_ATTR, lambda app_id: grants.get(app_id, ())
    )
    return TestClient(backend)


def _listed(client, user):
    headers = {"x-test-user": user} if user else {}
    return [a["name"] for a in client.get("/_apps", headers=headers).json()["apps"]]


@pytest.mark.parametrize("user", [STATIC, GRANTED, BOTH, NEITHER, None])
def test_launcher_verdict_equals_the_gate_predicate(user):
    """For every kind of user, listed in /_apps ⟺ allowed by the gate's predicate."""
    grants = {"gated": {GRANTED, BOTH}}
    app = _gated_app()
    client = _client([app], grants)
    gate_says = access.is_user_allowed(
        user,
        user,
        allowed_users=app.allowed_users,
        granted=access.granted_users(lambda a: grants.get(a, ()), "gated"),
    )
    assert ("gated" in _listed(client, user)) is gate_says
    # And the concrete expectations, so a predicate that is wrong on both
    # sides cannot pass by agreeing with itself.
    assert gate_says is (user in (STATIC, GRANTED, BOTH))


def test_grant_appears_and_expires_without_restart():
    """The resolver is consulted per request: no startup snapshot to go stale."""
    grants: dict = {}
    client = _client([_gated_app()], grants)
    assert _listed(client, GRANTED) == []
    grants["gated"] = {GRANTED}
    assert _listed(client, GRANTED) == ["gated"]
    del grants["gated"]
    assert _listed(client, GRANTED) == []


def test_granted_user_gets_the_icon_too():
    """The icon route shares the verdict — a listed app never has a broken icon."""
    client = _client([_gated_app()], {"gated": {GRANTED}})
    assert client.get("/_apps/gated/icon", headers={"x-test-user": GRANTED}).is_success
    assert (
        client.get("/_apps/gated/icon", headers={"x-test-user": NEITHER}).status_code
        == 404
    )


def test_matching_is_case_insensitive_on_both_sources():
    """The gate lowercases both sides; the launcher must as well."""
    app = _gated_app(allowed_users=["Static@X.com"])
    client = _client([app], {"gated": {"Granted@X.COM"}})
    assert _listed(client, "static@x.com") == ["gated"]
    assert _listed(client, "GRANTED@x.com") == ["gated"]


def test_failing_resolver_falls_back_to_static_allowlist():
    """A grants-store hiccup hides grant-only access but never breaks the listing."""

    def broken(app_id):
        raise OSError("grants store unavailable")

    backend = build_backend(PlatformConfig(apps=[_gated_app()]))
    backend.add_middleware(_FakeSession)
    setattr(backend.state, access.GRANTS_STATE_ATTR, broken)
    client = TestClient(backend)
    assert _listed(client, STATIC) == ["gated"]
    assert _listed(client, GRANTED) == []


def test_no_resolver_means_static_allowlist_only():
    """Without an auth plugin there are no grants; behaviour is unchanged."""
    backend = build_backend(PlatformConfig(apps=[_gated_app()]))
    backend.add_middleware(_FakeSession)
    client = TestClient(backend)
    assert _listed(client, STATIC) == ["gated"]
    assert _listed(client, GRANTED) == []


@pytest.mark.parametrize(
    "static, granted",
    list(itertools.product([(), ("a@x.com",)], [(), ("a@x.com",), ("b@x.com",)])),
)
def test_is_user_allowed_truth_table(static, granted):
    """Empty union ⇒ open to any signed-in user; otherwise membership in the union."""
    union = set(static) | set(granted)
    expected = (not union) or ("a@x.com" in union)
    assert (
        access.is_user_allowed(
            "a@x.com", "a@x.com", allowed_users=static, granted=granted
        )
        is expected
    )
    assert (
        access.is_user_allowed(None, None, allowed_users=static, granted=granted)
        is False
    )


@pytest.mark.parametrize(
    "level, expected",
    [
        ("public", True),
        ("local", True),
        ("protected:shared", True),
        ("protected:user", False),
        ("something-else", False),
    ],
)
def test_can_see_app_by_access_level_for_anonymous(level, expected):
    """Anonymous callers see open and shared-password apps, never user-gated ones."""
    assert access.can_see_app(level, None, None) is expected


def test_builtin_index_lists_only_what_the_caller_may_open():
    """The HTML index (no landing app) uses the launcher's predicate too."""
    public = AppConfig(
        name="open_one",
        route_prefix="/api/open_one",
        app_type="frontend_only",
        access="public",
    )
    client = _client([public, _gated_app()], {"gated": {GRANTED}})
    assert "Gated" not in client.get("/").text
    assert "Open One" in client.get("/").text
    assert "Gated" in client.get("/", headers={"x-test-user": GRANTED}).text
