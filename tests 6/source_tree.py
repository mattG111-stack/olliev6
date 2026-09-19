"""Which files are the application, now that the application is the repository.

Several tests read the source rather than calling it — "there is exactly one
definition of address_key", "no module writes the ceiling out again by hand".
Each of them used to say `glob.glob("app/**/*.py")`, and that one word did two
jobs: it found the code, and it EXCLUDED everything that is not the code.

The application now sits at the repository root, because that is the layout the
deployment has. The root is not the same thing as the application: tests/,
scripts/ and alembic/ live there too. A sweep that took the root literally
would find a second definition of every rule — inside the very test that checks
there is only one — and report a fault that does not exist. Worse, it would do
it convincingly.

So the boundary is stated once, here, rather than guessed at in a dozen files.

Paths come back relative to the repository root, and pytest runs with that as
the working directory, so `open(f)` on one of them works exactly as the old
glob strings did.
"""
from __future__ import annotations

import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent

# Everything at the root that is NOT application code. If you add a folder here
# that is neither, add it to this set in the same commit.
NOT_APP = {"tests", "scripts", "alembic", "docs", "node_modules", "__pycache__"}


def app_files() -> list[str]:
    """Every .py file of the application, repository-relative, sorted."""
    out = [p for p in ROOT.glob("*.py")]
    for d in sorted(ROOT.iterdir()):
        if not d.is_dir() or d.name.startswith(".") or d.name in NOT_APP:
            continue
        out += [p for p in d.rglob("*.py") if "__pycache__" not in p.parts]
    return sorted(str(p.relative_to(ROOT).as_posix()) for p in out)
