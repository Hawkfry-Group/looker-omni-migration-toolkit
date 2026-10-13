# Output Files

## Where files go

```
<--omni-repo>/                    # default migration/staging/views (explore mode: .../topics)
├── {view}.view.yaml              # table-based view
├── {view}.query.view             # derived table
├── {explore}.topic.yaml          # explore mode
└── {explore}.relationships       # explore mode, only if at least one join converts
<sibling>/reports/
├── {view}.migration.md
├── {view}_explore_analysis.md    # --explore-analysis
├── {explore}_explore.md
└── access_grants.md              # --parse-access-grants
```

View and topic files are named after the Looker view or explore, with no suffix. Each file is written atomically (a temporary file renamed into place). A run that fails partway can leave files it already wrote; it never leaves a half-written file. `--dry-run` writes nothing.

## 1. View files

### Table-based view

Real output of `--view users --phase 0 --owner "Team A"` on the sample (also in `examples/simple/expected/users.view.yaml`):

```yaml
schema: thelook_ecommerce
folder: thelook_ecommerce/curated
label: Users
description: Customers of the thelook e-commerce sample store. One row per registered customer.
tags: [users, fact, mig_phase_0, verified]
table_name: users
ai_context: Customers of the thelook e-commerce sample store. One row per registered customer.

dimensions:
  id:
    label: Id
    description: Id dimension
    tags: [id, mig_phase_0, verified]
    primary_key: true
    ai_context: Primary key for this view. Numeric identifier or value.

  email:
    label: Email
    description: Email dimension
    tags: [in_omni_in_looker_not_csv]
    hidden: true
    ai_context: Text field for filtering and display.
    required_access_grants: [can_view_pii]

  # age, country, traffic_source ...

  created:
    label: Created
    description: Created timestamp field
    tags: [date, temporal, mig_phase_0, verified]
    sql: created_at
    ai_context: Created timestamp field. Date/time dimension; Omni generates the date timeframes.

measures:
  count:
    aggregate_type: count
    label: Customer Count
    description: Customer Count measure
    tags: [metric, mig_phase_0, verified]
    ai_context: Use for counting records or distinct values.
```

Notes:

- `sql:` is omitted where a dimension reads its own column (`id`, `email`).
- `email` is out of scope, so it is hidden and tagged only `in_omni_in_looker_not_csv`. Its access grant is still copied.
- A three-part `sql_table_name` adds a `catalog:` key.
- Generated `label`, `description` and `ai_context` are a starting point; improve them during review.

### Derived-table view

`{view}.query.view` with either `sql:` (SQL derived table) or `query:` (native derived table). See [../docs/QUERY_VIEWS.md](../docs/QUERY_VIEWS.md).

## 2. Migration report (`{view}.migration.md`)

Real report for the run above (location shortened):

```markdown
# Migration Report: users

## PR Summary
Migrated users from Looker to Omni as users. Converted 3 dimensions and 1 measures with 13 total changes. Migration phase: 0.

## View File Decision
- **File Type**: Standard table-based view (.view.yaml)
- **Location**: .../migration/staging/views/users.view.yaml
- **Rationale**: Lift-and-shift migration of the in-scope fields

## Metadata for Standardization
- id: Added ai_context for AI assistant
- country: Added ai_context for AI assistant
- created: Added ai_context for AI assistant
- count: Added ai_context for AI assistant

## Changes Made
- **SQL Transformations**: ...
- **Other**: ..., country (dimension): Not migrated: map_layer_name, created (dimension): Converted dimension_group to a single date dimension, created (dimension): Looker timeframes [raw, date, month, year] not emitted (Omni generates timeframes), ...

## Redundancy Candidates
(none)

## Not Converted
- **email** - Reason: Not in CSV scope - added with hidden: true
- **age** - Reason: Not in CSV scope - added with hidden: true
- **traffic_source** - Reason: Not in CSV scope - added with hidden: true

## Notes / Risks
(none)

## ⚠️ Advanced Features Detected
**These features require special handling:**

### Field-Level Access Grants
- **Status**: CARRIED - MANUAL ACTION REQUIRED
- **Details**: required_access_grants copied onto these fields:
  dimension:email:
    - `can_view_pii`
      - User Attribute: `department`
      - Allowed Values: `data_team`
- **Action Required**: Define each grant in the Omni model file (access_grants:) before deploying.
```

