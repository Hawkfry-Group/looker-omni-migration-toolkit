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
| `--explore NAME` | — | `{explore}.topic.yaml` and (if any join converts) `{explore}.relationships` in `--omni-repo`; `{explore}_explore.md` in reports |
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

## Merging

A view migration merges into an existing Omni view when it finds one:

1. A match under `--omni-source-repo`, if given.
2. Otherwise, the file a previous run wrote to `--omni-repo` (`{view}.view.yaml` or `{view}.query.view`).

So re-running a view for a later phase keeps the earlier phase's fields visible. Delete the output file to start from scratch. Merge rules are in [../rules/migration_rules.md](../rules/migration_rules.md#merging).

## Exit codes

| Code | Meaning |
|------|---------|
| `0` | Success. Manual items may still be listed (`COMPLETED WITH N ITEM(S) NEEDING MANUAL ACTION`). |
| `1` | Input or runtime error: repo, CSV, view or explore not found; no CSV rows in scope; duplicate view/explore/grant names; unparseable `sql_table_name`; unsupported native derived table; Liquid or `${...}` in SQL derived-table SQL; unreadable existing view; existing `catalog` the LookML does not name; rename conflict (a `from_looker__<field>` tag whose target name is now also a LookML field, or the same tag on two fields); more than one `--omni-source-repo` match; invalid merged view; missing or malformed `--config`; missing `--omni-source-repo` path. These are detected before any file of the failing view or explore is written. Exceptions: an I/O error while writing (each file is replaced atomically, but files already written by the run stay), and a failing `--explore-analysis`, which runs after the view files are written. |
| `2` | Invalid arguments: no mode or two modes, `--view` without `--phase`/`--owner`, `--explore-analysis` without `--view`, unknown flags. |

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
[3/4] Generating topic and relationships files...
      ✓ Topic: .../migration/staging/topics/order_items.topic.yaml
      ✓ Relationships: .../migration/staging/topics/order_items.relationships
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

## See also

- [CSV_REFERENCE.md](CSV_REFERENCE.md)
- [OUTPUT_FILES.md](OUTPUT_FILES.md)
- [TROUBLESHOOTING.md](TROUBLESHOOTING.md)
