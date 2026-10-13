"""
Regression tests for semantic safety: every construct is either converted
correctly or skipped and reported as MANUAL ACTION REQUIRED (never silently
changed). Each test builds a small LookML project in tmp_path.
"""

import textwrap
from pathlib import Path

import pytest
import yaml

from conftest import SKILL_ROOT, all_files, load_yaml, run_cli
from lib.access_grant_parser import load_access_grants
from lib.explore_analyzer import find_explore_files, generate_explore_report, parse_explore_file
from lib.file_generators import FileGenerator, MergeError
from lib.orchestrator import MigrationOrchestrator

CSV_HEADER = "phase,view,field,type,omni_view,omni_field,owner,status,notes\n"


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text))
    return path


def csv(tmp_path, rows):
    """rows: (phase, view, field, type)"""
    lines = [f"{p},{v},{f},{t},,,Team A,ready," for p, v, f, t in rows]
    return write(tmp_path / "scope.csv", CSV_HEADER + "\n".join(lines) + "\n")


def orchestrator(tmp_path, repo, csv_path=None, **kw):
    return MigrationOrchestrator(skill_path=SKILL_ROOT, looker_repo=repo, omni_repo=tmp_path / "out" / "views",
                                 csv_path=csv_path, **kw)


ORDERS_VIEW = """
view: orders {
  sql_table_name: shop.orders ;;
  dimension: id { primary_key: yes type: number sql: ${TABLE}.id ;; }
  dimension: status { type: string sql: ${TABLE}.status ;; }
  dimension: amount { type: number sql: ${TABLE}.amount ;; }
  measure: count { type: count }
}
"""


# ----------------------------------------------------------------------
# 1. Explore restrictions
# ----------------------------------------------------------------------

EXPLORE_MODEL = """
access_grant: can_view_finance { user_attribute: department allowed_values: ["finance"] }

view: orders { sql_table_name: shop.orders ;; dimension: id { primary_key: yes } }
view: users { sql_table_name: shop.users ;; dimension: id { primary_key: yes } }
view: products { sql_table_name: shop.products ;; dimension: id { primary_key: yes } }
view: brands { sql_table_name: shop.brands ;; dimension: id { primary_key: yes } }

explore: orders {
  label: "Orders"
  group_label: "Shop"
  hidden: yes
  sql_always_having: ${orders.count} > 0 ;;
  always_join: [users]

  # declared before its parent on purpose
  join: brands {
    relationship: many_to_one
    sql_on: ${products.brand_id} = ${brands.id} ;;
  }
  join: products {
    relationship: many_to_one
    sql_on: ${orders.product_id} = ${products.id} ;;
    required_access_grants: [can_view_finance]
    required_joins: [users]
  }
  join: users {
    type: inner
    relationship: many_to_one
    sql_on: ${orders.user_id} = ${users.id} ;;
  }
  join: buyer {
    from: users
    relationship: many_to_one
    sql_on: ${orders.buyer_id} = ${buyer.id} ;;
  }
  join: buyer_region {
    relationship: many_to_one
    sql_on: ${buyer.region_id} = ${buyer_region.id} ;;
  }
  join: right_joined {
    type: right_outer
    sql_on: ${orders.x} = ${right_joined.x} ;;
  }
  join: odd_relationship {
    relationship: many_to_few
    sql_on: ${orders.x} = ${odd_relationship.x} ;;
  }
  join: dangling {
    sql_on: ${missing_view.x} = ${dangling.x} ;;
  }
  join: two_parents {
    sql_on: ${products.a} = ${two_parents.a} AND ${users.b} = ${two_parents.b} ;;
  }
  join: child_of_skipped {
    sql_on: ${dangling.y} = ${child_of_skipped.y} ;;
  }
  join: loop_a { sql_on: ${loop_b.k} = ${loop_a.k} ;; }
  join: loop_b { sql_on: ${loop_a.k} = ${loop_b.k} ;; }
}

explore: customers {
  from: users
  join: orders {
    relationship: one_to_many
    sql_on: ${customers.id} = ${orders.user_id} ;;
  }
}
"""


@pytest.fixture
def explore_repo(tmp_path):
    write(tmp_path / "repo" / "shop.model.lkml", EXPLORE_MODEL)
    return tmp_path / "repo"


