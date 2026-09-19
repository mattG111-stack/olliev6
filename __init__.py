"""Makes the repository root importable as the package `app`.

The code uses relative imports (`from .db import ...`), so Python must be able
to find every module under the name `app`. When files are uploaded through
GitHub's web uploader they land at the repository ROOT instead of inside an
`app/` folder, and `uvicorn app.main:app` then dies with ModuleNotFoundError
before the server starts — the build still succeeds, so the only symptom is a
health check that times out five minutes later.

A package may extend its own search path, so `app` looks in the directory above
it as well as inside it. Harmless in a correct layout: the modules are already
in this folder and are found first.
"""
import os

__path__.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
