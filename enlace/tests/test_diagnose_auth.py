"""Diagnostic checks for auth/store patterns in sub-apps."""

from pathlib import Path

from enlace.diagnose import Category, diagnose_app


def _write_app(tmp_path: Path, name: str, code: str) -> Path:
    app_dir = tmp_path / name
    app_dir.mkdir()
    (app_dir / "server.py").write_text(code)
    return app_dir


def _cats(report) -> set[Category]:
    return {i.category for i in report.issues}


def test_subapp_auth_middleware_flagged(tmp_path: Path):
    app_dir = _write_app(
        tmp_path,
        "bad_auth",
        "from starlette.middleware.authentication import AuthenticationMiddleware\n"
        "app.add_middleware(AuthenticationMiddleware)\n",
    )
    report = diagnose_app(app_dir)
    assert Category.SUBAPP_AUTH_MIDDLEWARE in _cats(report)


def test_identity_header_flagged(tmp_path: Path):
    app_dir = _write_app(
        tmp_path,
        "bad_headers",
        "def h(req):\n    return req.headers.get('X-User-ID')\n",
    )
    report = diagnose_app(app_dir)
    assert Category.CLIENT_IDENTITY_HEADER in _cats(report)


def test_hardcoded_user_id_flagged(tmp_path: Path):
    app_dir = _write_app(tmp_path, "bad_uid", "user_id = 'admin'\n")
    report = diagnose_app(app_dir)
    assert Category.HARDCODED_USER_ID in _cats(report)


def test_session_cookie_flagged(tmp_path: Path):
    app_dir = _write_app(
        tmp_path,
        "bad_cookie",
        "def h(resp):\n    resp.set_cookie('session', 'val')\n",
    )
    report = diagnose_app(app_dir)
    assert Category.SESSION_COOKIE_IN_SUBAPP in _cats(report)


def test_enlace_import_flagged(tmp_path: Path):
    app_dir = _write_app(
        tmp_path,
        "bad_import",
        "from enlace.stores import PrefixedStore\n",
    )
    report = diagnose_app(app_dir)
    assert Category.STORE_IMPORT_IN_APP in _cats(report)


def test_unsafe_store_key_flagged(tmp_path: Path):
    app_dir = _write_app(
        tmp_path,
        "bad_key",
        "def h(request):\n"
        "    store = request.state.store\n"
        "    store[request.body] = 1\n",
    )
    report = diagnose_app(app_dir)
    assert Category.UNSAFE_KEY_IN_STORE in _cats(report)


def test_clean_app_has_no_new_issues(tmp_path: Path):
    """A well-behaved app should trigger none of our new checks."""
    app_dir = _write_app(
        tmp_path,
        "clean",
        "from fastapi import FastAPI\n"
        "app = FastAPI()\n"
        "@app.get('/')\n"
        "def root(request):\n"
        "    return {'user_id': request.state.user_id}\n",
    )
    report = diagnose_app(app_dir)
    cats = _cats(report)
    for c in (
        Category.SUBAPP_AUTH_MIDDLEWARE,
        Category.CLIENT_IDENTITY_HEADER,
        Category.HARDCODED_USER_ID,
        Category.SESSION_COOKIE_IN_SUBAPP,
        Category.STORE_IMPORT_IN_APP,
        Category.UNSAFE_KEY_IN_STORE,
    ):
        assert c not in cats
