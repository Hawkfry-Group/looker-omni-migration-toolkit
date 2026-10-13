"""
Explore Analyzer - Parse Looker Explores and Suggest Topic Divisions

This module analyzes Looker explores to understand their structure and suggest
how to divide them into Omni topics.

## Purpose

Large Looker explores can carry dozens of joins, while Omni topics are easier
to use and to curate for AI when they stay focused. This analyzer:
1. Finds every explore in the repository (any `.lkml` file, typically
   `*.model.lkml` or `*.explore.lkml`)
2. Parses explore definitions, joins (including `from:` aliases) and
   row-level restrictions (`access_filter`, `sql_always_where`, `always_filter`)
3. Analyzes complexity based on join count
4. Suggests topic divisions
5. Generates analysis reports for LLM/human review

## Looker Explore Structure

```lkml
explore: order_items {
  label: "Orders"
  description: "Order line items with shopper and product context"

  required_access_grants: [can_view_financials]

  join: users {
    type: left_outer
    relationship: many_to_one
    sql_on: ${order_items.user_id} = ${users.id} ;;
  }

  join: products {
    type: left_outer
    relationship: many_to_one
    sql_on: ${order_items.product_id} = ${products.id} ;;
  }
}
```

## Topic Division Strategy

`suggest_topic_divisions` uses join count only:

| Joins | Suggestion |
|-------|------------|
| 0 | Single-view topic |
| 1-4 | Single topic with all migrated joins |
| 5-19 | Split by relationship type: a "core" topic with many_to_one/one_to_one joins and an "extended" topic with one_to_many/many_to_many joins |
| 20+ | Flagged `_NEEDS_MANUAL_DIVISION`; the first 10 migrated joins are listed as a starting point |

## Usage

```python
from lib.explore_analyzer import (
    find_explores_using_views,
    suggest_topic_divisions,
    generate_explore_report
)

explores = find_explores_using_views(
    looker_repo='/path/to/looker',
    view_names={'order_items', 'users'}
)

suggestions = suggest_topic_divisions(
    explores=explores,
    migrated_views={'order_items', 'users'}
)

report = generate_explore_report(
    explores=explores,
    migrated_views={'order_items'},
    suggestions=suggestions
)
```

## Key Features

- **Access Grant Detection**: Flags explores with `required_access_grants`
- **Row-level restrictions**: Captures `access_filter`, `sql_always_where`
  and `always_filter` so explore migration can carry or flag them
- **Join aliases**: `join: buyer { from: users }` keeps both the alias
  (`buyer`) and the source view (`users`)
- **Relationship Analysis**: Categorizes joins by relationship type
- **Markdown Reporting**: Generates detailed analysis reports
"""

import re

import lkml
from pathlib import Path
from typing import Dict, List, Set, Optional, Tuple
from dataclasses import dataclass, field


@dataclass
class JoinDefinition:
    """Represents a join from a Looker explore"""
    view: str  # Name the join is referenced by in the explore (the alias)
    type: Optional[str]  # left_outer, inner, etc.
    relationship: Optional[str]  # one_to_one, many_to_one, etc.
    sql_on: Optional[str]  # Join condition
    fields: Optional[List[str]]  # Specific fields included
    from_view: Optional[str] = None  # Source view when `from:` is used
    foreign_key: Optional[str] = None
    sql: Optional[str] = None  # Raw `sql:` join (instead of sql_on)
    required_access_grants: List[str] = field(default_factory=list)
    required_joins: List[str] = field(default_factory=list)

    @property
    def source_view(self) -> str:
        """Underlying Looker view (the `from:` view, or the join name)"""
        return self.from_view or self.view

    @property
    def is_aliased(self) -> bool:
        return bool(self.from_view) and self.from_view != self.view


