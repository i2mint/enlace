"""Who may reach an app: the one predicate the gate and the launcher both call.

Two code paths answer "may this user reach this app": the request-time gate
(``enlace_auth``'s ``PlatformAuthMiddleware``) and the ``/_apps`` launcher (which
hides what the caller could not open). When each carried its own copy of the
answer they drifted — the gate honoured runtime grants and the launcher did not,
so a granted user could open an app they could never find
(https://github.com/i2mint/enlace/issues/35). This module is the single copy;
both sides call it, so they cannot disagree.

enlace core still enforces nothing — enforcement is ``enlace_auth``'s job. The
launcher uses this module to *filter*, the gate to *deny*.

Runtime grants reach enlace core through one dependency-injection slot on the
root app's state, :data:`GRANTS_STATE_ATTR`: a callable ``app_id -> iterable of
emails`` of currently active grants, installed by the auth plugin (the same
callable its gate consults). Absent it, only the static ``allowed_users`` count.
"""

import logging
from typing import Callable, Iterable, Optional

_logger = logging.getLogger("enlace.access")

#: The root app's ``state`` attribute an auth plugin sets to its grants resolver.
GRANTS_STATE_ATTR = "dynamic_allowed_users"

GrantsResolver = Callable[[str], Iterable[str]]


def granted_users(resolver: Optional[GrantsResolver], app_id: str) -> frozenset[str]:
    """The lowercased emails holding an active runtime grant for ``app_id``.

    Called per request, never cached: expiry is evaluated by the resolver at call
    time, so the gate and the launcher expire a grant at the same instant.

    A failing resolver yields no grants (fail closed for grant-based access,
    static ``allowed_users`` still work): a grants-store hiccup must never break
    auth or the listing. It is logged, not raised.
    """
    if resolver is None:
        return frozenset()
    try:
        return frozenset(e.lower() for e in (resolver(app_id) or ()))
    except Exception as exc:  # noqa: BLE001 - see the fail-closed note above
        # One line per failure, traceback at DEBUG: the launcher resolves every
        # protected app per /_apps call, so a broken store would otherwise emit
        # a traceback per app per page load.
        _logger.warning(
            "dynamic grants lookup failed for app_id=%r (%s: %s); "
            "falling back to config allowed_users only",
            app_id,
            type(exc).__name__,
            exc,
        )
        _logger.debug("grants lookup traceback", exc_info=True)
        return frozenset()


def is_user_allowed(
    user_id: Optional[str],
    user_email: Optional[str],
    *,
    allowed_users: Iterable[str] = (),
    granted: Iterable[str] = (),
) -> bool:
    """Whether an authenticated user passes a ``protected:user`` app's allowlist.

    The allowlist is the static ``allowed_users`` ∪ the active runtime
    ``granted`` emails, compared case-insensitively. An empty union means the
    app is open to any authenticated user. An unauthenticated caller
    (``user_id is None``) never passes.
    """
    if user_id is None:
        return False
    allowed = {e.lower() for e in allowed_users} | {e.lower() for e in granted}
    if not allowed:
        return True
    return (user_email or user_id).lower() in allowed


def can_see_app(
    access: str,
    user_id: Optional[str],
    user_email: Optional[str],
    *,
    allowed_users: Iterable[str] = (),
    granted: Iterable[str] = (),
) -> bool:
    """Whether the ``/_apps`` launcher shows an app of this access level.

    - ``public`` / ``local`` → always.
    - ``protected:shared`` → always: it is gated when opened, not when listed,
      so users know the app exists and can ask for the password.
    - ``protected:user`` → exactly when :func:`is_user_allowed` — the gate's own
      predicate, so a user sees precisely the apps they can open.
    - anything else → never (deny by default).
    """
    if access in ("public", "local", "protected:shared"):
        return True
    if access == "protected:user":
        return is_user_allowed(
            user_id, user_email, allowed_users=allowed_users, granted=granted
        )
    return False
