"""
Follow-up rules: `convert_tz: no`, explore and join `fields:` restrictions,
named date periods in required filters, Liquid inside SQL comments and
`${view.SQL_TABLE_NAME}` that points at a plain table. Each test builds a
small LookML project in tmp_path.
"""

import csv as csv_module
import textwrap
from pathlib import Path

import pytest

from conftest import SKILL_ROOT, load_yaml
from lib.liquid_translator import LiquidContext, LiquidField, remove_liquid_comments, translate_liquid
from lib.omni_models import OmniDimension
from lib.orchestrator import MigrationOrchestrator
from lib.rule_applier import RuleApplier


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text))
    return path


def make_repo(tmp_path, views: str, model: str = "") -> Path:
    repo = tmp_path / "repo"
    write(repo / "views" / "shop.view.lkml", views)
    if model:
        write(repo / "shop.model.lkml", model)
    return repo


def scope_csv(tmp_path, view: str, fields) -> Path:
    path = tmp_path / "scope.csv"
    with open(path, "w", newline="") as handle:
        writer = csv_module.writer(handle)
        writer.writerow(["owner", "phase", "view", "field", "field_type"])
        for name in fields:
            writer.writerow(["Team A", "0", view, name, "dimension"])
    return path


def migrate_view(tmp_path, repo, view, fields, expect_success=True):
    orch = MigrationOrchestrator(skill_path=SKILL_ROOT, looker_repo=repo, omni_repo=tmp_path / "out" / "views",
                                 csv_path=scope_csv(tmp_path, view, fields))
    result = orch.migrate_view(view, 0, "Team A")
    if not expect_success:
        return result, None, None
    assert result.success, result.error_message
    report = (tmp_path / "out" / "reports" / f"{view}.migration.md").read_text()
    return result, load_yaml(result.view_file_path), report


def migrate_explore(tmp_path, repo, name, topic=None, **kwargs):
    topics = tmp_path / "out" / "topics"
    orch = MigrationOrchestrator(skill_path=SKILL_ROOT, looker_repo=repo, omni_repo=topics)
    result = orch.migrate_explore(name, **kwargs)
    assert result.success, result.error_message
    topic = topic or name
    report = (tmp_path / "out" / "reports" / f"{topic}_explore.md").read_text()
    return result, load_yaml(topics / f"{topic}.topic.yaml"), report


def section(report: str, heading: str) -> str:
    return report.split(f"\n## {heading}\n")[1].split("\n## ")[0]


def manual_item(report: str, title: str) -> str:
    return report.split(f"### {title}")[1].split("\n#")[0]


# ----------------------------------------------------------------------
# Z1: convert_tz: no
# ----------------------------------------------------------------------

TZ_VIEWS = """
view: visits {
  sql_table_name: shop.visits ;;
  dimension: id { primary_key: yes type: number sql: ${TABLE}.id ;; }
  dimension_group: visited {
    type: time
    datatype: date
    convert_tz: no
    timeframes: [raw, date, week]
    sql: ${TABLE}.visit_date ;;
  }
  dimension_group: updated { type: time convert_tz: yes sql: ${TABLE}.updated_at ;; }
  dimension_group: created { type: time sql: ${TABLE}.created_at ;; }
  dimension: local_day { type: date convert_tz: no sql: ${TABLE}.local_day ;; }
  dimension: code { type: string convert_tz: no sql: ${TABLE}.code ;; }
  dimension: visit_day_text { type: string sql: CAST(${visited_date} AS VARCHAR) ;; }
  dimension: is_recent { type: yesno sql: ${visits.visited_raw} > '2024-01-01' ;; }
  measure: count { type: count }
}

view: web_visits {
  extends: [visits]
  sql_table_name: shop.web_visits ;;
  dimension: visit_day_upper { type: string sql: UPPER(${visited_date}) ;; }
}
"""

TZ_MODEL = """
explore: visits {
  always_filter: { filters: [visits.visited_date: "7 days ago for 7 days"] }
}
"""

TZ_FIELDS = ["id", "visited", "updated", "created", "local_day", "code", "visit_day_text", "is_recent", "count"]


