# Quick Start

Migrate your first Looker view to Omni in about five minutes.

## 1. Install

Python 3.9 or later. From the skill directory (`skills/looker-to-omni-migrator/`):

```bash
pip install -r requirements.txt
```

## 2. Try the sample

The skill ships a small LookML project based on the thelook e-commerce dataset (`examples/simple/thelook/`) and a matching CSV. Do a dry run first. It prints what would be written and writes nothing:

```bash
python scripts/migrate.py --view users --phase 0 --owner "Team A" \
  --looker-repo examples/simple/thelook \
  --csv examples/migration.csv \
  --dry-run
```

Output (paths shortened):

```
[1/5] Loading CSV context...
      ✓ CSV fields in scope: 4

[2/5] Parsing Looker view...
      ✓ Found: examples/simple/thelook/views/users.view.lkml
      ✓ Dimensions: 5
      ✓ Dimension groups: 1
      ✓ Measures: 1

[3/5] Applying conversion rules...
      ✓ dimension: id
      ✓ dimension: country
      ✓ dimension_group: created
      ✓ measure: count

      Summary:
      ✓ Converted (CSV scope): 4
      ⊗ Hidden (not in CSV): 3

[4/5] Building Omni view...
      ✓ Schema: thelook_ecommerce
      ✓ Dimensions: 6  Measures: 1

[5/5] Generating files...
      [dry-run] would write .../migration/staging/views/users.view.yaml (1793 bytes)
      [dry-run] would write .../migration/staging/reports/users.migration.md (2481 bytes)

============================================================
⚠️  COMPLETED WITH 1 ITEM(S) NEEDING MANUAL ACTION: users
   - Field access grants copied; define them in the Omni model: can_view_pii
============================================================

Dry run: no files were written.
```

The four in-scope fields are converted. `email`, `age` and `traffic_source` (phase 1) are written hidden. The `can_view_pii` grant on `email` is copied, but the grant itself has to be defined in Omni.

Drop `--dry-run` to write:

```
migration/staging/views/users.view.yaml
migration/staging/reports/users.migration.md
```

Compare them with `examples/simple/expected/users.view.yaml`. To see incremental phases, run the same command with `--phase 1`: `traffic_source` becomes visible and the phase 0 fields stay visible.

## 3. Prepare your own project

You need:

- **Your Looker repo.** Point `--looker-repo` at it, or symlink it to `./looker` (the default). Every `*.lkml` file under it is searched.
- **A scope CSV.** Default location `migration/views.csv`. Copy `examples/migration.csv` as a starting point. Columns are described in [../reference/CSV_REFERENCE.md](../reference/CSV_REFERENCE.md).

## 4. Migrate with Claude

Start Claude Code and ask:

```
Use the looker-to-omni-migrator skill to migrate the users view for phase 0, owner Team A.
```

Claude asks for anything missing, runs `python scripts/migrate.py`, lists every MANUAL ACTION REQUIRED item, then summarises the report: what was converted, which dependencies were auto-included, what was not converted and why.

Useful follow-ups:

- "Show me the generated view file."
- "Migrate the order_items explore to a topic."
- "Which explores use order_items? Run the explore analysis."
- "List the access grants in my Looker repo."

## 5. Or run the CLI directly

```bash
# View migration
python scripts/migrate.py --view users --phase 0 --owner "Team A"

# Merge into existing schema-synced Omni views
python scripts/migrate.py --view users --phase 0 --owner "Team A" \
  --omni-source-repo ./omni

# Also analyse explores that use the view (off by default)
python scripts/migrate.py --view order_items --phase 0 --owner "Data Team" --explore-analysis

# Explore to topic + relationships (no --phase/--owner)
python scripts/migrate.py --explore order_items

# List access grants
python scripts/migrate.py --parse-access-grants
```

To migrate several views, run one command per view:

```bash
for view in users order_items products; do
  python scripts/migrate.py --view "$view" --phase 0 --owner "Team A"
done
```

Exit codes: `0` success (check the manual items), `1` input or runtime error, `2` invalid arguments.

## Common problems

| Message | Check |
|---------|-------|
| `CSV file not found` | Pass `--csv`, or create `migration/views.csv` |
| `Looker repository not found` | `--looker-repo` path |
| `No CSV rows found for owner=..., phase=..., view=...` | `--owner` matches exactly; `--phase` matches the CSV; the `view` value matches the LookML view name |
| `Looker view '...' not found ... (searched every *.lkml file recursively)` | View name spelling and `--looker-repo` |
| `No primary key dimension in the final view` | Add the key field to the CSV and re-run, or set `primary_key: true` by hand |

More in [../reference/TROUBLESHOOTING.md](../reference/TROUBLESHOOTING.md).

## Next

- [WORKFLOW.md](WORKFLOW.md): the views, explores, topics sequence
- [../rules/migration_rules.md](../rules/migration_rules.md): full conversion rules
- [../SKILL.md](../SKILL.md): what Claude does when the skill runs