| Section | Contents |
|---------|----------|
| PR Summary | One line for a pull request |
| Merge/Enrich Summary | Only when merging: source file and counts for Updated (in scope), Merged under a warehouse column name, Added from Looker, Hidden, Kept visible (earlier run), plus notes such as replaced `sql`/`table_name` |
| View File Decision | File type, location, rationale |
| Metadata for Standardization | Generated `ai_context` and synonyms |
| Changes Made | Grouped: Canonical Mappings, SQL Transformations, Metadata Additions, Format Mappings, Other. Dropped `html`, `link`, `drill_fields`, `map_layer_name` and unemitted timeframes are under Other. |
| Redundancy Candidates | Hidden dimensions, listed as possible legacy fields (a simple heuristic, not duplicate detection) |
| Not Converted | Hidden out-of-scope fields and fields that were not written, with reasons |
| Notes / Risks | `HIGH RISK` (field not written, missing primary key), `MEDIUM RISK` (format not migrated and similar), `INFO` (dependencies) |
| Advanced Features Detected | Access grants, access filters, refinements, `extends`, parameters, derived tables, persistence; each with a status such as `CARRIED - MANUAL ACTION REQUIRED`, `NOT MIGRATED` or `CONVERTED` |

## 3. Topic and relationships files

From `--explore order_items` on the sample:

```yaml
# order_items.topic.yaml
base_view: order_items
label: Order Items
description: Sales by item, with the customer who placed each order
fields: [all_views.*, "-tag:table_key"]
joins:
  users: {}
ai_context: |-
  This topic provides access to Order Items data.
  ...
access_filters:
- field: users.country
  user_attribute: allowed_country
```

```yaml
# order_items.relationships
# Relationships migrated from Looker explore: order_items
# Base view: order_items. Joins: 1.
- join_from_view: order_items
  join_to_view: users
  join_type: always_left
  on_sql: ${order_items.user_id} = ${users.id}
  relationship_type: many_to_one
  reversible: false
```

- Topic keys the migrator can write: `base_view`, `label`, `description`, `fields`, `joins`, `ai_context`, `access_filters`, `always_where_sql`, `always_having_sql`, `required_access_grants`, `hidden`, `group_label`.
- `joins` is nested by dependency: a join whose `sql_on` references another joined view sits under it.
- Joins that were not migrated are listed in a comment at the top of the relationships file (`# Not migrated (see users_explore.md): orders`) and in the report.
- If a run produces no joins, an older `{explore}.relationships` is removed.

### Explore report (`{explore}_explore.md`)

Sections: Summary (explore, source file, base view, joins migrated), MANUAL ACTION REQUIRED, Carried Into the Topic, Join Definitions (each join with type, relationship, SQL, and whether it was migrated), Generated Files, Notes.

## 4. Explore analysis report (`{view}_explore_analysis.md`)

Written with `--explore-analysis`. Real excerpt:

```markdown
## Explores Using Migrated Views

| Explore | Base View | Joins | Migrated Joins | Status |
|---------|-----------|-------|----------------|--------|
| order_items | order_items | 1 | 0 | ⚠️ base view migrated, 0/1 joins migrated |

## ⚠️ Row-Level Restrictions Detected

**MANUAL ACTION REQUIRED:** these explores limit which rows users can see. ...

- access_filter: field `users.country`, user attribute `allowed_country`

## Topic Suggestions

### order_items_migration
**Reason:** Small explore (1 joins) - single topic
```

Status counts only the view passed to this run; "Ready" requires the base view and every joined view to be among them. Suggestions: under 5 joins, one topic; 5 or more but fewer than 20, a core and an extended topic (empty groups are left out); 20 or more, manual design.

## 5. Access grants report (`access_grants.md`)

The source files, then a table of every grant with its user attribute, allowed values and the Omni action (define it under `access_grants:` in the Omni model), followed by setup steps.

## YAML formatting

- Blank line between fields; tags written inline (`tags: [mig_phase_0, verified]`).
- When a merge puts a Looker field under an existing field of another name, that field also gets the tag `from_looker__<looker field>` (for example `from_looker__customer_email` on `email`). Keep it: later runs use it to find the Looker field and apply its access grants.
- SQL for derived tables is formatted with `sqlparse` and written as a `|-` block.
- When merging, unknown top-level keys in the existing view are kept.
- Every view is validated against the tool's Pydantic models before it is written. This is not a check against a live Omni instance.

## See also

- [COMMAND_REFERENCE.md](COMMAND_REFERENCE.md)
- [CONVERSION_PATTERNS.md](CONVERSION_PATTERNS.md)
