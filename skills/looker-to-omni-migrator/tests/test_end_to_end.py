"""
End-to-end tests: run the real CLI (scripts/migrate.py) against the thelook
fixture project in tests/fixtures/thelook and check the generated files.

Every test runs in its own tmp_path, so the default output locations
(./migration/staging/{views,topics,reports}) are created under tmp_path.
"""

from pathlib import Path

import pytest

from conftest import FIXTURE_REPO, SCOPE_CSV, all_files, load_yaml, run_cli

VIEWS = Path("migration/staging/views")
TOPICS = Path("migration/staging/topics")
REPORTS = Path("migration/staging/reports")


def migrate_view(tmp_path, view, owner, *extra, phase="0"):
    result = run_cli(
        "--view", view, "--phase", phase, "--owner", owner,
        "--looker-repo", FIXTURE_REPO, "--csv", SCOPE_CSV, *extra,
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result


def migrate_explore(tmp_path, explore, *extra):
    result = run_cli("--explore", explore, "--looker-repo", FIXTURE_REPO, *extra, cwd=tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    return result


# ----------------------------------------------------------------------
# View migration
# ----------------------------------------------------------------------

@pytest.fixture
def order_items(tmp_path):
    result = migrate_view(tmp_path, "order_items", "Data Team")
    view = load_yaml(tmp_path / VIEWS / "order_items.view.yaml")
    report = (tmp_path / REPORTS / "order_items.migration.md").read_text()
    return result, view, report


class TestOrderItemsView:
    def test_files_and_locations(self, tmp_path, order_items):
        assert {p.relative_to(tmp_path) for p in all_files(tmp_path)} == {
            VIEWS / "order_items.view.yaml",
            REPORTS / "order_items.migration.md",
        }

    def test_view_header(self, order_items):
        _, view, _ = order_items
        assert view["schema"] == "thelook_ecommerce"  # from the backtick-quoted sql_table_name
        assert view["table_name"] == "order_items"
        assert view["folder"] == "thelook_ecommerce/curated"
        assert "mig_phase_0" in view["tags"] and "verified" in view["tags"]
        assert view["ai_context"].startswith("Line items of the thelook sample store")

    def test_in_scope_fields(self, order_items):
        _, view, _ = order_items
        dims, measures = view["dimensions"], view["measures"]
        assert dims["id"]["primary_key"] is True
        assert "sql" not in dims["id"]
        for name in ("id", "order_id", "status", "created"):
            assert dims[name]["tags"][-2:] == ["mig_phase_0", "verified"], name
            assert "type" not in dims[name]
        assert measures["count"]["aggregate_type"] == "count"
        assert "sql" not in measures["count"]
        assert measures["order_count"] == {**measures["order_count"], "aggregate_type": "count_distinct",
                                           "sql": "${order_id}", "format": "number_0"}
        assert measures["total_sale_price"]["aggregate_type"] == "sum"
        assert measures["total_sale_price"]["label"] == "Total Sales"
        assert measures["total_sale_price"]["format"] == "usdcurrency_2"
        assert measures["median_sale_price"]["aggregate_type"] == "median"
        assert "aggregate_type" not in measures["gross_margin_per_order"]
        assert measures["gross_margin_per_order"]["sql"] == "${total_gross_margin} / NULLIF(${order_count}, 0)"

    def test_dimension_group_has_no_timeframes(self, order_items):
        _, view, report = order_items
        created = view["dimensions"]["created"]
        assert created["sql"] == "created_at"
        assert "timeframes" not in created and "datatype" not in created
        assert "date" in created["tags"]
        assert "raw, date, week, month, year" in report

    def test_self_column_reference_is_raw(self, order_items):
        _, view, _ = order_items
        assert view["dimensions"]["sale_price"]["sql"] == "COALESCE(sale_price, 0)"

    def test_cross_view_reference_kept(self, order_items):
        _, view, report = order_items
        assert view["dimensions"]["gross_margin"]["sql"] == "${sale_price} - ${inventory_items.cost}"
        assert "inventory_items.cost" in report
        assert "__" not in view["dimensions"]["gross_margin"]["sql"]

    def test_transitive_dependencies_resolved_with_grants(self, order_items):
        _, view, _ = order_items
        # gross_margin_per_order -> total_gross_margin -> gross_margin -> sale_price
        assert "auto_dependency" in view["measures"]["total_gross_margin"]["tags"]
        for name in ("gross_margin", "sale_price"):
            assert "auto_dependency" in view["dimensions"][name]["tags"], name
            assert not view["dimensions"][name].get("hidden"), name
        assert view["dimensions"]["gross_margin"]["required_access_grants"] == ["can_view_financials"]

    def test_out_of_scope_fields_hidden_with_sql(self, order_items):
        _, view, _ = order_items
        is_returned = view["dimensions"]["is_returned"]
        assert is_returned["hidden"] is True
        assert is_returned["tags"] == ["in_omni_in_looker_not_csv"]
        assert is_returned["sql"] == "returned_at IS NOT NULL"
        assert view["measures"]["average_sale_price"]["hidden"] is True

    def test_measure_filters(self, order_items):
        _, view, _ = order_items
        m = view["measures"]
        assert m["returned_count"]["filters"] == {"status": {"is": "Returned"}}
        assert m["completed_or_shipped_count"]["filters"] == {"status": {"is": ["Complete", "Shipped"]}}
        assert m["large_order_count"]["filters"] == {"sale_price": {"greater_than_or_equal_to": 100}}

    def test_unsupported_items_not_emitted_and_reported(self, order_items):
        result, view, report = order_items
        for name in ("price_tier", "fulfillment"):
            assert name not in view["dimensions"]
        for name in ("running_total_sales", "wildcard_status_count"):
            assert name not in view["measures"]
        assert "running_total" in report and "%Ship%" in report
        assert "MANUAL ACTION REQUIRED" in report
        assert "running_total_sales" in result.stdout

    def test_unmapped_format_flagged(self, order_items):
        _, view, report = order_items
        assert "format" not in view["measures"]["total_sales_eur"]
        assert "€#,##0.00" in report

    def test_csv_field_missing_from_lookml_reported(self, order_items):
        result, view, report = order_items
        assert "discount_amount" not in view["dimensions"]
        assert "CSV Fields Not Found in LookML" in report
        assert "discount_amount" in report
        assert "discount_amount" in result.stdout
        assert "NEEDING MANUAL ACTION" in result.stdout

    def test_drill_fields_and_html_not_migrated(self, order_items):
        _, view, report = order_items
        assert "drill_fields" not in str(view)
        assert "html" not in view["dimensions"]["status"]
        assert "Not migrated: html" in report
        assert "drill_fields" in report


class TestOtherViews:
    def test_sql_derived_table(self, tmp_path):
        migrate_view(tmp_path, "user_order_facts", "Data Team")
        assert not (tmp_path / VIEWS / "user_order_facts.view.yaml").exists()
        view = load_yaml(tmp_path / VIEWS / "user_order_facts.query.view")
        assert "table_name" not in view
        assert "FROM thelook_ecommerce.order_items" in view["sql"]
        assert view["schema"] == "thelook_ecommerce"
        assert view["dimensions"]["user_id"]["primary_key"] is True
        assert view["dimensions"]["is_repeat_buyer"]["sql"] == "${lifetime_orders} > 1"
        assert "auto_dependency" in view["dimensions"]["lifetime_orders"]["tags"]
        assert view["dimensions"]["first_order"]["sql"] == "first_order_at"
        report = (tmp_path / REPORTS / "user_order_facts.migration.md").read_text()
        assert "datagroup_trigger" in report
        assert ".query.view" in report

    def test_native_derived_table(self, tmp_path):
        migrate_view(tmp_path, "brand_sales", "Data Team")
        view = load_yaml(tmp_path / VIEWS / "brand_sales.query.view")
        assert view["query"] == {
            "fields": {"products.brand": "brand", "order_items.total_sale_price": "total_sales"},
            "base_view": "order_items",
            "topic": "order_items",
            "filters": {"order_items.status": {"is": "Complete"}},
        }
        assert "sql" not in view and "table_name" not in view

    def test_users_view(self, tmp_path):
        result = migrate_view(tmp_path, "users", "Team A", phase="P0")
        view = load_yaml(tmp_path / VIEWS / "users.view.yaml")
        dims = view["dimensions"]
        assert dims["full_name"]["sql"] == "CONCAT(${first_name}, ' ', ${last_name})"
        for dep in ("first_name", "last_name"):
            assert "auto_dependency" in dims[dep]["tags"]
        assert dims["email"]["required_access_grants"] == ["can_view_pii"]
        assert dims["age"]["hidden"] is True  # only in phase 1
        report = (tmp_path / REPORTS / "users.migration.md").read_text()
        assert "date_granularity" in report and "Parameters" in report
        assert "can_see_pii" in report  # grant details from the model file
        assert "date_granularity" in result.stdout

    def test_unverified(self, tmp_path):
        migrate_view(tmp_path, "users", "Team A", "--unverified")
        view = load_yaml(tmp_path / VIEWS / "users.view.yaml")
        assert "unverified" in view["tags"]
        assert view["dimensions"]["id"]["tags"][-1] == "unverified"

    def test_canonical_mapping_only_with_config(self, tmp_path):
        migrate_view(tmp_path, "products", "Team A")
        without = load_yaml(tmp_path / VIEWS / "products.view.yaml")
        assert without["dimensions"]["sku"]["label"] == "SKU Code"

        config = tmp_path / "config.yaml"
        config.write_text("canonical_mappings:\n  sku: product\n")
        out = tmp_path / "with_config" / "views"
        migrate_view(tmp_path, "products", "Team A", "--config", config, "--omni-repo", out)
        with_config = load_yaml(out / "products.view.yaml")
        assert with_config["dimensions"]["sku"]["label"] == "Product Code"
        assert "sku" in with_config["dimensions"]  # field names unchanged

    def test_config_schema_and_cli_overrides(self, tmp_path):
        config = tmp_path / "config.yaml"
        config.write_text("default_schema: fallback\ncurated_folder: modeled\n")
        migrate_view(tmp_path, "products", "Team A", "--config", config, "--curated-folder", "gold")
        view = load_yaml(tmp_path / VIEWS / "products.view.yaml")
        assert view["folder"] == "thelook_ecommerce/gold"

    def test_custom_omni_repo_reports_sibling(self, tmp_path):
        migrate_view(tmp_path, "users", "Team A", "--omni-repo", "out/views")
        assert (tmp_path / "out/views/users.view.yaml").exists()
        assert (tmp_path / "out/reports/users.migration.md").exists()

    def test_merge_with_omni_source_repo(self, tmp_path):
        source = tmp_path / "omni_source"
        source.mkdir()
        (source / "users.view.yaml").write_text(
            "schema: thelook_ecommerce\n"
            "table_name: users\n"
            "dbt:\n  unique_id: model.shop.users\n"
            "dimensions:\n  id: {}\n  postal_code: {}\n  country:\n    format: string\n"
        )
        migrate_view(tmp_path, "users", "Team A", "--omni-source-repo", source)
        view = load_yaml(tmp_path / VIEWS / "users.view.yaml")
        assert view["dbt"] == {"unique_id": "model.shop.users"}
        assert view["dimensions"]["postal_code"]["hidden"] is True
        assert "only_omni" in view["dimensions"]["postal_code"]["tags"]
        assert "format" not in view["dimensions"]["country"]  # conversion-owned: replaced
        assert "mig_phase_0" in view["dimensions"]["country"]["tags"]
        assert "count" in view["measures"]
        assert "Merge/Enrich Summary" in (tmp_path / REPORTS / "users.migration.md").read_text()

    def test_explore_analysis(self, tmp_path):
        migrate_view(tmp_path, "users", "Team A", "--explore-analysis")
        analysis = (tmp_path / REPORTS / "users_explore_analysis.md").read_text()
        assert "order_items" in analysis and "events" in analysis
        assert "Row-Level Restrictions" in analysis


# ----------------------------------------------------------------------
# Explore migration
# ----------------------------------------------------------------------

class TestExploreMigration:
    def test_order_items_topic(self, tmp_path):
        result = migrate_explore(tmp_path, "order_items")
        topic = load_yaml(tmp_path / TOPICS / "order_items.topic.yaml")
        assert topic["base_view"] == "order_items"
        assert topic["label"] == "Orders"
        assert topic["fields"] == ["all_views.*", "-tag:table_key"]
        assert topic["joins"] == {
            "orders": {},
            "users": {},
            "inventory_items": {"products": {"distribution_centers": {}}},
        }
        # Row-level restrictions carried
        assert topic["access_filters"] == [{"field": "users.country", "user_attribute": "allowed_country"}]
        assert topic["always_where_sql"] == "${order_items.status} != 'Cancelled'"
        assert topic["required_access_grants"] == ["can_view_financials"]

        rels = load_yaml(tmp_path / TOPICS / "order_items.relationships")
        assert isinstance(rels, list) and len(rels) == 5
        by_target = {r["join_to_view"]: r for r in rels}
        assert by_target["inventory_items"]["join_type"] == "inner"
        assert by_target["inventory_items"]["relationship_type"] == "one_to_one"
        assert by_target["products"]["join_from_view"] == "inventory_items"
        assert by_target["distribution_centers"]["join_from_view"] == "products"
        assert by_target["users"]["on_sql"] == "${order_items.user_id} = ${users.id}"

        report = (tmp_path / REPORTS / "order_items_explore.md").read_text()
        assert "MANUAL ACTION REQUIRED" in report
        assert "always_filter (NOT carried)" in report
        assert "allowed_country" in report
        assert "department" in report  # grant user attribute from the registry
        assert "always_filter" in result.stdout

    def test_liquid_join_skipped_and_reported(self, tmp_path):
        migrate_explore(tmp_path, "users")
        topic = load_yaml(tmp_path / TOPICS / "users.topic.yaml")
        assert topic["joins"] == {"user_order_facts": {}}
        rels = load_yaml(tmp_path / TOPICS / "users.relationships")
        assert [r["join_to_view"] for r in rels] == ["user_order_facts"]
        report = (tmp_path / REPORTS / "users_explore.md").read_text()
        assert "Join orders (NOT migrated)" in report and "Liquid" in report

    def test_aliased_join(self, tmp_path):
        migrate_explore(tmp_path, "events")
        rels = load_yaml(tmp_path / TOPICS / "events.relationships")
        assert rels == [{
            "join_from_view": "events",
            "join_to_view": "users",
            "join_to_view_as": "visitor",
            "join_type": "always_left",
            "on_sql": "${events.user_id} = ${visitor.id}",
            "relationship_type": "many_to_one",
            "reversible": False,
        }]
        assert load_yaml(tmp_path / TOPICS / "events.topic.yaml")["joins"] == {"visitor": {}}

    def test_unknown_explore_fails(self, tmp_path):
        result = run_cli("--explore", "nope", "--looker-repo", FIXTURE_REPO, cwd=tmp_path)
        assert result.returncode == 1
        assert "not found" in result.stdout


# ----------------------------------------------------------------------
# Dry run, access grants, CLI contract
# ----------------------------------------------------------------------

class TestDryRun:
    @pytest.mark.parametrize("args", [
        ["--view", "order_items", "--phase", "0", "--owner", "Data Team", "--csv", SCOPE_CSV, "--explore-analysis"],
        ["--view", "user_order_facts", "--phase", "0", "--owner", "Data Team", "--csv", SCOPE_CSV],
        ["--explore", "order_items"],
        ["--parse-access-grants"],
    ])
    def test_dry_run_writes_nothing(self, tmp_path, args):
        result = run_cli(*args, "--looker-repo", FIXTURE_REPO, "--dry-run", cwd=tmp_path)
        assert result.returncode == 0, result.stdout + result.stderr
        assert all_files(tmp_path) == []
        assert list(tmp_path.iterdir()) == []
        assert "would write" in result.stdout

    def test_dry_run_lists_planned_files(self, tmp_path):
        result = run_cli("--view", "order_items", "--phase", "0", "--owner", "Data Team", "--csv", SCOPE_CSV,
                         "--looker-repo", FIXTURE_REPO, "--dry-run", cwd=tmp_path)
        assert "would write migration/staging/views/order_items.view.yaml" in result.stdout
        assert "would write migration/staging/reports/order_items.migration.md" in result.stdout


class TestAccessGrantsMode:
    def test_parse_access_grants(self, tmp_path):
        result = run_cli("--parse-access-grants", "--looker-repo", FIXTURE_REPO, cwd=tmp_path)
        assert result.returncode == 0
        report = (tmp_path / REPORTS / "access_grants.md").read_text()
        assert "`can_view_financials`" in report and "`finance`, `executive`" in report
        assert "can_view_pii" in result.stdout


class TestCliContract:
    def test_help(self, tmp_path):
        result = run_cli("--help", cwd=tmp_path)
        assert result.returncode == 0
        for flag in ("--view", "--explore", "--parse-access-grants", "--phase", "--owner", "--looker-repo",
                     "--omni-repo", "--omni-source-repo", "--config", "--default-schema", "--curated-folder",
                     "--csv", "--unverified", "--dry-run", "--explore-analysis"):
            assert flag in result.stdout, flag
        for removed in ("--batch", "--skip-explore-analysis", "--verified "):
            assert removed not in result.stdout

    @pytest.mark.parametrize("args", [
        [],
        ["--view", "users", "--explore", "order_items"],
        ["--view", "users", "--owner", "Team A"],
        ["--view", "users", "--phase", "0"],
        ["--batch"],
        ["--view", "users", "--phase", "0", "--owner", "Team A", "--verified"],
        ["--view", "users", "--phase", "0", "--owner", "Team A", "--skip-explore-analysis"],
        ["--view", "users", "--phase", "zero", "--owner", "Team A"],
        ["--explore", "order_items", "--explore-analysis"],
    ])
    def test_invalid_arguments(self, tmp_path, args):
        result = run_cli(*args, "--looker-repo", FIXTURE_REPO, cwd=tmp_path)
        assert result.returncode == 2, result.stdout + result.stderr

    def test_missing_looker_repo(self, tmp_path):
        result = run_cli("--explore", "order_items", "--looker-repo", tmp_path / "nope", cwd=tmp_path)
        assert result.returncode == 1

    def test_missing_view(self, tmp_path):
        result = run_cli("--view", "nope", "--phase", "0", "--owner", "Team A", "--csv", SCOPE_CSV,
                         "--looker-repo", FIXTURE_REPO, cwd=tmp_path)
        assert result.returncode == 1

    def test_missing_csv(self, tmp_path):
        result = run_cli("--view", "users", "--phase", "0", "--owner", "Team A", "--csv", tmp_path / "x.csv",
                         "--looker-repo", FIXTURE_REPO, cwd=tmp_path)
        assert result.returncode == 1
        assert "CSV file not found" in result.stdout

    def test_missing_omni_source_repo(self, tmp_path):
        result = run_cli("--view", "users", "--phase", "0", "--owner", "Team A", "--csv", SCOPE_CSV,
                         "--looker-repo", FIXTURE_REPO, "--omni-source-repo", tmp_path / "nope", cwd=tmp_path)
        assert result.returncode == 1

    def test_no_rows_for_owner(self, tmp_path):
        result = run_cli("--view", "users", "--phase", "0", "--owner", "Nobody", "--csv", SCOPE_CSV,
                         "--looker-repo", FIXTURE_REPO, cwd=tmp_path)
        assert result.returncode == 1
        assert "No CSV rows found" in result.stdout


class TestOrchestratorApi:
    def test_issues_and_planned_files(self, tmp_path):
        from conftest import SKILL_ROOT
        from lib.orchestrator import MigrationOrchestrator

        orchestrator = MigrationOrchestrator(
            skill_path=SKILL_ROOT, looker_repo=FIXTURE_REPO, omni_repo=tmp_path / "views",
            csv_path=SCOPE_CSV, dry_run=True
        )
        result = orchestrator.migrate_view("order_items", phase=0, owner="Data Team")
        assert result.success
        assert any("discount_amount" in i for i in result.issues)
        assert any("running_total" in i for i in result.issues)
        assert result.planned_files == [tmp_path / "views" / "order_items.view.yaml",
                                        tmp_path / "reports" / "order_items.migration.md"]
        assert not (tmp_path / "views").exists()
