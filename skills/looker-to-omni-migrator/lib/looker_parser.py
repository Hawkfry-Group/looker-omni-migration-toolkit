"""
Looker Parser - Find and Parse LookML View Files

This module locates Looker view files and parses their contents into structured Python objects.

## Purpose

Reads Looker `.view.lkml` files and extracts:
- View metadata (name, label, description, sql_table_name)
- Dimensions (regular fields)
- Dimension Groups (date/time fields with multiple timeframes)
- Measures (aggregations)
- Parameters (user-configurable filters)
- Derived Tables (SQL or native)
- Advanced features (extends, access_filters, user_attributes)

## Looker View Structure

```lkml
view: users {
  sql_table_name: thelook_ecommerce.users ;;

  label: "Users"
  description: "One row per registered shopper"

  dimension: id {
    primary_key: yes
    type: number
    sql: ${TABLE}.id ;;
  }

  dimension: first_name {
    type: string
    label: "First Name"
    sql: ${TABLE}.first_name ;;
  }

  measure: count {
    type: count
    label: "Count of Users"
  }
}
```

## Parser Features

### Basic Field Extraction
- Dimensions with type, sql, label, description
- Measures with aggregate_type and SQL expressions
- Dimension groups (dates) with timeframes

### Advanced Feature Detection
- **SQL Derived Tables**: Views based on custom SQL queries
- **Templated Views**: Views that extend other views
- **Access Filters**: Row-level security filters
- **User Attributes**: Liquid template variables for personalization
- **Primary Keys**: Identifies unique identifiers

### Error Handling
- Handles malformed LookML gracefully
- Continues processing if view not found
- Logs warnings for parse errors

## Usage

```python
from lib.looker_parser import find_and_parse_view

# Find and parse a view
looker_view = find_and_parse_view(
    looker_repo='/path/to/looker',
    view_name='users'
)

# Access parsed data
print(f"View: {looker_view.view_name}")
print(f"Table: {looker_view.sql_table_name}")
print(f"Dimensions: {list(looker_view.dimensions.keys())}")
print(f"Measures: {list(looker_view.measures.keys())}")

# Check for advanced features
if looker_view.extends:
    print(f"Templated view extending: {looker_view.extends}")

if looker_view.access_filters:
    print(f"Access filters detected: {looker_view.access_filters}")
```

## Integration with Migration

1. **View Discovery**: Parses every `*.lkml` file in the repository and
   collects each `view: {view_name}` block (file names do not matter;
   multi-view files are supported). A view defined in more than one file is
   an error. `view: +{view_name}` refinements are listed, not applied.
2. **Parse Contents**: Extracts all field definitions
3. **Flag Advanced Features**: Detects features needing special handling
4. **Pass to Rule Applier**: Provides structured data for conversion

## Repository index and `extends`

`load_views` parses every view of the repository once. `resolve_view_extends`
resolves a view's `extends:` chain against that index (chains, shared
grandparents, cycles) and `effective_view` returns the view as Looker sees it
after inheritance.

## LookML Parsing

Uses the `lkml` library to parse LookML:
- Handles nested structures
- Preserves field metadata
- Supports Liquid templating detection
"""

import os
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import lkml


