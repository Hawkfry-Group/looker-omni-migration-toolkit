# Conversion Patterns

Before and after examples, taken from real runs on `examples/simple/thelook/` and `tests/fixtures/thelook/`. Generated `label`, `description` and `ai_context` lines are trimmed. Full rules: [../rules/migration_rules.md](../rules/migration_rules.md).

## Dimensions

### Column with the same name

```lkml
dimension: status {
  type: string
  sql: ${TABLE}.status ;;
}
```

```yaml
status:
  label: Status
  tags: [mig_phase_0, verified]
```

`type:` is dropped and `sql:` is omitted, because Omni reads the `status` column directly.

### Primary key

```lkml
dimension: id {
  primary_key: yes
  type: number
  sql: ${TABLE}.id ;;
}
```

```yaml
id:
  label: Id
  tags: [id, mig_phase_0, verified]
  primary_key: true
```

### SQL expression

```lkml
dimension: sale_price {
  type: number
  sql: COALESCE(${TABLE}.sale_price, 0) ;;
  value_format_name: usd
}
```

```yaml
sale_price:
  sql: COALESCE(sale_price, 0)
  format: usdcurrency_2
```

`${TABLE}.sale_price` becomes the raw column `sale_price`, not `${sale_price}`, which would refer to the field itself. If a different dimension's SQL were exactly `${TABLE}.sale_price`, the reference would become `${that_dimension}`.

### Yes/no

```lkml
dimension: is_returned {
  type: yesno
  sql: ${TABLE}.returned_at IS NOT NULL ;;
}
```

```yaml
is_returned:
  sql: returned_at IS NOT NULL
```

### Cross-view reference

```lkml
dimension: gross_margin {
  type: number
  sql: ${sale_price} - ${inventory_items.cost} ;;
  required_access_grants: [can_view_financials]
}
```

```yaml
gross_margin:
  sql: ${sale_price} - ${inventory_items.cost}
  required_access_grants: [can_view_financials]
```

`${sale_price}` (same view) makes `sale_price` a dependency. `${inventory_items.cost}` is kept; `inventory_items` must be joined in the topic that uses this field. The grant is copied; define it in Omni.

### Cross-view reference through a join alias

```lkml
# view: sales_fact
dimension: store_label { sql: ${shop.store_name} ;; }

# explore: sales { from: sales_fact  join: shop { from: stores ... } }
```

`shop` is a join alias, not a view. In a view file Omni rejects the field ("No such view"), so view mode leaves it out and lists it under "Topic-scoped fields". `--explore sales` writes it in the topic:

```yaml
base_view: sales
views:
  sales:
    extends: [sales_fact]
    dimensions:
      store_label:
        sql: ${shop.store_name}
```

A topic where `shop` is missing, or stands for a view without `store_name`, does not get the field; the report says why.

### Dropped parameters

`html`, `link`, `drill_fields` and `map_layer_name` are removed and the field is kept. The report notes each under Changes Made → Other, for example `country (dimension): Not migrated: map_layer_name`.

### Not converted

`type: tier`, `location` or `distance`, `case`, `tiers`, Liquid with no exact Omni equivalent, quoted `${TABLE}."col"`, a bare `${TABLE}`, or `${...}` inside a string literal or comment. The field is not written:

```
price_tier: MANUAL ACTION REQUIRED: dimension type 'tier' has no automatic Omni conversion
metric_label: MANUAL ACTION REQUIRED: sql uses Liquid that is not converted automatically: `{% if %}` (conditional logic: Omni cannot remove SQL text); `{% else %}` (...); `{% endif %}` (...)
```

## Liquid, parameters and filter-only fields

### Parameter and filter-only field

```lkml
parameter: metric {
  type: unquoted
  allowed_value: { label: "Revenue" value: "revenue" }
  allowed_value: { label: "Cost" value: "cost" }
  default_value: "revenue"
}

filter: status_pick {
  type: string
  suggest_dimension: order_items.status
  default_value: "Complete"
}

dimension: selected_metric {
  type: number
  sql: ${TABLE}.{% parameter metric %} ;;
}
```

