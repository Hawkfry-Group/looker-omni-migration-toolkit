"""
Liquid to Omni Mustache: the tokenizer and translator (L1, L4 to L9), the
filter-only fields written for Looker `filter:` and `parameter:` (L2, L3),
and the output rules for templated SQL. Integration tests build a small
LookML project in tmp_path; the CLI test uses the thelook fixture.
"""

import textwrap
from pathlib import Path

import pytest
from pydantic import ValidationError

from conftest import FIXTURE_REPO, SCOPE_CSV, SKILL_ROOT, load_yaml, run_cli
from lib.file_generators import FileGenerator
from lib.liquid_translator import (
    LiquidContext,
    LiquidField,
    assert_no_looker_liquid,
    find_looker_liquid,
    scan_liquid,
    translate_liquid,
)
from lib.looker_parser import effective_view, load_views, resolve_view_extends
from lib.omni_models import OmniDimension, OmniFilter, OmniView
from lib.orchestrator import MigrationOrchestrator
from lib.rule_applier import RuleApplier

CSV_HEADER = "phase,view,field,type,omni_view,omni_field,owner,status,notes\n"

# Fields the fake project of the unit tests knows, all in view `orders`
FIELDS = {
    'status_pick': ('filter', 'string', 'string'),
    'order_date': ('filter', 'date', 'timestamp'),
    'min_price': ('parameter', 'number', 'number'),
    'label_pick': ('parameter', 'string', 'string'),
    'flag': ('parameter', 'yesno', 'boolean'),
    'col_pick': ('parameter', 'unquoted', 'column'),
    'text_pick': ('parameter', 'unquoted', 'string'),
    'status': ('dimension', 'string', None),
    'created': ('dimension_group', 'time', None),
    'total': ('measure', 'measure', None),
}
VIEWS = {'orders': 'orders', 'customers': 'users', 'users': 'users'}


def resolve_field(ref):
    view, _, name = ref.rpartition('.')
    view = view or 'orders'
    if view not in VIEWS:
        return None, f"`{view}` is not a view"
    timeframe = None
    if name == 'created_date':
        name, timeframe = 'created', 'date'
    if name not in FIELDS or name == 'created' and timeframe is None:
        return None, f"`{ref}` is not a field of view `{VIEWS[view]}`"
    kind, looker_type, omni_type = FIELDS[name]
    return LiquidField(view=VIEWS[view], name=name, kind=kind, looker_type=looker_type, origin=VIEWS[view],
                       local=VIEWS[view] == 'orders', omni_type=omni_type, timeframe=timeframe), None


def resolve_view(name):
    return (VIEWS[name], None) if name in VIEWS else (None, f"`{name}` is not a view")


CONTEXT = LiquidContext(field=resolve_field, view=resolve_view)


def translate(sql):
    return translate_liquid(sql, CONTEXT)


def blocked(sql):
    translation = translate(sql)
    assert translation.sql is None, translation.sql
    assert translation.blockers
    return translation.reason


# ----------------------------------------------------------------------
# Tokenizer
# ----------------------------------------------------------------------

class TestScan:
    def test_finds_tags_and_outputs_in_order(self):
        constructs, problems = scan_liquid("a {% condition f %} b {% endcondition %} {{ v.f._in_query }}")
        assert [c.kind for c in constructs] == ['condition', 'endcondition', 'in_query']
        assert [c.ref for c in constructs] == ['f', None, 'v.f']
        assert problems == []

    def test_sql_context_of_each_construct(self):
        sql = "SELECT '{% parameter a %}', \"{% parameter b %}\", 'x{% parameter c %}' -- {% parameter d %}\n, {% parameter e %}"
        by_ref = {c.ref: c for c in scan_liquid(sql)[0]}
        assert (by_ref['a'].context, by_ref['a'].quote, by_ref['a'].wrapped) == ('quoted', "'", True)
        assert (by_ref['b'].quote, by_ref['b'].wrapped) == ('"', True)
        assert (by_ref['c'].context, by_ref['c'].wrapped) == ('quoted', False)
        assert by_ref['d'].context == 'comment'
        assert by_ref['e'].context == 'code'

    def test_quotes_inside_a_tag_do_not_change_the_sql_state(self):
        constructs, _ = scan_liquid("x = {{ _user_attributes['a'] }} AND y = {% parameter p %}")
        assert [c.context for c in constructs] == ['code', 'code']

    def test_escaped_quote_keeps_the_string_open(self):
        constructs, _ = scan_liquid("'it''s {% parameter p %}' || {% parameter q %}")
        assert [(c.context, c.wrapped) for c in constructs] == [('quoted', False), ('code', False)]

    def test_two_tags_in_one_string_are_not_wrapped(self):
        constructs, _ = scan_liquid("'{% parameter a %}{% parameter b %}'")
        assert [c.wrapped for c in constructs] == [False, False]

    @pytest.mark.parametrize("sql", ["a {% condition f %} b {% endcondition", "x = {{ v.f._in_query"])
    def test_unclosed_tag_is_a_problem(self, sql):
        assert scan_liquid(sql)[1]
        assert "never closed" in blocked(sql)


# ----------------------------------------------------------------------
# L1. Condition blocks
# ----------------------------------------------------------------------

class TestCondition:
    def test_condition_becomes_a_filter_section(self):
        translation = translate("WHERE {% condition orders.status_pick %} o.status {% endcondition %}")
        assert translation.sql == ("WHERE {{# orders.status_pick.filter }} o.status "
                                   "{{/ orders.status_pick.filter }}")
        assert translation.translated[0] == ("`{% condition orders.status_pick %}` → "
                                             "`{{# orders.status_pick.filter }}`")

    def test_unqualified_reference_names_the_view(self):
        assert "{{# orders.status_pick.filter }}" in translate("{% condition status_pick %} s {% endcondition %}").sql

    def test_whitespace_control_is_accepted(self):
        sql = translate("{%- condition status_pick -%} s {%- endcondition -%}").sql
        assert sql == "{{# orders.status_pick.filter }} s {{/ orders.status_pick.filter }}"

    def test_alias_becomes_the_view_it_points_to(self):
        # `customers` is an alias of view `users` in the fake project
        assert "{{# users.status.filter }}" in translate("{% condition customers.status %} s {% endcondition %}").sql

    def test_dimension_group_timeframe_names_the_group(self):
        assert "{{# orders.created.filter }}" in translate("{% condition created_date %} c {% endcondition %}").sql

    def test_unknown_view_is_not_converted(self):
        assert "`nowhere` is not a view" in blocked("{% condition nowhere.f %} s {% endcondition %}")

    def test_unknown_field_is_not_converted(self):
        assert "not a field" in blocked("{% condition orders.nope %} s {% endcondition %}")

    @pytest.mark.parametrize("sql", [
        "{% condition status_pick %} s",
        "s {% endcondition %}",
        "{% condition status_pick %} {% parameter min_price %} {% endcondition %}",
        "{% condition status_pick %} {% condition order_date %} a {% endcondition %} {% endcondition %}",
        "{% condition status_pick %}   {% endcondition %}",
        "{% condition %} s {% endcondition %}",
        "'{% condition status_pick %} s {% endcondition %}'",
    ])
    def test_malformed_or_nested_conditions_are_not_converted(self, sql):
        assert "condition" in blocked(sql)

    def test_measure_cannot_be_a_condition_target(self):
        assert "is a measure" in blocked("{% condition total %} x {% endcondition %}")


