"""
Context Loader - CSV Scope Loading and Rule File Management

This module loads migration scope from a CSV file and reads the rule/syntax
documentation files that guide the conversion. Handles filtering by owner,
phase, and view.

## CSV Format

```csv
phase,view,field,type,omni_view,omni_field,owner,status,notes
P0,users,id,dimension,users,id,Team A,ready,Primary key
P0,users,first_name,dimension,users,first_name,Team A,ready,
P0,users,created,dimension_group,users,created,Team A,ready,
P0,users,count,measure,users,count,Team A,ready,
P1,order_items,total_sale_price,measure,order_items,total_sale_price,Data Team,pending,
```

**Columns:**
- **phase** - Migration phase (`P0` or `0`, `P1` or `1`, ...)
- **view** - Looker view name (e.g., users)
- **field** - Field name in Looker (e.g., first_name). For a dimension_group
  use the group name (`created`), not a timeframe (`created_date`)
- **type** - `dimension`, `dimension_group` or `measure`
- **omni_view / omni_field** - Tracking only; the migrator keeps Looker names
- **owner** - Team/person responsible; matched exactly against `--owner`
- **status** - Free text (ready, pending, blocked)
- **notes** - Optional notes

A legacy column layout (`view,field,field_type,phase,owner,status`) is also
accepted; both are normalized to the same row structure.

## Filtering

Rows are kept when `owner` matches exactly, `phase` matches (`P0` and `0`
are equivalent) and, for single-view migration, `view` matches.

```python
rows = load_and_filter_csv(
    csv_path='migration/views.csv',
    owner='Team A',
    phase=0,
    view='users'
)
```

## Rule Files

`load_rules` reads `rules/migration_rules.md`, `rules/model_details_omni.md`
and `rules/task_prompt_template.md`. They are loaded into the context so an
LLM-driven workflow can use them; the Python conversion itself does not
parse them.

## Normalized Row

```python
{
    'view': 'users',
    'field': 'id',
    'field_type': 'dimension',
    'phase': 'P0',
    'owner': 'Team A',
    'status': 'ready',
    'omni_view': 'users',
    'omni_field': 'id',
    'notes': 'Primary key'
}
```

## Usage

```python
from lib.context_loader import load_migration_context, get_fields_for_view

context = load_migration_context(
    skill_path='/path/to/skill',
    csv_path='migration/views.csv',
    owner='Team A',
    phase=0,
    view='users'
)
fields = get_fields_for_view(context.csv_rows, 'users')
```

## Errors

- `FileNotFoundError` - CSV or a rule file is missing
- `ValueError` - no CSV rows match the owner/phase/view filter

The loader does not validate field names; the orchestrator checks every
scoped field against the LookML and reports the ones it cannot find.
"""

import csv
import os
from pathlib import Path
from typing import Dict, List, Optional


class MigrationContext:
    """Container for migration context"""

    def __init__(self):
        self.migration_rules = ""
        self.omni_syntax = ""
        self.execution_format = ""
        self.csv_rows = []
        self.views_in_scope = []

    def summary(self) -> str:
        """Return summary of loaded context"""
        return f"""
Migration Context Loaded:
- CSV rows in scope: {len(self.csv_rows)}
- Unique views: {len(self.views_in_scope)}
- Migration rules: {len(self.migration_rules)} chars
- Omni syntax ref: {len(self.omni_syntax)} chars
"""


def load_rules(skill_path: str) -> Dict[str, str]:
    """
    Load all rule files from skill directory

    Args:
        skill_path: Path to skill directory

    Returns:
        Dict with keys: migration_rules, omni_syntax, execution_format
    """
    rules_path = Path(skill_path) / "rules"

    rules = {}

    # Load migration_rules.md
    migration_rules_file = rules_path / "migration_rules.md"
    if migration_rules_file.exists():
        with open(migration_rules_file, 'r') as f:
            rules['migration_rules'] = f.read()
    else:
        raise FileNotFoundError(f"migration_rules.md not found at {migration_rules_file}")

    # Load model_details_omni.md
    omni_syntax_file = rules_path / "model_details_omni.md"
    if omni_syntax_file.exists():
        with open(omni_syntax_file, 'r') as f:
            rules['omni_syntax'] = f.read()
    else:
        raise FileNotFoundError(f"model_details_omni.md not found at {omni_syntax_file}")

    # Load task_prompt_template.md
    template_file = rules_path / "task_prompt_template.md"
    if template_file.exists():
        with open(template_file, 'r') as f:
            rules['execution_format'] = f.read()
    else:
        raise FileNotFoundError(f"task_prompt_template.md not found at {template_file}")

    return rules


