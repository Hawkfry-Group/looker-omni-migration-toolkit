# Looker to Omni Migrator

A Claude Code skill and Python CLI that converts LookML views and explores into Omni model files. A CSV decides which fields move in each phase, every run writes a reviewable report, and anything the converter cannot translate safely is reported as MANUAL ACTION REQUIRED rather than guessed.

## What it produces

| Input | Output |
|-------|--------|
| Looker view (`--view`) | `{view}.view.yaml`, or `{view}.query.view` for a derived table, plus `{view}.migration.md` |
| Looker explore (`--explore`) | `{explore}.topic.yaml` (joins and their relationships in one file), plus `{explore}_explore.md`. With `--relationships-file`, also `{explore}.relationships` |
| Access grants (`--parse-access-grants`) | `access_grants.md` listing each grant, user attribute and allowed values |

Default output locations, relative to where you run the CLI:

```
migration/staging/
├── views/     # view mode output (--omni-repo)
├── topics/    # explore mode output
└── reports/   # reports (sibling of --omni-repo)
```

## Install

Python 3.9 or later. From this directory (`skills/looker-to-omni-migrator/`), which is also where the sample quick start below runs:

```bash
pip install -r requirements.txt
```

## Quick start

Run real migrations from your project root (the folder holding the `looker` symlink and `migration/`), calling the script by its path: `python skills/looker-to-omni-migrator/scripts/migrate.py` in this repo, or the skill's install path if you added it as a plugin. Relative paths and defaults (`./looker`, `migration/views.csv`, `migration/staging/`) resolve against the current directory, so running from inside the skill directory would write output into the skill.

Try it on the bundled thelook e-commerce sample without writing anything:

```bash
python scripts/migrate.py --view users --phase 0 --owner "Team A" \
  --looker-repo examples/simple/thelook \
  --csv examples/migration.csv \
  --dry-run
```

The run ends with:

```
============================================================
⚠️  COMPLETED WITH 1 ITEM(S) NEEDING MANUAL ACTION: users
   - Field access grants copied; define them in the Omni model: can_view_pii
============================================================
```

Drop `--dry-run` to write `migration/staging/views/users.view.yaml` and `migration/staging/reports/users.migration.md`. Compare them with `examples/simple/expected/`.

Or ask Claude: *"Use the looker-to-omni-migrator skill to migrate the users view for phase 0, owner Team A."* Claude asks for anything missing, runs the CLI and walks you through the report, starting with the manual items.

See [docs/QUICKSTART.md](docs/QUICKSTART.md) for a full first run.

## How scope works

The CSV lists the fields to migrate:

```csv
phase,view,field,type,omni_view,omni_field,owner,status,notes
P0,users,id,dimension,,,Team A,ready,Primary key
P0,users,country,dimension,,,Team A,ready,
P1,users,traffic_source,dimension,,,Team A,ready,
P0,users,created,dimension_group,,,Team A,ready,
P0,users,count,measure,,,Team A,ready,
```

`--phase` and `--owner` select rows; `--view` selects the view. Phases can be written `P0` or `0`. Owner matching is exact and case-sensitive.

| Field | Result |
|-------|--------|
| In scope | Converted, visible, tagged `mig_phase_N` and `verified` |
| Referenced by an in-scope field (same view) | Auto-included, visible, tagged `auto_dependency` |
| Other convertible field in the view | Converted, `hidden: true`, tagged `in_omni_in_looker_not_csv` |
| Cannot be converted (or depends on such a field) | Omitted and reported |
| In the CSV but not in the LookML | Reported |

A Looker `hidden: yes` is kept, even on in-scope and auto-included fields. When merging into an existing Omni view, Looker fields it does not have yet are added following the same table (out-of-scope ones hidden).

Re-running the same view for a later phase merges into the earlier output: phase 0 fields stay visible and phase 1 fields become visible.

## Conversion summary