# ----------------------------------------------------------------------
# L4. Parameter values
# ----------------------------------------------------------------------

class TestParameterValue:
    @pytest.mark.parametrize("looker", ["{% parameter min_price %}", "{{ min_price._parameter_value }}",
                                        "{% parameter orders.min_price %}", "{{- orders.min_price._parameter_value -}}"])
    def test_parameter_value_token(self, looker):
        assert translate(f"price >= {looker}").sql == "price >= {{ filters.orders.min_price.value }}"

    def test_quotes_directly_around_a_string_parameter_are_dropped(self):
        # Omni renders a string value already quoted
        assert translate("x = '{% parameter label_pick %}'").sql == "x = {{ filters.orders.label_pick.value }}"

    def test_bare_string_parameter_keeps_its_place(self):
        assert translate("x = {% parameter label_pick %}").sql == "x = {{ filters.orders.label_pick.value }}"

    @pytest.mark.parametrize("sql", [
        "x LIKE '%{% parameter label_pick %}%'",       # embedded in a longer string
        "x = 'a {% parameter label_pick %}'",
        "x = \"col_{% parameter label_pick %}\"",       # embedded in a longer identifier
        "x = '{% parameter label_pick %}{% parameter label_pick %}'",
    ])
    def test_tag_embedded_in_a_longer_quoted_text_is_not_converted(self, sql):
        assert "embedded in a longer quoted string or identifier" in blocked(sql)

    def test_quoted_number_parameter_is_not_converted(self):
        # '5' is a string in Looker, Omni would render a bare 5
        assert "wrapped in ' quotes" in blocked("x = '{% parameter min_price %}'")

    def test_double_quoted_string_parameter_is_not_converted(self):
        assert 'wrapped in " quotes' in blocked('x = "{% parameter label_pick %}"')

    def test_parameter_tag_needs_a_parameter(self):
        assert "is a filter" in blocked("x = {% parameter status_pick %}")
        assert "is a dimension" in blocked("x = {{ status._parameter_value }}")

    def test_yesno_parameter_value_is_not_converted(self):
        assert "not confirmed" in blocked("x = {% parameter flag %}")

    def test_parameter_in_a_comment_goes_with_the_comment(self):
        translation = translate("SELECT 1 -- {% parameter min_price %}")
        assert translation.sql == "SELECT 1"
        assert translation.fields == []


# ----------------------------------------------------------------------
# L5. Unquoted parameters
# ----------------------------------------------------------------------

class TestUnquotedParameter:
    @pytest.mark.parametrize("looker,omni", [
        ("SELECT ${TABLE}.{% parameter col_pick %}", "SELECT {{ filters.orders.col_pick.value }}"),
        ("SELECT t.{% parameter col_pick %} FROM t", "SELECT t.{{ filters.orders.col_pick.value }} FROM t"),
        ("SELECT * FROM {% parameter col_pick %}", "SELECT * FROM {{ filters.orders.col_pick.value }}"),
        ("SELECT * FROM a JOIN {% parameter col_pick %} b", "SELECT * FROM a JOIN {{ filters.orders.col_pick.value }} b"),
        ("SELECT * FROM {% parameter col_pick %}.events", "SELECT * FROM {{ filters.orders.col_pick.value }}.events"),
        ('SELECT "{% parameter col_pick %}"', "SELECT {{ filters.orders.col_pick.value }}"),
        ("SELECT `{% parameter col_pick %}`", "SELECT {{ filters.orders.col_pick.value }}"),
    ])
    def test_whole_identifier_uses_the_column_type(self, looker, omni):
        assert translate(looker).sql == omni

    def test_single_quoted_use_is_a_string(self):
        assert translate("x = '{% parameter text_pick %}'").sql == "x = {{ filters.orders.text_pick.value }}"

    @pytest.mark.parametrize("sql", [
        'SELECT "prefix_{% parameter col_pick %}"',
        "SELECT prefix_{% parameter col_pick %} FROM t",
        "SELECT {% parameter col_pick %}_suffix FROM t",
        "SELECT * FROM shop.events_{% parameter col_pick %}",
    ])
    def test_use_inside_a_larger_identifier_is_not_converted(self, sql):
        reason = blocked(sql)
        assert "each allowed value must hold the full identifier" in reason

    @pytest.mark.parametrize("sql", [
        "DATE_TRUNC({% parameter col_pick %}, created_at)",   # a keyword, not a column
        "SELECT {% parameter col_pick %} AS x",
        "{% parameter col_pick %}(sale_price)",
    ])
    def test_bare_use_that_may_not_be_an_identifier_is_not_converted(self, sql):
        assert "does not show whether the value is a column, a keyword or a number" in blocked(sql)

    def test_use_that_disagrees_with_the_written_type_is_not_converted(self):
        # text_pick is written as `type: string`: an identifier use cannot read it
        assert "written with `type: string`" in blocked("SELECT t.{% parameter text_pick %} FROM t")
        assert "written with `type: column`" in blocked("x = '{% parameter col_pick %}'")


# ----------------------------------------------------------------------
# L6. Date bounds
# ----------------------------------------------------------------------

class TestDateBounds:
    def test_date_start_and_end(self):
        sql = translate("c >= {% date_start order_date %} AND c < {% date_end orders.order_date %}").sql
        assert sql == ("c >= {{ filters.orders.order_date.range_start }} AND c < "
                       "{{ filters.orders.order_date.range_end }}")

    def test_dimension_group_timeframe(self):
        assert translate("{% date_start created_date %}").sql == "{{ filters.orders.created.range_start }}"

    def test_non_date_field_is_not_converted(self):
        assert "not a date field" in blocked("c >= {% date_start status_pick %}")

    def test_inside_quotes_is_not_converted(self):
        assert "inside a quoted string" in blocked("c >= '{% date_start order_date %}'")


# ----------------------------------------------------------------------
# L7. Query-state variables
# ----------------------------------------------------------------------