class TestExploreSemantics:
    def migrate(self, tmp_path, repo, name="orders"):
        orch = MigrationOrchestrator(skill_path=SKILL_ROOT, looker_repo=repo, omni_repo=tmp_path / "out" / "topics")
        result = orch.migrate_explore(name)
        assert result.success, result.error_message
        topic = load_yaml(tmp_path / "out" / "topics" / f"{name}.topic.yaml")
        rel_path = tmp_path / "out" / "topics" / f"{name}.relationships"
        rels = load_yaml(rel_path) if rel_path.exists() else []
        report = (tmp_path / "out" / "reports" / f"{name}_explore.md").read_text()
        return result, topic, rels, report

    def test_restrictions_carried_or_reported(self, tmp_path, explore_repo):
        result, topic, _, report = self.migrate(tmp_path, explore_repo)
        assert topic["always_having_sql"] == "${orders.count} > 0"
        assert topic["hidden"] is True
        assert topic["group_label"] == "Shop"
        for text in ("always_join (NOT carried)", "Join access grants on products (NOT carried)",
                     "required_joins on products (NOT carried)", "sql_always_having (carried)"):
            assert text in report, text
        joined = "\n".join(result.issues)
        assert "Join access grants on products" in joined
        assert "always_join" in joined
        assert "department" in report  # grant user attribute

    def test_join_graph_is_order_independent(self, tmp_path, explore_repo):
        _, topic, rels, _ = self.migrate(tmp_path, explore_repo)
        assert topic["joins"]["products"] == {"brands": {}}
        by_to = {(r["join_to_view"], r.get("join_to_view_as")): r for r in rels}
        assert by_to[("brands", None)]["join_from_view"] == "products"

    def test_aliased_parent_keeps_source_view(self, tmp_path, explore_repo):
        _, topic, rels, _ = self.migrate(tmp_path, explore_repo)
        buyer = next(r for r in rels if r.get("join_to_view_as") == "buyer")
        region = next(r for r in rels if r["join_to_view"] == "buyer_region")
        assert buyer["join_to_view"] == "users"
        assert region["join_from_view"] == "users"
        assert region["join_from_view_as"] == "buyer"
        assert topic["joins"]["buyer"] == {"buyer_region": {}}

    def test_canonical_join_types(self, tmp_path, explore_repo):
        _, _, rels, _ = self.migrate(tmp_path, explore_repo)
        users = next(r for r in rels if r["join_to_view"] == "users" and "join_to_view_as" not in r)
        assert users["join_type"] == "inner"
        assert {r["join_type"] for r in rels} <= {"always_left", "inner", "full_outer", "cross"}

    @pytest.mark.parametrize("join,reason", [
        ("right_joined", "right_outer"),
        ("odd_relationship", "many_to_few"),
        ("dangling", "missing_view"),
        ("two_parents", "several joined views"),
        ("child_of_skipped", "depends on join dangling"),
        ("loop_a", "circular"),
        ("loop_b", "circular"),
    ])
    def test_unconvertible_joins_skipped_and_reported(self, tmp_path, explore_repo, join, reason):
        result, topic, rels, report = self.migrate(tmp_path, explore_repo)
        names = {r.get("join_to_view_as") or r["join_to_view"] for r in rels}
        assert join not in names
        assert join not in str(topic["joins"])
        section = report.split(f"### Join {join} (NOT migrated)")[1].split("###")[0]
        assert reason in section
        assert any(i.startswith(f"Join {join} (NOT migrated)") for i in result.issues)

    def test_aliased_base_view(self, tmp_path, explore_repo):
        _, topic, rels, report = self.migrate(tmp_path, explore_repo, "customers")
        assert topic["base_view"] == "users"
        assert "Aliased base view" in report

    def test_stale_relationships_file_removed(self, tmp_path):
        repo = tmp_path / "repo"
        model = write(repo / "m.model.lkml", """
            view: a { dimension: id {} }
            view: b { dimension: id {} }
            explore: a { join: b { sql_on: ${a.id} = ${b.id} ;; } }
        """)
        _, _, rels, _ = self.migrate(tmp_path, repo, "a")
        assert len(rels) == 1
        model.write_text("view: a { dimension: id {} }\nexplore: a {}\n")
        self.migrate(tmp_path, repo, "a")
        assert not (tmp_path / "out" / "topics" / "a.relationships").exists()

    def test_conflicting_relationship_in_other_explore_reported(self, tmp_path):
        repo = tmp_path / "repo"
        write(repo / "m.model.lkml", """
            view: a { dimension: id {} }
            view: b { dimension: id {} }
            explore: a { join: b { sql_on: ${a.id} = ${b.id} ;; } }
            explore: a2 { from: a join: b { sql_on: ${a2.id} = ${b.other_id} ;; } }
        """)
        self.migrate(tmp_path, repo, "a")
        result, _, _, report = self.migrate(tmp_path, repo, "a2")
        assert "Conflicting relationship" in report
        assert any("Conflicting relationship" in i for i in result.issues)

    def test_topic_selectors_quoted(self, tmp_path, explore_repo):
        self.migrate(tmp_path, explore_repo)
        text = (tmp_path / "out" / "topics" / "orders.topic.yaml").read_text()
        assert 'fields: [all_views.*, "-tag:table_key"]' in text

    def test_duplicate_explore_stops(self, tmp_path):
        repo = tmp_path / "repo"
        write(repo / "one.model.lkml", "explore: orders {}\n")
        write(repo / "two.model.lkml", "explore: orders {}\n")
        result = MigrationOrchestrator(skill_path=SKILL_ROOT, looker_repo=repo,
                                       omni_repo=tmp_path / "out").migrate_explore("orders")
        assert not result.success
        assert "more than once" in result.error_message

    def test_explore_refinement_reported(self, tmp_path):
        repo = tmp_path / "repo"
        write(repo / "m.model.lkml", "view: orders { dimension: id {} }\nexplore: orders {}\n")
        write(repo / "refine.lkml", "explore: +orders { sql_always_where: 1 = 0 ;; }\n")
        result, _, _, report = self.migrate(tmp_path, repo)
        assert "Refinements (explore: +name) NOT applied" in report
        assert any("Refinements" in i for i in result.issues)

    def test_whitespace_tolerant_discovery(self, tmp_path):
        write(tmp_path / "x.lkml", 'explore : spaced {}\naccess_grant : g { user_attribute: a allowed_values: ["1"] }\n')
        assert find_explore_files(str(tmp_path))
        assert "g" in load_access_grants(str(tmp_path))


# ----------------------------------------------------------------------
# 2. Merge semantics
# ----------------------------------------------------------------------