class TestConvertTz:
    @pytest.fixture
    def applier(self):
        return RuleApplier(phase=0)

    def test_group_is_written_with_convert_tz_false(self, applier):
        result = applier.convert_dimension_group(
            'visited', {'type': 'time', 'convert_tz': 'no', 'datatype': 'date', 'sql': '${TABLE}.visit_date'})
        assert result.omni_field.convert_tz is False
        assert result.omni_field.datatype is None
        assert not any('MANUAL' in w for w in result.warnings)
        assert any('convert_tz: no' in c and 'convert_tz: false' in c for c in result.changes)

    @pytest.mark.parametrize('extra', [{}, {'convert_tz': 'yes'}])
    def test_default_and_yes_write_nothing(self, applier, extra):
        result = applier.convert_dimension_group('created', {'type': 'time', 'sql': '${TABLE}.created_at', **extra})
        assert result.omni_field.convert_tz is None
        assert not any('convert_tz' in c for c in result.changes)

    def test_date_dimension(self, applier):
        result = applier.convert_dimension('local_day', {'type': 'date', 'convert_tz': 'no', 'sql': '${TABLE}.local_day'})
        assert result.omni_field.convert_tz is False

    def test_non_date_dimension_gets_no_key(self, applier):
        result = applier.convert_dimension('code', {'type': 'string', 'convert_tz': 'no', 'sql': '${TABLE}.code'})
        assert result.omni_field is not None and result.omni_field.convert_tz is None

    def test_model_serialises_false_and_omits_none(self):
        base = dict(label='A', description='a', tags=['mig_phase_0', 'verified'])
        assert OmniDimension(**base, convert_tz=False).model_dump(exclude_none=True)['convert_tz'] is False
        assert 'convert_tz' not in OmniDimension(**base).model_dump(exclude_none=True)

    @pytest.fixture
    def migrated(self, tmp_path):
        return migrate_view(tmp_path, make_repo(tmp_path, TZ_VIEWS, TZ_MODEL), "visits", TZ_FIELDS)

    def test_view_file(self, migrated):
        _, view, _ = migrated
        dims = view['dimensions']
        assert dims['visited']['convert_tz'] is False
        assert dims['visited']['sql'] == 'visit_date'
        assert 'datatype' not in dims['visited']
        assert dims['local_day']['convert_tz'] is False
        for name in ('updated', 'created', 'code', 'id'):
            assert 'convert_tz' not in dims[name]

    def test_dependent_fields_convert(self, migrated):
        _, view, report = migrated
        assert view['dimensions']['visit_day_text']['sql'] == 'CAST(${visited[date]} AS VARCHAR)'
        assert view['dimensions']['is_recent']['sql'] == "${visits.visited[raw]} > '2024-01-01'"
        assert "which is not migrated" not in report

    def test_report_lists_it_as_a_change_not_a_manual_item(self, migrated):
        result, _, report = migrated
        assert "visited (dimension): convert_tz: no → convert_tz: false" in report
        assert "local_day (dimension): convert_tz: no → convert_tz: false" in report
        assert "check Omni's timezone handling" not in report
        assert "(none)" in section(report, "Not Converted")
        assert not any('convert_tz' in issue for issue in result.issues)

    def test_inherited_group(self, tmp_path):
        repo = make_repo(tmp_path, TZ_VIEWS, TZ_MODEL)
        _, view, report = migrate_view(tmp_path, repo, "web_visits", ["visit_day_upper"])
        assert view['dimensions']['visit_day_upper']['sql'] == 'UPPER(${visited[date]})'
        assert "which is not migrated" not in report

    def test_required_filter_on_the_group_is_carried(self, tmp_path):
        _, topic, report = migrate_explore(tmp_path, make_repo(tmp_path, TZ_VIEWS, TZ_MODEL), "visits")
        assert topic['default_filters']['visits.visited'] == {
            'time_for_duration': ['7 complete days ago', '7 days'], 'required': True}
        assert "always_filter (NOT carried)" not in report


# ----------------------------------------------------------------------
# J4: explore and join `fields:` restrictions
# ----------------------------------------------------------------------