class TestQueryState:
    @pytest.mark.parametrize("looker,omni", [
        ("{{ orders.status._in_query }}", "{{ orders.status.in_query }}"),
        ("{{ status._is_selected }}", "{{ orders.status.is_selected }}"),
        ("{{ orders.status_pick._is_filtered }}", "{{ orders.status_pick.is_filtered }}"),
        ("{{ users._in_query }}", "{{ users.in_query }}"),
        ("{{ customers._in_query }}", "{{ users.in_query }}"),
        ("{{ total._in_query }}", "{{ orders.total.in_query }}"),
    ])
    def test_standalone_expression(self, looker, omni):
        assert translate(f"CASE WHEN {looker} THEN 1 END").sql == f"CASE WHEN {omni} THEN 1 END"

    def test_view_level_only_exists_for_in_query(self):
        assert blocked("{{ users._is_selected }}")

    def test_timeframe_is_not_converted(self):
        assert "one timeframe of dimension group" in blocked("{{ created_date._in_query }}")

    def test_name_that_is_a_field_and_a_view_is_ambiguous(self):
        context = LiquidContext(field=resolve_field, view=lambda name: ('status', None))
        translation = translate_liquid("{{ status._in_query }}", context)
        assert translation.sql is None and "both a field of the view and a view name" in translation.reason

    def test_inside_if_falls_under_l8(self):
        assert "`{% if %}`" in blocked("{% if orders.status._in_query %} a {% endif %}")


# ----------------------------------------------------------------------
# L8. Everything else stays manual, and the report names the construct
# ----------------------------------------------------------------------

class TestNotConverted:
    @pytest.mark.parametrize("sql,named", [
        ("{% if x %} a {% else %} b {% endif %}", ["`{% if %}`", "`{% else %}`", "`{% endif %}`"]),
        ("{% if a %} 1 {% elsif b %} 2 {% endif %}", ["`{% elsif %}`"]),
        ("{% assign x = 1 %} SELECT 1", ["`{% assign %}` (variable assignment)"]),
        ("{% for i in (1..3) %} x {% endfor %}", ["`{% for %}` (loop)"]),
        ("SELECT {{ status._in_query | upcase }}", ["Liquid filter"]),
        ("SELECT {{ value }}", ["`{{ value }}` (Liquid output with no Omni equivalent)"]),
        ("SELECT {{ _view._name }}", ["`{{ _view._name }}`"]),
        ("{% unless x %} a {% endunless %}", ["`{% unless %}`"]),
        ("{% comment %} a {% endcomment %}", ["`{% comment %}`"]),
    ])
    def test_reason_names_each_construct(self, sql, named):
        reason = blocked(sql)
        assert reason.startswith("sql uses Liquid that is not converted automatically: ")
        for name in named:
            assert name in reason

    def test_one_unsupported_construct_blocks_the_whole_block(self):
        sql = ("WHERE {% condition status_pick %} s {% endcondition %} "
               "{% if x %} AND y = {% parameter min_price %} {% endif %}")
        reason = blocked(sql)
        assert "`{% if %}`" in reason and "{% condition" not in reason

    def test_without_a_project_only_user_attributes_translate(self):
        assert translate_liquid("x = {% parameter p %}").sql is None
        assert translate_liquid("x = {{ _user_attributes['a'] }}").sql == "x = {{ omni_attributes.a }}"


# ----------------------------------------------------------------------
# L9. User attributes
# ----------------------------------------------------------------------

class TestUserAttributes:
    @pytest.mark.parametrize("looker,omni", [
        ("region = '{{ _user_attributes['region'] }}'", "region = '{{ omni_attributes.region }}'"),
        ('region = {{ _user_attributes["region"] }}', "region = {{ omni_attributes.region }}"),
        ("FROM shop_{{ _user_attributes['tenant'] }}.orders", "FROM shop_{{ omni_attributes.tenant }}.orders"),
    ])
    def test_translated_raw_with_its_quotes_kept(self, looker, omni):
        translation = translate(looker)
        assert translation.sql == omni
        assert translation.attributes

    @pytest.mark.parametrize("sql", ["{{ _user_attributes['a b'] }}", "{{ _user_attributes[x] }}",
                                     "{{ _user_attributes['a'] | upcase }}"])
    def test_other_forms_are_not_converted(self, sql):
        blocked(sql)


# ----------------------------------------------------------------------
# Final guard
# ----------------------------------------------------------------------

class TestNoLiquidLeft:
    @pytest.mark.parametrize("sql", [
        "{{# v.f.filter }} x {{/ v.f.filter }} {{^ v.f.filter }} 1 = 1 {{/ v.f.filter }}",
        "{{ filters.v.f.value }} {{filters.v.f.range_start}} {{ filters.v.f.range_end }}",
        "{{ v.f.in_query }} {{ v.f.is_selected }} {{ v.f.is_filtered }} {{ v.in_query }} {{ omni_attributes.a }}",
        "plain sql", None,
    ])
    def test_omni_mustache_passes(self, sql):
        assert find_looker_liquid(sql) == []
        assert_no_looker_liquid(sql)

    @pytest.mark.parametrize("sql", [
        "{% if x %}", "{{ v.f._in_query }}", "{{ value }}", "{{ _user_attributes['a'] }}",
        "{{ filters.v.f.value | upcase }}", "{{ unclosed", "{{ v.f._parameter_value }}",
    ])
    def test_looker_liquid_is_refused(self, sql):
        with pytest.raises(ValueError, match="still contains Liquid"):
            assert_no_looker_liquid(sql)

    def test_a_view_with_liquid_left_is_never_rendered(self, tmp_path):
        tags = ['mig_phase_0', 'verified']
        dims = {'bad': OmniDimension(label='Bad', description='d', tags=tags, sql="{{ value }}")}
        view = OmniView(schema='s', folder='f', label='V', description='d', tags=tags, table_name='t', dimensions=dims)
        with pytest.raises(ValueError, match="the sql of bad still contains Liquid"):
            FileGenerator(output_dir=tmp_path).render_view_yaml(view)


# ----------------------------------------------------------------------
# Rule applier: field sql
# ----------------------------------------------------------------------

class TestFieldSql:
    @pytest.fixture
    def applier(self):
        return RuleApplier(phase=0, view_name='orders', column_fields={'status': 'status'},
                           field_types={'status': 'string'}, liquid_context=CONTEXT)

    def test_dimension_with_a_condition(self, applier):
        result = applier.convert_dimension('picked', {
            'sql': "CASE WHEN {% condition status_pick %} ${TABLE}.status {% endcondition %} THEN 1 END"})
        assert result.omni_field.sql == ("CASE WHEN {{# orders.status_pick.filter }} ${status} "
                                         "{{/ orders.status_pick.filter }} THEN 1 END")
        assert result.dependencies == {'status'}
        assert any(change.startswith("Liquid translated: ") for change in result.changes)
        assert result.liquid.translated

    def test_view_name_inside_a_token_is_never_rewritten(self, applier):
        # `orders.status` is a column reference to rewrite; the same text inside a token is not
        result = applier.convert_dimension('x', {
            'sql': "CASE WHEN {{ orders.status._in_query }} THEN orders.status END"})
        assert result.omni_field.sql == "CASE WHEN {{ orders.status.in_query }} THEN ${status} END"
        assert result.dependencies == {'status'}

    def test_field_named_in_a_token_is_a_dependency(self, applier):
        result = applier.convert_measure('m', {'type': 'number', 'sql': "MAX({{ total._in_query }})"})
        assert result.dependencies == {'total'}
        result = applier.convert_dimension('d', {'sql': "{{ users.status._is_filtered }}"})
        assert result.cross_view_refs == {'users.status'} and result.dependencies == set()

    def test_unquoted_column_of_the_table(self, applier):
        result = applier.convert_measure('m', {'type': 'sum', 'sql': "${TABLE}.{% parameter col_pick %}"})
        assert result.omni_field.sql == "{{ filters.orders.col_pick.value }}"

    @pytest.mark.parametrize("convert", ['convert_dimension', 'convert_dimension_group', 'convert_measure'])
    def test_unsupported_liquid_names_the_construct(self, applier, convert):
        result = getattr(applier, convert)('x', {'type': 'time' if 'group' in convert else 'number',
                                                 'sql': "{% if status._in_query %} 1 {% else %} 0 {% endif %}"})
        assert result.omni_field is None and result.risk_level == 'HIGH'
        assert result.warnings[0].startswith("MANUAL ACTION REQUIRED: sql uses Liquid that is not converted")
        assert "`{% if %}` (conditional logic" in result.warnings[0]

    def test_user_attribute_keeps_its_quotes(self):
        result = RuleApplier(phase=0).convert_dimension('d', {
            'sql': "${TABLE}.region = '{{ _user_attributes['region'] }}'"})
        assert result.omni_field.sql == "region = '{{ omni_attributes.region }}'"
        assert result.liquid.attributes == ['region']


