"""
View-side `extends`: template views, `extends:` on the Omni view with the
parent conflict rule, references to inherited fields, inherited primary keys
and views with no fields of their own. Each test builds a small LookML
project in tmp_path.
"""

import textwrap
from pathlib import Path

import pytest
from pydantic import ValidationError

from conftest import SKILL_ROOT, load_yaml
from lib import orchestrator as orchestrator_module
from lib.file_generators import FileGenerator
from lib.looker_parser import effective_view, load_views, resolve_view_extends
from lib.omni_models import OmniView
from lib.orchestrator import MigrationOrchestrator

CSV_HEADER = "phase,view,field,type,omni_view,omni_field,owner,status,notes\n"

BASE_VIEWS = """
view: base_entity {
  extension: required
  dimension: id { primary_key: yes type: number sql: ${TABLE}.id ;; }
  dimension: status { type: string sql: ${TABLE}.status ;; }
  dimension: amount { type: number sql: ${TABLE}.amount ;; }
  dimension: price_tier { type: tier tiers: [0, 10] sql: ${TABLE}.amount ;; }
  dimension_group: created { type: time timeframes: [raw, date, week] sql: ${TABLE}.created_at ;; }
  measure: count { type: count }
}

view: audit_fields {
  dimension: updated_by { type: string sql: ${TABLE}.updated_by ;; }
  dimension: status { type: string sql: UPPER(${TABLE}.status) ;; label: "Audit Status" }
}
"""


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text))
    return path


def csv(tmp_path, rows):
    """rows: (phase, view, field, type)"""
    lines = [f"{p},{v},{f},{t},,,Team A,ready," for p, v, f, t in rows]
    return write(tmp_path / "scope.csv", CSV_HEADER + "\n".join(lines) + "\n")


def make_repo(tmp_path, *lkml):
    for index, text in enumerate((BASE_VIEWS,) + lkml):
        write(tmp_path / "repo" / f"file_{index}.lkml", text)
    return tmp_path / "repo"


def migrate(tmp_path, repo, view, rows=(), **kw):
    """Migrate `view`; rows are (field, type) pairs of the CSV scope for it"""
    scope = [("0", view, field, kind) for field, kind in rows] or [("0", "unrelated_view", "x", "dimension")]
    orch = MigrationOrchestrator(skill_path=SKILL_ROOT, looker_repo=repo, omni_repo=tmp_path / "out" / "views",
                                 csv_path=csv(tmp_path, scope), **kw)
    result = orch.migrate_view(view, 0, "Team A")
    return result, orch


def output(tmp_path, view):
    out = load_yaml(tmp_path / "out" / "views" / f"{view}.view.yaml")
    report = (tmp_path / "out" / "reports" / f"{view}.migration.md").read_text()
    return out, report


# ----------------------------------------------------------------------
# Resolution of the parent chain (pure LookML)
# ----------------------------------------------------------------------

