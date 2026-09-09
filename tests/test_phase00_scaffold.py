"""Phase 0 - verify the project scaffold and environment are in place."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

REQUIRED_DIRS = [
    "data", "src", "sql", "alteryx", "dashboard", "streamlit",
    "reports", "logs", "tests", "config", "scripts", "docs", "models",
]

REQUIRED_FILES = [
    "README.md", "requirements.txt", ".env.example", ".gitignore",
    "run_pipeline.py", "docs/PHASE_STATUS.md",
]


def test_required_directories_exist():
    missing = [d for d in REQUIRED_DIRS if not (PROJECT_ROOT / d).is_dir()]
    assert not missing, f"missing directories: {missing}"


def test_required_files_exist():
    missing = [f for f in REQUIRED_FILES if not (PROJECT_ROOT / f).is_file()]
    assert not missing, f"missing files: {missing}"


def test_env_file_is_not_committed():
    """A real .env must never be tracked by git."""
    result = subprocess.run(
        ["git", "ls-files", ".env"],
        cwd=PROJECT_ROOT, capture_output=True, text=True, check=False,
    )
    assert result.stdout.strip() == "", ".env must not be tracked by git"


def test_gitignore_excludes_secrets():
    text = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")
    for pattern in (".env", "*.key", "*.pem"):
        assert pattern in text, f".gitignore should exclude {pattern}"


def test_env_example_is_committed_and_has_no_real_secrets():
    example = PROJECT_ROOT / ".env.example"
    assert example.is_file()
    text = example.read_text(encoding="utf-8")
    # placeholders only - the template must not carry a populated API key
    assert "GEMINI_API_KEY=" in text
    assert "GEMINI_API_KEY=AI" not in text  # real google keys start with "AI"


def test_run_pipeline_help_executes():
    result = subprocess.run(
        [sys.executable, "run_pipeline.py", "--help"],
        cwd=PROJECT_ROOT, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0
    assert "InsightForge AI" in result.stdout


def test_run_pipeline_reports_orchestrator_not_ready():
    """Until Phase 19 the manual entry point should exit cleanly with code 3."""
    result = subprocess.run(
        [sys.executable, "run_pipeline.py", "--scan"],
        cwd=PROJECT_ROOT, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 3
    assert "not implemented yet" in result.stdout