# ----------------------------------------------------------------------
# L2, L3. Filter-only fields and parameters (rule applier)
# ----------------------------------------------------------------------

class TestFilterFieldConversion:
    @pytest.fixture
    def applier(self):
        return RuleApplier(phase=0, view_name='orders')

    def dump(self, result):
        return result.omni_field.model_dump(exclude_none=True)

    @pytest.mark.parametrize("looker_type,omni_type", [
        (None, 'string'), ('string', 'string'), ('number', 'number'), ('date', 'timestamp'),
        ('date_time', 'timestamp'), ('yesno', 'boolean'),
    ])
    def test_type_map(self, applier, looker_type, omni_type):
        data = {'type': looker_type} if looker_type else {}
        for kind in ('filter', 'parameter'):
            assert applier.convert_filter_field('f', data, kind).omni_field.type == omni_type

    def test_filter(self, applier):
        result = applier.convert_filter_field('status_pick', {
            'type': 'string', 'label': 'Status', 'description': 'Pick a status', 'group_label': 'Pickers',
            'suggest_dimension': 'users.status', 'default_value': 'Complete', 'hidden': 'yes'}, 'filter')
        assert self.dump(result) == {
            'type': 'string', 'label': 'Status', 'description': 'Pick a status', 'group_label': 'Pickers',
            'suggest_from_field': 'users.status', 'default_filter': {'is': 'Complete'}, 'hidden': True}

    def test_unqualified_suggest_dimension_gets_the_view(self, applier):
        result = applier.convert_filter_field('f', {'suggest_dimension': 'status'}, 'filter')
        assert result.omni_field.suggest_from_field == 'orders.status'

    def test_filter_suggestions_become_a_suggestion_list(self, applier):
        result = applier.convert_filter_field('f', {'suggestions': ['a', 'b']}, 'filter')
        assert result.omni_field.suggestion_list == [{'value': 'a'}, {'value': 'b'}]

    def test_parameter(self, applier):
        result = applier.convert_filter_field('metric', {
            'type': 'string', 'default_value': 'a',
            'allowed_values': [{'label': 'Label A', 'value': 'a'}, {'value': 'b'}]}, 'parameter')
        assert self.dump(result) == {
            'type': 'string',
            'suggestion_list': [{'value': 'a', 'label': 'Label A'}, {'value': 'b'}],
            'default_filter': {'is': 'a'},
            'filter_single_select_only': True}

    def test_number_parameter_values_are_numbers(self, applier):
        result = applier.convert_filter_field('n', {
            'type': 'number', 'default_value': '5', 'allowed_values': [{'value': '5'}, {'value': '7.5'}]}, 'parameter')
        assert result.omni_field.default_filter == {'is': 5}
        assert result.omni_field.suggestion_list == [{'value': 5}, {'value': 7.5}]

    def test_unquoted_parameter_takes_the_decided_type(self, applier):
        data = {'type': 'unquoted', 'allowed_values': [{'value': 'revenue'}]}
        assert applier.convert_filter_field('p', data, 'parameter', 'column').omni_field.type == 'column'
        assert applier.convert_filter_field('p', data, 'parameter', 'string').omni_field.type == 'string'
        assert applier.convert_filter_field('p', data, 'parameter').omni_field.type == 'string'

    def test_date_default_is_reported_not_written(self, applier):
        result = applier.convert_filter_field('d', {'type': 'date', 'default_value': '7 days'}, 'filter')
        assert result.omni_field.default_filter is None
        assert result.risk_level == 'MEDIUM' and '"7 days" not migrated' in result.warnings[0]

    def test_untranslatable_filter_default_is_reported_not_written(self, applier):
        result = applier.convert_filter_field('f', {'default_value': '%abc%'}, 'filter')
        assert result.omni_field.default_filter is None and result.warnings

    def test_filter_with_sql_is_not_converted(self, applier):
        result = applier.convert_filter_field('f', {'sql': "${status} = 'a'"}, 'filter')
        assert result.omni_field is None and "MANUAL ACTION REQUIRED" in result.warnings[0]

    def test_unknown_type_is_not_converted(self, applier):
        result = applier.convert_filter_field('f', {'type': 'tier'}, 'filter')
        assert result.omni_field is None and "MANUAL ACTION REQUIRED" in result.warnings[0]

    def test_liquid_in_a_label_is_dropped_and_reported(self, applier):
        result = applier.convert_filter_field('f', {'label': "{{ _user_attributes['x'] }}"}, 'filter')
        assert result.omni_field.label is None and "`label` uses Liquid" in result.warnings[0]

    def test_filter_model_rejects_unknown_types_and_keys(self):
        with pytest.raises(ValidationError):
            OmniFilter(type='date')
        with pytest.raises(ValidationError):
            OmniFilter(type='string', suggestion_list=[{'label': 'no value'}])


# ----------------------------------------------------------------------
# Parser: parameters and filter-only fields are inherited
# ----------------------------------------------------------------------

def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text))
    return path


