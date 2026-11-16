"""
Golden tests: running the documented commands on examples/simple/thelook must
reproduce examples/simple/expected/* (compared as parsed YAML).
"""

import pytest

from conftest import SKILL_ROOT, all_files, load_yaml, run_cli

EXAMPLES = SKILL_ROOT / "examples"
EXPECTED = EXAMPLES / "simple" / "expected"
CSV = EXAMPLES / "migration.csv"


@pytest.mark.parametrize("view,owner,looker_repo", [
    ("order_items", "Data Team", EXAMPLES / "simple"),
    ("users", "Team A", EXAMPLES / "simple" / "thelook"),
])
def test_view_matches_expected(tmp_path, view, owner, looker_repo):
    result = run_cli("--view", view, "--phase", "0", "--owner", owner, "--looker-repo", looker_repo,
                     "--csv", CSV, "--omni-repo", tmp_path / "views", cwd=tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    actual = load_yaml(tmp_path / "views" / f"{view}.view.yaml")
    assert actual == load_yaml(EXPECTED / f"{view}.view.yaml")


def test_explore_matches_expected(tmp_path):
    result = run_cli("--explore", "order_items", "--looker-repo", EXAMPLES / "simple",
                     "--omni-repo", tmp_path / "topics", cwd=tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert load_yaml(tmp_path / "topics" / "order_items.topic.yaml") == load_yaml(EXPECTED / "order_items.topic.yaml")
    assert not (tmp_path / "topics" / "order_items.relationships").exists()


def test_explore_with_relationships_file_matches_expected(tmp_path):
    """--relationships-file: the same joins in a separate file, none in the topic"""
    result = run_cli("--explore", "order_items", "--looker-repo", EXAMPLES / "simple", "--relationships-file",
                     "--omni-repo", tmp_path / "topics", cwd=tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    expected_topic = load_yaml(EXPECTED / "order_items.topic.yaml")
    relationships = expected_topic.pop("relationships")
    assert load_yaml(tmp_path / "topics" / "order_items.topic.yaml") == expected_topic
    assert load_yaml(tmp_path / "topics" / "order_items.relationships") == relationships
    assert relationships == load_yaml(EXPECTED / "order_items.relationships")


def test_documented_quick_start_dry_run():
    """The README quick start, run from the skill dir, writes nothing"""
    before = set(all_files(SKILL_ROOT))
    result = run_cli("--view", "users", "--phase", "0", "--owner", "Team A",
                     "--looker-repo", "examples/simple/thelook", "--csv", "examples/migration.csv",
                     "--dry-run", cwd=SKILL_ROOT)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "would write migration/staging/views/users.view.yaml" in result.stdout
    assert set(all_files(SKILL_ROOT)) == before