@dataclass
class ExploreInfo:
    """Information about a Looker explore"""
    name: str
    file_path: str
    base_view: str
    joins: List[JoinDefinition]
    label: Optional[str]
    description: Optional[str]
    group_label: Optional[str]
    access_grants: Optional[List[str]] = None  # required_access_grants
    access_filters: List[Dict] = field(default_factory=list)  # [{field, user_attribute}]
    sql_always_where: Optional[str] = None
    always_filter: Optional[Dict] = None  # raw lkml always_filter block
    conditionally_filter: Optional[Dict] = None
    fields: Optional[List[str]] = None  # explore-level field restriction
    extends: List[str] = field(default_factory=list)
    sql_always_having: Optional[str] = None
    always_join: List[str] = field(default_factory=list)
    hidden: bool = False


@dataclass
class TopicSuggestion:
    """Suggested Omni topic based on explore analysis"""
    topic_name: str
    base_view: str
    suggested_joins: List[str]
    reason: str
    source_explore: str


def find_explore_files(looker_repo: str) -> List[str]:
    """
    Find every LookML file that declares at least one explore

    Explores usually live in `*.model.lkml` or `*.explore.lkml` files, but
    LookML allows them in any `.lkml` file, so all of them are checked.

    Args:
        looker_repo: Path to Looker repository

    Returns:
        Sorted list of file paths
    """
    repo_path = Path(looker_repo)
    explore_files = []

    for file_path in sorted(repo_path.rglob("*.lkml")):
        try:
            text = file_path.read_text()
        except (OSError, UnicodeDecodeError):
            continue
        if _EXPLORE_RE.search(text):
            explore_files.append(str(file_path))

    return explore_files


_EXPLORE_RE = re.compile(r'\bexplore\s*:')


def _as_list(value) -> List:
    """Normalize lkml values that may be a scalar, a list, or a dict"""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _flatten_all(value) -> List[str]:
    """Flatten lkml `*__all` list-of-lists representations"""
    result = []
    for item in _as_list(value):
        if isinstance(item, list):
            result.extend(str(v) for v in item)
        else:
            result.append(str(item))
    return result


def parse_explore_file(file_path: str) -> List[ExploreInfo]:
    """
    Parse every explore declared in a LookML file

    Args:
        file_path: Path to explore file

    Returns:
        List of ExploreInfo objects
    """
    with open(file_path, 'r') as f:
        parsed = lkml.load(f)

    explores = []

    # lkml.load returns {"explores": [explore_dict, ...]}
    for explore_dict in parsed.get('explores', []):
        explore_name = explore_dict.get('name', '')

        # Extract joins
        joins = []
        for join_dict in explore_dict.get('joins', []):
            join = JoinDefinition(
                view=join_dict.get('name', ''),
                type=join_dict.get('type'),
                relationship=join_dict.get('relationship'),
                sql_on=join_dict.get('sql_on'),
                fields=join_dict.get('fields'),
                from_view=join_dict.get('from'),
                foreign_key=join_dict.get('foreign_key'),
                sql=join_dict.get('sql'),
                required_access_grants=[str(g) for g in _as_list(join_dict.get('required_access_grants'))],
                required_joins=[str(j) for j in _as_list(join_dict.get('required_joins'))]
            )
            joins.append(join)

        # Extract access grants
        access_grants = explore_dict.get('required_access_grants')
        if access_grants:
            # Can be a string or list
            if isinstance(access_grants, str):
                access_grants = [access_grants]

        explore = ExploreInfo(
            name=explore_name,
            file_path=file_path,
            base_view=explore_dict.get('view_name') or explore_dict.get('from') or explore_name,
            joins=joins,
            label=explore_dict.get('label'),
            description=explore_dict.get('description'),
            group_label=explore_dict.get('group_label'),
            access_grants=access_grants,
            access_filters=[
                af for af in _as_list(explore_dict.get('access_filters') or explore_dict.get('access_filter'))
                if isinstance(af, dict)
            ],
            sql_always_where=explore_dict.get('sql_always_where'),
            always_filter=explore_dict.get('always_filter'),
            conditionally_filter=explore_dict.get('conditionally_filter'),
            fields=explore_dict.get('fields'),
            extends=_flatten_all(explore_dict.get('extends__all') or explore_dict.get('extends')),
            sql_always_having=explore_dict.get('sql_always_having'),
            always_join=[str(j) for j in _as_list(explore_dict.get('always_join'))],
            hidden=str(explore_dict.get('hidden', 'no')).lower() == 'yes'
        )

        explores.append(explore)

    return explores


