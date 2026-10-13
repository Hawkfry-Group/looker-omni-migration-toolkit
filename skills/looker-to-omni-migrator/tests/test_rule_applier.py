"""Unit tests for lib/rule_applier.py"""

import pytest

from lib.omni_models import AggregateType
from lib.rule_applier import (
    RuleApplier,
    extract_measure_filters,
    filter_kind,
    normalize_tag,
    split_sql,
    translate_filter_expression,
)

# Fields whose sql is exactly ${TABLE}.<column>
COLUMN_FIELDS = {'id': 'id', 'order_id': 'order_id', 'first_name': 'first_name', 'last_name': 'last_name',
                 'status': 'status', 'user_id': 'customer_id'}
FIELD_TYPES = {'id': 'number', 'order_id': 'number', 'first_name': 'string', 'last_name': 'string',
               'status': 'string', 'sale_price': 'number', 'is_returned': 'yesno', 'zip': 'zipcode',
               'created': 'time', 'count': 'measure', 'total_sale_price': 'measure'}


def other_view_types(view, field):
    return {('users', 'country'): 'string', ('users', 'age'): 'number'}.get((view, field))


@pytest.fixture
def applier():
    return RuleApplier(phase=0, verified=True, view_name='order_items', column_fields=COLUMN_FIELDS,
                       field_types=FIELD_TYPES, type_resolver=other_view_types)


class TestTags:
    def test_normalize_tag(self):
        assert normalize_tag('Mig Phase 0') == 'mig_phase_0'
        assert normalize_tag('Has-Special Chars!') == 'has_special_chars'

    def test_unverified(self):
        result = RuleApplier(phase=2, verified=False).convert_dimension('status', {'sql': '${TABLE}.status'})
        assert result.omni_field.tags[-2:] == ['mig_phase_2', 'unverified']


class TestDimensionSql:
    def test_standard_sql_omitted(self, applier):
        result = applier.convert_dimension('id', {'sql': '${TABLE}.id', 'type': 'number', 'primary_key': 'yes'})
        assert result.omni_field.sql is None
        assert result.omni_field.primary_key is True
        assert result.risk_level == 'LOW'

    def test_missing_sql_defaults_to_own_column(self, applier):
        assert applier.convert_dimension('status', {'type': 'string'}).omni_field.sql is None

    def test_own_column_in_expression_is_raw_not_self_reference(self, applier):
        result = applier.convert_dimension('sale_price', {'sql': 'COALESCE(${TABLE}.sale_price, 0)'})
        assert result.omni_field.sql == 'COALESCE(sale_price, 0)'
        assert 'sale_price' not in result.dependencies

    def test_table_column_equivalent_to_a_field_becomes_field_reference(self, applier):
        result = applier.convert_dimension('full_name', {'sql': "CONCAT(${TABLE}.first_name, ' ', ${TABLE}.last_name)"})
        assert result.omni_field.sql == "CONCAT(${first_name}, ' ', ${last_name})"
        assert result.dependencies == {'first_name', 'last_name'}

    def test_column_read_by_renamed_field_uses_that_field(self, applier):
        result = applier.convert_dimension('has_user', {'sql': '${TABLE}.user_id IS NOT NULL'})
        assert result.omni_field.sql == '${customer_id} IS NOT NULL'

    def test_column_of_transformed_field_stays_raw(self, applier):
        # sale_price is a field, but its sql is COALESCE(...), not the raw column
        result = applier.convert_measure('total_raw', {'type': 'sum', 'sql': '${TABLE}.sale_price'})
        assert result.omni_field.sql == 'sale_price'
        assert result.dependencies == set()

    @pytest.mark.parametrize('sql', ['${TABLE}."price"', '${TABLE}.`price`', 'SELECT MAX(x) FROM ${TABLE}'])
    def test_quoted_identifier_or_bare_table_not_converted(self, applier, sql):
        result = applier.convert_dimension('price', {'sql': sql})
        assert result.omni_field is None
        assert 'MANUAL ACTION REQUIRED' in result.warnings[0]

    def test_string_literals_and_comments_preserved(self, applier):
        sql = "CASE WHEN ${TABLE}.status = 'order_items.status' THEN 1 END -- order_items.id"
        result = applier.convert_dimension('flag', {'sql': sql})
        assert result.omni_field.sql == "CASE WHEN ${status} = 'order_items.status' THEN 1 END -- order_items.id"

    def test_bare_view_reference_converted_like_table(self, applier):
        result = applier.convert_dimension('flag', {'sql': 'order_items.returned_at IS NOT NULL'})
        assert result.omni_field.sql == 'returned_at IS NOT NULL'

    @pytest.mark.parametrize('sql', ["CONCAT('${first_name}', 'x')", "${status} -- uses ${id}"])
    def test_reference_inside_literal_or_comment_not_converted(self, applier, sql):
        result = applier.convert_dimension('x', {'sql': sql})
        assert result.omni_field is None
        assert 'quoted' in result.warnings[0]

    def test_split_sql(self):
        assert split_sql("a = 'b' /* c */ d") == [('a = ', True), ("'b'", False), (' ', True), ('/* c */', False), (' d', True)]

    def test_unknown_table_column_stays_raw(self, applier):
        result = applier.convert_dimension('is_returned', {'type': 'yesno', 'sql': '${TABLE}.returned_at IS NOT NULL'})
        assert result.omni_field.sql == 'returned_at IS NOT NULL'
        assert result.dependencies == set()

    def test_cross_view_reference_kept(self, applier):
        result = applier.convert_dimension('gross_margin', {'sql': '${sale_price} - ${inventory_items.cost}'})
        assert result.omni_field.sql == '${sale_price} - ${inventory_items.cost}'
        assert result.cross_view_refs == {'inventory_items.cost'}
        assert result.dependencies == {'sale_price'}

    def test_liquid_is_not_converted(self, applier):
        result = applier.convert_dimension('x', {'sql': "{% if status._in_query %}1{% else %}0{% endif %}"})
        assert result.omni_field is None
        assert result.risk_level == 'HIGH'
        assert 'MANUAL ACTION REQUIRED' in result.warnings[0]

    @pytest.mark.parametrize('dim', [
        {'type': 'tier', 'tiers': [0, 10], 'sql': '${sale_price}'},
        {'type': 'location', 'sql_latitude': 'lat', 'sql_longitude': 'lon'},
        {'case': {'when': []}},
    ])
    def test_unsupported_dimensions(self, applier, dim):
        assert applier.convert_dimension('x', dim).omni_field is None

    def test_dropped_params_noted(self, applier):
        result = applier.convert_dimension('status', {'sql': '${TABLE}.status', 'html': '<b>{{ value }}</b>', 'drill_fields': ['id']})
        assert result.omni_field is not None
        assert set(result.dropped_params) == {'html', 'drill_fields'}
        assert 'Not migrated: html' in result.changes


