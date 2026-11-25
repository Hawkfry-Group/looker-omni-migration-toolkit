"""
Looker aliases kept and scoped to the topic: the base alias declared in the
topic's `views:` block, alias reference verification, view labels, the alias
map, cross-view fields written in the topic instead of the view file, the
topic file prefix, the duplicate view message, filters that name the base
alias and references to dimension group timeframes. Each test builds a small
LookML project in tmp_path.
"""

import csv as csv_module
import textwrap
from pathlib import Path

import pytest
from pydantic import ValidationError

from conftest import SKILL_ROOT, load_yaml, run_cli
from lib.explore_analyzer import find_models_including, load_explores
from lib.file_generators import FileGenerator
from lib.looker_parser import load_views, view_from_index
from lib.omni_models import OmniTopic
from lib.orchestrator import MigrationOrchestrator
from lib.rule_applier import RuleApplier, match_timeframe, time_groups_of

VIEWS = """
view: sales_fact {
  sql_table_name: shop.sales ;;
  dimension: id { primary_key: yes type: number sql: ${TABLE}.id ;; }
  dimension: store_key { type: number sql: ${TABLE}.store_key ;; }
  dimension: amount { type: number sql: ${TABLE}.amount ;; }
  dimension: signup_date { type: string sql: ${TABLE}.signup_date ;; }
  dimension_group: sold {
    type: time
    timeframes: [raw, date, week, month]
    sql: ${TABLE}.sold_at ;;
  }
  dimension_group: packed { type: time sql: ${TABLE}.packed_at ;; }

  dimension: sold_day { type: string sql: CAST(${sold_date} AS VARCHAR) ;; }
  dimension: sold_instant { sql: ${sales_fact.sold_raw} ;; }
  dimension: sold_month_text { sql: ${sold_month} ;; }
  dimension: sold_year_text { sql: ${sold_year} ;; }
  dimension: packed_week_text { sql: ${packed_week} ;; }
  dimension: packed_day { sql: ${packed_date} ;; }
  dimension: signup_text { sql: UPPER(${signup_date}) ;; }
  dimension: store_opened { sql: ${stores.opened_date} ;; }

  dimension: store_label { label: "Store Label" sql: ${shop.store_name} ;; }
  dimension: store_label_upper { sql: UPPER(${store_label}) ;; }
  dimension: shop_opened_on { hidden: yes sql: ${shop.opened_date} ;; }
  dimension: depot_label { sql: ${depot.name} ;; }
  dimension: real_ref { sql: ${stores.store_name} ;; }
  measure: north_sales {
    type: sum
    sql: ${amount} ;;
    filters: [shop.region: "north"]
    required_access_grants: [can_view_sales]
  }
  measure: north_sales_double { type: number sql: ${north_sales} * 2 ;; }
  measure: count { type: count }
}

view: stores {
  sql_table_name: shop.stores ;;
  dimension: store_key { primary_key: yes type: number sql: ${TABLE}.store_key ;; }
  dimension: store_name { sql: ${TABLE}.store_name ;; }
  dimension: region { sql: ${TABLE}.region ;; }
  dimension_group: opened { type: time sql: ${TABLE}.opened_at ;; }
}

view: warehouses {
  sql_table_name: shop.warehouses ;;
  dimension: warehouse_key { primary_key: yes type: number sql: ${TABLE}.warehouse_key ;; }
  dimension: name { sql: ${TABLE}.name ;; }
}
"""

MODEL = """
access_grant: can_view_sales { user_attribute: team allowed_values: ["sales"] }

explore: sales {
  from: sales_fact
  label: "Sales"
  view_label: "Sales Facts"
  sql_always_where: ${sales.amount} > 0 ;;
  always_filter: { filters: [sales.sold_date: "7 days ago for 7 days"] }
  access_filter: { field: shop.region user_attribute: region }
  join: shop {
    from: stores
    view_label: "Shop"
    relationship: many_to_one
    sql_on: ${sales.store_key} = ${shop.store_key} ;;
  }
}

explore: logistics {
  view_name: sales_fact
  join: depot {
    from: warehouses
    relationship: many_to_one
    sql_on: ${sales_fact.store_key} = ${depot.warehouse_key} ;;
  }
  join: shop {
    from: warehouses
    relationship: many_to_one
    sql_on: ${sales_fact.store_key} = ${shop.warehouse_key} ;;
  }
}
"""

SALES_FIELDS = ["id", "store_key", "amount", "sold", "sold_day", "sold_instant", "sold_month_text", "packed_day",
                "signup_text", "store_label", "store_label_upper", "depot_label", "real_ref", "north_sales",
                "north_sales_double", "count"]


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text))
    return path


def make_repo(tmp_path, model: str = MODEL, views: str = VIEWS, extra_files=None) -> Path:
    repo = tmp_path / "repo"
    write(repo / "views" / "shop.view.lkml", views)
    write(repo / "shop.model.lkml", model)
    for name, text in (extra_files or {}).items():
        write(repo / name, text)
    return repo


def scope_csv(tmp_path, view: str, fields) -> Path:
    path = tmp_path / "scope.csv"
    with open(path, "w", newline="") as handle:
        writer = csv_module.writer(handle)
        writer.writerow(["owner", "phase", "view", "field", "field_type"])
        for name in fields:
            writer.writerow(["Team A", "0", view, name, "dimension"])
    return path


def migrate_view(tmp_path, repo, view="sales_fact", fields=SALES_FIELDS):
    orch = MigrationOrchestrator(skill_path=SKILL_ROOT, looker_repo=repo, omni_repo=tmp_path / "out" / "views",
                                 csv_path=scope_csv(tmp_path, view, fields))
    result = orch.migrate_view(view, 0, "Team A")
    assert result.success, result.error_message
    report = (tmp_path / "out" / "reports" / f"{view}.migration.md").read_text()
    return result, load_yaml(result.view_file_path), report


