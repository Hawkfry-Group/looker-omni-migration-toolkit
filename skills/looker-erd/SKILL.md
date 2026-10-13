---
name: looker-erd
description: "Generate an interactive ERD of a Looker project from its LookML: every explore, the views it joins, join counts, extends inheritance and a risk score per explore. Use when assessing or scoping a Looker to Omni migration (or any Looker migration), mapping explore and view relationships, auditing LookML complexity, or deciding which explores to migrate first."
---

# Looker ERD Generator

Generate an interactive D3.js network diagram of explores and the views they join, by parsing LookML and filling in [./template.html](./template.html).

Always build the output from the template. Do not write a new HTML page from scratch; the template holds the layout, legend, filters and interactions, and every run should look the same.

## Arguments

- **Model path** (required): directory containing the LookML (e.g. `./looker/mymodel`)
- `--output` (optional): where to save the HTML. Default `./looker-erd.html`
- `--title` (optional): diagram title. Default `Looker ERD`
- `--filter` (optional): only include explores whose name or file matches this text (e.g. `p0` for a P0/P1/P2 naming scheme)

## Usage

```
/looker-erd ./looker/mymodel
/looker-erd ./looker/mymodel --title "P0 Explores" --filter p0
/looker-erd ./project/explores --output ./docs/erd.html
```

## Step 1: Find the explores

Search every `*.lkml` file under the model path for `explore:` blocks. Explores can be defined in `.explore.lkml`, `.model.lkml` and even `.view.lkml` files, so do not filter by file type.

- `explore: name {` defines an explore
- `explore: +name {` is a **refinement**, and can sit in any file. Merge its joins into the base explore `name`; do not create a second node
- Skip explores that only exist inside a `.dashboard.lookml` file

## Step 2: Parse each explore

For each explore, read the block from `explore: name {` to its matching closing brace and extract the fields below.

When matching braces, ignore any inside `#` comments, quoted strings, SQL blocks (`sql_on:`, `sql:` and similar run to `;;`) and Liquid tags (`{% ... %}`, `{{ ... }}`). Counting those breaks the match on most real projects.

| Field | How to get it |
|-------|---------------|
| `name` | the explore name |
| `file` | path of the file it is defined in, relative to the model path |
| `description` | the explore-level `description:`, else its `label:`, else `""`. Ignore `description:` inside nested blocks such as `query:` or `join:` |
| `baseView` | `from:` if set, else `view_name:` if set, else (for an explore that `extends` a parent) the parent's `baseView`, else the explore name |
| `lines` | lines in the explore block plus the lines of every refinement of it, wherever those refinements live |
| `joins` | number of `join:` blocks declared in the explore and its refinements (aliased joins of the same view each count). Do **not** count joins inherited through `extends`; the extends arrow already shows that relationship |
| `views` | **unique** underlying view names: `baseView` plus each join's view. A join's view is its `from:` value when present, else the join name |
| `viewCounts` | `[{name, count}]` for each view in `views`: how many times it appears (base counts as 1, each join adds 1) |
| `extends` | names listed in `extends: [...]`, if any. Used only to build `EXTENDS_JSON`; it does not need to appear in `EXPLORES_JSON` |

## Step 3: Score risk

Use the highest tier where **both** thresholds are exceeded:

| Risk | Lines | Joins |
|------|-------|-------|
| `critical` | > 500 | > 50 |
| `high` | > 200 | > 20 |
| `medium` | > 100 | > 10 |
| `low` | otherwise | |

## Step 4: Build the data

Explore ids **must** be prefixed with `explore:`. Views are keyed by their bare name, and explores often share a name with their base view (`explore: orders` on `view: orders`); without the prefix the two nodes collide and the graph breaks.

`EXPLORES_JSON` is an array:

```json
[
  {
    "id": "explore:orders",
    "name": "orders",
    "file": "explores/orders.explore.lkml",
    "description": "Order level reporting",
    "baseView": "fact_orders",
    "lines": 214,
    "joins": 23,
    "risk": "high",
    "views": ["fact_orders", "dim_customer", "dim_date"],
    "viewCounts": [
      {"name": "fact_orders", "count": 1},
      {"name": "dim_customer", "count": 1},
      {"name": "dim_date", "count": 3}
    ]
  }
]
```

`EXTENDS_JSON` is an array with one entry per `extends:` relationship. `source` is the **parent**, `target` is the **child**, so the arrow points from parent into the explore that extends it. Only include links where both explores are in `EXPLORES_JSON` (drop any whose parent was filtered out).

```json
[{"source": "explore:orders_base", "target": "explore:orders"}]
```

## Step 5: Fill the template

Read `template.html` and replace every placeholder:

| Placeholder | Value |
|-------------|-------|
| `{{TITLE}}` | title (appears twice) |
| `{{SUBTITLE}}` | e.g. `12 explores from ./looker/mymodel` |
| `{{EXPLORES_JSON}}` | the explores array |
| `{{EXTENDS_JSON}}` | the extends array (`[]` if none) |
| `{{EXPLORE_COUNT}}` | number of explores |
| `{{VIEW_COUNT}}` | number of unique views across all explores |
| `{{JOIN_COUNT}}` | sum of `joins` |
| `{{CRITICAL_COUNT}}` | explores with risk `critical` |
| `{{HIGH_COUNT}}` | explores with risk `high` |

When writing the JSON into the page, replace every `<` with `\u003c`. LookML descriptions can contain HTML or Liquid, and a literal `</script>` inside one would end the script block early. HTML-escape `{{TITLE}}` and `{{SUBTITLE}}`.

After writing the file, confirm no `{{` placeholders remain, then tell the user where it was saved and summarise the risk breakdown and the most widely shared views.

## View classification

The template colours views by name prefix: `fact_*` (fact), `dim_*` (dimension), `agg_*` or `dt_*` (aggregate/derived), anything else (other). If the project uses a different convention, say so in the summary rather than renaming views.