class TestDbColumn:
    @pytest.mark.parametrize('sql,expected', [
        ('${TABLE}.user_id', 'user_id'),
        ('user_id', 'user_id'),
        ('${TABLE}.customer_id', None),  # same as the field name
        ('${other}', None),
        ('1', None),
        ('${a} / ${b}', None),
        ('UPPER(${TABLE}.x)', None),
        ('NULL', None),
    ])
    def test_extract_db_column_name(self, applier, sql, expected):
        assert applier.extract_db_column_name(sql, 'customer_id') == expected

    def test_renamed_column_adds_synonym(self):
        result = RuleApplier(phase=0, view_name='order_items').convert_dimension('customer_id', {'sql': '${TABLE}.user_id'})
        assert result.db_column_name == 'user_id'
        assert result.omni_field.sql == 'user_id'
        assert result.omni_field.synonyms == ['user_id']


class TestMeasures:
    @pytest.mark.parametrize('looker_type', ['count_distinct', 'sum', 'average', 'min', 'max', 'median'])
    def test_aggregate_types(self, applier, looker_type):
        result = applier.convert_measure('m', {'type': looker_type, 'sql': '${sale_price}'})
        assert result.omni_field.aggregate_type == AggregateType(looker_type)
        assert result.omni_field.sql == '${sale_price}'

    def test_count_has_no_sql(self, applier):
        result = applier.convert_measure('count', {'type': 'count'})
        assert result.omni_field.aggregate_type == AggregateType.COUNT
        assert result.omni_field.sql is None

    def test_list_uses_list_field(self, applier):
        result = applier.convert_measure('statuses', {'type': 'list', 'list_field': 'status'})
        assert result.omni_field.aggregate_type == AggregateType.LIST
        assert result.omni_field.sql == '${status}'

    def test_percentile(self, applier):
        result = applier.convert_measure('p90', {'type': 'percentile', 'percentile': '90', 'sql': '${sale_price}'})
        assert result.omni_field.percentile == 90

    def test_number_measure_is_calculated(self, applier):
        result = applier.convert_measure(
            'per_order', {'type': 'number', 'sql': '${total_sale_price} / NULLIF(${count}, 0)'},
            all_measures={'total_sale_price', 'count'}
        )
        assert result.omni_field.aggregate_type is None
        assert result.dependencies == {'total_sale_price', 'count'}

    @pytest.mark.parametrize('looker_type', ['running_total', 'percent_of_total', 'sum_distinct', 'period_over_period', 'bogus'])
    def test_unsupported_type_not_emitted(self, applier, looker_type):
        result = applier.convert_measure('m', {'type': looker_type, 'sql': '${sale_price}'})
        assert result.omni_field is None
        assert result.risk_level == 'HIGH'
        assert looker_type in result.warnings[0]

    @pytest.mark.parametrize('looker,omni', [
        ('usd', 'usdcurrency_2'), ('decimal_0', 'number_0'), ('decimal_2', 'number_2'),
        ('percent_1', 'percent_1'), ('eur', 'eurcurrency_2'), ('id', 'id'),
    ])
    def test_format_mapping(self, applier, looker, omni):
        result = applier.convert_measure('m', {'type': 'sum', 'sql': '${sale_price}', 'value_format_name': looker})
        assert result.omni_field.format == omni

    @pytest.mark.parametrize('fmt', [{'value_format_name': 'decimal_9'}, {'value_format': '$#,##0.00'}])
    def test_unmapped_format_flagged_not_copied(self, applier, fmt):
        result = applier.convert_measure('m', {'type': 'sum', 'sql': '${sale_price}', **fmt})
        assert result.omni_field.format is None
        assert result.risk_level == 'MEDIUM'
        assert result.warnings