class TestMergeSemantics:
    @pytest.fixture
    def repo(self, tmp_path):
        write(tmp_path / "repo" / "orders.view.lkml", """
            view: orders {
              sql_table_name: shop.orders ;;
              dimension: id { primary_key: yes type: number sql: ${TABLE}.id ;; }
              dimension: status { type: string sql: ${TABLE}.status ;; }
              dimension: country { type: string sql: ${TABLE}.country ;; }
              dimension: email { type: string sql: ${TABLE}.email ;; required_access_grants: [can_view_pii] }
              measure: total { type: sum sql: ${TABLE}.amount ;; }
            }
        """)
        return tmp_path / "repo"

    def run(self, tmp_path, repo, rows, verified=True, source=None):
        orch = orchestrator(tmp_path, repo, csv(tmp_path, rows), omni_source_repo=source)
        phase = int(rows[0][0])
        result = orch.migrate_view("orders", phase, "Team A", verified=verified)
        assert result.success, result.error_message
        return load_yaml(tmp_path / "out" / "views" / "orders.view.yaml"), result

    def test_phase_progression(self, tmp_path, repo):
        p0 = [("0", "orders", "id", "dimension"), ("0", "orders", "country", "dimension")]
        view, _ = self.run(tmp_path, repo, p0)
        assert view["dimensions"]["status"]["hidden"] is True
        # P1 migrates status: it becomes visible; P0's country stays visible
        view, _ = self.run(tmp_path, repo, [("1", "orders", "status", "dimension")])
        assert "hidden" not in view["dimensions"]["status"]
        assert "mig_phase_1" in view["dimensions"]["status"]["tags"]
        assert "in_omni_in_looker_not_csv" not in view["dimensions"]["status"]["tags"]
        assert "hidden" not in view["dimensions"]["country"]
        assert "mig_phase_0" in view["dimensions"]["country"]["tags"]

    def test_verified_and_unverified_exclusive(self, tmp_path, repo):
        rows = [("0", "orders", "id", "dimension")]
        self.run(tmp_path, repo, rows, verified=False)
        view, _ = self.run(tmp_path, repo, rows, verified=True)
        assert "verified" in view["tags"] and "unverified" not in view["tags"]
        assert view["dimensions"]["id"]["tags"].count("verified") == 1
        assert "unverified" not in view["dimensions"]["id"]["tags"]

    def test_conversion_owned_keys_replaced(self, tmp_path, repo):
        source = write(tmp_path / "omni" / "shop" / "orders.view", """
            schema: shop
            table_name: orders
            dimensions:
              id: {}
              status:
                sql: UPPER(status)
                hidden: true
                format: id
                description: Order status
                tags: [mig_phase_0, unverified, core]
            measures:
              total:
                sql: ${amount}
                aggregate_type: average
                filters: {status: {is: Complete}}
        """).parent.parent
        rows = [("0", "orders", "id", "dimension"), ("0", "orders", "status", "dimension"),
                ("0", "orders", "total", "measure")]
        view, _ = self.run(tmp_path, repo, rows, source=source)
        status = view["dimensions"]["status"]
        assert "sql" not in status and "hidden" not in status and "format" not in status
        assert "core" in status["tags"] and "unverified" not in status["tags"]
        total = view["measures"]["total"]
        assert total["aggregate_type"] == "sum"
        assert total["sql"] == "amount"
        assert "filters" not in total

    def test_out_of_scope_existing_field_gets_access_grant(self, tmp_path, repo):
        source = write(tmp_path / "omni" / "orders.view.yaml", """
            schema: shop
            table_name: orders
            dimensions:
              id: {}
              email: {description: Email}
        """).parent
        view, result = self.run(tmp_path, repo, [("0", "orders", "id", "dimension")], source=source)
        assert view["dimensions"]["email"]["required_access_grants"] == ["can_view_pii"]
        assert view["dimensions"]["email"]["hidden"] is True
        assert any("can_view_pii" in i for i in result.issues)

    def test_unreadable_existing_view_is_an_error(self, tmp_path, repo):
        out = tmp_path / "out" / "views"
        out.mkdir(parents=True)
        (out / "orders.view.yaml").write_text("dimensions: [unclosed\n")
        orch = orchestrator(tmp_path, repo, csv(tmp_path, [("0", "orders", "id", "dimension")]))
        result = orch.migrate_view("orders", 0, "Team A")
        assert not result.success
        assert "could not be parsed" in result.error_message
        assert (out / "orders.view.yaml").read_text() == "dimensions: [unclosed\n"

    def test_multiple_source_matches_is_an_error(self, tmp_path, repo):
        write(tmp_path / "omni" / "a" / "orders.view", "table_name: orders\n")
        write(tmp_path / "omni" / "b" / "orders.view.yaml", "table_name: orders\n")
        orch = orchestrator(tmp_path, repo, csv(tmp_path, [("0", "orders", "id", "dimension")]),
                            omni_source_repo=tmp_path / "omni")
        result = orch.migrate_view("orders", 0, "Team A")
        assert not result.success and "More than one existing Omni view" in result.error_message

    def test_merge_stats_from_operations(self, tmp_path, repo):
        source = write(tmp_path / "omni" / "orders.view.yaml", """
            table_name: orders
            dimensions: {id: {}, status: {}, legacy_flag: {}}
        """).parent
        self.run(tmp_path, repo, [("0", "orders", "id", "dimension"), ("0", "orders", "total", "measure")],
                 source=source)
        report = (tmp_path / "out" / "reports" / "orders.migration.md").read_text()
        assert "**Updated (in scope)**: 1 fields" in report
        assert "**Added from Looker** (not in the Omni view): 1 fields" in report
        assert "**Hidden** (existing, not in scope): 2 fields" in report


class TestMergeUnit:
    def gen(self, tmp_path):
        return FileGenerator(output_dir=tmp_path)

    def test_rename_map_applies_to_filter_keys_and_qualified_refs(self, tmp_path):
        new = {"table_name": "orders", "dimensions": {"customer_id": {"sql": "user_id", "tags": ["mig_phase_0"]}},
               "measures": {"c": {"aggregate_type": "count", "tags": ["mig_phase_0"],
                                  "filters": {"customer_id": {"is": 1}, "orders.customer_id": {"is": 2}}},
                            "d": {"sql": "${orders.customer_id}", "aggregate_type": "count_distinct",
                                  "tags": ["mig_phase_0"]}}}
        existing = {"table_name": "orders", "dimensions": {"user_id": {}}}
        merged, info = self.gen(tmp_path).merge_with_existing_view(
            new, existing, {"customer_id", "c", "d"}, {"customer_id": "user_id"}, view_name="orders")
        assert merged["measures"]["c"]["filters"] == {"user_id": {"is": 1}, "orders.user_id": {"is": 2}}
        assert merged["measures"]["d"]["sql"] == "${orders.user_id}"
        assert info["stats"]["renamed"] == 1

    def test_self_reference_after_rename_rejected(self, tmp_path):
        # customer_id is merged under the existing user_id field; its own sql
        # then reads ${user_id}: a self-reference
        new = {"table_name": "orders", "dimensions": {"customer_id": {"sql": "${user_id} + 0", "tags": ["mig_phase_0"]}}}
        existing = {"table_name": "orders", "dimensions": {"user_id": {}}}
        with pytest.raises(MergeError, match="references itself"):
            self.gen(tmp_path).merge_with_existing_view(
                new, existing, {"customer_id"}, {"customer_id": "user_id"}, view_name="orders")

    def test_missing_reference_rejected(self, tmp_path):
        new = {"table_name": "orders", "dimensions": {"x": {"sql": "${nope}", "tags": ["mig_phase_0"]}}}
        with pytest.raises(MergeError, match="not a field of the view"):
            self.gen(tmp_path).merge_with_existing_view(new, {"table_name": "orders"}, {"x"}, {})

    def test_two_primary_keys_rejected(self, tmp_path):
        new = {"table_name": "orders", "dimensions": {"id": {"primary_key": True, "tags": ["mig_phase_0"]}}}
        existing = {"table_name": "orders", "dimensions": {"id": {}, "uuid": {"primary_key": True}}}
        with pytest.raises(MergeError, match="more than one primary key"):
            self.gen(tmp_path).merge_with_existing_view(new, existing, {"id"}, {})

    def test_derived_source_replaced_and_noted(self, tmp_path):
        new = {"sql": "SELECT 2 AS id", "dimensions": {}}
        existing = {"sql": "SELECT 1 AS id", "dimensions": {"id": {}}}
        merged, info = self.gen(tmp_path).merge_with_existing_view(new, existing, set(), {})
        assert merged["sql"] == "SELECT 2 AS id"
        assert any("Replaced existing `sql`" in n for n in info["notes"])

        existing = {"table_name": "orders"}
        merged, info = self.gen(tmp_path).merge_with_existing_view(new, existing, set(), {})
        assert "table_name" not in merged
        assert any("Removed existing `table_name`" in n for n in info["notes"])


