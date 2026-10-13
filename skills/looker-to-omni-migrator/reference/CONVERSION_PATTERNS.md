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

### Dropped parameters

`html`, `link`, `drill_fields` and `map_layer_name` are removed and the field is kept. The report notes each under Changes Made → Other, for example `country (dimension): Not migrated: map_layer_name`.

### Not converted

`type: tier`, `location` or `distance`, `case`, `tiers`, Liquid, quoted `${TABLE}."col"`, a bare `${TABLE}`, or `${...}` inside a string literal or comment. The field is not written:

```
price_tier: MANUAL ACTION REQUIRED: dimension type 'tier' has no automatic Omni conversion
```

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

Not converted: `type: duration`, `datatype: epoch`/`yyyymmdd`, `convert_tz: no`, `sql_date`, `sql_start`/`sql_end`:

```
fulfillment: MANUAL ACTION REQUIRED: dimension_group type 'duration' has no automatic Omni conversion
```

A field that references a timeframe, such as `${created_date}`, is not written; Omni has no field by that name.

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
| `join: visitor { from: users }` | `join_to_view: users`, `join_to_view_as: visitor` |
| Liquid in `sql_on` | join not migrated; original SQL in the report |
| `access_filter` | topic `access_filters` |
| `sql_always_where` | topic `always_where_sql` |
| `always_filter` | reported only |

## See also

- [OUTPUT_FILES.md](OUTPUT_FILES.md)
- [../docs/QUERY_VIEWS.md](../docs/QUERY_VIEWS.md)
