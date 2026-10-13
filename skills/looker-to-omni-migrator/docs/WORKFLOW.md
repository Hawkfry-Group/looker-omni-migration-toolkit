# Migration Workflow

A migration runs in three steps: views, then (optionally) explore analysis, then topics. All commands run from the skill directory with `python scripts/migrate.py`.

## Step 1: Migrate views

Migrate shared views first (customers, products), then the views that depend on them.

```bash
python scripts/migrate.py --view order_items --phase 0 --owner "Data Team" \
  --looker-repo ./looker --csv migration/views.csv
```

For each view the CLI:

1. Loads the CSV rows matching `--phase`, `--owner` and the view.
2. Finds the view in any `*.lkml` file under `--looker-repo` (a duplicate view name stops the run).
3. Converts every field of the view. In-scope fields are visible. Same-view fields they reference are auto-included (tag `auto_dependency`). Other convertible fields are written `hidden: true`. Fields that cannot be converted are left out and reported. A Looker `hidden: yes` is kept, even on in-scope and auto-included fields.
4. Merges into an existing view if there is one: a match under `--omni-source-repo`, or otherwise the output of an earlier run in `--omni-repo`. Looker fields the existing view does not have are added (out-of-scope ones hidden).
5. Validates the result, then writes:
   - `migration/staging/views/order_items.view.yaml` (or `.query.view` for a derived table)
   - `migration/staging/reports/order_items.migration.md`

The console ends with either `✅ MIGRATION COMPLETE` or `⚠️ COMPLETED WITH N ITEM(S) NEEDING MANUAL ACTION`, followed by the items. Deal with those before moving on.

To migrate several views, loop over them; there is no batch flag:

```bash
for view in users products inventory_items; do
  python scripts/migrate.py --view "$view" --phase 0 --owner "Team A"
done
```

### Phases

Run the same view again for a later phase and it merges into the existing output. Fields migrated in earlier phases stay visible; the new phase's fields become visible and are tagged with their phase.

With Claude, ask in plain language: *"Migrate users, then products, for phase 0, owner Team A. Stop after each so I can review."*

## Step 2: Analyse explores (optional)

Add `--explore-analysis` to a view migration to find the explores that use the view:

```bash
python scripts/migrate.py --view order_items --phase 0 --owner "Data Team" --explore-analysis
```

This writes `reports/order_items_explore_analysis.md`, listing each explore, its base view, join count, row-level restrictions and a topic suggestion:

| Joins | Suggestion |
|-------|------------|
| Fewer than 5 | One topic |
| 5 or more, fewer than 20 | A core topic and an extended topic |
| 20 or more | Design the split by hand |

The analysis only counts the view you just migrated as migrated. "Ready" needs the base view and every joined view migrated, so an explore with joins usually shows as partly migrated. Suggested join groups list only migrated views and can be empty. Treat the output as a starting point; the CLI does not split explores.

## Step 3: Migrate explores to topics

Once the views an explore needs are migrated:

```bash
python scripts/migrate.py --explore order_items --looker-repo ./looker
```

This writes `order_items.topic.yaml` and `order_items.relationships` to `migration/staging/topics/`, and `order_items_explore.md` to `migration/staging/reports/`. From the test fixture explore (five joins plus an access filter, `sql_always_where`, `always_filter` and an access grant):

```
⚠️  COMPLETED WITH 4 ITEM(S) NEEDING MANUAL ACTION: order_items
   - Access filters (carried): Create these user attributes in Omni before deploying: `allowed_country`
   - sql_always_where (carried): Verify `${order_items.status} != 'Cancelled'` returns the same rows in Omni.
   - always_filter (NOT carried): Looker always_filter: `order_items.created_date: 90 days`. Recreate with Omni `default_filters` or `always_where_filters` as appropriate.
   - Access grants (carried): Define these grants in the Omni model file (`access_grants:`):
```

```yaml
# order_items.topic.yaml
base_view: order_items
label: Orders
description: Order line items with shopper, product and inventory context
fields: [all_views.*, "-tag:table_key"]
joins:
  orders: {}
  users: {}
  inventory_items:
    products:
      distribution_centers: {}
access_filters:
- field: users.country
  user_attribute: allowed_country
always_where_sql: ${order_items.status} != 'Cancelled'
required_access_grants: [can_view_financials]
```

What happens to explore features:

| Looker | Omni |
|--------|------|
| `join` with `type: left_outer` (or no type) | `join_type: always_left` |
| `type: inner`, `full_outer`, `cross` | `inner`, `full_outer`, `cross` |
| other join types or relationships | join not migrated; reported |
| `sql_on` | `on_sql`, with `${view.field}` references kept; joins nested by which view they reference |
| `from:` alias | `join_to_view` plus `join_to_view_as` |
| Liquid, `foreign_key`, raw `sql` joins, or joins that depend on a skipped join | not migrated; reported with the original SQL |
| `access_filter` | topic `access_filters` (create the user attributes in Omni) |
| `sql_always_where`, `sql_always_having` | `always_where_sql`, `always_having_sql` (verify) |
| `required_access_grants`, `hidden`, `group_label` | carried (define the grants in Omni) |
| `always_filter`, `conditionally_filter`, `fields:`, `always_join`, `required_joins`, join-level grants, `extends` | reported only |

The relationships file is written only when at least one join converts. A stale `{explore}.relationships` from an earlier run is removed when a run produces no joins. Omni relationships apply model-wide, so check for conflicting joins between the same two views across files; the CLI warns when it finds one in the same output directory.

Each explore becomes one topic. To split a large explore, copy the generated topic and trim the joins in each copy. Ask Claude to help: *"Split the order_items topic into a sales topic and an inventory topic."*

## Access grants and model-level settings

```bash
python scripts/migrate.py --parse-access-grants --looker-repo ./looker
```

lists every `access_grant` in any `*.lkml` file, with its user attribute and allowed values. See [ACCESS_GRANTS.md](ACCESS_GRANTS.md).

Connections, cache policies and workspace permissions are one-time Omni configuration; the migrator does not touch them. Looker persistence settings (`datagroup_trigger` and so on) are reported as not migrated.

## Output layout

```
migration/staging/
├── views/
│   ├── users.view.yaml
│   ├── order_items.view.yaml
│   └── user_order_facts.query.view
├── topics/
│   ├── order_items.topic.yaml
│   └── order_items.relationships
└── reports/
    ├── users.migration.md
    ├── order_items.migration.md
    ├── order_items_explore_analysis.md
    ├── order_items_explore.md
    └── access_grants.md
```

Review the files, copy them into your Omni model on a branch, validate in Omni, then promote. For a hand-finished example of the target, see `examples/omni_model/`.

## Checklist per view

- [ ] The dry run shows the expected fields in scope
- [ ] Every MANUAL ACTION REQUIRED item has an owner
- [ ] Every auto-included dependency makes sense
- [ ] The view has exactly one primary key
- [ ] Cross-view `${view.field}` references point at views that exist and are joined in the topic
- [ ] The YAML loads in an Omni branch and the fields query correctly

## Tips

- If explore A extends explore B, migrate B first; `extends` is reported, not applied.
- Start with small explores to prove the approach.
- Use `--unverified` for drafts; re-run without it once reviewed.
- Keep the CSV under version control. It is your record of what moved in each phase.