# ----------------------------------------------------------------------
# 5. Dependencies
# ----------------------------------------------------------------------

class TestDependencies:
    @pytest.fixture
    def repo(self, tmp_path):
        write(tmp_path / "repo" / "orders.view.lkml", """
            view: orders {
              sql_table_name: shop.orders ;;
              dimension: id { primary_key: yes type: number sql: ${TABLE}.id ;; }
              dimension: tier { type: tier tiers: [0, 10] sql: ${TABLE}.amount ;; }
              dimension: uses_tier { sql: CONCAT(${tier}, 'x') ;; }
              dimension: uses_uses_tier { sql: ${uses_tier} ;; }
              dimension: loop_a { sql: ${loop_b} ;; }
              dimension: loop_b { sql: ${loop_a} ;; }
              dimension: hidden_broken { sql: ${created_date} ;; }
              dimension: fine { sql: ${TABLE}.fine ;; }
            }
        """)
        return tmp_path / "repo"

    def test_blocked_dependents_not_written(self, tmp_path, repo):
        rows = [("0", "orders", n, "dimension") for n in ("id", "uses_uses_tier", "loop_a", "fine")]
        orch = orchestrator(tmp_path, repo, csv(tmp_path, rows))
        result = orch.migrate_view("orders", 0, "Team A")
        assert result.success
        view = load_yaml(tmp_path / "out" / "views" / "orders.view.yaml")
        dims = view["dimensions"]
        for name in ("tier", "uses_tier", "uses_uses_tier", "loop_a", "loop_b", "hidden_broken"):
            assert name not in dims, name
        assert set(dims) == {"id", "fine"}
        issues = "\n".join(result.issues)
        assert "uses_uses_tier" in issues and "loop_a" in issues and "circular" in issues
        report = (tmp_path / "out" / "reports" / "orders.migration.md").read_text()
        assert "hidden_broken" in report and "created_date" in report
        assert "Converted 2 dimensions" in report


# ----------------------------------------------------------------------
# 6-8. Sources, native derived tables
# ----------------------------------------------------------------------

class TestSources:
    def run(self, tmp_path, view_lkml, rows, extra=""):
        write(tmp_path / "repo" / "v.view.lkml", view_lkml)
        write(tmp_path / "repo" / "m.model.lkml", extra)
        orch = orchestrator(tmp_path, tmp_path / "repo", csv(tmp_path, rows))
        return orch.migrate_view(rows[0][1], 0, "Team A")

    def test_catalog_kept(self, tmp_path):
        result = self.run(tmp_path, """
            view: users { sql_table_name: `my-project.thelook_ecommerce.users` ;; dimension: id { primary_key: yes } }
        """, [("0", "users", "id", "dimension")])
        assert result.success
        view = load_yaml(result.view_file_path)
        assert (view["catalog"], view["schema"], view["table_name"]) == ("my-project", "thelook_ecommerce", "users")

    @pytest.mark.parametrize("table", ["${base.SQL_TABLE_NAME}", "shop.{{ _user_attributes['t'] }}"])
    def test_dynamic_table_name_fails_cleanly(self, tmp_path, table):
        result = self.run(tmp_path, f"view: users {{ sql_table_name: {table} ;; dimension: id {{}} }}",
                          [("0", "users", "id", "dimension")])
        assert not result.success
        assert "MANUAL ACTION REQUIRED" in result.error_message
        assert not (tmp_path / "out").exists()

    NDT_MODEL = """
        view: order_items { sql_table_name: shop.order_items ;; dimension: status {} dimension: amount { type: number } }
        explore: sales { from: order_items }
    """

    def test_native_dt_sorts_limit_and_aliased_base(self, tmp_path):
        result = self.run(tmp_path, """
            view: top_status {
              derived_table: { explore_source: sales {
                column: status { field: sales.status }
                sorts: [sales.status: desc]
                limit: 10
                filters: [sales.amount: ">10"]
              } }
              dimension: status { primary_key: yes }
            }
        """, [("0", "top_status", "status", "dimension")], self.NDT_MODEL)
        assert result.success, result.error_message
        query = load_yaml(result.view_file_path)["query"]
        # explore sales { from: order_items }: every sales.* ref is rewritten to the base view
        assert query == {"fields": {"order_items.status": "status"}, "base_view": "order_items", "topic": "sales",
                         "filters": {"order_items.amount": {"greater_than": 10}},
                         "sorts": [{"field": "order_items.status", "desc": True}], "limit": 10}
        assert "sales." not in str(query["fields"]) + str(query["filters"]) + str(query["sorts"])
        assert any("rewritten from sales.*" in i for i in result.issues)

    @pytest.mark.parametrize("block,reason", [
        ("bind_filters: { from_field: sales.status to_field: sales.status }", "bind_filters"),
        ("bind_all_filters: yes", "bind_all_filters"),
        ("column: status_again { field: sales.status }", "selected twice"),
        ('filters: [sales.status: "%x%"]', "filter sales.status"),
    ])
    def test_native_dt_unsupported(self, tmp_path, block, reason):
        result = self.run(tmp_path, f"""
            view: top_status {{
              derived_table: {{ explore_source: sales {{
                column: status {{ field: sales.status }}
                {block}
              }} }}
              dimension: status {{ primary_key: yes }}
            }}
        """, [("0", "top_status", "status", "dimension")], self.NDT_MODEL)
        assert not result.success
        assert reason in result.error_message

    def test_duplicate_view_definition_stops(self, tmp_path):
        write(tmp_path / "repo" / "a.view.lkml", "view: users { dimension: id {} }")
        write(tmp_path / "repo" / "b" / "users.view.lkml", "view: users { dimension: id {} }")
        orch = orchestrator(tmp_path, tmp_path / "repo", csv(tmp_path, [("0", "users", "id", "dimension")]))
        result = orch.migrate_view("users", 0, "Team A")
        assert not result.success and "more than one file" in result.error_message

    def test_view_refinement_reported(self, tmp_path):
        result = self.run(tmp_path, "view: users { sql_table_name: s.users ;; dimension: id { primary_key: yes } }",
                          [("0", "users", "id", "dimension")],
                          "view: +users { dimension: id { required_access_grants: [g] } }")
        assert result.success
        assert any("refined" in i for i in result.issues)

    def test_duplicate_grant_stops(self, tmp_path):
        write(tmp_path / "a.lkml", 'access_grant: g { user_attribute: a allowed_values: ["1"] }')
        write(tmp_path / "b.lkml", 'access_grant: g { user_attribute: b allowed_values: ["2"] }')
        with pytest.raises(ValueError, match="more than one file"):
            load_access_grants(str(tmp_path))
        result = run_cli("--parse-access-grants", "--looker-repo", tmp_path, cwd=tmp_path)
        assert result.returncode == 1