| Looker | Omni |
|--------|------|
| `dimension` with `type:` | dimension without `type:` |
| `${TABLE}.column` | raw `column`, or `${field}` when another dimension is exactly that column; `sql:` omitted when a dimension reads its own column |
| `dimension_group` (`type: time`) | one dimension; Omni provides the timeframes |
| `${created_date}`, `${view.created_raw}` (timeframe of a dimension group) | `${created[date]}`, `${view.created[raw]}`; other timeframes are reported, not rewritten |
| `measure` `type: sum` (and count, count_distinct, average, min, max, median, list, percentile) | `aggregate_type` of the same name |
| `measure` `type: number`, `string`, `yesno`, `date` | calculated measure (SQL kept, no `aggregate_type`) |
| `value_format_name: usd` / `decimal_2` | `format: usdcurrency_2` / `number_2` |
| `${field}`, `${view.field}` | unchanged |
| `dimension_group` with `convert_tz: no` | dimension with `convert_tz: false` |
| `derived_table: { sql: ... }` | `{view}.query.view` with `sql:` |
| `${view.SQL_TABLE_NAME}` in derived table SQL, pointing at a view with a literal `sql_table_name` | the table name, verbatim; blocked when the view is a derived table, is not found or has a templated table name |
| SQL comment that holds Liquid (`-- {% if %} ...`) | removed from the SQL and listed in the report |
| `derived_table: { explore_source: ... }` | `{view}.query.view` with `query:` |
| `filter:`, `parameter:` | filter-only field under the view's `filters:` key (`allowed_value` becomes `suggestion_list`, `default_value` becomes `default_filter`) |
| `{% condition f %} x {% endcondition %}` | `{{# view.f.filter }} x {{/ view.f.filter }}` |
| `{% parameter p %}`, `{{ p._parameter_value }}` | `{{ filters.view.p.value }}` (quotes directly around the tag are dropped) |
| `{% date_start f %}`, `{% date_end f %}` | `{{ filters.view.f.range_start }}`, `{{ filters.view.f.range_end }}` |
| `{{ view.f._in_query }}`, `._is_selected`, `._is_filtered` | `{{ view.f.in_query }}`, `.is_selected`, `.is_filtered` |
| `{{ _user_attributes['x'] }}` | `{{ omni_attributes.x }}` |
| `{% if %}`, `{% assign %}`, loops, Liquid filters | not converted; the SQL block is left out and the construct is named in the report |
| view `extends: [a, b]` | view `extends: [a, b]`; a field the parents define differently is written into the child (Looker: last parent wins, Omni: first) and tagged `resolved_from_extends` |
| view `extension: required`, or a view with no table that is only extended | view `template: true`, without `schema` and `table_name` |
| explore `join` | topic join plus an entry under the topic's `relationships:` |
| `join: shop { from: stores }` | `views: { shop: { extends: [stores] } }` and `join_to_view: shop`, scoped to the topic |
| explore `from:` / `view_name:` that rename the base view | `base_view: <alias>`, declared in the topic's `views:` block with `extends: [real_view]` |
| explore or join `view_label` | `label` for that view in the topic's `views:` block |
| field whose SQL names a join alias (`${shop.store_name}`) | left out of the view file; written in the `views:` block of each topic that has the alias |
| explore `access_filter`, `sql_always_where`, `sql_always_having` | topic `access_filters`, `always_where_sql`, `always_having_sql` |
| explore `always_filter` (`N days ago for N days`, named periods such as `last month`, string values, a number) | topic `default_filters` entry with `required: true`; other expressions are reported |
| explore `fields: [ALL_FIELDS*, -view.a]` | topic `fields: [all_views.*, -view.a, "-tag:table_key"]` |
| join `fields: [view.x]`, `fields: []`, `fields: [-view.x]` | `-view.*, view.x`; `-view.*`; `-view.x` in the topic `fields:` (sets expanded) |
| explore `extends` | topic `extends`, with the merged joins and settings written into the topic |
| explore `extension: required` | topic `template: true` |
| `required_access_grants` | copied; grant definitions are MANUAL ACTION REQUIRED |

Full rules: [rules/migration_rules.md](rules/migration_rules.md).

## Optional features

- **Merge into existing Omni views.** Pass `--omni-source-repo PATH` to enrich schema-synced Omni views. A Looker field that reads a differently named column can merge under that existing column's name; the rename is recorded in the report.
- **Explore analysis.** Pass `--explore-analysis` with `--view` to list the explores that use the view, their restrictions and a topic suggestion. Off by default.
- **Primary key finder.** A separate script, `scripts/find_primary_keys.py`, for views that end up with no primary key. It tests candidate columns on a sample of the data through the Omni CLI, reports what it finds, and writes the key only with `--apply`. See [docs/PRIMARY_KEYS.md](docs/PRIMARY_KEYS.md).
- **Configuration.** Pass `--config` with a copy of [config/migration_config.example.yaml](config/migration_config.example.yaml) to set `default_schema`, `curated_folder` and `canonical_mappings` (label renames such as `sku: product`; empty by default).

