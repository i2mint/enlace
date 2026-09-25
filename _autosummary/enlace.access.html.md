# enlace.access

Who may reach an app: the one predicate the gate and the launcher both call.

Two code paths answer “may this user reach this app”: the request-time gate
(`enlace_auth`’s `PlatformAuthMiddleware`) and the `/_apps` launcher (which
hides what the caller could not open). When each carried its own copy of the
answer they drifted — the gate honoured runtime grants and the launcher did not,
so a granted user could open an app they could never find
([https://github.com/i2mint/enlace/issues/35](https://github.com/i2mint/enlace/issues/35)). This module is the single copy;
both sides call it, so they cannot disagree.

enlace core still enforces nothing — enforcement is `enlace_auth`’s job. The
launcher uses this module to *filter*, the gate to *deny*.

Runtime grants reach enlace core through one dependency-injection slot on the
root app’s state, [`GRANTS_STATE_ATTR`](#enlace.access.GRANTS_STATE_ATTR): a callable `app_id -> iterable of
emails` of currently active grants, installed by the auth plugin (the same
callable its gate consults). Absent it, only the static `allowed_users` count.

### Module Attributes

| [`GRANTS_STATE_ATTR`](#enlace.access.GRANTS_STATE_ATTR)   | The root app's `state` attribute an auth plugin sets to its grants resolver.   |
|----------------------------------------------------------------------|--------------------------------------------------------------------------------|

### Functions

| [`can_see_app`](#enlace.access.can_see_app)(access, user_id, user_email, \*)    | Whether the `/_apps` launcher shows an app of this access level.         |
|--------------------------------------------------------------------------------------------------|--------------------------------------------------------------------------|
| [`granted_users`](#enlace.access.granted_users)(resolver, app_id)                 | The lowercased emails holding an active runtime grant for `app_id`.      |
| [`is_user_allowed`](#enlace.access.is_user_allowed)(user_id, user_email, \*[, ...]) | Whether an authenticated user passes a `protected:user` app's allowlist. |

### enlace.access.GRANTS_STATE_ATTR *= 'dynamic_allowed_users'*

The root app’s `state` attribute an auth plugin sets to its grants resolver.

### enlace.access.can_see_app(access, user_id, user_email, , allowed_users=(), granted=())

Whether the `/_apps` launcher shows an app of this access level.

- `public` / `local` → always.
- `protected:shared` → always: it is gated when opened, not when listed,
  so users know the app exists and can ask for the password.
- `protected:user` → exactly when [`is_user_allowed()`](#enlace.access.is_user_allowed) — the gate’s own
  predicate, so a user sees precisely the apps they can open.
- anything else → never (deny by default).

* **Return type:**
  [`bool`](https://docs.python.org/3/builtins/functions.html#bool)

### enlace.access.granted_users(resolver, app_id)

The lowercased emails holding an active runtime grant for `app_id`.

Called per request, never cached: expiry is evaluated by the resolver at call
time, so the gate and the launcher expire a grant at the same instant.

A failing resolver yields no grants (fail closed for grant-based access,
static `allowed_users` still work): a grants-store hiccup must never break
auth or the listing. It is logged, not raised.

* **Return type:**
  [`frozenset`](https://docs.python.org/3/builtins/stdtypes.html#frozenset)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str)]

### enlace.access.is_user_allowed(user_id, user_email, , allowed_users=(), granted=())

Whether an authenticated user passes a `protected:user` app’s allowlist.

The allowlist is the static `allowed_users` ∪ the active runtime
`granted` emails, compared case-insensitively. An empty union means the
app is open to any authenticated user. An unauthenticated caller
(`user_id is None`) never passes.

* **Return type:**
  [`bool`](https://docs.python.org/3/builtins/functions.html#bool)
