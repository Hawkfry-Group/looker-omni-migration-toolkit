"""
Rule Applier - Core Looker to Omni Field Conversion Engine

This module transforms Looker LookML field definitions into Omni YAML fields,
applying migration rules, optional canonical label mappings, and metadata
generation.

## Purpose

Handles the field-level conversion from Looker to Omni:
1. Dimension conversion (type removal, SQL cleaning)
2. Measure conversion (type -> aggregate_type, filters, format)
3. Dimension group conversion (dimension_group -> one date dimension)
4. Canonical label mapping (optional, from `--config`)
5. Metadata generation (ai_context, synonyms, tags)
6. Dependency detection (same-view `${field}` references)
7. Tag normalization (e.g. "Phase 0" -> phase_0)

Anything the converter cannot translate faithfully is NOT emitted. The field
gets `omni_field=None`, `risk_level="HIGH"` and a warning that starts with
`MANUAL ACTION REQUIRED`, so the orchestrator can list it in the report.

## Dimension Conversion

**Looker:**
```lkml
dimension: id {
  primary_key: yes
  type: number
  sql: ${TABLE}.id ;;
}

dimension: full_name {
  type: string
  sql: CONCAT(${first_name}, ' ', ${last_name}) ;;
}
```

**Omni:**
```yaml
id:
  label: Id
  primary_key: true
  tags: [id, mig_phase_0, verified]

full_name:
  sql: CONCAT(${first_name}, ' ', ${last_name})
  label: Full Name
  tags: [name, pii, mig_phase_0, verified]
```

- `type:` is dropped (Omni infers it from the warehouse column)
- `sql: ${TABLE}.<field name>` is omitted entirely (Omni maps the column)

## SQL Expression Rules

| Looker | Omni | Notes |
|--------|------|-------|
| `${TABLE}.column` / `<this_view>.column`, and another dimension's sql is exactly `${TABLE}.column` | `${that_dimension}` | Equivalence proven |
| `${TABLE}.column`, otherwise (incl. the field's own column) | `column` | Raw warehouse column; no self-reference |
| `${field}` | `${field}` | Same-view reference, recorded as a dependency |
| `${view.field}` | `${view.field}` | Cross-view reference, unchanged; the view must be joined in the topic |
| `${TABLE}."col"`, `${TABLE}` not followed by a column, `${...}` inside quotes/comments | not converted | MANUAL ACTION REQUIRED |
| `{% ... %}` / `{{ ... }}` | not converted | Liquid has no Omni equivalent: MANUAL ACTION REQUIRED |

String literals, quoted identifiers and comments are never rewritten.

## Measure Conversion

| Looker `type` | Omni |
|---------------|------|
| count, count_distinct, sum, average, min, max, median | `aggregate_type` of the same name |
| list | `aggregate_type: list` on `${list_field}` |
| percentile | `aggregate_type: percentile` + `percentile: N` |
| number, string, yesno, date | Calculated measure: `sql` kept, no `aggregate_type` |
| anything else (running_total, percent_of_total, sum_distinct, period_over_period, ...) | Not converted, MANUAL ACTION REQUIRED |

Measure `filters:` are translated using the grammar of the filtered field's
Looker type (see `translate_filter_expression`): number fields accept
values, lists, comparisons, `<>`/`!=`, NULL/NOT NULL; string fields accept
values, lists, all-negated lists (`-value`) and NULL (values stay strings,
even `00123`); yes/no fields accept yes/no. Anything else (wildcards,
ranges, relative dates such as "90 days", `NOT x`, a leading `-` on numbers,
filters on dimension groups, measures or fields whose type cannot be
resolved) stops the measure from being converted and is reported.

`value_format_name` values in `FORMAT_MAPPINGS` are mapped (on dimensions and
measures), e.g. decimal_0 -> number_0, usd -> usdcurrency_2. Unmapped names and custom `value_format` strings are not emitted;
they are reported (MEDIUM risk) so a format can be set by hand.

## Dimension Group Conversion

A `type: time` dimension_group becomes a single Omni dimension named after
the group. Looker `timeframes:` are not emitted (Omni builds timeframes for
date/timestamp columns itself); the original list is recorded in the
report. Not converted (reported): `type: duration`, `datatype` other than
timestamp/datetime/date (e.g. epoch, yyyymmdd), `convert_tz: no`, `sql_date`.

## Not Migrated

`drill_fields`, `link`, `html`, `case`, `tiers` and the dimension types
`tier`, `location`, `distance` are not converted. `case`/`tiers`/those types
block the field; `drill_fields`/`link`/`html` are dropped and noted.

## Canonical Label Mapping

Optional and empty by default (see `config/migration_config.example.yaml`).
Configured terms are replaced, as whole words, in labels only. A dimension,
dimension group or measure whose name contains the new term as a whole
`_`-separated token also gets the old-term spelling as a synonym.

## Tag Normalization

| Input | Output |
|-------|--------|
| `Phase 0` | `phase_0` |
| `Verified` | `verified` |
| `Has-Special Chars!` | `has_special_chars` |

## Usage

```python
from lib.rule_applier import RuleApplier

applier = RuleApplier(phase=0, verified=True, view_name='users',
                      column_fields={'id': 'id', 'first_name': 'first_name'},
                      field_types={'id': 'number', 'first_name': 'string'})
result = applier.convert_dimension('id', {
    'sql': '${TABLE}.id', 'type': 'number', 'primary_key': 'yes'
})
print(result.risk_level, result.changes, result.warnings, result.dependencies)
omni_dim = result.omni_field
```

## Risk Levels

- **LOW**: Standard conversion, no warnings
- **MEDIUM**: Converted, but something needs review (unmapped format)
- **HIGH**: Not converted (unsupported construct or validation error)
"""

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from .config import DEFAULT_CANONICAL_MAPPINGS
from .omni_models import AggregateType, OmniDimension, OmniMeasure


