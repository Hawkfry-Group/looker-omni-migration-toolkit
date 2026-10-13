# Looker to Omni Migration Rules

These are the rules the migrator implements and that Claude follows when reviewing or hand-editing its output. For Omni YAML syntax, see [model_details_omni.md](model_details_omni.md). If the two disagree about Omni syntax, `model_details_omni.md` wins.

Examples use the thelook e-commerce model (`users`, `orders`, `order_items`, `products`, `inventory_items`).

---

## Golden rules

1. **The CSV defines scope.** Rows matching `--phase`, `--owner` and the view are in scope. The migrator never edits the CSV.
2. **One view per run.** Each `--view` run migrates exactly one view. Loop for more.
3. **Never remove existing Omni fields.** When merging, existing fields are kept (hidden if out of scope), never deleted.
4. **Never guess.** Anything that cannot be converted faithfully is not written. It is reported as MANUAL ACTION REQUIRED with a reason.
5. **Never drop a restriction silently.** Access grants, access filters and explore filters are carried or reported.
6. **Annotate opinions.** Hand-made improvements (renames, simplified SQL) get a `# OPINION:` comment in the YAML and a line in the report.
7. **One primary key per view.** `primary_key: yes` is carried over. If the final view has no primary key, the report says so (HIGH risk); add the key to the CSV or set it by hand. The migrator does not pick one.
8. **No parameters or Liquid.** Parameters, Liquid (`{{ }}`, `{% %}`) and fields that use them are not converted.
9. **Resolve dependencies.** If an in-scope field references another field in the same view (`${field}`), that field is migrated too, recursively.
10. **Validate before writing.** The whole view is rendered and validated first; if validation fails, nothing is written for that view.

---

## Scope

| Field | Result |
|-------|--------|
| In CSV scope | Converted, visible, tagged `mig_phase_N` + `verified`/`unverified` |
| Referenced (transitively) by an in-scope field in the same view | Converted, visible, tagged `mig_phase_N` + `verified`/`unverified` + `auto_dependency` |
| Any other convertible field of the view | Converted with its SQL, `hidden: true`, tagged only `in_omni_in_looker_not_csv` |
| Cannot be converted, references an undefined or unconvertible field, or is part of a reference cycle | Not written; reported (this applies to hidden fields too) |
| CSV row with no matching LookML field | Reported as MANUAL ACTION REQUIRED |

A Looker `hidden: yes` is kept, even on in-scope and auto-included fields.
| CSV row with `type: parameter` | Not converted; reported |

### CSV columns

| Column | Required | Meaning |
|--------|----------|---------|
| `phase` | yes | `P0` or `0`; both match `--phase 0` |
| `view` | yes | Looker view name |
| `field` | yes | Looker field name |
| `type` (or `field_type`) | yes | `dimension`, `dimension_group`, `measure`, `parameter` |
| `owner` | yes | Exact, case-sensitive match with `--owner` |
| `omni_view`, `omni_field` | no | Tracking only. Not used for naming. |
| `status`, `notes` | no | For your team |

---

## Finding LookML

- Views, explores and access grants are found in **any** `*.lkml` file under `--looker-repo`, at any depth, including model files.
- A view, explore or grant name defined more than once stops the run (exit 1) with the file locations.
- Refinements (`view: +name`, `explore: +name`) are detected and reported, not applied.
- `extends:` is reported; inherited fields are not included.
- Model `include:` statements are ignored.

---

## File types and locations

| Looker view | Omni file |
|-------------|-----------|
| `sql_table_name` | `{view}.view.yaml` |
| `derived_table` with `sql` | `{view}.query.view` with top-level `sql:` |
| `derived_table` with `explore_source` | `{view}.query.view` with structured `query:` |

The file is named after the Looker view; no suffix is added. Files go to `--omni-repo` (default `migration/staging/views`); the report goes to the sibling `reports/` directory as `{view}.migration.md`.

### View-level keys

```yaml
schema: thelook_ecommerce
folder: thelook_ecommerce/curated
label: Users
description: Customers of the thelook e-commerce sample store. One row per registered customer.
tags: [users, fact, mig_phase_0, verified]
table_name: users
```

- `sql_table_name: schema.table` gives `schema` and `table_name`. A three-part name (`project.dataset.table`) also gives `catalog`. Quoted names are handled.
- `sql_table_name` containing Liquid or `${...SQL_TABLE_NAME}`, more than three parts, or anything unparseable: the view fails (exit 1) and nothing is written.
- If the schema cannot be read, `--default-schema` (default `analytics`) is used.
- `folder` is `<schema>/<curated_folder>` (default folder `curated`), using the resolved schema.