def find_explores_using_views(
    looker_repo: str,
    view_names: Set[str]
) -> List[ExploreInfo]:
    """
    Find all explores that use any of the specified views

    Args:
        looker_repo: Path to Looker repository
        view_names: Set of view names to search for

    Returns:
        List of ExploreInfo objects that reference these views
    """
    explore_files = find_explore_files(looker_repo)
    matching_explores = []

    for file_path in explore_files:
        try:
            explores = parse_explore_file(file_path)

            for explore in explores:
                # Check if base view is in our set
                if explore.base_view in view_names:
                    matching_explores.append(explore)
                    continue

                # Check if any joined views are in our set
                for join in explore.joins:
                    if join.view in view_names or join.source_view in view_names:
                        matching_explores.append(explore)
                        break

        except Exception as e:
            print(f"Warning: Could not parse {file_path}: {e}")
            continue

    return matching_explores


def suggest_topic_divisions(
    explores: List[ExploreInfo],
    migrated_views: Set[str]
) -> List[TopicSuggestion]:
    """
    Suggest how to divide explores into Omni topics

    Strategy (by join count):
    - 0 joins: single-view topic
    - 1-4 joins: single topic with all migrated joins
    - 5-19 joins: "core" (many_to_one/one_to_one) and "extended"
      (one_to_many/many_to_many) topics
    - 20+ joins: flagged for manual division

    Args:
        explores: List of explores to analyze
        migrated_views: Set of views that have been migrated

    Returns:
        List of topic suggestions
    """
    suggestions = []

    for explore in explores:
        join_count = len(explore.joins)

        # Only suggest joins for views that are actually migrated
        migrated_joins = [j.view for j in explore.joins if j.source_view in migrated_views]

        if join_count == 0:
            # No joins - single view topic
            suggestions.append(TopicSuggestion(
                topic_name=f"{explore.name}_migration",
                base_view=explore.base_view,
                suggested_joins=[],
                reason="No joins - simple topic",
                source_explore=explore.name
            ))

        elif join_count < 5:
            # Small explore - single topic with all joins
            suggestions.append(TopicSuggestion(
                topic_name=f"{explore.name}_migration",
                base_view=explore.base_view,
                suggested_joins=migrated_joins,
                reason=f"Small explore ({join_count} joins) - single topic",
                source_explore=explore.name
            ))

        elif join_count < 20:
            # Medium explore - suggest dividing by relationship type
            core_joins = [j.view for j in explore.joins
                         if j.relationship in ['one_to_one', 'many_to_one']
                         and j.source_view in migrated_views]

            fact_joins = [j.view for j in explore.joins
                         if j.relationship in ['one_to_many', 'many_to_many']
                         and j.source_view in migrated_views]

            # Core topic with dimensional joins
            if core_joins:
                suggestions.append(TopicSuggestion(
                    topic_name=f"{explore.name}_core_migration",
                    base_view=explore.base_view,
                    suggested_joins=core_joins,
                    reason=f"Medium explore ({join_count} joins) - core dimensional joins",
                    source_explore=explore.name
                ))

            # Extended topic with fact joins
            if fact_joins:
                suggestions.append(TopicSuggestion(
                    topic_name=f"{explore.name}_extended_migration",
                    base_view=explore.base_view,
                    suggested_joins=fact_joins,
                    reason=f"Medium explore ({join_count} joins) - extended fact joins",
                    source_explore=explore.name
                ))

        else:
            # Large explore - flag for manual division
            suggestions.append(TopicSuggestion(
                topic_name=f"{explore.name}_NEEDS_MANUAL_DIVISION",
                base_view=explore.base_view,
                suggested_joins=migrated_joins[:10],  # First 10 only
                reason=f"Large explore ({join_count} joins) - requires manual topic design",
                source_explore=explore.name
            ))

    return suggestions


