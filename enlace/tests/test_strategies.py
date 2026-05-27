"""Tests for the backend-strategy registry and built-in strategies.

Covers the open/closed extension point that makes external plugins (like
``enlace_docker``) pluggable without core changes:

- Built-in registration (``asgi`` / ``process`` / ``external`` / ``static``).
- ``get_strategy`` error message for unknown modes.
- Per-strategy TOML field-map contributions (no collision among built-ins).
- ``AppConfig.mode`` validation routes to the right strategy.
- A fake plugin strategy: register, discover via app.toml, validate, mount.
"""

import pytest
from pydantic import ValidationError

from enlace import strategies as strat
from enlace.base import AppConfig
from enlace.strategies import (
    AsgiStrategy,
    BackendStrategy,
    ExternalStrategy,
    ProcessStrategy,
    StaticStrategy,
    collect_strategy_field_maps,
    get_strategy,
    known_modes,
    register_strategy,
)

# -- Built-in registry --------------------------------------------------------


def test_builtin_modes_registered():
    """All four built-in strategies are registered at import time."""
    modes = known_modes()
    for name in ("asgi", "process", "external", "static"):
        assert name in modes


def test_get_strategy_returns_correct_class():
    assert isinstance(get_strategy("asgi"), AsgiStrategy)
    assert isinstance(get_strategy("process"), ProcessStrategy)
    assert isinstance(get_strategy("external"), ExternalStrategy)
    assert isinstance(get_strategy("static"), StaticStrategy)


def test_get_strategy_unknown_mode_raises_actionable_error():
    with pytest.raises(ValueError, match="Unknown app mode"):
        get_strategy("does-not-exist-xyz")


def test_get_strategy_error_lists_known_modes():
    """Error message includes the known modes so the user can spot typos."""
    with pytest.raises(ValueError) as exc:
        get_strategy("docker_xyz")
    msg = str(exc.value)
    assert "asgi" in msg and "process" in msg


# -- is_supervisable flag -----------------------------------------------------


def test_only_process_is_supervisable_by_default():
    """Among built-ins, only the process strategy claims to need supervision."""
    assert ProcessStrategy.is_supervisable is True
    assert AsgiStrategy.is_supervisable is False
    assert ExternalStrategy.is_supervisable is False
    assert StaticStrategy.is_supervisable is False


# -- skip_python_introspection ------------------------------------------------


def test_only_asgi_inspects_python():
    """asgi-mode is the only built-in that does Python module introspection."""
    assert AsgiStrategy.skip_python_introspection is False
    assert ProcessStrategy.skip_python_introspection is True
    assert ExternalStrategy.skip_python_introspection is True
    assert StaticStrategy.skip_python_introspection is True


# -- TOML field-map contributions ---------------------------------------------


def test_no_field_map_collisions_among_builtins():
    """Built-in strategies must not redefine the same TOML key differently.

    Plugins MAY redefine keys with the same target field (e.g. enlace_docker
    might also use ``port``), but built-ins should be disjoint.
    """
    seen: dict[str, str] = {}
    for strategy in (
        AsgiStrategy(),
        ProcessStrategy(),
        ExternalStrategy(),
        StaticStrategy(),
    ):
        for toml_key, field_name in strategy.toml_field_map.items():
            if toml_key in seen and seen[toml_key] != field_name:
                pytest.fail(
                    f"Collision: TOML key {toml_key!r} maps to "
                    f"{seen[toml_key]!r} in one strategy and {field_name!r} in another"
                )
            seen[toml_key] = field_name


def test_collect_strategy_field_maps_includes_builtins():
    """Discovery's overlay routine merges in every registered strategy's keys."""
    field_map, path_keys = collect_strategy_field_maps()
    # Spot-check one key per strategy.
    assert field_map.get("entry_point") == "entry_module_path"  # asgi
    assert field_map.get("command") == "command"  # process
    assert field_map.get("upstream_url") == "upstream_url"  # external
    assert field_map.get("public_dir") == "public_dir"  # static
    # path_keys aggregates per-strategy path declarations.
    assert "entry_point" in path_keys
    assert "public_dir" in path_keys


# -- Mode validation delegates to strategy ------------------------------------


def test_unknown_mode_raises_validation_error():
    """AppConfig with an unknown mode surfaces the registry's error."""
    with pytest.raises(ValidationError, match="Unknown app mode"):
        AppConfig(
            name="x",
            route_prefix="/x",
            app_type="asgi_app",
            mode="this-mode-does-not-exist",
        )