def load_and_filter_csv(
    csv_path: str,
    owner: str,
    phase: int,
    view: Optional[str] = None
) -> List[Dict[str, str]]:
    """
    Load CSV and filter by scope

    Supports CSV formats:
    - phase,view,field,type,omni_view,omni_field,owner,status,notes (actual format)
    - view,field,field_type,phase,owner,status (old format)

    Args:
        csv_path: Path to CSV file
        owner: Owner filter
        phase: Phase filter
        view: Optional view filter (for single mode)

    Returns:
        List of CSV rows as dicts (normalized to: view, field, field_type, phase, owner)
    """
    if not os.path.exists(csv_path):
        raise FileNotFoundError(f"CSV file not found: {csv_path}")

    rows = []
    with open(csv_path, 'r') as f:
        reader = csv.DictReader(f)
        for row in reader:
            # Normalize column names
            normalized_row = {
                'view': row.get('view'),
                'field': row.get('field'),
                'field_type': row.get('type') or row.get('field_type'),  # 'type' in new CSV
                'phase': row.get('phase'),
                'owner': row.get('owner'),
                'status': row.get('status'),
                'omni_view': row.get('omni_view', ''),
                'omni_field': row.get('omni_field', ''),
                'notes': row.get('notes', '')
            }

            # Filter by owner
            if normalized_row.get('owner') != owner:
                continue

            # Filter by phase (handle both "P0" and "0" formats)
            row_phase = str(normalized_row.get('phase', '')).upper().replace('P', '')
            if row_phase != str(phase):
                continue

            # Filter by view if specified (single mode)
            if view and normalized_row.get('view') != view:
                continue

            rows.append(normalized_row)

    return rows


def extract_unique_views(csv_rows: List[Dict[str, str]]) -> List[str]:
    """
    Extract unique view names from CSV rows

    Args:
        csv_rows: List of CSV row dicts

    Returns:
        List of unique view names
    """
    views = set()
    for row in csv_rows:
        view_name = row.get('view')
        if view_name:
            views.add(view_name)

    return sorted(list(views))


def load_migration_context(
    skill_path: str,
    csv_path: str,
    owner: str,
    phase: int,
    view: Optional[str] = None
) -> MigrationContext:
    """
    Load complete migration context

    Args:
        skill_path: Path to skill directory
        csv_path: Path to CSV file
        owner: Owner filter
        phase: Phase filter
        view: Optional view filter

    Returns:
        MigrationContext object

    Raises:
        ValueError: If no CSV rows match scope
    """
    context = MigrationContext()

    # Load rules
    rules = load_rules(skill_path)
    context.migration_rules = rules['migration_rules']
    context.omni_syntax = rules['omni_syntax']
    context.execution_format = rules['execution_format']

    # Load and filter CSV
    csv_rows = load_and_filter_csv(csv_path, owner, phase, view)

    if not csv_rows:
        raise ValueError(f"No CSV rows found for owner={owner}, phase={phase}, view={view}")

    context.csv_rows = csv_rows
    context.views_in_scope = extract_unique_views(csv_rows)

    return context


def get_fields_for_view(csv_rows: List[Dict[str, str]], view_name: str) -> List[Dict[str, str]]:
    """
    Get all CSV rows (fields) for a specific view

    Args:
        csv_rows: All CSV rows
        view_name: View to filter by

    Returns:
        List of CSV rows for this view
    """
    return [row for row in csv_rows if row.get('view') == view_name]
