"""The CLI grammar is a contract; this pins the part of it that a dispatcher swap moves.

Recorded from the ``argh`` implementation before the ``cw`` migration and replayed
after it: 24 argv vectors (top-level and per-subcommand ``--help``, the no-argument
case, four usage-error cases, and eight real invocations) produced byte-identical
stdout, stderr and exit codes. That full-body diff cannot live in CI — CPython rewrites
argparse's own option column between versions — so what is asserted here is the
grammar itself, which does not move between interpreters.

The load-bearing case is ``build``. Its signature is
``build(app_name="", *, dry_run=False, ...)``: a *defaulted positional*. Under cw's
default convention (:data:`cw.ARGH`, which reproduces ``argh.dispatch_commands``)
``app_name`` renders as the option ``--app-name``. Under ``cw.MODERN`` — equally
``cw.BY_NAME_IF_KWONLY``, which is what ``argh.add_commands`` applies post-0.30 — it
would become the positional ``enlace build <app-name>``. That reinterpretation runs,
exits 0, and silently means something else, so the test below asserts both halves:
the option is accepted AND the positional is rejected.

These run the real dispatch path in a subprocess with ``sys.argv[0]`` pinned, because
the claim is about exit codes and stdout, which an in-process call cannot make.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

import enlace
from enlace.__main__ import COMMANDS

_ENLACE_ROOT = Path(enlace.__file__).resolve().parent.parent
_CLI_TIMEOUT = 120

#: Every verb the CLI exposes, spelled as the command line spells it.
COMMAND_NAMES = [f.__name__.replace("_", "-") for f in COMMANDS]

_RUNNER = """
import sys
sys.argv = ['enlace'] + {argv!r}
from enlace.__main__ import main
main()
"""


def run_cli(*argv, cwd):
    """Run ``enlace <argv>`` in a subprocess, with ``argv[0]`` pinned to the name."""
    return subprocess.run(
        [sys.executable, "-c", _RUNNER.format(argv=list(argv))],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=_CLI_TIMEOUT,
        env={
            "PYTHONPATH": str(_ENLACE_ROOT),
            "PATH": "/usr/bin:/bin",
            "COLUMNS": "80",
        },
    )


@pytest.fixture
def empty_platform(tmp_path):
    """A directory with no apps, so every verb reports rather than discovers."""
    return tmp_path


def test_every_command_is_reachable_and_documents_itself(empty_platform):
    """Each verb in COMMANDS is a subcommand, and its ``--help`` renders."""
    top = run_cli("--help", cwd=empty_platform)
    assert top.returncode == 0
    for name in COMMAND_NAMES:
        assert name in top.stdout, f"{name} missing from `enlace --help`"
        sub = run_cli(name, "--help", cwd=empty_platform)
        assert sub.returncode == 0, sub.stderr
        assert sub.stdout.startswith(f"usage: enlace {name}")


def test_build_takes_app_name_as_an_option_not_a_positional(empty_platform):
    """The grammar trap: ``--app-name x``, never ``build x``.

    See the module docstring for why this is the load-bearing case.
    """
    help_ = run_cli("build", "--help", cwd=empty_platform)
    assert "[--app-name APP_NAME]" in help_.stdout

    # The option form is accepted and reaches the function: exit 1 is build's own
    # "no such app" report, which only a parsed --app-name can produce.
    named = run_cli(
        "build", "--app-name", "no-such-app", "--dry-run", cwd=empty_platform
    )
    assert named.returncode == 1
    assert "no-such-app" in named.stderr

    # The positional form is a usage error. Under a BY_NAME_IF_KWONLY-style convention
    # this would exit 1 instead, having silently redefined the command line.
    positional = run_cli("build", "no-such-app", cwd=empty_platform)
    assert positional.returncode == 2
    assert "unrecognized arguments" in positional.stderr


def test_dry_run_keeps_its_short_flag(empty_platform):
    """``-d`` is inferred from ``dry_run``; losing it would break a deploy script."""
    assert run_cli("build", "-d", cwd=empty_platform).returncode == 0


def test_no_arguments_prints_usage_to_stdout_and_exits_zero(empty_platform):
    """argh's behaviour; plain argparse with a required subparser does NOT do this."""
    result = run_cli(cwd=empty_platform)
    assert result.returncode == 0
    assert result.stdout.startswith("usage: enlace")
    assert result.stderr == ""


@pytest.mark.parametrize(
    "argv, expected",
    [
        (("no-such-command",), 2),
        (("build", "--no-such-flag"), 2),
        (("check", "--json", "extra"), 2),
        (("diagnose",), 2),  # a required positional, omitted
    ],
)
def test_usage_errors_exit_two(argv, expected, empty_platform):
    """``cw.dispatch`` *returns* the code, so ``main`` must ``raise SystemExit`` on it.

    Dropping that ``SystemExit`` turns every one of these into exit 0 and is invisible
    to every other test in the suite.
    """
    assert run_cli(*argv, cwd=empty_platform).returncode == expected


def test_doctor_json_stays_machine_readable(empty_platform):
    """A downstream deploy smoke test shells out to this and judges its output."""
    result = run_cli("doctor", "--json", cwd=empty_platform)
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert set(payload) >= {"ok", "summary", "checks"}


def test_check_json_stays_machine_readable(empty_platform):
    result = run_cli("check", "--json", cwd=empty_platform)
    assert result.returncode == 0
    assert set(json.loads(result.stdout)) == {"errors", "warnings"}


def test_the_package_declares_the_cli_library_it_imports():
    """``enlace/__main__.py`` imports cw; the dependency list has to say so."""
    pyproject = (_ENLACE_ROOT / "pyproject.toml").read_text()
    assert '"cw>=' in pyproject
    assert "argh" not in pyproject