class TestFilters:
    @pytest.mark.parametrize('kind,expr,expected', [
        ('string', 'Returned', {'is': 'Returned'}),
        ('string', 'Complete, Shipped', {'is': ['Complete', 'Shipped']}),
        ('string', '-Cancelled', {'not': 'Cancelled'}),
        ('string', '-Cancelled, -Returned', {'not': ['Cancelled', 'Returned']}),
        ('string', 'NULL', {'is': None}),
        ('string', '00123', {'is': '00123'}),          # numeric-looking strings stay strings
        ('string', '42', {'is': '42'}),
        ('string', 'yes', {'is': 'yes'}),             # yes/no only means boolean on yesno fields
        ('number', '42', {'is': 42}),
        ('number', '1, 3, 5', {'is': [1, 3, 5]}),
        ('number', '>=100', {'greater_than_or_equal_to': 100}),
        ('number', '<5.5', {'less_than': 5.5}),
        ('number', '>-5', {'greater_than': -5}),
        ('number', '<>5', {'not': 5}),
        ('number', '!=5', {'not': 5}),
        ('number', 'NULL', {'is': None}),
        ('number', 'NOT NULL', {'not': None}),
        ('yesno', 'yes', {'is': True}),
        ('yesno', 'No', {'is': False}),
    ])
    def test_translatable(self, kind, expr, expected):
        assert translate_filter_expression(expr, kind) == expected

    @pytest.mark.parametrize('kind,expr', [
        ('string', '%Ship%'), ('string', 'Ship%'), ('string', 'Complete_Order'), ('string', '[0, 10]'),
        ('string', 'last 7 days'), ('string', '90 days'), ('string', 'NOT Complete'), ('string', 'EMPTY'),
        ('string', '-NULL'), ('string', '-Complete, Shipped'), ('string', 'Santa Cruz^, CA'), ('string', ''),
        ('number', '-5'), ('number', 'NOT 5'), ('number', '[1, 10]'), ('number', '>=10 AND <=20'),
        ('number', 'abc'), ('number', '5, abc'),
        ('yesno', 'true'), ('yesno', '1'),
        (None, 'Returned'),
    ])
    def test_untranslatable(self, kind, expr):
        assert translate_filter_expression(expr, kind) is None

    @pytest.mark.parametrize('looker_type,kind', [
        (None, 'string'), ('string', 'string'), ('zipcode', 'string'), ('number', 'number'),
        ('yesno', 'yesno'), ('time', None), ('measure', None), ('tier', None),
    ])
    def test_filter_kind(self, looker_type, kind):
        assert filter_kind(looker_type) == kind

    def test_lkml_filters_all_shape(self):
        measure = {'filters__all': [[{'status': 'Returned'}, {'users.country': 'Japan'}]]}
        assert extract_measure_filters(measure) == [('status', 'Returned'), ('users.country', 'Japan')]

    def test_measure_filters_translated_by_field_type(self, applier):
        result = applier.convert_measure('m', {'type': 'count', 'filters__all': [[
            {'status': 'Returned'}, {'order_id': '>=10'}, {'is_returned': 'no'}, {'users.country': '00123'}
        ]]})
        assert result.omni_field.filters == {
            'status': {'is': 'Returned'},
            'order_id': {'greater_than_or_equal_to': 10},
            'is_returned': {'is': False},
            'users.country': {'is': '00123'},
        }
        assert {'status', 'order_id', 'is_returned'} <= result.dependencies
        assert 'users.country' in result.cross_view_refs

    @pytest.mark.parametrize('filters', [
        {'status': '%Ship%'},           # wildcard
        {'created': '90 days'},         # dimension group (dates)
        {'count': '>5'},                # measure
        {'unknown_field': 'x'},         # unresolvable type
        {'users.unknown': 'x'},         # unresolvable cross-view type
        {'zip': '-NULL'},
        {'order_id': '-5'},
    ])
    def test_untranslatable_filter_blocks_measure(self, applier, filters):
        result = applier.convert_measure('x', {'type': 'count', 'filters__all': [[filters]]})
        assert result.omni_field is None
        assert 'MANUAL ACTION REQUIRED' in result.warnings[0]


