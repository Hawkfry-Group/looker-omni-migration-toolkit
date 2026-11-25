"""
Explore-side rules: always_filter as a required default filter, filter
expression translation, topic-scoped relationships, explore `extends` and
template topics. Each test builds a small LookML project in tmp_path.
"""

import textwrap
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from conftest import FIXTURE_REPO, SKILL_ROOT, load_yaml, run_cli
from lib.explore_analyzer import (
    find_extending_explores,
    load_explores,
    parse_explore_file,
    resolve_explore_extends,
)
from lib.file_generators import FileGenerator
from lib.omni_models import OmniTopic
from lib.orchestrator import MigrationOrchestrator
from lib.rule_applier import translate_date_filter_expression

VIEWS = """
view: orders {
  sql_table_name: shop.orders ;;
  dimension: id { primary_key: yes type: number sql: ${TABLE}.id ;; }
  dimension: status { type: string sql: ${TABLE}.status ;; }
  dimension: region { sql: ${TABLE}.region ;; }
  dimension: priority { type: number sql: ${TABLE}.priority ;; }
  dimension: is_gift { type: yesno sql: ${TABLE}.is_gift ;; }
  dimension: delivered_on { type: date sql: ${TABLE}.delivered_on ;; }
  dimension_group: created {
    type: time
    timeframes: [raw, date, week, month]
    sql: ${TABLE}.created_at ;;
  }
  dimension_group: shipped {
    type: time
    timeframes: [week, month]
    sql: ${TABLE}.shipped_at ;;
  }
  dimension_group: transit {
    type: duration
    intervals: [day]
    sql_start: ${TABLE}.shipped_at ;;
    sql_end: ${TABLE}.delivered_at ;;
  }
  measure: count { type: count }
}

view: users {
  sql_table_name: shop.users ;;
  dimension: id { primary_key: yes type: number sql: ${TABLE}.id ;; }
  dimension: country { type: string sql: ${TABLE}.country ;; }
  dimension_group: created { type: time sql: ${TABLE}.created_at ;; }
}

view: products {
  sql_table_name: shop.products ;;
  dimension: id { primary_key: yes type: number sql: ${TABLE}.id ;; }
  dimension: category { type: string sql: ${TABLE}.category ;; }
}

view: brands {
  sql_table_name: shop.brands ;;
  dimension: id { primary_key: yes type: number sql: ${TABLE}.id ;; }
}
"""

USERS_JOIN = """
  join: users {
    type: left_outer
    relationship: many_to_one
    sql_on: ${orders.user_id} = ${users.id} ;;
  }
"""

USERS_REL = {
    "join_from_view": "orders",
    "join_to_view": "users",
    "join_type": "always_left",
    "on_sql": "${orders.user_id} = ${users.id}",
    "relationship_type": "many_to_one",
    "reversible": False,
}


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text))
    return path


def make_repo(tmp_path, explores: str, extra_files=None) -> Path:
    repo = tmp_path / "repo"
    write(repo / "views.lkml", VIEWS)
    write(repo / "shop.model.lkml", explores)
    for name, text in (extra_files or {}).items():
        write(repo / name, text)
    return repo


def migrate(tmp_path, repo, name="orders", **kwargs):
    topics = tmp_path / "out" / "topics"
    orch = MigrationOrchestrator(skill_path=SKILL_ROOT, looker_repo=repo, omni_repo=topics)
    result = orch.migrate_explore(name, **kwargs)
    assert result.success, result.error_message
    topic = load_yaml(topics / f"{name}.topic.yaml")
    report = (tmp_path / "out" / "reports" / f"{name}_explore.md").read_text()
    return result, topic, report


def item(report: str, title: str) -> str:
    """Body of one `### title` item of the report"""
    return report.split(f"### {title}")[1].split("\n#")[0]


def always_filter_explore(filters: str, joins: str = USERS_JOIN) -> str:
    return "explore: orders {\n  always_filter: { filters: [" + filters + "] }\n" + joins + "}\n"


# ----------------------------------------------------------------------
# A1 / A2: always_filter becomes a required default filter
# ----------------------------------------------------------------------