FIELD_VIEWS = """
view: base_person {
  extension: required
  set: contact { fields: [name, email] }
  dimension: id { primary_key: yes type: number sql: ${TABLE}.id ;; }
  dimension: name { sql: ${TABLE}.name ;; }
  dimension: email { sql: ${TABLE}.email ;; }
}

view: users {
  extends: [base_person]
  sql_table_name: shop.users ;;
  set: profile { fields: [id, contact*, signup_date, signup_week] }
  set: broken { fields: [id, missing*] }
  dimension: city { sql: ${TABLE}.city ;; }
  dimension: age_tier { type: tier tiers: [0, 18] sql: ${TABLE}.age ;; }
  dimension_group: signup { type: time timeframes: [date, week] sql: ${TABLE}.signup_at ;; }
  dimension_group: seen { type: time sql: ${TABLE}.seen_at ;; }
  parameter: unit { type: string }
}

view: orders {
  sql_table_name: shop.orders ;;
  dimension: id { primary_key: yes type: number sql: ${TABLE}.id ;; }
  dimension: user_id { type: number sql: ${TABLE}.user_id ;; }
  dimension: status { sql: ${TABLE}.status ;; }
  dimension: cost { type: number sql: ${TABLE}.cost ;; }
  dimension: buyer_city { sql: ${buyer.city} ;; }
  measure: count { type: count }
}
"""


def field_model(explore_body: str, join_body: str = "", extra: str = "") -> str:
    return f"""
explore: orders {{
  {explore_body}
  join: users {{
    relationship: many_to_one
    sql_on: ${{orders.user_id}} = ${{users.id}} ;;
    {join_body}
  }}
}}
{extra}
"""


def topic_for(tmp_path, explore_body="", join_body="", extra="", name="orders"):
    repo = make_repo(tmp_path, FIELD_VIEWS, field_model(explore_body, join_body, extra))
    return migrate_explore(tmp_path, repo, name)