class TestResolveViewExtends:
    def resolve(self, tmp_path, lkml, name="child"):
        repo = make_repo(tmp_path, lkml)
        definitions, _ = load_views(str(repo))
        return resolve_view_extends(definitions[name][0], definitions), definitions

    def test_chain_and_diamond_are_not_conflicts(self, tmp_path):
        resolution, _ = self.resolve(tmp_path, """
            view: left { extends: [base_entity] dimension: left_only { sql: ${TABLE}.l ;; } }
            view: right { extends: [base_entity] dimension: right_only { sql: ${TABLE}.r ;; } }
            view: child { extends: [left, right] sql_table_name: shop.child ;; }
        """)
        assert resolution.problem is None
        assert resolution.parents == ["left", "right"]
        assert resolution.ancestors == ["base_entity", "left", "right"]
        assert {"id", "status", "created", "count", "left_only", "right_only"} <= set(resolution.fields)
        assert not [name for name, inherited in resolution.fields.items() if inherited.conflict]
        assert resolution.fields["id"].origin == "base_entity"
        assert resolution.fields["created"].kind == "dimension_group"

    def test_conflict_uses_the_last_parent(self, tmp_path):
        resolution, _ = self.resolve(tmp_path, """
            view: child { extends: [base_entity, audit_fields] sql_table_name: shop.child ;; }
        """)
        status = resolution.fields["status"]
        assert status.conflict == ["base_entity", "audit_fields"]
        assert status.origin == "audit_fields"
        assert status.data["sql"] == "UPPER(${TABLE}.status)"
        assert [n for n, f in resolution.fields.items() if f.conflict] == ["status"]

    def test_override_in_one_branch_of_a_diamond_is_a_conflict(self, tmp_path):
        resolution, _ = self.resolve(tmp_path, """
            view: left { extends: [base_entity] dimension: status { sql: LOWER(${TABLE}.status) ;; } }
            view: right { extends: [base_entity] }
            view: child { extends: [left, right] }
        """)
        assert resolution.fields["status"].conflict == ["left", "right"]

    @pytest.mark.parametrize("lkml, reason", [
        ("view: child { extends: [missing_parent] }", "`missing_parent` was not found"),
        ("view: child { extends: [loop_a] }\nview: loop_a { extends: [loop_b] }\nview: loop_b { extends: [loop_a] }",
         "circular extends"),
        ("view: child { extends: [child] }", "circular extends"),
        ("view: other { measure: status { type: count } }\nview: child { extends: [base_entity, other] }",
         "`status` is a dimension"),
    ])
    def test_unresolvable_chain(self, tmp_path, lkml, reason):
        resolution, _ = self.resolve(tmp_path, lkml)
        assert reason in resolution.problem
        assert resolution.fields == {}

    def test_parent_defined_twice(self, tmp_path):
        repo = make_repo(tmp_path, "view: child { extends: [twice] }\nview: twice { dimension: a {} }",
                         "view: twice { dimension: b {} }")
        definitions, _ = load_views(str(repo))
        resolution = resolve_view_extends(definitions["child"][0], definitions)
        assert "defined more than once" in resolution.problem

    def test_inherited_source_and_partial_override(self, tmp_path):
        resolution, definitions = self.resolve(tmp_path, """
            view: stored { extends: [base_entity] sql_table_name: shop.stored ;; }
            view: child {
              extends: [stored]
              dimension: status { label: "Child Status" }
              dimension: own { sql: ${TABLE}.own ;; }
            }
        """)
        assert resolution.source.sql_table_name == "shop.stored" and resolution.source.origin == "stored"
        merged = effective_view(definitions["child"][0], resolution)
        # A partial override keeps the parameters the child leaves out
        assert merged.dimensions["status"]["label"] == "Child Status"
        assert merged.dimensions["status"]["sql"] == "${TABLE}.status"
        assert set(merged.dimensions) == {"status", "own"}
        everything = effective_view(definitions["child"][0], resolution, include_inherited=True)
        assert {"id", "amount", "status", "own"} <= set(everything.dimensions)
        assert "created" in everything.dimension_groups and "count" in everything.measures

    def test_load_views_reads_every_file_and_lists_refinements(self, tmp_path):
        repo = make_repo(tmp_path, "view: +base_entity { dimension: extra {} }")
        definitions, refinements = load_views(str(repo))
        assert set(definitions) == {"base_entity", "audit_fields"}
        assert [Path(p).name for p in refinements["base_entity"]] == ["file_1.lkml"]


# ----------------------------------------------------------------------
# E1. Template views
# ----------------------------------------------------------------------

