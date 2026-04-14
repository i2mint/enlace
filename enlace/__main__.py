"""CLI entry point for enlace.

Usage::

    enlace serve              # Start the backend server
    enlace show-config        # Show resolved configuration
    enlace check              # Validate configuration
    enlace list-apps          # List discovered apps
"""

import json as json_module
import sys
from pathlib import Path

import argh

from enlace.base import PlatformConfig
from enlace.diagnose import diagnose_app
from enlace.discover import discover_apps
from enlace.serve import serve


def _build_config(
    apps_dir: str = "",
    apps_dirs: str = "",
    app_dirs: str = "",
) -> PlatformConfig:
    """Build PlatformConfig from TOML, with CLI args as overrides.

    When no CLI directory args are given, uses platform.toml values.
    """
    config = PlatformConfig.from_toml()

    updates: dict = {}
    resolved_apps_dirs: list[Path] = []
    if apps_dir:
        resolved_apps_dirs.append(Path(apps_dir))
    if apps_dirs:
        resolved_apps_dirs.extend(
            Path(d.strip()) for d in apps_dirs.split(",") if d.strip()
        )
    if resolved_apps_dirs:
        updates["apps_dirs"] = resolved_apps_dirs

    resolved_app_dirs: list[Path] = []
    if app_dirs:
        resolved_app_dirs.extend(
            Path(d.strip()) for d in app_dirs.split(",") if d.strip()
        )
    if resolved_app_dirs:
        updates["app_dirs"] = resolved_app_dirs

    if updates:
        config = config.model_copy(update=updates)

    return discover_apps(config)


def show_config(
    *,
    verbose: bool = False,
    json: bool = False,
    apps_dir: str = "",
    apps_dirs: str = "",
    app_dirs: str = "",
):
    """Show resolved platform configuration with provenance annotations.

    Args:
        verbose: Show provenance for every field.
        json: Output as JSON.
        apps_dir: Path to the apps directory.
        apps_dirs: Comma-separated container directories.
        app_dirs: Comma-separated individual app directories.
    """
    config = _build_config(apps_dir, apps_dirs, app_dirs)

    if json:
        data = config.model_dump(mode="json")
        print(json_module.dumps(data, indent=2))
        return

    print("Platform Configuration (resolved)")
    print("=" * 38)
    print()
    print("Meta-conventions:")
    print(f"  entry_points: {config.conventions.entry_points}")
    print(f"  app_attr: {config.conventions.app_attr}")
    print(f"  frontend_dir: {config.conventions.frontend_dir}")
    print(f"  apps_dirs: {[str(d) for d in config.apps_dirs]}")
    if config.app_dirs:
        print(f"  app_dirs: {[str(d) for d in config.app_dirs]}")
    print()

    has_non_asgi = any(a.mode != "asgi" for a in config.apps)

    if not config.apps:
        print("Discovered Apps: None")
    else:
        print("Discovered Apps:")
        for app in config.apps:
            print(f"  {app.name}")
            prov = app.provenance

            # Show mode when non-asgi apps exist
            if has_non_asgi:
                mode_src = f"  [{prov.get('mode', 'default')}]" if verbose else ""
                print(f"    mode:     {app.mode}{mode_src}")

            route_src = f"  [{prov.get('route_prefix', 'default')}]" if verbose else ""
            print(f"    route:    {app.route_prefix}{route_src}")

            if app.entry_module_path:
                entry_src = (
                    f"  [{prov.get('entry_module_path', '')}]" if verbose else ""
                )
                print(f"    entry:    {app.entry_module_path}{entry_src}")

            type_src = f"  [{prov.get('app_type', '')}]" if verbose else ""
            print(f"    type:     {app.app_type}{type_src}")

            access_src = f"  [{prov.get('access', 'default')}]" if verbose else ""
            print(f"    access:   {app.access}{access_src}")

            # Show process-mode details
            if app.mode == "process":
                if app.command:
                    print(f"    command:  {' '.join(app.command)}")
                if app.port is not None:
                    print(f"    port:     {app.port}")
            elif app.mode == "external":
                if app.upstream_url:
                    print(f"    upstream: {app.upstream_url}")
            elif app.mode == "static":
                if app.public_dir:
                    print(f"    dir:      {app.public_dir}")

            if app.frontend_dir:
                print(f"    frontend: {app.frontend_dir}")

            if verbose and app.source_dir:
                print(f"    source:   {app.source_dir}")

            print()

    errors = config.check_conflicts()
    if errors:
        print("Conflicts:")
        for e in errors:
            print(f"  - {e}")
    else:
        print("Conflicts: None")
    print("Warnings: None")