class LookerView:
    """Container for parsed Looker view"""

    def __init__(self, file_path: str, parsed_data: Dict, file_comment: str = ''):
        self.file_path = file_path
        self.data = parsed_data
        self.file_comment = file_comment  # Raw comment block from top of .view.lkml
        self.refinement_files: List[str] = []  # files with `view: +name` (not applied)
        self.view_name = self._extract_view_name()
        self.sql_table_name = self._extract_sql_table_name()
        self.derived_table = self._extract_derived_table()
        self.dimensions = self._extract_dimensions()
        self.dimension_groups = self._extract_dimension_groups()
        self.measures = self._extract_measures()
        self.parameters = self._extract_parameters()
        self.label = self._extract_label()
        self.description = self._extract_description()

        # Detect advanced features that need special handling
        self.extends = self._extract_extends()
        self.is_templated = bool(self.extends)  # view inherits from other view(s)
        self.is_extension = self.data.get('extension') == 'required'  # abstract base view

        # Drill configuration (reported, not converted)
        self.drill_fields = list(self.data.get('drill_fields') or [])
        self.sets = {s.get('name'): s.get('fields', []) for s in self.data.get('sets', []) if s.get('name')}

        # Access control features
        self.access_filters = self._extract_access_filters()
        self.has_user_attributes = self._detect_user_attributes()
        self.required_access_grants = self._extract_required_access_grants()  # View-level
        self.field_access_grants = self._detect_field_access_grants()  # Field-level

    def _extract_view_name(self) -> str:
        """Extract view name from parsed data"""
        return self.data.get('name', '')

    def _extract_sql_table_name(self) -> Optional[str]:
        """Extract sql_table_name if present"""
        return self.data.get('sql_table_name')

    def _extract_derived_table(self) -> Optional[Dict]:
        """Extract derived_table block if present"""
        return self.data.get('derived_table')

    def _extract_dimensions(self) -> Dict[str, Dict]:
        """
        Extract all dimension blocks

        Returns dict of {dimension_name: dimension_dict}
        """
        dimensions = {}
        dim_list = self.data.get('dimensions', [])

        for dim in dim_list:
            dim_name = dim.get('name', '')
            if dim_name:
                dimensions[dim_name] = dim

        return dimensions

    def _extract_dimension_groups(self) -> Dict[str, Dict]:
        """
        Extract all dimension_group blocks

        Returns dict of {dimension_group_name: dimension_group_dict}
        """
        dimension_groups = {}
        dim_group_list = self.data.get('dimension_groups', [])

        for dim_group in dim_group_list:
            name = dim_group.get('name', '')
            if name:
                dimension_groups[name] = dim_group

        return dimension_groups

    def _extract_measures(self) -> Dict[str, Dict]:
        """
        Extract all measure blocks

        Returns dict of {measure_name: measure_dict}
        """
        measures = {}
        measure_list = self.data.get('measures', [])

        for measure in measure_list:
            measure_name = measure.get('name', '')
            if measure_name:
                measures[measure_name] = measure

        return measures

    def _extract_parameters(self) -> Dict[str, Dict]:
        """
        Extract all parameter blocks

        Returns dict of {parameter_name: parameter_dict}
        """
        parameters = {}
        param_list = self.data.get('parameters', [])

        for param in param_list:
            param_name = param.get('name', '')
            if param_name:
                parameters[param_name] = param

        return parameters

    def _extract_extends(self) -> List[str]:
        """
        Extract extends (template inheritance)

        Returns list of base view names
        """
        # lkml represents `extends: [a, b]` as `extends__all: [['a', 'b']]`
        result: List[str] = []
        for key in ('extends__all', 'extends'):
            value = self.data.get(key)
            if not value:
                continue
            if isinstance(value, str):
                value = [value]
            for item in value:
                if isinstance(item, list):
                    result.extend(str(v) for v in item)
                else:
                    result.append(str(item))
        return list(dict.fromkeys(result))

    def _extract_label(self) -> Optional[str]:
        """Extract view label if present"""
        return self.data.get('label')

    def _extract_description(self) -> Optional[str]:
        """Extract view description if present"""
        return self.data.get('description')

    def _extract_access_filters(self) -> Optional[List[Dict]]:
        """
        Extract access_filter blocks from view

        Returns list of access filter dicts if present
        """
        access_filters = self.data.get('access_filters') or self.data.get('access_filter', [])
        if not access_filters:
            return None
        if isinstance(access_filters, dict):
            return [access_filters]
        return access_filters if access_filters else None

    def _detect_user_attributes(self) -> bool:
        """
        Detect user_attribute usage in Liquid templates

        Searches through all string values in view for _user_attributes patterns
        """
        import json

        # Convert data to JSON string for easy pattern matching
        data_str = json.dumps(self.data)

        # Check for Liquid template user_attribute patterns
        return '_user_attributes' in data_str

    def _extract_required_access_grants(self) -> Optional[List[str]]:
        """
        Extract required_access_grants at view level

        Returns list of access grant names if present
        """
        grants = self.data.get('required_access_grants')
        if not grants:
            return None
        if isinstance(grants, str):
            return [grants]
        return grants if grants else None

    def _detect_field_access_grants(self) -> Optional[Dict[str, List[str]]]:
        """
        Detect required_access_grants on individual fields (dimensions/measures)

        Returns dict of {field_name: [grant_names]} for fields with access grants
        """
        field_grants = {}

        # Check dimensions
        for dim_name, dim_data in self.dimensions.items():
            grants = dim_data.get('required_access_grants')
            if grants:
                if isinstance(grants, str):
                    grants = [grants]
                field_grants[f"dimension:{dim_name}"] = grants

        # Check measures
        for measure_name, measure_data in self.measures.items():
            grants = measure_data.get('required_access_grants')
            if grants:
                if isinstance(grants, str):
                    grants = [grants]
                field_grants[f"measure:{measure_name}"] = grants

        # Check dimension_groups
        for dg_name, dg_data in self.dimension_groups.items():
            grants = dg_data.get('required_access_grants')
            if grants:
                if isinstance(grants, str):
                    grants = [grants]
                field_grants[f"dimension_group:{dg_name}"] = grants

        return field_grants if field_grants else None

    def is_derived_table(self) -> bool:
        """Check if this is a derived table view"""
        return self.derived_table is not None

    def is_standard_view(self) -> bool:
        """Check if this is a standard table view"""
        return self.sql_table_name is not None

    def summary(self) -> str:
        """Return summary of parsed view"""
        view_type = "Derived Table" if self.is_derived_table() else "Standard View"
        return f"""
Looker View: {self.view_name}
Type: {view_type}
Table: {self.sql_table_name or 'N/A'}
Dimensions: {len(self.dimensions)}
Dimension Groups: {len(self.dimension_groups)}
Measures: {len(self.measures)}
Parameters: {len(self.parameters)}
"""


