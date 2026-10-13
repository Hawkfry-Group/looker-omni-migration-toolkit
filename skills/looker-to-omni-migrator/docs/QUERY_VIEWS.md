# Derived Tables to Omni Query Views

Looker derived tables become Omni **query views**, saved as `{view}.query.view`:

| Looker | Omni query view |
|--------|-----------------|
| SQL derived table (`derived_table: { sql: ... }`) | Top-level `sql:` with the raw SQL |
| Native derived table (`derived_table: { explore_source: ... }`) | Top-level `query:` with `fields`, `base_view`, `topic`, `filters`, `sorts`, `limit` |

The migrator chooses the file type automatically: a view with a `derived_table` block produces `{view}.query.view`; any other view produces `{view}.view.yaml`. Dimensions and measures come from the fields declared in the LookML view, converted with the normal rules.

Persistence settings (`datagroup_trigger`, `sql_trigger_value`, `persist_for` and similar) are reported as not migrated. Configure caching in Omni or materialise in the warehouse if you need it.

## SQL derived tables

Input (`tests/fixtures/thelook/views/user_order_facts.view.lkml`, abridged):

```lkml
view: user_order_facts {
  derived_table: {
    sql:
      SELECT
        user_id,
        COUNT(DISTINCT order_id) AS lifetime_orders,
        SUM(sale_price) AS lifetime_revenue,
        MIN(created_at) AS first_order_at
      FROM thelook_ecommerce.order_items
      GROUP BY user_id ;;
    datagroup_trigger: nightly
  }

  dimension: user_id {
    primary_key: yes
    type: number
    sql: ${TABLE}.user_id ;;
  }
}
```

Output (`user_order_facts.query.view`, abridged):

```yaml
schema: thelook_ecommerce
folder: thelook_ecommerce/curated
label: User Order Facts
description: Lifetime order totals per user, built from order_items.
tags: [user, fact, mig_phase_0, verified]
sql: |-
  SELECT user_id,
         COUNT(DISTINCT order_id) AS lifetime_orders,
         SUM(sale_price) AS lifetime_revenue,
         MIN(created_at) AS first_order_at
  FROM thelook_ecommerce.order_items
  GROUP BY user_id

dimensions:
  user_id:
    label: User Id
    tags: [id, mig_phase_0, verified]
    primary_key: true
```

Notes:

- The SQL goes under `sql:` and is reformatted with `sqlparse`; Looker's `;;` is removed.
- The schema is read from the table names in the SQL where possible, otherwise `--default-schema`.
- Liquid or `${...}` references (for example `${other_view.SQL_TABLE_NAME}`) in the SQL stop the conversion: MANUAL ACTION REQUIRED, exit 1, nothing is written for the view.

## Native derived tables

Input (`tests/fixtures/thelook/views/brand_sales.view.lkml`):

```lkml
view: brand_sales {
  derived_table: {
    explore_source: order_items {
      column: brand { field: products.brand }
      column: total_sales { field: order_items.total_sale_price }
      filters: [order_items.status: "Complete"]
    }
  }

  dimension: brand {
    primary_key: yes
    type: string
    sql: ${TABLE}.brand ;;
  }

  dimension: total_sales {
    type: number
    sql: ${TABLE}.total_sales ;;
    value_format_name: usd
  }
}
```

Output (`brand_sales.query.view`, abridged):

```yaml
query:
  fields:
    products.brand: brand
    order_items.total_sale_price: total_sales
  base_view: order_items
  filters:
    order_items.status:
      is: Complete
  topic: order_items

dimensions:
  brand:
    primary_key: true
  total_sales:
    format: usdcurrency_2
```

- `base_view` is the explore's real base view; `topic` is the explore name. The topic must exist in Omni (migrate the explore too).
- `sorts: [field: desc]` become `sorts: [{field, desc: true}]`; `limit` is kept.
- Filters are translated with the same rules as measure filters (see [../rules/migration_rules.md](../rules/migration_rules.md)).
- Only columns declared as dimensions or measures in the LookML view appear under `dimensions:`/`measures:`. If the LookML view selects columns it never declares, add them by hand.
- The view **fails** (exit code 1, nothing written) if the native derived table uses `bind_filters`, `bind_all_filters`, `derived_column`, selects the same field twice, or has a filter that cannot be translated. Migrate those by hand.

## Query view keys

| Key | Purpose |
|-----|---------|
| `query.fields` | `view.field: alias`. Time fields use brackets, for example `users.created_at[year]: created_year`. |
| `query.base_view` | The view the query starts from |
| `query.topic` | The topic the query runs against |
| `query.filters` | `view.field: { is: value }`, `{ not: value }`, `{ is: null }`, comparisons such as `{ greater_than_or_equal_to: 100 }` |
| `query.sorts` | List of `{ field: view.field, desc: true }` |
| `query.limit` | Row limit |
| `dimensions`, `measures` | Top level, not under `query` |

A hand-finished example is in `examples/omni_model/customer_order_summary.query.view` (native) and `examples/omni_model/fx_sales.query.view` (SQL).

## Common mistakes

- Using `.view.yaml` for a derived table. Use `.query.view`.
- Writing `${TABLE}.column` in query view SQL. Use the column name.
- Putting raw SQL under `query:`. Raw SQL goes under `sql:`; `query:` is for structured Omni queries.

## Further reading

- Query views: <https://docs.omni.co/modeling/query-views>
- Omni documentation index: <https://docs.omni.co/llms.txt>