## Known limitations

- No validation against a live Omni instance. Load the output in an Omni branch before promoting it.
- Model `include:` boundaries are ignored: every `*.lkml` under `--looker-repo` is searched, and duplicate names stop the run.
- LookML refinements are reported, not applied.
- Each file is written atomically; a whole run is not.
- Relationships are written topic-scoped. With `--relationships-file` they are model-wide in Omni, so separate `.relationships` files can conflict.
- `always_filter` is translated for a small set of expressions only; `N days` and other period expressions are reported. Named periods other than `last month` are written and flagged for verification. `conditionally_filter` is reported only.
- Field restrictions (`fields:`) are carried as topic selectors. Looker hides a restricted field; Omni rejects queries that use it. A list that cannot be resolved with confidence is reported, not carried.
- An extending topic needs its parent topic in the Omni model: migrate the parent explores too.
- An extending view needs its parent views in the Omni model: migrate every view of the chain. Inherited fields are not copied, so their visibility follows the parent view's CSV scope.
- Explore mode does not rewrite join SQL for fields renamed by a merge.
- The CSV `omni_view` / `omni_field` columns are tracking only.
- References to Looker timeframe fields other than `date` and `raw` (for example `${created_month}`) are reported, not rewritten.
- Fields that reference a join alias are written by explore mode, which has no scope CSV: they are visible unless Looker hides them.
- Topic names must be unique in an Omni model. Use `--topic-prefix` when two Looker models have explores of the same name. An explore whose base alias is also the name of another view is not migrated.
- Liquid converts only in derived table SQL and field `sql:`, only for the constructs in the table above, and only when every construct of the SQL block converts. `{% if %}`, `{% assign %}`, loops and Liquid filters stay manual, as does a `type: unquoted` parameter that is not clearly a whole identifier or a quoted string.
- Not converted: `drill_fields`, `link`, `html`, `map_layer_name`, `case`, `tiers`, and `tier`/`location`/`distance` dimensions.
- One view per run (no batch mode) and one topic per explore.

## Documentation

- [SKILL.md](SKILL.md): instructions Claude follows when the skill runs
- [docs/QUICKSTART.md](docs/QUICKSTART.md): first migration, step by step
- [docs/WORKFLOW.md](docs/WORKFLOW.md): views, then explores, then topics
- [docs/QUERY_VIEWS.md](docs/QUERY_VIEWS.md): derived tables to Omni query views
- [docs/ACCESS_GRANTS.md](docs/ACCESS_GRANTS.md): what happens to access controls
- [docs/PRIMARY_KEYS.md](docs/PRIMARY_KEYS.md): optional primary key finder for views that have no key
- [rules/](rules/): conversion rules and Omni syntax reference
- [reference/](reference/): CLI flags, CSV format, output files, conversion patterns, troubleshooting
- [examples/](examples/): sample LookML, CSV, expected output, and a hand-written example Omni model

## Layout

```
looker-to-omni-migrator/
├── SKILL.md
├── README.md
├── requirements.txt
├── scripts/
│   ├── migrate.py             # converter CLI entry point
│   └── find_primary_keys.py   # optional primary key finder
├── lib/                   # parser, converter, generators, Pydantic models
├── config/                # example configuration
├── rules/                 # conversion rules read by the CLI and by Claude
├── docs/
├── reference/
├── examples/
│   ├── migration.csv
│   ├── simple/            # thelook LookML input and expected output
│   └── omni_model/        # example Omni model: views, query views, topics, relationships
└── tests/
```

## Tests

```bash
pip install -r requirements.txt && pytest
```

Run from this directory. Tests live in `tests/`.

## Contributing and support

Report bugs or ask questions through [GitHub Issues](https://github.com/Hawkfry-Group/looker-omni-migration-toolkit/issues). When reporting a conversion problem, include a minimal LookML snippet and the output you expected.

## License

[Apache License 2.0](../../../LICENSE). See [NOTICE](../../../NOTICE).
