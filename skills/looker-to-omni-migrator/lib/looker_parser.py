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

## LookML Parsing

Uses the `lkml` library to parse LookML:
- Handles nested structures
- Preserves field metadata
- Supports Liquid templating detection
"""

import os
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple
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