class TestRequiredDefaultFilters:
    @pytest.mark.parametrize("expression,expected", [
        ("7 days ago for 7 days", ["7 complete days ago", "7 days"]),
        ("2 weeks ago for 2 weeks", ["2 complete weeks ago", "2 weeks"]),
        ("3 months ago for 3 months", ["3 complete months ago", "3 months"]),
        ("  14 Days Ago  For 14 days ", ["14 complete days ago", "14 days"]),
    ])
    def test_duration_on_timeframe_field(self, tmp_path, expression, expected):
        repo = make_repo(tmp_path, always_filter_explore(f'orders.created_date: "{expression}"'))
        result, topic, report = migrate(tmp_path, repo)
        # The key is the dimension group's Omni name, not the Looker timeframe field
        assert topic["default_filters"] == {
            "orders.created": {"time_for_duration": expected, "required": True}
        }
        assert "always_filter (carried)" in report
        assert "always_filter (NOT carried)" not in report
        assert "`default_filters`" in report.split("## Carried Into the Topic")[1]
        assert any(i.startswith("always_filter (carried)") for i in result.issues)

    def test_duration_on_date_dimension(self, tmp_path):
        repo = make_repo(tmp_path, always_filter_explore('orders.delivered_on: "7 days ago for 7 days"'))
        _, topic, _ = migrate(tmp_path, repo)
        assert topic["default_filters"] == {
            "orders.delivered_on": {"time_for_duration": ["7 complete days ago", "7 days"], "required": True}
        }

    @pytest.mark.parametrize("field,expression,expected", [
        ("orders.status", "global", {"is": "global"}),
        ("orders.region", "emea", {"is": "emea"}),  # no type: string by default
        ("orders.status", "complete,shipped", {"is": ["complete", "shipped"]}),
        ("orders.status", "5", {"is": "5"}),  # stays a string on a string field
        ("orders.priority", "5", {"is": 5}),
        ("users.country", "Portugal", {"is": "Portugal"}),
    ])
    def test_string_and_number_values(self, tmp_path, field, expression, expected):
        repo = make_repo(tmp_path, always_filter_explore(f'{field}: "{expression}"'))
        _, topic, _ = migrate(tmp_path, repo)
        assert topic["default_filters"] == {field: {**expected, "required": True}}

    def test_several_filters_and_yaml_shape(self, tmp_path):
        repo = make_repo(tmp_path, always_filter_explore(
            'orders.created_date: "7 days ago for 7 days", orders.status: "complete,shipped"'))
        _, topic, _ = migrate(tmp_path, repo)
        assert list(topic["default_filters"]) == ["orders.created", "orders.status"]
        text = (tmp_path / "out" / "topics" / "orders.topic.yaml").read_text()
        assert "    time_for_duration: [7 complete days ago, 7 days]\n    required: true\n" in text
        assert "    is: [complete, shipped]\n    required: true\n" in text

    def test_aliased_join_uses_the_alias(self, tmp_path):
        repo = make_repo(tmp_path, always_filter_explore('buyer.country: "Portugal"', """
          join: buyer {
            from: users
            relationship: many_to_one
            sql_on: ${orders.buyer_id} = ${buyer.id} ;;
          }
        """))
        _, topic, _ = migrate(tmp_path, repo)
        assert topic["default_filters"] == {"buyer.country": {"is": "Portugal", "required": True}}

    def test_aliased_base_view_keeps_the_alias(self, tmp_path):
        # F1/J3a: the key keeps the base alias, which the topic declares in `views:`
        repo = make_repo(tmp_path, """
            explore: customers {
              from: users
              always_filter: { filters: [customers.country: "Portugal", customers.created_date: "7 days ago for 7 days"] }
            }
        """)
        _, topic, _ = migrate(tmp_path, repo, "customers")
        assert topic["default_filters"] == {
            "customers.country": {"is": "Portugal", "required": True},
            "customers.created": {"time_for_duration": ["7 complete days ago", "7 days"], "required": True},
        }
        assert topic["base_view"] == "customers"
        assert topic["views"] == {"customers": {"extends": ["users"]}}

    def test_legacy_block_syntax(self, tmp_path):
        repo = make_repo(tmp_path, """
            explore: orders {
              always_filter: {
                filters: { field: orders.status value: "complete" }
              }
            }
        """)
        _, topic, _ = migrate(tmp_path, repo)
        assert topic["default_filters"] == {"orders.status": {"is": "complete", "required": True}}