class TestTemplateViews:
    def test_extension_required_is_a_template(self, tmp_path):
        repo = make_repo(tmp_path, "view: orders { extends: [base_entity] sql_table_name: shop.orders ;; }")
        result, _ = migrate(tmp_path, repo, "base_entity", [("id", "dimension"), ("created", "dimension_group")])
        assert result.success, result.error_message
        view, report = output(tmp_path, "base_entity")
        assert view["template"] is True
        assert "schema" not in view and "table_name" not in view
        # ${TABLE}.col becomes the raw column; the child's table supplies it
        assert view["dimensions"]["created"]["sql"] == "created_at"
        assert "sql" not in view["dimensions"]["id"]
        assert view["dimensions"]["id"]["primary_key"] is True
        assert not any("using the view name as table" in issue for issue in result.issues)
        assert "Missing View Source" not in report
        assert "template: true" in report and "extension: required" in report and "orders" in report

    def test_view_without_table_extended_and_unused_is_a_template(self, tmp_path):
        repo = make_repo(tmp_path, """
            view: orders { extends: [audit_fields] sql_table_name: shop.orders ;; dimension: id { primary_key: yes } }
            explore: orders {}
        """)
        result, _ = migrate(tmp_path, repo, "audit_fields", [("updated_by", "dimension")])
        assert result.success, result.error_message
        view, report = output(tmp_path, "audit_fields")
        assert view["template"] is True and "table_name" not in view and "schema" not in view
        assert not any("using the view name as table" in issue for issue in result.issues)
        assert "never used directly" in report and "`orders`" in report

    @pytest.mark.parametrize("explore", [
        "explore: audit_fields {}",
        "explore: audit { from: audit_fields }",
        "explore: audit { view_name: audit_fields }",
        "explore: orders { join: audit_fields { sql_on: ${orders.id} = ${audit_fields.id} ;; } }",
        "explore: orders { join: audit { from: audit_fields sql_on: ${orders.id} = ${audit.id} ;; } }",
        "explore: base_explore { extension: required view_name: audit_fields }\n"
        "explore: audit { extends: [base_explore] }",
    ])
    def test_view_used_by_an_explore_is_not_a_template(self, tmp_path, explore):
        repo = make_repo(tmp_path, "view: orders { extends: [audit_fields] sql_table_name: shop.orders ;; }", explore)
        result, _ = migrate(tmp_path, repo, "audit_fields", [("updated_by", "dimension")])
        assert result.success, result.error_message
        view, _ = output(tmp_path, "audit_fields")
        assert "template" not in view and view["table_name"] == "audit_fields"
        assert any("using the view name as table" in issue for issue in result.issues)

    def test_view_without_table_that_nothing_extends_is_not_a_template(self, tmp_path):
        repo = make_repo(tmp_path)
        result, _ = migrate(tmp_path, repo, "audit_fields", [("updated_by", "dimension")])
        view, _ = output(tmp_path, "audit_fields")
        assert "template" not in view and view["table_name"] == "audit_fields"
        assert any("using the view name as table" in issue for issue in result.issues)

    def test_template_with_its_own_table_drops_it_and_the_child_carries_it(self, tmp_path):
        repo = make_repo(tmp_path, """
            view: stored_base {
              extension: required
              sql_table_name: shop.stored ;;
              dimension: id { primary_key: yes }
            }
            view: child { extends: [stored_base] dimension: name { sql: ${TABLE}.name ;; } }
        """)
        result, _ = migrate(tmp_path, repo, "stored_base", [("id", "dimension")])
        assert result.success, result.error_message
        view, _ = output(tmp_path, "stored_base")
        assert view["template"] is True and "table_name" not in view and "schema" not in view

        result, _ = migrate(tmp_path, repo, "child", [("name", "dimension")])
        assert result.success, result.error_message
        child, report = output(tmp_path, "child")
        assert child["schema"] == "shop" and child["table_name"] == "stored"
        assert child["extends"] == ["stored_base"]
        assert "Inherited View Source" in report
        assert not any("using the view name as table" in issue for issue in result.issues)

    def test_inherited_derived_table_is_written_into_the_child(self, tmp_path):
        repo = make_repo(tmp_path, """
            view: summary_base {
              derived_table: { sql: SELECT user_id, COUNT(*) AS n FROM shop.orders GROUP BY 1 ;; }
              dimension: user_id { primary_key: yes }
            }
            view: summary { extends: [summary_base] dimension: n { type: number } }
        """)
        result, _ = migrate(tmp_path, repo, "summary", [("n", "dimension")])
        assert result.success, result.error_message
        view = load_yaml(tmp_path / "out" / "views" / "summary.query.view")
        assert "FROM shop.orders" in view["sql"] and view["extends"] == ["summary_base"]
        assert not any("No primary key" in issue for issue in result.issues)

    def test_model_rules(self):
        base = dict(folder="s/curated", label="L", description="D", tags=["mig_phase_0", "verified"])
        OmniView(template=True, **base)
        OmniView(schema="s", table_name="t", extends=["a", "b"], **base)
        with pytest.raises(ValidationError):
            OmniView(template=True, schema="s", table_name="t", **base)
        with pytest.raises(ValidationError):
            OmniView(**base)
        with pytest.raises(ValidationError):
            OmniView(table_name="t", **base)


