"""The CLI's diagnostic verbs must survive an app that will not import.

Discovery imports every asgi-mode entry module and every verb discovers first,
so before ``on_import_error`` a single broken app killed ``enlace doctor``
with a raw traceback and an empty ``--json`` stdout — nothing was reported
about the apps that were fine, and a deploy smoke test had nothing to gate on.

These run the real CLI in a subprocess: the claim under test is about exit
codes and what lands on stdout, which an in-process call cannot make.
"""

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import enlace
from enlace.tests.conftest import BROKEN_MODULE, PERMISSION_ERROR_MODULE, _make_app_code

# The checkout (or site-packages dir) the imported enlace lives in, so the
# subprocess runs the same code as the test, not whatever is installed.
_ENLACE_ROOT = Path(enlace.__file__).resolve().parent.parent

_CLI_TIMEOUT = 120  # generous; these verbs do no I/O beyond the filesystem


@pytest.fixture
def apps_with_a_broken_one(tmp_path):
    """An apps dir with two healthy apps and one whose import raises."""
    apps_dir = tmp_path / "apps"
    apps_dir.mkdir()
    for name in ("alpha", "zulu"):
        d = apps_dir / name
        d.mkdir()
        (d / "server.py").write_text(_make_app_code(name))
    broken = apps_dir / "broken"
    broken.mkdir()
    (broken / "server.py").write_text(BROKEN_MODULE)
    return apps_dir


def _run_cli(apps_dir: Path, *args: str) -> subprocess.CompletedProcess:
    """Run ``python -m enlace <args> --apps-dir <apps_dir>`` in isolation."""
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(_ENLACE_ROOT), env.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    # A stray platform.toml or ENLACE_* var in the ambient environment would
    # point discovery somewhere other than the fixture.
    for var in ("ENLACE_APPS_DIR", "ENLACE_APPS_DIRS", "ENLACE_APP_DIRS"):
        env.pop(var, None)
    env["ENLACE_PLUGINS"] = ""
    return subprocess.run(
        [sys.executable, "-m", "enlace", *args, "--apps-dir", str(apps_dir)],
        capture_output=True,
        text=True,
        cwd=str(apps_dir.parent),
        env=env,
        timeout=_CLI_TIMEOUT,
    )


def test_doctor_json_is_parseable_and_names_the_broken_app(apps_with_a_broken_one):
    """`doctor --json` emits valid JSON and a FAIL naming the app + exception."""
    result = _run_cli(apps_with_a_broken_one, "doctor", "--json", "--skip-env-checks")

    assert result.returncode == 1, result.stderr
    report = json.loads(result.stdout)  # used to be empty — the expensive part
    assert report["ok"] is False

    failures = [c for c in report["checks"] if c["status"] == "fail"]
    assert [c["name"] for c in failures] == ["import:broken"]
    assert failures[0]["extra"]["exception_type"] == "ModuleNotFoundError"
    assert "nonexistent_package_xyz" in failures[0]["detail"]


def test_doctor_reports_a_non_import_exception(tmp_path):
    """A PermissionError at module scope is reported, not propagated.

    This is the shape of the only production instance of this crash: a
    dependency reading a root-only dotenv at import time.
    """
    apps_dir = tmp_path / "apps"
    apps_dir.mkdir()
    d = apps_dir / "dotenv_reader"
    d.mkdir()
    (d / "server.py").write_text(PERMISSION_ERROR_MODULE)

    result = _run_cli(apps_dir, "doctor", "--json", "--skip-env-checks")

    assert result.returncode == 1, result.stderr
    report = json.loads(result.stdout)
    failure = next(c for c in report["checks"] if c["status"] == "fail")
    assert failure["name"] == "import:dotenv_reader"
    assert failure["extra"]["exception_type"] == "PermissionError"


def test_check_still_exits_nonzero_but_reports(apps_with_a_broken_one):
    """`check` stays loud — it just says which app, instead of tracebacking."""
    result = _run_cli(apps_with_a_broken_one, "check")

    assert result.returncode == 1
    assert "Traceback" not in result.stderr
    assert "broken: entry module failed to import" in result.stdout
    assert "ModuleNotFoundError" in result.stdout


def test_list_apps_lists_the_healthy_apps_alongside_the_broken_one(
    apps_with_a_broken_one,
):
    result = _run_cli(apps_with_a_broken_one, "list-apps")

    assert result.returncode == 0, result.stderr
    assert "Traceback" not in result.stderr
    for name in ("alpha", "broken", "zulu"):
        assert name in result.stdout
    assert "Un-importable apps" in result.stdout


def test_show_config_flags_the_broken_app(apps_with_a_broken_one):
    result = _run_cli(apps_with_a_broken_one, "show-config")

    assert result.returncode == 0, result.stderr
    assert "ModuleNotFoundError" in result.stdout


def test_serve_still_refuses_to_boot(apps_with_a_broken_one):
    """Boot behaviour is untouched: serve dies on discovery, as it always has.

    It never reaches uvicorn — `discover_apps` raises first — so this does not
    bind a port.
    """
    result = _run_cli(apps_with_a_broken_one, "serve")

    assert result.returncode != 0
    assert "nonexistent_package_xyz" in result.stderr


def test_a_healthy_tree_is_byte_identical(tmp_path):
    """With no broken app, the diagnostic verbs print exactly what they used to."""
    apps_dir = tmp_path / "apps"
    apps_dir.mkdir()
    d = apps_dir / "alpha"
    d.mkdir()
    (d / "server.py").write_text(_make_app_code("alpha"))

    result = _run_cli(apps_dir, "list-apps")

    assert result.returncode == 0, result.stderr
    assert result.stdout == textwrap.dedent("""\
        Name   Route       Type      Access
        -----------------------------------
        alpha  /api/alpha  asgi_app  local
    """)
