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
- Other topic keys the migrator can write: `always_having_sql`, `hidden`, `group_label`, `ai_context`.
- When curating a topic for business users, prefer an explicit field list over wildcards.

## 9. Relationships

A `.relationships` file is a plain YAML list:

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
- Relationships apply to the whole model, not one topic. Define each pair once.

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
- Lists can be `[a, b]` or `- a`.

If a file does not parse, Omni may ignore the whole view.
