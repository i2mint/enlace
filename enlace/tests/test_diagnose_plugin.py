"""Tests for the diagnoser extension point (enlace.diagnosers)."""

import enlace.diagnose as diag
from enlace.diagnose import (
    Issue,
    Severity,
    diagnose_app,
    register_diagnoser,
)


def _reset_diagnosers():
    diag._DIAGNOSERS.clear()
    diag._DIAGNOSER_EPS_LOADED = True  # skip entry-point scan in unit tests


def test_registered_diagnoser_runs_and_appends_issue(tmp_path):
    _reset_diagnosers()

    def my_diagnoser(app_dir, report):
        report.issues.append(
            Issue(
                severity=Severity.MEDIUM,
                category="docker_healthcheck",  # plugin-defined str category
                summary="no HEALTHCHECK in Dockerfile",
            )
        )

    register_diagnoser(my_diagnoser)
    report = diagnose_app(tmp_path, app_name="x")

    summaries = [i.summary for i in report.issues]
    assert "no HEALTHCHECK in Dockerfile" in summaries


def test_plugin_string_category_renders_in_report_and_json(tmp_path):
    _reset_diagnosers()

    def my_diagnoser(app_dir, report):
        report.issues.append(
            Issue(
                severity=Severity.CRITICAL,
                category="compose_multi_port",
                summary="multiple published ports without 'service'",
            )
        )

    register_diagnoser(my_diagnoser)
    report = diagnose_app(tmp_path, app_name="x")

    # Text rendering uses the plugin's string category verbatim.
    assert "compose_multi_port" in report.format_text()
    # JSON too.
    assert "compose_multi_port" in report.to_json()
    # A CRITICAL plugin issue flips the verdict.
    assert report.is_enlaceable is False


def test_register_diagnoser_is_idempotent(tmp_path):
    _reset_diagnosers()
    calls = []

    def my_diagnoser(app_dir, report):
        calls.append(1)

    register_diagnoser(my_diagnoser)
    register_diagnoser(my_diagnoser)  # second registration is a no-op
    diagnose_app(tmp_path, app_name="x")
    assert len(calls) == 1


def test_misbehaving_diagnoser_does_not_sink_report(tmp_path):
    _reset_diagnosers()

    def boom(app_dir, report):
        raise RuntimeError("plugin bug")

    register_diagnoser(boom)
    # Should not raise; report still produced.
    report = diagnose_app(tmp_path, app_name="x")
    assert report.app_name == "x"
