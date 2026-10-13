"""Unit tests for the LookML parsers (views, explores, access grants, CSV scope)"""

from pathlib import Path

import pytest

from lib.access_grant_parser import find_access_grants_files, load_access_grants
from lib.context_loader import load_and_filter_csv
from lib.explore_analyzer import (
    find_explore_files,
    find_explores_using_views,
    parse_explore_file,
    suggest_topic_divisions,
)
from lib.looker_parser import find_and_parse_view, find_looker_view_file
from lib.orchestrator import MigrationOrchestrator


class TestViewParser:
    def test_finds_views_outside_views_dir(self, tmp_path):
        (tmp_path / "anything.lkml").write_text(
            "view: first { dimension: a {} }\nview: second { dimension: b {} }\n"
        )
        view = find_and_parse_view(str(tmp_path), "second")
        assert set(view.dimensions) == {"b"}

    def test_missing_view_raises(self, fixture_repo):
        with pytest.raises(FileNotFoundError):
            find_and_parse_view(str(fixture_repo), "does_not_exist")

    def test_extends_is_normalized(self, tmp_path):
        (tmp_path / "v.view.lkml").write_text("view: child { extends: [base_a, base_b] dimension: x {} }")
        view = find_and_parse_view(str(tmp_path), "child")
        assert view.extends == ["base_a", "base_b"]
        assert view.is_templated and not view.is_extension

    def test_fixture_view_features(self, fixture_repo):
        view = find_and_parse_view(str(fixture_repo), "order_items")
        assert view.sql_table_name == "`thelook_ecommerce.order_items`"
        assert "created" in view.dimension_groups
        assert view.drill_fields == ["id", "order_id"]
        assert view.sets == {"detail": ["id", "order_id", "status", "sale_price"]}
        assert view.field_access_grants == {"dimension:gross_margin": ["can_view_financials"]}

    def test_file_name_match_must_define_view(self, tmp_path):
        (tmp_path / "users.view.lkml").write_text("view: not_users { dimension: a {} }")
        (tmp_path / "other.lkml").write_text("view: users { dimension: b {} }")
        assert find_looker_view_file(str(tmp_path), "users").endswith("other.lkml")


class TestExploreParser:
    def test_explores_found_in_model_file(self, fixture_repo):
        files = find_explore_files(str(fixture_repo))
        assert any(f.endswith("thelook.model.lkml") for f in files)

    def test_restrictions_and_aliases_parsed(self, fixture_repo):
        explores = {e.name: e for e in parse_explore_file(str(fixture_repo / "thelook.model.lkml"))}
        order_items = explores["order_items"]
        assert order_items.access_filters == [{"field": "users.country", "user_attribute": "allowed_country"}]
        assert order_items.sql_always_where == "${order_items.status} != 'Cancelled'"
        assert order_items.always_filter is not None
        assert order_items.access_grants == ["can_view_financials"]

        visitor = explores["events"].joins[0]
        assert visitor.view == "visitor"
        assert visitor.source_view == "users"
        assert visitor.is_aliased

    def test_alias_source_counts_as_using_view(self, fixture_repo):
        names = {e.name for e in find_explores_using_views(str(fixture_repo), {"users"})}
        assert names == {"order_items", "users", "events"}

    def test_division_tiers(self, fixture_repo):
        explores = parse_explore_file(str(fixture_repo / "thelook.model.lkml"))
        order_items = next(e for e in explores if e.name == "order_items")
        all_views = {"orders", "users", "inventory_items", "products", "distribution_centers"}
        suggestions = suggest_topic_divisions([order_items], all_views)
        # 5 joins: split into core (many_to_one/one_to_one) topic
        assert [s.topic_name for s in suggestions] == ["order_items_core_migration"]
        assert set(suggestions[0].suggested_joins) == all_views


class TestAccessGrants:
    def test_grants_found_in_model_file(self, fixture_repo):
        files = find_access_grants_files(str(fixture_repo))
        assert [Path(f).name for f in files] == ["thelook.model.lkml"]
        grants = load_access_grants(str(fixture_repo))
        assert grants["can_view_financials"].allowed_values == ["finance", "executive"]
        assert grants["can_view_pii"].user_attribute == "can_see_pii"


class TestCsvScope:
    def test_phase_formats_and_exact_owner(self, scope_csv):
        rows = load_and_filter_csv(str(scope_csv), owner="Data Team", phase=0, view="user_order_facts")
        assert len(rows) == 5  # numeric "0" phase rows
        assert load_and_filter_csv(str(scope_csv), owner="data team", phase=0) == []
        p1 = load_and_filter_csv(str(scope_csv), owner="Team A", phase=1)
        assert [r["field"] for r in p1] == ["average_age"]


class TestTableNameParsing:
    @pytest.fixture
    def orchestrator(self, tmp_path):
        return MigrationOrchestrator(skill_path=tmp_path, looker_repo=tmp_path, omni_repo=tmp_path / "out", dry_run=True)

    @pytest.mark.parametrize("sql_table_name,expected", [
        ("thelook_ecommerce.users", ("thelook_ecommerce", {"table_name": "users"})),
        ("`thelook_ecommerce.users`", ("thelook_ecommerce", {"table_name": "users"})),
        ("`my-project.thelook_ecommerce.users`",
         ("thelook_ecommerce", {"catalog": "my-project", "table_name": "users"})),
        ("my_db.thelook_ecommerce.users ;;",
         ("thelook_ecommerce", {"catalog": "my_db", "table_name": "users"})),
        ('"thelook_ecommerce"."users"', ("thelook_ecommerce", {"table_name": "users"})),
        ('"my.db"."thelook_ecommerce"."users"', None),
        ("users", ("analytics", {"table_name": "users"})),
    ])
    def test_parse_table_name(self, orchestrator, sql_table_name, expected):
        if expected is None:
            with pytest.raises(ValueError, match="MANUAL ACTION REQUIRED"):
                orchestrator._parse_table_name(sql_table_name)
        else:
            assert orchestrator._parse_table_name(sql_table_name) == expected

    @pytest.mark.parametrize("sql_table_name", [
        "${users.SQL_TABLE_NAME}",
        "thelook_ecommerce.{{ _user_attributes['table'] }}",
        "{% if x %}a.b{% else %}a.c{% endif %}",
        "a.b.c.d",
    ])
    def test_dynamic_or_unparseable_table_names_rejected(self, orchestrator, sql_table_name):
        with pytest.raises(ValueError, match="MANUAL ACTION REQUIRED"):
            orchestrator._parse_table_name(sql_table_name)