# ----------------------------------------------------------------------
# A4: anything else is reported, never written
# ----------------------------------------------------------------------

class TestUntranslatableAlwaysFilter:
    @pytest.mark.parametrize("field,expression,reason", [
        ("orders.created_date", "7 days", "includes today"),
        ("orders.created_date", "90 days", "includes today"),
        ("orders.created_date", "7 days ago for 3 days", "outside the forms"),
        ("orders.created_date", "7 days ago for 1 weeks", "outside the forms"),
        ("orders.created_date", "2 years ago for 2 years", "outside the forms"),
        ("orders.created_date", "next month", "outside the forms"),
        ("orders.created_date", "last 2 months", "outside the forms"),
        ("orders.created_date", "0 days ago for 0 days", "outside the forms"),
        ("orders.created_week", "last month", "timeframe"),
        ("orders.created_date", "2024-01-01", "outside the forms"),
        ("orders.created_week", "2 weeks ago for 2 weeks", "timeframe"),
        ("orders.shipped_date", "7 days ago for 7 days", "timeframe"),
        ("orders.transit_days", "5", "not defined"),
        ("orders.status", "", "no value"),
        ("orders.status", "-cancelled", "outside the forms"),
        ("orders.status", "%pending%", "outside the forms"),
        ("orders.status", "NULL", "outside the forms"),
        ("orders.status", "7 days ago for 7 days", "outside the forms"),
        ("orders.priority", ">5", "outside the forms"),
        ("orders.priority", "1,2", "outside the forms"),
        ("orders.priority", "high", "outside the forms"),
        ("orders.is_gift", "yes", "type `yesno`"),
        ("orders.count", ">0", "measure"),
        ("orders.nope", "x", "not defined"),
        ("status", "complete", "view-qualified"),
        ("products.category", "Jeans", "not part of the explore"),
        ("missing_view.category", "Jeans", "not part of the explore"),
    ])
    def test_not_written_and_reported(self, tmp_path, field, expression, reason):
        repo = make_repo(tmp_path, always_filter_explore(f'{field}: "{expression}"'))
        result, topic, report = migrate(tmp_path, repo)
        assert "default_filters" not in topic
        section = item(report, "always_filter (NOT carried)")
        assert f'{field}: "{expression}"' in section  # the Looker expression is quoted
        assert reason in section, section
        assert "always_filter (carried)" not in report
        assert any(i.startswith("always_filter (NOT carried)") for i in result.issues)

    def test_filter_on_skipped_join_not_written(self, tmp_path):
        repo = make_repo(tmp_path, always_filter_explore('users.country: "Portugal"', """
          join: users {
            type: right_outer
            sql_on: ${orders.user_id} = ${users.id} ;;
          }
        """))
        _, topic, report = migrate(tmp_path, repo)
        assert "default_filters" not in topic
        assert "not migrated" in item(report, "always_filter (NOT carried)")

    def test_unknown_view_file_not_written(self, tmp_path):
        repo = make_repo(tmp_path, """
            explore: orders {
              always_filter: { filters: [ghost.name: "x"] }
              join: ghost { sql_on: ${orders.id} = ${ghost.id} ;; }
            }
        """)
        _, topic, report = migrate(tmp_path, repo)
        assert "default_filters" not in topic
        assert "not found" in item(report, "always_filter (NOT carried)")

    def test_translated_and_untranslated_together(self, tmp_path):
        repo = make_repo(tmp_path, always_filter_explore(
            'orders.status: "complete", orders.created_date: "7 days"'))
        _, topic, report = migrate(tmp_path, repo)
        assert topic["default_filters"] == {"orders.status": {"is": "complete", "required": True}}
        assert "always_filter (carried)" in report
        section = item(report, "always_filter (NOT carried)")
        assert 'orders.created_date: "7 days"' in section
        assert "orders.status" not in section

    def test_two_looker_fields_for_one_omni_field(self, tmp_path):
        repo = make_repo(tmp_path, always_filter_explore(
            'orders.created_date: "7 days ago for 7 days", orders.created_raw: "2 weeks ago for 2 weeks"'))
        _, topic, report = migrate(tmp_path, repo)
        assert topic["default_filters"] == {
            "orders.created": {"time_for_duration": ["7 complete days ago", "7 days"], "required": True}
        }
        assert "already has a filter" in report

    def test_conditionally_filter_still_reported_only(self, tmp_path):
        repo = make_repo(tmp_path, """
            explore: orders {
              conditionally_filter: {
                filters: [orders.status: "complete"]
                unless: [orders.id]
              }
            }
        """)
        _, topic, report = migrate(tmp_path, repo)
        assert "default_filters" not in topic
        assert "conditionally_filter (NOT carried)" in report

    @pytest.mark.parametrize("expression", ["7 days", "next month", "last 2 months", "", None, 7, "7 days ago", "0 days ago for 0 days"])
    def test_translate_date_filter_expression_rejects(self, expression):
        condition, reason = translate_date_filter_expression(expression)
        assert condition is None and reason