def extract_file_comment(file_path: str) -> str:
    """
    Extract the leading comment block from a .view.lkml file.

    Reads lines from the top of the file that start with '#' (ignoring leading
    whitespace) and returns them as a single string with the '#' prefix stripped.

    Args:
        file_path: Path to .view.lkml file

    Returns:
        Cleaned comment text, or empty string if no comment block found
    """
    comment_lines = []
    with open(file_path, 'r') as f:
        for line in f:
            stripped = line.strip()
            if stripped.startswith('#'):
                # Remove the leading '# ' or '#'
                text = stripped.lstrip('#').strip()
                comment_lines.append(text)
            elif stripped == '':
                # Allow blank lines within comment block
                if comment_lines:
                    comment_lines.append('')
            else:
                # First non-comment, non-blank line — stop
                break

    # Remove trailing blank lines
    while comment_lines and comment_lines[-1] == '':
        comment_lines.pop()

    return ' '.join(line for line in comment_lines if line).strip()


def _view_names_in_file(lkml_file: Path) -> List[str]:
    """Names of the view blocks in a LookML file (refinements keep their `+`)"""
    try:
        text = Path(lkml_file).read_text()
    except (OSError, UnicodeDecodeError):
        return []
    if not re.search(r'\bview\s*:', text):
        return []
    try:
        parsed = lkml.load(text)
    except Exception as e:
        print(f"Warning: could not parse {lkml_file}: {e}")
        return []
    return [v.get('name', '') for v in parsed.get('views', [])]


def find_view_definitions(looker_repo: str, view_name: str) -> Tuple[List[str], List[str]]:
    """
    Every file that defines `view: view_name`, and every file that refines it
    (`view: +view_name`), across all `*.lkml` files in the repository.
    """
    definitions, refinements = [], []
    for lkml_file in sorted(Path(looker_repo).rglob("*.lkml")):
        names = _view_names_in_file(lkml_file)
        if view_name in names:
            definitions.append(str(lkml_file))
        if f"+{view_name}" in names:
            refinements.append(str(lkml_file))
    return definitions, refinements


def find_looker_view_file(looker_repo: str, view_name: str) -> Optional[str]:
    """
    Find the LookML file that defines a view (any `*.lkml` file, multi-view
    files included)

    Raises:
        ValueError: if the view is defined in more than one file, since the
            repository may hold several projects/models and the right one
            cannot be chosen safely
    """
    definitions, _ = find_view_definitions(looker_repo, view_name)
    if len(definitions) > 1:
        raise ValueError(
            f"View '{view_name}' is defined in more than one file: {', '.join(definitions)}. "
            f"Point --looker-repo at a single project."
        )
    return definitions[0] if definitions else None