# ----------------------------------------------------------------------
# 9. Reports, config, analysis
# ----------------------------------------------------------------------

class TestReportsAndConfig:
    def test_missing_pk_on_measure_only_view(self, tmp_path):
        write(tmp_path / "repo" / "v.view.lkml", ORDERS_VIEW)
        orch = orchestrator(tmp_path, tmp_path / "repo", csv(tmp_path, [("0", "orders", "count", "measure")]))
        result = orch.migrate_view("orders", 0, "Team A")
        # id is written hidden, but still the primary key; drop it to get a measure-only view
        assert result.success
        write(tmp_path / "repo" / "v.view.lkml", "view: orders { sql_table_name: shop.orders ;; measure: count { type: count } }")
        (tmp_path / "out" / "views" / "orders.view.yaml").unlink()
        result = orchestrator(tmp_path, tmp_path / "repo", tmp_path / "scope.csv").migrate_view("orders", 0, "Team A")
        assert any("No primary key" in i for i in result.issues)

    def test_converted_count_excludes_failures(self, tmp_path):
        write(tmp_path / "repo" / "v.view.lkml", """
            view: orders {
              sql_table_name: shop.orders ;;
              dimension: id { primary_key: yes }
              measure: running { type: running_total sql: ${count} ;; }
              measure: count { type: count }
            }
        """)
        rows = [("0", "orders", "id", "dimension"), ("0", "orders", "running", "measure")]
        result = orchestrator(tmp_path, tmp_path / "repo", csv(tmp_path, rows)).migrate_view("orders", 0, "Team A")
        report = (tmp_path / "out" / "reports" / "orders.migration.md").read_text()
        assert "Converted 1 dimensions and 0 measures" in report
        assert result.success

    def test_bad_yaml_config_exits_1(self, tmp_path):
        bad = write(tmp_path / "bad.yaml", "canonical_mappings: [unclosed\n")
        result = run_cli("--explore", "x", "--looker-repo", tmp_path, "--config", bad, cwd=tmp_path)
        assert result.returncode == 1
        assert "could not be read" in result.stdout
        assert "Traceback" not in result.stderr

    def test_analysis_ready_requires_base_view(self, tmp_path):
        write(tmp_path / "m.model.lkml", """
            explore: order_items { join: users { sql_on: ${order_items.user_id} = ${users.id} ;; } }
        """)
        explores = parse_explore_file(str(tmp_path / "m.model.lkml"))
        report = generate_explore_report(explores, {"users"}, [])
        assert "✅ Ready" not in report
        assert "base view not migrated" in report
        assert "✅ Ready" in generate_explore_report(explores, {"users", "order_items"}, [])


# ----------------------------------------------------------------------
# Round 3 regressions
# ----------------------------------------------------------------------

