"""Python fed to the interpreter from a shell heredoc must load .env by explicit path.

With no argument, python-dotenv's load_dotenv() finds .env by walking the caller's stack
for a frame whose file exists on disk. Code read from stdin (``python - <<PY``) has no
such frame, so the walk runs off the end and fails an assert. The post-mortem pipeline's
summary step crashed this way on 2026-10-15, the first run after the bare call was added
on 2026-10-13, and nothing exercised it in between.

Contract under test: no shell script under scripts/ calls load_dotenv() with no
argument. Pass the path, e.g. ``load_dotenv(Path("${ROOT_DIR}") / ".env")``.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BARE_LOAD_DOTENV = re.compile(r"\bload_dotenv\(\s*\)")

SHELL_SCRIPTS = sorted((ROOT / "scripts").rglob("*.sh"))


def test_shell_scripts_are_found():
    # Guards against the glob silently matching nothing, which would pass every case below.
    assert SHELL_SCRIPTS, "no shell scripts found under scripts/"


@pytest.mark.parametrize(
    "script", SHELL_SCRIPTS, ids=lambda p: str(p.relative_to(ROOT))
)
def test_no_bare_load_dotenv_in_shell_heredocs(script):
    offending = [
        f"{script.relative_to(ROOT)}:{number}: {line.strip()}"
        for number, line in enumerate(script.read_text().splitlines(), start=1)
        if BARE_LOAD_DOTENV.search(line)
    ]
    assert not offending, (
        "load_dotenv() with no path fails when the Python is read from stdin; "
        "pass the .env path explicitly:\n" + "\n".join(offending)
    )