def migrate_explore(tmp_path, repo, name="sales", topic=None, **kwargs):
    topics = tmp_path / "out" / "topics"
    orch = MigrationOrchestrator(skill_path=SKILL_ROOT, looker_repo=repo, omni_repo=topics)
    result = orch.migrate_explore(name, **kwargs)
    assert result.success, result.error_message
    topic = topic or name
    report = (tmp_path / "out" / "reports" / f"{topic}_explore.md").read_text()
    return result, load_yaml(topics / f"{topic}.topic.yaml"), report


def section(report: str, heading: str) -> str:
    return report.split(f"## {heading}")[1].split("\n## ")[0]


def manual_item(report: str, title: str) -> str:
    return report.split(f"### {title}")[1].split("\n#")[0]


# ----------------------------------------------------------------------
# F1: filters that name the base alias
# ----------------------------------------------------------------------

class TestFiltersOnTheBaseAlias:
    def test_always_filter_through_from_and_view_name(self, tmp_path):
        repo = make_repo(tmp_path, """
            explore: orders {
              from: sales_fact
              view_name: orders
              always_filter: { filters: [orders.sold_date: "7 days ago for 7 days", orders.amount: "5"] }
            }
        """)
        _, topic, report = migrate_explore(tmp_path, repo, "orders")
        assert topic["default_filters"] == {
            "orders.sold": {"time_for_duration": ["7 complete days ago", "7 days"], "required": True},
            "orders.amount": {"is": 5, "required": True},
        }
        assert "always_filter (NOT carried)" not in report
        assert "was not found under --looker-repo" not in report

    def test_real_view_is_the_from_view_when_both_are_set(self, tmp_path):
        repo = make_repo(tmp_path, "explore: orders { from: sales_fact view_name: orders }\n")
        explore = load_explores(str(repo))[0]["orders"][0]
        assert (explore.base_view, explore.base_alias, explore.base_is_aliased) == ("sales_fact", "orders", True)
        assert explore.view_names() == {"orders": "sales_fact"}

    @pytest.mark.parametrize("body,expected", [
        ("", ("plain", "plain", False)),
        ("from: sales_fact", ("sales_fact", "plain", True)),
        ("view_name: sales_fact", ("sales_fact", "sales_fact", False)),
    ])
    def test_base_alias_forms(self, tmp_path, body, expected):
        repo = make_repo(tmp_path, f"explore: plain {{ {body} }}\n")
        explore = load_explores(str(repo))[0]["plain"][0]
        assert (explore.base_view, explore.base_alias, explore.base_is_aliased) == expected

    def test_filter_on_a_name_that_is_not_the_base_alias_is_not_carried(self, tmp_path):
        repo = make_repo(tmp_path, """
            explore: orders {
              from: sales_fact
              always_filter: { filters: [sales_fact.amount: "5"] }
            }
        """)
        _, topic, report = migrate_explore(tmp_path, repo, "orders")
        assert "default_filters" not in topic
        assert "`sales_fact` is not part of the explore" in manual_item(report, "always_filter (NOT carried)")

    def test_row_restrictions_through_the_base_alias_are_verified(self, tmp_path):
        repo = make_repo(tmp_path, """
            explore: orders {
              from: sales_fact
              sql_always_where: ${orders.amount} > 0 AND ${orders.nowhere} = 1 AND ${ghost.x} = 1 ;;
              sql_always_having: ${orders.count} > 0 ;;
              access_filter: { field: orders.missing user_attribute: team }
            }
        """)
        _, topic, report = migrate_explore(tmp_path, repo, "orders")
        assert topic["always_where_sql"].startswith("${orders.amount} > 0")
        unresolved = manual_item(report, "Unresolved field references")
        assert "always_where_sql: `${orders.nowhere}`" in unresolved and "has no field `nowhere`" in unresolved
        assert "`ghost` is not a view of this topic" in unresolved
        assert "access_filters: `${orders.missing}`" in unresolved
        assert "orders.amount" not in unresolved and "orders.count" not in unresolved


# ----------------------------------------------------------------------
# One alias map for filters, Liquid and cross-view fields
# ----------------------------------------------------------------------

class TestSharedAliasMap:
    LIQUID_VIEW = """
        view: sales_window {
          derived_table: {
            sql: SELECT id, sold_at FROM shop.sales
                 WHERE {% condition orders.sold_date %} sold_at {% endcondition %} ;;
          }
          dimension: id { primary_key: yes type: number sql: ${TABLE}.id ;; }
        }
    """

    def orchestrator(self, tmp_path, model, extra=""):
        repo = make_repo(tmp_path, model, VIEWS + textwrap.dedent(self.LIQUID_VIEW) + textwrap.dedent(extra))
        return MigrationOrchestrator(skill_path=SKILL_ROOT, looker_repo=repo, omni_repo=tmp_path / "out" / "views",
                                     csv_path=scope_csv(tmp_path, "sales_window", ["id"]))

    @pytest.mark.parametrize("model", [
        "explore: orders { from: sales_fact view_name: orders }",
        "explore: orders { from: sales_fact }",
        "explore: wide { from: sales_fact view_name: orders }",
    ])
    def test_liquid_condition_through_a_base_alias(self, tmp_path, model):
        result = self.orchestrator(tmp_path, model).migrate_view("sales_window", 0, "Team A")
        assert result.success, result.error_message
        view = load_yaml(result.view_file_path)
        # The alias is kept: Omni binds a filter to the name the view goes by in the topic
        assert "{{# orders.sold.filter }} sold_at {{/ orders.sold.filter }}" in view["sql"]

    def test_map_holds_base_and_join_aliases(self, tmp_path):
        orch = self.orchestrator(tmp_path, MODEL)
        assert orch._alias_targets() == {
            "sales": {"sales_fact"}, "shop": {"stores", "warehouses"}, "depot": {"warehouses"},
        }
        assert orch._resolve_liquid_view("sales", "sales_window") == ("sales_fact", None)
        assert orch._resolve_liquid_view("depot", "sales_window") == ("warehouses", None)

    def test_base_alias_of_several_views_stays_manual(self, tmp_path):
        model = "explore: orders { from: sales_fact }\nexplore: other { from: stores view_name: orders }\n"
        result = self.orchestrator(tmp_path, model).migrate_view("sales_window", 0, "Team A")
        assert not result.success
        assert "MANUAL ACTION REQUIRED" in result.error_message
        assert "alias of several views (sales_fact, stores)" in result.error_message

    def test_view_name_alone_is_not_an_alias(self, tmp_path):
        orch = self.orchestrator(tmp_path, "explore: orders { view_name: sales_fact }")
        assert orch._alias_targets() == {}


