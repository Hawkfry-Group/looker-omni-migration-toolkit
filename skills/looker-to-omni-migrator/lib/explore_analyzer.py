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
   and resolves explore inheritance (`extends:`, see `resolve_explore_extends`)
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
    fields: Optional[List[str]]  # Join-level field restriction; `[]` exposes no fields, None is no restriction
    from_view: Optional[str] = None  # Source view when `from:` is used
    foreign_key: Optional[str] = None
    sql: Optional[str] = None  # Raw `sql:` join (instead of sql_on)
    required_access_grants: List[str] = field(default_factory=list)
    required_joins: List[str] = field(default_factory=list)
    view_label: Optional[str] = None  # label of the joined view in this explore

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
    base_view: str  # the real view the explore is based on (`from:`, else `view_name:`, else the name)
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
    hidden_explicit: bool = False  # `hidden:` is written on the explore (yes or no)
    view_name: Optional[str] = None  # raw `view_name:`
    from_view: Optional[str] = None  # raw `from:`
    extension_required: bool = False  # `extension: required`
    view_label: Optional[str] = None  # label of the base view in this explore
    # Filled by resolve_explore_extends: item -> explore that supplied it.
    # Items are attribute names (e.g. "always_filter") and "join:<name>".
    sources: Dict[str, str] = field(default_factory=dict)

    @property
    def base_alias(self) -> str:
        """
        The name the explore's own fields are referenced by: `view_name:`
        when set, else the explore name. It differs from `base_view` when
        `from:` names another view.
        """
        return self.view_name or self.name

    @property
    def base_is_aliased(self) -> bool:
        return self.base_alias != self.base_view

    def view_names(self) -> Dict[str, str]:
        """{name a view goes by in this explore: the real view}, base first, then each join"""
        names = {self.base_alias: self.base_view}
        for join in self.joins:
            names.setdefault(join.view, join.source_view)
        return names


def base_view_of(name: str, view_name: Optional[str], from_view: Optional[str]) -> str:
    """The real view of an explore: `from:` wins over `view_name:`, which wins over the explore name"""
    return from_view or view_name or name


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
                required_joins=[str(j) for j in _as_list(join_dict.get('required_joins'))],
                view_label=join_dict.get('view_label')
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
            base_view=base_view_of(explore_name, explore_dict.get('view_name'), explore_dict.get('from')),
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
            hidden=str(explore_dict.get('hidden', 'no')).lower() == 'yes',
            hidden_explicit='hidden' in explore_dict,
            view_name=explore_dict.get('view_name'),
            from_view=explore_dict.get('from'),
            extension_required=str(explore_dict.get('extension', '')).lower() == 'required',
            view_label=explore_dict.get('view_label')
        )

        explores.append(explore)

    return explores


def load_explores(looker_repo: str) -> Tuple[Dict[str, List[ExploreInfo]], Dict[str, List[str]]]:
    """
    Parse every explore under the repository

    Returns:
        (definitions, refinements): explore name -> its definitions (more
        than one means the name is declared twice), and explore name -> files
        holding a refinement of it (`explore: +name`)
    """
    definitions: Dict[str, List[ExploreInfo]] = {}
    refinements: Dict[str, List[str]] = {}
    for file_path in find_explore_files(looker_repo):
        try:
            explores = parse_explore_file(file_path)
        except Exception as e:
            print(f"      ⚠️  Could not parse {file_path}: {e}")
            continue
        for explore in explores:
            if explore.name.startswith('+'):
                refinements.setdefault(explore.name[1:], []).append(file_path)
            else:
                definitions.setdefault(explore.name, []).append(explore)
    return definitions, refinements


_INCLUDE_RE = re.compile(r'^\s*include\s*:\s*"([^"]+)"', re.MULTILINE)
MODEL_SUFFIX = '.model.lkml'


def _include_regex(pattern: str) -> 're.Pattern':
    """A LookML include pattern as a regex over repo-relative paths (`*` stays in a folder, `**` crosses folders)"""
    out = []
    i = 0
    while i < len(pattern):
        if pattern.startswith('**/', i):
            out.append('(?:.*/)?')
            i += 3
        elif pattern.startswith('**', i):
            out.append('.*')
            i += 2
        elif pattern[i] == '*':
            out.append('[^/]*')
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile(''.join(out) + r'(?:\.lkml)?')


