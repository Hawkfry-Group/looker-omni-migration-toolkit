"""
Shared pytest setup.

Puts the skill root on sys.path so `import lib...` works when running plain
`pytest` from the skill directory, and provides paths to the thelook fixture
project plus a helper to run the CLI.
"""

import subprocess
import sys
from pathlib import Path

import pytest
import yaml

SKILL_ROOT = Path(__file__).resolve().parent.parent
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

FIXTURE_REPO = SKILL_ROOT / "tests" / "fixtures" / "thelook"
SCOPE_CSV = FIXTURE_REPO / "scope.csv"
MIGRATE = SKILL_ROOT / "scripts" / "migrate.py"


def run_cli(*args, cwd):
    """Run scripts/migrate.py with the current interpreter; return CompletedProcess"""
    return subprocess.run(
        [sys.executable, str(MIGRATE), *map(str, args)],
        cwd=str(cwd),
        capture_output=True,
        text=True,
    )


def load_yaml(path):
    return yaml.safe_load(Path(path).read_text())


def all_files(root):
    return sorted(p for p in Path(root).rglob("*") if p.is_file())


@pytest.fixture
def fixture_repo():
    return FIXTURE_REPO


@pytest.fixture
def scope_csv():
    return SCOPE_CSV