# ----------------------------------------------------------------------
# T1: references to dimension group timeframes
# ----------------------------------------------------------------------

class TestTimeframeReferences:
    @pytest.fixture
    def migrated(self, tmp_path):
        return migrate_view(tmp_path, make_repo(tmp_path))

    def test_date_and_raw_are_rewritten(self, migrated):
        _, view, _ = migrated
        dims = view["dimensions"]
        assert dims["sold_day"]["sql"] == "CAST(${sold[date]} AS VARCHAR)"
        # with a view prefix, and a group that has no `timeframes:` list (Looker's defaults)
        assert dims["sold_instant"]["sql"] == "${sales_fact.sold[raw]}"
        assert dims["packed_day"]["sql"] == "${packed[date]}"

    def test_timeframe_of_another_view(self, migrated):
        _, view, _ = migrated
        assert view["dimensions"]["store_opened"]["sql"] == "${stores.opened[date]}"

    def test_other_timeframes_are_not_written(self, migrated):
        result, view, _ = migrated
        assert "sold_month_text" not in view["dimensions"]
        issue = next(i for i in result.issues if i.startswith("sold_month_text"))
        assert "MANUAL ACTION REQUIRED" in issue
        assert "`month` timeframe of dimension group `sold`" in issue
        assert "formatted string or an integer" in issue and "silently change meaning" in issue
        # out of scope and not convertible: omitted, reported, no hidden field
        assert "packed_week_text" not in view["dimensions"]

    def test_suffix_outside_the_group_timeframes_is_an_undefined_reference(self, migrated):
        # `sold` lists [raw, date, week, month]: `sold_year` is not one of its timeframes
        _, view, report = migrated
        assert "sold_year_text" not in view["dimensions"]
        assert "references ${sold_year}, which is not defined in the view" in report

    def test_a_real_dimension_named_like_a_timeframe_is_left_alone(self, migrated):
        _, view, _ = migrated
        assert view["dimensions"]["signup_text"]["sql"] == "UPPER(${signup_date})"

    def test_the_group_becomes_a_dependency(self, tmp_path):
        # `sold` is out of the CSV scope: the rewritten reference still pulls it in
        _, view, _ = migrate_view(tmp_path, make_repo(tmp_path), fields=["id", "sold_day"])
        assert "auto_dependency" in view["dimensions"]["sold"]["tags"]

    def test_report_lists_each_rewrite(self, migrated):
        _, _, report = migrated
        rewritten = section(report, "Timeframe references rewritten")
        assert "**dimension `sold_day`**: `${sold_date}` → `${sold[date]}`" in rewritten
        assert "`${sales_fact.sold_raw}` → `${sales_fact.sold[raw]}`" in rewritten
        assert "`${stores.opened_date}` → `${stores.opened[date]}`" in rewritten
        assert "sold_month" not in rewritten
        # the group's own `timeframes:` list is not a rewrite
        assert "**dimension group" not in rewritten

    def test_match_timeframe(self):
        groups = time_groups_of({
            "created": {"type": "time", "timeframes": ["date", "week"]},
            "shipped": {},
            "transit": {"type": "duration", "intervals": ["day"]},
        })
        assert groups == {"created": ["date", "week"], "shipped": None}
        assert match_timeframe("created_date", groups) == [("created", "date")]
        assert match_timeframe("created_month", groups) == []
        assert match_timeframe("shipped_day_of_week", groups) == [("shipped", "day_of_week")]
        assert match_timeframe("shipped_hour4", groups) == [("shipped", "hour4")]
        assert match_timeframe("shipped_banana", groups) == []
        assert match_timeframe("transit_date", groups) == []

    def test_name_two_groups_could_supply_is_not_guessed(self):
        applier = RuleApplier(phase=0, view_name="v", time_groups={"created": None, "created_fiscal": None})
        result = applier.convert_dimension("x", {"sql": "${created_fiscal_month_num}"})
        assert result.omni_field is None
        assert "several dimension groups" in result.warnings[0]

    def test_rewritten_reference_survives_a_merge_rename(self, tmp_path):
        from lib.file_generators import _rewrite_field_refs
        assert _rewrite_field_refs("${created[date]} ${v.created[raw]} ${created}", {"created": "created_at"}, "v") \
            == "${created_at[date]} ${v.created_at[raw]} ${created_at}"

    def test_join_condition_timeframes(self, tmp_path):
        repo = make_repo(tmp_path, """
            explore: sales_fact {
              join: stores {
                relationship: many_to_one
                sql_on: ${sales_fact.sold_date} = ${stores.opened_date} ;;
              }
              join: warehouses {
                relationship: many_to_one
                sql_on: ${sales_fact.sold_month} = ${warehouses.name} ;;
              }
            }
        """)
        _, topic, report = migrate_explore(tmp_path, repo, "sales_fact")
        assert [r["on_sql"] for r in topic["relationships"]] == [
            "${sales_fact.sold[date]} = ${stores.opened[date]}"]
        assert "timeframe reference rewritten" in section(report, "Carried Into the Topic")
        assert "`month` timeframe of dimension group `sold`" in manual_item(report, "Join warehouses (NOT migrated)")