def parse_looker_view(file_path: str, target_view_name: str = None) -> LookerView:
    """
    Parse Looker view file using lkml library

    Args:
        file_path: Path to .view.lkml file

    Returns:
        LookerView object

    Raises:
        FileNotFoundError: If file doesn't exist
        ValueError: If file can't be parsed
    """
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"Looker view file not found: {file_path}")

    try:
        # Extract comment block before lkml parsing (lkml strips comments)
        file_comment = extract_file_comment(file_path)

        with open(file_path, 'r') as f:
            parsed_dict = lkml.load(f)

        # lkml.load() returns {"views": [view_dict, ...]}
        views = parsed_dict.get('views', [])
        if not views:
            raise ValueError("No views found in LookML file")
        view_dict = views[0]
        if target_view_name:
            matches = [v for v in views if v.get('name') == target_view_name]
            if not matches:
                raise ValueError(f"View '{target_view_name}' not defined in file")
            view_dict = matches[0]

        return LookerView(file_path, view_dict, file_comment=file_comment)
    except Exception as e:
        raise ValueError(f"Failed to parse Looker view {file_path}: {e}")


def find_and_parse_view(looker_repo: str, view_name: str) -> LookerView:
    """
    Find and parse a Looker view

    Args:
        looker_repo: Path to Looker repository root
        view_name: Name of the view to find

    Returns:
        LookerView object

    Raises:
        FileNotFoundError: If view file not found
        ValueError: If file can't be parsed
    """
    file_path = find_looker_view_file(looker_repo, view_name)

    if not file_path:
        raise FileNotFoundError(
            f"Looker view '{view_name}' not found in {looker_repo} "
            f"(searched every *.lkml file recursively)"
        )

    view = parse_looker_view(file_path, target_view_name=view_name)
    _, view.refinement_files = find_view_definitions(looker_repo, view_name)
    return view


# ----------------------------------------------------------------------
# Repository index (every view, parsed once)
# ----------------------------------------------------------------------

_VIEW_RE = re.compile(r'\bview\s*:')


def load_views(looker_repo: str) -> Tuple[Dict[str, List[LookerView]], Dict[str, List[str]]]:
    """
    Parse every view under the repository, reading each `*.lkml` file once

    Returns:
        (definitions, refinements): view name -> its definitions (more than
        one file means the name is declared twice), and view name -> files
        holding a refinement of it (`view: +name`)
    """
    definitions: Dict[str, List[LookerView]] = {}
    refinements: Dict[str, List[str]] = {}
    for lkml_file in sorted(Path(looker_repo).rglob("*.lkml")):
        try:
            text = lkml_file.read_text()
        except (OSError, UnicodeDecodeError):
            continue
        if not _VIEW_RE.search(text):
            continue
        try:
            parsed = lkml.load(text)
        except Exception as e:
            print(f"Warning: could not parse {lkml_file}: {e}")
            continue
        file_comment: Optional[str] = None
        for view_dict in parsed.get('views', []):
            name = view_dict.get('name', '')
            if name.startswith('+'):
                refinements.setdefault(name[1:], []).append(str(lkml_file))
                continue
            if file_comment is None:
                file_comment = extract_file_comment(str(lkml_file))
            definitions.setdefault(name, []).append(LookerView(str(lkml_file), view_dict, file_comment=file_comment))
    for name, files in refinements.items():
        for view in definitions.get(name, []):
            view.refinement_files = list(files)
    return definitions, refinements


def view_from_index(
    definitions: Dict[str, List[LookerView]],
    looker_repo: str,
    view_name: str
) -> LookerView:
    """
    The single definition of a view in a `load_views` index

    Raises the same errors as `find_and_parse_view`: FileNotFoundError when
    the view is not defined, ValueError when more than one file defines it.
    """
    found = definitions.get(view_name, [])
    files = sorted({view.file_path for view in found})
    if len(files) > 1:
        raise ValueError(
            f"View '{view_name}' is defined in more than one file: {', '.join(files)}. "
            f"Point --looker-repo at a single project."
        )
    if not found:
        raise FileNotFoundError(
            f"Looker view '{view_name}' not found in {looker_repo} "
            f"(searched every *.lkml file recursively)"
        )
    return found[0]


