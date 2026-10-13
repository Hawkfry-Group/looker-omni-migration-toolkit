---
name: looker-to-omni-migrator
description: "Migrate Looker views and explores to Omni. Converts LookML views to Omni .view.yaml (or .query.view for derived tables) with a CSV controlling which fields are in scope, converts explores to Omni topics and relationships, writes a migration report per view and explore, and lists Looker access grants. Use when the user asks to migrate, convert or port a Looker view or explore to Omni."
---

# Looker to Omni Migrator

This skill wraps a Python CLI, `scripts/migrate.py`, that converts LookML into Omni model files. You (Claude) collect the inputs, run the CLI from the user's project, then review the generated files and report with the user.

The CLI has three mutually exclusive modes:

| Mode | Flag | Writes |
|------|------|--------|
| View migration | `--view NAME` | `{view}.view.yaml` (or `{view}.query.view` for a derived table) and `{view}.migration.md` |
| Explore migration | `--explore NAME` | `{explore}.topic.yaml`, `{explore}.relationships` (only if at least one join converts) and `{explore}_explore.md` |
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
- **Dates.** A `type: time` `dimension_group` becomes one dimension named after the group. No `timeframes:` or `datatype:` is written; the Looker timeframes are noted in the report.
- **Measures.** `count`, `count_distinct`, `sum`, `average`, `min`, `max`, `median`, `list`, `percentile` become `aggregate_type`. `number`, `string`, `yesno`, `date` become calculated measures. Anything else (for example `running_total`, `percent_of_total`, `sum_distinct`) is not written: HIGH risk, manual. Measure `filters` are translated by field type, and a filter that cannot be translated stops the measure.
- **Formats.** `decimal_N` becomes `number_N`, `percent_N` stays `percent_N`, `usd`/`eur`/`gbp` (and `_0`) become `usdcurrency_2` and so on, and `id` stays `id`. Other names and custom `value_format` strings are not written (MEDIUM risk).
- **Derived tables.** SQL derived tables become `{view}.query.view` with `sql:`; Liquid or `${...}` references in the SQL stop the view (exit 1, nothing written). Native derived tables become `{view}.query.view` with `query:` (`fields`, `base_view`, `topic`, `filters`, `sorts`, `limit`).
- **Explores.** One topic per explore, with `fields: [all_views.*, "-tag:table_key"]` and joins nested by dependency. `access_filter`, `sql_always_where`, `sql_always_having`, `required_access_grants`, `hidden` and `group_label` are carried; `always_filter`, `conditionally_filter`, `always_join`, `required_joins`, field restrictions and join-level grants are reported only.
- **Access grants.** Grant names are copied onto fields and topics. The grant definitions and user attributes must be created in Omni by hand.
- **Canonical mappings.** None by default. From `--config` only; applied to labels, with old-term synonyms added. Descriptions are not changed.

## Known limitations

- No validation against a live Omni instance; output is checked against the tool's own models only.
- Model `include:` boundaries are ignored: every `*.lkml` under `--looker-repo` is searched. Duplicate view, explore or grant names stop the run.
- Refinements (`view: +name`, `explore: +name`) are detected and reported, not applied.
- Each file is written atomically, but a run is not: if a run fails partway, files written earlier in that run stay.
- Omni relationships are model-wide; per-explore `.relationships` files can conflict (the CLI warns when it sees a conflict in the same directory).
- Explore mode does not rewrite join SQL for fields that a merge renamed.
- `omni_view` / `omni_field` CSV columns are tracking only.
- References to Looker timeframe fields such as `${created_date}` are reported, not rewritten.
- Not converted: `drill_fields`, `link`, `html`, `map_layer_name`, parameters, Liquid, `case`, `tiers`, and `tier`/`location`/`distance` dimension types.
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
| Flags, defaults, exit codes, merging | `reference/COMMAND_REFERENCE.md` |
| CSV format | `reference/CSV_REFERENCE.md` |
| Output files and report sections | `reference/OUTPUT_FILES.md` |
| Before/after examples | `reference/CONVERSION_PATTERNS.md` |
| Errors | `reference/TROUBLESHOOTING.md` |
| Sample LookML and expected output | `examples/simple/` |
| Hand-written example Omni model | `examples/omni_model/` |

Tests live in `tests/`. Run them from this directory with `pip install -r requirements.txt && pytest`.