---

## Dimensions

- `type:` is dropped; Omni infers the type.
- `primary_key: yes` → `primary_key: true`; `hidden: yes` → `hidden: true`.
- `type: yesno` keeps its SQL, which already returns true/false.
- `html`, `link`, `drill_fields` and `map_layer_name` are dropped and noted under Changes Made.
- `case`, `tiers`, and `type: tier`, `location` or `distance` are not converted (MANUAL).

### SQL rewriting

| Looker SQL | Omni SQL |
|------------|----------|
| `${TABLE}.col`, where `col` is the field's own name | `sql:` omitted |
| `${TABLE}.col`, where another dimension's SQL is exactly `${TABLE}.col` | `${that_dimension}` |
| `${TABLE}.col`, otherwise | `col` (raw warehouse column) |
| `this_view.col` | treated like `${TABLE}.col` |
| `${field}` (same view) | unchanged; the field is a dependency |
| `${other_view.field}` | unchanged; the view must be joined in the topic |
| `${TABLE}."col"` (quoted), bare `${TABLE}`, `${...}` inside a string literal or comment | field not converted (MANUAL) |
| Liquid | field not converted (MANUAL) |

String literals, quoted identifiers and comments are never rewritten.

```lkml
dimension: sale_price {
  type: number
  sql: COALESCE(${TABLE}.sale_price, 0) ;;
  value_format_name: usd
}
```

```yaml
dimensions:
  sale_price:
    label: Sale Price
    tags: [mig_phase_0, verified, auto_dependency]
    sql: COALESCE(sale_price, 0)
    format: usdcurrency_2
```

## Dates (`dimension_group`)

Omni has no `dimension_groups:` section. A `type: time` dimension_group becomes **one dimension named after the group**:

- `sql:` is the underlying column.
- No `timeframes:` and no `datatype:` are written; Omni generates timeframes for date and timestamp columns. The Looker timeframes are noted in the report.
- Not converted (MANUAL): `type: duration`, `datatype` other than `timestamp`/`datetime`/`date` (for example `epoch`, `yyyymmdd`), `convert_tz: no`, `sql_date`, `sql_start`/`sql_end`.
- References to Looker timeframe fields, such as `${created_date}`, point at a field that does not exist in Omni. Fields using them are not written and are reported.

```lkml
dimension_group: created {
  type: time
  timeframes: [raw, date, month, year]
  sql: ${TABLE}.created_at ;;
}
```

```yaml
dimensions:
  created:
    label: Created
    tags: [date, temporal, mig_phase_0, verified]
    sql: created_at
```

## Measures

| Looker `type` | Omni |
|---------------|------|
| `count`, `count_distinct`, `sum`, `average`, `min`, `max`, `median`, `list` | `aggregate_type` of the same name |
| `percentile` | `aggregate_type: percentile` plus `percentile: N` (MANUAL if N is missing) |
| `number`, `string`, `yesno`, `date` | Calculated measure: SQL kept, no `aggregate_type` |
| anything else (`running_total`, `percent_of_total`, `sum_distinct`, `period_over_period`, …) | Not written; HIGH risk, MANUAL |

```lkml
measure: gross_margin_per_order {
  type: number
  sql: ${total_gross_margin} / NULLIF(${order_count}, 0) ;;
}
```

```yaml
measures:
  gross_margin_per_order:
    sql: ${total_gross_margin} / NULLIF(${order_count}, 0)
    tags: [metric, mig_phase_0, verified]
```

### Measure filters

Each filter is translated using the Looker type of the filtered field. If any filter on a measure cannot be translated, the **measure is not written** and is reported.

| Field type | Accepted | Omni |
|------------|----------|------|
| number | `5`, `1.5`, `1, 3, 5` | `is: 5` / `is: [1, 3, 5]` |
| number | `>=100`, `>100`, `<=100`, `<100` (signed numbers allowed) | `greater_than_or_equal_to: 100` and so on |
| number | `<>5`, `!=5` | `not: 5` |
| number | `NULL` / `NOT NULL` | `is: null` / `not: null` |
| string | `Complete`, `Complete, Shipped` | `is: Complete` / `is: [Complete, Shipped]` (values stay strings) |
| string | `-Cancelled`, `-Cancelled, -Returned` (all negated) | `not: ...` |
| string | `NULL` | `is: null` |
| yesno | `yes`, `no` | `is: true` / `is: false` |

Rejected: a leading `-` on numbers (Looker reads it as "not"); string values containing `%`, `_`, `^`, quotes or brackets; the words `NOT`, `NULL`, `EMPTY` inside values; relative dates such as `90 days`; mixed negated and plain lists; filters on dimension groups, on measures, or on fields whose type cannot be resolved.