class TestFieldRestrictions:
    def test_no_restriction_keeps_the_default(self, tmp_path):
        _, topic, report = topic_for(tmp_path)
        assert topic['fields'] == ['all_views.*', '-tag:table_key']
        assert "## Field restrictions" not in report
        assert "Topic uses `all_views.*`" in report

    def test_explore_exclusions(self, tmp_path):
        result, topic, report = topic_for(tmp_path, "fields: [ALL_FIELDS*, -orders.cost, -users.email]")
        assert topic['fields'] == ['all_views.*', '-orders.cost', '-users.email', '-tag:table_key']
        assert "NOT carried" not in report
        assert "explore `fields: [ALL_FIELDS*, -orders.cost, -users.email]` → topic `fields`" in \
            section(report, "Carried Into the Topic")
        assert "Looker hides the restricted fields; Omni rejects queries that use them" in \
            manual_item(report, "Field restrictions (carried)")
        assert any(issue.startswith("Field restrictions (carried)") for issue in result.issues)

    def test_join_field_list(self, tmp_path):
        _, topic, report = topic_for(tmp_path, join_body="fields: [users.id, users.name]")
        assert topic['fields'] == ['all_views.*', '-users.*', 'users.id', 'users.name', '-tag:table_key']
        restrictions = section(report, "Field restrictions")
        assert "Looker join `users`: `fields: [users.id, users.name]`" in restrictions
        assert "Topic: `fields: [all_views.*, -users.*, users.id, users.name, -tag:table_key]`" in restrictions
        assert "Omni rejects queries that use it" in restrictions

    def test_join_with_no_fields(self, tmp_path):
        _, topic, _ = topic_for(tmp_path, join_body="fields: []")
        assert topic['fields'] == ['all_views.*', '-users.*', '-tag:table_key']

    def test_join_exclusions_only(self, tmp_path):
        _, topic, _ = topic_for(tmp_path, join_body="fields: [-users.email, -city]")
        assert topic['fields'] == ['all_views.*', '-users.email', '-users.city', '-tag:table_key']

    def test_names_are_the_aliases_of_the_topic(self, tmp_path):
        model = """
        explore: purchases {
          from: orders
          fields: [ALL_FIELDS*, -purchases.cost]
          join: buyer {
            from: users
            relationship: many_to_one
            sql_on: ${purchases.user_id} = ${buyer.id} ;;
            fields: [buyer.name]
          }
        }
        """
        _, topic, _ = migrate_explore(tmp_path, make_repo(tmp_path, FIELD_VIEWS, model), "purchases")
        assert topic['fields'] == ['all_views.*', '-purchases.cost', '-buyer.*', 'buyer.name', '-tag:table_key']

    def test_sets_are_expanded_with_nested_and_inherited_sets(self, tmp_path):
        _, topic, report = topic_for(tmp_path, join_body="fields: [users.profile*]")
        assert topic['fields'] == ['all_views.*', '-users.*', 'users.id', 'users.name', 'users.email',
                                   'users.signup', '-tag:table_key']
        assert "`users.signup_date` → `users.signup`" in section(report, "Field restrictions")

    def test_unresolved_set_is_not_carried(self, tmp_path):
        result, topic, report = topic_for(tmp_path, "fields: [ALL_FIELDS*, -orders.cost]",
                                          join_body="fields: [users.broken*]")
        assert topic['fields'] == ['all_views.*', '-orders.cost', '-tag:table_key']
        item = manual_item(report, "Join field restriction on users (NOT carried)")
        assert "set `users.missing*` is not defined" in item and "[users.broken*]" in item
        assert any(issue.startswith("Join field restriction on users (NOT carried)") for issue in result.issues)

    def test_excluding_every_timeframe_excludes_the_dimension(self, tmp_path):
        _, topic, _ = topic_for(tmp_path, "fields: [ALL_FIELDS*, -users.signup_date, -users.signup_week]")
        assert topic['fields'] == ['all_views.*', '-users.signup', '-tag:table_key']

    @pytest.mark.parametrize('excluded', ['-users.signup_date', '-users.seen_date'])
    def test_excluding_some_timeframes_is_not_carried(self, tmp_path, excluded):
        _, topic, report = topic_for(tmp_path, f"fields: [ALL_FIELDS*, -orders.cost, {excluded}]")
        assert topic['fields'] == ['all_views.*', '-tag:table_key']
        assert "only some timeframes of dimension group" in manual_item(report, "Explore field restriction (NOT carried)")

    def test_field_the_topic_will_not_contain_is_dropped_and_noted(self, tmp_path):
        _, topic, report = topic_for(tmp_path, join_body="fields: [users.id, users.age_tier, users.nothing]")
        assert topic['fields'] == ['all_views.*', '-users.*', 'users.id', '-tag:table_key']
        notes = section(report, "Field restrictions")
        assert "`users.age_tier`: dropped, the topic will not contain it" in notes
        assert "`users.nothing`: dropped, view `users` has no such field" in notes

    def test_filter_only_field_is_left_out_with_a_verify_note(self, tmp_path):
        _, topic, report = topic_for(tmp_path, join_body="fields: [users.id, users.unit]")
        assert topic['fields'] == ['all_views.*', '-users.*', 'users.id', '-tag:table_key']
        assert "`users.unit` is a filter-only field" in manual_item(report, "Field restrictions (carried)")

    def test_explore_list_of_fields_only(self, tmp_path):
        _, topic, _ = topic_for(tmp_path, "fields: [orders.id, orders.count, users.contact*, -users.email]",
                                join_body="fields: [-users.city]")
        assert topic['fields'] == ['-all_views.*', 'orders.id', 'orders.count', 'users.name', '-tag:table_key']

    def test_explore_list_meets_a_join_list(self, tmp_path):
        _, topic, _ = topic_for(tmp_path, "fields: [orders.id, users.name, users.city]",
                                join_body="fields: [users.name, users.email]")
        assert topic['fields'] == ['-all_views.*', 'orders.id', 'users.name', '-tag:table_key']

    def test_explore_exclusion_removes_a_field_from_a_join_list(self, tmp_path):
        _, topic, _ = topic_for(tmp_path, "fields: [ALL_FIELDS*, -users.email]",
                                join_body="fields: [users.name, users.email]")
        assert topic['fields'] == ['all_views.*', '-users.*', 'users.name', '-tag:table_key']

    @pytest.mark.parametrize('body, reason', [
        ("fields: [-orders.cost]", "no `ALL_FIELDS*`"),
        ("fields: [ALL_FIELDS*, -cost]", "not view-qualified"),
        ("fields: [ALL_FIELDS*, -payments.id]", "not a view of the explore"),
    ])
    def test_explore_forms_that_are_not_carried(self, tmp_path, body, reason):
        _, topic, report = topic_for(tmp_path, body)
        assert topic['fields'] == ['all_views.*', '-tag:table_key']
        assert reason in manual_item(report, "Explore field restriction (NOT carried)")

    def test_join_list_naming_another_view_is_not_carried(self, tmp_path):
        _, topic, report = topic_for(tmp_path, join_body="fields: [orders.id]")
        assert topic['fields'] == ['all_views.*', '-tag:table_key']
        assert "not this join" in manual_item(report, "Join field restriction on users (NOT carried)")

    def test_field_written_in_the_topic_views_block_counts_as_present(self, tmp_path):
        model = """
        explore: orders {
          fields: [orders.id, orders.buyer_city]
          join: buyer {
            from: users
            relationship: many_to_one
            sql_on: ${orders.user_id} = ${buyer.id} ;;
          }
        }
        """
        _, topic, _ = migrate_explore(tmp_path, make_repo(tmp_path, FIELD_VIEWS, model), "orders")
        assert 'buyer_city' in topic['views']['orders']['dimensions']
        assert topic['fields'] == ['-all_views.*', 'orders.id', 'orders.buyer_city', '-tag:table_key']

    def test_restriction_on_a_join_that_was_not_migrated(self, tmp_path):
        model = """
        explore: orders {
          fields: [ALL_FIELDS*, -orders.cost, -users.email]
          join: users { relationship: many_to_one foreign_key: user_id fields: [users.id] }
        }
        """
        _, topic, report = migrate_explore(tmp_path, make_repo(tmp_path, FIELD_VIEWS, model), "orders")
        assert topic['fields'] == ['all_views.*', '-orders.cost', '-tag:table_key']
        assert "`-users.email`: dropped, join `users` was not migrated" in section(report, "Field restrictions")

    def test_extends_child_explore_list_overrides_and_join_list_is_inherited(self, tmp_path):
        extra = """
        explore: recent_orders {
          extends: [orders]
          view_name: orders
          fields: [ALL_FIELDS*, -orders.status]
        }
        explore: plain_orders {
          extends: [orders]
          view_name: orders
          join: users { fields: [] }
        }
        """
        repo = make_repo(tmp_path, FIELD_VIEWS, field_model(
            "fields: [ALL_FIELDS*, -orders.cost]", "fields: [users.id]", extra))
        _, child, _ = migrate_explore(tmp_path, repo, "recent_orders")
        assert child['fields'] == ['all_views.*', '-orders.status', '-users.*', 'users.id', '-tag:table_key']
        _, plain, _ = migrate_explore(tmp_path, repo, "plain_orders")
        assert plain['fields'] == ['all_views.*', '-orders.cost', '-users.*', '-tag:table_key']