class TestInheritedFilterFields:
    LKML = """
        view: base {
          extension: required
          parameter: metric { type: unquoted allowed_value: { value: "revenue" } }
          filter: order_date { type: date }
        }
        view: other { parameter: metric { type: string } }
        view: child { extends: [base] sql_table_name: shop.t ;; parameter: metric { default_value: "revenue" } }
        view: both { extends: [base, other] sql_table_name: shop.t ;; }
    """

    def views(self, tmp_path):
        write(tmp_path / "repo" / "views.lkml", self.LKML)
        return load_views(str(tmp_path / "repo"))[0]

    def test_view_parses_filter_only_fields(self, tmp_path):
        base = self.views(tmp_path)['base'][0]
        assert list(base.filters) == ['order_date'] and base.filters['order_date']['type'] == 'date'
        assert list(base.parameters) == ['metric']

    def test_child_inherits_parameters_and_filters(self, tmp_path):
        views = self.views(tmp_path)
        resolution = resolve_view_extends(views['child'][0], views)
        assert resolution.fields['order_date'].kind == 'filter'
        assert resolution.fields['metric'].kind == 'parameter'
        full = effective_view(views['child'][0], resolution, include_inherited=True)
        assert set(full.filters) == {'order_date'}
        # A partial override keeps the parameters the child leaves out
        assert full.parameters['metric']['type'] == 'unquoted'
        assert full.parameters['metric']['default_value'] == 'revenue'

    def test_parents_that_disagree_are_a_conflict(self, tmp_path):
        views = self.views(tmp_path)
        resolution = resolve_view_extends(views['both'][0], views)
        assert resolution.fields['metric'].conflict == ['base', 'other']
        assert effective_view(views['both'][0], resolution).parameters['metric']['type'] == 'string'


# ----------------------------------------------------------------------
# View migration
# ----------------------------------------------------------------------

def csv(tmp_path, rows):
    """rows: (view, field, type)"""
    lines = [f"0,{v},{f},{t},,,Team A,ready," for v, f, t in rows] or ["0,unrelated_view,x,dimension,,,Team A,ready,"]
    return write(tmp_path / "scope.csv", CSV_HEADER + "\n".join(lines) + "\n")


def migrate(tmp_path, lkml, view, rows=()):
    write(tmp_path / "repo" / "project.lkml", lkml)
    orch = MigrationOrchestrator(skill_path=SKILL_ROOT, looker_repo=tmp_path / "repo",
                                 omni_repo=tmp_path / "out" / "views", csv_path=csv(tmp_path, rows))
    return orch.migrate_view(view, 0, "Team A")


def output(result):
    return load_yaml(result.view_file_path), result.view_file_path.read_text(), result.report_file_path.read_text()


SALES = """
    view: sales {
      derived_table: {
        sql: SELECT user_id, status, SUM(sale_price) AS revenue, SUM(cost) AS cost
          FROM shop.order_items
          WHERE {% condition order_date %} created_at {% endcondition %}
            AND {% condition sales.status_pick %} status {% endcondition %}
            AND created_at < {% date_end order_date %}
            AND sale_price >= {% parameter min_price %}
            AND region = '{{ _user_attributes['region'] }}'
          GROUP BY 1, 2 ;;
      }
      filter: order_date { type: date label: "Order Date" }
      filter: status_pick { type: string suggest_dimension: sales.status default_value: "Complete" }
      parameter: min_price { type: number default_value: "0" }
      parameter: spare { type: string allowed_value: { label: "A" value: "a" } }
      dimension: user_id { primary_key: yes type: number sql: ${TABLE}.user_id ;; }
      dimension: status { type: string sql: ${TABLE}.status ;; }
    }
"""


class TestDerivedTable:
    def test_templated_derived_table_converts(self, tmp_path):
        result = migrate(tmp_path, SALES, "sales", [("sales", "user_id", "dimension")])
        assert result.success, result.error_message
        assert result.view_file_path.name == "sales.query.view"
        view, text, report = output(result)
        assert "{{# sales.order_date.filter }} created_at {{/ sales.order_date.filter }}" in view["sql"]
        assert "{{# sales.status_pick.filter }} status {{/ sales.status_pick.filter }}" in view["sql"]
        assert "created_at < {{ filters.sales.order_date.range_end }}" in view["sql"]
        assert "sale_price >= {{ filters.sales.min_price.value }}" in view["sql"]
        assert "region = '{{ omni_attributes.region }}'" in view["sql"]
        assert "{%" not in text and "_user_attributes" not in text
        # A block scalar, with the lines the LookML wrote
        assert "\nsql: |-\n  SELECT user_id" in text
        assert "\n  FROM shop.order_items\n" in text

    def test_referenced_filter_only_fields_are_written(self, tmp_path):
        view, _, report = output(migrate(tmp_path, SALES, "sales", [("sales", "user_id", "dimension")]))
        assert view["filters"] == {
            "order_date": {"type": "timestamp", "label": "Order Date"},
            "status_pick": {"type": "string", "suggest_from_field": "sales.status",
                            "default_filter": {"is": "Complete"}},
            "min_price": {"type": "number", "default_filter": {"is": 0}, "filter_single_select_only": True},
            # Not in the CSV and named by no SQL: written hidden
            "spare": {"type": "string", "suggestion_list": [{"value": "a", "label": "A"}],
                      "filter_single_select_only": True, "hidden": True},
        }
        assert "Parameters and Filter-Only Fields" in report
        assert "spare (Looker parameter, type string → `type: string`): hidden" in report

    def test_csv_type_filter_makes_a_filter_only_field_visible(self, tmp_path):
        result = migrate(tmp_path, SALES, "sales", [("sales", "user_id", "dimension"), ("sales", "spare", "filter")])
        view, _, report = output(result)
        assert "hidden" not in view["filters"]["spare"]
        assert "spare (Looker parameter, type string → `type: string`): visible (in the CSV scope)" in report
        assert not any("spare" in issue for issue in result.issues)

    def test_csv_row_with_another_type_is_reported(self, tmp_path):
        result = migrate(tmp_path, SALES, "sales", [("sales", "user_id", "dimension"), ("sales", "spare", "dimension")])
        assert "hidden" not in load_yaml(result.view_file_path)["filters"]["spare"]
        assert any("'spare' is listed as dimension but is a parameter" in issue for issue in result.issues)

    def test_report_lists_each_construct_under_liquid_translated(self, tmp_path):
        result = migrate(tmp_path, SALES, "sales", [("sales", "user_id", "dimension")])
        section = output(result)[2].split("## Liquid translated")[1].split("\n## ")[0]
        assert "**derived table `sql`**" in section
        for line in (
            "`{% condition order_date %}` → `{{# sales.order_date.filter }}`",
            "`{% endcondition %}` → `{{/ sales.order_date.filter }}`",
            "`{% date_end order_date %}` → `{{ filters.sales.order_date.range_end }}`",
            "`{% parameter min_price %}` → `{{ filters.sales.min_price.value }}`",
            "`{{ _user_attributes['region'] }}` → `{{ omni_attributes.region }}`",
        ):
            assert line in section
        assert any("create them in Omni: region" in issue for issue in result.issues)

    @pytest.mark.parametrize("extra,named", [
        ("{% if min_price._parameter_value == '1' %} AND 1 = 1 {% endif %}", "`{% if %}`"),
        ("AND x = {{ value }}", "`{{ value }}`"),
        ("AND {% condition nowhere.f %} x {% endcondition %}", "`nowhere` is not a view"),
        ("AND {% condition missing %} x {% endcondition %}", "`missing` is not a field of view `sales`"),
        ("AND x LIKE '%{% parameter spare %}%'", "embedded in a longer quoted string"),
    ])
    def test_one_unsupported_construct_stops_the_view_and_is_named(self, tmp_path, extra, named):
        lkml = SALES.replace("GROUP BY 1, 2", extra + " GROUP BY 1, 2")
        result = migrate(tmp_path, lkml, "sales", [("sales", "user_id", "dimension")])
        assert not result.success
        assert "MANUAL ACTION REQUIRED" in result.error_message and named in result.error_message
        assert not (tmp_path / "out").exists()

    def test_sql_table_name_reference_stays_manual(self, tmp_path):
        lkml = SALES.replace("FROM shop.order_items", "FROM ${order_items.SQL_TABLE_NAME}")
        result = migrate(tmp_path, lkml, "sales", [("sales", "user_id", "dimension")])
        assert not result.success and "SQL_TABLE_NAME" in result.error_message

    def test_condition_on_a_dimension_that_is_not_migrated_stops_the_view(self, tmp_path):
        lkml = """
            view: sales {
              derived_table: { sql: SELECT * FROM shop.t WHERE {% condition tier %} amount {% endcondition %} ;; }
              dimension: id { primary_key: yes }
              dimension: tier { type: tier tiers: [0, 10] sql: ${TABLE}.amount ;; }
            }
        """
        result = migrate(tmp_path, lkml, "sales", [("sales", "id", "dimension")])
        assert not result.success and "names field `tier`" in result.error_message
        assert not (tmp_path / "out").exists()

    def test_condition_on_a_dimension_of_the_view(self, tmp_path):
        lkml = """
            view: sales {
              derived_table: { sql: SELECT * FROM shop.t WHERE {% condition created_date %} created_at {% endcondition %} ;; }
              dimension: id { primary_key: yes }
              dimension_group: created { type: time timeframes: [date, month] sql: ${TABLE}.created_at ;; }
            }
        """
        view, _, _ = output(migrate(tmp_path, lkml, "sales", [("sales", "id", "dimension")]))
        assert "{{# sales.created.filter }} created_at {{/ sales.created.filter }}" in view["sql"]
        assert "filters" not in view