```lkml
measure: large_order_count {
  type: count_distinct
  sql: ${order_id} ;;
  filters: [sale_price: ">=100"]
}
```

```yaml
measures:
  large_order_count:
    aggregate_type: count_distinct
    sql: ${order_id}
    filters:
      sale_price:
        greater_than_or_equal_to: 100
```

## Formats

`value_format_name` on dimensions and measures becomes `format:`:

| Looker | Omni |
|--------|------|
| `decimal_0` … `decimal_4` | `number_0` … `number_4` |
| `percent_0` … `percent_4` | `percent_0` … `percent_4` |
| `usd`, `usd_0` | `usdcurrency_2`, `usdcurrency_0` |
| `eur`, `eur_0` | `eurcurrency_2`, `eurcurrency_0` |
| `gbp`, `gbp_0` | `gbpcurrency_2`, `gbpcurrency_0` |
| `id` | `id` |

Any other name, and any custom `value_format` string, is not written. The field gets a MEDIUM risk asking you to set a format by hand.

## Not converted

| Looker | Handling |
|--------|----------|
| `parameter`, Liquid, fields using them | Not written; reported |
| `drill_fields`, `link`, `html`, `map_layer_name` | Dropped, field kept; noted under Changes Made |
| `case`, `tiers`, `tier`/`location`/`distance` types | Field not written; reported |
| Unsupported measure types, untranslatable filters | Measure not written; reported |
| Unmapped or custom formats | Format not written; MEDIUM risk |
| View-level `required_access_grants`, `access_filter` | Reported (not carried) |
| Field `required_access_grants` | Copied onto the field; define the grant in Omni (MANUAL) |
| Persistence (`datagroup_trigger` and so on) | Reported |

---

## Merging

The migrator merges into an existing Omni view when it finds one:

1. With `--omni-source-repo`: a recursive search for `{view}.view.yaml`, `{view}.view` (and `{view}.query.view` for derived tables). More than one match stops the run.
2. Otherwise: `{view}.view.yaml` / `{view}.query.view` already in `--omni-repo` from an earlier run.

When merging:

- Conversion-owned keys are replaced from Looker: `sql`, `filters`, `aggregate_type`, `primary_key`, `format`, `hidden`, `percentile`, `custom_primary_key_sql`, `timeframes`, `datatype`.
- Your own tags are kept; governance tags are replaced (`verified` and `unverified` are exclusive). Access grants are unioned.
- Every LookML field grant is applied to the final field for that Looker field (same name, or the existing field it was merged into), whether or not its SQL was converted. An existing field kept while its Looker SQL could not be converted is flagged MANUAL ACTION REQUIRED. The report lists the grants present in the final output.
- A Looker field is never merged into an existing field whose name belongs to another Looker field, and only one Looker field can be merged into a given column; the others keep their Looker names (reported).
- A Looker field that reads a differently named column (for example `dimension: customer_id { sql: ${TABLE}.user_id ;; }`) is merged under the existing `user_id` field. The rename is recorded, references and filter keys are rewritten, and `user_id` gets the tag `from_looker__customer_id`. Later runs use that tag to find the Looker field, whatever its scope or conversion result: it is merged into `user_id` again when in scope, never re-added as a separate copy, and its grants are applied to `user_id`.
- Rename conflicts stop the run (exit 1, nothing written) instead of picking one definition: an existing field tagged `from_looker__<field>` whose name is now also a field in LookML, or the same tag on two fields. Rename a field in LookML or remove the tag in the Omni view. A tag for a Looker field that no longer exists is ignored. A name used by both a dimension and a measure after merging is also an error.
- A Looker field with `required_access_grants` that reads a column (`${TABLE}.email`) also protects an existing field named after that column (`email`), unless that name is itself a Looker field: both expose the same data. Reported.
- Fields migrated by an earlier run stay visible. Existing fields that are not in scope are hidden.
- Looker fields the existing view does not have are added: in-scope ones visible, the others hidden and tagged `in_omni_in_looker_not_csv` (as in a fresh run).
- An existing `catalog` that the LookML source does not name stops the run (it could otherwise query a different database).
- An existing `sql`, `query`, `table_name`, `schema` or `catalog` that differs from Looker is replaced and the conflict is reported.
- `${TABLE}.col` becomes `${field}` only through a field whose final definition is exactly that column: never through an existing Omni field that is also a Looker field, or whose existing `sql` is an expression.
- An existing file that cannot be parsed stops the run; it is never overwritten.
- The merged view is validated (self-references, missing references, more than one primary key). If it fails, nothing is written.