def normalize_tag(tag: str) -> str:
    """
    Convert tag to snake_case format for Omni compatibility.

    Examples:
        'Phase 0' -> 'phase_0'
        'Verified' -> 'verified'
        'Has-Special Chars!' -> 'has_special_chars'
    """
    tag = tag.lower()
    tag = re.sub(r'[:\s\-]+', '_', tag)
    tag = re.sub(r'[^a-z0-9_]', '', tag)
    tag = re.sub(r'_+', '_', tag)
    return tag.strip('_')


def phase_tag(phase: int) -> str:
    """Migration phase tag, e.g. mig_phase_0"""
    return normalize_tag(f'mig phase {phase}')


def status_tag(verified: bool) -> str:
    """Verification tag: verified or unverified"""
    return 'verified' if verified else 'unverified'


# Canonical term mappings are organization-specific and EMPTY by default.
# Define them in a config file and pass it with --config.
CANONICAL_MAPPINGS = dict(DEFAULT_CANONICAL_MAPPINGS)

# Looker value_format_name to Omni format mappings
# (Omni named formats: https://docs.omni.co/modeling/models/format-values;
# a bare `number` has no fixed precision, so decimals are always explicit)
FORMAT_MAPPINGS = {
    **{f'decimal_{n}': f'number_{n}' for n in range(5)},
    **{f'percent_{n}': f'percent_{n}' for n in range(5)},
    'usd': 'usdcurrency_2',
    'usd_0': 'usdcurrency_0',
    'eur': 'eurcurrency_2',
    'eur_0': 'eurcurrency_0',
    'gbp': 'gbpcurrency_2',
    'gbp_0': 'gbpcurrency_0',
    'id': 'id',
}

# Looker measure type to Omni aggregate_type
AGGREGATE_TYPE_MAPPINGS = {
    'count': AggregateType.COUNT,
    'count_distinct': AggregateType.COUNT_DISTINCT,
    'sum': AggregateType.SUM,
    'average': AggregateType.AVERAGE,
    'min': AggregateType.MIN,
    'max': AggregateType.MAX,
    'median': AggregateType.MEDIAN,
    'list': AggregateType.LIST,
    'percentile': AggregateType.PERCENTILE,
}

# Looker measure types whose sql is already a complete (aggregated) expression
CALCULATED_MEASURE_TYPES = {'number', 'string', 'yesno', 'date'}

# Dimension types / parameters with no automatic Omni translation
UNSUPPORTED_DIMENSION_TYPES = {'tier', 'location', 'distance'}
UNSUPPORTED_DIMENSION_PARAMS = ('case', 'tiers')

# Field parameters that are dropped (and noted) rather than converted
DROPPED_FIELD_PARAMS = ('drill_fields', 'link', 'links', 'html', 'map_layer_name')

MANUAL = "MANUAL ACTION REQUIRED"

_IDENT = r'[a-zA-Z_][a-zA-Z0-9_]*'
_LIQUID_RE = re.compile(r'\{%.*?%\}|\{\{.*?\}\}', re.DOTALL)
_COMPARISON_KEYS = {
    '>=': 'greater_than_or_equal_to',
    '<=': 'less_than_or_equal_to',
    '>': 'greater_than',
    '<': 'less_than',
}


@dataclass
class ConversionResult:
    """Result of converting a Looker field to Omni"""
    omni_field: Optional[Any]  # OmniDimension | OmniMeasure | None
    changes: List[str]  # What changed
    warnings: List[str]  # Potential issues
    dependencies: Set[str]  # Same-view fields referenced via ${field}
    risk_level: str  # "LOW", "MEDIUM", "HIGH"
    db_column_name: Optional[str] = None  # Warehouse column when it differs from the field name
    cross_view_refs: Set[str] = field(default_factory=set)  # "view.field" references
    dropped_params: List[str] = field(default_factory=list)  # e.g. drill_fields, html


def _sentence(text: str) -> str:
    """Terminate a description with a period so ai_context parts read cleanly"""
    text = text.strip()
    return text if not text or text[-1] in '.!?' else text + '.'


def has_liquid(text: Optional[str]) -> bool:
    """True if the text contains Liquid tags or output blocks"""
    return bool(text) and bool(_LIQUID_RE.search(text))


class UnsafeSQL(Exception):
    """Raised when SQL cannot be converted without guessing"""


# String literals, quoted identifiers and comments. Field references are never
# rewritten inside these; finding one there blocks the conversion.
_SQL_SPAN_RE = re.compile(
    r"""('(?:[^'\\]|\\.|'')*'|"(?:[^"\\]|\\.)*"|`[^`]*`|--[^\n]*|/\*.*?\*/)""",
    re.DOTALL,
)


def split_sql(sql: str) -> List[Tuple[str, bool]]:
    """Split SQL into (text, is_code) segments; quoted text and comments are not code"""
    parts = _SQL_SPAN_RE.split(sql)
    return [(part, i % 2 == 0) for i, part in enumerate(parts) if part]