```yaml
dimensions:
  selected_metric:
    sql: |-
      {{ filters.sales_by_period.metric.value }}

filters:
  metric:
    type: column
    suggestion_list:
    - value: revenue
      label: Revenue
    - value: cost
      label: Cost
    default_filter:
      is: revenue
    filter_single_select_only: true

  status_pick:
    type: string
    suggest_from_field: order_items.status
    default_filter:
      is: Complete
```

`metric` is `type: unquoted` and used as a column of the table, so it becomes `type: column` (Omni quotes the value as an identifier) and `${TABLE}.` is dropped. Used as `'{% parameter metric %}'` it would become `type: string`, with the quotes dropped.

### Liquid in SQL

| Looker | Omni |
|--------|------|
| `{% condition view.f %} expr {% endcondition %}` | `{{# view.f.filter }} expr {{/ view.f.filter }}` |
| `{% parameter view.p %}`, `{{ view.p._parameter_value }}` | `{{ filters.view.p.value }}` |
| `{% date_start view.f %}`, `{% date_end view.f %}` | `{{ filters.view.f.range_start }}`, `{{ filters.view.f.range_end }}` |
| `{{ view.f._in_query }}`, `._is_selected`, `._is_filtered` (used as a value) | `{{ view.f.in_query }}`, `.is_selected`, `.is_filtered` |
| `{{ view._in_query }}` | `{{ view.in_query }}` |
| `{{ _user_attributes['x'] }}` | `{{ omni_attributes.x }}` |
| `{% if %}`, `{% elsif %}`, `{% else %}`, `{% assign %}`, loops, Liquid filters (`\| upcase`), any other `{{ ... }}` | not converted (MANUAL); the report names the construct |

A SQL block converts only when every construct in it converts. Quote handling is exact:

| Looker SQL | Result |
|------------|--------|
| `x = {% parameter p %}` (string or number) | `x = {{ filters.view.p.value }}` |
| `x = '{% parameter p %}'` (string, or unquoted used as a string) | `x = {{ filters.view.p.value }}` (Omni adds the quotes) |
| `x LIKE '%{% parameter p %}%'`, `"prefix_{% parameter p %}"` | not converted: the tag is embedded in a longer string or identifier |
| `${TABLE}.{% parameter p %}`, `FROM {% parameter p %}`, `"{% parameter p %}"` (unquoted) | `{{ filters.view.p.value }}` with `type: column` |
| `DATE_TRUNC({% parameter p %}, x)` (unquoted) | not converted: the value may be a keyword |
| `region = '{{ _user_attributes['region'] }}'` | `region = '{{ omni_attributes.region }}'` (quotes kept) |

Rules and edge cases: [../rules/migration_rules.md](../rules/migration_rules.md).

## Dates

```lkml
dimension_group: created {
  type: time
  timeframes: [raw, date, month, year]
  sql: ${TABLE}.created_at ;;
}
```

```yaml
created:
  tags: [date, temporal, mig_phase_0, verified]
  sql: created_at
```

One dimension named after the group. No `timeframes:` or `datatype:`; Omni generates timeframes. The report notes `Looker timeframes [raw, date, month, year] not emitted (Omni generates timeframes)`.

`convert_tz: no` is carried (the field keeps the database timezone, with no conversion); `convert_tz: yes` or no `convert_tz` writes nothing:

```lkml
dimension_group: visited {
  type: time
  datatype: date
  convert_tz: no
  sql: ${TABLE}.visit_date ;;
}
```

```yaml
visited:
  tags: [date, temporal, mig_phase_0, verified]
  sql: visit_date
  convert_tz: false
```

Not converted: `type: duration`, `datatype: epoch`/`yyyymmdd`, `sql_date`, `sql_start`/`sql_end`:

```
fulfillment: MANUAL ACTION REQUIRED: dimension_group type 'duration' has no automatic Omni conversion
```

A reference to the `date` or `raw` timeframe is rewritten to Omni's form. Any other timeframe is not written, because Looker renders it as a string or an integer and Omni as a date:

```lkml
dimension: created_day { sql: CAST(${created_date} AS VARCHAR) ;; }
dimension: created_month_text { sql: ${created_month} ;; }
```

```yaml
created_day:
  sql: CAST(${created[date]} AS VARCHAR)
```