# ----------------------------------------------------------------------
# A3: named date periods in required filters
# ----------------------------------------------------------------------

PERIOD_VIEWS = """
view: orders {
  sql_table_name: shop.orders ;;
  dimension: id { primary_key: yes type: number sql: ${TABLE}.id ;; }
  dimension: status { sql: ${TABLE}.status ;; }
  dimension: delivered_on { type: date sql: ${TABLE}.delivered_on ;; }
  dimension_group: created { type: time timeframes: [raw, date, month] sql: ${TABLE}.created_at ;; }
}
"""

NAMED_PERIODS = ["last month", "this month", "last week", "this week", "last year", "this year", "last quarter",
                 "this quarter", "today", "yesterday"]


def period_topic(tmp_path, filters: str):
    model = f"explore: orders {{ always_filter: {{ filters: [{filters}] }} }}"
    return migrate_explore(tmp_path, make_repo(tmp_path, PERIOD_VIEWS, model), "orders")


class TestNamedDatePeriods:
    @pytest.mark.parametrize('period', NAMED_PERIODS)
    def test_named_period_is_written(self, tmp_path, period):
        _, topic, report = period_topic(tmp_path, f'orders.created_date: "{period}"')
        assert topic['default_filters'] == {'orders.created': {'is': period, 'required': True}}
        assert "always_filter (NOT carried)" not in report

    def test_last_month_needs_no_verify_item(self, tmp_path):
        _, _, report = period_topic(tmp_path, 'orders.created_date: "last month"')
        assert "Named date periods (verify)" not in report

    def test_other_periods_get_a_verify_item(self, tmp_path):
        result, _, report = period_topic(
            tmp_path, 'orders.created_date: "This  Week", orders.delivered_on: "yesterday"')
        verify = manual_item(report, "Named date periods (verify)")
        assert "`orders.created`: `is: this week`" in verify
        assert "`orders.delivered_on`: `is: yesterday`" in verify
        assert any(issue.startswith("Named date periods (verify)") for issue in result.issues)

    def test_named_period_on_a_string_field_is_not_written(self, tmp_path):
        _, topic, report = period_topic(tmp_path, 'orders.status: "last month"')
        assert 'default_filters' not in topic
        assert "outside the forms" in manual_item(report, "always_filter (NOT carried)")

    @pytest.mark.parametrize('expression, expected', [
        ("1 days ago for 1 days", ["1 complete days ago", "1 days"]),
        ("1 day ago for 1 day", ["1 complete days ago", "1 days"]),
        ("1 month ago for 1 month", ["1 complete months ago", "1 months"]),
        ("2 week ago for 2 weeks", ["2 complete weeks ago", "2 weeks"]),
    ])
    def test_single_period_durations(self, tmp_path, expression, expected):
        _, topic, _ = period_topic(tmp_path, f'orders.created_date: "{expression}"')
        assert topic['default_filters'] == {'orders.created': {'time_for_duration': expected, 'required': True}}

    @pytest.mark.parametrize('expression', ["next month", "last 2 months", "this month to date", "0 days ago for 0 days",
                                            "1 day ago for 1 week", "tomorrow"])
    def test_other_expressions_stay_manual(self, tmp_path, expression):
        _, topic, report = period_topic(tmp_path, f'orders.created_date: "{expression}"')
        assert 'default_filters' not in topic
        assert "always_filter (NOT carried)" in report