# ----------------------------------------------------------------------
# E2. extends on views
# ----------------------------------------------------------------------

CHILD = """
view: orders {
  extends: [base_entity, audit_fields]
  sql_table_name: shop.orders ;;
  dimension: region { type: string sql: ${TABLE}.region ;; }
}
"""


class TestViewExtends:
    def test_extends_written_in_order_and_not_reported(self, tmp_path):
        repo = make_repo(tmp_path, CHILD)
        result, _ = migrate(tmp_path, repo, "orders", [("region", "dimension")])
        assert result.success, result.error_message
        view, report = output(tmp_path, "orders")
        assert view["extends"] == ["base_entity", "audit_fields"]
        text = (tmp_path / "out" / "views" / "orders.view.yaml").read_text()
        assert "extends: [base_entity, audit_fields]" in text
        assert not any("inherited fields are not included" in issue for issue in result.issues)
        assert "View Inheritance (extends)" in report
        # Fields only one parent defines, or that reach the child once, are not copied
        assert set(view["dimensions"]) == {"region", "status"}
        assert "measures" not in view

    def test_conflict_between_parents_is_written_with_the_looker_winner(self, tmp_path):
        repo = make_repo(tmp_path, CHILD)
        result, _ = migrate(tmp_path, repo, "orders", [("region", "dimension"), ("status", "dimension")])
        view, report = output(tmp_path, "orders")
        status = view["dimensions"]["status"]
        # Looker: the last parent wins. Omni would take the first, so it is written out
        assert status["sql"] == "UPPER(status)"
        assert status["label"] == "Audit Status"
        assert "resolved_from_extends" in status["tags"]
        assert "hidden" not in status
        assert "Extends Conflict" in report and "status" in report and "audit_fields" in report
        assert not any("CSV field 'status'" in issue for issue in result.issues)

    def test_conflict_field_outside_the_csv_is_written_hidden(self, tmp_path):
        repo = make_repo(tmp_path, CHILD)
        migrate(tmp_path, repo, "orders", [("region", "dimension")])
        status = output(tmp_path, "orders")[0]["dimensions"]["status"]
        assert status["hidden"] is True
        assert set(status["tags"]) == {"in_omni_in_looker_not_csv", "resolved_from_extends"}
        assert status["sql"] == "UPPER(status)"

    def test_shared_grandparent_and_agreeing_parents_write_nothing(self, tmp_path):
        repo = make_repo(tmp_path, """
            view: left { extends: [base_entity] dimension: left_only { sql: ${TABLE}.l ;; } }
            view: right { extends: [base_entity] dimension: right_only { sql: ${TABLE}.r ;; } }
            view: orders {
              extends: [left, right]
              sql_table_name: shop.orders ;;
              dimension: region { sql: ${TABLE}.region ;; }
            }
        """)
        result, _ = migrate(tmp_path, repo, "orders", [("region", "dimension")])
        view, report = output(tmp_path, "orders")
        assert view["extends"] == ["left", "right"]
        assert set(view["dimensions"]) == {"region"}
        assert "Extends Conflict" not in report
        assert "base_entity" in report  # the whole chain is listed

    def test_child_definition_overrides_all_parents(self, tmp_path):
        repo = make_repo(tmp_path, """
            view: orders {
              extends: [base_entity, audit_fields]
              sql_table_name: shop.orders ;;
              dimension: status { label: "Order Status" }
            }
        """)
        migrate(tmp_path, repo, "orders", [("status", "dimension")])
        view, report = output(tmp_path, "orders")
        status = view["dimensions"]["status"]
        # The child sets only the label; the rest is what Looker merges in (last parent wins)
        assert status["label"] == "Order Status" and status["sql"] == "UPPER(status)"
        assert "resolved_from_extends" not in status["tags"]
        assert "Extends Conflict" not in report

    @pytest.mark.parametrize("child, reason", [
        ("view: orders { extends: [base_entity, missing_parent] sql_table_name: shop.orders ;; "
         "dimension: region {} dimension: uses_status { sql: ${status} ;; } }", "missing_parent"),
        ("view: orders { extends: [loop_a] sql_table_name: shop.orders ;; dimension: region {} "
         "dimension: uses_status { sql: ${status} ;; } }\n"
         "view: loop_a { extends: [loop_b] dimension: status {} }\nview: loop_b { extends: [loop_a] }", "circular"),
    ])
    def test_unresolved_parent_keeps_the_report_item(self, tmp_path, child, reason):
        repo = make_repo(tmp_path, child)
        result, _ = migrate(tmp_path, repo, "orders", [("region", "dimension"), ("uses_status", "dimension")])
        assert result.success, result.error_message
        view, report = output(tmp_path, "orders")
        assert "extends" not in view
        assert any("inherited fields are not included" in issue and reason in issue for issue in result.issues)
        # Nothing is resolved against a chain that could not be read
        assert "uses_status" not in view["dimensions"]
        assert any("No primary key" in issue for issue in result.issues)
        assert "MANUAL ACTION REQUIRED" in report

    def test_inherited_access_restrictions_are_reported(self, tmp_path):
        repo = make_repo(tmp_path, """
            access_grant: can_view_finance { user_attribute: department allowed_values: ["finance"] }
            view: restricted { required_access_grants: [can_view_finance] dimension: secret { sql: ${TABLE}.secret ;; } }
            view: orders { extends: [restricted] sql_table_name: shop.orders ;; dimension: id { primary_key: yes } }
        """)
        result, _ = migrate(tmp_path, repo, "orders", [("id", "dimension")])
        assert any("can_view_finance" in issue and "restricted" in issue for issue in result.issues)

    def test_parent_refinement_is_reported(self, tmp_path):
        repo = make_repo(tmp_path, CHILD, "view: +audit_fields { dimension: extra {} }")
        result, _ = migrate(tmp_path, repo, "orders", [("region", "dimension")])
        assert any("audit_fields" in issue and "refine" in issue.lower() for issue in result.issues)

    def test_merge_into_existing_view_writes_extends_and_template(self, tmp_path):
        gen = FileGenerator(tmp_path)
        tags = ["mig_phase_0", "verified"]
        child = OmniView(schema="shop", table_name="orders", folder="shop/curated", label="Orders",
                         description="d", tags=tags, extends=["base_entity"])
        text, info = gen.render_view_yaml(
            child, existing_view={"schema": "shop", "table_name": "orders", "extends": ["old_parent"],
                                  "dimensions": {"kept": {}}},
            csv_fields=set(), field_to_column_mapping={})
        assert info["final"]["extends"] == ["base_entity"]
        assert any("extends" in note for note in info["notes"])
        template = OmniView(folder="shop/curated", label="Base", description="d", tags=tags, template=True)
        _, info = gen.render_view_yaml(
            template, existing_view={"schema": "shop", "table_name": "base", "dimensions": {"kept": {}}},
            csv_fields=set(), field_to_column_mapping={})
        final = info["final"]
        assert final["template"] is True and "table_name" not in final and "schema" not in final
        assert "kept" in final["dimensions"]


