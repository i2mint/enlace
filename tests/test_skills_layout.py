"""The agent skills live in one place, and Claude Code sees that same place.

The real skill files live in ``enlace/data/skills/<name>/`` (they ship with
``pip install`` and ``gh skill`` finds them there). ``.claude/skills/<name>``
must be a relative symlink to that directory, never a copy: two copies drifted
apart before, each carrying edits the other lacked.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from enlace import skills_dir

REPO = Path(__file__).resolve().parents[1]
CLAUDE_SKILLS = REPO / ".claude" / "skills"
PACKAGED = sorted(p.parent.name for p in skills_dir().glob("*/SKILL.md"))

pytestmark = pytest.mark.skipif(
    not CLAUDE_SKILLS.is_dir(), reason="not a source checkout (no .claude/skills)"
)


def test_there_are_packaged_skills():
    assert PACKAGED, f"no SKILL.md under {skills_dir()}"


@pytest.mark.parametrize("name", PACKAGED)
def test_claude_skill_is_a_relative_symlink_to_the_packaged_one(name):
    link = CLAUDE_SKILLS / name
    assert link.is_symlink(), f"{link} must be a symlink, not a copy"
    target = Path(link.readlink())
    assert not target.is_absolute(), f"{link} -> {target} must be relative"
    assert link.resolve() == (skills_dir() / name).resolve()


def test_no_claude_skill_without_a_packaged_source():
    stray = sorted(
        p.name
        for p in CLAUDE_SKILLS.iterdir()
        if not p.name.startswith(".") and p.name not in PACKAGED
    )
    assert not stray, f"only in .claude/skills (move them to the package): {stray}"


@pytest.mark.parametrize("name", PACKAGED)
def test_skill_frontmatter_is_spec_valid(name):
    text = (skills_dir() / name / "SKILL.md").read_text()
    m = re.match(r"---\n(.*?)\n---\n", text, re.S)
    assert m, "SKILL.md must open with a --- frontmatter block"
    front = m.group(1)
    assert re.search(rf"^name: {re.escape(name)}$", front, re.M), "name == folder"
    assert re.fullmatch(r"[a-z0-9-]+", name)
    desc = front.split("description:", 1)[1]
    desc = " ".join(line.strip() for line in desc.splitlines() if line.strip() != ">")
    assert 0 < len(desc) <= 1024, len(desc)