class TestRound3:
    def test_existing_column_not_equivalent_to_transformed_looker_field(self, tmp_path):
        """Existing raw `amount` + Looker `amount = ${TABLE}.amount * 100`: SUM(${TABLE}.amount) stays raw"""
        write(tmp_path / "repo" / "orders.view.lkml", """
            view: orders {
              sql_table_name: shop.orders ;;
              dimension: id { primary_key: yes type: number sql: ${TABLE}.id ;; }
              dimension: amount { type: number sql: ${TABLE}.amount * 100 ;; }
              measure: total { type: number sql: SUM(${TABLE}.amount) ;; }
            }
        """)
        source = write(tmp_path / "omni" / "orders.view.yaml", "table_name: orders\nschema: shop\ndimensions: {id: {}, amount: {}}\n").parent
        rows = [("0", "orders", "id", "dimension"), ("0", "orders", "amount", "dimension"), ("0", "orders", "total", "measure")]
        result = orchestrator(tmp_path, tmp_path / "repo", csv(tmp_path, rows), omni_source_repo=source).migrate_view("orders", 0, "Team A")
        assert result.success, result.error_message
        view = load_yaml(result.view_file_path)
        assert view["measures"]["total"]["sql"] == "SUM(amount)"
        assert view["dimensions"]["amount"]["sql"] == "amount * 100"

    def test_existing_out_of_scope_field_with_custom_sql_not_used(self, tmp_path):
        write(tmp_path / "repo" / "orders.view.lkml", """
            view: orders {
              sql_table_name: shop.orders ;;
              dimension: id { primary_key: yes sql: ${TABLE}.id ;; }
              dimension: amount { type: number sql: ${TABLE}.amount ;; }
              measure: total { type: number sql: SUM(${TABLE}.amount) ;; }
            }
        """)
        source = write(tmp_path / "omni" / "orders.view.yaml",
                       "table_name: orders\nschema: shop\ndimensions: {id: {}, amount: {sql: amount / 100}}\n").parent
        rows = [("0", "orders", "id", "dimension"), ("0", "orders", "total", "measure")]
        result = orchestrator(tmp_path, tmp_path / "repo", csv(tmp_path, rows), omni_source_repo=source).migrate_view("orders", 0, "Team A")
        assert load_yaml(result.view_file_path)["measures"]["total"]["sql"] == "SUM(amount)"

    def test_catalog_not_named_by_lookml_stops_merge(self, tmp_path):
        write(tmp_path / "repo" / "orders.view.lkml", ORDERS_VIEW)
        out = tmp_path / "out" / "views"
        write(out / "orders.view.yaml", "catalog: old_project\nschema: shop\ntable_name: orders\ndimensions: {id: {}}\n")
        before = (out / "orders.view.yaml").read_text()
        result = orchestrator(tmp_path, tmp_path / "repo", csv(tmp_path, [("0", "orders", "id", "dimension")])).migrate_view("orders", 0, "Team A")
        assert not result.success
        assert "catalog `old_project`" in result.error_message
        assert (out / "orders.view.yaml").read_text() == before
        assert not (tmp_path / "out" / "reports").exists()

    def test_catalog_replaced_when_lookml_names_one(self, tmp_path):
        write(tmp_path / "repo" / "orders.view.lkml", ORDERS_VIEW.replace("shop.orders", "new_project.shop.orders"))
        write(tmp_path / "out" / "views" / "orders.view.yaml", "catalog: old_project\nschema: shop\ntable_name: orders\n")
        result = orchestrator(tmp_path, tmp_path / "repo", csv(tmp_path, [("0", "orders", "id", "dimension")])).migrate_view("orders", 0, "Team A")
        assert result.success
        assert load_yaml(result.view_file_path)["catalog"] == "new_project"
        assert any("Replaced existing `catalog`" in i for i in result.issues)

    @pytest.mark.parametrize("dt_sql", [
        "SELECT * FROM ${orders.SQL_TABLE_NAME}",
        "SELECT * FROM shop.orders WHERE {% condition status_filter %} status {% endcondition %}",
    ])
    def test_liquid_or_refs_in_sql_derived_table_rejected(self, tmp_path, dt_sql):
        write(tmp_path / "repo" / "v.view.lkml", f"""
            view: facts {{
              derived_table: {{ sql: {dt_sql} ;; }}
              dimension: id {{ primary_key: yes }}
            }}
        """)
        result = run_cli("--view", "facts", "--phase", "0", "--owner", "Team A", "--looker-repo", tmp_path / "repo",
                         "--csv", csv(tmp_path, [("0", "facts", "id", "dimension")]), "--omni-repo", tmp_path / "out" / "views",
                         cwd=tmp_path)
        assert result.returncode == 1
        assert "MANUAL ACTION REQUIRED" in result.stdout
        assert not (tmp_path / "out").exists()

    def test_merge_adds_new_out_of_scope_fields_hidden(self, tmp_path):
        write(tmp_path / "repo" / "orders.view.lkml", ORDERS_VIEW)
        source = write(tmp_path / "omni" / "orders.view.yaml", "schema: shop\ntable_name: orders\ndimensions: {id: {}}\n").parent
        result = orchestrator(tmp_path, tmp_path / "repo", csv(tmp_path, [("0", "orders", "id", "dimension")]),
                              omni_source_repo=source).migrate_view("orders", 0, "Team A")
        view = load_yaml(result.view_file_path)
        # same as a fresh generation: status/amount/count exist, hidden, provenance-tagged
        for section, name in (("dimensions", "status"), ("dimensions", "amount"), ("measures", "count")):
            assert view[section][name]["hidden"] is True
            assert view[section][name]["tags"] == ["in_omni_in_looker_not_csv"]
        report = result.report_file_path.read_text()
        assert "**Added hidden** (Looker fields not in scope, not in the Omni view): 3 fields" in report

    def test_looker_hidden_is_honoured_for_in_scope_fields(self, tmp_path):
        write(tmp_path / "repo" / "orders.view.lkml", """
            view: orders {
              sql_table_name: shop.orders ;;
              dimension: id { primary_key: yes hidden: yes sql: ${TABLE}.id ;; }
            }
        """)
        result = orchestrator(tmp_path, tmp_path / "repo", csv(tmp_path, [("0", "orders", "id", "dimension")])).migrate_view("orders", 0, "Team A")
        dim = load_yaml(result.view_file_path)["dimensions"]["id"]
        assert dim["hidden"] is True
        assert "mig_phase_0" in dim["tags"]

    def test_cli_help_and_startup_describe_output_merge(self, tmp_path):
        help_text = " ".join(run_cli("--help", cwd=tmp_path).stdout.split())
        assert "already in --omni-repo (from an earlier run) is merged into" in help_text
        result = run_cli("--parse-access-grants", "--looker-repo", tmp_path, "--dry-run", cwd=tmp_path)
        assert "creating from scratch" not in result.stdout

    def test_explore_files_rendered_before_writing(self, tmp_path, monkeypatch):
        write(tmp_path / "repo" / "m.model.lkml", """
            view: a { dimension: id {} }
            view: b { dimension: id {} }
            explore: a { join: b { sql_on: ${a.id} = ${b.id} ;; } }
        """)
        orch = MigrationOrchestrator(skill_path=SKILL_ROOT, looker_repo=tmp_path / "repo", omni_repo=tmp_path / "out" / "topics")

        def boom(*args, **kwargs):
            raise RuntimeError("render failed")
        monkeypatch.setattr(orch.file_generator, "render_relationships_yaml", boom)
        result = orch.migrate_explore("a")
        assert not result.success
        assert not (tmp_path / "out").exists()


# ----------------------------------------------------------------------
# Round 4 regressions: a merge never drops a Looker definition or grant
# ----------------------------------------------------------------------