def find_models_including(looker_repo: str, explore_file: str) -> List[str]:
    """
    Names of the Looker models that hold an explore file: the
    `*.model.lkml` file that is the file itself, or whose `include:`
    patterns reach it (directly or through an included file)

    A pattern starting with `/` is read from the repository root, any other
    from the folder of the including file. Includes of other projects
    (`//project/...`) are ignored.
    """
    root = Path(looker_repo).resolve()
    target = Path(explore_file).resolve()
    files = {path.resolve().relative_to(root).as_posix(): path.resolve() for path in root.rglob('*.lkml')}

    def included(path: Path) -> Set[str]:
        try:
            text = path.read_text()
        except (OSError, UnicodeDecodeError):
            return set()
        found: Set[str] = set()
        folder = path.parent.relative_to(root).as_posix()
        for pattern in _INCLUDE_RE.findall(text):
            if pattern.startswith('//'):
                continue
            if pattern.startswith('/'):
                pattern = pattern[1:]
            elif folder != '.':
                pattern = f"{folder}/{pattern}"
            regex = _include_regex(pattern)
            found.update(name for name in files if regex.fullmatch(name))
        return found

    models = []
    for name in sorted(files):
        if not name.endswith(MODEL_SUFFIX):
            continue
        seen = {name}
        queue = [name]
        while queue:
            current = queue.pop()
            for other in included(files[current]) - seen:
                seen.add(other)
                queue.append(other)
        if any(files[item] == target for item in seen):
            models.append(Path(name).name[:-len(MODEL_SUFFIX)])
    return models


def find_extending_explores(explore_name: str, definitions: Dict[str, List[ExploreInfo]]) -> List[str]:
    """Names of the explores that list `explore_name` in their `extends:`"""
    return sorted({
        explore.name
        for explores in definitions.values()
        for explore in explores
        if explore_name in explore.extends
    })


# ----------------------------------------------------------------------
# Explore inheritance (extends)
# ----------------------------------------------------------------------

# Explore attributes that follow "the child's value overrides the parent's".
# `hidden` is handled apart (it needs to know whether the child wrote it).
_INHERITED_ATTRIBUTES = (
    'label', 'description', 'group_label', 'access_grants', 'access_filters', 'sql_always_where',
    'sql_always_having', 'always_filter', 'conditionally_filter', 'fields', 'always_join',
    'view_name', 'from_view', 'view_label',
)
_JOIN_ATTRIBUTES = ('type', 'relationship', 'fields', 'from_view', 'required_access_grants', 'required_joins',
                    'view_label')
# Alternative ways to write the join condition: taken together from one side
_JOIN_CONDITION_ATTRIBUTES = ('sql_on', 'foreign_key', 'sql')


@dataclass
class ExtendsResolution:
    """Result of resolving an explore's `extends:` chain"""
    explore: ExploreInfo  # merged explore (the explore itself when `problem` is set)
    ancestors: List[str] = field(default_factory=list)  # every explore merged in, nearest last
    problem: Optional[str] = None  # why the chain could not be resolved
    # (join, explore that overrides it, explore it was inherited from, partial)
    overridden_joins: List[Tuple[str, str, str, bool]] = field(default_factory=list)
    # (attribute, overriding explore, overridden explore) for values set on both sides
    overridden_settings: List[Tuple[str, str, str]] = field(default_factory=list)


class _Unresolved(Exception):
    pass


def _is_set(value) -> bool:
    return value is not None and value != [] and value != {}


def _merge_join(base: JoinDefinition, over: JoinDefinition) -> Tuple[JoinDefinition, bool]:
    """
    Merge a redeclared join parameter by parameter (the redeclaration wins)

    Returns the merged join and whether it kept any parameter of `base`
    (a partial override).
    """
    values = {}
    partial = False
    for attr in _JOIN_ATTRIBUTES:
        # `fields: []` is a value (the join exposes no fields), not an unset parameter
        if _is_set(getattr(over, attr)) or (attr == 'fields' and over.fields is not None):
            values[attr] = getattr(over, attr)
        else:
            values[attr] = getattr(base, attr)
            partial = partial or _is_set(getattr(base, attr))
    condition_source = over if any(_is_set(getattr(over, a)) for a in _JOIN_CONDITION_ATTRIBUTES) else base
    partial = partial or condition_source is base
    for attr in _JOIN_CONDITION_ATTRIBUTES:
        values[attr] = getattr(condition_source, attr)
    return JoinDefinition(view=over.view, **values), partial