# ----------------------------------------------------------------------
# L10: Liquid inside SQL comments
# ----------------------------------------------------------------------

def resolve_field(ref):
    name = ref.rpartition('.')[2]
    kinds = {'period': ('filter', 'date', 'timestamp'), 'floor': ('parameter', 'number', 'number')}
    if name not in kinds:
        return None, f"`{ref}` is not a field"
    kind, looker_type, omni_type = kinds[name]
    return LiquidField(view='sales', name=name, kind=kind, looker_type=looker_type, origin='sales', local=True,
                       omni_type=omni_type), None


CONTEXT = LiquidContext(field=resolve_field, view=lambda name: (None, "not a view"))
REMOVED = "SQL comment containing Liquid removed"


class TestLiquidInComments:
    def test_line_comment_is_removed(self):
        translation = translate_liquid("SELECT a -- AND {% if x %} b {% endif %}\nFROM t", CONTEXT)
        assert translation.sql == "SELECT a\nFROM t"
        assert translation.translated == [f"{REMOVED}: `-- AND {{% if x %}} b {{% endif %}}`"]

    def test_whole_line_comment_takes_its_line(self):
        sql = "SELECT a\n  -- , {% parameter floor %} AS floor\nFROM t\n  /* {{ x | upcase }} */\nWHERE 1 = 1"
        assert translate_liquid(sql, CONTEXT).sql == "SELECT a\nFROM t\nWHERE 1 = 1"

    def test_block_comment_keeps_the_tokens_apart(self):
        assert translate_liquid("SELECT a/* {% assign y = 1 %} */FROM t", CONTEXT).sql == "SELECT a FROM t"
        assert translate_liquid("SELECT a /* {{ y }} */ FROM t", CONTEXT).sql == "SELECT a  FROM t"

    def test_comment_without_liquid_is_kept_and_code_liquid_is_translated(self):
        sql = ("SELECT a -- plain note\nFROM t /* keep */\n"
               "WHERE {% condition period %} d {% endcondition %} -- old: {% date_start period %}\n")
        translation = translate_liquid(sql, CONTEXT)
        assert translation.sql == ("SELECT a -- plain note\nFROM t /* keep */\n"
                                   "WHERE {{# sales.period.filter }} d {{/ sales.period.filter }}\n")
        assert translation.translated[0].startswith(REMOVED)
        assert len(translation.translated) == 3
        assert [target.name for target in translation.fields] == ['period']

    def test_commented_out_block_over_several_lines(self):
        sql = "SELECT a\n-- {% if x %}\n-- AND b = 1\n-- {% endif %}\nFROM t"
        translation = translate_liquid(sql, CONTEXT)
        assert translation.sql == "SELECT a\n-- AND b = 1\nFROM t"
        assert len(translation.translated) == 2

    @pytest.mark.parametrize('sql, named', [
        ("SELECT a\n-- {% if x %}\nAND b = 1\n-- {% endif %}\nFROM t", "`{% if %}`"),
        ("SELECT a /* {% if x %} */ , b /* {% endif %} */ FROM t", "`{% if %}`"),
        ("SELECT {% if x %} a -- {% else %}\n b {% endif %} FROM t", "`{% else %}`"),
        ("SELECT a -- {% endif %}\nFROM t", "`{% endif %}`"),
        ("SELECT a\n-- {% condition period %} d\n{% endcondition %}", "`{% condition %}`"),
    ])
    def test_block_tag_that_reaches_outside_the_comment_blocks(self, sql, named):
        translation = translate_liquid(sql, CONTEXT)
        assert translation.sql is None
        assert named in translation.reason and "not opened and closed inside the comment" in translation.reason

    def test_optimizer_hint_is_not_removed(self):
        translation = translate_liquid("SELECT /*+ {{ hint }} */ a FROM t", CONTEXT)
        assert translation.sql is None and "optimizer hint" in translation.reason

    @pytest.mark.parametrize('sql', ["SELECT '{% if x %}a{% endif %}'", 'SELECT "{{ col }}" FROM t',
                                     "SELECT '-- {% parameter floor %}' FROM t"])
    def test_liquid_in_a_string_or_identifier_still_blocks(self, sql):
        assert translate_liquid(sql, CONTEXT).sql is None

    def test_other_liquid_in_code_still_blocks_after_the_comment_is_removed(self):
        translation = translate_liquid("SELECT a -- {{ y }}\n{% if x %} , b {% endif %} FROM t", CONTEXT)
        assert translation.sql is None and "`{% if %}`" in translation.reason

    def test_sql_without_liquid_in_comments_is_untouched(self):
        sql = "SELECT a -- note\nFROM t /* x */"
        assert remove_liquid_comments(sql) == (sql, [], [])

    LKML = """
    view: sales {
      derived_table: {
        sql: SELECT user_id, amount
          FROM shop.order_items
          -- WHERE {% if period._is_filtered %} 1 = 1 {% endif %}
          WHERE {% condition period %} created_at {% endcondition %}
            /* AND amount >= {% parameter old_floor %} */
            AND amount >= {% parameter floor %} ;;
      }
      filter: period { type: date }
      parameter: floor { type: number }
      parameter: old_floor { type: unquoted }
      dimension: user_id { primary_key: yes type: number sql: ${TABLE}.user_id ;; }
      dimension: amount {
        type: number
        sql: ${TABLE}.amount /* was: {{ amount._value | times: 2 }} */ ;;
      }
    }
    """

    @pytest.fixture
    def migrated(self, tmp_path):
        return migrate_view(tmp_path, make_repo(tmp_path, self.LKML), "sales", ["user_id", "amount"])

    def test_derived_table_with_commented_liquid_converts(self, migrated):
        result, view, report = migrated
        assert result.view_file_path.name == "sales.query.view"
        assert view['sql'] == (
            "SELECT user_id, amount\nFROM shop.order_items\n"
            "WHERE {{# sales.period.filter }} created_at {{/ sales.period.filter }}\n"
            "  AND amount >= {{ filters.sales.floor.value }}"
        )
        assert set(view['filters']) == {'period', 'floor', 'old_floor'}
        assert view['filters']['old_floor']['type'] == 'string'

    def test_field_sql_with_commented_liquid_converts(self, migrated):
        _, view, _ = migrated
        assert 'sql' not in view['dimensions']['amount']

    def test_report_lists_the_removed_comments(self, migrated):
        _, _, report = migrated
        translated = section(report, "Liquid translated")
        assert translated.count(REMOVED) == 3
        assert f"{REMOVED}: `/* AND amount >= {{% parameter old_floor %}} */`" in translated