# ----------------------------------------------------------------------
# J3a: aliases kept, scoped to the topic
# ----------------------------------------------------------------------

class TestAliasesInTheTopic:
    @pytest.fixture
    def migrated(self, tmp_path):
        return migrate_explore(tmp_path, make_repo(tmp_path))

    def test_base_alias_is_declared_in_the_views_block(self, migrated):
        _, topic, _ = migrated
        assert topic["base_view"] == "sales"
        assert topic["views"]["sales"]["extends"] == ["sales_fact"]
        assert list(topic)[:3] == ["base_view", "label", "description"]
        assert list(topic).index("views") < list(topic).index("relationships") < list(topic).index("joins")

    def test_relationship_uses_the_aliases(self, migrated):
        _, topic, _ = migrated
        assert topic["relationships"] == [{
            "join_from_view": "sales",
            "join_to_view": "shop",
            "join_type": "always_left",
            "on_sql": "${sales.store_key} = ${shop.store_key}",
            "relationship_type": "many_to_one",
            "reversible": False,
        }]
        assert topic["joins"] == {"shop": {}}

    def test_filters_keep_the_alias(self, migrated):
        _, topic, _ = migrated
        assert list(topic["default_filters"]) == ["sales.sold"]
        assert topic["access_filters"] == [{"field": "shop.region", "user_attribute": "region"}]
        assert topic["always_where_sql"] == "${sales.amount} > 0"

    def test_no_manual_item_for_an_alias_that_resolves(self, migrated):
        result, _, report = migrated
        assert "Aliased join" not in report and "Aliased base view" not in report
        assert "Unresolved field references" not in report
        assert not any(issue.startswith(("Aliased", "Unresolved", "Alias references")) for issue in result.issues)

    def test_view_labels(self, migrated):
        _, topic, report = migrated
        assert topic["views"]["sales"]["label"] == "Sales Facts"
        assert topic["views"]["shop"]["label"] == "Shop"
        assert "view_label of `shop` → `label: Shop`" in section(report, "Carried Into the Topic")

    def test_view_label_equal_to_the_views_own_label_is_not_written(self, tmp_path):
        repo = make_repo(tmp_path, """
            explore: sales_fact {
              view_label: "Sales Fact"
              join: stores { view_label: "Stores" sql_on: ${sales_fact.store_key} = ${stores.store_key} ;; }
            }
        """)
        _, topic, _ = migrate_explore(tmp_path, repo, "sales_fact")
        # no alias, no label that differs, and no field of these views needs an alias of this topic
        assert "views" not in topic

    def test_alias_map_in_the_report(self, migrated):
        _, _, report = migrated
        table = section(report, "Alias map")
        assert "| Alias | Real view | Where declared |" in table
        assert "| `sales` | `sales_fact` | topic `views:` block (`extends: [sales_fact]`), used as `base_view` |" in table
        assert "| `shop` | `stores` | topic `views:` block (`extends: [stores]`), joined by that name |" in table
        assert "**Base View**: sales (alias of view `sales_fact`)" in report

    def test_alias_map_without_aliases(self, tmp_path):
        repo = make_repo(tmp_path, "explore: sales_fact {}\n")
        _, topic, report = migrate_explore(tmp_path, repo, "sales_fact")
        assert "(no aliases" in section(report, "Alias map")
        assert "views" not in topic

    def test_unresolved_alias_reference_is_reported(self, tmp_path):
        repo = make_repo(tmp_path, """
            explore: sales {
              from: sales_fact
              join: shop {
                from: stores
                relationship: many_to_one
                sql_on: ${sales.store_key} = ${shop.nowhere} AND ${sales.sold_date} = ${shop.opened_date} ;;
              }
            }
        """)
        result, topic, report = migrate_explore(tmp_path, repo)
        unresolved = manual_item(report, "Unresolved field references")
        assert "join `shop` on_sql: `${shop.nowhere}`: `shop` is `stores` in this topic" in unresolved
        assert "has no field `nowhere`" in unresolved
        # references that resolve (own field, timeframe) are not listed
        assert "store_key" not in unresolved and "opened" not in unresolved and "sold" not in unresolved
        assert topic["relationships"][0]["on_sql"].endswith("${sales.sold[date]} = ${shop.opened[date]}")
        assert any(i.startswith("Unresolved field references") for i in result.issues)

    def test_reference_through_an_inherited_field_resolves(self, tmp_path):
        views = VIEWS + """
            view: keyed { extension: required dimension: legacy_key { type: number sql: ${TABLE}.legacy_key ;; } }
            view: outlets { extends: [keyed] sql_table_name: shop.outlets ;; dimension: id { primary_key: yes } }
        """
        repo = make_repo(tmp_path, """
            explore: sales {
              from: sales_fact
              join: outlet { from: outlets sql_on: ${sales.store_key} = ${outlet.legacy_key} ;; }
            }
        """, views)
        _, _, report = migrate_explore(tmp_path, repo)
        assert "Unresolved field references" not in report

    def test_alias_of_a_view_that_is_not_in_the_repo_is_not_verified(self, tmp_path):
        repo = make_repo(tmp_path, """
            explore: sales_fact {
              join: buyer { from: people sql_on: ${sales_fact.id} = ${buyer.id} ;; }
            }
        """)
        result, topic, report = migrate_explore(tmp_path, repo, "sales_fact")
        assert topic["relationships"][0]["join_to_view"] == "buyer"
        assert "join_to_view_as" not in topic["relationships"][0]
        assert topic["views"]["buyer"] == {"extends": ["people"]}
        assert "`buyer` (view `people`)" in manual_item(report, "Alias references not verified")

    def test_reference_by_the_real_name_of_an_aliased_base_is_not_part_of_the_explore(self, tmp_path):
        repo = make_repo(tmp_path, """
            explore: sales {
              from: sales_fact
              join: stores { sql_on: ${sales_fact.store_key} = ${stores.store_key} ;; }
            }
        """)
        _, topic, report = migrate_explore(tmp_path, repo)
        assert topic["joins"] == {}
        assert "sales_fact, which is not part of the explore" in manual_item(report, "Join stores (NOT migrated)")

    def test_base_alias_named_like_another_view_stops(self, tmp_path):
        repo = make_repo(tmp_path, "explore: stores { from: sales_fact }\n")
        orch = MigrationOrchestrator(skill_path=SKILL_ROOT, looker_repo=repo, omni_repo=tmp_path / "out" / "topics")
        result = orch.migrate_explore("stores")
        assert not result.success
        assert "MANUAL ACTION REQUIRED" in result.error_message and "`stores` is also a view" in result.error_message
        assert not (tmp_path / "out").exists()

    def test_join_alias_named_like_another_view_is_reported(self, tmp_path):
        repo = make_repo(tmp_path, """
            explore: sales_fact {
              join: warehouses { from: stores sql_on: ${sales_fact.store_key} = ${warehouses.store_key} ;; }
            }
        """)
        _, topic, report = migrate_explore(tmp_path, repo, "sales_fact")
        assert topic["relationships"][0]["join_to_view_as"] == "warehouses"
        assert "Join alias warehouses has the name of another view" in report

    def test_same_alias_for_different_views_in_two_topics(self, tmp_path):
        repo = make_repo(tmp_path)
        _, sales, _ = migrate_explore(tmp_path, repo, "sales")
        _, logistics, _ = migrate_explore(tmp_path, repo, "logistics")
        target = {t["base_view"]: {r["join_to_view"]: t["views"][r["join_to_view"]]["extends"][0]
                                   for r in t["relationships"]}
                  for t in (sales, logistics)}
        assert target == {"sales": {"shop": "stores"},
                          "sales_fact": {"depot": "warehouses", "shop": "warehouses"}}

    def test_base_alias_with_relationships_file_is_reported(self, tmp_path):
        _, _, report = migrate_explore(tmp_path, make_repo(tmp_path), relationships_file=True)
        assert "Base view alias with --relationships-file" in report
        assert "`join_to_view_as` in the relationships file" in section(report, "Alias map")

    def test_inherited_base_alias(self, tmp_path):
        repo = make_repo(tmp_path, """
            explore: sales_base {
              extension: required
              from: sales_fact
              view_name: sales
              view_label: "Sales Facts"
              join: shop { from: stores sql_on: ${sales.store_key} = ${shop.store_key} ;; }
            }
            explore: retail { extends: [sales_base] }
        """)
        _, topic, _ = migrate_explore(tmp_path, repo, "retail")
        assert topic["base_view"] == "sales"
        assert topic["views"]["sales"]["extends"] == ["sales_fact"]
        assert topic["views"]["sales"]["label"] == "Sales Facts"

    def test_views_block_validation(self):
        base = dict(base_view="a", label="A", description="d", fields=["all_views.*"], ai_context="c")
        assert OmniTopic(**base, views={"a": {"extends": ["b"]}}).views == {"a": {"extends": ["b"]}}
        with pytest.raises(ValidationError, match="Unsupported keys"):
            OmniTopic(**base, views={"a": {"sql": "x"}})
        with pytest.raises(ValidationError, match="is empty"):
            OmniTopic(**base, views={"a": {}})


