# Omni Model Syntax Reference

The Omni YAML syntax the migrator writes and that Claude should follow when editing its output. It exists to prevent invalid YAML, such as Looker keys (`type:`, `value_format:`) inside Omni fields. For anything not covered here, check the Omni documentation: <https://docs.omni.co>.

---

## 1. Layers

Omni models have two layers:

1. **Schema layer.** Generated from the warehouse and refreshed automatically. Do not store curated semantics here.
2. **Curated layer.** Hand-maintained views, fields, labels, AI context and topics. The migration targets this layer.

## 2. File types

| File | Purpose |
|------|---------|
| `{view}.view.yaml` | View backed by a warehouse table |
| `{view}.query.view` | View backed by a query: raw SQL (`sql:`) or a native Omni query (`query:`) |
| `{topic}.topic.yaml` | Topic: a curated dataset with a base view and joins |
| `{name}.relationships` | Join definitions between views |

### 2.1 Table view

```yaml
schema: thelook_ecommerce
folder: thelook_ecommerce/curated
table_name: order_items
# catalog: my-project   # written for three-part sql_table_name values

label: "Order Items"
description: "One row per item sold."
ai_context: |
  Grain: one row per order item.
  Join keys: order_id (orders), user_id (users), product_id (products).
tags: [sales, mig_phase_0, verified]

dimensions:
  id:
    primary_key: true
    label: "Order Item ID"
    description: "Unique identifier of the order item."
    tags: [id, mig_phase_0, verified]

measures:
  count:
    aggregate_type: count
    label: "Order Item Count"
    tags: [metric, mig_phase_0, verified]
```

### 2.2 Query view (raw SQL)

```yaml
schema: thelook_ecommerce
sql: |-
  SELECT user_id, COUNT(*) AS order_count
  FROM thelook_ecommerce.orders
  GROUP BY 1

label: "Customer Order Counts"
tags: [sales, mig_phase_0, verified]

dimensions:
  user_id:
    primary_key: true
  order_count: {}
```

Raw SQL goes under `sql:`. A structured `query:` block is only for native Omni queries; see [../docs/QUERY_VIEWS.md](../docs/QUERY_VIEWS.md).

### 2.2.1 Filter-only fields and Mustache

A filter-only field holds no column. It lives under the view's top-level `filters:` key and its value is read in SQL through Mustache. The migrator writes one for each Looker `filter:` and `parameter:`.

```yaml
sql: |-
  SELECT user_id, SUM(sale_price) AS revenue
  FROM thelook_ecommerce.order_items
  WHERE {{# sales_by_period.order_date.filter }} created_at {{/ sales_by_period.order_date.filter }}
    AND sale_price >= {{ filters.sales_by_period.min_sale_price.value }}
  GROUP BY 1

filters:
  order_date:
    type: timestamp
  min_sale_price:
    type: number
    default_filter:
      is: 0
    filter_single_select_only: true
```

| Token | Renders |
|-------|---------|
| `{{# view.field.filter }} expr {{/ view.field.filter }}` | the filter applied to `expr` (TRUE when the filter is unset). No `filters.` prefix |
| `{{ filters.view.field.value }}` | the value: a `string` as a quoted literal, a `number` bare, a `column` as a quoted identifier. Never add quotes around it |
| `{{ filters.view.field.range_start }}`, `.range_end` | the bounds of a `timestamp` filter |
| `{{ view.field.in_query }}`, `.is_selected`, `.is_filtered`, `{{ view.in_query }}` | TRUE or FALSE |
| `{{ omni_attributes.name }}` | a user attribute, interpolated raw |

- Filter-only field types: `string`, `number`, `timestamp`, `boolean`, `column`. There is no `date` type.
- Keys the migrator writes: `type`, `label`, `description`, `group_label`, `hidden`, `suggestion_list` (items with `value` and an optional `label`), `suggest_from_field`, `default_filter`, `filter_single_select_only`.
- The view name in a token is the Omni view that has the field. A view that extends another names itself.
- Mustache has no equivalent of Liquid `{% if %}`: it cannot remove SQL text.

### 2.3 Template views and `extends`

```yaml
# base_entity.view.yaml: a template has no schema and no table_name
folder: analytics/curated
label: Base Entity
tags: [base, fact, mig_phase_0, verified]
template: true

dimensions:
  id:
    primary_key: true
    tags: [id, mig_phase_0, verified]
```

```yaml
# orders.view.yaml
schema: shop
table_name: orders
extends: [base_entity, audit_fields]
```

- `template: true` marks a view that only exists to be extended. It has no `schema` and no source (`table_name`, `sql`, `query`).
- `extends:` is a list. The extending view inherits the fields of the listed views and overrides them with its own.
- When two views in `extends:` define the same field, Omni uses the FIRST one in the list. Looker uses the last. The migrator keeps the Looker order and writes any field the parents disagree on into the extending view, tagged `resolved_from_extends`.
- A field of the extending view may reference an inherited field with `${field}`.

## 3. View-level metadata

| Key | Content |
|-----|---------|
| `label` | Business name, not the table name |
| `description` | One or two sentences, including grain |
| `ai_context` | Grain, join keys, filters and exclusions, business rules, example questions |
| `tags` | Domain tags plus `mig_phase_N` and `verified` or `unverified` |

## 4. Dimensions

```yaml
dimensions:
  full_name:
    sql: CONCAT(${first_name}, ' ', ${last_name})
    label: "Full Name"
    description: "Customer first and last name."
```

- **No `type:`.** Omni infers it. For booleans, write SQL that returns true/false.
- **No `html:`.** Drop it and keep the field.
- **Raw columns** are written bare (`sql: email`), never `${TABLE}.email`. `${email}` means the modeled field `email`; the migrator writes it only when a dimension of that name reads exactly that column.
- **Same-view fields** use `${field}`. **Other views** use `${view.field}`.
- **SQL is an expression**, never a `SELECT`.
- **One primary key per view:** `primary_key: true`.
- **Hidden:** `hidden: true`.