# ----------------------------------------------------------------------
# S1: ${view.SQL_TABLE_NAME} that points at a plain table
# ----------------------------------------------------------------------

TABLE_VIEWS = """
view: orders {
  sql_table_name: shop.orders ;;
  dimension: id { primary_key: yes type: number sql: ${TABLE}.id ;; }
}

view: base_users {
  extension: required
  sql_table_name: `analytics-prod.crm.users` ;;
  dimension: id { primary_key: yes type: number sql: ${TABLE}.id ;; }
}

view: users { extends: [base_users] }

view: order_facts {
  derived_table: { sql: SELECT 1 AS id ;; }
  dimension: id { primary_key: yes type: number sql: ${TABLE}.id ;; }
}

view: order_facts_child { extends: [order_facts] }

view: regional { sql_table_name: shop.{{ _user_attributes['region'] }}_orders ;; }

view: constant_table { sql_table_name: "@{schema}.orders" ;; }

view: no_table { dimension: id { sql: ${TABLE}.id ;; } }
"""


def table_view(tmp_path, sql: str, expect_success=True):
    views = TABLE_VIEWS + f"""
view: summary {{
  derived_table: {{
    sql: {sql} ;;
  }}
  dimension: id {{ primary_key: yes type: number sql: ${{TABLE}}.id ;; }}
}}
"""
    return migrate_view(tmp_path, make_repo(tmp_path, views), "summary", ["id"], expect_success=expect_success)


