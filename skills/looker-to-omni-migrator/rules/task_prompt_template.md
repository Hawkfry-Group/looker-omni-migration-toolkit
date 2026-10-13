# Task Template: Migrate a Looker View to Omni

Use this template when asking Claude to migrate, review or hand-finish a view. Fill in the placeholders. Do not hard-code machine-specific absolute paths; pass them per run.

## 1. Run inputs

```
view        = <LOOKER_VIEW_NAME>
phase       = <PHASE_NUMBER>          # matches P<N> or <N> in the CSV
owner       = <OWNER_NAME>            # exact, case-sensitive
looker_repo = <PATH>                  # default ./looker
csv_path    = <PATH>                  # default migration/views.csv
omni_repo   = <PATH>                  # default ./migration/staging/views
omni_source = <PATH or none>          # existing Omni views to merge into
config      = <PATH or none>          # optional migration config YAML
```

To migrate several views, repeat the task once per view.

## 2. Steps

1. Dry run first:
   ```bash
   python scripts/migrate.py --view <view> --phase <phase> --owner "<owner>" \
     --looker-repo <looker_repo> --csv <csv_path> --dry-run
   ```
   Confirm the fields in scope with the user.
2. Run the same command without `--dry-run`. Add `--omni-source-repo <omni_source>` and `--config <config>` if given.
3. If the console says `COMPLETED WITH N ITEM(S) NEEDING MANUAL ACTION`, list every item to the user first.
4. Read `{view}.migration.md` in the reports directory and check the output against `rules/migration_rules.md`:
   - Exactly one primary key, or the missing key is reported.
   - No `type:`, `html:`, `value_format:` or `datatype:` in fields.
   - `dimension_group` fields are single dimensions without `timeframes:`.
   - Derived tables are `{view}.query.view` with `sql:` (raw SQL) or `query:` (native query).
   - Cross-view `${view.field}` references point at views that exist in Omni and are joined in the topic.
   - Everything under Not Converted, Notes / Risks (HIGH, MEDIUM) and Advanced Features Detected is understood.
   - In-scope and auto-included fields are tagged `mig_phase_<phase>` and `verified` (or `unverified`); hidden out-of-scope fields carry `in_omni_in_looker_not_csv`.
   - Canonical mappings are applied only if the config defines them.
5. Summarise for the user: converted fields, auto-included dependencies, items not converted, risks, and every MANUAL ACTION REQUIRED item.
6. If the user asks for hand edits, mark each opinionated change with `# OPINION:` in the YAML and note it for the PR.

## 3. Stop and ask when

- The CLI exits with code 1 (for example: no CSV rows match, the view is not found or defined twice, the merged view is invalid).
- A field cannot be converted safely (ambiguous logic, unsupported feature).

## 4. Never

- Edit the CSV.
- Remove existing Omni fields.
- Guess a conversion instead of reporting it.

Suggested commit message for the result: `MIG: <view> (phase <PHASE_NUMBER>)`.