class TestDimensionGroups:
    def test_time_group(self, applier):
        result = applier.convert_dimension_group(
            'created', {'type': 'time', 'timeframes': ['raw', 'date', 'month'], 'sql': '${TABLE}.created_at'}
        )
        dim = result.omni_field
        assert dim.sql == 'created_at'
        assert dim.timeframes is None and dim.datatype is None
        assert 'date' in dim.tags
        assert any('raw, date, month' in c for c in result.changes)

    def test_standard_group_sql_omitted(self, applier):
        result = applier.convert_dimension_group('created', {'type': 'time', 'sql': '${TABLE}.created'})
        assert result.omni_field.sql is None

    @pytest.mark.parametrize('extra', [
        {'datatype': 'epoch'}, {'datatype': 'yyyymmdd'}, {'convert_tz': 'no'}, {'sql_date': 'x'},
    ])
    def test_temporal_semantics_not_converted(self, applier, extra):
        result = applier.convert_dimension_group('created', {'type': 'time', 'sql': '${TABLE}.created_at', **extra})
        assert result.omni_field is None
        assert 'MANUAL ACTION REQUIRED' in result.warnings[0]

    @pytest.mark.parametrize('datatype', ['timestamp', 'datetime', 'date'])
    def test_plain_datatypes_converted(self, applier, datatype):
        result = applier.convert_dimension_group('created', {'type': 'time', 'sql': '${TABLE}.created_at', 'datatype': datatype})
        assert result.omni_field.sql == 'created_at'

    def test_duration_not_converted(self, applier):
        result = applier.convert_dimension_group('fulfillment', {'type': 'duration', 'sql_start': 'a', 'sql_end': 'b'})
        assert result.omni_field is None


class TestCanonicalMapping:
    def test_empty_by_default(self):
        result = RuleApplier(phase=0).convert_dimension('sku', {'label': 'SKU Code'})
        assert result.omni_field.label == 'SKU Code'

    def test_configured_mapping_applies_to_labels(self):
        applier = RuleApplier(phase=0, canonical_mappings={'sku': 'product'})
        result = applier.convert_dimension('sku', {'label': 'SKU Code'})
        assert result.omni_field.label == 'Product Code'
        assert result.omni_field.sql is None  # names and SQL untouched

    def test_mapping_terms_are_escaped(self):
        applier = RuleApplier(phase=0, canonical_mappings={'c++': 'cpp', 'a.b': 'x'})
        label, _ = applier.apply_canonical_mapping('aXb total')
        assert label == 'aXb total'

    def test_synonym_from_mapping(self):
        applier = RuleApplier(phase=0, canonical_mappings={'sku': 'product'})
        result = applier.convert_dimension('product_code', {'sql': '${TABLE}.product_code'})
        assert 'sku_code' in result.omni_field.synonyms

    def test_synonyms_token_aware_and_on_all_field_types(self):
        applier = RuleApplier(phase=0, canonical_mappings={'sku': 'product'})
        assert applier.canonical_synonyms('byproduct_flag') == []
        assert applier.canonical_synonyms('Product_count') == ['sku_count']
        measure = applier.convert_measure('product_count', {'type': 'count'})
        assert measure.omni_field.synonyms == ['sku_count']
        group = applier.convert_dimension_group('product_created', {'sql': '${TABLE}.created_at'})
        assert group.omni_field.synonyms == ['sku_created']
