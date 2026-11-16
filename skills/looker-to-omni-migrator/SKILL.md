---
name: looker-to-omni-migrator
description: "Migrate Looker views and explores to Omni. Converts LookML views to Omni .view.yaml (or .query.view for derived tables) with a CSV controlling which fields are in scope, converts explores to Omni topics with their relationships, writes a migration report per view and explore, and lists Looker access grants. Use when the user asks to migrate, convert or port a Looker view or explore to Omni."
---

# Looker to Omni Migrator

This skill wraps a Python CLI, `scripts/migrate.py`, that converts LookML into Omni model files. You (Claude) collect the inputs, run the CLI from the user's project, then review the generated files and report with the user.

The CLI has three mutually exclusive modes:

| Mode | Flag | Writes |
|------|------|--------|
| View migration | `--view NAME` | `{view}.view.yaml` (or `{view}.query.view` for a derived table) and `{view}.migration.md` |
| Explore migration | `--explore NAME` | `{explore}.topic.yaml` (joins and relationships) and `{explore}_explore.md`. With `--relationships-file`, the relationships go to `{explore}.relationships` instead. `--template` forces `template: true` |
| Access grant listing | `--parse-access-grants` | `access_grants.md` |

## Instructions for Claude

### 1. Collect inputs

For a **view migration**, ask the user for anything they have not given you:

- **Phase** (required): `--phase N`. The CSV may write phases as `P0` or `0`; both match `--phase 0`.
- **Owner** (required): `--owner "Name"`. Exact, case-sensitive match on the CSV `owner` column.
- **Looker repo** (optional): `--looker-repo PATH`, default `./looker`. Every `*.lkml` file under it is searched.
- **CSV** (optional): `--csv PATH`, default `migration/views.csv`. A sample is in `examples/migration.csv`.
- **Existing Omni views to merge into** (optional): `--omni-source-repo PATH`, for schema-synced Omni views the user wants enriched rather than replaced.

For an **explore migration**, you need the explore name and the Looker repo path. Phase and owner are not used.

### 2. Run the CLI

Run from the root of the user's project (the directory holding the `looker` symlink and `migration/`), calling the script by its path inside this skill's base directory, shown as `<skill-dir>` below. Relative paths such as `./looker`, `migration/views.csv` and `./migration/staging` resolve against the current directory, so never `cd` into the skill directory for a real migration; outputs would land inside the skill. If the Python packages are missing, install them once with `pip install -r <skill-dir>/requirements.txt`.

On a new repo, offer `--dry-run` first: it prints `[dry-run] would write <path> (<n> bytes)` for each file and writes nothing.

```bash
python <skill-dir>/scripts/migrate.py --view users --phase 0 --owner "Team A" \
  --looker-repo ./looker --csv migration/views.csv
```

```bash
python <skill-dir>/scripts/migrate.py --explore order_items --looker-repo ./looker
```

```bash
python <skill-dir>/scripts/migrate.py --parse-access-grants --looker-repo ./looker
```

Other flags:

| Flag | Default | Purpose |
|------|---------|---------|
| `--omni-repo PATH` | `./migration/staging/views` (view mode), `./migration/staging/topics` (explore mode) | Output directory. Reports go to the sibling `reports/` directory. |
| `--omni-source-repo PATH` | none | Searched recursively for `{view}.view.yaml`, `{view}.view` or `{view}.query.view` to merge into. |
| `--config PATH` | none | YAML config (see `config/migration_config.example.yaml`): `default_schema`, `curated_folder`, `canonical_mappings`. |
| `--default-schema NAME` | `analytics` | Schema used when it cannot be read from `sql_table_name`. |
| `--curated-folder NAME` | `curated` | Produces `folder: <schema>/<curated_folder>` in the YAML. |
| `--unverified` | off | Tag output `unverified` instead of `verified`. |
| `--dry-run` | off | Print what would be written or removed; write nothing. |
| `--explore-analysis` | off | With `--view` only: also write `{view}_explore_analysis.md` for explores that use the view. |
| `--relationships-file` | off | With `--explore` only: write the join definitions to `{explore}.relationships` instead of the topic file's `relationships:` key. |
| `--template` | off | With `--explore` only: write `template: true` on the topic. Automatic for an explore with `extension: required`. |