class TestTopicModel:
    def base(self, **kw):
        return OmniTopic(base_view="orders", label="Orders", description="d", fields=["all_views.*"],
                         ai_context="c", **kw)

    def test_required_default_filter_needs_a_value(self):
        with pytest.raises(ValidationError):
            self.base(default_filters={"orders.status": {"required": True}})
        with pytest.raises(ValidationError):
            self.base(default_filters={"orders.status": {"is": None, "required": True}})
        assert self.base(default_filters={"orders.status": {"is": "x", "required": True}})

    def test_key_order(self, tmp_path):
        topic = self.base(extends=["base"], template=True, relationships=[dict(USERS_REL)],
                          joins={"users": {}}, default_filters={"orders.status": {"is": "x", "required": True}})
        text = FileGenerator(output_dir=tmp_path).render_topic_yaml(topic)
        keys = list(yaml.safe_load(text))
        assert keys[:3] == ["base_view", "extends", "template"]
        assert keys.index("relationships") < keys.index("joins")
        assert "extends: [base]" in text
        assert "template: true" in text


# ----------------------------------------------------------------------
# J1: relationships live in the topic file
# ----------------------------------------------------------------------

class TestTopicScopedRelationships:
    def test_relationships_written_in_topic(self, tmp_path):
        repo = make_repo(tmp_path, "explore: orders {\n" + USERS_JOIN + "}\n")
        result, topic, report = migrate(tmp_path, repo)
        assert topic["relationships"] == [USERS_REL]
        assert topic["joins"] == {"users": {}}
        assert not (tmp_path / "out" / "topics" / "orders.relationships").exists()
        assert result.relationships_file_path is None
        assert [p.name for p in result.planned_files] == ["orders.topic.yaml", "orders_explore.md"]
        assert "orders.relationships" not in report
        assert "topic-scoped" in report

    def test_aliases_are_extended_views_of_the_topic(self, tmp_path):
        repo = make_repo(tmp_path, """
            explore: orders {
              join: buyer {
                from: users
                relationship: many_to_one
                sql_on: ${orders.buyer_id} = ${buyer.id} ;;
              }
              join: products {
                relationship: one_to_many
                sql_on: ${buyer.favourite_product_id} = ${products.id} ;;
              }
            }
        """)
        _, topic, _ = migrate(tmp_path, repo)
        buyer, products = topic["relationships"]
        assert buyer["join_to_view"] == "buyer" and "join_to_view_as" not in buyer
        assert products["join_from_view"] == "buyer" and "join_from_view_as" not in products
        assert topic["views"]["buyer"] == {"extends": ["users"]}
        assert topic["joins"] == {"buyer": {"products": {}}}

    def test_no_joins_no_relationships_key(self, tmp_path):
        repo = make_repo(tmp_path, "explore: orders {}\n")
        _, topic, _ = migrate(tmp_path, repo)
        assert "relationships" not in topic
        assert topic["joins"] == {}

    def test_multiline_on_sql_round_trips(self, tmp_path):
        repo = make_repo(tmp_path, """
            explore: orders {
              join: users {
                relationship: many_to_one
                sql_on: ${orders.user_id} = ${users.id}
                  AND ${users.country} IS NOT NULL ;;
              }
            }
        """)
        _, topic, _ = migrate(tmp_path, repo)
        on_sql = topic["relationships"][0]["on_sql"]
        assert "${orders.user_id} = ${users.id}" in on_sql and "AND ${users.country} IS NOT NULL" in on_sql

    def test_stale_relationships_file_from_earlier_run_removed(self, tmp_path):
        repo = make_repo(tmp_path, "explore: orders {\n" + USERS_JOIN + "}\n")
        stale = write(tmp_path / "out" / "topics" / "orders.relationships", "- join_from_view: orders\n")
        migrate(tmp_path, repo)
        assert not stale.exists()

    def test_no_cross_file_conflict_check(self, tmp_path):
        repo = make_repo(tmp_path, "explore: orders {\n" + USERS_JOIN + "}\n")
        write(tmp_path / "out" / "topics" / "other.relationships", yaml.safe_dump([
            {**USERS_REL, "on_sql": "${orders.buyer_id} = ${users.id}"}
        ]))
        result, _, report = migrate(tmp_path, repo)
        assert "Conflicting relationship" not in report
        assert not any("Conflicting" in i for i in result.issues)

    def test_relationships_file_flag_keeps_old_behaviour(self, tmp_path):
        repo = make_repo(tmp_path, "explore: orders {\n" + USERS_JOIN + "}\n")
        write(tmp_path / "out" / "topics" / "other.relationships", yaml.safe_dump([
            {**USERS_REL, "on_sql": "${orders.buyer_id} = ${users.id}"}
        ]))
        result, topic, report = migrate(tmp_path, repo, relationships_file=True)
        assert "relationships" not in topic
        assert topic["joins"] == {"users": {}}
        path = tmp_path / "out" / "topics" / "orders.relationships"
        assert load_yaml(path) == [USERS_REL]
        assert result.relationships_file_path == path
        assert "orders.relationships" in report
        assert "Conflicting relationship" in report

    def test_cli_default_and_flag(self, tmp_path):
        topics = tmp_path / "migration" / "staging" / "topics"
        result = run_cli("--explore", "events", "--looker-repo", FIXTURE_REPO, cwd=tmp_path)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "Relationships:" not in result.stdout
        assert len(load_yaml(topics / "events.topic.yaml")["relationships"]) == 1
        assert not (topics / "events.relationships").exists()

        result = run_cli("--explore", "events", "--looker-repo", FIXTURE_REPO, "--relationships-file", cwd=tmp_path)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "Relationships:" in result.stdout
        assert "relationships" not in load_yaml(topics / "events.topic.yaml")
        assert len(load_yaml(topics / "events.relationships")) == 1

    @pytest.mark.parametrize("flag", ["--relationships-file", "--template"])
    def test_explore_flags_need_explore_mode(self, tmp_path, flag):
        result = run_cli("--view", "users", "--phase", "0", "--owner", "Team A", flag,
                         "--looker-repo", FIXTURE_REPO, cwd=tmp_path)
        assert result.returncode == 2

    def test_help_lists_flags(self, tmp_path):
        result = run_cli("--help", cwd=tmp_path)
        assert "--relationships-file" in result.stdout and "--template" in result.stdout


