# Looker to Omni Migration Toolkit

A Claude Code toolkit for migrating from Looker to Omni. Provides skills and agents for analyzing LookML, generating ERD diagrams, and validating Omni syntax.

## Repository Setup

This toolkit uses symlinks to access your Looker and Omni repositories:

```bash
# Create symlinks (run from toolkit root)
ln -s /path/to/your/looker-repo ./looker
ln -s /path/to/your/omni-repo ./omni
```

### Expected Structure After Setup

```
looker-omni-migration-toolkit/
├── looker -> /path/to/your/looker-repo    # symlink
├── omni -> /path/to/your/omni-repo        # symlink
├── CLAUDE.md
├── .claude/
│   ├── skills/
│   │   ├── looker-erd/
│   │   ├── materialization-review/
│   │   └── sankey-diagram/
│   └── agents/
│       ├── lookml-docs-expert.md
│       └── omni-docs-lookup.md
├── queries/
│   └── system_activity_tile_count.sql
└── ...
```

## Available Tools

### Skills

#### `/looker-erd [path]`
Generate interactive ERD diagrams from LookML explore files.

```bash
/looker-erd ./looker/model_name
/looker-erd ./looker/mymodel --title "P0 Explores"
```

**Output:** Interactive HTML visualization showing:
- Explores (color-coded by risk level)
- Views (color-coded by type: fact/dim/derived)
- Join relationships and counts
- Explore inheritance (extends)
- Click-to-highlight relationships

#### `/materialization-review [path]`
Analyze LookML joins and recommend which should be pre-materialized in the warehouse.

```bash
/materialization-review ./looker/model_name
/materialization-review ./looker/mymodel --output ./docs/warehouse-plan.md
```

**Output:** Markdown report covering:
- Priority 1 always-fire candidates (`required_joins`, widely shared derived tables)
- Priority 2 complex multi-field join candidates
- Priority 3 conditional candidates needing usage validation
- Explicit do-not-materialize list (UNNEST, simple FK, rarely-used dimensions)
- Suggested Omni topic mapping

#### `/sankey-diagram [mapping]`
Generate an interactive Dashboard → Explore → View lineage diagram.

```bash
/sankey-diagram ./dashboard-explore-mapping.md
/sankey-diagram --title "P0 Dashboard Flow" --output ./docs/sankey.html
```

**Output:** Standalone HTML visualization showing:
- Three-column ECharts Sankey flow
- Priority (P0 to P4) and LookML/UGC filters
- Dashboard, explore, and view counts
- Explores ranked by dashboard usage
- Optional complexity overrides via `dashboard-complexity.csv`

### Agents

#### `omni-docs-lookup`
Validates Omni code syntax and looks up Omni documentation. Use when:
- Writing Omni model definitions
- Checking derived table syntax
- Validating generated Omni code
- Understanding Omni features

#### `lookml-docs-expert`
Validates LookML syntax and looks up Looker documentation. Use when:
- Analyzing existing LookML code
- Understanding Liquid templating
- Checking explore/view patterns
- Debugging LookML errors

## Queries

### `queries/system_activity_tile_count.sql`
Run against Looker System Activity (admin access required) to get what the repo
cannot give you: dashboard tile counts, owners, legacy-viewer flags, and the
existence of UGC dashboards built in the Looker UI.

Use it to size the migration (roughly 10 minutes per tile across the whole
estate, never per dashboard) and to check how much of the estate your Sankey is
actually showing.

Read the header comment before trusting the output. As written it returns only
dashboards that trip a Looker performance warning, and caps at 100 rows.

## Migration Workflow

### Phase 1: Analysis
1. Create symlink to Looker repo
2. Run `/looker-erd ./looker/model` to visualize explores
3. Identify high-risk explores (red = critical, orange = high)
4. Map content dependencies to explores
5. Run `/sankey-diagram` to visualize dashboard, explore, and view lineage
6. Run `queries/system_activity_tile_count.sql` to find UGC dashboards and tile counts the repo cannot show

### Phase 2: Planning
1. Group content by shared explores
2. Prioritize simple explores first (green = low risk)
3. Run `/materialization-review ./looker/model` to decide what to pre-materialize
4. Create phased migration plan

### Phase 3: Migration
1. Create symlink to Omni repo
2. Migrate views first (shared dims like `dim_date`, `dim_customer`)
3. Migrate explores in dependency order
4. Use `omni-docs-lookup` agent to validate syntax

### Phase 4: Validation
1. Compare ERDs between source and target
2. Validate field counts and relationships
3. Test content against migrated explores

## Risk Assessment

The ERD generator classifies explores by complexity:

| Risk | Criteria | Action |
|------|----------|--------|
| 🔴 Critical | >500 lines + >50 joins | Migrate last, thorough testing |
| 🟠 High | >200 lines + >20 joins | Careful review, phased approach |
| 🟡 Medium | >100 lines + >10 joins | Standard migration |
| 🟢 Low | Simple explores | Migrate first, validate approach |

## Tips

- **Shared views first**: Migrate `dim_date`, `dim_customer`, etc. before explores
- **Extends together**: If explore A extends B, migrate B first
- **Check join counts**: Views joined multiple times (e.g., `dim_sales_rep: 11x`) indicate aliased joins
- **Liquid templating**: Complex conditional joins need careful translation to Omni

## Support

Report issues or ask questions via [GitHub Issues](https://github.com/Hawkfry-Group/looker-omni-migration-toolkit/issues).
