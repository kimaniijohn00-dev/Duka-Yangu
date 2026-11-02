"""Duka-Yangu on Modal: one app serving the API and the built frontend.

Deploy with ``modal deploy modal_app.py`` (see DEPLOY.md). This follows the
Modal docs only:

- Web endpoint: ``@modal.asgi_app()`` on a function returning the FastAPI
  app (docs: "Web Functions" -> "Serving ASGI and WSGI apps" -> "ASGI apps").
- Image: ``modal.Image.debian_slim(python_version="3.12")`` with the
  ``PYTHON_PACKAGES`` list below (kept identical to
  ``backend/requirements.txt`` by tests), plus the backend source and
  the built frontend bundle ``frontend/dist`` (docs: "Images").
- Import safety: importing this file never reads local project files.
  The only local touches (the ``frontend/dist`` presence check) run
  behind ``modal.is_local()``, so a container-side re-import cannot fail
  on missing local paths.
- SQLite persistence: the ``duka-yangu-data`` Volume mounted at ``/data``,
  with ``DUKA_DB_PATH=/data/duka.db`` (docs: "Volumes" ->
  ``Volume.from_name`` + ``volumes={...}``). Per the docs' "Volume commits
  and reloads" section, attached Volumes are committed by background
  commits plus a final commit on container shutdown, so no application
  code is needed to persist writes; with ``max_containers=1`` there is a
  single writer, so no cross-container staleness either.
- Secrets: ``modal.Secret.from_name("duka-yangu")`` injects ``MODEL_API_KEY``
  and ``SPARK_MODEL`` as environment variables (docs: "Secrets"). No keys
  appear in code or in the image.
- Scaling: ``max_containers=1`` (SQLite has a single writer) with a
  ``scaledown_window`` so idle containers linger briefly (docs:
  "Scaling out").
"""

from pathlib import Path

import modal

REPO_ROOT = Path(__file__).resolve().parent
BACKEND_DIR = REPO_ROOT / "backend"
FRONTEND_DIST = REPO_ROOT / "frontend" / "dist"

if modal.is_local() and not FRONTEND_DIST.is_dir():
    raise RuntimeError(
        "frontend/dist is missing. Build it first with `npm run build` "
        "inside frontend/ (see DEPLOY.md), then redeploy."
    )


# Plain pinned list: no file reads at import time, so importing this
# module inside the container (where local project files are absent)
# cannot raise FileNotFoundError. Kept identical to
# backend/requirements.txt by backend/tests/test_modal_app.py.
PYTHON_PACKAGES = [
    "openai",
    "python-dotenv",
    "fastapi",
    "uvicorn",
    "pytest",
    "httpx",
    "python-multipart",
]


app = modal.App("duka-yangu")

data_volume = modal.Volume.from_name("duka-yangu-data", create_if_missing=True)

image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install(*PYTHON_PACKAGES)
    .env({"DUKA_DB_PATH": "/data/duka.db", "DUKA_STATIC_DIR": "/app/frontend/dist"})
    .add_local_dir(str(BACKEND_DIR), remote_path="/app/backend")
    .add_local_dir(str(FRONTEND_DIST), remote_path="/app/frontend/dist")
)


@app.function(
    image=image,
    volumes={"/data": data_volume},
    secrets=[modal.Secret.from_name("duka-yangu")],
    max_containers=1,
    scaledown_window=300,
)
@modal.asgi_app()
def web():
    """Expose the FastAPI app (API routes plus static frontend)."""
    import sys

    sys.path.insert(0, "/app/backend")

    from app import app as fastapi_app

    return fastapi_app
