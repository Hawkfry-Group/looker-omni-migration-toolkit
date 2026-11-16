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
8. **Liquid converts only when it is exact.** Parameters and filter-only fields become Omni filter-only fields. Liquid in SQL becomes Omni Mustache when every construct of the SQL block has an exact equivalent; otherwise the whole block is left out and the report names the construct that blocked it.
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

| CSV row with `type: filter` naming a `parameter:` or `filter:` | Written visible under the view's `filters:` key |
| A `parameter:` or `filter:` not in the CSV | Written; visible when a Liquid tag in the repository's SQL names it, `hidden: true` otherwise |

A Looker `hidden: yes` is kept, even on in-scope and auto-included fields.

### CSV columns

| Column | Required | Meaning |
|--------|----------|---------|
| `phase` | yes | `P0` or `0`; both match `--phase 0` |
| `view` | yes | Looker view name |
| `field` | yes | Looker field name |
| `type` (or `field_type`) | yes | `dimension`, `dimension_group`, `measure`, `filter` (a Looker `filter:` or `parameter:`; `parameter` is accepted too) |
| `owner` | yes | Exact, case-sensitive match with `--owner` |
| `omni_view`, `omni_field` | no | Tracking only. Not used for naming. |
| `status`, `notes` | no | For your team |

---

## Finding LookML

