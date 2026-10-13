"""
File Generators - YAML and Markdown Report Generation

This module writes Omni YAML files and migration reports from converted
Looker views and explores.

## Outputs

| File | Written to | Produced by |
|------|-----------|-------------|
| `{view}.view.yaml` | views dir (`--omni-repo`) | view migration, table-based view |
| `{view}.query.view` | views dir | view migration, derived table (`sql:` or `query:`) |
| `{view}.migration.md` | reports dir | view migration |
| `{explore}.topic.yaml` | topics dir | explore migration |
| `{explore}.relationships` | topics dir | explore migration (YAML list of joins) |
| `{explore}_explore.md` | reports dir | explore migration |

The reports dir defaults to the sibling `reports/` folder of the output dir
(`migration/staging/views` -> `migration/staging/reports`).

With `dry_run=True` nothing is written and no directory is created: every
write is recorded in `planned_writes` and printed instead.

## YAML Formatting

- ruamel.yaml, no line wrapping, 2-space indentation
- Tags rendered inline: `tags: [mig_phase_0, verified]`
- Multi-line or long SQL rendered as a `|-` block
- View-level `sql:` (SQL derived tables) is reformatted with `sqlparse`
- Blank lines between dimension/measure entries

## Example View Output

```yaml
schema: thelook_ecommerce
folder: thelook_ecommerce/curated
label: Users
description: One row per registered shopper
tags: [users, fact, mig_phase_0, verified]
table_name: users

dimensions:
  id:
    label: Id
    description: Id dimension
    tags: [id, mig_phase_0, verified]
    primary_key: true

  full_name:
    label: Full Name
    description: Full Name dimension
    tags: [name, pii, mig_phase_0, verified]
    sql: CONCAT(${first_name}, ' ', ${last_name})

measures:
  count:
    aggregate_type: count
    label: Count
    description: Count measure
    tags: [metric, mig_phase_0, verified]
```

## Merging With an Existing Omni View

When an existing Omni view file is found (from `--omni-source-repo`, or a
previous run in the output dir), `generate_view_yaml` merges into it:

1. All existing top-level keys are kept (e.g. `dbt`,
   `required_access_grants`), Looker `ai_context` is added, and missing
   `label`/`description` are filled in. Tags are unioned.
2. A Looker field that reads a differently named column (for example
   `dimension: customer_id { sql: ${TABLE}.user_id ;; }`) is merged into the
   existing `user_id` field, with `customer_id` added as a synonym, and every
   `${customer_id}` reference in the merged view is rewritten to `${user_id}`.
   This only happens when `user_id` already exists in the Omni view.
3. In-scope fields (and auto-included dependencies) overlay their Looker
   metadata on the existing field.
4. Existing fields that are not in scope are kept, hidden, and tagged
   `in_omni_in_looker_not_csv` or `only_omni`.
5. Looker fields missing from the existing view are added: in-scope ones
   visible, the others hidden and tagged `in_omni_in_looker_not_csv` (the
   same as a fresh generation).
6. An existing `catalog` that the LookML source does not name stops the
   merge (MergeError): it could otherwise query a different database.

## Report Builder

`ConversionReportBuilder` collects per-field results, categorizes changes
(Canonical Mappings, SQL Transformations, Metadata Additions, Format
Mappings, Other), tracks HIGH/MEDIUM risks, fields not converted, and
"advanced features" (derived tables, access grants, unsupported
constructs). Items that need a human are reported with status
`MANUAL ACTION REQUIRED`.
"""

import io
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import sqlparse
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedSeq
from ruamel.yaml.scalarstring import DoubleQuotedScalarString, LiteralScalarString

from .omni_models import (
    OmniView, OmniTopic, MigrationReport,
    view_to_yaml_dict, topic_to_yaml_dict
)
from .rule_applier import ConversionResult

# Known top-level view keys, in output order. Unknown keys are kept and
# written after these (before dimensions/measures).
_VIEW_KEY_ORDER = [
    'schema', 'catalog', 'folder', 'label', 'description', 'tags', 'table_name', 'sql',
    'query', 'template', 'extends', 'ai_context',
]

_PROVENANCE_TAGS = ('in_omni_in_looker_not_csv', 'only_omni')
_STATUS_TAGS = ('verified', 'unverified')
_SOURCE_KEYS = ('table_name', 'sql', 'query')

# Field keys owned by the conversion: on an in-scope field they are REPLACED by
# the newly converted definition (a key the new definition omits is removed),
# never overlaid on stale values.
CONVERSION_OWNED_KEYS = (
    'sql', 'filters', 'aggregate_type', 'primary_key', 'format', 'hidden',
    'percentile', 'custom_primary_key_sql', 'timeframes', 'datatype',
)

_REF_RE = re.compile(r'\$\{([a-zA-Z_][a-zA-Z0-9_]*)\}')


# Tag recording that a field holds a Looker field merged under another name
# (e.g. `from_looker__customer_email` on `email`). It is not a governance tag,
# so it survives later runs and lets them find the field whatever the scope.
LOOKER_ORIGIN_PREFIX = 'from_looker__'


