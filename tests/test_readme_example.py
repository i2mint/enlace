"""The README's minimal runnable example runs and prints what it says it prints.

The block follows the ``tested by tests/test_readme_example.py`` marker in
``README.md``. Its last line is a comment holding the expected output.
"""

from __future__ import annotations

import contextlib
import io
import re
import tempfile
from pathlib import Path

README = Path(__file__).resolve().parents[1] / "README.md"
MARKER = "<!-- tested by tests/test_readme_example.py"


def _example_block() -> str:
    text = README.read_text()
    after = text[text.index(MARKER) :]
    m = re.search(r"```python\n(.*?)```", after, re.S)
    assert m, "no python block after the README marker"
    return m.group(1)


def test_readme_minimal_example_runs_and_prints_its_expected_output(
    tmp_path, monkeypatch
):
    # Env vars override platform.toml by design; the example assumes none set.
    for var in ("ENLACE_APPS_DIRS", "ENLACE_APP_DIRS", "ENLACE_APPS_DIR"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))  # mkdtemp lands here
    code = _example_block()
    expected = code.rstrip().splitlines()[-1]
    assert expected.startswith("# "), "last line must be '# <expected output>'"

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        exec(compile(code, str(README), "exec"), {"__name__": "readme_example"})

    assert out.getvalue().strip() == expected[2:]