# ----------------------------------------------------------------------
# E3. References to inherited fields
# ----------------------------------------------------------------------

REFERENCING_CHILD = """
view: orders {
  extends: [base_entity]
  sql_table_name: shop.orders ;;
  dimension: status_upper { type: string sql: UPPER(${status}) ;; }
  dimension: tier_label { type: string sql: CONCAT(${price_tier}, ' tier') ;; }
  dimension: created_day { type: string sql: ${created_date} ;; }
  dimension: created_text { type: string sql: CAST(${created} AS VARCHAR) ;; }
  dimension: undefined_ref { type: string sql: ${nowhere} ;; }
  measure: total_amount { type: sum sql: ${amount} ;; }
  measure: complete_count { type: count filters: [status: "complete"] }
  measure: big_count { type: count filters: [amount: ">100"] }
  measure: count_twice { type: number sql: ${count} * 2 ;; }
}
"""
REFERENCING_ROWS = [
    ("status_upper", "dimension"), ("tier_label", "dimension"), ("created_day", "dimension"),
    ("created_text", "dimension"), ("undefined_ref", "dimension"), ("total_amount", "measure"),
    ("complete_count", "measure"), ("big_count", "measure"), ("count_twice", "measure"),
]


class TestInheritedReferences:
    @pytest.fixture
    def migrated(self, tmp_path):
        repo = make_repo(tmp_path, REFERENCING_CHILD)
        result, _ = migrate(tmp_path, repo, "orders", REFERENCING_ROWS)
        assert result.success, result.error_message
        view, report = output(tmp_path, "orders")
        return result, view, report

    def test_reference_to_inherited_field_is_valid(self, migrated):
        _, view, _ = migrated
        assert view["dimensions"]["status_upper"]["sql"] == "UPPER(${status})"
        assert view["dimensions"]["created_text"]["sql"] == "CAST(${created} AS VARCHAR)"
        assert view["measures"]["total_amount"]["sql"] == "${amount}"
        assert view["measures"]["count_twice"]["sql"] == "${count} * 2"

    def test_inherited_dependency_is_not_copied_into_the_child(self, migrated):
        _, view, report = migrated
        for inherited in ("status", "amount", "id", "created", "price_tier"):
            assert inherited not in view["dimensions"], inherited
        assert "count" not in view["measures"]
        assert "auto_dependency" not in str(view)
        assert "Auto-included" not in report

    def test_filters_on_inherited_fields_use_their_type(self, migrated):
        _, view, _ = migrated
        assert view["measures"]["complete_count"]["filters"] == {"status": {"is": "complete"}}
        assert view["measures"]["big_count"]["filters"] == {"amount": {"greater_than": 100}}

    def test_unconvertible_inherited_field_blocks_the_dependent(self, migrated):
        result, view, _ = migrated
        assert "tier_label" not in view["dimensions"]
        issue = next(i for i in result.issues if i.startswith("tier_label"))
        assert "price_tier" in issue and "base_entity" in issue and "MANUAL ACTION REQUIRED" in issue

    def test_timeframe_rule_is_unchanged_for_an_inherited_group(self, migrated):
        result, view, _ = migrated
        assert "created_day" not in view["dimensions"]
        issue = next(i for i in result.issues if i.startswith("created_day"))
        assert "created_date" in issue and "must be rewritten for Omni" in issue

    def test_undefined_reference_still_blocks(self, migrated):
        result, view, _ = migrated
        assert "undefined_ref" not in view["dimensions"]
        assert any(i.startswith("undefined_ref") and "not defined in the view" in i for i in result.issues)

    def test_reference_through_a_grandparent_and_a_blocked_parent_field(self, tmp_path):
        repo = make_repo(tmp_path, """
            view: middle {
              extends: [base_entity]
              dimension: tier_text { sql: CONCAT(${price_tier}, 'x') ;; }
              dimension: status_lower { sql: LOWER(${status}) ;; }
            }
            view: orders {
              extends: [middle]
              sql_table_name: shop.orders ;;
              dimension: uses_grandparent { sql: ${amount} + 1 ;; }
              dimension: uses_parent { sql: ${status_lower} ;; }
              dimension: uses_blocked { sql: ${tier_text} ;; }
            }
        """)
        rows = [("uses_grandparent", "dimension"), ("uses_parent", "dimension"), ("uses_blocked", "dimension")]
        result, _ = migrate(tmp_path, repo, "orders", rows)
        view, _ = output(tmp_path, "orders")
        assert set(view["dimensions"]) == {"uses_grandparent", "uses_parent"}
        issue = next(i for i in result.issues if i.startswith("uses_blocked"))
        assert "tier_text" in issue and "middle" in issue

    def test_csv_row_for_an_inherited_field_is_not_reported_missing(self, tmp_path):
        repo = make_repo(tmp_path, REFERENCING_CHILD)
        result, _ = migrate(tmp_path, repo, "orders",
                            [("status_upper", "dimension"), ("status", "dimension"), ("price_tier", "dimension"),
                             ("nowhere", "dimension")])
        view, report = output(tmp_path, "orders")
        assert "status" not in view["dimensions"]
        not_found = [i for i in result.issues if "not found in Looker view" in i]
        assert len(not_found) == 1 and "'nowhere'" in not_found[0]
        assert "status" not in report.split("CSV Fields Not Found in LookML")[1].split("\n\n")[0]
        inherited = next(i for i in result.issues if "inherited" in i and "status" in i)
        assert "base_entity" in inherited
        assert "CSV Fields Inherited" in report
        # An inherited field its parent cannot migrate is called out
        assert any("price_tier" in i and "not migrated" in i for i in result.issues)

    def test_failed_override_warns_that_the_parent_definition_shows(self, tmp_path):
        repo = make_repo(tmp_path, """
            view: orders {
              extends: [base_entity]
              sql_table_name: shop.orders ;;
              dimension: status { sql: {% if x %} a {% else %} b {% endif %} ;; }
            }
        """)
        result, _ = migrate(tmp_path, repo, "orders", [("status", "dimension")])
        view, _ = output(tmp_path, "orders")
        assert "status" not in (view.get("dimensions") or {})
        issue = next(i for i in result.issues if i.startswith("status"))
        assert "base_entity" in issue and "inherit" in issue

    def test_always_filter_on_an_inherited_field_translates(self, tmp_path):
        repo = make_repo(tmp_path, """
            view: orders { extends: [base_entity] sql_table_name: shop.orders ;; }
            explore: orders {
              always_filter: { filters: [orders.status: "complete", orders.created_date: "7 days ago for 7 days"] }
            }
        """)
        orch = MigrationOrchestrator(skill_path=SKILL_ROOT, looker_repo=repo, omni_repo=tmp_path / "out" / "topics")
        result = orch.migrate_explore("orders")
        assert result.success, result.error_message
        topic = load_yaml(tmp_path / "out" / "topics" / "orders.topic.yaml")
        assert topic["default_filters"] == {
            "orders.status": {"is": "complete", "required": True},
            "orders.created": {"time_for_duration": ["7 complete days ago", "7 days"], "required": True},
        }
        assert not any("NOT carried" in issue for issue in result.issues)