def looker_origin_tag(looker_field: str) -> str:
    return f"{LOOKER_ORIGIN_PREFIX}{looker_field}"


def prior_renames(*sections: Dict) -> Dict[str, str]:
    """
    {Looker field: existing field} from origin tags written by earlier runs

    Raises:
        MergeError: the same Looker field is tagged on two fields
    """
    result = {}
    for section in sections:
        for name, data in _as_dict(section).items():
            for tag in (_as_dict(data).get('tags') or []):
                if isinstance(tag, str) and tag.startswith(LOOKER_ORIGIN_PREFIX):
                    looker = tag[len(LOOKER_ORIGIN_PREFIX):]
                    if looker in result and result[looker] != name:
                        raise MergeError(
                            f"Looker field `{looker}` is tagged `{tag}` on both `{result[looker]}` and `{name}` "
                            f"in the existing view; remove one tag and re-run"
                        )
                    result[looker] = name
    return result


class MergeError(ValueError):
    """The merged view would be invalid; nothing is written"""


def _as_dict(value: Any) -> Dict:
    """Existing YAML fields may be None (`field:`), {} or a mapping"""
    return dict(value) if isinstance(value, dict) else {}


def _is_governance_tag(tag: Any) -> bool:
    return isinstance(tag, str) and (
        tag in _PROVENANCE_TAGS or tag in _STATUS_TAGS or tag == 'auto_dependency'
        or tag.startswith('mig_phase_')
    )


def _was_migrated(field: Dict) -> bool:
    """True for a field written visible by an earlier migration run"""
    tags = field.get('tags') or []
    return any(isinstance(t, str) and t.startswith('mig_phase_') for t in tags) and not field.get('hidden')


def _union(a: Any, b: Any) -> List:
    return list(dict.fromkeys(list(a or []) + list(b or [])))


def _rewrite_field_refs(obj: Any, renames: Dict[str, str], view_name: Optional[str] = None) -> Any:
    """
    Apply a rename map {old field: new field} to every field reference:
    `${old}`, `${view.old}` in strings and `old` / `view.old` keys of filter
    mappings.
    """
    if not renames:
        return obj

    def _key(key):
        if isinstance(key, str):
            if key in renames:
                return renames[key]
            if view_name and key.startswith(view_name + '.') and key[len(view_name) + 1:] in renames:
                return f"{view_name}.{renames[key[len(view_name) + 1:]]}"
        return key

    if isinstance(obj, str):
        for old, new in renames.items():
            obj = re.sub(r'\$\{' + re.escape(old) + r'\}', '${' + new + '}', obj)
            if view_name:
                obj = re.sub(r'\$\{' + re.escape(view_name) + r'\.' + re.escape(old) + r'\}',
                             '${' + view_name + '.' + new + '}', obj)
        return obj
    if isinstance(obj, dict):
        result = {}
        for k, v in obj.items():
            if k == 'filters' and isinstance(v, dict):
                result[k] = {_key(fk): _rewrite_field_refs(fv, renames, view_name) for fk, fv in v.items()}
            else:
                result[k] = _rewrite_field_refs(v, renames, view_name)
        return result
    if isinstance(obj, list):
        return [_rewrite_field_refs(v, renames, view_name) for v in obj]
    return obj


def _merge_field(existing: Dict, new: Dict) -> Dict:
    """
    In-scope field: keep the existing field's other keys (descriptions, user
    tags, ...), REPLACE conversion-owned keys and governance tags, union
    access grants (a merge never removes an access restriction)
    """
    merged = {k: v for k, v in existing.items() if k not in CONVERSION_OWNED_KEYS and k != 'tags'}
    merged.update({k: v for k, v in new.items() if k not in ('tags', 'required_access_grants')})
    user_tags = [t for t in (existing.get('tags') or []) if not _is_governance_tag(t)]
    merged['tags'] = _union(user_tags, new.get('tags'))
    grants = _union(existing.get('required_access_grants'), new.get('required_access_grants'))
    if grants:
        merged['required_access_grants'] = grants
    return merged


def find_self_and_missing_refs(fields: Dict[str, Dict], names: Set[str]) -> List[str]:
    """Problems with same-view ${field} references in the given field definitions"""
    problems = []
    for name, data in fields.items():
        if not isinstance(data, dict):
            continue
        texts = [data.get('sql')] if isinstance(data.get('sql'), str) else []
        for ref in {r for t in texts for r in _REF_RE.findall(t)}:
            if ref == name:
                problems.append(f"{name} references itself (${{{name}}})")
            elif ref not in names:
                problems.append(f"{name} references ${{{ref}}}, which is not a field of the view")
        for key in (data.get('filters') or {}):
            if isinstance(key, str) and '.' not in key and key not in names:
                problems.append(f"{name} filters on {key}, which is not a field of the view")
    return problems