# ----------------------------------------------------------------------
# J3d / J3c: cross-view fields live in the topic
# ----------------------------------------------------------------------

class TestTopicScopedFieldsInViewMode:
    @pytest.fixture
    def migrated(self, tmp_path):
        return migrate_view(tmp_path, make_repo(tmp_path))

    def test_alias_fields_are_not_in_the_view_file(self, migrated):
        _, view, _ = migrated
        for name in ("store_label", "store_label_upper", "shop_opened_on", "depot_label"):
            assert name not in view["dimensions"], name
        for name in ("north_sales", "north_sales_double"):
            assert name not in view["measures"], name
        assert "shop." not in str(view) and "depot." not in str(view)

    def test_reference_to_a_real_view_stays(self, migrated):
        _, view, report = migrated
        assert view["dimensions"]["real_ref"]["sql"] == "${stores.store_name}"
        assert "stores.store_name" in report.split("### Cross-View References")[1]

    def test_report_lists_them_with_their_aliases(self, migrated):
        _, _, report = migrated
        listed = section(report, "Topic-scoped fields")
        assert "- **store_label** (dimension): needs `shop`" in listed
        assert "- **store_label_upper** (dimension): needs `shop`" in listed
        assert "- **depot_label** (dimension): needs `depot`" in listed
        assert "- **north_sales** (measure): needs `shop`" in listed
        assert "- **north_sales_double** (measure): needs `shop`" in listed
        assert "real_ref" not in listed and "sold_day" not in listed

    def test_they_are_not_manual_items(self, migrated):
        result, _, report = migrated
        for name in ("store_label", "store_label_upper", "depot_label", "north_sales", "north_sales_double"):
            assert not any(issue.startswith(f"{name}:") for issue in result.issues), name
            assert f"**{name}**" not in section(report, "Not Converted"), name

    def test_other_fields_are_unaffected(self, migrated):
        _, view, _ = migrated
        assert {"id", "store_key", "amount", "sold", "real_ref"} <= set(view["dimensions"])
        assert set(view["measures"]) == {"count"}

    def test_no_alias_no_section(self, tmp_path):
        _, _, report = migrate_view(tmp_path, make_repo(tmp_path), "stores", ["store_key", "store_name"])
        assert "## Topic-scoped fields" not in report

    def test_unknown_name_is_kept_and_flagged(self, tmp_path):
        views = VIEWS + "view: notes { sql_table_name: shop.notes ;; dimension: id { primary_key: yes } " \
                        "dimension: ghost_ref { sql: ${ghost.name} ;; } }\n"
        _, view, report = migrate_view(tmp_path, make_repo(tmp_path, views=views), "notes", ["id", "ghost_ref"])
        assert view["dimensions"]["ghost_ref"]["sql"] == "${ghost.name}"
        assert "Not found under --looker-repo as a view or a join alias: `ghost`" in report
        assert "## Topic-scoped fields" not in report

    def test_inherited_topic_scoped_field(self, tmp_path):
        views = VIEWS + """
            view: sales_recent {
              extends: [sales_fact]
              dimension: label_length { type: number sql: LENGTH(${store_label}) ;; }
              dimension: plain { sql: ${TABLE}.plain ;; }
            }
        """
        _, view, report = migrate_view(tmp_path, make_repo(tmp_path, views=views), "sales_recent",
                                       ["label_length", "plain"])
        assert set(view["dimensions"]) == {"plain"}
        listed = section(report, "Topic-scoped fields")
        assert "- **label_length** (dimension): needs `shop`" in listed
        assert "- **store_label** (dimension, inherited from `sales_fact`): needs `shop`" in listed


