# Command Reference

Commands below are written as `python scripts/migrate.py`, as run from the skill directory, which is right for the bundled `examples/`. For your own project, run from the project root and call the script by its path (`python skills/looker-to-omni-migrator/scripts/migrate.py` in this repo, or the installed skill's path) so `./looker`, `migration/views.csv` and `migration/staging/` resolve in your project:

```bash
python scripts/migrate.py <mode> [options]
```

## Modes

Exactly one is required.

| Mode | Needs | Writes |
|------|-------|--------|
| `--view NAME` | `--phase`, `--owner` | `{view}.view.yaml` or `{view}.query.view` in `--omni-repo`; `{view}.migration.md` in reports |
| `--explore NAME` | — | `{explore}.topic.yaml` in `--omni-repo` (with `--relationships-file`, also `{explore}.relationships` if any join converts); `{explore}_explore.md` in reports |
| `--parse-access-grants` | — | `access_grants.md` in reports |

## Flags

### Scope (view mode)

| Flag | Notes |
|------|-------|
| `--phase N` | Matches CSV `phase` values `PN` or `N`. Required with `--view`. |
| `--owner "Name"` | Exact, case-sensitive match on the CSV `owner` column. Quote names with spaces. Required with `--view`. |
| `--csv PATH` | Default `migration/views.csv`. A relative path is looked up in the current directory first, then the skill directory. Sample: `examples/migration.csv`. |

### Paths

| Flag | Default | Notes |
|------|---------|-------|
| `--looker-repo PATH` | `./looker` | Every `*.lkml` file under it is searched, at any depth, including model files. `include:` statements are ignored. |
| `--omni-repo PATH` | `./migration/staging/views` (view and grants modes), `./migration/staging/topics` (explore mode) | Output directory. Reports always go to the sibling directory `reports/` (for the defaults, `./migration/staging/reports`). |
| `--omni-source-repo PATH` | none | Existing Omni views to merge into. Searched recursively for `{view}.view.yaml`, `{view}.view`, and `{view}.query.view` for derived tables. More than one match is an error. A missing path is an error. |

### Configuration

| Flag | Default | Notes |
|------|---------|-------|
| `--config PATH` | none | YAML with `default_schema`, `curated_folder`, `canonical_mappings`. See `config/migration_config.example.yaml`. A missing or malformed file is an error. |
| `--default-schema NAME` | `analytics` | Used when the schema cannot be read from `sql_table_name` (or derived-table SQL). Overrides `--config`. |
| `--curated-folder NAME` | `curated` | Written as `folder: <schema>/<curated_folder>`, where `<schema>` is the resolved schema. Overrides `--config`. |

Precedence: command-line flag, then `--config`, then the built-in default.

### Behaviour

| Flag | Notes |
|------|-------|
| `--unverified` | Tag migrated objects `unverified` instead of `verified`. |
| `--dry-run` | Prints `[dry-run] would write <path> (<n> bytes)` and `[dry-run] would remove <path>` lines. Creates, changes and deletes nothing, including directories. |
| `--explore-analysis` | Only with `--view`. Also writes `{view}_explore_analysis.md` for explores that use the view. |
| `--relationships-file` | Only with `--explore`. Writes the join definitions to a separate `{explore}.relationships` file (model-wide in Omni) and reports joins that conflict with other `.relationships` files in the output directory. Default: the join definitions go inside the topic file under `relationships:` (topic-scoped), and no separate file is written. |
| `--topic-prefix VALUE` | Only with `--explore`. Writes `{VALUE}__{explore}.topic.yaml` (and the report under the same name) and prefixes the topics named in `extends:`. `model` uses the name of the one `*.model.lkml` file that defines the explore or includes its file; the run stops if there is none or more than one. Default: no prefix. |
| `--phase N` with `--explore` | Optional. Tags the fields written in the topic's `views:` block with `mig_phase_N`. Without it they carry no phase tag. |
| `--template` | Only with `--explore`. Writes `template: true` on the topic, for an explore that is only a base for other explores. An explore with `extension: required` gets it without the flag. |

## Merging

A view migration merges into an existing Omni view when it finds one:

1. A match under `--omni-source-repo`, if given.
2. Otherwise, the file a previous run wrote to `--omni-repo` (`{view}.view.yaml` or `{view}.query.view`).

So re-running a view for a later phase keeps the earlier phase's fields visible. Delete the output file to start from scratch. Merge rules are in [../rules/migration_rules.md](../rules/migration_rules.md#merging).

## Exit codes

| Code | Meaning |
|------|---------|
| `0` | Success. Manual items may still be listed (`COMPLETED WITH N ITEM(S) NEEDING MANUAL ACTION`). |
| `1` | Input or runtime error: repo, CSV, view or explore not found; no CSV rows in scope; duplicate view/explore/grant names; unparseable `sql_table_name`; unsupported native derived table; untranslatable Liquid or `${...}` references (other than `${view.SQL_TABLE_NAME}` of a plain table) in SQL derived-table SQL; unreadable existing view; existing `catalog` the LookML does not name; rename conflict (a `from_looker__<field>` tag whose target name is now also a LookML field, or the same tag on two fields); more than one `--omni-source-repo` match; invalid merged view; missing or malformed `--config`; missing `--omni-source-repo` path. These are detected before any file of the failing view or explore is written. Exceptions: an I/O error while writing (each file is replaced atomically, but files already written by the run stay), and a failing `--explore-analysis`, which runs after the view files are written. |
| `2` | Invalid arguments: no mode or two modes, `--view` without `--phase`/`--owner`, `--explore-analysis` without `--view`, `--relationships-file` or `--template` without `--explore`, unknown flags. |

## Examples

### Migrate a view (sample project)

```bash
python scripts/migrate.py --view order_items --phase 0 --owner "Data Team" \
  --looker-repo examples/simple/thelook --csv examples/migration.csv
```

```
[1/5] Loading CSV context...
      ✓ CSV fields in scope: 8
[2/5] Parsing Looker view...
      ✓ Found: examples/simple/thelook/views/order_items.view.lkml
      ✓ Dimensions: 6
      ✓ Dimension groups: 1
      ✓ Measures: 4
[3/5] Applying conversion rules...
      ...
      Summary:
      ✓ Converted (CSV scope): 8
      ⊗ Hidden (not in CSV): 3
[4/5] Building Omni view...
      ✓ Schema: thelook_ecommerce
      ✓ Dimensions: 7  Measures: 4
[5/5] Generating files...
      ✓ View: .../migration/staging/views/order_items.view.yaml
      ✓ Report: .../migration/staging/reports/order_items.migration.md

============================================================
✅ MIGRATION COMPLETE: order_items
============================================================
```

### Preview without writing

```bash
python scripts/migrate.py --view users --phase 0 --owner "Team A" \
  --looker-repo examples/simple/thelook --csv examples/migration.csv --dry-run
```

### Draft output

```bash
python scripts/migrate.py --view users --phase 0 --owner "Team A" --unverified
```

### Explore analysis

```bash
python scripts/migrate.py --view order_items --phase 0 --owner "Data Team" \
  --looker-repo examples/simple/thelook --csv examples/migration.csv --explore-analysis
```

```
============================================================
ANALYZING EXPLORES
============================================================

Found 1 explore(s) using order_items:
  - order_items (base: order_items, joins: 1)
   Explore analysis: .../migration/staging/reports/order_items_explore_analysis.md
```

### Explore to topic

```bash
python scripts/migrate.py --explore order_items --looker-repo examples/simple/thelook
```

```
[1/4] Finding explore...
      ✓ Found: examples/simple/thelook/explores/order_items.explore.lkml
      ✓ Base view: order_items
      ✓ Joins: 1
[2/4] Building Omni topic...
      ✓ Topic: order_items (1 join(s), 0 skipped)
[3/4] Writing topic file...
      ✓ Topic: .../migration/staging/topics/order_items.topic.yaml
[4/4] Generating explore migration report...
      ✓ Report: .../migration/staging/reports/order_items_explore.md

============================================================
⚠️  COMPLETED WITH 1 ITEM(S) NEEDING MANUAL ACTION: order_items
   - Access filters (carried): Create these user attributes in Omni before deploying: `allowed_country`
============================================================
```

### List access grants

```bash
python scripts/migrate.py --parse-access-grants --looker-repo examples/simple/thelook
```

### Your own project with a config

```bash
cp config/migration_config.example.yaml config/migration_config.yaml
# edit default_schema / curated_folder / canonical_mappings

python scripts/migrate.py --view users --phase 0 --owner "Team A" \
  --looker-repo ./looker --csv migration/views.csv \
  --config config/migration_config.yaml \
  --omni-source-repo ./omni
```

### Several views

There is no batch mode. Loop instead and review each report:

```bash
for view in users products order_items; do
  python scripts/migrate.py --view "$view" --phase 0 --owner "Team A" || break
done
```

## Primary key finder

A separate, optional script for views that have no primary key. It queries a sample of the data through the Omni CLI and needs an Omni role that is allowed to run manually written SQL. Full description: [../docs/PRIMARY_KEYS.md](../docs/PRIMARY_KEYS.md).

```bash
python scripts/find_primary_keys.py --model-id MODEL_ID [options]
```

| Flag | Default | Notes |
|------|---------|-------|
| `--model-id ID` | required | Omni model the SQL runs against. |
| `--branch-id ID` | none | Omni branch, when the views only exist on a branch. |
| `--omni-repo DIR` | `./migration/staging/views` | Searched recursively for `*.view.yaml`, `*.view` and `*.query.view`. |
| `--view NAME` | all views | Only this view (the file name without its suffix). Repeatable. An unknown name is an error. |
| `--key VIEW=DIMENSION` | none | Your choice of key for a view with status `multiple_candidates`. Repeatable, one per view. Used only if that dimension is unique and not null in the sample; otherwise it is refused and nothing is written for that view. Written only with `--apply`. A view name that is not in the run is an error. |
| `--recheck` | off | Also test views that already have a primary key. The existing key is reported, never replaced. |
| `--sample-rows N` | `1000000` | Target sample size per view. |
| `--date-column NAME` | none | Sample a recent window on this column instead of the plan for the warehouse. |
| `--window-days N` | `30` | Size of the date window. Used with `--date-column`, and for the Redshift sort key window. |
| `--max-unpartitioned-rows N` | `5000000` | BigQuery: an unpartitioned table with more rows is not scanned (`needs_decision`). |
| `--allow-full-scan` | off | BigQuery: scan unpartitioned tables whatever their size. |
| `--min-rows N` | `1000` | Smallest sample that can qualify a key. |
| `--max-candidates N` | `12` | Most columns tested per view. |
| `--max-pairs N` | `10` | Most column pairs tested when no single column is unique. `0` turns pairs off. |
| `--profile NAME` | CLI default | Passed to the Omni CLI as `-p`. |
| `--base-url URL` | CLI default | Passed to the Omni CLI as `--base-url`. |
| `--dialect NAME` | from the model's connection | `bigquery`, `snowflake`, `redshift`, `postgres`, `duckdb` or `motherduck`. Skips the connection lookup. |
| `--apply` | off (dry run) | Write `primary_key: true` and the tag `primary_key_from_sample` on the chosen dimension. |
| `--report PATH` | `primary_keys.md` | Markdown report. Written in dry runs too. |
| `--json PATH` | none | Machine-readable report: `model_id`, `branch_id`, `dialect`, `mode`, `note`, `summary`, and `views` with one record per view (`view`, `file`, `table`, `dialect`, `strategy`, `filter`, `rows_checked`, `candidates`, `qualifying`, `chosen_key`, `key_source`, `existing_key`, `pair_suggestions`, `status`, `message`, `applied`). |

Statuses: `key_found`, `multiple_candidates`, `pair_suggested`, `no_key_found`, `sample_too_small`, `needs_decision`, `query_error`, `skipped`.

| Code | Meaning |
|------|---------|
| `0` | The run completed. Some views may have no key or several candidates to choose from (`multiple_candidates`, nothing written), a refused `--key` is reported for its view, and a query error on one view is recorded as `query_error` without stopping the run. |
| `1` | An input or connection error stopped the run: `--omni-repo` missing or empty, unknown `--view`, `--key` naming a view that is not in the run, Omni CLI not installed, the dialect could not be read, authentication failed, or the role may not run manually written SQL. No report is written when a connection error stops the run. |
| `2` | Invalid arguments, including a `--key` that is not `VIEW=DIMENSION` or two different `--key` values for one view. |

```bash
# Dry run, then read primary_keys.md
python scripts/find_primary_keys.py --model-id MODEL_ID --branch-id BRANCH_ID

# Write the keys
python scripts/find_primary_keys.py --model-id MODEL_ID --branch-id BRANCH_ID --apply

# Several columns were unique in the sample: choose one, then write it
python scripts/find_primary_keys.py --model-id MODEL_ID --view sales --key sales=transaction_id --apply

# One large table, last 7 days on a date column
python scripts/find_primary_keys.py --model-id MODEL_ID --view events --date-column event_date --window-days 7
```

## See also

- [CSV_REFERENCE.md](CSV_REFERENCE.md)
- [OUTPUT_FILES.md](OUTPUT_FILES.md)
- [TROUBLESHOOTING.md](TROUBLESHOOTING.md)
- [../docs/PRIMARY_KEYS.md](../docs/PRIMARY_KEYS.md)
