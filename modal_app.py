"""Duka-Yangu on Modal: one app serving the API and the built frontend.

Deploy with ``modal deploy modal_app.py`` (see DEPLOY.md). This follows the
Modal docs only:

- Web endpoint: ``@modal.asgi_app()`` on a function returning the FastAPI
  app (docs: "Web Functions" -> "Serving ASGI and WSGI apps" -> "ASGI apps").
- Image: ``modal.Image.debian_slim(python_version="3.12")`` with the
  packages from ``backend/requirements.txt``, plus the backend source and
  the built frontend bundle ``frontend/dist`` (docs: "Images").
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

if not FRONTEND_DIST.is_dir():
    raise RuntimeError(
        "frontend/dist is missing. Build it first with `npm run build` "
        "inside frontend/ (see DEPLOY.md), then redeploy."
    )


def _requirements() -> list[str]:
    """The packages from backend/requirements.txt, in file order."""
    packages = []
    for line in (BACKEND_DIR / "requirements.txt").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            packages.append(line)
    return packages


app = modal.App("duka-yangu")

data_volume = modal.Volume.from_name("duka-yangu-data", create_if_missing=True)

image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install(*_requirements())
    .env({"DUKA_DB_PATH": "/data/duka.db"})
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