class TestSqlTableNameReferences:
    def test_plain_table_is_substituted(self, tmp_path):
        result, view, report = table_view(
            tmp_path, "SELECT o.id FROM ${orders.SQL_TABLE_NAME} o JOIN ${ orders.SQL_TABLE_NAME } p ON o.id = p.id")
        assert ' '.join(view['sql'].split()) == "SELECT o.id FROM shop.orders o JOIN shop.orders p ON o.id = p.id"
        assert view['schema'] == 'shop'
        assert "`${orders.SQL_TABLE_NAME}` → `shop.orders`" in report
        assert "Table References Substituted" in report

    def test_table_inherited_through_extends(self, tmp_path):
        _, view, report = table_view(tmp_path, "SELECT id FROM ${users.SQL_TABLE_NAME}")
        assert ' '.join(view['sql'].split()) == "SELECT id FROM `analytics-prod.crm.users`"
        assert "`${users.SQL_TABLE_NAME}` → `` `analytics-prod.crm.users` `` (inherited from `base_users`)" in report

    def test_with_translated_liquid(self, tmp_path):
        views = TABLE_VIEWS + """
view: summary {
  derived_table: {
    sql: SELECT id FROM ${orders.SQL_TABLE_NAME}
      -- FROM ${order_facts.SQL_TABLE_NAME} WHERE {% if a %} 1 {% endif %}
      WHERE {% condition period %} created_at {% endcondition %} ;;
  }
  filter: period { type: date }
  dimension: id { primary_key: yes type: number sql: ${TABLE}.id ;; }
}
"""
        _, view, _ = migrate_view(tmp_path, make_repo(tmp_path, views), "summary", ["id"])
        assert view['sql'] == ("SELECT id FROM shop.orders\n"
                               "WHERE {{# summary.period.filter }} created_at {{/ summary.period.filter }}")

    @pytest.mark.parametrize('name, case', [
        ("order_facts", "is itself a derived table"),
        ("order_facts_child", "is itself a derived table"),
        ("ghost", "was not found under --looker-repo"),
        ("regional", "is templated"),
        ("constant_table", "is templated"),
        ("no_table", "has no `sql_table_name`"),
        ("summary", "is itself a derived table"),
    ])
    def test_other_references_stay_blocked_and_say_why(self, tmp_path, name, case):
        result, _, _ = table_view(tmp_path, f"SELECT id FROM ${{{name}.SQL_TABLE_NAME}}", expect_success=False)
        assert not result.success
        assert "MANUAL ACTION REQUIRED" in result.error_message
        assert f"${{{name}.SQL_TABLE_NAME}}" in result.error_message and case in result.error_message
        assert not (tmp_path / "out").exists()

    def test_one_blocked_reference_blocks_the_view(self, tmp_path):
        result, _, _ = table_view(
            tmp_path, "SELECT 1 FROM ${orders.SQL_TABLE_NAME} JOIN ${order_facts.SQL_TABLE_NAME} USING (id)",
            expect_success=False)
        assert not result.success and "order_facts" in result.error_message

    def test_other_references_still_block(self, tmp_path):
        result, _, _ = table_view(tmp_path, "SELECT ${orders.id} FROM ${orders.SQL_TABLE_NAME}",
                                  expect_success=False)
        assert not result.success and "${...} references" in result.error_message

    def test_view_with_refinements_is_not_substituted(self, tmp_path):
        repo = make_repo(tmp_path, TABLE_VIEWS + """
view: summary {
  derived_table: { sql: SELECT id FROM ${orders.SQL_TABLE_NAME} ;; }
  dimension: id { primary_key: yes type: number sql: ${TABLE}.id ;; }
}
""")
        write(repo / "refine.lkml", "view: +orders { sql_table_name: shop.orders_v2 ;; }")
        result, _, _ = migrate_view(tmp_path, repo, "summary", ["id"], expect_success=False)
        assert not result.success and "refinement" in result.error_message