def filter_kind(looker_type: Optional[str]) -> Optional[str]:
    """
    Filter grammar to use for a field, from its Looker type:
    'number', 'string', 'yesno', or None (no automatic translation).
    """
    if looker_type in (None, '', 'string', 'zipcode'):
        return 'string'
    if looker_type in ('number', 'int'):
        return 'number'
    if looker_type == 'yesno':
        return 'yesno'
    return None


_NUMBER = r'\d+(?:\.\d+)?'
_SIGNED_NUMBER = r'-?\d+(?:\.\d+)?'
# Words with special meaning in Looker string/date filter expressions, plus
# date units: values containing them are never treated as literals.
_STRING_KEYWORDS = re.compile(
    r'\b(NOT|NULL|EMPTY|BEFORE|AFTER|AGO|LAST|NEXT|THIS|THROUGH|TO|FOR|NOW|TODAY|YESTERDAY|TOMORROW|'
    r'SECONDS?|MINUTES?|HOURS?|DAYS?|WEEKS?|MONTHS?|QUARTERS?|YEARS?)\b',
    re.IGNORECASE,
)


def _number(token: str) -> Any:
    return float(token) if '.' in token else int(token)


def translate_filter_expression(expression: Any, kind: Optional[str] = 'string') -> Optional[Dict[str, Any]]:
    """
    Translate a Looker filter expression into an Omni filter condition, using
    the grammar of the filtered field's type (see `filter_kind`).

    Supported (anything else returns None and must be reported):

    - number: `5`, `1.5`, `1, 3, 5`, `>=5` / `>5` / `<=5` / `<5` (signed
      numbers allowed after the operator), `<>5` / `!=5`, `NULL`, `NOT NULL`.
      A leading `-` is rejected (Looker reads `-5` as "not 5").
    - string: one value or a comma list of values -> `is`; every value
      prefixed with `-` -> `not`; `NULL` -> `is: null`. Values containing
      `%`, `_`, `^`, quotes, brackets or Looker keywords (NOT, EMPTY, date
      words such as "90 days") are rejected. Values always stay strings.
    - yesno: `yes` / `no` (any case).
    """
    if kind is None:
        return None
    if isinstance(expression, bool):
        return {'is': expression} if kind == 'yesno' else None
    if isinstance(expression, (int, float)):
        expression = str(expression)
    if not isinstance(expression, str):
        return None
    expr = expression.strip()
    if not expr:
        return None

    if kind == 'yesno':
        if expr.lower() == 'yes':
            return {'is': True}
        if expr.lower() == 'no':
            return {'is': False}
        return None

    if kind == 'number':
        if expr.upper() == 'NULL':
            return {'is': None}
        if re.fullmatch(r'NOT\s+NULL', expr, re.IGNORECASE):
            return {'not': None}
        comparison = re.fullmatch(r'(>=|<=|>|<)\s*(' + _SIGNED_NUMBER + r')', expr)
        if comparison:
            return {_COMPARISON_KEYS[comparison.group(1)]: _number(comparison.group(2))}
        not_equal = re.fullmatch(r'(?:<>|!=)\s*(' + _SIGNED_NUMBER + r')', expr)
        if not_equal:
            return {'not': _number(not_equal.group(1))}
        tokens = [t.strip() for t in expr.split(',')]
        if all(re.fullmatch(_NUMBER, t) for t in tokens):
            values = [_number(t) for t in tokens]
            return {'is': values[0] if len(values) == 1 else values}
        return None

    # string
    if expr.upper() == 'NULL':
        return {'is': None}
    if re.search(r'[%_^"\'`\[\](){}<>=!]', expr):
        return None
    tokens = [t.strip() for t in expr.split(',')]
    if any(not t for t in tokens):
        return None
    negated = [t.startswith('-') for t in tokens]
    if all(negated):
        values = [t[1:].strip() for t in tokens]
        key = 'not'
    elif not any(negated):
        values = tokens
        key = 'is'
    else:
        return None
    if any(not v or v.startswith('-') or _STRING_KEYWORDS.search(v) for v in values):
        return None
    return {key: values[0] if len(values) == 1 else values}


def extract_measure_filters(looker_measure: Dict) -> List[Tuple[str, Any]]:
    """
    Normalize the lkml representations of measure filters to (field, expression)

    lkml parses `filters: [status: "Complete"]` as
    `filters__all: [[{"status": "Complete"}]]`; the older block form
    `filters: {field: status value: "Complete"}` is also accepted.
    """
    pairs: List[Tuple[str, Any]] = []

    for group in looker_measure.get('filters__all', []) or []:
        items = group if isinstance(group, list) else [group]
        for item in items:
            if isinstance(item, dict):
                for key, value in item.items():
                    pairs.append((key, value))

    legacy = looker_measure.get('filters')
    if isinstance(legacy, dict):
        legacy = [legacy]
    for item in legacy or []:
        if isinstance(item, dict):
            if 'field' in item:
                pairs.append((item['field'], item.get('value', '')))
            else:
                pairs.extend(item.items())

    return pairs


