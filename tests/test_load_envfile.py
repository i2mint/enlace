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

import os
import sys

import pytest

from enlace.__main__ import _load_envfile


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