class TestRound4:
    def run(self, tmp_path, view_lkml, existing_yaml, rows):
        write(tmp_path / "repo" / "orders.view.lkml", view_lkml)
        source = write(tmp_path / "omni" / "orders.view.yaml", existing_yaml).parent
        orch = orchestrator(tmp_path, tmp_path / "repo", csv(tmp_path, rows), omni_source_repo=source)
        result = orch.migrate_view("orders", 0, "Team A")
        assert result.success, result.error_message
        return load_yaml(result.view_file_path), result, result.report_file_path.read_text()

    def test_rename_target_owned_by_another_looker_field_is_not_taken(self, tmp_path):
        view, result, _ = self.run(tmp_path, """
            view: orders {
              sql_table_name: shop.orders ;;
              dimension: id { primary_key: yes sql: ${TABLE}.id ;; }
              dimension: amount { type: number sql: ${TABLE}.amount * 100 ;; }
              dimension: raw_amount { type: number sql: ${TABLE}.amount ;; }
              measure: total { type: sum sql: ${amount} ;; }
            }
        """, "schema: shop\ntable_name: orders\ndimensions: {id: {}, amount: {}}\n",
            [("0", "orders", n, t) for n, t in (("id", "dimension"), ("amount", "dimension"),
                                                ("raw_amount", "dimension"), ("total", "measure"))])
        dims = view["dimensions"]
        # the Looker definition survives (raw_amount is exactly the raw column)
        assert dims["amount"]["sql"] == "${raw_amount} * 100"
        assert dims["raw_amount"]["sql"] == "amount"         # kept under its own name
        assert view["measures"]["total"]["sql"] == "${amount}"
        assert any("raw_amount" in i and "kept as `raw_amount`" in i for i in result.issues)

    def test_two_fields_reading_one_column_only_first_merged(self, tmp_path):
        view, result, _ = self.run(tmp_path, """
            view: orders {
              sql_table_name: shop.orders ;;
              dimension: id { primary_key: yes sql: ${TABLE}.id ;; }
              dimension: buyer_id { type: number sql: ${TABLE}.user_id ;; }
              dimension: customer_id { type: number sql: ${TABLE}.user_id ;; }
            }
        """, "schema: shop\ntable_name: orders\ndimensions: {id: {}, user_id: {}}\n",
            [("0", "orders", n, "dimension") for n in ("id", "buyer_id", "customer_id")])
        dims = view["dimensions"]
        assert "buyer_id" in dims["user_id"]["synonyms"]
        # customer_id reads the same raw column as the merged user_id field
        assert dims["customer_id"]["sql"] == "${user_id}"
        assert "sql" not in dims["user_id"]
        assert any("both read column `user_id`" in i for i in result.issues)

    def test_retained_field_keeps_lookml_grant_when_conversion_fails(self, tmp_path):
        view, result, report = self.run(tmp_path, """
            view: orders {
              sql_table_name: shop.orders ;;
              dimension: id { primary_key: yes sql: ${TABLE}.id ;; }
              dimension: email {
                sql: {% if _user_attributes['region'] == 'eu' %} NULL {% else %} ${TABLE}.email {% endif %} ;;
                required_access_grants: [can_view_pii]
              }
            }
        """, "schema: shop\ntable_name: orders\ndimensions:\n  id: {}\n  email: {tags: [mig_phase_0, verified]}\n",
            [("0", "orders", "id", "dimension"), ("0", "orders", "email", "dimension")])
        email = view["dimensions"]["email"]
        assert email["required_access_grants"] == ["can_view_pii"]
        assert "hidden" not in email                     # previously migrated: kept visible ...
        assert any("existing Omni field `email` was kept" in i for i in result.issues)  # ... but flagged
        assert "required_access_grants present on these fields in the output" in report
        assert "  email:" in report

    def test_grant_follows_renamed_field(self, tmp_path):
        view, _, report = self.run(tmp_path, """
            view: orders {
              sql_table_name: shop.orders ;;
              dimension: id { primary_key: yes sql: ${TABLE}.id ;; }
              dimension: customer_email { sql: ${TABLE}.email ;; required_access_grants: [can_view_pii] }
            }
        """, "schema: shop\ntable_name: orders\ndimensions: {id: {}, email: {}}\n",
            [("0", "orders", "id", "dimension"), ("0", "orders", "customer_email", "dimension")])
        assert view["dimensions"]["email"]["required_access_grants"] == ["can_view_pii"]
        assert "email (Looker customer_email)" in report

    def test_grant_reported_absent_not_carried_when_field_not_output(self, tmp_path):
        write(tmp_path / "repo" / "orders.view.lkml", """
            view: orders {
              sql_table_name: shop.orders ;;
              dimension: id { primary_key: yes sql: ${TABLE}.id ;; }
              dimension: email { sql: {% if x %}a{% endif %} ;; required_access_grants: [can_view_pii] }
            }
        """)
        rows = [("0", "orders", "id", "dimension"), ("0", "orders", "email", "dimension")]
        result = orchestrator(tmp_path, tmp_path / "repo", csv(tmp_path, rows)).migrate_view("orders", 0, "Team A")
        report = result.report_file_path.read_text()
        assert "CARRIED" not in report
        assert "not in the output view: email" in report


# ----------------------------------------------------------------------
# Round 5: grants follow fields renamed by an EARLIER merge run
# ----------------------------------------------------------------------