## 5. Dates

Write a date or timestamp column as a single dimension. Omni provides timeframes (date, week, month, quarter, year and so on) from the column type.

```yaml
dimensions:
  created:
    sql: created_at
    label: "Created"
    description: "When the order was placed."
```

The migrator writes neither `timeframes:` nor `datatype:`. If you need to change the available timeframes by hand, check the Omni docs for the current syntax first. Looker timeframe fields such as `${created_date}` have no Omni equivalent field.

## 6. Measures

```yaml
measures:
  total_sale_price:
    aggregate_type: sum
    sql: ${sale_price}
    label: Total Sales
    format: usdcurrency_2
```

`aggregate_type` values the migrator writes: `count`, `count_distinct`, `sum`, `average`, `min`, `max`, `median`, `list`, `percentile` (with `percentile: N`). Omni also has `*_distinct_on` variants (`sum_distinct_on`, `average_distinct_on`, `median_distinct_on`, `percentile_distinct_on`, which need `custom_primary_key_sql`); the migrator does not write these, so Looker `sum_distinct` and similar are manual.

A calculated measure has no `aggregate_type` and references other measures:

```yaml
measures:
  gross_margin_per_order:
    sql: ${total_gross_margin} / NULLIF(${order_count}, 0)
    format: usdcurrency_2
```

Measure filters are keyed by field, with a condition:

```yaml
measures:
  returned_count:
    aggregate_type: count
    filters:
      status:
        is: Returned
  large_order_count:
    aggregate_type: count_distinct
    sql: ${order_id}
    filters:
      sale_price:
        greater_than_or_equal_to: 100
```

Conditions the migrator writes: `is`, `not` (single value, list, or `null`), `greater_than`, `greater_than_or_equal_to`, `less_than`, `less_than_or_equal_to`.

## 7. Formats

`format:` replaces Looker's `value_format_name`. It controls presentation only; never use it to scale values. Mapped automatically by the migrator:

| Looker | Omni |
|--------|------|
| `decimal_0` … `decimal_4` | `number_0` … `number_4` |
| `percent_0` … `percent_4` | `percent_0` … `percent_4` |
| `usd`, `usd_0` | `usdcurrency_2`, `usdcurrency_0` |
| `eur`, `eur_0` | `eurcurrency_2`, `eurcurrency_0` |
| `gbp`, `gbp_0` | `gbpcurrency_2`, `gbpcurrency_0` |
| `id` | `id` |

For any other Looker format, choose an Omni named format by hand: <https://docs.omni.co/modeling/models/format-values>.

## 8. Topics

```yaml
base_view: order_items
label: Orders
description: Order line items with shopper, product and inventory context
fields: [all_views.*, "-tag:table_key"]
joins:
  orders: {}
  users: {}
  inventory_items:
    products:
      distribution_centers: {}
access_filters:
- field: users.country
  user_attribute: allowed_country
always_where_sql: ${order_items.status} != 'Cancelled'
required_access_grants: [can_view_financials]
```

- `joins` is a tree: a view nested under another is joined through it.
- Other topic keys the migrator can write: `always_having_sql`, `hidden`, `group_label`, `ai_context`, `relationships`, `default_filters`, `extends`, `template`.
- `relationships` (topic-scoped join definitions, same keys as section 9) applies to this topic only. `joins` is still required: it declares which views are in the topic and how they nest.
- `default_filters` is keyed by `view.field`. An entry with `required: true` beside its operator can be changed by users but not removed, and must have a value:

```yaml
default_filters:
  order_items.created:
    time_for_duration: [7 complete days ago, 7 days]
    required: true
```

- `extends: [other_topic]` inherits another topic; values set in the extending topic override the inherited ones, and its `joins:` replaces the inherited `joins:`. `template: true` marks a topic that can only be used through `extends`.
- When curating a topic for business users, prefer an explicit field list over wildcards.

## 9. Relationships

Join definitions are a plain YAML list. The migrator writes them inside the topic file under `relationships:` by default, or to a `.relationships` file with `--relationships-file`:

```yaml
- join_from_view: order_items
  join_to_view: inventory_items
  join_type: inner
  on_sql: ${order_items.inventory_item_id} = ${inventory_items.id}
  relationship_type: one_to_one
  reversible: false
- join_from_view: events
  join_to_view: users
  join_to_view_as: visitor
  join_type: always_left
  on_sql: ${events.user_id} = ${visitor.id}
  relationship_type: many_to_one
  reversible: false
```

- `join_type`: `always_left`, `inner`, `full_outer`, `cross`.
- `relationship_type`: `many_to_one`, `one_to_many`, `one_to_one`, `many_to_many`.
- `join_to_view_as` / `join_from_view_as` give an alias for a view joined more than once.
- Relationships in a relationships file apply to the whole model, not one topic: define each pair once. Relationships under a topic's `relationships:` key apply to that topic only.

## 10. Tags

Lowercase snake_case. Migrated views and fields carry `mig_phase_N` and `verified` (or `unverified`):

```yaml
tags: [order, fact, mig_phase_0, verified]
```

Hidden out-of-scope fields carry only `in_omni_in_looker_not_csv`.

## 11. YAML hygiene

- Spaces, never tabs; consistent indentation.
- Keys are case-sensitive.
- Multi-line strings use `|` or `|-`.
- A string that starts with `{{` must be a block scalar or double-quoted. The migrator writes every SQL that holds Mustache as a block scalar.
- Lists can be `[a, b]` or `- a`.

If a file does not parse, Omni may ignore the whole view.