```
created_month_text: MANUAL ACTION REQUIRED: `${created_month}` references the `month` timeframe of dimension group `created`. Only the `date` and `raw` timeframes are rewritten: ...
```

A real dimension named `signup_date` is not a timeframe reference and is left alone.

## View inheritance

### Template view

```lkml
view: base_entity {
  extension: required
  dimension: id { primary_key: yes type: number sql: ${TABLE}.id ;; }
  dimension_group: created { type: time sql: ${TABLE}.created_at ;; }
}
```

```yaml
# base_entity.view.yaml (no schema, no table_name)
template: true

dimensions:
  id:
    primary_key: true
  created:
    sql: created_at
```

A view with no `sql_table_name` and no `derived_table` that other views extend, and that no explore uses as its base or a join target, is written the same way.

### Extending view

```lkml
view: orders {
  extends: [base_entity, audit_fields]
  sql_table_name: shop.orders ;;
  dimension: status_upper { sql: UPPER(${status}) ;; }   # status is inherited
}
```

```yaml
schema: shop
table_name: orders
extends: [base_entity, audit_fields]

dimensions:
  status_upper:
    sql: UPPER(${status})
```

Inherited fields are not copied. A reference to one is valid when the parent view migrates that field; otherwise the dependent field is not written:

```
tier_label: MANUAL ACTION REQUIRED: depends on inherited field price_tier (from view base_entity), which is not migrated (...) - not written
```

### Parents that disagree

If `base_entity` and `audit_fields` both define `status` with different SQL, Looker uses `audit_fields` (the last parent) and Omni would use `base_entity` (the first). The migrator writes the Looker definition into the child:

```yaml
dimensions:
  status:
    sql: UPPER(status)
    tags: [mig_phase_0, verified, resolved_from_extends]
```

Outside the CSV scope the field is written `hidden: true` with the tags `in_omni_in_looker_not_csv` and `resolved_from_extends`.

## Measures

### Aggregates

```lkml
measure: order_count {
  type: count_distinct
  sql: ${order_id} ;;
  value_format_name: decimal_0
}
```

```yaml
order_count:
  aggregate_type: count_distinct
  sql: ${order_id}
  format: number_0
  tags: [metric, mig_phase_0, verified]
```

Same pattern for `count`, `sum`, `average`, `min`, `max`, `median`, `list`. `percentile` also writes `percentile: N`.

### Calculated

```lkml
measure: gross_margin_per_order {
  type: number
  sql: ${total_gross_margin} / NULLIF(${order_count}, 0) ;;
}
```

```yaml
gross_margin_per_order:
  sql: ${total_gross_margin} / NULLIF(${order_count}, 0)
```

`type: number`, `string`, `yesno` and `date` measures keep their SQL with no `aggregate_type`. Referenced measures (`total_gross_margin`) are auto-included.

### Filtered

```lkml
measure: returned_count {
  type: count
  filters: [status: "Returned"]
}

measure: completed_or_shipped_count {
  type: count
  filters: [status: "Complete, Shipped"]
}

measure: large_order_count {
  type: count_distinct
  sql: ${order_id} ;;
  filters: [sale_price: ">=100"]
}
```

```yaml
returned_count:
  aggregate_type: count
  filters:
    status:
      is: Returned

completed_or_shipped_count:
  aggregate_type: count
  filters:
    status:
      is:
      - Complete
      - Shipped

large_order_count:
  aggregate_type: count_distinct
  sql: ${order_id}
  filters:
    sale_price:
      greater_than_or_equal_to: 100
```

A filter outside the supported grammar stops the measure:

```lkml
measure: wildcard_status_count {
  type: count
  filters: [status: "%Ship%"]
}
```

```
wildcard_status_count: MANUAL ACTION REQUIRED: filter status: "%Ship%" (field type: string) cannot be translated automatically
```