Exit codes:

- `0`: success. There may still be manual items.
- `1`: input or conversion error. It is detected before any file of that view or explore is written, so nothing is written for it. Two exceptions: an I/O error while writing (each file is replaced atomically, but files already written by the run stay), and a failing `--explore-analysis`, which runs after the view's files are written.
- `2`: invalid arguments.

There is no batch flag. To migrate several views, run the command once per view and review each result before moving on.

Merging: if the output directory already holds `{view}.view.yaml` from an earlier run, the new run merges into it, so earlier phases stay visible. With `--omni-source-repo`, a match there is used instead.

### 3. After every run, surface manual work

1. Read the console summary. If it says `COMPLETED WITH N ITEM(S) NEEDING MANUAL ACTION`, list **every** item to the user, verbatim, before anything else. Do not summarise them away.
2. Read the report (`{view}.migration.md` or `{explore}_explore.md`) and tell the user:
   - what was converted, and which fields were auto-included as dependencies (tag `auto_dependency`);
   - everything under **Not Converted**, **Notes / Risks** (HIGH and MEDIUM) and **Advanced Features Detected**;
   - every line marked **MANUAL ACTION REQUIRED** (access grants and user attributes to define in Omni, explore restrictions not carried, skipped joins, unsupported measures, Liquid, refinements).
3. Show the generated YAML for anything the user asks about.
4. Never edit the user's CSV. Suggest CSV changes instead.
5. Remind the user the output is not validated against a live Omni instance: it should be loaded in an Omni branch before promotion.

### 4. Offer the primary key finder when views have no key

`scripts/find_primary_keys.py` is a separate, optional script. It is never part of a conversion run. Offer it when both of these are true:

- the converted views are loaded into an Omni branch and validate there, and
- one or more views have no primary key (the report says `No primary key dimension in the final view`, or Omni warns about a missing primary key).

Before running it, tell the user:

- It runs SQL against their warehouse through the Omni CLI (`omni`), so it needs the CLI configured and an Omni role that is allowed to run manually written SQL. If the role is not allowed, the run stops with exit code 1.
- It tests a sample, not the whole table. A sample can prove a column is not unique; it can only suggest that it is. Say "unique in the sample", and repeat the sample size and strategy from the report when you present a key.
- It is a dry run unless `--apply` is given. Run the dry run first, show the user `primary_keys.md`, and add `--apply` only when they ask for it.

```bash
python <skill-dir>/scripts/find_primary_keys.py --model-id MODEL_ID --branch-id BRANCH_ID \
  --omni-repo ./migration/staging/views
```

After the run, list every view by status. For `multiple_candidates` (two or more columns were unique in the sample), never choose for the user and never edit the view file yourself: show every qualifying column with its evidence and any "likely foreign key" note from the report, ask which column identifies a row, then re-run with `--key VIEW=DIMENSION` (repeatable; it is refused if that column did not qualify, and it only writes together with `--apply`). For `needs_decision` (a large unpartitioned BigQuery table), ask the user to choose between `--date-column` with `--window-days` and `--allow-full-scan`; do not choose a full scan for them. For `pair_suggested`, show the suggested dimension and let the user add it; the script never writes pairs. SQL query views are skipped and need their key set by hand. Details: `docs/PRIMARY_KEYS.md`.

### Prompts users typically give

There are no slash commands for this skill. Users describe what they want, for example:

- "Migrate the `users` view for phase 0, owner Team A."
- "Do a dry run of `order_items` first."
- "Migrate the `order_items` explore to an Omni topic."
- "Which explores use `users`?" (run the view migration with `--explore-analysis`)
- "List the access grants in my Looker repo."

## What the converter does

Full rules: `rules/migration_rules.md`. Omni syntax: `rules/model_details_omni.md`. In short:

