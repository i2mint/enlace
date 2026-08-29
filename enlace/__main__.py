"""CLI entry point for enlace.

Usage::

    enlace serve              # Start the backend server
    enlace show-config        # Show resolved configuration
    enlace check              # Validate configuration
    enlace list-apps          # List discovered apps
"""

import json as json_module
import os
import sys
from pathlib import Path
from typing import Optional

import argh

from enlace.base import PlatformConfig
from enlace.diagnose import diagnose_app
from enlace.discover import ImportErrorPolicy, discover_apps
from enlace.doctor import discover_plugin_checks, run_doctor
from enlace.serve import serve

# The diagnostic verbs (show-config, check, list-apps, app-meta, doctor) report
# ON apps; they must survive one that cannot be imported in order to say so.
# `serve` and `build_backend` keep the default "raise" — a gateway must not boot
# pretending a broken app is fine. `build` keeps it too: it is a deploy step,
# not a report.
_DIAGNOSTIC_IMPORT_POLICY: ImportErrorPolicy = "record"


def _build_config(
    apps_dir: str = "",
    apps_dirs: str = "",
    app_dirs: str = "",
    *,
    on_import_error: ImportErrorPolicy = "raise",
) -> PlatformConfig:
    """Build PlatformConfig from TOML, with CLI args as overrides.

    When no CLI directory args are given, uses platform.toml values.

    ``on_import_error`` is forwarded to :func:`enlace.discover.discover_apps`.
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

    return discover_apps(config, on_import_error=on_import_error)


def _import_error_lines(config: PlatformConfig) -> list[str]:
    """One line per app whose entry module failed to import, or an empty list."""
    return [
        f"{app.name}: entry module failed to import — {app.import_error}"
        for app in config.apps
        if app.import_error is not None
    ]


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
    config = _build_config(
        apps_dir, apps_dirs, app_dirs, on_import_error=_DIAGNOSTIC_IMPORT_POLICY
    )

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

            if app.import_error:
                print(f"    import:   FAILED — {app.import_error}")

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

            if app.build and app.build.build:
                print(f"    build:    {' '.join(app.build.build)}")
                if app.build.cwd:
                    print(f"    build-cwd: {app.build.cwd}")
                if verbose:
                    if app.build.install:
                        print(f"    build-install: {' '.join(app.build.install)}")
                    if app.build.env_vars:
                        print(f"    build-env: {', '.join(app.build.env_vars)}")

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
    from enlace.build import validate_build

    config = _build_config(
        apps_dir, apps_dirs, app_dirs, on_import_error=_DIAGNOSTIC_IMPORT_POLICY
    )

    # An un-importable app is an error, not a warning: `serve` would refuse to
    # boot on it. Recording it rather than raising changes only the *shape* of
    # the report — every healthy app still gets validated and listed.
    errors = _import_error_lines(config) + config.check_conflicts()
    warnings: list[str] = []
    for app in config.apps:
        warnings.extend(validate_build(app))

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
    config = _build_config(
        apps_dir, apps_dirs, app_dirs, on_import_error=_DIAGNOSTIC_IMPORT_POLICY
    )

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

    import_errors = _import_error_lines(config)
    if import_errors:
        print()
        print("Un-importable apps (they are listed above but will not mount):")
        for line in import_errors:
            print(f"  - {line}")


def build(
    app_name: str = "",
    *,
    dry_run: bool = False,
    apps_dir: str = "",
    apps_dirs: str = "",
    app_dirs: str = "",
):
    """Run declarative frontend builds for apps with a ``[build]`` section.

    Builds the named app, or every app that declares ``[build]`` when no name
    is given. This is the explicit pre-deploy step — enlace never builds at
    request time. Deployers may inject env values (e.g. VITE_API_BASE) into
    the environment before calling this; enlace passes the environment through.

    Args:
        app_name: Build only this app. Omit to build all apps with a build.
        dry_run: Print the commands that would run, without executing them.
        apps_dir: Path to the apps directory.
        apps_dirs: Comma-separated container directories.
        app_dirs: Comma-separated individual app directories.
    """
    from enlace.build import build_cwd, has_build, run_build

    config = _build_config(apps_dir, apps_dirs, app_dirs)
    targets = [a for a in config.apps if has_build(a)]
    if app_name:
        targets = [a for a in targets if a.name == app_name]
        if not targets:
            print(
                f"No app named {app_name!r} with a [build] section.",
                file=sys.stderr,
            )
            sys.exit(1)
    if not targets:
        print("No apps declare a [build] section. Nothing to build.")
        return

    for app in targets:
        cwd = build_cwd(app)
        result = run_build(app, dry_run=dry_run)
        verb = "Would build" if dry_run else "Building"
        print(f"{verb} {app.name} (cwd: {cwd})")
        for command in result.commands:
            print(f"  $ {' '.join(command)}")
        if not dry_run and result.returncode != 0:
            print(f"Build failed for {app.name} (exit {result.returncode})")
            sys.exit(1)


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


def _load_envfile(path: str) -> bool:
    """Parse a simple KEY=VALUE envfile into ``os.environ``.

    Only handles the systemd/shell common case: blank lines, ``#`` comments,
    and ``KEY=VALUE`` (value may be wrapped in single or double quotes). This
    is intentionally not a full shell parser — deploy envfiles are expected
    to avoid exotic syntax.

    Returns ``True`` if the file was loaded, ``False`` if it was skipped due
    to a recoverable read error (e.g. a permission issue when ``doctor`` is
    invoked by an unprivileged user against a ``root:root 0600`` envfile —
    intentional in some deploy topologies). The caller (``doctor``) auto-
    skips env-based checks when this returns ``False`` so the rest of the
    smoke still runs.

    A genuinely missing path still hard-fails with ``sys.exit(2)``, because
    that almost always means a typo or wrong path rather than a deliberate
    permission topology.
    """
    p = Path(path).expanduser()
    if not p.is_file():
        print(f"envfile not found: {p}", file=sys.stderr)
        sys.exit(2)
    try:
        text = p.read_text()
    except OSError as exc:
        # Most often PermissionError: envfile is intentionally root-only
        # and we're a non-root caller. Warn + degrade.
        print(
            f"envfile not readable ({exc.strerror or exc}: {p}); "
            f"continuing without env-based checks.",
            file=sys.stderr,
        )
        return False
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        os.environ[key] = value
    return True


def doctor(
    *,
    base_url: str = "",
    timeout: float = 5.0,
    apps: str = "",
    json: bool = False,
    envfile: str = "",
    skip_env_checks: bool = False,
    apps_dir: str = "",
    apps_dirs: str = "",
    app_dirs: str = "",
):
    """Probe a running enlace gateway for silent-degradation failures.

    Static checks cover signing-key / shared-password env vars (only
    meaningful when run in the gateway's env — see ``--envfile`` /
    ``--skip-env-checks``), oauth importability, and frontend_dir sanity.
    HTTP probes run when ``--base-url`` is given, against ``/auth/csrf``,
    each app's frontend, and each app's API prefix.

    An app whose entry module cannot be imported is reported as a ``FAIL``
    check naming the app and its exception class, rather than taking the
    doctor down with a traceback before it checks anything else.

    Exits nonzero if any check fails. Intended as a post-deploy smoke tool.

    Args:
        base_url: Base URL of a running gateway (e.g. http://127.0.0.1:8010).
            When omitted, runs static checks only.
        timeout: Per-request timeout for HTTP probes, seconds.
        apps: Comma-separated app names to restrict HTTP probes to.
            Static checks still run across all apps.
        json: Output as JSON (for CI / deploy pipelines).
        envfile: Path to a KEY=VALUE envfile to load before running env
            checks. Use this when probing from outside the service process
            (e.g. a deploy script sourcing /opt/tw_platform/.env).
        skip_env_checks: Skip signing-key / shared-password env checks
            entirely. Use when you are running from a shell that doesn't
            have the gateway's env and you trust the HTTP probes as the
            authoritative signal.
        apps_dir: Path to the apps directory.
        apps_dirs: Comma-separated container directories.
        app_dirs: Comma-separated individual app directories.
    """
    if envfile:
        # If the envfile is unreadable (e.g. root:root 0600 and we are the
        # deploy user) ``_load_envfile`` warns and returns False; auto-skip
        # env checks so the HTTP / config checks still run.
        if not _load_envfile(envfile):
            skip_env_checks = True

    config = _build_config(
        apps_dir, apps_dirs, app_dirs, on_import_error=_DIAGNOSTIC_IMPORT_POLICY
    )
    app_filter: Optional[list[str]] = None
    if apps:
        app_filter = [a.strip() for a in apps.split(",") if a.strip()]

    # Checks contributed by whatever plugins this platform loads (e.g.
    # enlace_auth's auth + OAuth probes). Without this the plugin's own
    # diagnostics never execute, however correct they are.
    plugin_static, plugin_http = discover_plugin_checks()
    report = run_doctor(
        config,
        base_url=base_url or None,
        timeout=timeout,
        app_filter=app_filter,
        include_env_checks=not skip_env_checks,
        extra_static_checks=plugin_static,
        extra_http_checks=plugin_http,
    )

    if json:
        print(json_module.dumps(report.as_dict(), indent=2, default=str))
    else:
        print(report.format_text())

    if not report.ok:
        sys.exit(1)


def app_meta(
    *,
    json: bool = False,
    apps_dir: str = "",
    apps_dirs: str = "",
    app_dirs: str = "",
):
    """Show each app's resolved launcher metadata + provenance (title, keywords, icon).

    A local migration/debug aid: it re-harvests from the filesystem, so it
    reflects live edits without a server restart, and prints the provenance
    (which source each value came from). This is a LOCAL CLI, not an HTTP
    surface — it may show absolute filesystem paths, which the ``/_apps``
    endpoint deliberately never exposes.

    Args:
        json: Output as JSON.
        apps_dir: Path to the apps directory.
        apps_dirs: Comma-separated container directories.
        app_dirs: Comma-separated individual app directories.
    """
    from enlace import appmeta

    config = _build_config(
        apps_dir, apps_dirs, app_dirs, on_import_error=_DIAGNOSTIC_IMPORT_POLICY
    )
    default_icon = config.app_meta.default_icon

    records = []
    for app in config.apps:
        tier_b = config.app_meta.apps.get(app.name)
        b_keywords = tier_b.keywords if tier_b else []
        merged, sources = appmeta.resolve_keywords(
            app_keywords=app.keywords,
            platform_keywords=b_keywords,
            overlay_keywords=[],  # overlay lives in the plugin store, not here
        )
        icon_spec = (tier_b.icon if tier_b else None) or app.icon or default_icon or ""
        icon = appmeta.resolve_icon(
            icon_spec,
            app_name=app.name,
            display_name=app.display_name,
            app_dir=appmeta.app_dir_of(app),
        )
        icon_kind = (
            "redirect"
            if icon.redirect_url
            else ("monogram" if not icon_spec else icon_spec)
        )
        records.append(
            {
                "name": app.name,
                "display_name": app.display_name,
                "description": app.description,
                "keywords": merged,
                "keyword_sources": sources,
                "icon": icon_spec or "(monogram)",
                "icon_kind": icon_kind,
                "icon_content_type": icon.content_type or "redirect",
                "provenance": {
                    k: app.provenance.get(k)
                    for k in ("display_name", "description", "keywords", "icon")
                    if app.provenance.get(k)
                },
            }
        )

    if json:
        print(json_module.dumps(records, indent=2))
        return

    if not records:
        print("No apps discovered.")
        return
    for r in records:
        prov = r["provenance"]
        src = r["keyword_sources"]
        title_src = prov.get("display_name", "derived")
        desc = r["description"] or "(none)"
        icon_src = prov.get("icon", "derived")
        print(f"{r['name']}")
        print(f"    title:       {r['display_name']}  [{title_src}]")
        print(f"    description: {desc}  [{prov.get('description', 'none')}]")
        print(f"    keywords:    {', '.join(r['keywords']) or '(none)'}")
        print(f"       app={src['app']} platform={src['platform']}")
        print(f"    icon:        {r['icon']}  [{icon_src}] -> {r['icon_content_type']}")
        print()


def main():
    argh.dispatch_commands(
        [
            serve,
            show_config,
            check,
            list_apps,
            app_meta,
            build,
            diagnose,
            doctor,
        ]
    )


if __name__ == "__main__":
    main()