def generate_explore_report(
    explores: List[ExploreInfo],
    migrated_views: Set[str],
    suggestions: List[TopicSuggestion]
) -> str:
    """
    Generate markdown report of explore analysis

    Args:
        explores: Analyzed explores
        migrated_views: Set of migrated view names
        suggestions: Topic suggestions

    Returns:
        Markdown report string
    """
    report = []
    report.append("# Explore Analysis Report\n")
    report.append(f"**Migrated Views:** {len(migrated_views)}\n")
    report.append(f"**Explores Found:** {len(explores)}\n")
    report.append(f"**Topic Suggestions:** {len(suggestions)}\n")
    report.append("\n---\n")

    # Summary table
    report.append("\n## Explores Using Migrated Views\n\n")
    report.append("Status counts only the views passed to this analysis (the view just migrated).\n\n")
    report.append("| Explore | Base View | Joins | Migrated Joins | Status |\n")
    report.append("|---------|-----------|-------|----------------|--------|\n")

    for explore in explores:
        migrated_join_count = sum(1 for j in explore.joins if j.source_view in migrated_views)
        base_ready = explore.base_view in migrated_views
        if base_ready and migrated_join_count == len(explore.joins):
            status = "✅ Ready"
        else:
            status = (f"⚠️ base view {'migrated' if base_ready else 'not migrated'}, "
                      f"{migrated_join_count}/{len(explore.joins)} joins migrated")

        report.append(f"| {explore.name} | {explore.base_view} | {len(explore.joins)} | {migrated_join_count} | {status} |\n")

    # Access grants detection
    explores_with_grants = [e for e in explores if e.access_grants]
    if explores_with_grants:
        report.append("\n---\n\n## ⚠️ Access Grants Detected\n\n")
        report.append("**These explores require access grants - will need Omni row-level security setup:**\n\n")

        for explore in explores_with_grants:
            report.append(f"### {explore.name}\n\n")
            report.append(f"**Required Access Grants:** {', '.join(explore.access_grants)}\n")
            report.append(f"**File:** `{explore.file_path}`\n\n")
            report.append("**Action Required:** Map to Omni row-level security policies or set up equivalent access controls\n\n")

    # Row-level restrictions
    restricted = [e for e in explores if e.access_filters or e.sql_always_where or e.always_filter]
    if restricted:
        report.append("\n---\n\n## ⚠️ Row-Level Restrictions Detected\n\n")
        report.append("**MANUAL ACTION REQUIRED:** these explores limit which rows users can see. "
                      "Run `--explore NAME` to migrate them; check the explore migration report for what was carried over.\n\n")
        for explore in restricted:
            report.append(f"### {explore.name}\n\n")
            for af in explore.access_filters:
                report.append(f"- access_filter: field `{af.get('field')}`, user attribute `{af.get('user_attribute')}`\n")
            if explore.sql_always_where:
                report.append(f"- sql_always_where: `{explore.sql_always_where}`\n")
            if explore.always_filter:
                report.append(f"- always_filter: `{explore.always_filter}`\n")
            report.append("\n")

    # Detailed suggestions
    report.append("\n---\n\n## Topic Suggestions\n\n")

    for suggestion in suggestions:
        report.append(f"### {suggestion.topic_name}\n\n")
        report.append(f"**Source Explore:** `{suggestion.source_explore}`\n")
        report.append(f"**Base View:** `{suggestion.base_view}`\n")
        report.append(f"**Reason:** {suggestion.reason}\n\n")

        if suggestion.suggested_joins:
            report.append("**Suggested Joins:**\n")
            for join in suggestion.suggested_joins:
                report.append(f"- `{join}`\n")
        else:
            report.append("**Suggested Joins:** None (single view topic)\n")

        report.append("\n")

    return ''.join(report)
