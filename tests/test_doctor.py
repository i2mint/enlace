

# ---------------------------------------------------------------------- #
# Plugin check discovery
#
# A plugin can ship a correct diagnosis of its own failure mode and have it
# never execute: `extra_static_checks` was hand-wiring nobody did. Discovery
# ties the checks that RUN to the plugins that RUN.
# ---------------------------------------------------------------------- #

import sys
import types

from enlace.doctor import discover_plugin_checks


def _fake_plugin(monkeypatch, name, *, static=(), http=(), broken=False):
    pkg = types.ModuleType(name)
    monkeypatch.setitem(sys.modules, name, pkg)
    if broken:
        return
    diag = types.ModuleType(f"{name}.diagnostics")
    diag.static_checks = static
    diag.http_checks = http
    monkeypatch.setitem(sys.modules, f"{name}.diagnostics", diag)


def test_discovers_checks_from_a_configured_plugin(monkeypatch):
    def a(config):
        return []

    def b(config, base_url, timeout):
        return []

    _fake_plugin(monkeypatch, "fakeplug", static=(a,), http=(b,))
    static, http = discover_plugin_checks("fakeplug:plugin")
    assert static == (a,) and http == (b,)


def test_reads_ENLACE_PLUGINS_by_default(monkeypatch):
    def a(config):
        return []

    _fake_plugin(monkeypatch, "envplug", static=(a,))
    monkeypatch.setenv("ENLACE_PLUGINS", "envplug:plugin")
    static, _ = discover_plugin_checks()
    assert static == (a,)


def test_a_plugin_without_diagnostics_contributes_nothing(monkeypatch):
    _fake_plugin(monkeypatch, "plainplug", broken=True)
    assert discover_plugin_checks("plainplug:plugin") == ((), ())


def test_an_unimportable_plugin_never_breaks_the_doctor():
    """Doctor reports problems; it must not become one."""
    assert discover_plugin_checks("no_such_module_at_all:plugin") == ((), ())


def test_each_package_is_collected_once(monkeypatch):
    def a(config):
        return []

    _fake_plugin(monkeypatch, "dupplug", static=(a,))
    static, _ = discover_plugin_checks("dupplug:plugin,dupplug:other")
    assert static == (a,)


def test_empty_and_blank_specs_are_harmless():
    assert discover_plugin_checks("") == ((), ())
    assert discover_plugin_checks("  ,  ") == ((), ())