# ----------------------------------------------------------------------
# E5: explore extends
# ----------------------------------------------------------------------

BASE_EXPLORE = """
access_grant: can_view_finance { user_attribute: department allowed_values: ["finance"] }

explore: orders_base {
  extension: required
  view_name: orders
  label: "Base Orders"
  group_label: "Shop"
  hidden: yes
  required_access_grants: [can_view_finance]
  sql_always_where: ${orders.status} != 'cancelled' ;;
  sql_always_having: ${orders.count} > 0 ;;
  access_filter: {
    field: users.country
    user_attribute: allowed_country
  }
  always_filter: { filters: [orders.status: "complete"] }

  join: users {
    type: left_outer
    relationship: many_to_one
    sql_on: ${orders.user_id} = ${users.id} ;;
  }
  join: products {
    type: inner
    relationship: many_to_one
    sql_on: ${orders.product_id} = ${products.id} ;;
  }
}
"""

PRODUCTS_REL = {
    "join_from_view": "orders",
    "join_to_view": "products",
    "join_type": "inner",
    "on_sql": "${orders.product_id} = ${products.id}",
    "relationship_type": "many_to_one",
    "reversible": False,
}


class TestExploreExtends:
    def repo(self, tmp_path, child: str, base: str = BASE_EXPLORE):
        # The parent lives in a different file from the child
        return make_repo(tmp_path, child, {"base/orders_base.explore.lkml": base})

    def test_parent_joins_and_settings_merged(self, tmp_path):
        repo = self.repo(tmp_path, """
            explore: sales {
              extends: [orders_base]
              join: brands {
                relationship: many_to_one
                sql_on: ${products.brand_id} = ${brands.id} ;;
              }
            }
        """)
        result, topic, report = migrate(tmp_path, repo, "sales")
        assert topic["extends"] == ["orders_base"]
        assert topic["base_view"] == "orders"
        assert "template" not in topic
        # The child carries the complete join list (Omni replaces, Looker adds)
        assert topic["joins"] == {"users": {}, "products": {"brands": {}}}
        assert topic["relationships"][:2] == [USERS_REL, PRODUCTS_REL]
        assert topic["relationships"][2]["join_to_view"] == "brands"
        assert topic["relationships"][2]["join_from_view"] == "products"
        # Inherited settings
        assert topic["label"] == "Base Orders"
        assert topic["group_label"] == "Shop"
        assert topic["hidden"] is True
        assert topic["required_access_grants"] == ["can_view_finance"]
        assert topic["always_where_sql"] == "${orders.status} != 'cancelled'"
        assert topic["always_having_sql"] == "${orders.count} > 0"
        assert topic["access_filters"] == [{"field": "users.country", "user_attribute": "allowed_country"}]
        assert topic["default_filters"] == {"orders.status": {"is": "complete", "required": True}}
        assert "inherited joins and settings were not merged" not in report
        section = report.split("## Inheritance (extends)")[1].split("\n## ")[0]
        assert "orders_base" in section
        for inherited in ("users", "products", "sql_always_where", "always_filter", "access_filter", "group_label"):
            assert inherited in section, inherited
        assert "department" in report  # inherited grant resolved from the registry
        assert any("orders_base" in i for i in result.issues)  # the parent topic must exist

    def test_child_overrides_parent(self, tmp_path):
        repo = self.repo(tmp_path, """
            explore: sales {
              extends: [orders_base]
              label: "Sales"
              group_label: "Revenue"
              hidden: no
              sql_always_where: ${orders.status} = 'complete' ;;
              always_filter: { filters: [orders.region: "emea"] }
              join: products {
                type: left_outer
                relationship: one_to_one
                sql_on: ${orders.main_product_id} = ${products.id} ;;
              }
            }
        """)
        _, topic, report = migrate(tmp_path, repo, "sales")
        assert topic["label"] == "Sales"
        assert topic["group_label"] == "Revenue"
        assert "hidden" not in topic
        assert topic["always_where_sql"] == "${orders.status} = 'complete'"
        assert topic["always_having_sql"] == "${orders.count} > 0"
        assert topic["default_filters"] == {"orders.region": {"is": "emea", "required": True}}
        assert topic["joins"] == {"users": {}, "products": {}}
        products = topic["relationships"][1]
        assert products == {**PRODUCTS_REL, "join_type": "always_left", "relationship_type": "one_to_one",
                            "on_sql": "${orders.main_product_id} = ${products.id}"}
        assert "overrides" in report.split("## Inheritance (extends)")[1].split("\n## ")[0]

    def test_partial_join_override_merges_parameters_and_is_flagged(self, tmp_path):
        repo = self.repo(tmp_path, """
            explore: sales {
              extends: [orders_base]
              join: products {
                sql_on: ${orders.main_product_id} = ${products.id} ;;
              }
            }
        """)
        result, topic, report = migrate(tmp_path, repo, "sales")
        products = topic["relationships"][1]
        assert products == {**PRODUCTS_REL, "on_sql": "${orders.main_product_id} = ${products.id}"}
        assert "Join products: partial override of an inherited join" in report
        assert any("partial override" in i for i in result.issues)

    def test_later_parent_wins_and_chain_followed(self, tmp_path):
        repo = make_repo(tmp_path, """
            explore: root {
              extension: required
              view_name: orders
              group_label: "Root"
              join: brands { relationship: many_to_one sql_on: ${orders.brand_id} = ${brands.id} ;; }
            }
            explore: first {
              extends: [root]
              extension: required
              label: "First"
              sql_always_where: 1 = 1 ;;
              join: users { relationship: many_to_one sql_on: ${orders.user_id} = ${users.id} ;; }
            }
            explore: second {
              extension: required
              view_name: orders
              label: "Second"
              join: users { type: inner relationship: one_to_one sql_on: ${orders.buyer_id} = ${users.id} ;; }
              join: products { relationship: many_to_one sql_on: ${orders.product_id} = ${products.id} ;; }
            }
            explore: sales { extends: [first, second] }
        """)
        _, topic, report = migrate(tmp_path, repo, "sales")
        assert topic["extends"] == ["first", "second"]
        assert topic["base_view"] == "orders"
        assert topic["label"] == "Second"  # later parent wins
        assert topic["group_label"] == "Root"  # through the chain
        assert topic["always_where_sql"] == "1 = 1"
        assert list(topic["joins"]) == ["brands", "users", "products"]
        users = next(r for r in topic["relationships"] if r["join_to_view"] == "users")
        assert users["on_sql"] == "${orders.buyer_id} = ${users.id}"
        assert users["join_type"] == "inner" and users["relationship_type"] == "one_to_one"
        assert "Multiple parents" in report

    @pytest.mark.parametrize("model,missing", [
        ("explore: sales { extends: [nowhere] view_name: orders }", "nowhere"),
        ("explore: mid { extends: [nowhere] view_name: orders }\nexplore: sales { extends: [mid] }", "nowhere"),
        ("explore: a { extends: [sales] view_name: orders }\nexplore: sales { extends: [a] view_name: orders }",
         "circular"),
    ])
    def test_unresolvable_parent_keeps_old_behaviour(self, tmp_path, model, missing):
        repo = make_repo(tmp_path, model + "\n")
        result, topic, report = migrate(tmp_path, repo, "sales")
        assert "extends" not in topic
        assert topic["joins"] == {}
        section = item(report, "Explore inheritance (extends)")
        assert "inherited joins and settings were not merged" in section
        assert missing in section
        assert any(i.startswith("Explore inheritance (extends)") for i in result.issues)

    def test_parent_defined_twice_not_merged(self, tmp_path):
        repo = make_repo(tmp_path, "explore: sales { extends: [orders_base] view_name: orders }\n", {
            "a.explore.lkml": "explore: orders_base { view_name: orders }\n",
            "b.explore.lkml": "explore: orders_base { view_name: orders }\n",
        })
        _, topic, report = migrate(tmp_path, repo, "sales")
        assert "extends" not in topic
        assert "more than once" in report

    def test_base_view_always_written(self, tmp_path):
        repo = self.repo(tmp_path, "explore: sales { extends: [orders_base] }\n")
        _, topic, _ = migrate(tmp_path, repo, "sales")
        assert topic["base_view"] == "orders"

    def test_parent_refinement_reported(self, tmp_path):
        repo = make_repo(tmp_path, "explore: sales { extends: [orders_base] }\n", {
            "base.explore.lkml": BASE_EXPLORE,
            "refine.lkml": "explore: +orders_base { sql_always_where: 1 = 0 ;; }\n",
        })
        _, _, report = migrate(tmp_path, repo, "sales")
        assert "Refinements (explore: +name) NOT applied" in report
        assert "orders_base" in item(report, "Refinements (explore: +name) NOT applied")

    def test_both_define_access_filters_flagged(self, tmp_path):
        repo = self.repo(tmp_path, """
            explore: sales {
              extends: [orders_base]
              access_filter: {
                field: orders.region
                user_attribute: allowed_region
              }
            }
        """)
        _, topic, report = migrate(tmp_path, repo, "sales")
        assert topic["access_filters"] == [{"field": "orders.region", "user_attribute": "allowed_region"}]
        assert "Access filters: child replaces the parent's" in report

    def test_resolver_api(self, tmp_path):
        repo = self.repo(tmp_path, "explore: sales { extends: [orders_base] }\n")
        definitions, _ = load_explores(str(repo))
        resolution = resolve_explore_extends(definitions["sales"][0], definitions)
        assert resolution.problem is None
        assert resolution.ancestors == ["orders_base"]
        assert resolution.explore.base_view == "orders"
        assert [j.view for j in resolution.explore.joins] == ["users", "products"]
        assert resolution.explore.sources["join:users"] == "orders_base"
        assert find_extending_explores("orders_base", definitions) == ["sales"]

    def test_fixture_cli(self, tmp_path):
        result = run_cli("--explore", "inventory", "--looker-repo", FIXTURE_REPO, cwd=tmp_path)
        assert result.returncode == 0, result.stdout + result.stderr
        topic = load_yaml(tmp_path / "migration" / "staging" / "topics" / "inventory.topic.yaml")
        assert topic["extends"] == ["inventory_base"]
        assert topic["base_view"] == "inventory_items"
        assert topic["joins"] == {"products": {"distribution_centers": {}}}
        assert [r["join_to_view"] for r in topic["relationships"]] == ["products", "distribution_centers"]
        assert topic["group_label"] == "Inventory"
        assert topic["default_filters"] == {
            "inventory_items.sold": {"time_for_duration": ["30 complete days ago", "30 days"], "required": True}
        }