class TestTopicScopedFieldsInExploreMode:
    @pytest.fixture
    def sales(self, tmp_path):
        return migrate_explore(tmp_path, make_repo(tmp_path))

    def test_fields_are_written_under_the_views_name_in_the_topic(self, sales):
        _, topic, _ = sales
        block = topic["views"]["sales"]
        assert block["dimensions"]["store_label"]["sql"] == "${shop.store_name}"
        assert block["dimensions"]["store_label_upper"]["sql"] == "UPPER(${store_label})"
        assert block["measures"]["north_sales"] == {
            "label": "North Sales", "description": "North Sales measure", "tags": ["metric", "verified"],
            "sql": "${amount}", "aggregate_type": "sum", "filters": {"shop.region": {"is": "north"}},
            "ai_context": "Aggregate sum across records.", "required_access_grants": ["can_view_sales"],
        }
        assert block["measures"]["north_sales_double"]["sql"] == "${north_sales} * 2"
        assert list(block) == ["label", "extends", "dimensions", "measures"]

    def test_field_looks_like_a_view_file_field(self, sales):
        _, topic, _ = sales
        field = topic["views"]["sales"]["dimensions"]["store_label"]
        assert field == {"label": "Store Label", "description": "Store Label dimension", "tags": ["verified"],
                         "sql": "${shop.store_name}", "ai_context": "Text field for filtering and display."}

    def test_timeframe_through_an_alias_and_looker_hidden(self, sales):
        _, topic, report = sales
        field = topic["views"]["sales"]["dimensions"]["shop_opened_on"]
        assert field["sql"] == "${shop.opened[date]}"
        assert field["hidden"] is True
        assert "`${shop.opened_date}` → `${shop.opened[date]}`" in section(report, "Topic-scoped fields")

    def test_field_needing_an_alias_the_topic_lacks_is_left_out(self, sales):
        result, topic, report = sales
        assert "depot_label" not in topic["views"]["sales"]["dimensions"]
        line = next(l for l in section(report, "Topic-scoped fields").splitlines() if "depot_label" in l)
        assert "left out" in line and "`depot` is not a view of this topic" in line
        assert not any("depot_label" in issue for issue in result.issues)

    def test_only_topic_scoped_fields_are_written(self, sales):
        _, topic, _ = sales
        block = topic["views"]["sales"]
        assert set(block["dimensions"]) == {"store_label", "store_label_upper", "shop_opened_on"}
        assert set(block["measures"]) == {"north_sales", "north_sales_double"}
        assert set(topic["views"]["shop"]) == {"label", "extends"}
        assert topic["views"]["shop"]["extends"] == ["stores"]

    def test_report_section(self, sales):
        _, _, report = sales
        listed = section(report, "Topic-scoped fields")
        assert "- `sales.store_label` (dimension, view `sales_fact`): written, uses `shop`" in listed
        assert "visible unless Looker hides it" in listed
        assert "(`views:`)" in section(report, "Generated Files")

    def test_field_access_grant_is_reported(self, sales):
        result, _, report = sales
        assert "can_view_sales" in manual_item(report, "Cross-view fields needing attention")

    def test_alias_pointing_at_another_view_in_this_topic(self, tmp_path):
        # In `logistics`, `shop` is view `warehouses`, which has none of the fields the `shop` fields read
        result, topic, report = migrate_explore(tmp_path, make_repo(tmp_path), "logistics")
        block = topic["views"]["sales_fact"]
        assert set(block["dimensions"]) == {"depot_label"}
        assert block["dimensions"]["depot_label"]["sql"] == "${depot.name}"
        assert "measures" not in block and "extends" not in block
        listed = section(report, "Topic-scoped fields")
        line = next(l for l in listed.splitlines() if "`sales_fact.store_label`" in l)
        assert "left out" in line and "`shop` is `warehouses` in this topic" in line
        assert "has no field `store_name`" in line
        dependent = next(l for l in listed.splitlines() if "`sales_fact.store_label_upper`" in l)
        assert "left out" in dependent and "depends on `store_label`" in dependent
        assert "Cross-view fields needing attention" not in report

    def test_phase_tag_when_a_phase_is_given(self, tmp_path):
        _, topic, _ = migrate_explore(tmp_path, make_repo(tmp_path), phase=2, verified=False)
        assert topic["views"]["sales"]["dimensions"]["store_label"]["tags"] == ["mig_phase_2", "unverified"]

    def test_joined_view_fields_go_under_the_join_alias(self, tmp_path):
        views = VIEWS + """
            view: regions {
              sql_table_name: shop.regions ;;
              dimension: code { primary_key: yes sql: ${TABLE}.code ;; }
              dimension: store_and_region { sql: CONCAT(${shop.store_name}, ${code}) ;; }
            }
        """
        repo = make_repo(tmp_path, """
            explore: sales_fact {
              join: shop { from: stores sql_on: ${sales_fact.store_key} = ${shop.store_key} ;; }
              join: area { from: regions sql_on: ${shop.region} = ${area.code} ;; }
            }
        """, views)
        _, topic, _ = migrate_explore(tmp_path, repo, "sales_fact")
        assert topic["views"]["area"] == {"extends": ["regions"], "dimensions": {"store_and_region": {
            "label": "Store And Region", "description": "Store And Region dimension", "tags": ["verified"],
            "sql": "CONCAT(${shop.store_name}, ${code})", "ai_context": "Text field for filtering and display.",
        }}}
        assert topic["joins"] == {"shop": {"area": {}}}

    def test_unconvertible_field_is_a_manual_item(self, tmp_path):
        views = VIEWS.replace(
            "dimension: depot_label { sql: ${depot.name} ;; }",
            "dimension: depot_label { sql: {% if x %} ${depot.name} {% endif %} ;; }")
        result, topic, report = migrate_explore(tmp_path, make_repo(tmp_path, views=views), "logistics")
        assert all(set(block) <= {"extends", "label"} for block in topic["views"].values())
        attention = manual_item(report, "Cross-view fields needing attention")
        assert "`sales_fact.depot_label`" in attention and "{% if %}" in attention
        assert any(i.startswith("Cross-view fields needing attention") for i in result.issues)

    def test_alias_whose_view_is_unknown_is_not_guessed(self, tmp_path):
        # J3c: `depot` stands for a view that is not under --looker-repo
        model = MODEL.replace("from: warehouses\n    relationship: many_to_one\n    sql_on: ${sales_fact.store_key} = "
                              "${depot.warehouse_key}", "from: outside_view\n    relationship: many_to_one\n    "
                              "sql_on: ${sales_fact.store_key} = ${depot.warehouse_key}")
        assert "outside_view" in model
        result, topic, report = migrate_explore(tmp_path, make_repo(tmp_path, model), "logistics")
        assert all(set(block) <= {"extends", "label"} for block in topic["views"].values())
        attention = manual_item(report, "Cross-view fields needing attention")
        assert "`sales_fact.depot_label`" in attention and "could not be checked" in attention

    def test_mustache_in_a_topic_field_is_a_block_scalar(self, tmp_path):
        topic = OmniTopic(base_view="a", label="A", description="d", fields=["all_views.*"], ai_context="c",
                          views={"a": {"dimensions": {"f": {"sql": "{{ omni_attributes.x }} = ${b.y}"}}}})
        text = FileGenerator(output_dir=tmp_path).render_topic_yaml(topic)
        assert "        sql: |-\n          {{ omni_attributes.x }} = ${b.y}\n" in text

    def test_looker_liquid_never_reaches_a_topic(self, tmp_path):
        topic = OmniTopic(base_view="a", label="A", description="d", fields=["all_views.*"], ai_context="c",
                          views={"a": {"dimensions": {"f": {"sql": "{% if x %} 1 {% endif %}"}}}})
        with pytest.raises(Exception, match="Liquid"):
            FileGenerator(output_dir=tmp_path).render_topic_yaml(topic)

    def test_views_are_parsed_once_per_run(self, tmp_path, monkeypatch):
        import lib.orchestrator as module
        calls = []
        real = module.load_views
        monkeypatch.setattr(module, "load_views", lambda repo: calls.append(repo) or real(repo))
        repo = make_repo(tmp_path)
        orch = MigrationOrchestrator(skill_path=SKILL_ROOT, looker_repo=repo, omni_repo=tmp_path / "out" / "topics")
        assert orch.migrate_explore("sales").success and orch.migrate_explore("logistics").success
        assert len(calls) == 1