- Views, explores and access grants are found in **any** `*.lkml` file under `--looker-repo`, at any depth, including model files.
- A view, explore or grant name defined more than once stops the run (exit 1) with the file locations.
- Refinements (`view: +name`, `explore: +name`) are detected and reported, not applied.
- View `extends:` is resolved across the whole repo and written to the Omni view; see "View inheritance". If a parent cannot be found it is reported and inherited fields are not included.
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
- In derived table SQL, `${view.SQL_TABLE_NAME}` is replaced by the referenced view's table name, verbatim, when that view is found under `--looker-repo` and has a literal `sql_table_name` (its own, or inherited through `extends:`). The report lists each substitution under "Table References Substituted". The reference stays blocked (the view fails, MANUAL ACTION REQUIRED, with the case named) when the referenced view is itself a derived table, is not found or is defined more than once, has a templated table name (Liquid, `${...}` or a `@{constant}`), has no `sql_table_name`, or has refinements. Any other `${...}` reference in derived table SQL still stops the view.
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
| Liquid | translated to Omni Mustache when every construct has an exact equivalent, else the field is not converted (MANUAL). See [Liquid](#liquid-parameters-and-filter-only-fields) |

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

## View inheritance (`extends`) and template views

### Template views

- A view with `extension: required` becomes an Omni view with `template: true`, with no `schema` and no `table_name`. A template view cannot be queried on its own; other views extend it and supply the table.
- A view is also treated as a template when it has no `sql_table_name` and no `derived_table`, at least one other view under `--looker-repo` extends it, and no explore under `--looker-repo` uses it as its base or as a join target (explore `extends` chains are resolved for this check). The report states the reason ("Template View" under Advanced Features, and the View File Decision).
- A template's fields are converted as usual. It has no table, so `${TABLE}.col` becomes the raw column `col` and the extending view's table supplies it. The "no sql_table_name, using the view name as table" item is not reported for a template.
- A template that declares a `sql_table_name` or `derived_table` of its own does not keep it; each extending view that declares no source gets the inherited one written explicitly (reported as "Inherited View Source").

### `extends` on views

- The parent chain is resolved across every `.lkml` file under `--looker-repo`: parents can extend other views, and a view several branches share is resolved once. The Omni view gets `extends: [a, b, c]` in the Looker order.
- Inherited fields stay in the parent views. They are not copied into the child, so migrate every view of the chain (the report lists them).
- **Conflicts.** Looker gives priority to the LAST view in `extends:`; Omni gives it to the FIRST. When two parents supply different definitions of the same field, the definition Looker uses (the parents merged parameter by parameter, the later one winning) is converted and written into the child, tagged `resolved_from_extends`, and listed in the report under "Extends Conflict". Like any other field it is visible when it is in the CSV scope or an in-scope field depends on it, and hidden otherwise. The same definition arriving twice through a shared grandparent is not a conflict, and nothing is written for a field only one parent defines.
- A field the child declares itself overrides all parents. When it sets only some parameters (for example `dimension: id { hidden: yes }`), the parameters it leaves out come from the inherited definition, as in Looker, and the complete field is written in the child.
- If a parent is missing, defined in more than one file, the chain is circular, or a name is a dimension on one side and a measure on the other, nothing is resolved: `extends:` is not written and the view is reported as before ("inherited fields are not included", with the reason).
- Also reported as MANUAL ACTION REQUIRED: view-level `required_access_grants` and `access_filter` of a parent (not carried), refinements of a parent (not applied), and a field of the child that cannot be converted while a parent defines the same name (Omni then shows the parent's definition).

### References to inherited fields

- A `${field}` reference, or a measure filter, that names an inherited field follows the same rules as a reference to a field of the view itself. The inherited field is not copied into the child and is not an `auto_dependency`.
- The reference is only valid when the parent view can migrate that field. Each inherited field is judged in the view that defines it; if it is not convertible there, the dependent field is not written and is reported with the parent's reason.
- The rule for Looker timeframe fields is the same for an inherited dimension group: `${created_date}` becomes `${created[date]}` (see Dates).
- In explore mode, an `always_filter` on an inherited field (or on a timeframe of an inherited dimension group) translates like one on a field of the view itself.
- The "No primary key dimension in the final view" warning is not raised when a view of the chain defines a `primary_key: yes` dimension that converts.

### CSV scope

- A CSV row that names an inherited field is not an error and writes nothing in the child. It is reported ("CSV Fields Inherited Through extends"): whether the field is visible follows the scope of the parent view that defines it, so list it in the CSV under that parent.
- A view with no fields of its own (it only extends other views and sets a table) converts without any CSV row: the file holds the view header and `extends:`. A template view also converts without CSV rows; its fields are then written hidden, which the extending views inherit, and this is reported.

---

## Liquid, parameters and filter-only fields

### Filter-only fields

A Looker `filter:` and a Looker `parameter:` both become an entry under the Omni view's top-level `filters:` key.

| Looker | Omni |
|--------|------|
| `type: string` (or no type) | `type: string` |
| `type: number` | `type: number` |
| `type: date`, `type: date_time` | `type: timestamp` |
| `type: yesno` | `type: boolean` |
| `type: unquoted` (parameter) | `type: column` when the SQL uses it as a whole identifier, `type: string` when it is used wrapped in single quotes or not used in SQL |
| `label`, `description`, `group_label`, `hidden: yes` | same keys |
| `allowed_value { label value }`, `suggestions: [...]` | `suggestion_list` items (`value`, optional `label`) |
| `suggest_dimension: view.dim` | `suggest_from_field: view.dim` (an unqualified name gets the view) |
| `default_value` | `default_filter: { is: value }`. A filter's default is a Looker filter expression: it is written only when it translates. A date default is reported, not written |
| any `parameter:` | `filter_single_select_only: true` |
| `filter:` with `sql:`, other types | not written; reported. SQL that names it is not converted either |

- Every parameter and filter-only field of the view is written. It is visible when the CSV lists it (type `filter`) or a Liquid tag in the SQL of any view names it, and `hidden: true` otherwise.
- Parameters and filter-only fields are inherited through `extends:` like any other field. One defined in a parent (a template included) is written on the parent and not copied into the child. Two parents that define it differently are resolved in the child, as for fields.
- A template view cannot supply suggestions: a `suggest_dimension` that names the template itself is not carried and is reported.

### Liquid in SQL

Scope: `derived_table: { sql: ... }` and the `sql:` of dimensions, dimension groups and measures. The SQL is tokenized (Liquid tags, SQL strings, quoted identifiers, comments); nothing is replaced by pattern.

| Looker | Omni |
|--------|------|
| `{% condition view.f %} expr {% endcondition %}` | `{{# view.f.filter }} expr {{/ view.f.filter }}` |
| `{% parameter view.p %}`, `{{ view.p._parameter_value }}` | `{{ filters.view.p.value }}` |
| `{% date_start view.f %}`, `{% date_end view.f %}` | `{{ filters.view.f.range_start }}`, `{{ filters.view.f.range_end }}` |
| `{{ view.f._in_query }}`, `._is_selected`, `._is_filtered` (used as a value) | `{{ view.f.in_query }}`, `.is_selected`, `.is_filtered` |
| `{{ view._in_query }}` | `{{ view.in_query }}` |
| `{{ _user_attributes['x'] }}` | `{{ omni_attributes.x }}` |
| `{% if %}`, `{% elsif %}`, `{% else %}`, `{% assign %}`, loops, Liquid filters (`\| upcase`), any other `{{ ... }}` | not converted (MANUAL); the report names the construct |

- **All or nothing per SQL block.** If any construct is outside the table, malformed, nested (Liquid inside a `{% condition %}`), inside a SQL string or quoted identifier where it cannot be translated, or names a field that cannot be resolved, the block is not converted: the field is not written, or the derived view stops (exit 1, nothing written). The reason names every construct that blocked it.
- **View name in the token.** An unqualified reference (`{% parameter p %}`) names the view being converted, also when the field is inherited. `view.field` is used when `view` is a view under `--looker-repo`. A join alias or explore alias is replaced by the view it stands for when it stands for exactly one view across all explores; otherwise the block is not converted.
- **Targets.** `{% condition %}` and date bounds accept a filter-only field, a dimension, or the `date`, `time` or `raw` timeframe of a dimension group (named by the group in Omni). `{% parameter %}` and `_parameter_value` need a parameter. A dimension or measure named in a token is a dependency like `${field}`: if it is not migrated, the field that names it is not written (a derived view stops).
- **Quotes.** Omni renders a `string` value as a quoted literal, a `number` bare and a `column` as a quoted identifier. Quotes that directly wrap a parameter tag (`'{% parameter p %}'`) are dropped. A tag embedded in a longer quoted string or identifier (`'%{% parameter p %}%'`, `"prefix_{% parameter p %}"`) is never converted. A quoted `number` or `yesno` parameter is not converted. User attributes are interpolated raw by both tools: quotes around them are kept.
- **SQL comments.** A SQL comment (`-- ...` or `/* ... */`) that holds Liquid is removed from the output SQL, because a comment has no effect on the query. The report lists it under "Liquid translated" as "SQL comment containing Liquid removed". Looker renders Liquid before the SQL is read, so a comment is removed only when every Liquid block opened in it (`{% if %}`, `{% for %}`, `{% condition %}` and so on) is closed in it, or in the comments that directly follow it with only whitespace between (a commented-out block of several lines). A block tag that reaches code outside the comment blocks the SQL block, and so does an optimizer hint (`/*+ ... */`) that holds Liquid. Comments without Liquid are kept. Liquid inside a quoted string or quoted identifier still blocks.
- **`type: unquoted`.** A use is an identifier when the tag is wrapped in double quotes or backticks, qualified with a dot (`${TABLE}.{% parameter p %}`, `{% parameter p %}.events`) or the table after `FROM` or `JOIN`. A use wrapped in single quotes is a string. A bare use anywhere else (for example `DATE_TRUNC({% parameter p %}, x)`) could be a keyword or a number and is not converted. A parameter used both as an identifier and as a string blocks every SQL block that reads it. With `type: column`, each allowed value must hold a full column or `schema.table` name.
- **Inherited templated fields.** A Mustache token names one view. An inherited field whose Liquid names "this view" is therefore written into the extending view too, converted there, tagged `resolved_from_extends`. It is visible when the CSV lists it under the view or under the parent it comes from.
- **Output.** SQL that holds Mustache is written as a block scalar and is not reformatted. Before a view is written, every SQL block converted in the run is checked again: a leftover `{%` or Looker-style `{{ ... }}` stops the write.
- **Report.** Each construct translated is listed under "Liquid translated", per SQL block. User attributes read by translated SQL are listed as MANUAL ACTION REQUIRED (create them in Omni).
- Liquid in `html`, `link`, `sql_table_name`, join `sql_on` and explore `sql_always_where` is not converted.

---

## Dates (`dimension_group`)

Omni has no `dimension_groups:` section. A `type: time` dimension_group becomes **one dimension named after the group**:

- `sql:` is the underlying column.
- No `timeframes:` and no `datatype:` are written; Omni generates timeframes for date and timestamp columns. The Looker timeframes are noted in the report.
- `convert_tz: no` becomes `convert_tz: false` on the Omni dimension: the field keeps the database timezone with no conversion. `convert_tz: yes`, or none, writes nothing (Omni's default is `true`). The same applies to a dimension of type `date`, `date_time` or `time`; on any other dimension type the parameter is ignored. It is listed in the report with the field's other changes, and is not a manual item. `datatype: date` needs no Omni key.
- Not converted (MANUAL): `type: duration`, `datatype` other than `timestamp`/`datetime`/`date` (for example `epoch`, `yyyymmdd`), `sql_date`, `sql_start`/`sql_end`.
- References to timeframe fields. A reference to the `date` or `raw` timeframe of a dimension group is rewritten to Omni's timeframe form: `${created_date}` becomes `${created[date]}` and `${view.created_raw}` becomes `${view.created[raw]}`, for the view's own and inherited groups, in field `sql:` and in join `sql_on`. These two timeframes have the same SQL type in both tools. Every rewrite is listed in the report under "Timeframe references rewritten".
- A reference to any other timeframe (`week`, `month`, `quarter`, `year`, `time`, `day_of_week`, `week_of_year` and so on) is not rewritten: Looker renders these as formatted strings or integers and Omni renders dates, so a comparison in the surrounding SQL could silently change meaning. The field (or join) is not written and the report names the timeframe.
- A name counts as a timeframe reference only when a `type: time` dimension group of that name exists in the view's chain and offers that timeframe (a group with no `timeframes:` list has Looker's defaults). A real dimension called `signup_date` is left alone.

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
| Liquid with no exact Omni equivalent (`{% if %}`, `{% assign %}`, loops, Liquid filters), and the SQL block that holds it | Not written; reported with the construct named |
| `filter:` with `sql:`, parameter or filter types with no Omni type | Not written; reported |
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

- Each explore becomes `{explore}.topic.yaml` with `fields: [all_views.*, "-tag:table_key"]` (plus the explore's field restrictions, see below), `base_view` (the name the explore's own fields go by, see Aliases below), and `joins` nested by which view each join's `sql_on` references.
- The join definitions are written inside the topic file under a top-level `relationships:` key (topic-scoped relationships), so they apply to that topic only. `joins:` is still written and keeps its nesting. With `--relationships-file` they go to `{explore}.relationships` instead (a plain YAML list, written only when at least one join converts), and a join between the same two views that differs from another `.relationships` file in the output directory is reported as a conflict. Both forms write the same keys.
- Join types: `left_outer` (or none) → `always_left`; `inner` → `inner`; `full_outer` → `full_outer`; `cross` → `cross`. Missing `relationship` defaults to `many_to_one`. Other join types or relationships: join not migrated.
- Aliased joins (`from:`) use `join_to_view` plus `join_to_view_as`; joins from an aliased view use `join_from_view_as`. `on_sql` keeps the Looker aliases.
- Not migrated, reported with the original SQL: joins with Liquid, `foreign_key`, raw `sql`, references outside the explore, more than one parent, cycles, or that depend on a skipped join.
- Carried into the topic: `access_filter` → `access_filters`, `sql_always_where` → `always_where_sql`, `sql_always_having` → `always_having_sql`, `required_access_grants`, `hidden`, `group_label`, and explore and join `fields:` restrictions (see Field restrictions).
- Reported only: `conditionally_filter` (Omni has no equivalent), `always_join`, `required_joins`, join-level `required_access_grants`.

### Field restrictions (`fields:`)

Looker restricts fields on the explore and on each join. Both are written as selectors in the topic's one `fields:` list. Omni evaluates selectors by type, not list order: `all_views.*`, then `view.*`, then tags, then `view.field`; a leading `-` excludes.

| Looker | Omni topic `fields:` |
|---|---|
| explore `fields: [ALL_FIELDS*, -view.a, -view.b]` | `all_views.*, -view.a, -view.b` |
| explore `fields: [view.a, other.b]` (no `ALL_FIELDS*`) | `-all_views.*, view.a, other.b` |
| join `fields: [view.x, view.y]` | `-view.*, view.x, view.y` |
| join `fields: []` | `-view.*` |
| join `fields: [-view.x]` (exclusions only) | `-view.x` |

- The migrator's default exclusion `-tag:table_key` is always added at the end. A field listed by name stays in the topic even when it carries that tag, because `view.field` is evaluated after tags.
- Names are the names the views have in the topic (the alias for an aliased join or base view).
- A set (`view.set_name*`) is expanded to its member fields from the view's `set:` definitions, nested sets and sets inherited through `extends:` included.
- A dimension group timeframe (`view.created_date`) maps to the Omni dimension (`view.created`), which offers every timeframe. An exclusion is carried only when it excludes every timeframe in the group's `timeframes:` list.
- The explore list and the join lists are combined: a field the explore excludes is removed from a join's list, and with an explore list of fields only, a field must also pass its join's list.
- Dropped from the list and noted in the report under "Field restrictions": a field the topic will not contain (not converted), a name the view does not define, and an entry for a join that was not migrated. A filter-only field or parameter is left out and listed for review.
- A list is carried whole or not at all. Not carried, reported as MANUAL ACTION REQUIRED ("Explore field restriction (NOT carried)", "Join field restriction on X (NOT carried)") with the reason: a set that is not defined, that holds `ALL_FIELDS*`, an exclusion or a field of another view; an exclusion of only some timeframes of a dimension group; an explore list with exclusions and no `ALL_FIELDS*`; an entry that is not view-qualified on the explore, or that names another view on a join; a view whose fields are not known (not a single view under `--looker-repo`, or its `extends:` chain does not resolve).
- With explore `extends`, the child's explore-level `fields:` replaces the parent's, and a redeclared join's `fields:` (including `fields: []`) replaces the inherited join's.
- The difference to Looker: Looker hides the restricted fields, Omni rejects queries that use them. Carried lists are also listed as "Field restrictions (carried)" to be verified.

### `always_filter`

A Looker `always_filter` is a filter users can change but not remove. It becomes a required default filter on the topic: `required: true` sits beside the operator, and a required entry must have a value.

```yaml
default_filters:
  order_items.created:
    time_for_duration: [7 complete days ago, 7 days]
    required: true
  order_items.status:
    is: [Complete, Shipped]
    required: true
```

Only these expressions are translated:

| Looker expression | Omni |
|---|---|
| `N days ago for N days` (also weeks, months; same N and unit on both sides, N of 1 or more) on a date field | `time_for_duration: [N complete days ago, N days]` |
| a named period on a date field: `last month`, `this month`, `last week`, `this week`, `last year`, `this year`, `last quarter`, `this quarter`, `today`, `yesterday` | `is: last month` (the period, lower case) |
| a plain value on a string field, e.g. `global` | `is: global` |
| a comma list of values on a string field, `a,b` | `is: [a, b]` |
| one number on a number field | `is: 5` |

- `complete` matters: Looker's "7 days ago for 7 days" is the last 7 complete days, without today.
- The filter key is the Omni field. A timeframe field of a dimension group (`created_date`, `created_time`, `created_raw`) maps to the group's Omni name (`created`); a dimension of type `date`, `date_time` or `time` keeps its name. A field on an aliased join keeps the alias (`buyer.country`), and so does a field on an aliased base view (`sales.sold`).
- Named periods: `is: last month` was tested on a live Omni instance and gives the previous calendar month. The other named periods use the same Omni mechanism and were not tested one by one, so each is written and listed under MANUAL ACTION REQUIRED as "Named date periods (verify)".
- Not written, reported as MANUAL ACTION REQUIRED with the Looker expression and the reason: an empty value, `N days` (it includes today so far), any other period expression (`next month`, `last 2 months`, `tomorrow`), negations, wildcards, comparisons, `NULL`, yesno fields, measures, other timeframes (`created_week`), a field that is not view-qualified or not found in the view, and a field on a view that is not in the topic.
- Translated filters are also listed under MANUAL ACTION REQUIRED as "carried", to be verified in Omni.

### Aliases (kept, scoped to the topic)

Looker projects reuse short names (`orders`, `session`) for different views in different explores. The migrator keeps every alias and declares it inside the topic, so it never becomes a model-wide name: two topics can use the same alias for different views.

- **Join alias.** `join: shop { from: stores }` becomes `join_to_view: stores` with `join_to_view_as: shop` in the topic's `relationships:`.
- **Base alias.** The explore's own fields go by `view_name:` when set, else by the explore name. The real view is `from:` when set, else `view_name:`, else the explore name. When the two differ (`explore: sales { from: sales_fact }`, or `from:` together with `view_name:`), the topic gets `base_view: <alias>` and declares the alias in its `views:` block:

```yaml
base_view: sales
views:
  sales:
    extends: [sales_fact]
relationships:
- join_from_view: sales
  join_to_view: stores
  join_to_view_as: shop
  on_sql: ${sales.store_key} = ${shop.store_key}
joins:
  shop: {}
```

- **Filters and restrictions keep the alias.** `always_filter`, `access_filter`, `sql_always_where` and `sql_always_having` name fields through the alias (`sales.sold_date`), and the field type is read from the real view. The `default_filters` key keeps the alias (`sales.sold`).
- **Verification replaces the blanket warning.** Every `${alias.field}` in the join conditions, `access_filters`, `always_where_sql` and `always_having_sql` is checked against the view the alias stands for (own fields, inherited fields, dimension group timeframes). Only the references that do not resolve are reported ("Unresolved field references"). An alias whose view is not a single view under `--looker-repo` is reported as not verified.
- **View labels.** A Looker `view_label` on the explore or a join becomes `label:` for that name in the topic's `views:` block when it differs from the view's own label.
- **Alias map.** The explore report has an "Alias map" table: alias, real view, where it is declared.
- **One alias map.** The same map (every base alias and join alias of every explore under `--looker-repo`) resolves aliases in Liquid, in filters and in cross-view fields. Outside the explore that declares it, a name that stands for more than one view is ambiguous and stays a manual item.
- Stops the run: a base alias that is also the name of another view under `--looker-repo` (declaring it would change that view inside the topic). Reported: a join alias with the name of another view; a base alias used with `--relationships-file` (the alias exists in the topic only).
- A join whose `sql_on` names the base by anything other than the base alias is not migrated ("not part of the explore").

### Cross-view fields (topic-scoped fields)

A field whose SQL or filter names another view through a join alias (a name that is not a view under `--looker-repo`) fails Omni model validation in a view file ("No such view"), and validates when it is defined in the topic's `views:` block. So:

- **View mode** does not write such a field in the view file. It is listed in the view report under "Topic-scoped fields" with the aliases it needs. It is not a manual item. A field built on a topic-scoped field (a measure on a cross-view dimension) is topic-scoped too, including through `extends:`.
- **Explore mode** converts these fields with the same rules as view mode (types, formats, filters, timeframes, Liquid), for the base view and every joined view, and writes each one under the view's name in the topic:

```yaml
views:
  sales:
    extends: [sales_fact]
    dimensions:
      store_label:
        sql: ${shop.store_name}
```

- A field is written only when every view it names is in the topic and has the field it reads. When an alias it needs is not in the topic, or stands for a view without that field there, the field is left out and noted in the report (it does not work in the Looker explore either). A field that cannot be converted, or whose alias stands for a view that cannot be checked, is a MANUAL ACTION REQUIRED item.
- Explore mode has no scope CSV, so these fields are written visible unless Looker hides them. They carry the `verified`/`unverified` tag, and the `mig_phase_N` tag only when `--phase` is passed with `--explore`. Field-level `required_access_grants` are copied and reported.
- A reference to another view by its real name stays in the view file, as before. A name that is neither a view nor an alias under `--looker-repo` is also kept, and flagged in the Cross-View References note.
- A topic-scoped field whose translated Liquid names a view that the topic does not have under that same name is not written (manual).

### Topic file prefix

Topic files are not namespaced by folder in Omni: two topics with the same name overwrite each other. `--topic-prefix VALUE` writes `{VALUE}__{explore}.topic.yaml` (and `{VALUE}__{explore}_explore.md`) and prefixes the topics named in `extends:` the same way. `--topic-prefix model` uses the name of the one `*.model.lkml` file that defines the explore or reaches its file through `include:` (patterns starting with `/` are read from `--looker-repo`, others from the including file's folder; includes of other projects are ignored). If no single model holds the explore, the run stops with a message. The topic `label` stays the Looker explore label. Default: no prefix.

### Explore `extends`

- The parent chain is resolved across every `.lkml` file under `--looker-repo`. The topic gets `extends: [parent]` and also the full merged result: the parents' joins plus its own, the merged relationships, and the merged settings. The child carries the complete join list because a child topic's `joins:` replaces the parent's in Omni, while Looker adds them. `base_view` is always written.
- Merge semantics follow Looker: the child's parameters override the parent's; joins are additive; with several parents a later one wins; chains are followed. A join declared again by the child is merged parameter by parameter (the child wins); when the child leaves parameters out, the merged join is reported for review.
- Inherited when the child does not set them: `label`, `description`, `view_name`/`from`, `access_filter`, `sql_always_where`, `sql_always_having`, `required_access_grants`, `always_filter`, `group_label`, `hidden`, and the reported-only settings. `extension` is not inherited.
- The report has an "Inheritance (extends)" section listing the explores merged, the inherited joins and settings and their origin, and the overrides.
- Reported as MANUAL ACTION REQUIRED: the parent topics must exist in Omni (migrate the parent explores too); several parents in `extends:` (Omni documents one); refinements on a parent (not applied); a child that defines `access_filter` when a parent also does.
- If a parent is missing, declared more than once, or the chain is circular, nothing is merged, `extends:` is not written, and the explore is reported as before.

### Template topics

- An explore with `extension: required` becomes a topic with `template: true`. A template topic cannot be queried on its own; other topics extend it.
- An explore that other explores extend but that has no `extension: required` stays a normal topic, because it is usable on its own in Looker. Pass `--template` to force `template: true` for an explore you know is only a base.
- The report's Summary states the topic type and the reason. An `extension: required` explore that no explore under `--looker-repo` extends is still written as a template and reported.
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
9. **Liquid translated**: only when Liquid was translated; each construct and its Mustache form, per SQL block
10. **Advanced Features Detected**: parameters and filter-only fields (type and visibility of each), user attributes to create, access grants, access filters, refinements, `extends` (the chain, conflicts resolved, inherited CSV fields), template views, derived tables, persistence, each with a status such as `CARRIED - MANUAL ACTION REQUIRED` or `NOT MIGRATED`

## Never

- Edit the CSV.
- Remove existing Omni fields.
- Simplify business logic without a `# OPINION:` note.
- Drop an access restriction without reporting it.