class RuleApplier:
    """
    Applies migration rules to convert Looker fields to Omni format

    Handles:
    - Dimension conversion (type removal, SQL cleaning)
    - Measure conversion (type -> aggregate_type, filters, format mapping)
    - Dimension group conversion (dimension_group -> one date dimension)
    - Canonical label mapping (optional, configured business-term renames)
    - Metadata generation (ai_context, synonyms, tags)
    - Dependency detection (same-view ${field} references)
    """

    def __init__(
        self,
        phase: int,
        verified: bool = True,
        view_name: Optional[str] = None,
        canonical_mappings: Optional[Dict[str, str]] = None,
        column_fields: Optional[Dict[str, str]] = None,
        field_types: Optional[Dict[str, str]] = None,
        type_resolver: Optional[Callable[[str, str], Optional[str]]] = None
    ):
        self.phase = phase
        self.verified = verified
        self.view_name = view_name
        # {warehouse column: field name} for fields whose sql is exactly that
        # column. Only these let `${TABLE}.column` become `${field}`.
        self.column_fields: Dict[str, str] = dict(column_fields or {})
        # {field name: Looker type} for this view ('time' for dimension
        # groups, 'measure' for measures); used to pick the filter grammar
        self.field_types: Dict[str, str] = dict(field_types or {})
        # Looks up the Looker type of `view.field` in other views
        self.type_resolver = type_resolver
        # Organization-specific term mappings; empty unless configured
        self.canonical_mappings = dict(
            canonical_mappings if canonical_mappings is not None else CANONICAL_MAPPINGS
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _migration_tags(self) -> List[str]:
        return [phase_tag(self.phase), status_tag(self.verified)]

    def apply_canonical_mapping(self, text: str) -> Tuple[str, List[str]]:
        """
        Apply canonical term mappings to a label.

        Terms are matched as whole words, case-insensitively, and the
        replacement follows the capitalization of the first letter of the
        matched word. A mapping is skipped when its target term is already
        present (avoids double-mapping).

        Returns: (mapped_text, list of applied mappings)
        """
        changes = []
        result = text

        for old_term, new_term in self.canonical_mappings.items():
            if not old_term or not new_term:
                continue
            if re.search(rf'\b{re.escape(new_term)}\b', result, re.IGNORECASE):
                continue
            pattern = rf'\b{re.escape(old_term)}\b'
            if re.search(pattern, result, re.IGNORECASE):
                def _replace(match, new_term=new_term):
                    original = match.group(0)
                    if original[:1].isupper():
                        return new_term[:1].upper() + new_term[1:]
                    return new_term
                result = re.sub(pattern, _replace, result, flags=re.IGNORECASE)
                changes.append(f"Canonical mapping: {old_term} → {new_term}")

        return result, changes

    def canonical_synonyms(self, field_name: str) -> List[str]:
        """
        Old-term spellings of a field name that contains a new canonical term
        as a whole `_`-separated token (case-insensitive): with `sku: product`,
        `product_code` gets the synonym `sku_code`.
        """
        synonyms = []
        for legacy_term, canonical_term in self.canonical_mappings.items():
            if not legacy_term or not canonical_term:
                continue
            pattern = rf'(?<![A-Za-z0-9]){re.escape(canonical_term)}(?![A-Za-z0-9])'
            legacy_name = re.sub(pattern, legacy_term, field_name, flags=re.IGNORECASE)
            if legacy_name != field_name:
                synonyms.append(legacy_name)
        return synonyms

    def clean_sql_expression(
        self,
        sql: str,
        field_name: Optional[str] = None
    ) -> Tuple[str, List[str], List[str]]:
        """
        Clean a Looker SQL expression for Omni

        Only code is rewritten; string literals, quoted identifiers and
        comments are left untouched. Transformations:

        - `${TABLE}.column` (and `<this_view>.column`) -> `${field}` when a
          field of the view is defined as exactly that column (equivalence
          proven), otherwise the raw column name `column`. Never produces a
          self-reference.
        - `${field}` and `${view.field}` are left unchanged

        Raises UnsafeSQL (the field must be migrated by hand) when:
        - a `${...}` reference appears inside a string literal, quoted
          identifier or comment
        - a `${TABLE}` remains that is not followed by a plain column name,
          e.g. `${TABLE}."price"` or a bare `FROM ${TABLE}` in a subquery

        Returns: (cleaned_sql, changes, warnings)
        """
        changes: List[str] = []
        warnings: List[str] = []

        def _column(match):
            column = match.group(1)
            target = self.column_fields.get(column)
            if target and target != field_name:
                return '${' + target + '}'
            return column

        out = []
        rewrote_table = rewrote_view = False
        for text, is_code in split_sql(sql.strip()):
            if not is_code:
                if '${' in text:
                    raise UnsafeSQL(
                        f"field reference inside a quoted string, quoted identifier or comment: {text.strip()}"
                    )
                out.append(text)
                continue
            if '${TABLE}.' in text:
                text = re.sub(r'\$\{TABLE\}\.(' + _IDENT + r')\b', _column, text)
                rewrote_table = True
            if '${TABLE}' in text:
                raise UnsafeSQL(
                    "${TABLE} is not followed by a plain column name (quoted identifier or a "
                    "subquery reading the view's own table)"
                )
            if self.view_name:
                bare_ref = r'(?<![\w${.])' + re.escape(self.view_name) + r'\.(' + _IDENT + r')\b'
                if re.search(bare_ref, text):
                    text = re.sub(bare_ref, _column, text)
                    rewrote_view = True
            out.append(text)

        if rewrote_table:
            changes.append("Converted ${TABLE}.column (field reference only when a field is exactly that column, else raw column)")
        if rewrote_view:
            changes.append(f"Converted {self.view_name}.column the same way as ${{TABLE}}.column")
        return ''.join(out), changes, warnings

    def extract_db_column_name(self, sql: Optional[str], field_name: str) -> Optional[str]:
        """
        Return the warehouse column a field reads, when it differs from the field name

        Only a direct column reference counts: `${TABLE}.column` or a bare
        identifier. Field references (`${other}`), literals and expressions
        return None.
        """
        if not sql:
            return None

        text = sql.strip()
        match = re.fullmatch(r'\$\{TABLE\}\.(' + _IDENT + r')', text)
        if not match:
            match = re.fullmatch(r'(' + _IDENT + r')', text)
        if not match:
            return None

        column = match.group(1)
        if column.upper() in {'NULL', 'TRUE', 'FALSE'}:
            return None
        return column if column != field_name else None

    def extract_dependencies(self, sql: str) -> Set[str]:
        """Same-view `${field}` references (excluding ${TABLE})"""
        dependencies = set()
        for match in re.finditer(r'\$\{(' + _IDENT + r')\}', sql or ''):
            if match.group(1) != 'TABLE':
                dependencies.add(match.group(1))
        return dependencies

    def extract_cross_view_references(self, sql: str) -> Set[str]:
        """Cross-view `${view.field}` references, returned as 'view.field'"""
        refs = set()
        for match in re.finditer(r'\$\{(' + _IDENT + r')\.(' + _IDENT + r')\}', sql or ''):
            if match.group(1) != 'TABLE':
                refs.add(f"{match.group(1)}.{match.group(2)}")
        return refs

    @staticmethod
    def _apply_format(looker_field: Dict, kwargs: Dict, changes: List[str], warnings: List[str], risk_level: str) -> str:
        """
        Map value_format_name via FORMAT_MAPPINGS. Unmapped names and custom
        value_format strings are NOT emitted; they are reported for review.
        """
        format_name = looker_field.get('value_format_name')
        format_string = looker_field.get('value_format')
        if format_name and format_name in FORMAT_MAPPINGS:
            kwargs['format'] = FORMAT_MAPPINGS[format_name]
            changes.append(f"Format mapping: {format_name} → {FORMAT_MAPPINGS[format_name]}")
            return risk_level
        if format_name:
            warnings.append(f"value_format_name '{format_name}' has no known Omni equivalent; not migrated, set a format manually")
        elif format_string:
            warnings.append(f"Custom value_format '{format_string}' not migrated; set an Omni format manually")
        else:
            return risk_level
        return "MEDIUM" if risk_level == "LOW" else risk_level

    def resolve_field_type(self, field_ref: str) -> Optional[str]:
        """Looker type of a filtered field (same-view name or view.field), None if unknown"""
        if '.' in field_ref:
            view, name = field_ref.split('.', 1)
            if view != self.view_name:
                return self.type_resolver(view, name) if self.type_resolver else None
            field_ref = name
        return self.field_types.get(field_ref)

    def _dropped_params(self, looker_field: Dict) -> List[str]:
        return [p for p in DROPPED_FIELD_PARAMS if looker_field.get(p)]

    def _failed(self, changes, warnings, dependencies=None, cross=None, dropped=None) -> ConversionResult:
        return ConversionResult(
            omni_field=None,
            changes=changes,
            warnings=warnings,
            dependencies=dependencies or set(),
            risk_level="HIGH",
            cross_view_refs=cross or set(),
            dropped_params=dropped or []
        )

    # ------------------------------------------------------------------
    # Metadata
    # ------------------------------------------------------------------

    def generate_dimension_metadata(
        self,
        field_name: str,
        looker_dim: Dict,
        db_column_name: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Generate ai_context, synonyms and tags for an Omni dimension
        """
        metadata: Dict[str, Any] = {}

        dim_type = looker_dim.get('type', 'string')
        description = looker_dim.get('description', '')

        ai_context_parts = []
        if looker_dim.get('primary_key') == 'yes':
            ai_context_parts.append("Primary key for this view.")
        if description:
            ai_context_parts.append(_sentence(description))
        if dim_type in ['number', 'int']:
            ai_context_parts.append("Numeric identifier or value.")
        elif dim_type in ['string', 'text']:
            ai_context_parts.append("Text field for filtering and display.")
        elif dim_type == 'yesno':
            ai_context_parts.append("Yes/no flag.")
        if ai_context_parts:
            metadata['ai_context'] = ' '.join(ai_context_parts)

        synonyms = []
        # A field whose name uses the new canonical term keeps the old-term
        # spelling as a synonym, so the pre-migration vocabulary still finds it.
        synonyms.extend(self.canonical_synonyms(field_name))
        # A field that reads a differently named column gets the column name
        if db_column_name and db_column_name != field_name:
            synonyms.append(db_column_name)
        if synonyms:
            metadata['synonyms'] = list(dict.fromkeys(synonyms))

        tags = []
        if looker_dim.get('primary_key') == 'yes':
            tags.append('id')
        if 'name' in field_name or 'title' in field_name:
            tags.append('name')
        if dim_type in ['number', 'int'] and ('id' in field_name or 'key' in field_name):
            tags.append('id')
        if any(pii_term in field_name.lower() for pii_term in ['email', 'phone', 'ssn', 'name']):
            tags.append('pii')
        if dim_type in ['time', 'date', 'datetime', 'timestamp']:
            tags.append('date')
        tags.extend(self._migration_tags())

        metadata['tags'] = list(dict.fromkeys(normalize_tag(tag) for tag in tags))
        return metadata

    def generate_measure_metadata(
        self,
        field_name: str,
        looker_measure: Dict
    ) -> Dict[str, Any]:
        """Generate ai_context and tags for an Omni measure"""
        metadata: Dict[str, Any] = {}

        measure_type = looker_measure.get('type', 'count')
        description = looker_measure.get('description', '')

        ai_context_parts = []
        if description:
            ai_context_parts.append(_sentence(description))
        if measure_type in ('count', 'count_distinct'):
            ai_context_parts.append("Use for counting records or distinct values.")
        elif measure_type == 'sum':
            ai_context_parts.append("Aggregate sum across records.")
        elif measure_type in ['average', 'avg']:
            ai_context_parts.append("Average value across records.")
        if ai_context_parts:
            metadata['ai_context'] = ' '.join(ai_context_parts)

        tags = ['metric'] + self._migration_tags()
        metadata['tags'] = list(dict.fromkeys(normalize_tag(tag) for tag in tags))
        return metadata

    # ------------------------------------------------------------------
    # Conversions
    # ------------------------------------------------------------------

    def convert_dimension(
        self,
        field_name: str,
        looker_dim: Dict
    ) -> ConversionResult:
        """
        Convert a Looker dimension to an Omni dimension

        Args:
            field_name: Dimension name from Looker
            looker_dim: Looker dimension definition dict

        Returns:
            ConversionResult with OmniDimension (or None if not convertible)
        """
        changes: List[str] = []
        warnings: List[str] = []
        risk_level = "LOW"
        dropped = self._dropped_params(looker_dim)

        dim_type = looker_dim.get('type')
        if dim_type in UNSUPPORTED_DIMENSION_TYPES:
            warnings.append(f"{MANUAL}: dimension type '{dim_type}' has no automatic Omni conversion")
            return self._failed(changes, warnings, dropped=dropped)
        for param in UNSUPPORTED_DIMENSION_PARAMS:
            if looker_dim.get(param):
                warnings.append(f"{MANUAL}: '{param}' dimensions must be rewritten as a CASE expression")
                return self._failed(changes, warnings, dropped=dropped)

        # Looker defaults sql to ${TABLE}.<field name>
        sql_raw = looker_dim.get('sql') or '${TABLE}.' + field_name
        if has_liquid(sql_raw):
            warnings.append(f"{MANUAL}: sql uses Liquid templating, which Omni does not support")
            return self._failed(changes, warnings, dropped=dropped)

        db_column_name = self.extract_db_column_name(sql_raw, field_name)

        try:
            sql_clean, sql_changes, sql_warnings = self.clean_sql_expression(sql_raw, field_name)
        except UnsafeSQL as e:
            warnings.append(f"{MANUAL}: {e}")
            return self._failed(changes, warnings, dropped=dropped)
        changes.extend(sql_changes)
        warnings.extend(sql_warnings)
        if sql_warnings:
            risk_level = "MEDIUM"

        dependencies = self.extract_dependencies(sql_clean) - {field_name}
        cross_view_refs = self.extract_cross_view_references(sql_clean)

        label = looker_dim.get('label', field_name.replace('_', ' ').title())
        label_mapped, label_mapping_changes = self.apply_canonical_mapping(label)
        if label_mapping_changes:
            changes.append(f"Label mapping: {label} → {label_mapped}")
            label = label_mapped

        description = looker_dim.get('description', f"{label} dimension")
        metadata = self.generate_dimension_metadata(field_name, looker_dim, db_column_name)

        # Omit sql when it is just the field's own column (Omni infers it)
        is_standard_sql = sql_clean in (field_name, f'${{{field_name}}}')

        dim_kwargs: Dict[str, Any] = {
            'label': label,
            'description': description,
            'tags': metadata.get('tags', [])
        }
        if not is_standard_sql:
            dim_kwargs['sql'] = sql_clean
        else:
            changes.append("Standard SQL omitted (Omni infers from schema)")

        if looker_dim.get('primary_key') == 'yes':
            dim_kwargs['primary_key'] = True
            changes.append("Marked as primary_key: true")
        if looker_dim.get('hidden') == 'yes':
            dim_kwargs['hidden'] = True
            changes.append("Marked as hidden: true")
        if looker_dim.get('group_label'):
            dim_kwargs['group_label'] = looker_dim['group_label']
        risk_level = self._apply_format(looker_dim, dim_kwargs, changes, warnings, risk_level)
        if 'ai_context' in metadata:
            dim_kwargs['ai_context'] = metadata['ai_context']
        if 'synonyms' in metadata:
            dim_kwargs['synonyms'] = metadata['synonyms']
        if 'type' in looker_dim:
            changes.append(f"Removed type: {looker_dim['type']} (Omni infers from SQL)")
        for param in dropped:
            changes.append(f"Not migrated: {param}")

        try:
            omni_dim = OmniDimension(**dim_kwargs)
        except Exception as e:
            warnings.append(f"Validation error: {e}")
            return self._failed(changes, warnings, dependencies, cross_view_refs, dropped)

        return ConversionResult(
            omni_field=omni_dim,
            changes=changes,
            warnings=warnings,
            dependencies=dependencies,
            risk_level=risk_level,
            db_column_name=db_column_name,
            cross_view_refs=cross_view_refs,
            dropped_params=dropped
        )

    def convert_measure(
        self,
        field_name: str,
        looker_measure: Dict,
        all_dimensions: Optional[Set[str]] = None,
        all_measures: Optional[Set[str]] = None
    ) -> ConversionResult:
        """
        Convert a Looker measure to an Omni measure

        Args:
            field_name: Measure name
            looker_measure: Looker measure definition
            all_dimensions: Dimension names in the view (used for yes/no filters)
            all_measures: Measure names in the view
        """
        changes: List[str] = []
        warnings: List[str] = []
        risk_level = "LOW"
        dropped = self._dropped_params(looker_measure)
        all_dimensions = all_dimensions or set()
        all_measures = all_measures or set()

        looker_type = looker_measure.get('type') or 'number'
        is_calculated = looker_type in CALCULATED_MEASURE_TYPES
        if not is_calculated and looker_type not in AGGREGATE_TYPE_MAPPINGS:
            warnings.append(
                f"{MANUAL}: Looker measure type '{looker_type}' has no automatic Omni conversion"
            )
            return self._failed(changes, warnings, dropped=dropped)

        # list measures aggregate a dimension named by list_field
        sql_raw = looker_measure.get('sql') or ''
        if looker_type == 'list':
            list_field = looker_measure.get('list_field')
            if not list_field:
                warnings.append(f"{MANUAL}: list measure has no list_field")
                return self._failed(changes, warnings, dropped=dropped)
            sql_raw = '${' + list_field + '}'

        if has_liquid(sql_raw):
            warnings.append(f"{MANUAL}: sql uses Liquid templating, which Omni does not support")
            return self._failed(changes, warnings, dropped=dropped)

        if sql_raw:
            try:
                sql_clean, sql_changes, sql_warnings = self.clean_sql_expression(sql_raw, field_name)
            except UnsafeSQL as e:
                warnings.append(f"{MANUAL}: {e}")
                return self._failed(changes, warnings, dropped=dropped)
            changes.extend(sql_changes)
            warnings.extend(sql_warnings)
            if sql_warnings:
                risk_level = "MEDIUM"
        else:
            sql_clean = ''

        if is_calculated and not sql_clean:
            warnings.append(f"{MANUAL}: '{looker_type}' measure has no sql")
            return self._failed(changes, warnings, dropped=dropped)
        if not is_calculated and looker_type != 'count' and not sql_clean:
            warnings.append(f"{MANUAL}: '{looker_type}' measure has no sql")
            return self._failed(changes, warnings, dropped=dropped)

        dependencies = self.extract_dependencies(sql_clean) - {field_name}
        cross_view_refs = self.extract_cross_view_references(sql_clean)

        label = looker_measure.get('label', field_name.replace('_', ' ').title())
        label_mapped, label_changes = self.apply_canonical_mapping(label)
        if label_changes:
            changes.append(f"Label mapping: {label} → {label_mapped}")
            label = label_mapped

        description = looker_measure.get('description', f"{label} measure")
        metadata = self.generate_measure_metadata(field_name, looker_measure)

        measure_kwargs: Dict[str, Any] = {
            'label': label,
            'description': description,
            'tags': metadata.get('tags', [])
        }
        if sql_clean:
            measure_kwargs['sql'] = sql_clean

        if is_calculated:
            changes.append(f"Calculated measure (type: {looker_type}): sql kept, no aggregate_type")
        else:
            measure_kwargs['aggregate_type'] = AGGREGATE_TYPE_MAPPINGS[looker_type]
            changes.append(f"Mapped type: {looker_type} → aggregate_type: {AGGREGATE_TYPE_MAPPINGS[looker_type].value}")
            if looker_type == 'percentile':
                try:
                    measure_kwargs['percentile'] = int(looker_measure.get('percentile'))
                except (TypeError, ValueError):
                    warnings.append(f"{MANUAL}: percentile measure has no valid percentile value")
                    return self._failed(changes, warnings, dependencies, cross_view_refs, dropped)

        risk_level = self._apply_format(looker_measure, measure_kwargs, changes, warnings, risk_level)

        # Filters
        filter_pairs = extract_measure_filters(looker_measure)
        if filter_pairs:
            omni_filters: Dict[str, Any] = {}
            for filter_field, expression in filter_pairs:
                looker_type = self.resolve_field_type(filter_field)
                condition = None
                if looker_type is not None:  # unknown fields are never guessed
                    condition = translate_filter_expression(expression, filter_kind(looker_type))
                if condition is None:
                    warnings.append(
                        f"{MANUAL}: filter {filter_field}: \"{expression}\" "
                        f"(field type: {looker_type or 'unknown'}) cannot be translated automatically"
                    )
                    return self._failed(changes, warnings, dependencies, cross_view_refs, dropped)
                omni_filters[filter_field] = condition
                if '.' in filter_field:
                    cross_view_refs.add(filter_field)
                else:
                    dependencies.add(filter_field)
            measure_kwargs['filters'] = omni_filters
            changes.append(f"Translated filters: {', '.join(f'{f}' for f, _ in filter_pairs)}")

        if looker_measure.get('hidden') == 'yes':
            measure_kwargs['hidden'] = True
            changes.append("Marked as hidden: true")
        if looker_measure.get('group_label'):
            measure_kwargs['group_label'] = looker_measure['group_label']
        if 'ai_context' in metadata:
            measure_kwargs['ai_context'] = metadata['ai_context']
        if self.canonical_synonyms(field_name):
            measure_kwargs['synonyms'] = self.canonical_synonyms(field_name)
        for param in dropped:
            changes.append(f"Not migrated: {param}")

        try:
            omni_measure = OmniMeasure(**measure_kwargs)
        except Exception as e:
            warnings.append(f"Validation error: {e}")
            return self._failed(changes, warnings, dependencies, cross_view_refs, dropped)

        return ConversionResult(
            omni_field=omni_measure,
            changes=changes,
            warnings=warnings,
            dependencies=dependencies,
            risk_level=risk_level,
            cross_view_refs=cross_view_refs,
            dropped_params=dropped
        )

    def convert_dimension_group(
        self,
        field_name: str,
        looker_dim_group: Dict
    ) -> ConversionResult:
        """
        Convert a Looker `type: time` dimension_group to one Omni date dimension

        Looker:
          dimension_group: created {
            type: time
            timeframes: [raw, date, week, month]
            sql: ${TABLE}.created_at ;;
          }

        Omni:
          created:
            sql: created_at
            label: Created

        Timeframes are not emitted; the Looker list is recorded in `changes`.
        """
        changes: List[str] = []
        warnings: List[str] = []
        risk_level = "LOW"
        dropped = self._dropped_params(looker_dim_group)

        group_type = looker_dim_group.get('type', 'time')
        if group_type != 'time':
            warnings.append(
                f"{MANUAL}: dimension_group type '{group_type}' has no automatic Omni conversion"
            )
            return self._failed(changes, warnings, dropped=dropped)

        datatype = looker_dim_group.get('datatype')
        if datatype not in (None, 'timestamp', 'datetime', 'date'):
            warnings.append(
                f"{MANUAL}: dimension_group datatype '{datatype}' needs an explicit conversion to a date/timestamp"
            )
            return self._failed(changes, warnings, dropped=dropped)
        if str(looker_dim_group.get('convert_tz', 'yes')).lower() == 'no':
            warnings.append(
                f"{MANUAL}: dimension_group has convert_tz: no; check Omni's timezone handling for this field"
            )
            return self._failed(changes, warnings, dropped=dropped)
        for param in ('sql_date', 'sql_start', 'sql_end'):
            if looker_dim_group.get(param):
                warnings.append(f"{MANUAL}: dimension_group uses {param}, which is not converted automatically")
                return self._failed(changes, warnings, dropped=dropped)

        sql_raw = looker_dim_group.get('sql') or '${TABLE}.' + field_name
        if has_liquid(sql_raw):
            warnings.append(f"{MANUAL}: sql uses Liquid templating, which Omni does not support")
            return self._failed(changes, warnings, dropped=dropped)

        try:
            sql_clean, sql_changes, sql_warnings = self.clean_sql_expression(sql_raw, field_name)
        except UnsafeSQL as e:
            warnings.append(f"{MANUAL}: {e}")
            return self._failed(changes, warnings, dropped=dropped)
        warnings.extend(sql_warnings)
        if sql_warnings:
            risk_level = "MEDIUM"

        is_standard_sql = sql_clean in (field_name, f'${{{field_name}}}')
        if is_standard_sql:
            changes.append("Standard SQL omitted (Omni infers from schema)")
        else:
            changes.extend(sql_changes)
            changes.append(f"Custom SQL retained: {sql_clean}")

        dependencies = set() if is_standard_sql else self.extract_dependencies(sql_clean) - {field_name}
        cross_view_refs = self.extract_cross_view_references(sql_clean)
        db_column_name = self.extract_db_column_name(sql_raw, field_name)

        looker_label = looker_dim_group.get('label')
        default_label = field_name.replace('_', ' ').title()
        label = looker_label or default_label
        label_mapped, label_changes = self.apply_canonical_mapping(label)
        if label_changes:
            changes.append(f"Label mapping: {label} → {label_mapped}")
            label = label_mapped

        description = looker_dim_group.get('description') or f"{default_label} timestamp field"

        tags = ['date', 'temporal'] + self._migration_tags()
        dim_kwargs: Dict[str, Any] = {
            'label': label,
            'tags': [normalize_tag(tag) for tag in tags],
            'description': description,
            'ai_context': f"{_sentence(description)} Date/time dimension; Omni generates the date timeframes.",
        }
        if not is_standard_sql:
            dim_kwargs['sql'] = sql_clean
        if looker_dim_group.get('hidden') == 'yes':
            dim_kwargs['hidden'] = True
            changes.append("Marked as hidden: true")
        if looker_dim_group.get('group_label'):
            dim_kwargs['group_label'] = looker_dim_group['group_label']
        if self.canonical_synonyms(field_name):
            dim_kwargs['synonyms'] = self.canonical_synonyms(field_name)

        timeframes = looker_dim_group.get('timeframes')
        changes.append("Converted dimension_group to a single date dimension")
        if timeframes:
            changes.append(
                f"Looker timeframes [{', '.join(timeframes)}] not emitted (Omni generates timeframes)"
            )
        for param in dropped:
            changes.append(f"Not migrated: {param}")

        try:
            omni_dim = OmniDimension(**dim_kwargs)
        except Exception as e:
            warnings.append(f"Validation error: {e}")
            return self._failed(changes, warnings, dependencies, cross_view_refs, dropped)

        return ConversionResult(
            omni_field=omni_dim,
            changes=changes,
            warnings=warnings,
            dependencies=dependencies,
            risk_level=risk_level,
            db_column_name=db_column_name,
            cross_view_refs=cross_view_refs,
            dropped_params=dropped
        )