def _merge_explores(base: ExploreInfo, over: ExploreInfo, resolution: ExtendsResolution) -> ExploreInfo:
    """
    Looker `extends` semantics: parameters of `over` replace those of `base`,
    joins are additive, and a join declared on both sides is merged with the
    parameters of `over` winning.
    """
    sources: Dict[str, str] = {}

    def source_of(explore: ExploreInfo, item: str) -> str:
        return explore.sources.get(item, explore.name)

    values = {}
    for attr in _INHERITED_ATTRIBUTES:
        winner = over if _is_set(getattr(over, attr)) else base
        if winner is over and _is_set(getattr(base, attr)) and getattr(base, attr) != getattr(over, attr):
            resolution.overridden_settings.append((attr, source_of(over, attr), source_of(base, attr)))
        values[attr] = getattr(winner, attr)
        if _is_set(values[attr]):
            sources[attr] = source_of(winner, attr)

    hidden_from = over if over.hidden_explicit else base
    if hidden_from.hidden_explicit:
        sources['hidden'] = source_of(hidden_from, 'hidden')

    joins: Dict[str, JoinDefinition] = {join.view: join for join in base.joins}
    for join in base.joins:
        sources[f"join:{join.view}"] = source_of(base, f"join:{join.view}")
    for join in over.joins:
        key = f"join:{join.view}"
        if join.view in joins:
            joins[join.view], partial = _merge_join(joins[join.view], join)
            resolution.overridden_joins.append((join.view, source_of(over, key), source_of(base, key), partial))
        else:
            joins[join.view] = join
        sources[key] = source_of(over, key)

    return ExploreInfo(
        name=over.name,
        file_path=over.file_path,
        base_view=base_view_of(over.name, values['view_name'], values['from_view']),
        joins=list(joins.values()),
        hidden=hidden_from.hidden,
        hidden_explicit=hidden_from.hidden_explicit,
        extends=list(over.extends),
        extension_required=over.extension_required,  # `extension` is not inherited
        sources=sources,
        **values
    )


def resolve_explore_extends(
    explore: ExploreInfo,
    definitions: Dict[str, List[ExploreInfo]]
) -> ExtendsResolution:
    """
    Merge an explore with everything it inherits through `extends:`

    Parents are merged in list order (a later parent wins over an earlier
    one), each parent after its own chain was resolved, and the explore
    itself is applied last. `definitions` comes from `load_explores`.

    When a parent is missing, declared more than once, or the chain is
    circular, `problem` says why and `explore` is returned unchanged.
    """
    resolution = ExtendsResolution(explore=explore)

    def resolve(current: ExploreInfo, path: Tuple[str, ...]) -> ExploreInfo:
        merged: Optional[ExploreInfo] = None
        for parent_name in current.extends:
            if parent_name in path or parent_name == current.name:
                raise _Unresolved("circular extends: " + ' → '.join(path + (current.name, parent_name)))
            candidates = definitions.get(parent_name, [])
            if not candidates:
                raise _Unresolved(f"explore `{parent_name}` was not found under --looker-repo")
            if len(candidates) > 1:
                raise _Unresolved(
                    f"explore `{parent_name}` is defined more than once ("
                    + ', '.join(sorted({c.file_path for c in candidates})) + ")"
                )
            parent = resolve(candidates[0], path + (current.name,))
            if parent_name not in resolution.ancestors:
                resolution.ancestors.append(parent_name)
            merged = parent if merged is None else _merge_explores(merged, parent, resolution)
        return current if merged is None else _merge_explores(merged, current, resolution)

    try:
        resolution.explore = resolve(explore, ())
    except _Unresolved as e:
        return ExtendsResolution(explore=explore, problem=str(e))
    return resolution


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
