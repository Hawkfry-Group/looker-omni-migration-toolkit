# Looker to Omni Migrator

A Claude Code skill and Python CLI that converts LookML views and explores into Omni model files. A CSV decides which fields move in each phase, every run writes a reviewable report, and anything the converter cannot translate safely is reported as MANUAL ACTION REQUIRED rather than guessed.

## What it produces

| Input | Output |
|-------|--------|
| Looker view (`--view`) | `{view}.view.yaml`, or `{view}.query.view` for a derived table, plus `{view}.migration.md` |
| Looker explore (`--explore`) | `{explore}.topic.yaml`, `{explore}.relationships` (when at least one join converts), plus `{explore}_explore.md` |
| Access grants (`--parse-access-grants`) | `access_grants.md` listing each grant, user attribute and allowed values |

Default output locations, relative to where you run the CLI:

```
migration/staging/
├── views/     # view mode output (--omni-repo)
├── topics/    # explore mode output
└── reports/   # reports (sibling of --omni-repo)
```

## Install

Python 3.9 or later. From this directory (`skills/looker-to-omni-migrator/`):

```bash
pip install -r requirements.txt
```

## Quick start

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
| `measure` `type: sum` (and count, count_distinct, average, min, max, median, list, percentile) | `aggregate_type` of the same name |
| `measure` `type: number`, `string`, `yesno`, `date` | calculated measure (SQL kept, no `aggregate_type`) |
| `value_format_name: usd` / `decimal_2` | `format: usdcurrency_2` / `number_2` |
| `${field}`, `${view.field}` | unchanged |
| `derived_table: { sql: ... }` | `{view}.query.view` with `sql:` |
| `derived_table: { explore_source: ... }` | `{view}.query.view` with `query:` |
| explore `join` | topic join plus `.relationships` entry |
| explore `access_filter`, `sql_always_where`, `sql_always_having` | topic `access_filters`, `always_where_sql`, `always_having_sql` |
| `required_access_grants` | copied; grant definitions are MANUAL ACTION REQUIRED |

Full rules: [rules/migration_rules.md](rules/migration_rules.md).

## Optional features

- **Merge into existing Omni views.** Pass `--omni-source-repo PATH` to enrich schema-synced Omni views. A Looker field that reads a differently named column can merge under that existing column's name; the rename is recorded in the report.
- **Explore analysis.** Pass `--explore-analysis` with `--view` to list the explores that use the view, their restrictions and a topic suggestion. Off by default.
- **Configuration.** Pass `--config` with a copy of [config/migration_config.example.yaml](config/migration_config.example.yaml) to set `default_schema`, `curated_folder` and `canonical_mappings` (label renames such as `sku: product`; empty by default).

## Known limitations

- No validation against a live Omni instance. Load the output in an Omni branch before promoting it.
- Model `include:` boundaries are ignored: every `*.lkml` under `--looker-repo` is searched, and duplicate names stop the run.
- LookML refinements are reported, not applied.
- Each file is written atomically; a whole run is not.
- Omni relationships are model-wide, so separate `.relationships` files can conflict.
- Explore mode does not rewrite join SQL for fields renamed by a merge.
- The CSV `omni_view` / `omni_field` columns are tracking only.
- References to Looker timeframe fields (for example `${created_date}`) are reported, not rewritten.
- Not converted: `drill_fields`, `link`, `html`, `map_layer_name`, parameters, Liquid, `case`, `tiers`, and `tier`/`location`/`distance` dimensions.
- One view per run (no batch mode) and one topic per explore.

## Documentation

- [SKILL.md](SKILL.md): instructions Claude follows when the skill runs
- [docs/QUICKSTART.md](docs/QUICKSTART.md): first migration, step by step
- [docs/WORKFLOW.md](docs/WORKFLOW.md): views, then explores, then topics
- [docs/QUERY_VIEWS.md](docs/QUERY_VIEWS.md): derived tables to Omni query views
- [docs/ACCESS_GRANTS.md](docs/ACCESS_GRANTS.md): what happens to access controls
- [rules/](rules/): conversion rules and Omni syntax reference
- [reference/](reference/): CLI flags, CSV format, output files, conversion patterns, troubleshooting
- [examples/](examples/): sample LookML, CSV, expected output, and a hand-written example Omni model

## Layout

```
looker-to-omni-migrator/
├── SKILL.md
├── README.md
├── requirements.txt
├── scripts/migrate.py     # CLI entry point
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