The report's **Merge/Enrich Summary** lists counts for: Updated (in scope), Merged under a warehouse column name, Added from Looker, Hidden, and Kept visible (earlier run).

---

## Explores

- Each explore becomes `{explore}.topic.yaml` with `fields: [all_views.*, "-tag:table_key"]`, `base_view` (from `from:`/`view_name:` if set), and `joins` nested by which view each join's `sql_on` references.
- `{explore}.relationships` is a plain YAML list, written only when at least one join converts.
- Join types: `left_outer` (or none) → `always_left`; `inner` → `inner`; `full_outer` → `full_outer`; `cross` → `cross`. Missing `relationship` defaults to `many_to_one`. Other join types or relationships: join not migrated.
- Aliased joins (`from:`) use `join_to_view` plus `join_to_view_as`; joins from an aliased view use `join_from_view_as`.
- Not migrated, reported with the original SQL: joins with Liquid, `foreign_key`, raw `sql`, references outside the explore, more than one parent, cycles, or that depend on a skipped join.
- Carried into the topic: `access_filter` → `access_filters`, `sql_always_where` → `always_where_sql`, `sql_always_having` → `always_having_sql`, `required_access_grants`, `hidden`, `group_label`.
- Reported only: `always_filter`, `conditionally_filter`, `fields:` restrictions, `extends`, `always_join`, `required_joins`, join-level `required_access_grants`.
- Splitting an explore into several topics is manual; `--explore-analysis` suggests splits.

---

## Metadata and tags

Generated metadata per field: `label` (the Looker label, or the field name in title case), `description` (the Looker description, or "<Label> dimension/measure"), `ai_context` (from the description and field type) and, for some fields, `synonyms`. Review and improve these by hand; they are a starting point.

| Tag | Applied to |
|-----|-----------|
| `mig_phase_N` | In-scope and auto-included fields, and the view |
| `verified` or `unverified` | Same |
| `auto_dependency` | Fields included because an in-scope field references them |
| `in_omni_in_looker_not_csv` | Hidden out-of-scope fields (their only tag) |
| `only_omni` | Existing Omni fields with no Looker counterpart, when merging |
| `from_looker__<field>` | An existing Omni field that a Looker field was merged into under another name (e.g. `from_looker__customer_email` on `email`). Kept on later runs, so the Looker field is found there whatever its scope; do not remove it |
| `id`, `date`, `temporal`, `metric`, … | Inferred from the field |

Topics are not tagged.

### Canonical mappings (optional)

If your Omni vocabulary differs from Looker's, set `canonical_mappings` in a config file passed with `--config`. They are **empty by default**. Matching is case-insensitive on whole words (whole `_`-separated tokens in field names). They change **labels** of dimensions, dimension groups and measures, and add the old-term spelling as a synonym when a field name contains the new term. Descriptions, SQL and field names are not changed. Example:

```yaml
canonical_mappings:
  sku: product
```

---

## Naming guidance for manual review

The migrator keeps Looker names (except when a merge folds a field into an existing column name). When you rename by hand, follow these conventions and record the rename in the CSV's `omni_field` column:

1. `snake_case`, lowercase, starting with a letter.
2. Keys end with `_id` (`user_id`, `order_id`).
3. Timestamps end with `_at` (`shipped_at`, `delivered_at`).
4. Booleans start with `is_`, `has_` or `can_` (`is_returned`).
5. Include units where ambiguous: `_count`, `_amount`, `_pct`, `_days`, currency codes such as `_usd`.

---

## Report contents

`{view}.migration.md` has these sections:

1. **PR Summary**: one or two sentences for a pull request
2. **Merge/Enrich Summary**: only when merging (see above)
3. **View File Decision**: file type, location, rationale
4. **Metadata for Standardization**: generated `ai_context` and synonyms
5. **Changes Made**: grouped as Canonical Mappings, SQL Transformations, Metadata Additions, Format Mappings, Other (dropped `html`/`link`/`drill_fields`/`map_layer_name` are under Other)
6. **Redundancy Candidates**: hidden dimensions, listed as possible legacy fields
7. **Not Converted**: hidden out-of-scope fields and fields that could not be written
8. **Notes / Risks**: HIGH, MEDIUM and INFO entries
9. **Advanced Features Detected**: access grants, access filters, refinements, `extends`, parameters, derived tables, persistence, each with a status such as `CARRIED - MANUAL ACTION REQUIRED` or `NOT MIGRATED`

## Never

- Edit the CSV.
- Remove existing Omni fields.
- Simplify business logic without a `# OPINION:` note.
- Drop an access restriction without reporting it.