# ----------------------------------------------------------------------
# J5: topic file prefix
# ----------------------------------------------------------------------

PREFIX_FILES = {
    "explores/retail.explore.lkml": """
        explore: retail_base { extension: required view_name: sales_fact }
        explore: retail { extends: [retail_base] label: "Retail Sales" }
    """,
    "retail.model.lkml": 'include: "/explores/*.explore.lkml"\ninclude: "/views/*.view.lkml"\n',
}


class TestTopicPrefix:
    def test_named_prefix(self, tmp_path):
        repo = make_repo(tmp_path, extra_files=PREFIX_FILES)
        result, topic, report = migrate_explore(tmp_path, repo, "retail", topic="core__retail", topic_prefix="core")
        assert result.topic_file_path.name == "core__retail.topic.yaml"
        assert result.report_file_path.name == "core__retail_explore.md"
        assert topic["extends"] == ["core__retail_base"]
        assert topic["label"] == "Retail Sales"
        assert "**Topic**: core__retail (prefix `core`, from `--topic-prefix`)" in report
        assert not (tmp_path / "out" / "topics" / "retail.topic.yaml").exists()

    def test_model_prefix_from_the_including_model(self, tmp_path):
        repo = make_repo(tmp_path, extra_files=PREFIX_FILES)
        result, topic, _ = migrate_explore(tmp_path, repo, "retail", topic="retail__retail", topic_prefix="model")
        assert topic["extends"] == ["retail__retail_base"]

    def test_model_prefix_from_the_defining_model(self, tmp_path):
        repo = make_repo(tmp_path, extra_files=PREFIX_FILES)
        result, _, _ = migrate_explore(tmp_path, repo, "sales", topic="shop__sales", topic_prefix="model")
        assert result.topic_file_path.name == "shop__sales.topic.yaml"

    def test_default_is_no_prefix(self, tmp_path):
        repo = make_repo(tmp_path, extra_files=PREFIX_FILES)
        result, topic, _ = migrate_explore(tmp_path, repo, "retail")
        assert result.topic_file_path.name == "retail.topic.yaml"
        assert topic["extends"] == ["retail_base"]

    @pytest.mark.parametrize("extra,reason", [
        ({"other.model.lkml": 'include: "/explores/**/*.lkml"\n'}, "several models (other, retail)"),
        ({"retail.model.lkml": 'include: "/views/*.view.lkml"\n'}, "no *.model.lkml file"),
    ])
    def test_model_prefix_without_a_single_model_fails(self, tmp_path, extra, reason):
        repo = make_repo(tmp_path, extra_files={**PREFIX_FILES, **extra})
        orch = MigrationOrchestrator(skill_path=SKILL_ROOT, looker_repo=repo, omni_repo=tmp_path / "out" / "topics")
        result = orch.migrate_explore("retail", topic_prefix="model")
        assert not result.success
        assert "--topic-prefix model" in result.error_message and reason in result.error_message
        assert not (tmp_path / "out").exists()

    def test_invalid_prefix_fails(self, tmp_path):
        orch = MigrationOrchestrator(skill_path=SKILL_ROOT, looker_repo=make_repo(tmp_path),
                                     omni_repo=tmp_path / "out" / "topics")
        result = orch.migrate_explore("sales", topic_prefix="bad/prefix")
        assert not result.success and "letters, digits and underscores" in result.error_message

    @pytest.mark.parametrize("include,found", [
        ('include: "/explores/*.explore.lkml"', True),
        ('include: "/explores/*.explore"', True),
        ('include: "explores/retail.explore.lkml"', True),
        ('include: "/**/*.explore.lkml"', True),
        ('include: "*.explore.lkml"', False),
        ('include: "//other_project/explores/*.explore.lkml"', False),
        ('include: "/shared/all.lkml"', True),
    ])
    def test_include_patterns(self, tmp_path, include, found):
        repo = tmp_path / "repo"
        explore = write(repo / "explores" / "retail.explore.lkml", "explore: retail {}\n")
        write(repo / "shared" / "all.lkml", 'include: "/explores/retail.explore.lkml"\n')
        write(repo / "models" / "unrelated.model.lkml", 'include: "/views/*.view.lkml"\n')
        write(repo / "core.model.lkml", include + "\n")
        assert find_models_including(str(repo), str(explore)) == (["core"] if found else [])

    def test_cli(self, tmp_path):
        repo = make_repo(tmp_path, extra_files=PREFIX_FILES)
        result = run_cli("--explore", "retail", "--topic-prefix", "model", "--looker-repo", repo,
                         "--omni-repo", tmp_path / "out" / "topics", cwd=tmp_path)
        assert result.returncode == 0, result.stdout + result.stderr
        assert (tmp_path / "out" / "topics" / "retail__retail.topic.yaml").exists()
        bad = run_cli("--view", "stores", "--phase", "0", "--owner", "Team A", "--topic-prefix", "x",
                      "--looker-repo", repo, cwd=tmp_path)
        assert bad.returncode == 2 and "--topic-prefix can only be used with --explore" in bad.stderr


