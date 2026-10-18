"""The built distributions carry the agent skills.

``.claude/skills/<name>`` are symlinks to ``enlace/data/skills/<name>``.
hatchling follows symlinks and visits each real directory once, so unless the
sdist excludes ``.claude/skills`` it collects the skills under ``.claude`` and
drops ``enlace/data/skills`` -- and CI builds the wheel from that sdist, so
``pip install enlace`` shipped no skills. This lists what hatchling would put
in the sdist, without building anything.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from enlace import skills_dir

REPO = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(
    not (REPO / "pyproject.toml").is_file(), reason="needs a source tree"
)


def _sdist_paths() -> set[str]:
    sdist = pytest.importorskip("hatchling.builders.sdist")
    builder = sdist.SdistBuilder(str(REPO))
    return {f.distribution_path for f in builder.recurse_included_files()}


def test_sdist_includes_every_packaged_skill():
    paths = _sdist_paths()
    expected = {
        f"enlace/data/skills/{p.parent.name}/SKILL.md"
        for p in skills_dir().glob("*/SKILL.md")
    }
    assert expected, "no packaged skills found"
    assert expected <= paths, sorted(expected - paths)


def test_sdist_keeps_claude_md_but_not_the_skill_symlinks():
    paths = _sdist_paths()
    assert ".claude/CLAUDE.md" in paths
    assert not any(p.startswith(".claude/skills/") for p in paths)


def test_tomli_fallback_is_a_declared_dependency_before_311():
    """Modules fall back to ``tomli`` on 3.10; a plain install must have it (#92)."""
    import re

    try:
        import tomllib
    except ModuleNotFoundError:  # Python 3.10
        import tomli as tomllib

    uses_tomli = [
        p.name
        for p in (REPO / "enlace").glob("*.py")
        if re.search(r"^\s*import tomli\b", p.read_text(), re.M)
    ]
    deps = tomllib.loads((REPO / "pyproject.toml").read_text())["project"][
        "dependencies"
    ]
    declared = [
        d for d in deps if re.match(r"tomli\b", d) and "python_version < '3.11'" in d
    ]
    assert not uses_tomli or declared, f"{uses_tomli} import tomli; deps: {deps}"