class TestRound5:
    BASE = """
        view: orders {{
          sql_table_name: shop.orders ;;
          dimension: id {{ primary_key: yes sql: ${{TABLE}}.id ;; }}
          dimension: customer_email {{ {body} }}
        }}
    """

    def migrate(self, tmp_path, body, rows, source=None):
        write(tmp_path / "repo" / "orders.view.lkml", self.BASE.format(body=body))
        orch = orchestrator(tmp_path, tmp_path / "repo", csv(tmp_path, rows), omni_source_repo=source)
        result = orch.migrate_view("orders", 0, "Team A")
        assert result.success, result.error_message
        return load_yaml(result.view_file_path), result, result.report_file_path.read_text()

    @pytest.fixture
    def run1(self, tmp_path):
        source = write(tmp_path / "omni" / "orders.view.yaml",
                       "schema: shop\ntable_name: orders\ndimensions: {id: {}, email: {}}\n").parent
        view, _, _ = self.migrate(tmp_path, "sql: ${TABLE}.email ;;",
                                  [("0", "orders", "id", "dimension"), ("0", "orders", "customer_email", "dimension")],
                                  source)
        assert "from_looker__customer_email" in view["dimensions"]["email"]["tags"]
        assert "customer_email" not in view["dimensions"]
        return source

    def check(self, view, result, report):
        dims = view["dimensions"]
        assert dims["email"]["required_access_grants"] == ["can_view_pii"]
        assert "customer_email" not in dims                       # no duplicate copy
        assert "email (Looker customer_email)" in report
        assert "not in the output view" not in report
        assert not any("without required_access_grants" in i for i in result.issues)

    def test_out_of_scope_in_later_run(self, tmp_path, run1):
        # run 2 merges into run 1's output (no source repo): only id in scope
        view, result, report = self.migrate(
            tmp_path, "sql: ${TABLE}.email ;; required_access_grants: [can_view_pii]",
            [("0", "orders", "id", "dimension")])
        self.check(view, result, report)
        assert "hidden" not in view["dimensions"]["email"]  # migrated earlier: stays visible

    def test_liquid_in_later_run(self, tmp_path, run1):
        view, result, report = self.migrate(
            tmp_path, "sql: {% if x %} NULL {% else %} ${TABLE}.email {% endif %} ;; required_access_grants: [can_view_pii]",
            [("0", "orders", "id", "dimension"), ("0", "orders", "customer_email", "dimension")])
        self.check(view, result, report)
        assert any("existing Omni field `email` was kept" in i and "customer_email" in i for i in result.issues)

    def test_in_scope_again_merges_into_same_field(self, tmp_path, run1):
        view, _, _ = self.migrate(
            tmp_path, "sql: ${TABLE}.email ;; label: \"Customer Email\" required_access_grants: [can_view_pii]",
            [("0", "orders", "id", "dimension"), ("0", "orders", "customer_email", "dimension")])
        email = view["dimensions"]["email"]
        assert email["label"] == "Customer Email"
        assert email["required_access_grants"] == ["can_view_pii"]
        assert "customer_email" not in view["dimensions"]

    def test_column_counterpart_protected_without_provenance(self, tmp_path):
        # merging into an original source file that has no provenance tag:
        # the existing `email` exposes the same column, so it gets the grant
        source = write(tmp_path / "omni" / "orders.view.yaml",
                       "schema: shop\ntable_name: orders\ndimensions: {id: {}, email: {}}\n").parent
        view, result, _ = self.migrate(
            tmp_path, "sql: ${TABLE}.email ;; required_access_grants: [can_view_pii]",
            [("0", "orders", "id", "dimension")], source)
        assert view["dimensions"]["email"]["required_access_grants"] == ["can_view_pii"]
        assert view["dimensions"]["customer_email"]["required_access_grants"] == ["can_view_pii"]
        assert any("reads the same column as Looker `customer_email`" in i for i in result.issues)


# ----------------------------------------------------------------------
# Round 6: rename conflicts fail closed
# ----------------------------------------------------------------------

class TestRound6:
    def migrate(self, tmp_path, lkml, rows, source=None):
        write(tmp_path / "repo" / "orders.view.lkml", lkml)
        return orchestrator(tmp_path, tmp_path / "repo", csv(tmp_path, rows),
                            omni_source_repo=source).migrate_view("orders", 0, "Team A")

    def test_prior_rename_target_now_a_looker_field_is_an_error(self, tmp_path):
        source = write(tmp_path / "omni" / "orders.view.yaml",
                       "schema: shop\ntable_name: orders\ndimensions: {id: {}, email: {}}\n").parent
        run1 = self.migrate(tmp_path, """
            view: orders {
              sql_table_name: shop.orders ;;
              dimension: id { primary_key: yes sql: ${TABLE}.id ;; }
              dimension: customer_email { sql: ${TABLE}.email ;; }
            }
        """, [("0", "orders", "id", "dimension"), ("0", "orders", "customer_email", "dimension")], source)
        assert run1.success
        out = tmp_path / "out" / "views" / "orders.view.yaml"
        before = out.read_text()
        run2 = self.migrate(tmp_path, """
            view: orders {
              sql_table_name: shop.orders ;;
              dimension: id { primary_key: yes sql: ${TABLE}.id ;; }
              dimension: customer_email { sql: ${TABLE}.email ;; }
              dimension: email { sql: LOWER(${TABLE}.email) ;; }
              dimension: normalized_email { sql: ${email} ;; }
            }
        """, [("0", "orders", n, "dimension") for n in ("id", "customer_email", "email", "normalized_email")])
        assert not run2.success
        assert "customer_email" in run2.error_message and "`email`" in run2.error_message
        assert "from_looker__customer_email" in run2.error_message
        assert out.read_text() == before  # nothing written

    def test_same_looker_field_tagged_twice_is_an_error(self, tmp_path):
        write(tmp_path / "out" / "views" / "orders.view.yaml",
              "schema: shop\ntable_name: orders\ndimensions:\n"
              "  a: {tags: [from_looker__x]}\n  b: {tags: [from_looker__x]}\n")
        result = self.migrate(tmp_path, ORDERS_VIEW, [("0", "orders", "id", "dimension")])
        assert not result.success and "tagged" in result.error_message

    def test_stale_tag_for_removed_looker_field_ignored(self, tmp_path):
        write(tmp_path / "out" / "views" / "orders.view.yaml",
              "schema: shop\ntable_name: orders\ndimensions:\n  id: {}\n  email: {tags: [from_looker__gone]}\n")
        result = self.migrate(tmp_path, ORDERS_VIEW, [("0", "orders", "id", "dimension")])
        assert result.success

    def test_dimension_and_measure_with_same_name_is_an_error(self, tmp_path):
        write(tmp_path / "out" / "views" / "orders.view.yaml",
              "schema: shop\ntable_name: orders\ndimensions: {id: {}}\nmeasures: {status: {aggregate_type: count}}\n")
        result = self.migrate(tmp_path, ORDERS_VIEW, [("0", "orders", "id", "dimension"), ("0", "orders", "status", "dimension")])
        assert not result.success and "both a dimension and a measure: status" in result.error_message
