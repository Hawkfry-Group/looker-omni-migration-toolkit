# Looker to Omni Migration Toolkit

A Claude Code toolkit for migrating from Looker to Omni. Built by [Hawkfry](https://hawkfry.com).

## Prerequisites

- [Claude Code](https://docs.claude.com/en/docs/claude-code/overview) installed
- Access to your Looker repository (LookML files)
- Access to your Omni repository (target)

## Quick Start

1. **Clone this toolkit**
   ```bash
   git clone https://github.com/Hawkfry-Group/looker-omni-migration-toolkit.git
   cd looker-omni-migration-toolkit
   ```

2. **Create symlinks to your repositories**
   ```bash
   ln -s /path/to/your/looker-repo ./looker
   ln -s /path/to/your/omni-repo ./omni
   ```

3. **Start Claude Code**
   ```bash
   claude
   ```

4. **Run a skill**
   ```
   /looker-erd ./looker/your_model
   /materialization-review ./looker/your_model
   /sankey-diagram ./dashboard-explore-mapping.md
   ```

## What's Included

### Skills

| Skill | Description |
|-------|-------------|
| `/looker-erd` | Generate interactive ERD diagrams from LookML explores |
| `/materialization-review` | Analyze LookML joins and recommend warehouse pre-materialization vs lazy joins |
| `/sankey-diagram` | Generate Dashboard → Explore → View lineage flow diagrams |

### Agents

| Agent | Description |
|-------|-------------|
| `omni-docs-lookup` | Validate Omni syntax and lookup documentation |
| `lookml-docs-expert` | Validate LookML syntax and lookup documentation |

### Queries

- `system_activity_tile_count.sql`: Looker System Activity query returning dashboard tile counts, owners, and legacy flags. Needed for tile-based sizing and for finding UGC dashboards that never appear in the repo. Read the header comment first; it filters to performance-flagged dashboards and caps at 100 rows.

### Templates

- `migration-advisory.md`: report template for migration assessments

### Examples

- `sample-erd.html`: sample ERD output

## Optional: Project Tracking and Agent Memory

A Looker migration runs for weeks and produces a lot of decisions: which explores were merged, which joins were materialized, which dashboards were retired rather than rebuilt. The toolkit does not store any of that for you. We recommend connecting one of the following so Claude can read and record migration state across sessions:

- **Linear** (what we use): add the [Linear MCP server](https://linear.app/docs/mcp) and track each explore or dashboard group as an issue. Claude can then pick up where the last session left off, link findings from `/materialization-review` to issues, and update status as content moves.
- **Your own tracker or memory system**: any MCP server that gives Claude somewhere durable to read and write works the same way (Jira, GitHub Issues, Notion, or a plain `MIGRATION_LOG.md` committed alongside your Omni repo).

The bundled agents deliberately only get read and web tools. If you want them to write to your tracker, add those tools to the `tools:` line in `.claude/agents/*.md`.

## ERD Features

The generated ERD diagrams include:

- **Interactive visualization**: D3.js force-directed graph
- **Risk assessment**: colour-coded by complexity (critical/high/medium/low)
- **Click to highlight**: click any node to see its relationships
- **Join counts**: how many times each view is joined
- **File names**: source file for each explore
- **Extends relationships**: arrows from parent explore to the explore that extends it
- **Filters**: filter by explore or view type

## Materialization Review

The generated report covers:

- **Always-fire candidates**: joins declared in `required_joins` that cost you on every query
- **Derived table reuse**: DTs shared across explores that rebuild per query
- **Complex join conditions**: multi-field `sql_on` blocks worth flattening before the semantic layer
- **Keep-as-lazy list**: UNNEST expansion, simple FK joins, rarely-used dimensions
- **Omni topic mapping**: suggested topic structure based on the materialized tables

## Sankey Features

The generated lineage diagrams include:

- **Three-column flow**: Dashboards → Explores → Views, rendered with ECharts
- **Priority filter**: P0 through P4, defaulting to P0
- **Type toggle**: LookML dashboards against user-generated content
- **Search**: filter by dashboard, explore, or view name
- **Ranking table**: explores ranked by dashboard usage
- **Complexity overrides**: optional `dashboard-complexity.csv` overrides the calculated H/M/L/XS rating

> **Coverage warning:** the Sankey can only auto-map dashboards defined in LookML. Dashboards built in the Looker UI (UGC) are not in the repo and will be missing. Run `queries/system_activity_tile_count.sql` to find them before treating the diagram as a complete picture of the estate.

## Migration Workflow

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│  Analysis   │ ──▶ │  Planning   │ ──▶ │  Migration  │ ──▶ │ Validation  │
│             │     │             │     │             │     │             │
│ - ERD       │     │ - Group by  │     │ - Views     │     │ - Compare   │
│ - Risk      │     │   content   │     │ - Explores  │     │ - Test      │
│ - Depends   │     │ - Prioritize│     │ - Content   │     │ - UAT       │
└─────────────┘     └─────────────┘     └─────────────┘     └─────────────┘
```

## License

[Apache License 2.0](LICENSE). Copyright 2026 Hawkfry Group Ltd; see [NOTICE](NOTICE).

## Contributing

Issues and PRs welcome at [GitHub](https://github.com/Hawkfry-Group/looker-omni-migration-toolkit).