class TestFieldsWithLiquid:
    LKML = """
        view: orders {
          sql_table_name: shop.orders ;;
          parameter: metric { type: unquoted allowed_value: { value: "revenue" } allowed_value: { value: "cost" } }
          parameter: threshold { type: number }
          filter: status_pick { type: string }
          dimension: id { primary_key: yes sql: ${TABLE}.id ;; }
          dimension: status { sql: ${TABLE}.status ;; }
          dimension: picked { type: number sql: ${TABLE}.{% parameter metric %} ;; }
          dimension: is_big { type: yesno sql: ${TABLE}.amount >= {% parameter threshold %} ;; }
          dimension: label { sql: {% if metric._parameter_value == 'cost' %} 'Cost' {% else %} 'Revenue' {% endif %} ;; }
          dimension: tier { type: tier tiers: [0, 10] sql: ${TABLE}.amount ;; }
          measure: matching { type: number sql: COUNT(CASE WHEN {% condition status_pick %} ${status} {% endcondition %} THEN 1 END) ;; }
          measure: tiered { type: number sql: MAX({{ tier._in_query }}) ;; }
        }
    """
    ROWS = [("orders", name, kind) for name, kind in (
        ("id", "dimension"), ("picked", "dimension"), ("is_big", "dimension"), ("label", "dimension"),
        ("matching", "measure"), ("tiered", "measure"))]

    def test_fields_convert_as_block_scalars(self, tmp_path):
        result = migrate(tmp_path, self.LKML, "orders", self.ROWS)
        assert result.success, result.error_message
        view, text, report = output(result)
        dims, measures = view["dimensions"], view["measures"]
        assert dims["picked"]["sql"] == "{{ filters.orders.metric.value }}"
        assert dims["is_big"]["sql"] == "amount >= {{ filters.orders.threshold.value }}"
        assert measures["matching"]["sql"] == ("COUNT(CASE WHEN {{# orders.status_pick.filter }} ${status} "
                                               "{{/ orders.status_pick.filter }} THEN 1 END)")
        # Never a plain scalar that starts with `{{`
        assert "sql: |-\n      {{ filters.orders.metric.value }}" in text
        assert "sql: {{" not in text
        assert "auto_dependency" in dims["status"]["tags"]
        assert "**dimension `picked`**" in report.split("## Liquid translated")[1]

    def test_unquoted_parameter_used_as_a_column_is_type_column(self, tmp_path):
        view, _, report = output(migrate(tmp_path, self.LKML, "orders", self.ROWS))
        assert view["filters"]["metric"] == {
            "type": "column", "suggestion_list": [{"value": "revenue"}, {"value": "cost"}],
            "filter_single_select_only": True}
        assert "each allowed value must be a full column" in report
        assert view["filters"]["threshold"] == {"type": "number", "filter_single_select_only": True}

    def test_liquid_if_is_not_converted_and_named(self, tmp_path):
        result = migrate(tmp_path, self.LKML, "orders", self.ROWS)
        view, text, report = output(result)
        assert "label" not in view["dimensions"]
        issue = next(i for i in result.issues if i.startswith("label:"))
        assert "`{% if %}` (conditional logic" in issue and "`{% endif %}`" in issue
        assert "{%" not in text and "_parameter_value" not in text

    def test_token_on_a_field_that_is_not_migrated_blocks_the_field(self, tmp_path):
        result = migrate(tmp_path, self.LKML, "orders", self.ROWS)
        assert "tiered" not in (load_yaml(result.view_file_path).get("measures") or {})
        assert any(i.startswith("tiered:") and "depends on tier" in i for i in result.issues)

    def test_unquoted_parameter_used_both_ways_blocks_its_sql(self, tmp_path):
        lkml = self.LKML.replace(
            "dimension: tier", "dimension: is_cost { sql: 'cost' = '{% parameter metric %}' ;; }\n  dimension: tier")
        result = migrate(tmp_path, lkml, "orders", self.ROWS + [("orders", "is_cost", "dimension")])
        view, _, _ = output(result)
        assert "picked" not in view["dimensions"] and "is_cost" not in view["dimensions"]
        assert any("used both as an identifier and as a quoted string" in i for i in result.issues)
        assert view["filters"]["metric"]["type"] == "string"

    def test_embedded_use_blocks_only_its_own_block(self, tmp_path):
        lkml = self.LKML.replace(
            "dimension: tier", "dimension: wide { sql: ${TABLE}.total_{% parameter metric %} ;; }\n  dimension: tier")
        result = migrate(tmp_path, lkml, "orders", self.ROWS + [("orders", "wide", "dimension")])
        view, _, _ = output(result)
        assert "wide" not in view["dimensions"]
        assert view["dimensions"]["picked"]["sql"] == "{{ filters.orders.metric.value }}"
        assert any(i.startswith("wide:") and "each allowed value must hold the full identifier" in i
                   for i in result.issues)

    def test_parameter_with_an_unsupported_definition_blocks_the_sql_that_reads_it(self, tmp_path):
        lkml = """
            view: orders {
              sql_table_name: shop.orders ;;
              filter: scoped { type: string sql: ${status} = 'a' ;; }
              dimension: id { primary_key: yes }
              dimension: status {}
              dimension: flagged { sql: CASE WHEN {% condition scoped %} ${status} {% endcondition %} THEN 1 END ;; }
            }
        """
        result = migrate(tmp_path, lkml, "orders", [("orders", "id", "dimension"), ("orders", "flagged", "dimension")])
        view, _, report = output(result)
        assert "flagged" not in view["dimensions"] and "filters" not in view
        assert any(i.startswith("scoped:") and "has `sql:`" in i for i in result.issues)
        assert any(i.startswith("flagged:") and "is not migrated (it has `sql:`)" in i for i in result.issues)


