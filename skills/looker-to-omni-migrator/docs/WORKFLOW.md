# Migration Workflow

A migration runs in three steps: views, then (optionally) explore analysis, then topics. All commands run from your project root (the folder holding the `looker` symlink and `migration/`), calling the script by its path. In this repo that is `python skills/looker-to-omni-migrator/scripts/migrate.py`; with the plugin installed, use the skill's install path. Relative defaults resolve against the current directory.

## Step 1: Migrate views

Migrate shared views first (customers, products), then the views that depend on them.

```bash
python skills/looker-to-omni-migrator/scripts/migrate.py --view order_items --phase 0 --owner "Data Team" \
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
  python skills/looker-to-omni-migrator/scripts/migrate.py --view "$view" --phase 0 --owner "Team A"
done
```

### Views that extend other views

`--view child` resolves `extends:` across every `.lkml` file under `--looker-repo` and writes `extends: [parent]` on the Omni view. Inherited fields stay in the parent views, so migrate each view of the chain (the report lists them), templates included: a view with `extension: required` becomes a view with `template: true` and no table. List a field in the CSV under the view that defines it; a row for an inherited field under the child writes nothing. A view with no fields of its own needs no CSV rows.

### Phases

Run the same view again for a later phase and it merges into the existing output. Fields migrated in earlier phases stay visible; the new phase's fields become visible and are tagged with their phase.

With Claude, ask in plain language: *"Migrate users, then products, for phase 0, owner Team A. Stop after each so I can review."*

## Step 2: Analyse explores (optional)

Add `--explore-analysis` to a view migration to find the explores that use the view:

```bash
python skills/looker-to-omni-migrator/scripts/migrate.py --view order_items --phase 0 --owner "Data Team" --explore-analysis
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
python skills/looker-to-omni-migrator/scripts/migrate.py --explore order_items --looker-repo ./looker
```

This writes `order_items.topic.yaml` (the topic, with its join definitions under `relationships:`) to `migration/staging/topics/`, and `order_items_explore.md` to `migration/staging/reports/`. From the test fixture explore (five joins plus an access filter, `sql_always_where`, `always_filter` and an access grant):

```
⚠️  COMPLETED WITH 4 ITEM(S) NEEDING MANUAL ACTION: order_items
   - Access filters (carried): Create these user attributes in Omni before deploying: `allowed_country`
   - sql_always_where (carried): Verify `${order_items.status} != 'Cancelled'` returns the same rows in Omni.
   - always_filter (NOT carried): Looker always_filter not written: `order_items.created_date: "90 days"`. Recreate each one as an Omni `default_filters` entry with `required: true` (it needs a value).
   - Access grants (carried): Define these grants in the Omni model file (`access_grants:`):
```

```yaml
# order_items.topic.yaml
base_view: order_items
label: Orders
description: Order line items with shopper, product and inventory context
fields: [all_views.*, "-tag:table_key"]
relationships:
- join_from_view: order_items
  join_to_view: orders
  join_type: always_left
  on_sql: ${order_items.order_id} = ${orders.order_id}
  relationship_type: many_to_one
  reversible: false
# ... one entry per migrated join
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
| join `from:` alias | declared in the topic's `views:` block with `extends: [real_view]` and joined by the alias |
| explore `from:` / `view_name:` that rename the base view | `base_view: <alias>`, declared in the topic's `views:` block with `extends: [real_view]` |
| `view_label` | `label` in the topic's `views:` block |
| fields of the topic's views that reference a join alias | written in the topic's `views:` block (view mode leaves them out of the view file) |
| Liquid, `foreign_key`, raw `sql` joins, or joins that depend on a skipped join | not migrated; reported with the original SQL |
| `access_filter` | topic `access_filters` (create the user attributes in Omni) |
| `sql_always_where`, `sql_always_having` | `always_where_sql`, `always_having_sql` (verify) |
| `required_access_grants`, `hidden`, `group_label` | carried (define the grants in Omni) |
| `always_filter` with `N days ago for N days` (also weeks, months), a string value or list, or one number | topic `default_filters` entry with `required: true` (verify) |
| `always_filter` with a named period on a date field (`last month`, `this week`, `today`, ...) | `is: last month` with `required: true` (periods other than `last month`: verify) |
| `always_filter` with any other expression (`90 days`, `next month`) or no value | not written; reported with the Looker expression |
| explore and join `fields:` | topic `fields:` selectors (`all_views.*, -view.a`; `-view.*, view.x`); reported when a list cannot be resolved |
| `extends` | topic `extends` plus the merged joins and settings of the parent chain; reported and not merged if a parent cannot be found |
| `extension: required` | topic `template: true` |
| `conditionally_filter`, `always_join`, `required_joins`, join-level grants | reported only |

The join definitions are topic-scoped: they live in the topic file and apply to that topic only, so two explores can join the same views differently. To keep one model-wide relationships file instead, add `--relationships-file`: the joins are then written to `{explore}.relationships` (only when at least one join converts; a stale file from an earlier run is removed when a run produces no joins) and the CLI warns when another `.relationships` file in the output directory joins the same two views differently.

### Aliases and cross-view fields

Aliases are kept as in Looker and declared inside the topic, so the same alias can mean different views in different topics. The explore report lists them in an "Alias map" table and reports only the alias references that do not resolve.

A field such as `sql: ${shop.store_name}`, where `shop` is a join alias, cannot live in a view file. View mode lists it under "Topic-scoped fields" in the view report, and explore mode writes it in the `views:` block of each topic that has the alias. These fields have no scope CSV row in explore mode: they are written visible unless Looker hides them. Pass `--phase N` with `--explore` to tag them with the migration phase.

### Topic names across models

Topic names must be unique in an Omni model, and topic files are not namespaced by folder. When two Looker models have an explore of the same name, add a prefix:

```bash
python skills/looker-to-omni-migrator/scripts/migrate.py --explore orders --looker-repo ./looker --topic-prefix model
```

This writes `{model}__orders.topic.yaml`, using the Looker model file that holds the explore. Pass a name instead of `model` to choose the prefix yourself.

### Explores that extend other explores

`--explore child` resolves `extends:` across every `.lkml` file under `--looker-repo` and writes `extends: [parent]` together with the full merged result, because a child topic's `joins:` replaces the parent's in Omni. Migrate the parent explores as well, so the topics named in `extends:` exist. A parent with `extension: required` becomes a `template: true` topic. A parent that is extended but can also be used on its own stays a normal topic; pass `--template` when you know it is only a base:

```bash
python skills/looker-to-omni-migrator/scripts/migrate.py --explore orders_base --looker-repo ./looker --template
```

Each explore becomes one topic. To split a large explore, copy the generated topic and trim the joins in each copy. Ask Claude to help: *"Split the order_items topic into a sales topic and an inventory topic."*

## Access grants and model-level settings

```bash
python skills/looker-to-omni-migrator/scripts/migrate.py --parse-access-grants --looker-repo ./looker
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
│   └── order_items.topic.yaml
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

- If view A extends view B, migrate both: A's file only refers to B in `extends:`.
- If explore A extends explore B, migrate both: A's topic carries the merged joins and settings and refers to B's topic in `extends:`.
- Start with small explores to prove the approach.
- Use `--unverified` for drafts; re-run without it once reviewed.
- Keep the CSV under version control. It is your record of what moved in each phase.
