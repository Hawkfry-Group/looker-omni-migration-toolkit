# Troubleshooting

Exit code `2` means the command line is wrong; `1` means the inputs or the conversion failed (detected before any file of that view or explore is written; only an I/O error while writing, or a failing `--explore-analysis` after the view is written, can leave files from the run); `0` means success, but read any MANUAL ACTION REQUIRED items.

## Argument errors (exit 2)

```
scripts/migrate.py: error: --view requires both --phase and --owner
scripts/migrate.py: error: --explore-analysis can only be used with --view
```

Also: no mode, two modes at once, or an unknown flag. Fix the command:

```bash
python scripts/migrate.py --view order_items --phase 0 --owner "Data Team"
```

Quote owners with spaces: `--owner "Data Team"`, not `--owner Data Team`.

## Input errors (exit 1)

### CSV file not found

```
❌ Error: CSV file not found: .../migration/views.csv
```

The default is `migration/views.csv`. A relative `--csv` is looked up in the current directory, then the skill directory. Try the sample: `--csv examples/migration.csv`.

### Looker repository not found

```
❌ Error: Looker repository not found: looker
```

The default `--looker-repo` is `./looker`, relative to where you run the command. Pass your LookML project path, or `examples/simple/thelook` for the sample.

### No CSV rows found

```
Error: No CSV rows found for owner=Team A, phase=0, view=users
```

- The owner must match exactly: `"Team A"` does not match `"team a"` or `"Team A "`.
- The phase must match (`P0` and `0` are equivalent).
- The `view` value must match the LookML `view:` name.

```bash
grep ",users," migration/views.csv
```

### Looker view not found

```
Error: Looker view 'ghost' not found in examples/simple/thelook (searched every *.lkml file recursively)
```

Check the name and `--looker-repo`. Every `*.lkml` file is searched, whatever the folder layout.

```bash
grep -rn "view: *ghost\b" /path/to/looker
```

### Explore not found

```
Error: Explore 'nope' not found in Looker repository
```

Explores are found in any `*.lkml` file, including model files.

### Duplicate definitions

A view, explore or access grant name defined in more than one file stops the run, listing the files. This often means two models define the same view; `include:` statements are not used to choose between them. Rename or remove one copy, or point `--looker-repo` at a narrower folder.

### Table name cannot be parsed

A `sql_table_name` with Liquid, `${other_view.SQL_TABLE_NAME}`, more than three dot-separated parts, or other unparseable content fails the view. Migrate that view by hand or simplify the LookML.

### Native derived table not supported

`bind_filters`, `bind_all_filters`, `derived_column`, a field selected twice, or an untranslatable filter in an `explore_source` fails the view. Write the query view by hand; see [../docs/QUERY_VIEWS.md](../docs/QUERY_VIEWS.md).

### Merge problems

- `--omni-source-repo not found: ...`: the path does not exist.
- `More than one existing Omni view for users in ...`: two files match; narrow `--omni-source-repo`.
- `Existing Omni view ... could not be parsed`: fix or remove the existing file. It is never overwritten.
- The merged view failed validation (a self-reference, a reference to a missing field, more than one primary key, or a name used by both a dimension and a measure): nothing is written. Fix the existing Omni view and re-run.
- `existing field `email` holds Looker field `customer_email` (tag `from_looker__customer_email` ...) but the LookML now also defines a field named `email``: an earlier run merged `customer_email` into `email`, and LookML now has its own `email`. Rename one of the two fields in LookML, or remove the `from_looker__customer_email` tag from `email` in the Omni view, then re-run. Nothing is written.
- `Looker field ... is tagged ... on both ...`: the same `from_looker__<field>` tag is on two fields of the existing view; remove one.
- `the existing view sets catalog ...`: the existing view names a catalog the LookML does not; resolve it in the Omni view or use a 3-part `sql_table_name`.

### Config problems

`Config file not found: ...` or `Config file ... could not be read: ...`: check the `--config` path and the YAML syntax. Without `--config`, built-in defaults are used.

## Successful runs with manual items (exit 0)

The console ends with `⚠️ COMPLETED WITH N ITEM(S) NEEDING MANUAL ACTION` and lists them. Common ones:

| Item | What to do |
|------|------------|
| `CSV field 'x' (dimension) not found in Looker view v` | Fix the CSV row or the field name |
| `Field access grants copied; define them in the Omni model: ...` | Define the grants and user attributes in Omni |
| `...: MANUAL ACTION REQUIRED: dimension type 'tier' has no automatic Omni conversion` | Rebuild the field by hand |
| `...: MANUAL ACTION REQUIRED: filter ... cannot be translated automatically` | Rewrite the filter in Omni syntax by hand |
| `...: MANUAL ACTION REQUIRED: Looker measure type 'running_total' has no automatic Omni conversion` | Rebuild with an Omni calculation or table calculation |
| `Custom value_format '...' not migrated; set an Omni format manually` | Pick an Omni format |
| `No primary key dimension in the final view` | Add the key to the CSV and re-run, or set `primary_key: true` |
| `View is refined in ...; refinements are not applied` | Apply the refinement by hand |
| `View extends ...; inherited fields are not included` | Migrate the base view too, or copy the fields |
| `Join x (NOT migrated): sql_on uses Liquid...` | Write the relationship by hand |
| `always_filter (NOT carried)` | Recreate with an Omni topic filter |

## Other issues

### A field I did not list is in the output

Either it is hidden (`in_omni_in_looker_not_csv`) because every convertible field of the view is written, or it is visible with `auto_dependency` because an in-scope field references it.

### A field I listed is missing

It could not be converted, or it depends on a field that could not be. Look under Notes / Risks and Not Converted in the report.

### A field references `${created_date}` and is missing

Looker timeframe fields do not exist in Omni. The field is reported, not rewritten. Rewrite the reference to the base dimension (for example `${created}`) by hand.

### Earlier output was changed

A view run merges into the existing `{view}.view.yaml` in `--omni-repo`. Delete it first to start fresh.

### The relationships file disappeared

An explore run that produces no joins removes the old `{explore}.relationships`.

### Can't find the output files

By default: `migration/staging/views/`, `migration/staging/topics/` and `migration/staging/reports/` under the directory you ran the command from. With `--omni-repo X`, reports go to `X/../reports`.

### Omni rejects the generated YAML

The tool validates against its own models, not a live Omni instance. Check the syntax in [../rules/model_details_omni.md](../rules/model_details_omni.md) and the Omni docs (<https://docs.omni.co>), and report the case on GitHub.

## Getting help

- Preview any run with `--dry-run`.
- Read the report for the view or explore.
- Open an issue at <https://github.com/Hawkfry-Group/looker-omni-migration-toolkit/issues> with a minimal LookML snippet, the command, and the output.

## See also

- [COMMAND_REFERENCE.md](COMMAND_REFERENCE.md)
- [CSV_REFERENCE.md](CSV_REFERENCE.md)