class TestInheritedParameters:
    LKML = """
        view: period_base {
          extension: required
          parameter: metric { type: unquoted allowed_value: { value: "revenue" } }
          parameter: min_orders { type: number default_value: "5" }
          filter: order_date { type: date }
          filter: unused { type: string suggest_dimension: status }
          dimension: status { sql: ${TABLE}.status ;; }
          dimension: is_big { type: yesno sql: ${TABLE}.orders >= {% parameter min_orders %} ;; }
          dimension: region_match { sql: ${TABLE}.region = '{{ _user_attributes['region'] }}' ;; }
        }
        view: sales {
          extends: [period_base]
          derived_table: {
            sql: SELECT user_id, {% parameter metric %}.amount AS amount, COUNT(*) AS orders
              FROM {% parameter metric %}
              WHERE {% condition order_date %} created_at {% endcondition %} GROUP BY 1, 2 ;;
          }
          dimension: user_id { primary_key: yes }
        }
    """

    def test_child_sql_names_the_child_and_does_not_copy_the_filters(self, tmp_path):
        result = migrate(tmp_path, self.LKML, "sales", [("sales", "user_id", "dimension")])
        assert result.success, result.error_message
        view, _, _ = output(result)
        assert "{{# sales.order_date.filter }} created_at {{/ sales.order_date.filter }}" in view["sql"]
        assert "FROM {{ filters.sales.metric.value }}" in view["sql"]
        assert view["extends"] == ["period_base"]
        # The parent writes them, the child inherits them through `extends:`
        assert "filters" not in view

    def test_template_parent_writes_the_filter_only_fields(self, tmp_path):
        result = migrate(tmp_path, self.LKML, "period_base")
        assert result.success, result.error_message
        view, _, report = output(result)
        assert view["template"] is True
        filters = view["filters"]
        # Used as a table name by the child: an identifier
        assert filters["metric"] == {"type": "column", "suggestion_list": [{"value": "revenue"}],
                                     "filter_single_select_only": True}
        assert filters["order_date"] == {"type": "timestamp"}
        assert "hidden" not in filters["min_orders"]
        assert "visible (named in the SQL of `period_base`, `sales`)" in report
        # Named by no SQL: hidden. A template cannot supply suggestions.
        assert filters["unused"] == {"type": "string", "hidden": True}
        assert any("unused" in i and "a template view cannot supply suggestions" in i for i in result.issues)

    def test_inherited_templated_field_is_rewritten_for_the_child(self, tmp_path):
        result = migrate(tmp_path, self.LKML, "sales", [("sales", "user_id", "dimension")])
        view, _, report = output(result)
        is_big = view["dimensions"]["is_big"]
        assert is_big["sql"] == "orders >= {{ filters.sales.min_orders.value }}"
        assert "resolved_from_extends" in is_big["tags"] and is_big["hidden"] is True
        assert "Templated Fields Rewritten From extends" in report
        # Liquid that names no view needs no copy
        assert "region_match" not in view["dimensions"]
        parent = load_yaml(migrate(tmp_path, self.LKML, "period_base").view_file_path)
        assert parent["dimensions"]["is_big"]["sql"] == "orders >= {{ filters.period_base.min_orders.value }}"

    def test_rewritten_field_is_visible_when_the_csv_lists_it_under_the_parent(self, tmp_path):
        rows = [("sales", "user_id", "dimension"), ("period_base", "is_big", "dimension")]
        view, _, _ = output(migrate(tmp_path, self.LKML, "sales", rows))
        assert "hidden" not in view["dimensions"]["is_big"]

    def test_csv_row_for_an_inherited_filter_is_reported(self, tmp_path):
        rows = [("sales", "user_id", "dimension"), ("sales", "order_date", "filter")]
        result = migrate(tmp_path, self.LKML, "sales", rows)
        assert any("inherited through extends" in i and "order_date (from `period_base`)" in i for i in result.issues)

    def test_parents_that_disagree_on_a_parameter_are_resolved_in_the_child(self, tmp_path):
        lkml = """
            view: a { extension: required parameter: pick { type: string default_value: "a" } }
            view: b { extension: required parameter: pick { type: string default_value: "b" } }
            view: child { extends: [a, b] sql_table_name: shop.t ;; dimension: id { primary_key: yes } }
        """
        view, _, report = output(migrate(tmp_path, lkml, "child", [("child", "id", "dimension")]))
        # Looker uses the last parent, Omni the first: the Looker definition is written here
        assert view["filters"]["pick"]["default_filter"] == {"is": "b"}
        assert "pick (parameter): defined differently by `a`, `b`" in report
        assert "Written into this view" in report


