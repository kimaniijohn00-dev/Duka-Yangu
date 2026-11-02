"""Tests for modal_app.py import safety and dependency parity.

Importing modal_app.py must never read local project files: Modal
re-imports the module inside the container, where paths like
backend/requirements.txt do not exist. The only local touch (the
frontend/dist presence check) runs behind modal.is_local().
"""
import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def load_modal_app(name):
    """Import modal_app.py from its absolute path under a fresh name."""
    spec = importlib.util.spec_from_file_location(
        name, REPO_ROOT / "modal_app.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def requirements_entries():
    lines = (REPO_ROOT / "backend" / "requirements.txt").read_text().splitlines()
    return [
        line.strip()
        for line in lines
        if line.strip() and not line.strip().startswith("#")
    ]


def test_packages_match_requirements():
    module = load_modal_app("modal_app_parity")
    assert module.PYTHON_PACKAGES == requirements_entries()


def test_import_with_empty_cwd(tmp_path, monkeypatch):
    """Importing with no project files around must not raise."""
    monkeypatch.chdir(tmp_path)
    load_modal_app("modal_app_empty_cwd")
