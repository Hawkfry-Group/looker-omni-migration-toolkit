---
name: sankey-diagram
description: Generate an interactive Sankey of Looker dashboard to explore to view lineage, with P0 to P4 priority filters, LookML vs UGC dashboard toggles and an explore ranking. Includes a Looker System Activity SQL query for tile counts and finding UI-built dashboards. Use when scoping or prioritising a Looker to Omni migration, sizing migration effort, or tracing which dashboards depend on which explores.
---

# Sankey Diagram Generator

Generate interactive ECharts Sankey diagrams showing three-column data flow relationships, typically Dashboard → Explore → View dependencies.

## Usage

```
/sankey-diagram
/sankey-diagram ./mapping.md
/sankey-diagram --title "P0 Dashboard Flow" --output ./docs/sankey.html
/sankey-diagram ./mapping.md --brand "Acme Data Team"
```

## What You Need

Provide one of the following:
1. A path to a mapping document (markdown or JSON)
2. Ask Claude to analyze LookML files to build the mapping
3. Provide inline data

## Inputs: What LookML Can and Cannot Tell You

**This skill is not self-contained.** Unlike `/looker-erd` and
`/materialization-review`, which read everything they need from the repo, the
Sankey needs inputs that do not exist in LookML. Be explicit with the user about
which half of the picture they are looking at.

| Input | Derivable from LookML? | Where it comes from |
|-------|------------------------|---------------------|
| Dashboard to explore links | Yes | `.dashboard.lookml` files |
| Explore to view links | Yes | `join:` blocks in any `.lkml` file that defines explores |
| UGC dashboards | **No** | Built in the Looker UI, never in the repo. Use [`system_activity_tile_count.sql`](./system_activity_tile_count.sql) in this skill's folder |
| Priority (P0 to P4) | **No** | The client's own prioritisation |
| Tile counts | **No** | Looker System Activity |
| Complexity overrides | **No** | Human judgement, via `dashboard-complexity.csv` |

**Warn the user when coverage is partial.** UGC dashboards routinely outnumber
code-defined ones, and on a mature estate the repo can easily hold only half the
dashboards in use. A team that reads a repo-only Sankey as complete will
under-scope the migration.

Note also that System Activity reports dashboard **display names**, while the
repo uses LookML file names. These often differ, so reconciling the two lists is
a manual matching step.

## Output

Generates a standalone HTML file with:
- **Sankey visualization** - Three-column flow (Dashboards → Explores → Views)
- **Priority filter** - Filter by P0-P4 priority levels
- **Type toggle** - Filter by LookML vs UGC dashboards
- **Search** - Filter by dashboard, explore, or view name
- **Statistics cards** - Dashboard/explore/view counts
- **Ranking table** - Explores ranked by dashboard usage

## Instructions

When generating a Sankey diagram:

### Step 1: Gather Data

Parse source data to extract:

**Dashboard-to-Explore mappings:**
- From dashboard LookML files (`.dashboard.lookml`)
- From existing mapping documentation
- From user-provided data

For LookML dashboards:
- Use the dashboard's `title:` as its name, not the file or dashboard id. It is what the business recognises, and what System Activity reports, so the two lists can be reconciled
- Collect every `explore:` referenced by an element (tile) **and** by a dashboard filter. A filter bound to an explore is a dependency to migrate too

**Explore-to-View mappings:**
- Find explores the same way `/looker-erd` does: in any `.lkml` file, with refinements (`explore: +name`) merged into the base explore
- Extract `join:` blocks to find views. When a join uses `from:`, list the underlying view, not the alias
- Put the base view first, then joins in declaration order. The chart shows at most 4 views per explore, taken in this order

### Step 2: Structure the Data

Build two JavaScript data structures:

```javascript
// Dashboard data
const dashboardData = [
    {
        priority: "P0",              // P0, P1, P2, P3, P4
        dashboard: "Dashboard Name", // Source node
        explores: ["explore1"],      // Intermediate nodes
        isLookML: true               // true = LookML, false = UGC
    }
];

// Explore-to-View mappings
const exploreViewMap = {
    "explore1": ["view1", "view2", "view3"],
    "explore2": ["view2", "view4"]
};
```

### Step 3: Generate HTML

Use the template at `./template.html`:

1. Replace `{{TITLE}}` with the diagram title
2. Replace `{{SUBTITLE}}` with subtitle text
   Replace `{{BRAND}}` with the `--brand` value (HTML-escaped), or an empty string if none was given. The header label hides itself when empty
3. Replace `{{DASHBOARD_DATA_JSON}}` with dashboard data array
4. Replace `{{EXPLORE_VIEW_MAP_JSON}}` with explore-view mappings

When writing the JSON into the page, replace every `<` with `\u003c` so a name containing `</script>` cannot end the script block early. HTML-escape `{{TITLE}}`, `{{SUBTITLE}}` and `{{BRAND}}`. Confirm no `{{` placeholders remain.

### Step 4: Save

Save the generated HTML to the output path (default: `./sankey-diagram.html`) and tell the user where it is.

## Template Variables

| Variable | Description |
|----------|-------------|
| `{{TITLE}}` | Page title |
| `{{SUBTITLE}}` | Subtitle text |
| `{{BRAND}}` | Optional team or company name shown top right. Empty string hides it |
| `{{DASHBOARD_DATA_JSON}}` | Dashboard data as JSON array |
| `{{EXPLORE_VIEW_MAP_JSON}}` | Explore→view map as JSON object |

## Color Scheme

| Node Type | Color |
|-----------|-------|
| LookML Dashboard | Green (#27ae60) |
| UGC Dashboard | Purple (#9b59b6) |
| Explore | Blue (#3498db) |
| View | Teal (#1abc9c) |

| Priority | Color |
|----------|-------|
| P0 | Red (#e74c3c) |
| P1 | Orange (#e67e22) |
| P2 | Yellow (#f1c40f) |
| P3 | Blue (#3498db) |
| P4 | Gray (#95a5a6) |

## Complexity Overrides (CSV)

The generated diagrams support manual complexity tagging via a CSV file.

**Default behavior:** Complexity is calculated from explore count:
- **H** (High): 5+ explores
- **M** (Medium): 3-4 explores
- **L** (Low): 2 explores
- **XS** (Extra Small): 1 explore

**To override:** Create a `dashboard-complexity.csv` file in the same directory as the generated HTML:

```csv
dashboard,complexity,notes
Executive Overview,H,Complex merge queries
Weekly Summary,M,Simpler than expected
```

| Column | Required | Description |
|--------|----------|-------------|
| `dashboard` | Yes | Exact dashboard name (must match) |
| `complexity` | Yes | H, M, L, or XS |
| `notes` | No | Optional notes |

Only include dashboards you want to override. Others use calculated values. Overridden values show with a bullet (•) indicator.

## Notes

- Maximum 4 views shown per explore to prevent visual clutter (the first 4 in `exploreViewMap` order). The ranking table's view count still includes all of them
- Node names are prefixed internally: `D:`, `E:`, `V:` for uniqueness
- Empty searches show all results
- Priority filter defaults to P0
- Complexity CSV is optional and loaded at runtime

## Template

The template file is at [./template.html](./template.html).
