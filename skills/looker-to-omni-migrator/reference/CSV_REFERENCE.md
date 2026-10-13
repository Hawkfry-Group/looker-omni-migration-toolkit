# CSV Reference

The scope CSV says which Looker fields move in which phase and who owns them. The migrator reads it and never writes to it.

Default location: `migration/views.csv` (override with `--csv`). Sample: `examples/migration.csv`.

## Columns

```csv
phase,view,field,type,omni_view,omni_field,owner,status,notes
```

| Column | Required | Meaning |
|--------|----------|---------|
| `phase` | yes | `P0`, `P1`, … or `0`, `1`, … Both forms match `--phase 0`. |
| `view` | yes | Looker view name, exactly as in `view: name`. |
| `field` | yes | Looker field name. For a dimension group, the group name (`created`, not `created_date`). |
| `type` | yes | `dimension`, `dimension_group`, `measure` or `parameter`. A legacy `field_type` column is also accepted. |
| `owner` | yes | Person or team. Exact, case-sensitive match with `--owner`. |
| `omni_view`, `omni_field` | no | Tracking only. The migrator does not use them for naming. |
| `status`, `notes` | no | For your team. Ignored by the migrator. |

## Sample

`examples/migration.csv`:

```csv
phase,view,field,type,omni_view,omni_field,owner,status,notes
P0,order_items,id,dimension,,,Data Team,ready,Primary key
P0,order_items,order_id,dimension,,,Data Team,ready,
P0,order_items,status,dimension,,,Data Team,ready,
P0,order_items,sale_price,dimension,,,Data Team,ready,
P0,order_items,created,dimension_group,,,Data Team,ready,
P0,order_items,count,measure,,,Data Team,ready,
P0,order_items,order_count,measure,,,Data Team,ready,
P0,order_items,total_sale_price,measure,,,Data Team,ready,
P0,users,id,dimension,,,Team A,ready,Primary key
P0,users,country,dimension,,,Team A,ready,
P1,users,traffic_source,dimension,,,Team A,ready,
P0,users,created,dimension_group,,,Team A,ready,
P0,users,count,measure,,,Team A,ready,
```

## How rows are selected

`--view users --phase 0 --owner "Team A"` selects rows where `view = users`, the phase is `P0` or `0`, and `owner = Team A` exactly. In the sample that is `id`, `country`, `created` and `count`. `traffic_source` is phase 1, so it is out of scope for this run.

If no rows match, the run stops with exit code 1:

```
Error: No CSV rows found for owner=Team A, phase=0, view=users
```

## What happens to each field

| Field | Result |
|-------|--------|
| In scope | Converted and visible, tagged `mig_phase_N` + `verified` (or `unverified`) |
| Not in scope, but an in-scope field references it (`${field}`, same view, any depth) | Auto-included: converted and visible, also tagged `auto_dependency`, listed in the console and report |
| Not in scope, not referenced | Converted with its SQL but `hidden: true`, tagged only `in_omni_in_looker_not_csv`, listed under Not Converted |
| Cannot be converted (unsupported type, Liquid, untranslatable filter, …) or depends on such a field | Not written, whether in scope or not; reported as MANUAL ACTION REQUIRED |
| In the CSV but not in the LookML view | Reported: `CSV field 'x' (dimension) not found in Looker view v` |
| `type: parameter` | Not converted; reported |

A Looker `hidden: yes` is kept, even on in-scope and auto-included fields.

When the run merges into an existing Omni view (a previous run's output, or `--omni-source-repo`):

- Fields made visible by an earlier phase stay visible.
- Existing fields that are not in scope are kept and hidden; they are never deleted.
- Looker fields the existing view does not have are added: in-scope ones visible, the others hidden and tagged `in_omni_in_looker_not_csv` (as in a fresh run).
- A Looker field that reads a column already present under another name in the Omni view is merged under that name (recorded in the report) and that field is tagged `from_looker__<looker field>`, so later runs, in any phase, keep treating it as that Looker field (no duplicate copy; its access grants follow).

## Phases

Add rows for later phases to the same CSV and run the view again with the new phase:

```bash
python scripts/migrate.py --view users --phase 1 --owner "Team A" \
  --looker-repo examples/simple/thelook --csv examples/migration.csv
```

`traffic_source` becomes visible and tagged `mig_phase_1`. Phase 0 fields stay visible with their `mig_phase_0` tags.

## Common issues

| Symptom | Cause |
|---------|-------|
| `No CSV rows found` | Owner differs in case or spacing; phase does not match; view name differs from the LookML |
| A field is hidden | It is not in scope for this phase and owner |
| A field you did not list is visible | An in-scope field references it; see `auto_dependency` in the report |
| A field you listed is missing | It could not be converted; see Notes / Risks in the report |
| No primary key in the output | The key field is missing from the LookML or could not be converted; add `primary_key: yes` or set it by hand |

## Tips

- Keep the CSV in version control alongside your Omni model.
- Include each view's primary key in its first phase.
- Use one owner value per person or team, spelled consistently.

## See also

- [COMMAND_REFERENCE.md](COMMAND_REFERENCE.md)
- [../rules/migration_rules.md](../rules/migration_rules.md)