# ----------------------------------------------------------------------
# E4. Inherited primary key
# ----------------------------------------------------------------------

class TestInheritedPrimaryKey:
    def test_no_warning_when_a_parent_defines_the_key(self, tmp_path):
        repo = make_repo(tmp_path, """
            view: middle { extends: [base_entity] }
            view: orders { extends: [middle] sql_table_name: shop.orders ;; dimension: region {} }
        """)
        result, _ = migrate(tmp_path, repo, "orders", [("region", "dimension")])
        _, report = output(tmp_path, "orders")
        assert not any("No primary key" in issue for issue in result.issues)
        assert "Missing Primary Key" not in report

    def test_warning_when_no_parent_defines_a_key(self, tmp_path):
        repo = make_repo(tmp_path, "view: orders { extends: [audit_fields] sql_table_name: shop.orders ;; "
                                   "dimension: region {} }")
        result, _ = migrate(tmp_path, repo, "orders", [("region", "dimension")])
        assert any("No primary key" in issue for issue in result.issues)

    def test_warning_when_the_inherited_key_does_not_convert(self, tmp_path):
        repo = make_repo(tmp_path, """
            view: keyed { dimension: id { primary_key: yes sql: {{ liquid_key }} ;; } }
            view: orders { extends: [keyed] sql_table_name: shop.orders ;; dimension: region {} }
        """)
        result, _ = migrate(tmp_path, repo, "orders", [("region", "dimension")])
        assert any("No primary key" in issue for issue in result.issues)