# ----------------------------------------------------------------------
# J6: duplicate view names
# ----------------------------------------------------------------------

class TestDuplicateViewMessage:
    def test_message_lists_every_file(self, tmp_path):
        repo = tmp_path / "repo"
        files = [write(repo / path, "view: users { dimension: id {} }\n")
                 for path in ("a/users.view.lkml", "b/users.view.lkml", "c/people.view.lkml")]
        orch = MigrationOrchestrator(skill_path=SKILL_ROOT, looker_repo=repo, omni_repo=tmp_path / "out" / "views",
                                     csv_path=scope_csv(tmp_path, "users", ["id"]))
        result = orch.migrate_view("users", 0, "Team A")
        assert not result.success
        message = result.error_message
        assert "defined in more than one file (3 definitions)" in message
        for path in files:
            assert f"  - {path}" in message.splitlines()
        assert "Rename all but one" in message
        assert not (tmp_path / "out").exists()

    def test_two_definitions_in_one_file(self, tmp_path):
        repo = tmp_path / "repo"
        path = write(repo / "users.view.lkml", "view: users { dimension: id {} }\nview: users { dimension: id {} }\n")
        with pytest.raises(ValueError) as error:
            view_from_index(load_views(str(repo))[0], str(repo), "users")
        assert f"  - {path} (2 definitions in this file)" in str(error.value)