class FileGenerator:
    """Generates Omni view, topic, relationship and report files"""

    def __init__(self, output_dir: Path, reports_dir: Optional[Path] = None, dry_run: bool = False):
        self.output_dir = Path(output_dir)
        self.reports_dir = Path(reports_dir) if reports_dir else self.output_dir.parent / 'reports'
        self.dry_run = dry_run
        self.planned_writes: List[Path] = []

        self.yaml = YAML()
        self.yaml.preserve_quotes = True
        self.yaml.default_flow_style = False
        self.yaml.width = 4096  # Prevent line wrapping
        self.yaml.indent(mapping=2, sequence=2, offset=0)

    # ------------------------------------------------------------------
    # IO
    # ------------------------------------------------------------------

    def write_file(self, path: Path, content: str) -> Path:
        """Write a file, or (dry run) only record and print the planned write"""
        path = Path(path)
        self.planned_writes.append(path)
        if self.dry_run:
            print(f"      [dry-run] would write {path} ({len(content.encode())} bytes)")
            return path
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write to a temp file in the same directory, then replace atomically
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, 'w') as f:
                f.write(content)
            os.replace(tmp, path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
        return path

    def remove_file(self, path: Path) -> bool:
        """Delete a stale generated file (dry run: only print). True if it existed."""
        path = Path(path)
        if not path.exists():
            return False
        if self.dry_run:
            print(f"      [dry-run] would remove stale {path}")
        else:
            path.unlink()
            print(f"      ✓ Removed stale {path}")
        return True

    def _dump_yaml(self, data: Any) -> str:
        stream = io.StringIO()
        self.yaml.dump(data, stream)
        return stream.getvalue()

    def read_existing_omni_view(self, view_filename: str) -> Optional[Dict]:
        """Read an existing Omni view file from the output directory"""
        return self.read_view_from_path(self.output_dir / view_filename)

    def read_view_from_path(self, view_path: Path) -> Optional[Dict]:
        """
        Read an Omni view file; None if it does not exist.

        Raises:
            MergeError: the file exists but cannot be parsed (it is never
                treated as absent and overwritten)
        """
        view_path = Path(view_path)
        if not view_path.exists():
            return None
        try:
            with open(view_path, 'r') as f:
                existing_view = self.yaml.load(f)
        except Exception as e:
            raise MergeError(f"Existing Omni view {view_path} could not be parsed: {e}")
        if existing_view is None:
            return None
        if not isinstance(existing_view, dict):
            raise MergeError(f"Existing Omni view {view_path} is not a YAML mapping")
        return existing_view

    # ------------------------------------------------------------------
    # Merge
    # ------------------------------------------------------------------

    def _merge_section(
        self,
        new_fields: Dict,
        existing_fields: Dict,
        csv_fields: Set[str],
        field_to_column_mapping: Dict[str, str],
        all_looker_fields: Set[str],
        applied_renames: Dict[str, str],
        stats: Dict[str, int],
        written: Set[str],
        manual: List[str],
        unconverted: Dict[str, str],
        earlier_renames: Dict[str, str]
    ) -> Dict:
        """Merge one section (dimensions or measures). Updates applied_renames/stats/written/manual."""
        merged: Dict[str, Any] = {}

        def _in_scope(name: str, data: Any) -> bool:
            is_auto_dep = isinstance(data, dict) and 'auto_dependency' in (data.get('tags') or [])
            return name in csv_fields or is_auto_dep

        # Looker fields that read a differently named column that EXISTS in
        # the existing view are merged into that column's field.
        # The target name must not belong to another Looker field (converted
        # or not), and only the first Looker field reading it gets it: otherwise a Looker
        # definition would be silently replaced. The others keep their own
        # Looker names.
        column_to_field = {}
        owner_of = {target: looker for looker, target in earlier_renames.items()}
        skip_new: Set[str] = set()
        # A Looker field merged under another name by an earlier run lives on
        # in that field: merge into it again (in scope) or leave it alone (out
        # of scope), never emit a duplicate copy under the Looker name
        for looker_field, target in sorted(earlier_renames.items()):
            if target not in existing_fields or looker_field in existing_fields:
                continue
            if looker_field not in all_looker_fields:
                continue  # stale tag: that Looker field no longer exists
            if target in all_looker_fields:
                # Fail closed: merging would silently replace one of two
                # current Looker definitions
                raise MergeError(
                    f"existing field `{target}` holds Looker field `{looker_field}` (tag "
                    f"`{looker_origin_tag(looker_field)}` from an earlier run), but the LookML now also defines "
                    f"a field named `{target}`. Resolve it before re-running: rename one of the two fields in "
                    f"LookML, or remove the `{looker_origin_tag(looker_field)}` tag from `{target}` in the Omni view"
                )
            applied_renames[looker_field] = target
            if looker_field in new_fields and _in_scope(looker_field, new_fields[looker_field]):
                column_to_field[target] = looker_field
            else:
                skip_new.add(looker_field)
        for looker_field, column in sorted(field_to_column_mapping.items()):
            if looker_field in applied_renames:
                continue
            if not (column in existing_fields and looker_field in new_fields
                    and looker_field not in existing_fields
                    and _in_scope(looker_field, new_fields[looker_field])):
                continue
            if column in owner_of and owner_of[column] != looker_field:
                manual.append(
                    f"{looker_field} reads column `{column}`, but the existing `{column}` already holds Looker "
                    f"field {owner_of[column]} (earlier run): kept as `{looker_field}`"
                )
                continue
            if column in all_looker_fields:
                manual.append(
                    f"{looker_field} reads column `{column}`, but `{column}` is also a Looker field with its own "
                    f"definition: kept as `{looker_field}`, not merged into the existing `{column}`"
                )
                continue
            if column in column_to_field:
                manual.append(
                    f"{looker_field} and {column_to_field[column]} both read column `{column}`: only "
                    f"{column_to_field[column]} was merged into the existing `{column}`; {looker_field} kept as is"
                )
                continue
            column_to_field[column] = looker_field

        for name, data in existing_fields.items():
            existing_field = _as_dict(data)

            if name in column_to_field:
                looker_field = column_to_field[name]
                merged_field = _merge_field(existing_field, _as_dict(new_fields[looker_field]))
                synonyms = [s for s in (merged_field.get('synonyms') or []) if s != name]
                if looker_field not in synonyms:
                    synonyms.insert(0, looker_field)
                merged_field['synonyms'] = synonyms
                merged_field['tags'] = _union(merged_field.get('tags'), [looker_origin_tag(looker_field)])
                if merged_field.get('sql') in (name, f'${{{name}}}'):
                    del merged_field['sql']
                merged[name] = merged_field
                applied_renames[looker_field] = name
                stats['renamed'] += 1
                written.add(name)
                continue

            new_data = new_fields.get(name)
            if new_data is not None and _in_scope(name, new_data):
                merged_field = _merge_field(existing_field, _as_dict(new_data))
                if merged_field.get('sql') in (name, f'${{{name}}}'):
                    del merged_field['sql']
                merged[name] = merged_field
                stats['enriched'] += 1
                written.add(name)
                continue

            # Not in scope this run (or its Looker definition could not be
            # converted): keep the existing definition
            field = existing_field
            looker_name = owner_of.get(name, name)
            if looker_name in unconverted:
                manual.append(
                    f"existing Omni field `{name}` was kept, but the LookML definition of `{looker_name}` was not "
                    f"migrated ({unconverted[looker_name]}); check that it still matches Looker"
                )
            if field.get('sql') in (name, f'${{{name}}}'):
                del field['sql']
            # Access restrictions from LookML apply regardless of CSV scope
            if isinstance(new_data, dict) and new_data.get('required_access_grants'):
                field['required_access_grants'] = _union(
                    field.get('required_access_grants'), new_data['required_access_grants']
                )
            if _was_migrated(field):
                # Migrated by an earlier run (another phase/owner): leave visible
                stats['kept_previous_migration'] += 1
            else:
                prov_tag = 'in_omni_in_looker_not_csv' if name in all_looker_fields else 'only_omni'
                tags = list(field.get('tags') or [])
                if prov_tag not in tags:
                    tags.append(prov_tag)
                field['tags'] = tags
                field['hidden'] = True
                stats['hidden_out_of_scope'] += 1
            merged[name] = field

        # Looker fields that do not exist in the Omni view yet. Out-of-scope
        # ones arrive already hidden and tagged (same as a fresh generation).
        merged_sources = set(column_to_field.values()) | skip_new
        for name, data in new_fields.items():
            if name in merged or name in merged_sources:
                continue
            merged[name] = data
            written.add(name)
            if _in_scope(name, data):
                stats['added'] += 1
            else:
                stats['added_hidden'] += 1

        return merged

    def merge_with_existing_view(
        self,
        new_view: Dict,
        existing_view: Dict,
        csv_fields: Set[str],
        field_to_column_mapping: Dict[str, str],
        view_name: Optional[str] = None,
        looker_fields: Optional[Set[str]] = None,
        field_grants: Optional[Dict[str, List[str]]] = None,
        unconverted: Optional[Dict[str, str]] = None,
        grant_columns: Optional[Dict[str, str]] = None
    ) -> Tuple[Dict, Dict[str, Any]]:
        """
        Merge the newly converted view into an existing Omni view

        Args:
            looker_fields: every Looker field name of the view, converted or not
            field_grants: {Looker field: required_access_grants}
            unconverted: {Looker field: reason} for fields that were not written
            grant_columns: {Looker field with grants: warehouse column it reads}.
                An existing field named after that column (and not itself a
                Looker field) exposes the same data, so it gets the grants too.

        LookML field grants are applied to every final field that corresponds
        to a Looker field (same name, or the existing field it was merged
        into), whether or not its SQL was converted.

        Returns:
            (merged view dict, info) where info has 'stats', 'notes' and
            'manual' (items that need a human)

        Raises:
            MergeError: the merged view has broken/self references or more
                than one primary key
        """
        merged = dict(existing_view)
        notes: List[str] = []
        manual: List[str] = []
        field_grants = field_grants or {}
        unconverted = unconverted or {}
        stats = {k: 0 for k in ('enriched', 'renamed', 'added', 'added_hidden', 'hidden_out_of_scope',
                                'kept_previous_migration')}
        stats['existing_dims'] = len(_as_dict(existing_view.get('dimensions')))
        stats['existing_measures'] = len(_as_dict(existing_view.get('measures')))

        # A catalog the LookML does not name could point the view at another
        # database: never keep or drop it silently
        if existing_view.get('catalog') and not new_view.get('catalog'):
            raise MergeError(
                f"the existing view sets catalog `{existing_view['catalog']}` but the Looker source has no "
                f"catalog/project. Keeping it or removing it could query a different database: resolve the "
                f"catalog in the existing Omni view (or use a 3-part sql_table_name) and re-run"
            )

        # View source: the generated source replaces the existing one
        new_sources = [k for k in _SOURCE_KEYS if new_view.get(k) is not None]
        for key in _SOURCE_KEYS + ('schema', 'catalog'):
            if key in new_view and new_view[key] is not None:
                if key in existing_view and existing_view[key] != new_view[key]:
                    notes.append(f"Replaced existing `{key}` with the value generated from LookML")
                merged[key] = new_view[key]
        if new_sources:
            for key in _SOURCE_KEYS:
                if key not in new_sources and key in merged:
                    notes.append(f"Removed existing `{key}`: the LookML view uses `{new_sources[0]}`")
                    del merged[key]

        if new_view.get('ai_context'):
            merged['ai_context'] = new_view['ai_context']
        if new_view.get('description') and not existing_view.get('description'):
            merged['description'] = new_view['description']
        if new_view.get('label') and not existing_view.get('label'):
            merged['label'] = new_view['label']
        if new_view.get('tags'):
            existing_tags = merged.get('tags')
            tags = list(existing_tags) if isinstance(existing_tags, list) else []
            if any(t in _STATUS_TAGS for t in new_view['tags']):
                tags = [t for t in tags if t not in _STATUS_TAGS]
            merged['tags'] = _union(tags, new_view['tags'])

        new_dims = new_view.get('dimensions') or {}
        new_measures = new_view.get('measures') or {}
        all_looker_fields = (set(new_dims) | set(new_measures) | set(field_to_column_mapping)
                             | set(looker_fields or ()) | set(unconverted))
        applied_renames: Dict[str, str] = {}
        written: Set[str] = set()
        existing_dims = _as_dict(existing_view.get('dimensions'))
        existing_measures = _as_dict(existing_view.get('measures'))

        merged_dims = self._merge_section(
            new_dims, existing_dims, csv_fields,
            field_to_column_mapping, all_looker_fields, applied_renames, stats, written, manual, unconverted,
            prior_renames(existing_dims)
        )
        merged_measures = self._merge_section(
            new_measures, existing_measures, csv_fields,
            field_to_column_mapping, all_looker_fields, applied_renames, stats, written, manual, unconverted,
            prior_renames(existing_measures)
        )

        # LookML field grants apply to the final field, whatever happened to
        # its SQL (a retained existing field must not lose its restriction)
        for looker, grants in field_grants.items():
            targets = [applied_renames.get(looker, looker)]
            column = (grant_columns or {}).get(looker)
            if column and column not in targets and column not in all_looker_fields:
                if column in merged_dims or column in merged_measures:
                    targets.append(column)
                    manual.append(
                        f"existing field `{column}` reads the same column as Looker `{looker}`: "
                        f"required_access_grants {grants} applied to it as well"
                    )
            for target in targets:
                for section in (merged_dims, merged_measures):
                    if target in section:
                        field = _as_dict(section[target])
                        field['required_access_grants'] = _union(field.get('required_access_grants'), grants)
                        section[target] = field

        # Fields merged under their column name: point every reference at it
        merged_dims = _rewrite_field_refs(merged_dims, applied_renames, view_name)
        merged_measures = _rewrite_field_refs(merged_measures, applied_renames, view_name)
        if applied_renames:
            notes.append("Renamed references: " + ', '.join(f"{a} → {b}" for a, b in applied_renames.items()))

        # Validate what this run wrote against the final field set
        names = set(merged_dims) | set(merged_measures)
        problems = find_self_and_missing_refs(
            {n: f for n, f in {**merged_dims, **merged_measures}.items() if n in written}, names
        )
        both = sorted(set(merged_dims) & set(merged_measures))
        if both:
            problems.append(f"names used by both a dimension and a measure: {', '.join(both)}")
        primary_keys = [n for n, d in merged_dims.items() if isinstance(d, dict) and d.get('primary_key') is True]
        if len(primary_keys) > 1:
            problems.append(f"more than one primary key after merging: {', '.join(primary_keys)}")
        if problems:
            raise MergeError("Merged view is invalid: " + '; '.join(problems))

        if merged_dims:
            merged['dimensions'] = merged_dims
        if merged_measures:
            merged['measures'] = merged_measures

        return merged, {'stats': stats, 'notes': notes, 'manual': manual, 'renames': dict(applied_renames)}

    # ------------------------------------------------------------------
    # Views
    # ------------------------------------------------------------------

    def render_view_yaml(
        self,
        view: OmniView,
        existing_view: Optional[Dict] = None,
        csv_fields: Optional[Set[str]] = None,
        field_to_column_mapping: Optional[Dict[str, str]] = None,
        view_name: Optional[str] = None,
        looker_fields: Optional[Set[str]] = None,
        field_grants: Optional[Dict[str, List[str]]] = None,
        unconverted: Optional[Dict[str, str]] = None,
        grant_columns: Optional[Dict[str, str]] = None
    ) -> Tuple[str, Dict[str, Any]]:
        """
        Render (and merge) an Omni view to YAML text

        Returns:
            (yaml text, info) - info has 'final' (the final view dict),
            'stats' and 'notes' (merge only)
        """
        view_dict = view_to_yaml_dict(view)
        info: Dict[str, Any] = {'stats': None, 'notes': [], 'manual': [], 'renames': {}}

        if existing_view is not None and csv_fields is not None:
            view_dict, merge_info = self.merge_with_existing_view(
                view_dict,
                existing_view,
                csv_fields,
                field_to_column_mapping or {},
                view_name,
                looker_fields=looker_fields,
                field_grants=field_grants,
                unconverted=unconverted,
                grant_columns=grant_columns
            )
            info.update(merge_info)
        info['final'] = view_dict

        if view_dict.get('sql'):
            formatted_sql = sqlparse.format(
                str(view_dict['sql']),
                reindent=True,
                keyword_case='upper',
                indent_width=2
            )
            view_dict['sql'] = LiteralScalarString(formatted_sql)

        view_dict = self._add_yaml_spacing(view_dict)
        view_dict = self._format_tags_inline(view_dict)

        for section in ('dimensions', 'measures'):
            for field_data in (view_dict.get(section) or {}).values():
                if isinstance(field_data, dict) and isinstance(field_data.get('sql'), str):
                    field_sql = field_data['sql']
                    if '\n' in field_sql or len(field_sql) > 80:
                        field_data['sql'] = LiteralScalarString(field_sql)

        return self._add_blank_lines_to_yaml(self._dump_yaml(view_dict)), info

    def generate_view_yaml(
        self,
        view: OmniView,
        output_filename: str,
        existing_view: Optional[Dict] = None,
        csv_fields: Optional[Set[str]] = None,
        field_to_column_mapping: Optional[Dict[str, str]] = None
    ) -> Path:
        """Generate an Omni view file, merging with an existing view if provided"""
        content, _ = self.render_view_yaml(view, existing_view, csv_fields, field_to_column_mapping)
        return self.write_file(self.output_dir / output_filename, content)

    def _format_tags_inline(self, data: Any, keys=('tags', 'required_access_grants')) -> Any:
        """Render short lists ('tags', 'required_access_grants') in flow style: [a, b]"""
        def convert_tags(obj):
            if isinstance(obj, dict):
                for key, value in obj.items():
                    if key in keys and isinstance(value, list) and all(isinstance(v, str) for v in value):
                        # Quote selectors such as -tag:table_key for parser portability
                        seq = CommentedSeq(DoubleQuotedScalarString(v) if ':' in v else v for v in value)
                        seq.fa.set_flow_style()
                        obj[key] = seq
                    elif isinstance(value, (dict, list)):
                        convert_tags(value)
            elif isinstance(obj, list):
                for item in obj:
                    if isinstance(item, (dict, list)):
                        convert_tags(item)

        convert_tags(data)
        return data

    def _add_yaml_spacing(self, view_dict: dict) -> dict:
        """
        Order top-level keys: known view keys first, then any other keys
        (preserved as-is, e.g. dbt or required_access_grants), then
        dimensions and measures.
        """
        ordered_dict = {}
        for key in _VIEW_KEY_ORDER:
            if key in view_dict:
                ordered_dict[key] = view_dict[key]
        for key, value in view_dict.items():
            if key not in ordered_dict and key not in ('dimensions', 'measures'):
                ordered_dict[key] = value
        if view_dict.get('dimensions'):
            ordered_dict['dimensions'] = view_dict['dimensions']
        if view_dict.get('measures'):
            ordered_dict['measures'] = view_dict['measures']
        return ordered_dict

    def _add_blank_lines_to_yaml(self, yaml_content: str) -> str:
        """Add blank lines before dimensions/measures and between their entries"""
        lines = yaml_content.split('\n')
        result = []
        in_fields = False

        for line in lines:
            if line.startswith('dimensions:') or line.startswith('measures:'):
                in_fields = True
                if result and result[-1].strip():
                    result.append('')
                result.append(line)
                continue
            if line and not line[0].isspace():
                in_fields = False

            stripped = line.lstrip()
            if not stripped:
                result.append(line)
                continue

            current_indent = len(line) - len(stripped)
            if in_fields and current_indent == 2:
                if result and result[-1].strip() and not result[-1].strip().endswith(':'):
                    result.append('')
            result.append(line)

        return '\n'.join(result)

    # ------------------------------------------------------------------
    # Topics, relationships, reports
    # ------------------------------------------------------------------

    def render_topic_yaml(self, topic: OmniTopic) -> str:
        """Render an Omni topic to YAML text"""
        topic_dict = topic_to_yaml_dict(topic)
        for key in ('ai_context', 'always_where_sql', 'always_having_sql'):
            if isinstance(topic_dict.get(key), str) and '\n' in topic_dict[key]:
                topic_dict[key] = LiteralScalarString(topic_dict[key])
        topic_dict = self._format_tags_inline(topic_dict, keys=('tags', 'required_access_grants', 'fields'))
        return self._dump_yaml(topic_dict)

    def generate_topic_yaml(self, topic: OmniTopic, output_filename: str) -> Path:
        """Generate an Omni topic file in the output directory"""
        return self.write_file(self.output_dir / output_filename, self.render_topic_yaml(topic))

    def generate_relationships_yaml(
        self,
        relationships: List[Dict[str, Any]],
        output_filename: str,
        header_lines: Optional[List[str]] = None
    ) -> Path:
        """Generate a relationships file (see render_relationships_yaml)"""
        return self.write_file(self.output_dir / output_filename,
                               self.render_relationships_yaml(relationships, header_lines))

    def render_relationships_yaml(
        self,
        relationships: List[Dict[str, Any]],
        header_lines: Optional[List[str]] = None
    ) -> str:
        """
        A relationships file: a YAML list of joins, preceded by an optional
        comment header (kept out of the data so the file stays valid)
        """
        rels = []
        for rel in relationships:
            rel = dict(rel)
            for key, value in rel.items():
                if isinstance(value, str) and '\n' in value:
                    rel[key] = LiteralScalarString(value)
            rels.append(rel)

        header = ''.join(f"# {line}\n" if line else "#\n" for line in (header_lines or []))
        return header + self._dump_yaml(rels)

    def generate_text_report(self, content: str, output_filename: str) -> Path:
        """Write a markdown report to the reports directory"""
        return self.write_file(self.reports_dir / output_filename, content)

    def generate_migration_report(self, report: MigrationReport, output_filename: str) -> Path:
        """Write a view migration report to the reports directory"""
        return self.generate_text_report(report.to_markdown(), output_filename)


class ConversionReportBuilder:
    """Builds detailed migration report from conversion results"""

    def __init__(
        self,
        looker_view_name: str,
        omni_view_name: str,
        phase: int
    ):
        self.looker_view_name = looker_view_name
        self.omni_view_name = omni_view_name
        self.phase = phase

        # Track conversion results
        self.dimension_results: Dict[str, ConversionResult] = {}
        self.measure_results: Dict[str, ConversionResult] = {}

        # Categorized changes
        self.changes_by_category: Dict[str, List[str]] = {
            'Canonical Mappings': [],
            'SQL Transformations': [],
            'Metadata Additions': [],
            'Format Mappings': [],
            'Other': []
        }

        # Risk tracking
        self.high_risks: List[str] = []
        self.medium_risks: List[str] = []
        self.warnings: List[str] = []

        # Not converted fields
        self.not_converted: List[Dict[str, str]] = []

        # Advanced features (templated views, access grants, etc.)
        self.advanced_features: List[Dict[str, str]] = []

        # Merge/enrich tracking
        self.merge_source: Optional[str] = None  # Path to existing Omni view used for merge
        self.merge_stats: Dict[str, int] = {}
        self.merge_notes: List[str] = []

        # Output file decision (set by the orchestrator)
        self.view_file_decision: Dict[str, str] = {
            'File Type': 'Standard table-based view (.view.yaml)',
            'Rationale': 'Lift-and-shift migration of the in-scope fields'
        }

    def set_view_file_decision(self, file_type: str, location: str, rationale: str):
        """Record which kind of Omni file was produced and where"""
        self.view_file_decision = {
            'File Type': file_type,
            'Location': location,
            'Rationale': rationale
        }

    def set_merge_info(self, source_path: str, stats: Dict[str, int], notes: List[str]):
        """Record merge statistics computed from the actual merge operations"""
        self.merge_source = source_path
        self.merge_stats = dict(stats)
        self.merge_notes = list(notes)

    def add_advanced_feature(self, feature: str, status: str, details: str, action_required: Optional[str] = None):
        """Track advanced features that need special handling"""
        feature_info = {
            'feature': feature,
            'status': status,
            'details': details
        }
        if action_required:
            feature_info['action_required'] = action_required
        self.advanced_features.append(feature_info)

    def add_dimension_result(self, field_name: str, result: ConversionResult):
        """Add dimension conversion result"""
        self.dimension_results[field_name] = result
        self._categorize_changes(field_name, result, is_dimension=True)
        self._track_risks(field_name, result)

    def add_measure_result(self, field_name: str, result: ConversionResult):
        """Add measure conversion result"""
        self.measure_results[field_name] = result
        self._categorize_changes(field_name, result, is_dimension=False)
        self._track_risks(field_name, result)

    def add_not_converted(self, field_name: str, reason: str):
        """Track fields that were not converted"""
        self.not_converted.append({
            'field': field_name,
            'reason': reason
        })

    def _categorize_changes(self, field_name: str, result: ConversionResult, is_dimension: bool):
        """Categorize changes into buckets"""
        field_type = 'dimension' if is_dimension else 'measure'

        for change in result.changes:
            if 'Canonical mapping' in change or 'Label mapping' in change:
                self.changes_by_category['Canonical Mappings'].append(
                    f"{field_name} ({field_type}): {change}"
                )
            elif '${TABLE}' in change or 'Cross-view reference' in change or 'Custom SQL' in change:
                self.changes_by_category['SQL Transformations'].append(
                    f"{field_name} ({field_type}): {change}"
                )
            elif 'ai_context' in change.lower() or 'synonym' in change.lower() or 'tag' in change.lower():
                self.changes_by_category['Metadata Additions'].append(
                    f"{field_name} ({field_type}): {change}"
                )
            elif 'Format mapping' in change:
                self.changes_by_category['Format Mappings'].append(
                    f"{field_name} ({field_type}): {change}"
                )
            else:
                self.changes_by_category['Other'].append(
                    f"{field_name} ({field_type}): {change}"
                )

    def _track_risks(self, field_name: str, result: ConversionResult):
        """Track risk levels and warnings"""
        if result.risk_level == 'HIGH':
            self.high_risks.append(
                f"{field_name}: {', '.join(result.warnings)}"
            )
        elif result.risk_level == 'MEDIUM':
            self.medium_risks.append(
                f"{field_name}: {', '.join(result.warnings)}"
            )

        # Collect all warnings
        for warning in result.warnings:
            self.warnings.append(f"{field_name}: {warning}")

    def build_report(self) -> MigrationReport:
        """Build final migration report"""

        # Generate PR summary
        # Only fields that were actually converted
        total_dims = sum(1 for r in self.dimension_results.values() if r.omni_field)
        total_measures = sum(1 for r in self.measure_results.values() if r.omni_field)
        total_changes = sum(len(changes) for changes in self.changes_by_category.values())

        pr_summary = (
            f"Migrated {self.looker_view_name} from Looker to Omni as {self.omni_view_name}. "
            f"Converted {total_dims} dimensions and {total_measures} measures with {total_changes} total changes. "
        )

        if self.high_risks:
            pr_summary += f"{len(self.high_risks)} HIGH RISK items require manual review. "
        if self.medium_risks:
            pr_summary += f"{len(self.medium_risks)} MEDIUM RISK items may need verification. "

        pr_summary += f"Migration phase: {self.phase}."

        view_decision = dict(self.view_file_decision)

        # Metadata for standardization
        metadata_items = []
        for field_name, result in {**self.dimension_results, **self.measure_results}.items():
            if result.omni_field and hasattr(result.omni_field, 'ai_context'):
                if result.omni_field.ai_context:
                    metadata_items.append(
                        f"{field_name}: Added ai_context for AI assistant"
                    )
            if result.omni_field and hasattr(result.omni_field, 'synonyms'):
                if result.omni_field.synonyms:
                    metadata_items.append(
                        f"{field_name}: Added synonyms {result.omni_field.synonyms}"
                    )

        # Changes made (with counts)
        changes_made = {}
        for category, items in self.changes_by_category.items():
            if items:
                changes_made[category] = items

        # Redundancy candidates (fields that might be duplicates or legacy)
        redundancy_candidates = []
        for field_name, result in self.dimension_results.items():
            if result.omni_field and result.omni_field.hidden:
                redundancy_candidates.append(
                    f"{field_name}: Hidden field marked as legacy/deprecated"
                )

        # Notes/Risks
        notes_risks = []

        # High risks
        for risk in self.high_risks:
            notes_risks.append({
                'level': 'HIGH RISK',
                'note': risk
            })

        # Medium risks
        for risk in self.medium_risks:
            notes_risks.append({
                'level': 'MEDIUM RISK',
                'note': risk
            })

        # Dependencies
        all_dependencies = set()
        for result in {**self.dimension_results, **self.measure_results}.values():
            all_dependencies.update(result.dependencies)

        if all_dependencies:
            notes_risks.append({
                'level': 'INFO',
                'note': f"Field dependencies detected: {', '.join(sorted(all_dependencies))}. Verify all dependencies are included in scope."
            })

        # Build merge info if we merged with an existing view
        merge_info = None
        if self.merge_source:
            merge_info = {
                'source': self.merge_source,
                'notes': self.merge_notes,
                **self.merge_stats
            }
            st = self.merge_stats
            pr_summary = (
                f"Merged Looker metadata from {self.looker_view_name} into existing Omni view {self.omni_view_name}. "
                f"Updated {st.get('enriched', 0)} existing fields, merged {st.get('renamed', 0)} renamed fields, "
                f"added {st.get('added', 0)} fields (+{st.get('added_hidden', 0)} hidden), "
                f"hid {st.get('hidden_out_of_scope', 0)} out-of-scope fields, "
                f"kept {st.get('kept_previous_migration', 0)} fields from earlier runs. "
            )
            if self.high_risks:
                pr_summary += f"{len(self.high_risks)} HIGH RISK items require manual review. "
            if self.medium_risks:
                pr_summary += f"{len(self.medium_risks)} MEDIUM RISK items may need verification. "
            pr_summary += f"Migration phase: {self.phase}."

        # Create report
        report = MigrationReport(
            looker_view=self.looker_view_name,
            omni_view=self.omni_view_name,
            phase=self.phase,
            pr_summary=pr_summary,
            view_file_decision=view_decision,
            metadata_for_standardization=metadata_items,
            changes_made=changes_made,
            redundancy_candidates=redundancy_candidates,
            not_converted=self.not_converted,
            notes_risks=notes_risks,
            advanced_features=self.advanced_features if self.advanced_features else None,
            merge_info=merge_info
        )

        return report