class TestViewNameResolution:
    BASE = """
        view: users {
          sql_table_name: shop.users ;;
          filter: signup_date { type: date }
          dimension: id { primary_key: yes }
        }
        view: accounts { sql_table_name: shop.accounts ;; filter: signup_date { type: date } dimension: id {} }
        view: facts {
          derived_table: { sql: SELECT * FROM shop.t WHERE {% condition REF %} created_at {% endcondition %} ;; }
          dimension: id { primary_key: yes }
        }
        explore: orders {
          join: customers { from: users sql_on: ${orders.user_id} = ${customers.id} ;; }
          join: buyers { from: users sql_on: ${orders.user_id} = ${buyers.id} ;; }
        }
    """

    def run(self, tmp_path, ref, extra=""):
        return migrate(tmp_path, self.BASE.replace("REF", ref) + extra, "facts", [("facts", "id", "dimension")])

    def test_real_view_name(self, tmp_path):
        result = self.run(tmp_path, "users.signup_date")
        assert "{{# users.signup_date.filter }}" in load_yaml(result.view_file_path)["sql"]
        assert "filters" not in load_yaml(result.view_file_path)
        # The other view has to be joined wherever this one is used
        assert "users.signup_date" in result.report_file_path.read_text().split("Cross-View References")[1]

    def test_alias_that_maps_to_one_view(self, tmp_path):
        result = self.run(tmp_path, "customers.signup_date")
        assert result.success, result.error_message
        # The token keeps the alias: naming the real view leaves the filter unbound in Omni
        assert "{{# customers.signup_date.filter }}" in load_yaml(result.view_file_path)["sql"]
        assert "customers.signup_date" in result.report_file_path.read_text().split("Cross-View References")[1]

    def test_explore_name_with_another_base_view(self, tmp_path):
        result = self.run(tmp_path, "people.signup_date", "explore: people { from: users }")
        assert "{{# people.signup_date.filter }}" in load_yaml(result.view_file_path)["sql"]

    def test_alias_of_the_view_itself_names_the_view(self, tmp_path):
        result = self.run(tmp_path, "window.id", "explore: window { from: facts }")
        assert result.success, result.error_message
        assert "{{# facts.id.filter }}" in load_yaml(result.view_file_path)["sql"]

    def test_alias_that_maps_to_two_views_with_the_same_field_is_kept(self, tmp_path):
        extra = "explore: more { join: customers { from: accounts sql_on: ${more.id} = ${customers.id} ;; } }"
        result = self.run(tmp_path, "customers.signup_date", extra)
        assert result.success, result.error_message
        assert "{{# customers.signup_date.filter }}" in load_yaml(result.view_file_path)["sql"]

    def test_alias_that_maps_to_two_views_that_differ_is_not_converted(self, tmp_path):
        extra = """
            view: ledgers { sql_table_name: shop.ledgers ;; dimension: signup_date { type: string } dimension: id {} }
            explore: more { join: customers { from: ledgers sql_on: ${more.id} = ${customers.id} ;; } }
        """
        result = self.run(tmp_path, "customers.signup_date", extra)
        assert not result.success
        assert "`customers` is an alias of several views (ledgers, users)" in result.error_message
        assert "differently" in result.error_message

    def test_alias_that_maps_to_two_views_one_without_the_field_is_not_converted(self, tmp_path):
        extra = """
            view: ledgers { sql_table_name: shop.ledgers ;; dimension: id {} }
            explore: more { join: customers { from: ledgers sql_on: ${more.id} = ${customers.id} ;; } }
        """
        result = self.run(tmp_path, "customers.signup_date", extra)
        assert not result.success
        assert "`customers` is an alias of several views (ledgers, users)" in result.error_message

    def test_unknown_name_is_not_converted(self, tmp_path):
        result = self.run(tmp_path, "nobody.signup_date")
        assert not result.success and "`nobody` is not a view under --looker-repo" in result.error_message


class TestMergeAndOutput:
    def test_filters_merge_into_an_existing_omni_view(self, tmp_path):
        write(tmp_path / "out" / "views" / "sales.query.view", """
            schema: shop
            sql: SELECT 1
            filters:
              order_date: {type: string}
              kept: {type: number}
        """)
        result = migrate(tmp_path, SALES, "sales", [("sales", "user_id", "dimension")])
        assert result.success, result.error_message
        filters = load_yaml(result.view_file_path)["filters"]
        assert filters["order_date"] == {"type": "timestamp", "label": "Order Date"}
        assert filters["kept"] == {"type": "number"}

    def test_token_follows_a_field_merged_under_its_column_name(self, tmp_path):
        lkml = """
            view: orders {
              sql_table_name: shop.orders ;;
              dimension: id { primary_key: yes }
              dimension: customer_status { sql: ${TABLE}.status ;; }
              measure: picked { type: number sql: MAX({{ customer_status._is_filtered }}) ;; }
              measure: seen { type: number sql: MAX({{ orders.customer_status._in_query }}) ;; }
            }
        """
        write(tmp_path / "out" / "views" / "orders.view.yaml",
              "schema: shop\ntable_name: orders\ndimensions: {id: {}, status: {}}\n")
        rows = [("orders", name, kind) for name, kind in (
            ("id", "dimension"), ("customer_status", "dimension"), ("picked", "measure"), ("seen", "measure"))]
        result = migrate(tmp_path, lkml, "orders", rows)
        assert result.success, result.error_message
        view = load_yaml(result.view_file_path)
        assert "customer_status" not in view["dimensions"]
        assert view["measures"]["picked"]["sql"] == "MAX({{ orders.status.is_filtered }})"
        assert view["measures"]["seen"]["sql"] == "MAX({{ orders.status.in_query }})"

    def test_filters_come_after_the_fields(self, tmp_path):
        text = output(migrate(tmp_path, SALES, "sales", [("sales", "user_id", "dimension")]))[1]
        assert text.index("\ndimensions:") < text.index("\nfilters:")
        assert "\n\n  status_pick:\n" in text


# ----------------------------------------------------------------------
# CLI on the thelook fixture
# ----------------------------------------------------------------------

class TestFixture:
    def test_sales_by_period(self, tmp_path):
        result = run_cli("--view", "sales_by_period", "--phase", "0", "--owner", "Team A",
                         "--looker-repo", FIXTURE_REPO, "--csv", SCOPE_CSV,
                         "--omni-repo", tmp_path / "views", cwd=tmp_path)
        assert result.returncode == 0, result.stdout + result.stderr
        text = (tmp_path / "views" / "sales_by_period.query.view").read_text()
        view = load_yaml(tmp_path / "views" / "sales_by_period.query.view")
        assert "{%" not in text
        for token in (
            "{{# sales_by_period.order_date.filter }} order_items.created_at {{/ sales_by_period.order_date.filter }}",
            "{{# sales_by_period.status_pick.filter }} order_items.status {{/ sales_by_period.status_pick.filter }}",
            "order_items.created_at < {{ filters.sales_by_period.order_date.range_end }}",
            "order_items.sale_price >= {{ filters.sales_by_period.min_sale_price.value }}",
            "order_items.region = '{{ omni_attributes.sales_region }}'",
        ):
            assert token in view["sql"]
        assert view["filters"] == {
            "order_date": {"type": "timestamp", "label": "Order Date",
                           "description": "Period the sales are counted over"},
            "status_pick": {"type": "string", "suggest_from_field": "order_items.status",
                            "default_filter": {"is": "Complete"}},
            "min_sale_price": {"type": "number", "default_filter": {"is": 0}, "filter_single_select_only": True},
            "metric": {"type": "column", "label": "Metric",
                       "suggestion_list": [{"value": "revenue", "label": "Revenue"},
                                           {"value": "cost", "label": "Cost"}],
                       "default_filter": {"is": "revenue"}, "filter_single_select_only": True},
            "unused_choice": {"type": "string", "suggestion_list": [{"value": "a"}],
                              "filter_single_select_only": True},
        }
        assert view["dimensions"]["selected_metric"]["sql"] == "{{ filters.sales_by_period.metric.value }}"
        assert view["measures"]["total_selected_metric"]["sql"] == "${selected_metric}"
        assert "metric_label" not in view["dimensions"]
        report = (tmp_path / "reports" / "sales_by_period.migration.md").read_text()
        assert "## Liquid translated" in report
        assert "metric_label" in report and "`{% if %}` (conditional logic" in report
        assert "`{% if %}`" in result.stdout
