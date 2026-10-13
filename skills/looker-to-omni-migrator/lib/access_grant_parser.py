"""
Access Grant Parser - Parse and Manage Looker Access Grants

This module finds and parses Looker `access_grant` definitions. Grants are
usually declared in a model file (`*.model.lkml`) but may live in any `.lkml`
file, so every LookML file in the repository is scanned.

## Purpose

Access grants restrict access to explores, views and fields in Looker. This parser:
1. Finds every `.lkml` file that declares at least one `access_grant`
2. Extracts grant definitions (name, user_attribute, allowed_values)
3. Provides a registry for enriching migration reports

## Looker Access Grant Structure

```lkml
access_grant: can_view_pii {
  user_attribute: can_see_pii
  allowed_values: ["yes"]
}

access_grant: can_view_financials {
  user_attribute: department
  allowed_values: ["finance", "executive"]
}
```

## Usage

```python
from lib.access_grant_parser import load_access_grants

grants = load_access_grants('/path/to/looker/repo')

grant = grants['can_view_pii']
print(f"User Attribute: {grant.user_attribute}")
print(f"Allowed Values: {grant.allowed_values}")

report = generate_access_grants_report(grants)
```

## Integration with Migration

During migration, this module:
1. Loads access grants at orchestrator initialization
2. Lets reports show the user attribute and allowed values behind every
   grant referenced by an explore, view or field
3. Flags the manual Omni configuration that each grant needs
"""

import re

import lkml
from pathlib import Path
from typing import Dict, List, Optional
from dataclasses import dataclass


_GRANT_RE = re.compile(r'\baccess_grant\s*:')


@dataclass
class AccessGrantDefinition:
    """Represents an `access_grant` definition from a LookML file"""
    grant_name: str
    user_attribute: str
    allowed_values: List[str]


def find_access_grants_files(looker_repo: str) -> List[str]:
    """
    Find every LookML file that declares at least one access_grant

    Args:
        looker_repo: Path to Looker repository root

    Returns:
        Sorted list of file paths
    """
    repo_path = Path(looker_repo)
    files = []

    for file_path in sorted(repo_path.rglob("*.lkml")):
        try:
            text = file_path.read_text()
        except (OSError, UnicodeDecodeError):
            continue
        # Cheap pre-check before parsing
        if not _GRANT_RE.search(text):
            continue
        try:
            parsed = lkml.load(text)
        except Exception as e:
            print(f"Warning: could not parse {file_path} (it may declare access grants): {e}")
            continue
        if parsed.get('access_grants'):
            files.append(str(file_path))

    return files


def parse_access_grants_file(file_path: str) -> Dict[str, AccessGrantDefinition]:
    """
    Parse the access grants declared in one LookML file

    Args:
        file_path: Path to a .lkml file

    Returns:
        Dict of {grant_name: AccessGrantDefinition}
    """
    with open(file_path, 'r') as f:
        parsed = lkml.load(f)

    grants = {}

    # lkml.load returns {"access_grants": [grant_dict, ...]}
    for grant_dict in parsed.get('access_grants', []):
        grant_name = grant_dict.get('name', '')
        user_attribute = grant_dict.get('user_attribute', '')
        allowed_values = grant_dict.get('allowed_values', [])
        if isinstance(allowed_values, str):
            allowed_values = [allowed_values]

        if grant_name:
            grants[grant_name] = AccessGrantDefinition(
                grant_name=grant_name,
                user_attribute=user_attribute,
                allowed_values=allowed_values
            )

    return grants


def load_access_grants(looker_repo: str) -> Optional[Dict[str, AccessGrantDefinition]]:
    """
    Find and parse all access grants files

    Merges the grants from every file into a single registry

    Args:
        looker_repo: Path to Looker repository root

    Returns:
        Dict of {grant_name: AccessGrantDefinition} or None if not found
    """
    file_paths = find_access_grants_files(looker_repo)

    if not file_paths:
        return None

    # Merge grants from all files. The same grant name in two files is
    # ambiguous (only one would apply in a given model): stop with a diagnostic.
    all_grants = {}
    sources: Dict[str, str] = {}

    for file_path in file_paths:
        try:
            grants = parse_access_grants_file(file_path)
        except Exception as e:
            print(f"Warning: Failed to parse {file_path}: {e}")
            continue
        for name, grant in grants.items():
            if name in all_grants:
                raise ValueError(
                    f"access_grant '{name}' is defined in more than one file "
                    f"({sources[name]} and {file_path}); point --looker-repo at a single project"
                )
            all_grants[name] = grant
            sources[name] = file_path

    return all_grants if all_grants else None


def generate_access_grants_report(grants: Dict[str, AccessGrantDefinition]) -> str:
    """
    Generate markdown report of access grants

    Args:
        grants: Dict of access grant definitions

    Returns:
        Markdown report string
    """
    report = []
    report.append("# Access Grants Registry\n\n")
    report.append(f"**Total Grants:** {len(grants)}\n\n")
    report.append("---\n\n")

    # Table
    report.append("| Grant Name | User Attribute | Allowed Values |\n")
    report.append("|------------|----------------|----------------|\n")

    for grant_name, grant in sorted(grants.items()):
        values_str = ', '.join([f'`{v}`' for v in grant.allowed_values])
        report.append(f"| `{grant_name}` | `{grant.user_attribute}` | {values_str} |\n")

    return ''.join(report)
