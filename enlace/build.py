"""Run and validate declarative app builds from ``app.toml`` ``[build]``.

enlace owns the ``[build]`` *contract* (the schema in
:class:`enlace.base.BuildConfig`) so any deployer can rely on it, and
provides an explicit ``enlace build`` step that runs it. Builds never run at
request time — production should pre-build before the gateway starts. This
module is the small, dependency-free runner behind that step.

The runner shells out to whatever ``install`` / ``build`` commands the app
declares (npm, pnpm, vite, …). enlace does not assume a toolchain; it just
runs the argv the app provides, in the resolved working directory, with the
caller's environment plus any injected overrides.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from enlace.base import AppConfig


def app_dir_of(app: AppConfig) -> Path:
    """Best-effort resolution of an app's own directory.

    Used as the default build working directory when ``[build].cwd`` is not
    set. Works for both container-discovered apps (``source_dir/name``) and
    directly-discovered ones, and falls back to the entry module's parent.
    """
    if app.source_dir is not None:
        candidate = app.source_dir / app.name
        if candidate.is_dir():
            return candidate
    if app.entry_module_path is not None:
        return app.entry_module_path.parent
    if app.frontend_dir is not None:
        return app.frontend_dir.parent
    return Path(".")


def build_cwd(app: AppConfig) -> Path:
    """The directory build commands run from: ``[build].cwd`` or the app dir."""
    if app.build is not None and app.build.cwd is not None:
        return app.build.cwd
    return app_dir_of(app)


def has_build(app: AppConfig) -> bool:
    """Whether the app declares a runnable build (a ``build`` command)."""
    return app.build is not None and bool(app.build.build)


def validate_build(app: AppConfig) -> list[str]:
    """Return human-readable problems with an app's ``[build]`` config.

    Pure validation — never runs commands. Checks that the section, if
    present, has a ``build`` command and that its working directory exists.
    """
    bc = app.build
    if bc is None:
        return []
    problems: list[str] = []
    if not bc.build:
        problems.append(
            f"App '{app.name}': [build] section present but no 'build' command "
            "— the section does nothing."
        )
    cwd = build_cwd(app)
    if not Path(cwd).is_dir():
        problems.append(f"App '{app.name}': [build].cwd does not exist: {cwd}")
    return problems


@dataclass
class BuildResult:
    """Outcome of building one app."""

    app: str
    cwd: Path
    commands: list[list[str]] = field(default_factory=list)
    ran: bool = False
    returncode: int = 0


def run_build(
    app: AppConfig,
    *,
    extra_env: Optional[dict[str, str]] = None,
    dry_run: bool = False,
    check: bool = True,
) -> BuildResult:
    """Run an app's ``install`` (if any) then ``build`` commands.

    Args:
        app: The app whose ``[build]`` section to run.
        extra_env: Values to overlay on the inherited environment (e.g. the
            deployer's ``VITE_API_BASE=/api/{name}``). enlace itself does not
            choose values; callers do.
        dry_run: Resolve and record the commands without executing them.
        check: Raise ``subprocess.CalledProcessError`` on nonzero exit.

    Returns:
        A :class:`BuildResult` describing what was (or would be) run.
    """
    cwd = build_cwd(app)
    result = BuildResult(app=app.name, cwd=cwd)
    if not has_build(app):
        return result

    bc = app.build
    assert bc is not None  # has_build guarantees this
    if bc.install:
        result.commands.append(list(bc.install))
    assert bc.build is not None
    result.commands.append(list(bc.build))

    if dry_run:
        return result

    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)

    for command in result.commands:
        completed = subprocess.run(command, cwd=str(cwd), env=env, check=check)
        result.returncode = completed.returncode
        if completed.returncode != 0:
            break
    result.ran = True
    return result