# ----------------------------------------------------------------------
# Scope CSV and views with no fields of their own
# ----------------------------------------------------------------------

class TestEmptyScope:
    def test_view_with_no_fields_of_its_own_converts_without_csv_rows(self, tmp_path):
        repo = make_repo(tmp_path, "view: orders { extends: [base_entity] sql_table_name: shop.orders ;; }")
        result, _ = migrate(tmp_path, repo, "orders")
        assert result.success, result.error_message
        view, report = output(tmp_path, "orders")
        assert view["schema"] == "shop" and view["table_name"] == "orders"
        assert view["extends"] == ["base_entity"]
        assert "dimensions" not in view and "measures" not in view
        assert result.issues == []
        assert "orders" in report

    def test_view_with_fields_and_no_csv_rows_still_fails(self, tmp_path):
        repo = make_repo(tmp_path, "view: orders { extends: [base_entity] sql_table_name: shop.orders ;; "
                                   "dimension: region {} }")
        result, _ = migrate(tmp_path, repo, "orders")
        assert not result.success and "No CSV rows found" in result.error_message

    def test_missing_view_without_csv_rows_reports_the_csv(self, tmp_path):
        result, _ = migrate(tmp_path, make_repo(tmp_path), "does_not_exist")
        assert not result.success and "No CSV rows found" in result.error_message

    def test_template_without_csv_rows_converts_hidden_and_reported(self, tmp_path):
        repo = make_repo(tmp_path, "view: orders { extends: [base_entity] sql_table_name: shop.orders ;; }")
        result, _ = migrate(tmp_path, repo, "base_entity")
        assert result.success, result.error_message
        view, _ = output(tmp_path, "base_entity")
        assert view["template"] is True
        assert all(d["hidden"] is True for d in view["dimensions"].values())
        assert any("No CSV rows" in issue and "hidden" in issue for issue in result.issues)


