---
name: materialization-review
description: Analyze LookML joins for warehouse materialization opportunities. Use when migrating Looker to Omni, optimizing query performance, or reducing explore complexity.
---

# Materialization Review

Analyze LookML join patterns and recommend which joins should be pre-materialized in the warehouse vs. kept as lazy joins.

## Overview

This skill analyzes LookML explores to identify:
- Joins that ALWAYS fire (`required_joins`) - strong materialization candidates
- Derived tables used across multiple explores - rebuild on every query
- Complex multi-field join conditions - simplify for semantic layer
- UNNEST patterns and simple FK joins - keep as lazy joins

## Arguments

The user should provide:
- **Model path**: Path to the LookML directory containing explore/model files (e.g., `./looker/mymodel`)
- **Output path** (optional): Where to save the report (defaults to `./materialization-review.md`)

## Decision Framework

**Materialize when:**
| Pattern | Why |
|---------|-----|
| `required_joins` in explore | Always fires, no lazy benefit |
| Derived table in multiple explores | DT rebuilds per query anyway |
| Complex multi-field join conditions | Simplifies semantic layer design |
| Window function calculations | Expensive to compute at query time |

**Keep as lazy join when:**
| Pattern | Why |
|---------|-----|
| UNNEST array expansion | BigQuery UNNEST is efficient; only fires when fields selected |
| Simple 1:1 FK joins | Standard pattern; no benefit to denormalize |
| Rarely-used dimensions | Lazy = no cost when not used |
| Parameter-driven conditional joins | Conditional logic should stay in semantic layer |

## How to Analyze

### Step 1: Find Required Joins
Search all explore files for `required_joins:`. These are the strongest materialization candidates.

```lookml
explore: order {
  required_joins: [fact_order_metrics, dim_customer]  # ALWAYS fire
}
```

### Step 2: Identify Derived Table Usage
Find all derived table views (`sql_table_name` with subquery or `derived_table:` block) and count how many explores use each.

```lookml
view: dt_example {
  derived_table: {
    sql: SELECT ... ;;  # Rebuilds on every query
  }
}
```

### Step 3: Analyze Join Complexity
Look for multi-field join conditions:

```lookml
join: dim_account_plan {
  sql_on: ${dt_billing.plan_code} = ${dim_account_plan.plan_code}
    AND ${dt_billing.account_id} = ${dim_account_plan.account_id}
    AND ${dt_billing.region} = ${dim_account_plan.region}
    AND ${dim_account_plan.latest} = 1 ;;
}
```

Joins with 3+ conditions or embedded filters (`latest = 1`, `status = 'active'`) are candidates.

### Step 4: Find UNNEST Patterns
Search for UNNEST in sql_on. These should generally stay as lazy joins:

```lookml
join: fact_order_items {
  sql: LEFT JOIN UNNEST(${fact_orders.items}) as fact_order_items ;;
}
```

### Step 5: Count Join Fanout
For each explore, count total joins and categorize by relationship type:
- `many_to_many` or cross joins = high risk
- `one_to_many` = potential fan-out
- `many_to_one` = safe dimension joins

### Step 6: Identify Repeated Patterns
Find views that are joined to 10+ explores with the same join logic. These are consolidation candidates.

## Output Format

Generate a markdown report with:

### 1. Summary Statistics
- Total explores analyzed
- Total joins found
- Count by relationship type
- High-complexity explores (>50 joins)

### 2. Priority 1: Always-Fire Candidates
Tables for joins that use `required_joins` or are derived tables used in 5+ explores.

| Candidate Table | Problem | Grain | Replaces |
|-----------------|---------|-------|----------|
| `agg_account_snapshot` | required_joins in 50+ explores | per customer-account | 4-table join chain |

### 3. Priority 2: Complex Join Candidates
Tables for multi-field joins or join chains.

### 4. Priority 3: Conditional Candidates
Patterns that need usage validation before materializing (dim_date cross joins, engagement consolidation).

### 5. DO NOT Materialize
Explicit list of patterns to keep as lazy joins (UNNEST, simple FK, rarely-used).

### 6. Omni Topic Mapping
Suggested topic structure based on materialized tables.

## Example Usage

```
/materialization-review ./looker/mymodel
/materialization-review ./looker/mymodel --output ./docs/warehouse-plan.md
```

## LookML Patterns to Search

```bash
# Find required_joins
grep -r "required_joins:" ./explores/

# Find derived tables
grep -r "derived_table:" ./views/

# Find complex joins (3+ conditions)
grep -A 10 "sql_on:" ./explores/*.lkml | grep -E "(AND|OR).*\$\{"

# Find UNNEST joins
grep -r "UNNEST" ./explores/

# Find cross joins
grep -r "relationship: many_to_many" ./explores/

# Count joins per explore
grep -c "join:" ./explores/*.lkml | sort -t: -k2 -nr
```

## Key Metrics to Report

1. **Query Performance Risk**: Lopsided joins overloading BigQuery workers
2. **Always-Fire Overhead**: `required_joins` firing on every query
3. **DT Rebuild Cost**: Derived tables recomputing per query
4. **Complexity Score**: Joins × relationship risk × condition complexity
