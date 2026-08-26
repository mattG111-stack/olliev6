"""Everything we import, we declare.

A deploy died on this:

    File "/app/app/notify.py", line 14, in <module>
        import httpx
    ModuleNotFoundError: No module named 'httpx'

Six of our modules import httpx. Not one line of our code had changed. httpx was
never in requirements.txt — it arrived as a dependency of the Anthropic SDK, and
that SDK, pinned only as `anthropic>=0.116`, released 1.0.0 and moved to httpx2:

    Collecting anthropic>=0.116 ... anthropic-1.0.0
    Collecting httpx2<3,>=2.0.0 (from anthropic>=0.116)

So the app could not start, the healthcheck retried eleven times over five
minutes, and the deploy failed — because of a package nobody touched.

Nothing in the test suite could have caught it: this machine has httpx installed
for its own reasons, so every import here succeeded. What is testable is the
DECLARATION. If our code imports it, requirements.txt has to name it, and then
it cannot vanish underneath us.
"""
from __future__ import annotations

import ast
import pathlib
import re
import sys

APP = pathlib.Path(__file__).resolve().parent.parent / "app"
REQUIREMENTS = pathlib.Path(__file__).resolve().parent.parent / "requirements.txt"

# Module name → the requirement that ships it, where the two differ.
PROVIDED_BY = {
    "jose": "python-jose",
    "multipart": "python-multipart",
    "dotenv": "python-dotenv",
    "pydantic_settings": "pydantic-settings",
    "email_validator": "email-validator",
    "dateutil": "python-dateutil",
    "yaml": "pyyaml",
    "PIL": "pillow",
    # Starlette is FastAPI's own dependency and FastAPI is pinned to an exact
    # version, so the version that arrives is fixed by that pin. Declaring it
    # separately risks a resolver conflict for no gain — unlike httpx, which
    # hung off a package pinned with >=.
    "starlette": "fastapi",
}


def _declared() -> set[str]:
    """Distribution names in requirements.txt, normalised."""
    out = set()
    for line in REQUIREMENTS.read_text().splitlines():
        line = line.split("#")[0].strip()
        if not line:
            continue
        name = re.split(r"[<>=!\[;]", line)[0].strip().lower()
        if name:
            out.add(name.replace("_", "-"))
    return out


def _imported() -> dict[str, list[str]]:
    """Third-party top-level modules our app imports, and where from."""
    found: dict[str, list[str]] = {}
    for path in sorted(APP.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:                       # not ours to police here
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:                    # relative: our own code
                    continue
                names = [node.module] if node.module else []
            else:
                continue
            for name in names:
                top = name.split(".")[0]
                if top in sys.stdlib_module_names or top == "app":
                    continue
                found.setdefault(top, []).append(str(path.relative_to(APP.parent)))
    return found


def test_every_third_party_import_is_declared():
    declared = _declared()
    missing = {}
    for module, users in _imported().items():
        want = PROVIDED_BY.get(module, module).lower().replace("_", "-")
        if want not in declared:
            missing[module] = sorted(set(users))[:4]

    assert not missing, (
        "imported but not in requirements.txt — these are one upstream release "
        "away from taking the app down at boot:\n"
        + "\n".join(f"  {m}  (imported by {', '.join(f)})" for m, f in missing.items())
    )


def test_httpx_is_declared_in_its_own_right():
    """The specific one that failed, kept as its own line.

    A general test can be satisfied by adding an entry to PROVIDED_BY. This one
    cannot: httpx has to be a requirement, because we import it and no package
    we depend on promises to bring it any more.
    """
    assert "httpx" in _declared(), (
        "httpx is imported directly by app/notify.py and five other modules. "
        "It must be declared, not inherited from an SDK."
    )


def test_nothing_is_free_to_change_version_on_a_deploy():
    """anthropic went 0.116 → 1.0.0 and openai 1.60 → 3.3.1 on a redeploy.

    Neither was a decision anyone made. This app rebuilds from requirements.txt
    on every push, so a requirement with no upper bound is a package that can
    change major version in a build that contains none of our commits — which is
    exactly how the app came to be missing httpx.

    A new dependency added without a bound fails here. That is the point: pick
    the bound while you are thinking about the package, not while the deploy is
    down.
    """
    unbounded = []
    for line in REQUIREMENTS.read_text().splitlines():
        line = line.split("#")[0].strip()
        if not line:
            continue
        if "<" not in line and "==" not in line and "~=" not in line:
            unbounded.append(line)

    assert not unbounded, (
        "no upper bound — these can jump a major version on a deploy that "
        "changes none of our code:\n" + "\n".join(f"  {ln}" for ln in unbounded)
    )