# ----------------------------------------------------------------------
# Performance: the repository is parsed once per run
# ----------------------------------------------------------------------

class TestParsedOnce:
    def test_views_are_loaded_once_per_orchestrator(self, tmp_path, monkeypatch):
        repo = make_repo(tmp_path, REFERENCING_CHILD,
                         "view: other { sql_table_name: shop.other ;; dimension: id { primary_key: yes } "
                         "measure: n { type: count filters: [orders.status: \"complete\"] } }")
        calls = []
        real = orchestrator_module.load_views

        def counting(looker_repo):
            calls.append(looker_repo)
            return real(looker_repo)

        monkeypatch.setattr(orchestrator_module, "load_views", counting)
        orch = MigrationOrchestrator(
            skill_path=SKILL_ROOT, looker_repo=repo, omni_repo=tmp_path / "out" / "views",
            csv_path=csv(tmp_path, [("0", "orders", f, k) for f, k in REFERENCING_ROWS]
                         + [("0", "other", "n", "measure")]))
        assert orch.migrate_view("orders", 0, "Team A").success
        assert orch.migrate_view("other", 0, "Team A").success
        assert len(calls) == 1
        # A filter on an inherited field of another view uses that field's type
        other = load_yaml(tmp_path / "out" / "views" / "other.view.yaml")
        assert other["measures"]["n"]["filters"] == {"orders.status": {"is": "complete"}}
