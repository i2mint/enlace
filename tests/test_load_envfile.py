"""Tests for ``enlace.__main__._load_envfile``.

Pinned by the post-deploy smoke story: the CI deploy invokes
``enlace doctor --envfile /opt/tw_platform/.env`` as the unprivileged
``deploy`` user, but the envfile is intentionally ``root:root 0600``.
Before the auto-skip behavior, that crashed the smoke with a
``PermissionError`` and red'd the workflow even though the deploy itself
had succeeded. These tests pin the new contract:

- valid envfile → loaded, returns True, populates ``os.environ``
- missing file → ``sys.exit(2)`` (typo / wrong path is a hard error)
- unreadable file (PermissionError, etc.) → warns to stderr, returns False
  (so the caller can degrade env-based checks without aborting)
"""

import json as _json
import os
import sys
from pathlib import Path

import pytest

from enlace.__main__ import _load_envfile, _resolve_envfile, doctor


def test_loads_valid_envfile(tmp_path, monkeypatch):
    """KEY=VALUE lines land in os.environ; returns True."""
    monkeypatch.delenv("ENLACE_TEST_KEY_A", raising=False)
    monkeypatch.delenv("ENLACE_TEST_KEY_B", raising=False)

    envfile = tmp_path / ".env"
    envfile.write_text(
        '# comment ignored\n\nENLACE_TEST_KEY_A="hello"\nENLACE_TEST_KEY_B=world\n'
    )

    assert _load_envfile(str(envfile)) is True
    assert os.environ["ENLACE_TEST_KEY_A"] == "hello"
    assert os.environ["ENLACE_TEST_KEY_B"] == "world"


def test_missing_envfile_exits_hard(tmp_path):
    """A missing path is treated as a typo — hard exit with code 2."""
    missing = tmp_path / "does-not-exist.env"
    with pytest.raises(SystemExit) as excinfo:
        _load_envfile(str(missing))
    assert excinfo.value.code == 2


def test_unreadable_envfile_warns_and_skips(tmp_path, monkeypatch, capsys):
    """Permission-denied is a deliberate topology — warn, return False, don't abort.

    Skipped when running as root (root sidesteps the read mode bits and
    would still succeed) — the case under test only fires for unprivileged
    callers like the ``deploy`` user.
    """
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root bypasses file permission bits")

    envfile = tmp_path / "locked.env"
    envfile.write_text("ENLACE_TEST_KEY=should-not-load\n")
    envfile.chmod(0o000)
    monkeypatch.delenv("ENLACE_TEST_KEY", raising=False)

    try:
        assert _load_envfile(str(envfile)) is False
    finally:
        # Restore so tmp_path cleanup can remove the file.
        envfile.chmod(0o600)

    # The key was not loaded into the environment.
    assert "ENLACE_TEST_KEY" not in os.environ

    # A clear, actionable warning was printed to stderr.
    captured = capsys.readouterr()
    assert "not readable" in captured.err
    assert str(envfile) in captured.err


# -- doctor's envfile default (#64) -------------------------------------------


def test_auto_envfile_is_the_dotenv_beside_platform_toml(tmp_path):
    toml = tmp_path / "platform.toml"
    assert _resolve_envfile("auto", toml_path=toml) is None  # no toml
    toml.write_text("[platform]\n")
    assert _resolve_envfile("auto", toml_path=toml) is None  # no .env
    (tmp_path / ".env").write_text("A=1\n")
    assert _resolve_envfile("auto", toml_path=toml) == tmp_path / ".env"


def test_empty_envfile_opts_out_and_a_path_is_taken_verbatim(tmp_path):
    (tmp_path / "platform.toml").write_text("[platform]\n")
    (tmp_path / ".env").write_text("A=1\n")
    assert _resolve_envfile("", toml_path=tmp_path / "platform.toml") is None
    assert _resolve_envfile("x/y.env") == Path("x/y.env")


def _doctor_in(tmp_path, monkeypatch, capsys, *, exported=True, **kwargs) -> dict:
    (tmp_path / "apps").mkdir()
    (tmp_path / "platform.toml").write_text('[platform]\napps_dirs = ["apps"]\n')
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ENLACE_PLUGINS", "")
    monkeypatch.delenv("ENLACE_APPS_DIRS", raising=False)
    if exported:
        monkeypatch.setenv("ENLACE_TEST_DOCTOR_KEY", "from-shell")
    doctor(json=True, **kwargs)
    return _json.loads(capsys.readouterr().out)


def test_doctor_loads_the_dotenv_beside_platform_toml_by_default(
    tmp_path, monkeypatch, capsys
):
    (tmp_path / ".env").write_text("ENLACE_TEST_DOCTOR_KEY=from-envfile\n")
    monkeypatch.delenv("ENLACE_TEST_DOCTOR_KEY", raising=False)
    report = _doctor_in(tmp_path, monkeypatch, capsys, exported=False)
    assert report["envfile"] == str(tmp_path / ".env")
    assert report["envfile_status"] == "loaded"
    assert os.environ["ENLACE_TEST_DOCTOR_KEY"] == "from-envfile"


def test_doctor_default_envfile_never_overrides_an_exported_variable(
    tmp_path, monkeypatch, capsys
):
    """Review of #72: what the caller exported wins over the auto .env."""
    (tmp_path / ".env").write_text("ENLACE_TEST_DOCTOR_KEY=from-envfile\n")
    report = _doctor_in(tmp_path, monkeypatch, capsys)
    assert report["envfile_status"] == "loaded"
    assert os.environ["ENLACE_TEST_DOCTOR_KEY"] == "from-shell"


def test_an_explicit_envfile_still_overrides(tmp_path, monkeypatch, capsys):
    env = tmp_path / "gw.env"
    env.write_text("ENLACE_TEST_DOCTOR_KEY=from-envfile\n")
    _doctor_in(tmp_path, monkeypatch, capsys, envfile=str(env))
    assert os.environ["ENLACE_TEST_DOCTOR_KEY"] == "from-envfile"


def test_auto_envfile_does_not_follow_a_symlinked_platform_toml(tmp_path):
    """Review of #72: the .env beside the link, not beside its target."""
    target = tmp_path / "elsewhere"
    target.mkdir()
    (target / "platform.toml").write_text("[platform]\n")
    (target / ".env").write_text("A=target\n")
    here = tmp_path / "here"
    here.mkdir()
    (here / "platform.toml").symlink_to(target / "platform.toml")
    (here / ".env").write_text("A=here\n")
    assert _resolve_envfile("auto", toml_path=here / "platform.toml") == (here / ".env")


def test_doctor_envfile_empty_string_loads_nothing(tmp_path, monkeypatch, capsys):
    (tmp_path / ".env").write_text("ENLACE_TEST_DOCTOR_KEY=from-envfile\n")
    report = _doctor_in(tmp_path, monkeypatch, capsys, envfile="")
    assert report["envfile"] is None and report["envfile_status"] == "none"
    assert os.environ["ENLACE_TEST_DOCTOR_KEY"] == "from-shell"


def test_doctor_without_a_dotenv_reports_none(tmp_path, monkeypatch, capsys):
    report = _doctor_in(tmp_path, monkeypatch, capsys)
    assert report["envfile"] is None and report["envfile_status"] == "none"