- **Scope.** In-scope CSV fields are written visible and tagged `mig_phase_N` plus `verified`/`unverified`. Same-view fields they depend on are auto-included (visible, tagged `auto_dependency`). The view's other convertible fields are written `hidden: true` with only the tag `in_omni_in_looker_not_csv`. Unconvertible fields, and fields that depend on them, are omitted and reported. CSV rows with no matching LookML field are reported. A Looker `hidden: yes` is kept, even on in-scope and auto-included fields. When merging into an existing Omni view, Looker fields it does not have yet are added the same way (out-of-scope ones hidden).
- **SQL.** `${TABLE}.col` becomes `${field}` only when another dimension's SQL is exactly `${TABLE}.col`; otherwise it becomes the raw column `col`. `sql:` is omitted when a dimension reads its own column. Same-view `${field}` and cross-view `${view.field}` stay as written. Quoted identifiers, a bare `${TABLE}`, or references inside literals or comments stop the field (manual).
- **Dates.** A `type: time` `dimension_group` becomes one dimension named after the group. No `timeframes:` or `datatype:` is written; the Looker timeframes are noted in the report. A reference to the `date` or `raw` timeframe (`${created_date}`, `${view.created_raw}`) is rewritten to `${created[date]}`, `${view.created[raw]}`, for own and inherited groups, and listed under "Timeframe references rewritten". A reference to any other timeframe stops the field: Looker renders those as strings or integers and Omni as dates.
- **Dates.** A `type: time` dimension_group becomes one dimension named after the group. `convert_tz: no` becomes `convert_tz: false` (database timezone, no conversion), so the field and everything that depends on it converts.
- **Measures.** `count`, `count_distinct`, `sum`, `average`, `min`, `max`, `median`, `list`, `percentile` become `aggregate_type`. `number`, `string`, `yesno`, `date` become calculated measures. Anything else (for example `running_total`, `percent_of_total`, `sum_distinct`) is not written: HIGH risk, manual. Measure `filters` are translated by field type, and a filter that cannot be translated stops the measure.
- **Formats.** `decimal_N` becomes `number_N`, `percent_N` stays `percent_N`, `usd`/`eur`/`gbp` (and `_0`) become `usdcurrency_2` and so on, and `id` stays `id`. Other names and custom `value_format` strings are not written (MEDIUM risk).
- **Derived tables.** SQL derived tables become `{view}.query.view` with `sql:`. `${view.SQL_TABLE_NAME}` is replaced by the table name when the referenced view has a literal `sql_table_name` (own or inherited), and listed in the report. Any other `${...}` reference (including `SQL_TABLE_NAME` of a derived table, a view that is not found or a templated table name), or Liquid with no exact Omni equivalent, stops the view (exit 1, nothing written). Native derived tables become `{view}.query.view` with `query:` (`fields`, `base_view`, `topic`, `filters`, `sorts`, `limit`).
- **Parameters and filter-only fields.** A Looker `filter:` or `parameter:` becomes an entry under the view's `filters:` key (`string`, `number`, `timestamp`, `boolean`; `allowed_value` becomes `suggestion_list`, `default_value` becomes `default_filter`, a parameter gets `filter_single_select_only: true`). A `type: unquoted` parameter becomes `type: column` when the SQL uses it as a whole identifier and `type: string` when it is used in single quotes. List one in the CSV with type `filter` to show it; one that no SQL names and the CSV does not list is written hidden. They are inherited through `extends:` and written on the view that defines them.
- **Liquid.** In derived table SQL and in dimension and measure `sql:`, `{% condition %}`, `{% parameter %}`, `_parameter_value`, `{% date_start %}`, `{% date_end %}`, `_in_query`, `_is_selected`, `_is_filtered` (as values) and `{{ _user_attributes['x'] }}` become Omni Mustache. The SQL is tokenized, and a block converts only when every construct in it does: one `{% if %}`, `{% assign %}`, loop, Liquid filter, nested or malformed tag, tag embedded in a longer quoted string, or unresolved reference leaves the whole block out, and the report names the construct. A join alias in a reference is replaced by its view when it stands for exactly one view. A SQL comment that holds Liquid is removed from the output (a comment has no effect on the query) when its Liquid blocks are closed inside it; Liquid in a quoted string still blocks. Translated constructs and removed comments are listed under "Liquid translated" in the report.
- **View extends.** The parent chain is resolved across the whole `--looker-repo` (chains and shared grandparents included) and the view gets `extends: [a, b]` in the Looker order. Inherited fields stay in the parent views; references to them (`${field}`, measure filters, `always_filter`) follow the same rules as references to the view's own fields, and an inherited primary key counts. Looker lets the last parent win a conflict and Omni the first, so a field two parents define differently is written into the child with the Looker definition, tagged `resolved_from_extends`. A view with no fields of its own converts without CSV rows. If a parent cannot be found, `extends:` is not written and it is reported.
- **Template views.** A view with `extension: required` becomes a view with `template: true` and no `schema` or `table_name`. So does a view with no table that other views extend and no explore uses directly. The report states the reason.
- **Explores.** One topic per explore, with `fields: [all_views.*, "-tag:table_key"]` and joins nested by dependency. The join definitions are written in the topic file under `relationships:` (topic-scoped); `--relationships-file` writes them to a separate `{explore}.relationships` instead. `access_filter`, `sql_always_where`, `sql_always_having`, `required_access_grants`, `hidden` and `group_label` are carried; `conditionally_filter`, `always_join`, `required_joins` and join-level grants are reported only.
- **Field restrictions.** Explore and join `fields:` lists become topic `fields:` selectors: `[ALL_FIELDS*, -view.a]` becomes `all_views.*, -view.a`; a join's `[view.x, view.y]` becomes `-view.*, view.x, view.y`; `[]` becomes `-view.*`. Names are the names in the topic, sets are expanded (inherited sets included), a timeframe maps to its dimension, and a field the topic will not contain is dropped and noted. A list that cannot be resolved with confidence (unknown set, only some timeframes of a group excluded) is not carried and is reported. Looker hides restricted fields; Omni rejects queries that use them.
- **Aliases.** Looker aliases are kept and scoped to the topic. A join alias uses `join_to_view_as`. A base alias (`from:` and/or `view_name:` make the explore's field name differ from the real view) becomes `base_view: <alias>`, declared in the topic's `views:` block with `extends: [real_view]`. Filters and restrictions keep the alias. Every `${alias.field}` the topic writes is checked against the real view and only unresolved references are reported. A `view_label` becomes a `label` in the `views:` block. The report has an "Alias map" table.
- **Cross-view fields.** A field that names another view through a join alias is not written in the view file (Omni validation rejects it there); the view report lists it under "Topic-scoped fields". Explore mode converts it with the same rules and writes it in the topic's `views:` block when the topic has every alias it needs, visible unless Looker hides it. Fields built on such a field follow it.
- **Topic prefix.** `--topic-prefix VALUE` writes `{VALUE}__{explore}.topic.yaml` and prefixes the topics in `extends:`; `model` uses the Looker model file that holds the explore.
- **always_filter.** Becomes a `default_filters` entry with `required: true` (changeable, not removable) when the expression is `N days ago for N days` (also weeks, months) or a named period (`last month`, `this week`, `today`, `yesterday` and so on, written as `is: last month`) on a date field, a value or comma list on a string field, or one number on a number field. A timeframe field such as `created_date` is keyed by the dimension group's Omni name (`created`). Any other expression, or an empty value, is not written and is reported with the Looker expression.
- **Explore extends.** The parent chain is resolved across the whole `--looker-repo`. The topic gets `extends: [parent]` and the complete merged joins, relationships and settings (Looker semantics: child overrides parent, joins are additive, a later parent wins). If a parent cannot be found, nothing is merged and it is reported.
- **Template topics.** An explore with `extension: required` becomes a topic with `template: true`. An explore that is extended without `extension: required` stays a normal topic; `--template` forces it. The report states the choice and the reason.
- **Access grants.** Grant names are copied onto fields and topics. The grant definitions and user attributes must be created in Omni by hand.
- **Canonical mappings.** None by default. From `--config` only; applied to labels, with old-term synonyms added. Descriptions are not changed.

## Known limitations

- No validation against a live Omni instance; output is checked against the tool's own models only.
- Model `include:` boundaries are ignored: every `*.lkml` under `--looker-repo` is searched. Duplicate view, explore or grant names stop the run, listing every file that defines the name.
- Refinements (`view: +name`, `explore: +name`) are detected and reported, not applied.
- Each file is written atomically, but a run is not: if a run fails partway, files written earlier in that run stay.
- With `--relationships-file`, Omni relationships are model-wide; per-explore `.relationships` files can conflict (the CLI warns when it sees a conflict in the same directory). The default topic-scoped relationships do not have this problem.
- `always_filter` expressions outside the short translated list (for example `N days`, `next month`, negations, comparisons) are reported, not converted. Of the named periods only `last month` was confirmed on a live Omni instance; the others are written and flagged for verification. `conditionally_filter` has no Omni equivalent.
- Field restrictions: Omni rejects a query that uses a restricted field, where Looker hides the field. A filter-only field in a `fields:` list is left out of the topic list and flagged. A dimension group can only be restricted as a whole.
- An extending view refers to its parent views by name, so every view of the chain must be migrated too. A CSV row for an inherited field writes nothing in the child: its visibility follows the parent view's own CSV scope. View-level access grants and access filters of a parent are reported, not carried.
- An extending topic refers to its parent topic by name, so the parent explores must be migrated too. Several parents in one `extends:` and partial overrides of an inherited join are converted and flagged for review.
- Explore mode does not rewrite join SQL for fields that a merge renamed.
- `omni_view` / `omni_field` CSV columns are tracking only.
- References to Looker timeframe fields other than `date` and `raw` (for example `${created_month}`) are reported, not rewritten.
- Topic-scoped fields have no scope CSV in explore mode: they are written visible unless Looker hides them. A topic-scoped field with Liquid is written only when the views its tokens name are in the topic under their own names.
- An explore whose base alias is also the name of another view is not migrated. A native derived table's `query:` names the topic by the explore name, so it does not follow `--topic-prefix`.
- Liquid converts only in derived table SQL and field `sql:`, and only the constructs with an exact Omni form. `{% if %}`, `{% assign %}`, loops and Liquid filters stay manual, as does Liquid in `html`, `link`, `sql_table_name`, join `sql_on` and `sql_always_where`. A bare `type: unquoted` parameter that is not clearly an identifier (for example inside `DATE_TRUNC(...)`) is not converted.
- A Mustache token names one view: an inherited field whose Liquid names its own view is written again in each extending view. A reference through an alias that stands for several views is not converted.
- Not converted: `drill_fields`, `link`, `html`, `map_layer_name`, `case`, `tiers`, and `tier`/`location`/`distance` dimension types.
- One topic per explore; one view per run.

## Where to look

| Question | File |
|----------|------|
| First run, end to end | `docs/QUICKSTART.md` |
| Views, then explores, then topics | `docs/WORKFLOW.md` |
| Conversion rules | `rules/migration_rules.md` |
| Omni YAML syntax | `rules/model_details_omni.md` |
| Derived tables | `docs/QUERY_VIEWS.md` |
| Access grants | `docs/ACCESS_GRANTS.md` |
| Views with no primary key (optional finder script) | `docs/PRIMARY_KEYS.md` |
| Flags, defaults, exit codes, merging | `reference/COMMAND_REFERENCE.md` |
| CSV format | `reference/CSV_REFERENCE.md` |
| Output files and report sections | `reference/OUTPUT_FILES.md` |
| Before/after examples | `reference/CONVERSION_PATTERNS.md` |
| Errors | `reference/TROUBLESHOOTING.md` |
| Sample LookML and expected output | `examples/simple/` |
| Hand-written example Omni model | `examples/omni_model/` |

Tests live in `tests/`. Run them from this directory with `pip install -r requirements.txt && pytest`.