# ----------------------------------------------------------------------
# View inheritance (extends)
# ----------------------------------------------------------------------

# (field kind, key of the parsed view that lists the fields of that kind)
FIELD_KINDS = (
    ('dimension', 'dimensions'),
    ('dimension_group', 'dimension_groups'),
    ('measure', 'measures'),
)


@dataclass
class InheritedField:
    """A field a view receives through `extends:`"""
    kind: str  # dimension, dimension_group or measure
    data: Dict[str, Any]  # the definition Looker uses (parameters merged along the chain)
    origin: str  # view whose definition was applied last
    via: str  # direct parent it arrives through (the last one when several supply it)
    # Direct parents that supply DIFFERENT definitions, in `extends:` order.
    # Looker uses the last one, Omni the first. Empty when there is no conflict.
    conflict: List[str] = field(default_factory=list)


@dataclass
class ViewSource:
    """Where a view reads its rows from, and the view that declares it"""
    origin: str
    sql_table_name: Optional[str] = None
    derived_table: Optional[Dict] = None


@dataclass
class ViewExtendsResolution:
    """Result of resolving a view's `extends:` chain"""
    view_name: str
    parents: List[str] = field(default_factory=list)  # as declared, in order
    ancestors: List[str] = field(default_factory=list)  # every view of the chain, each after its own parents
    problem: Optional[str] = None  # why the chain could not be resolved
    fields: Dict[str, InheritedField] = field(default_factory=dict)  # inherited, before the view's own fields
    source: Optional[ViewSource] = None  # inherited table or derived table, if any parent declares one

    @property
    def conflicts(self) -> Dict[str, InheritedField]:
        """Inherited fields that two parents define differently"""
        return {name: inherited for name, inherited in self.fields.items() if inherited.conflict}


@dataclass
class _ViewChain:
    inherited: Dict[str, InheritedField]
    fields: Dict[str, InheritedField]  # inherited plus the view's own
    ancestors: List[str]
    inherited_source: Optional[ViewSource]
    source: Optional[ViewSource]


class _UnresolvedExtends(Exception):
    pass


def own_fields(view: LookerView) -> Dict[str, Tuple[str, Dict]]:
    """{field name: (kind, definition)} for the fields declared in the view itself"""
    kinds = {'dimension': view.dimensions, 'dimension_group': view.dimension_groups, 'measure': view.measures}
    return {name: (kind, data) for kind, fields in kinds.items() for name, data in fields.items()}


def _build_chain(
    view: LookerView,
    definitions: Dict[str, List[LookerView]],
    cache: Dict[str, Any],
    path: Tuple[str, ...]
) -> _ViewChain:
    name = view.view_name
    inherited: Dict[str, InheritedField] = {}
    ancestors: List[str] = []
    inherited_source: Optional[ViewSource] = None

    for parent_name in view.extends:
        if parent_name == name or parent_name in path:
            raise _UnresolvedExtends("circular extends: " + ' → '.join(path + (name, parent_name)))
        candidates = definitions.get(parent_name, [])
        files = sorted({candidate.file_path for candidate in candidates})
        if not candidates:
            raise _UnresolvedExtends(f"view `{parent_name}` was not found under --looker-repo")
        if len(files) > 1:
            raise _UnresolvedExtends(f"view `{parent_name}` is defined more than once ({', '.join(files)})")
        parent = _chain(candidates[0], definitions, cache, path + (name,))
        for ancestor in parent.ancestors + [parent_name]:
            if ancestor not in ancestors:
                ancestors.append(ancestor)
        for field_name, supplied in parent.fields.items():
            previous = inherited.get(field_name)
            if previous is None:
                inherited[field_name] = replace(supplied, via=parent_name, conflict=[])
            elif previous.kind != supplied.kind:
                raise _UnresolvedExtends(
                    f"`{field_name}` is a {previous.kind} in `{previous.via}` and a {supplied.kind} in "
                    f"`{parent_name}`"
                )
            elif previous.data != supplied.data:
                # Looker merges parameter by parameter, the later parent winning
                inherited[field_name] = InheritedField(
                    kind=supplied.kind,
                    data={**previous.data, **supplied.data},
                    origin=supplied.origin,
                    via=parent_name,
                    conflict=(previous.conflict or [previous.via]) + [parent_name]
                )
        if parent.source is not None:
            inherited_source = parent.source

    fields = {field_name: replace(supplied, conflict=[]) for field_name, supplied in inherited.items()}
    for field_name, (kind, data) in own_fields(view).items():
        previous = inherited.get(field_name)
        if previous is not None and previous.kind != kind:
            raise _UnresolvedExtends(
                f"`{field_name}` is a {previous.kind} in `{previous.via}` and a {kind} in `{name}`"
            )
        merged = {**previous.data, **data} if previous is not None else data
        fields[field_name] = InheritedField(kind=kind, data=merged, origin=name, via=name)

    source = inherited_source
    if view.sql_table_name or view.derived_table:
        source = ViewSource(origin=name, sql_table_name=view.sql_table_name, derived_table=view.derived_table)
    return _ViewChain(inherited, fields, ancestors, inherited_source, source)