def check(
    *,
    json: bool = False,
    apps_dir: str = "",
    apps_dirs: str = "",
    app_dirs: str = "",
):
    """Validate platform configuration and check for conflicts.

    Exits with code 1 if errors are found.

    Args:
        json: Output as JSON.
        apps_dir: Path to the apps directory.
        apps_dirs: Comma-separated container directories.
        app_dirs: Comma-separated individual app directories.
    """
    config = _build_config(apps_dir, apps_dirs, app_dirs)

    errors = config.check_conflicts()
    warnings: list[str] = []

    if json:
        print(json_module.dumps({"errors": errors, "warnings": warnings}, indent=2))
    else:
        if errors:
            print("Errors:")
            for e in errors:
                print(f"  - {e}")
        else:
            print("No errors found.")
        if warnings:
            print("Warnings:")
            for w in warnings:
                print(f"  - {w}")

    if errors:
        sys.exit(1)


def list_apps(
    *,
    apps_dir: str = "",
    apps_dirs: str = "",
    app_dirs: str = "",
):
    """List discovered apps with their routes, types, and access levels.

    Args:
        apps_dir: Path to the apps directory.
        apps_dirs: Comma-separated container directories.
        app_dirs: Comma-separated individual app directories.
    """
    config = _build_config(apps_dir, apps_dirs, app_dirs)

    if not config.apps:
        print("No apps discovered.")
        return

    has_non_asgi = any(a.mode != "asgi" for a in config.apps)

    # Column widths
    name_w = max(len(a.name) for a in config.apps)
    route_w = max(len(a.route_prefix) for a in config.apps)
    type_w = max(len(a.app_type) for a in config.apps)

    if has_non_asgi:
        mode_w = max(len(a.mode) for a in config.apps)
        header = (
            f"{'Name':<{name_w}}  {'Mode':<{mode_w}}  "
            f"{'Route':<{route_w}}  {'Type':<{type_w}}  Access"
        )
        print(header)
        print("-" * len(header))
        for app in config.apps:
            print(
                f"{app.name:<{name_w}}  {app.mode:<{mode_w}}  "
                f"{app.route_prefix:<{route_w}}  "
                f"{app.app_type:<{type_w}}  {app.access}"
            )
    else:
        header = f"{'Name':<{name_w}}  {'Route':<{route_w}}  {'Type':<{type_w}}  Access"
        print(header)
        print("-" * len(header))
        for app in config.apps:
            print(
                f"{app.name:<{name_w}}  {app.route_prefix:<{route_w}}  "
                f"{app.app_type:<{type_w}}  {app.access}"
            )


def diagnose(
    app_dir: str,
    *,
    app_name: str = "",
    json: bool = False,
):
    """Diagnose an app directory for enlace compatibility.

    Scans for hardcoded URLs, CORS middleware, SSR requirements, missing
    entry points, and other patterns that block or complicate mounting.

    Args:
        app_dir: Path to the app directory to diagnose.
        app_name: Override app name (defaults to directory name).
        json: Output as JSON.
    """
    report = diagnose_app(app_dir, app_name=app_name)

    if json:
        print(report.to_json())
    else:
        print(report.format_text())

    if not report.is_enlaceable:
        sys.exit(1)


def main():
    argh.dispatch_commands([serve, show_config, check, list_apps, diagnose])


if __name__ == "__main__":
    main()