The full grammar is in [../rules/migration_rules.md](../rules/migration_rules.md#measure-filters).

### Unsupported types

```
running_total_sales: MANUAL ACTION REQUIRED: Looker measure type 'running_total' has no automatic Omni conversion
```

Also `percent_of_total`, `sum_distinct`, `period_over_period` and any other type not listed above.

## Formats

| Looker `value_format_name` | Omni `format` |
|----------------------------|---------------|
| `decimal_0` … `decimal_4` | `number_0` … `number_4` |
| `percent_0` … `percent_4` | `percent_0` … `percent_4` |
| `usd`, `usd_0` | `usdcurrency_2`, `usdcurrency_0` |
| `eur`, `eur_0` | `eurcurrency_2`, `eurcurrency_0` |
| `gbp`, `gbp_0` | `gbpcurrency_2`, `gbpcurrency_0` |
| `id` | `id` |

Anything else is not written, with a MEDIUM risk:

```
total_sales_eur: Custom value_format '€#,##0.00' not migrated; set an Omni format manually
```

## Canonical mappings

Empty unless set with `--config`:

```yaml
canonical_mappings:
  sku: product
```

With that config, a field labelled "SKU Code" is labelled "Product Code". A field named `product_id` also gets the synonym `sku_id`. Matching is case-insensitive on whole words and whole `_`-separated name tokens. Descriptions, SQL and field names are unchanged. Applied mappings are listed under Changes Made → Canonical Mappings.

## Explores

| Looker | Omni |
|--------|------|
| `type: left_outer` or no type | `join_type: always_left` |
| `type: inner` / `full_outer` / `cross` | `inner` / `full_outer` / `cross` |
| no `relationship` | `relationship_type: many_to_one` |
| `join: visitor { from: users }` | `join_to_view: users`, `join_to_view_as: visitor` (scoped to the topic) |
| `explore: sales { from: sales_fact }` | `base_view: sales` and `views: { sales: { extends: [sales_fact] } }` |
| `view_label: "Shop"` on the explore or a join | `views: { <name>: { label: Shop } }` when it differs from the view's label |
| `${orders.created_date}` in `sql_on` | `${orders.created[date]}` |
| Liquid in `sql_on` | join not migrated; original SQL in the report |
| `access_filter` | topic `access_filters` |
| `sql_always_where` | topic `always_where_sql` |
| `always_filter: { filters: [orders.created_date: "7 days ago for 7 days"] }` | topic `default_filters`: `orders.created: { time_for_duration: [7 complete days ago, 7 days], required: true }` |
| `always_filter` with a string value `global`, a list `a,b`, or a number `5` | `is: global`, `is: [a, b]`, `is: 5`, each with `required: true` |
| `always_filter` on a date field with a named period (`last month`, `this week`, `today`, `yesterday`, ...) | `is: last month`, with `required: true`; periods other than `last month` are flagged for verification |
| `always_filter` with any other expression (`7 days`, `next month`, `-x`, `>5`) or no value | not written; reported with the Looker expression |
| explore `fields: [ALL_FIELDS*, -orders.cost]` | topic `fields: [all_views.*, -orders.cost, "-tag:table_key"]` |
| explore `fields: [orders.id, users.name]` | topic `fields: [-all_views.*, orders.id, users.name, "-tag:table_key"]` |
| join `fields: [users.id, users.name]` | `-users.*, users.id, users.name` in the topic `fields:` |
| join `fields: []` / `fields: [-users.email]` | `-users.*` / `-users.email` |
| `users.contact*` (a set) in a `fields:` list | its member fields, nested and inherited sets expanded; an undefined set leaves the list uncarried (reported) |
| `users.created_date` in a `fields:` list | `users.created`; excluding only some timeframes of a group leaves the list uncarried (reported) |
| `conditionally_filter` | reported only |
| `extends: [base]` | topic `extends: [base]` plus the merged joins, relationships and settings written into the topic |
| `extension: required` (or `--template`) | topic `template: true` |

Joins are written under the topic's `relationships:` key next to `joins:`:

```yaml
base_view: order_items
relationships:
- join_from_view: order_items
  join_to_view: users
  join_type: always_left
  on_sql: ${order_items.user_id} = ${users.id}
  relationship_type: many_to_one
  reversible: false
joins:
  users: {}
```

`--relationships-file` writes the same list to `{explore}.relationships` instead.

## See also

- [OUTPUT_FILES.md](OUTPUT_FILES.md)
- [../docs/QUERY_VIEWS.md](../docs/QUERY_VIEWS.md)