def test_process_mode_validation_runs_via_strategy():
    """ProcessStrategy.validate is what enforces 'command' requirement now."""
    with pytest.raises(ValidationError, match="requires 'command'"):
        AppConfig(
            name="bad",
            route_prefix="/api/bad",
            app_type="asgi_app",
            mode="process",
            port=9100,
        )


# -- register_strategy --------------------------------------------------------


def test_register_strategy_requires_name():
    """Empty-name strategies are rejected (would be invisible in the registry)."""

    class _NoName(BackendStrategy):
        name = ""

    with pytest.raises(ValueError, match="empty 'name'"):
        register_strategy(_NoName())


# -- Plugin strategy round-trip ----------------------------------------------


class _FakeDockerStrategy(BackendStrategy):
    """A pretend plugin strategy used to exercise the extension point.

    Mirrors what ``enlace_docker`` will register in Phase 2 — without any
    real docker dependency.
    """

    name = "fake_docker"
    skip_python_introspection = True
    is_supervisable = True
    toml_field_map = {
        "image": "upstream_url",  # piggyback on existing AppConfig field
    }
    path_keys: set[str] = set()

    def validate(self, app):
        if not app.upstream_url:
            raise ValueError(
                f"App '{app.name}': mode='fake_docker' requires 'image'"
            )

    def make_asgi(self, app, platform):
        # Sentinel object, not a real ASGI app — we just assert it round-trips.
        return ("FAKE_PROXY", app.upstream_url)

    def make_lifecycle(self, app, platform):
        return None  # in real life this would return a container Lifecycle


@pytest.fixture
def fake_docker_registered():
    """Register the fake strategy for one test, then clean up."""
    register_strategy(_FakeDockerStrategy())
    try:
        yield
    finally:
        strat._STRATEGIES.pop("fake_docker", None)


def test_plugin_strategy_makes_unknown_mode_known(fake_docker_registered):
    assert "fake_docker" in known_modes()


def test_plugin_strategy_validates(fake_docker_registered):
    """A plugin strategy's validate() is called by AppConfig validation."""
    with pytest.raises(ValidationError, match="requires 'image'"):
        AppConfig(
            name="img",
            route_prefix="/img",
            app_type="asgi_app",
            mode="fake_docker",
        )


def test_plugin_strategy_make_asgi_called_by_compose(
    tmp_path, fake_docker_registered, monkeypatch
):
    """build_backend dispatches plugin modes through the registry."""
    from enlace.base import PlatformConfig
    from enlace.compose import build_backend

    app = AppConfig(
        name="img",
        route_prefix="/img",
        app_type="asgi_app",
        mode="fake_docker",
        upstream_url="ghcr.io/example/foo:1.0",
        source_dir=tmp_path,
    )
    platform = PlatformConfig(apps=[app], apps_dirs=[tmp_path])

    # We don't need the full app to actually serve — we just need build_backend
    # to not crash and to have called our fake strategy. The sentinel returned
    # by make_asgi is not ASGI-compatible, so we intercept the mount call.
    mounts: list[tuple[str, object]] = []

    monkeypatch.setattr(
        "enlace.compose.FastAPI.mount",
        lambda self, prefix, sub_app, **kw: mounts.append((prefix, sub_app)),
    )

    build_backend(platform)

    # The fake strategy's sentinel sub-app should have been mounted at /img.
    matching = [m for m in mounts if m[0] == "/img"]
    assert matching, f"expected /img mount, got {mounts!r}"


def test_plugin_strategy_appears_in_serve_supervised_apps(
    tmp_path, fake_docker_registered
):
    """A plugin strategy with is_supervisable=True is included in supervision."""
    from enlace.base import AppConfig, PlatformConfig
    from enlace.strategies import get_strategy

    app = AppConfig(
        name="img",
        route_prefix="/img",
        app_type="asgi_app",
        mode="fake_docker",
        upstream_url="ghcr.io/example/foo:1.0",
    )
    platform = PlatformConfig(apps=[app])

    # Mirror the supervision predicate used in serve.serve()
    supervised = [
        a for a in platform.apps if get_strategy(a.mode).is_supervisable
    ]
    assert [a.name for a in supervised] == ["img"]