def _chain(
    view: LookerView,
    definitions: Dict[str, List[LookerView]],
    cache: Dict[str, Any],
    path: Tuple[str, ...]
) -> _ViewChain:
    """`_build_chain`, computed once per view (a failure is remembered too)"""
    cached = cache.get(view.view_name)
    if isinstance(cached, _UnresolvedExtends):
        raise cached
    if cached is not None:
        return cached
    try:
        chain = _build_chain(view, definitions, cache, path)
    except _UnresolvedExtends as e:
        cache[view.view_name] = e
        raise
    cache[view.view_name] = chain
    return chain


def resolve_view_extends(
    view: LookerView,
    definitions: Dict[str, List[LookerView]],
    cache: Optional[Dict[str, Any]] = None
) -> ViewExtendsResolution:
    """
    Resolve everything a view inherits through `extends:`

    Parents are applied in list order (a later parent wins over an earlier
    one, parameter by parameter), each parent after its own chain. Views
    shared by several branches (diamonds) are resolved once. `definitions`
    comes from `load_views`; pass the same `cache` dict to every call of a
    run so each view of the repository is resolved once.

    A field that reaches the view through two parents with different
    definitions is a conflict (`InheritedField.conflict`): Looker uses the
    last parent, Omni the first. The same definition arriving twice (a
    shared grandparent) is not a conflict.

    When a parent is missing, declared more than once, the chain is
    circular, or a name is a different kind of field on two sides,
    `problem` says why and nothing is inherited.
    """
    resolution = ViewExtendsResolution(view_name=view.view_name, parents=list(view.extends))
    if not view.extends:
        return resolution
    try:
        chain = _chain(view, definitions, cache if cache is not None else {}, ())
    except _UnresolvedExtends as e:
        resolution.problem = str(e)
        return resolution
    resolution.ancestors = list(chain.ancestors)
    resolution.fields = dict(chain.inherited)
    resolution.source = chain.inherited_source
    return resolution


def effective_view(
    view: LookerView,
    resolution: ViewExtendsResolution,
    include_inherited: bool = False
) -> LookerView:
    """
    The view as Looker sees it after `extends:`

    The view's own fields get the parameters they inherit (a partial
    override such as `dimension: id { hidden: yes }` keeps the parent's
    `sql`), and the fields two parents define differently are added with the
    definition Looker uses. With `include_inherited` every inherited field
    is added, which gives the full field list of the view.
    """
    own = own_fields(view)
    data = dict(view.data)
    for kind, key in FIELD_KINDS:
        fields = []
        for name, (own_kind, own_data) in own.items():
            if own_kind != kind:
                continue
            inherited = resolution.fields.get(name)
            fields.append({**inherited.data, **own_data} if inherited is not None else own_data)
        for name, inherited in resolution.fields.items():
            if inherited.kind == kind and name not in own and (include_inherited or inherited.conflict):
                fields.append(inherited.data)
        data[key] = fields
    merged = LookerView(view.file_path, data, file_comment=view.file_comment)
    merged.refinement_files = list(view.refinement_files)
    return merged