# ----------------------------------------------------------------------
# E6: template topics
# ----------------------------------------------------------------------

class TestTemplateTopics:
    CHILD = "explore: sales { extends: [orders_base] }\n"

    def test_extended_and_extension_required_is_template(self, tmp_path):
        repo = make_repo(tmp_path, self.CHILD + BASE_EXPLORE)
        _, topic, report = migrate(tmp_path, repo, "orders_base")
        assert topic["template"] is True
        assert topic["base_view"] == "orders"
        line = next(l for l in report.splitlines() if l.startswith("- **Topic type**"))
        assert "template" in line and "extension: required" in line and "sales" in line

    def test_extended_without_extension_required_stays_normal(self, tmp_path):
        repo = make_repo(tmp_path, self.CHILD + BASE_EXPLORE.replace("extension: required", ""))
        _, topic, report = migrate(tmp_path, repo, "orders_base")
        assert "template" not in topic
        line = next(l for l in report.splitlines() if l.startswith("- **Topic type**"))
        assert "normal topic" in line and "sales" in line and "--template" in line

    def test_template_flag_forces(self, tmp_path):
        repo = make_repo(tmp_path, self.CHILD + BASE_EXPLORE.replace("extension: required", ""))
        _, topic, report = migrate(tmp_path, repo, "orders_base", template=True)
        assert topic["template"] is True
        line = next(l for l in report.splitlines() if l.startswith("- **Topic type**"))
        assert "--template" in line

    def test_extension_required_but_never_extended(self, tmp_path):
        repo = make_repo(tmp_path, BASE_EXPLORE)
        result, topic, report = migrate(tmp_path, repo, "orders_base")
        assert topic["template"] is True
        assert "Template topic is not extended" in report
        assert any(i.startswith("Template topic is not extended") for i in result.issues)

    def test_plain_explore_has_no_template_key(self, tmp_path):
        repo = make_repo(tmp_path, "explore: orders {}\n")
        _, topic, report = migrate(tmp_path, repo)
        assert "template" not in topic
        assert "- **Topic type**: normal topic" in report

    def test_cli_template_flag_and_fixture(self, tmp_path):
        topics = tmp_path / "migration" / "staging" / "topics"
        result = run_cli("--explore", "inventory_base", "--looker-repo", FIXTURE_REPO, cwd=tmp_path)
        assert result.returncode == 0, result.stdout + result.stderr
        assert load_yaml(topics / "inventory_base.topic.yaml")["template"] is True
        result = run_cli("--explore", "events", "--looker-repo", FIXTURE_REPO, "--template", cwd=tmp_path)
        assert result.returncode == 0, result.stdout + result.stderr
        assert load_yaml(topics / "events.topic.yaml")["template"] is True

    def test_parser_reads_extension_and_view_name(self, tmp_path):
        repo = make_repo(tmp_path, BASE_EXPLORE)
        base = next(e for e in parse_explore_file(str(repo / "shop.model.lkml")) if e.name == "orders_base")
        assert base.extension_required is True
        assert base.view_name == "orders"
        assert base.hidden is True and base.hidden_explicit is True
