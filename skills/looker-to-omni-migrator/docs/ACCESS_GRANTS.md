# Access Grants and Access Filters

The migrator copies Looker access declarations into the Omni files it writes, and reports everything you still have to set up. What it cannot create is the Omni side those declarations depend on: the grant definitions in the Omni model file and the user attributes in your Omni instance. Those are always MANUAL ACTION REQUIRED.

## Looker access control

**Grant definitions** declare who has access, keyed on a user attribute:

```lkml
access_grant: can_view_pii {
  user_attribute: department
  allowed_values: ["data_team"]
}

access_grant: can_view_finance {
  user_attribute: department
  allowed_values: ["data_team", "finance"]
}
```

**Grant usage** attaches a grant to an explore, a view or a field:

```lkml
explore: order_items {
  required_access_grants: [can_view_finance]
}

view: users {
  dimension: email {
    type: string
    sql: ${TABLE}.email ;;
    required_access_grants: [can_view_pii]
  }
}
```

**Row restrictions** limit what a user sees:

```lkml
explore: order_items {
  access_filter: {
    field: users.country
    user_attribute: allowed_country
  }
  sql_always_where: ${order_items.status} != 'Cancelled' ;;
}
```

## What the migrator does with each

| Looker | Omni output | Still manual |
|--------|-------------|--------------|
| Field `required_access_grants` | Copied onto the final field (`required_access_grants: [can_view_pii]`): hidden and auto-included fields, the existing field a Looker field was merged into, an existing Omni field kept by a merge even when the Looker SQL could not be converted (that case is also flagged MANUAL ACTION REQUIRED), the field a Looker field was merged into by an earlier run (found by its `from_looker__<field>` tag, whatever the field's current scope), and an existing field named after the column the Looker field reads. The report classifies each Looker field with grants from the final output: present and protected (CARRIED), present without the grant (MANUAL ACTION REQUIRED), or not in the output | Define the grant in the Omni model file |
| Explore `required_access_grants` | Copied onto the topic | Define the grant |
| Explore `access_filter` | Topic `access_filters` (field + user attribute) | Create the user attribute in Omni |
| Explore `sql_always_where` / `sql_always_having` | Topic `always_where_sql` / `always_having_sql` | Verify the rows match |
| Explore `always_filter`, `conditionally_filter`, `fields:` restrictions, join-level grants | Not carried | Recreate by hand |
| View-level `required_access_grants` or `access_filter` | Reported | Recreate by hand |
| Liquid `_user_attributes` in SQL | Field not converted; reported | Rewrite by hand |

Every one of these appears in the console summary and in the report. Restrictions are never dropped silently.

Example from `--view users` on the bundled sample (`users.migration.md`):

```markdown
### Field-Level Access Grants
- **Status**: CARRIED - MANUAL ACTION REQUIRED
- **Details**: required_access_grants copied onto these fields:
  dimension:email:
    - `can_view_pii`
      - User Attribute: `department`
      - Allowed Values: `data_team`
- **Action Required**: Define each grant in the Omni model file (access_grants:) before deploying.
```

## Listing all grants

```bash
python scripts/migrate.py --parse-access-grants --looker-repo ./looker
```

Grant definitions are read from every `*.lkml` file under `--looker-repo`, including model files. A grant name defined twice stops the run with exit code 1. The command writes `access_grants.md` to the reports directory. Output on the sample:

```
Found 1 file(s) with access grants:
  - examples/simple/thelook/settings/access_grants.lkml

============================================================
PARSED 2 ACCESS GRANTS
============================================================

📋 can_view_finance
   User Attribute: department
   Allowed Values: ["data_team", "finance"]

📋 can_view_pii
   User Attribute: department
   Allowed Values: ["data_team"]
```

View and explore migrations use the same definitions to add the user attribute and allowed values to their reports.

## Finishing access in Omni

1. Run `--parse-access-grants` to get the full list.
2. Create the matching user attributes in Omni.
3. Define each grant in the Omni model file (`access_grants:`).
4. Recreate anything reported as not carried (for example `always_filter`).
5. Test with users who should and should not see the data.

See the Omni documentation for current syntax: <https://docs.omni.co>.
