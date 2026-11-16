"""
Primary key finder for Omni views

Finds a primary key for Omni view files that have none, by testing candidate
columns against a SAMPLE of the real data through Omni's query API, and
(optionally) writing `primary_key: true` into the view file.

Pieces, in the order a run uses them:

    OmniQueryClient      the only place that calls the Omni CLI
    load_view            read one view file, decide whether it can be examined
    find_candidates      order the dimensions worth testing, cheapest evidence first
    ForeignKeyHints      advice on which candidates look like foreign keys
    Dialect (+ DIALECTS) one sampling strategy per warehouse, plus identifier quoting
    PrimaryKeyFinder     plan the sample, run one query per view, evaluate, test pairs
    apply_primary_key    write the chosen key, leaving the rest of the file alone
    render_report        markdown report;  results_to_json: machine-readable records

A sample can prove a column is NOT unique. It can only suggest that a column
is unique. Reports say so, and record how every sample was taken.

All generated SQL is read-only SELECT. Every schema, table and column name
comes from a YAML file, so each one is validated against IDENTIFIER_RE before
it is put into SQL; a view with a name that does not match is skipped.
"""

from __future__ import annotations

import io
import itertools
import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq

PRIMARY_KEY_TAG = 'primary_key_from_sample'

STATUS_KEY_FOUND = 'key_found'
STATUS_MULTIPLE_CANDIDATES = 'multiple_candidates'
STATUS_PAIR_SUGGESTED = 'pair_suggested'
STATUS_NO_KEY_FOUND = 'no_key_found'
STATUS_SAMPLE_TOO_SMALL = 'sample_too_small'
STATUS_SKIPPED = 'skipped'
STATUS_NEEDS_DECISION = 'needs_decision'
STATUS_QUERY_ERROR = 'query_error'

STATUS_ORDER = [
    STATUS_KEY_FOUND, STATUS_MULTIPLE_CANDIDATES, STATUS_PAIR_SUGGESTED, STATUS_NO_KEY_FOUND, STATUS_SAMPLE_TOO_SMALL,
    STATUS_NEEDS_DECISION, STATUS_QUERY_ERROR, STATUS_SKIPPED,
]

SAMPLE_NOTE = (
    "Keys in this report were tested on a sample of rows, not the whole table. "
    "A sample can prove that a column is NOT unique. It can only suggest that a column is unique: "
    "duplicates may exist in rows the sample did not read. Treat every key here as a suggestion to "
    "review, and check it against the full table before relying on it."
)

# Conservative identifier: letters, digits, underscore and dollar. Dots are
# only ever a separator between parts (see split_qualified), never part of a name
IDENTIFIER_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_$]*$')

VIEW_SUFFIXES = ('.query.view.yaml', '.query.view', '.view.yaml', '.view')
QUERY_VIEW_SUFFIXES = ('.query.view.yaml', '.query.view')

EVIDENCE_NAME = 'key_like_name'
EVIDENCE_COUNT_DISTINCT = 'count_distinct_measure'
EVIDENCE_TABLE_KEY = 'table_key_tag'
EVIDENCE_PLAIN_COLUMN = 'plain_column'
EVIDENCE_EXISTING = 'existing_primary_key'
_EVIDENCE_TIER = {
    EVIDENCE_EXISTING: 0, EVIDENCE_NAME: 1, EVIDENCE_COUNT_DISTINCT: 2,
    EVIDENCE_TABLE_KEY: 3, EVIDENCE_PLAIN_COLUMN: 4,
}

_KEY_SUFFIXES = ('_id', '_key', '_uuid', '_guid')
_DATE_TAGS = {'date', 'temporal', 'time', 'timestamp'}
_BOOLEAN_TAGS = {'yesno', 'boolean', 'bool'}
_DATE_NAME_RE = re.compile(r'(^|_)(date|time|timestamp|ts|datetime|day|week|month|year)$|_(at|on)$')
_BOOLEAN_NAME_RE = re.compile(r'^(is|has|was|can|should)_')
_FIELD_REF_RE = re.compile(r'^\s*\$\{([A-Za-z_][A-Za-z0-9_$]*)\}\s*$')


# ----------------------------------------------------------------------
# Omni CLI access (the only place the CLI is called)
# ----------------------------------------------------------------------

class OmniQueryError(Exception):
    """One query failed. Recorded against the view; the run continues."""


class OmniConnectionError(OmniQueryError):
    """The Omni CLI or API cannot be used at all. Stops the run."""


class OmniQueryClient:
    """
    Runs SQL through Omni's query API using the Omni CLI (`omni query run`).

    Tests replace this class with a fake that has the same two methods:
    run_sql(sql) -> list of row dicts, and dialect() -> str.
    """

    def __init__(self, model_id: str, branch_id: Optional[str] = None, profile: Optional[str] = None,
                 base_url: Optional[str] = None, executable: str = 'omni', timeout: int = 900,
                 runner=subprocess.run):
        self.model_id = model_id
        self.branch_id = branch_id
        self.profile = profile
        self.base_url = base_url
        self.executable = executable
        self.timeout = timeout
        self._runner = runner

    # -- public ---------------------------------------------------------

    def run_sql(self, sql: str) -> List[Dict[str, Any]]:
        body: Dict[str, Any] = {
            'query': {'modelId': self.model_id, 'fields': [], 'userEditedSQL': sql, 'rewriteSql': False},
            'resultType': 'json',
        }
        if self.branch_id:
            body['branchId'] = self.branch_id
        payload = self._run(['query', 'run', '--body', json.dumps(body)])
        if isinstance(payload, list):
            return [row for row in payload if isinstance(row, dict)]
        if isinstance(payload, dict):
            for key in ('rows', 'data', 'records', 'result'):
                if isinstance(payload.get(key), list):
                    return [row for row in payload[key] if isinstance(row, dict)]
        raise OmniQueryError(f"unexpected response from omni query run: {_shorten(json.dumps(payload))}")

    def dialect(self) -> str:
        """Dialect of the connection behind the model (for example 'bigquery')"""
        try:
            models = _records(self._run(['models', 'list', '--modelid', self.model_id]), 'records', 'models')
            model = next((m for m in models if m.get('id') == self.model_id), None)
            if model is None:
                raise OmniConnectionError(f"model {self.model_id} not found by `omni models list`")
            connection_id = model.get('connectionId')
            connections = _records(self._run(['connections', 'list']), 'connections', 'records')
            connection = next((c for c in connections if c.get('id') == connection_id), None)
            if connection is None or not connection.get('dialect'):
                raise OmniConnectionError(
                    f"connection {connection_id} of model {self.model_id} not found by `omni connections list`")
            return str(connection['dialect']).lower()
        except OmniConnectionError:
            raise
        except OmniQueryError as e:
            raise OmniConnectionError(f"could not read the model's dialect: {e}") from e

    # -- internals ------------------------------------------------------

    def _run(self, args: List[str]) -> Any:
        command = [self.executable, *args]
        if self.profile:
            command += ['-p', self.profile]
        if self.base_url:
            command += ['--base-url', self.base_url]
        try:
            completed = self._runner(command, capture_output=True, text=True, timeout=self.timeout)
        except FileNotFoundError as e:
            raise OmniConnectionError(
                f"Omni CLI not found ('{self.executable}'). Install and configure it, then re-run.") from e
        except subprocess.TimeoutExpired as e:
            raise OmniQueryError(f"omni {args[0]} {args[1]} timed out after {self.timeout}s") from e

        stdout, stderr = (completed.stdout or '').strip(), (completed.stderr or '').strip()
        payload = _parse_json(stdout)
        error = _error_message(payload) or (_error_message(_parse_json(stderr)) if completed.returncode else None)
        if error is None and completed.returncode != 0:
            error = stderr or stdout or f"exit code {completed.returncode}"
        if error is None and payload is None:
            error = f"output is not JSON: {_shorten(stdout) or '(empty)'}"
        if error is not None:
            raise _classify_error(error)
        return payload


def _parse_json(text: str) -> Any:
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def _records(payload: Any, *keys: str) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        for key in keys:
            if isinstance(payload.get(key), list):
                return [r for r in payload[key] if isinstance(r, dict)]
    return []


def _error_message(payload: Any) -> Optional[str]:
    """Error text of an Omni error body ({"detail": ..., "status": 400}, or a job body with an error)"""
    if not isinstance(payload, dict):
        return None
    status = payload.get('status')
    numeric_status = status if isinstance(status, int) else None
    parts = []
    for key in ('detail', 'error', 'errors', 'error_message', 'errorMessage', 'message'):
        value = payload.get(key)
        if not value:
            continue
        if key == 'message' and not (numeric_status and numeric_status >= 400) and not parts:
            continue  # a bare "message" is only an error next to an error status
        parts.append(value if isinstance(value, str) else json.dumps(value))
    if not parts and isinstance(status, str) and status.upper() in ('FAILED', 'ERROR'):
        parts.append(f"job status {status}")
    if not parts and numeric_status and numeric_status >= 400:
        parts.append(f"HTTP {numeric_status}")
    if not parts:
        return None
    text = '; '.join(dict.fromkeys(parts))
    return f"{text} (status {numeric_status})" if numeric_status else text


def _classify_error(message: str) -> OmniQueryError:
    lowered = message.lower()
    if 'manually written sql' in lowered or 'manual sql' in lowered:
        return OmniConnectionError(
            f"{message}. This script needs an Omni role that is allowed to run manually written SQL.")
    if re.search(r'\(status (401|403)\)', message) or 'unauthorized' in lowered or 'no api token' in lowered:
        return OmniConnectionError(message)
    return OmniQueryError(message)


def _shorten(text: str, limit: int = 300) -> str:
    text = ' '.join(str(text).split())
    return text if len(text) <= limit else text[:limit] + '...'


# ----------------------------------------------------------------------
# Views
# ----------------------------------------------------------------------

def is_valid_identifier(name: Any) -> bool:
    return isinstance(name, str) and bool(IDENTIFIER_RE.match(name))


def split_qualified(value: Any) -> Optional[List[str]]:
    """'schema.table' -> ['schema', 'table']; None if any part is not a safe identifier"""
    if not isinstance(value, str) or not value:
        return None
    parts = value.split('.')
    return parts if all(is_valid_identifier(p) for p in parts) else None


@dataclass
class TableRef:
    """Validated name parts, outermost first: [catalog..., schema..., table]"""
    parts: List[str]

    @property
    def table(self) -> str:
        return self.parts[-1]

    @property
    def container(self) -> List[str]:
        """Everything but the table name (catalog and schema)"""
        return self.parts[:-1]

    def __str__(self) -> str:
        return '.'.join(self.parts)


@dataclass
class ViewInfo:
    name: str
    path: Path
    table: Optional[TableRef] = None
    dimensions: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    measures: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    existing_key: Optional[str] = None
    skip_reason: Optional[str] = None


def view_name_from_path(path: Path) -> str:
    name = Path(path).name
    for suffix in VIEW_SUFFIXES:
        if name.endswith(suffix):
            return name[:-len(suffix)]
    return name


def discover_view_files(root: Path) -> List[Path]:
    """Every Omni view file under root, at any depth, sorted"""
    found = []
    for path in Path(root).rglob('*'):
        if path.is_file() and not path.name.startswith('.') and path.name.endswith(VIEW_SUFFIXES):
            found.append(path)
    return sorted(found)


def _round_trip_yaml(text: str = '') -> YAML:
    """Same settings as the converter's writer, so its files survive a round trip unchanged"""
    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.default_flow_style = False
    yaml.width = 4096
    if _sequences_are_indented(text):
        yaml.indent(mapping=2, sequence=4, offset=2)
    else:
        yaml.indent(mapping=2, sequence=2, offset=0)
    return yaml


def _sequences_are_indented(text: str) -> bool:
    """True when block lists sit two spaces in from their key (hand-written style)"""
    previous_key_indent = None
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        indent = len(line) - len(line.lstrip(' '))
        if line.lstrip().startswith('- ') and previous_key_indent is not None:
            return indent > previous_key_indent
        previous_key_indent = indent if line.rstrip().endswith(':') else None
    return False


def load_view(path: Path) -> ViewInfo:
    """Read a view file and decide whether it can be examined (skip_reason is set when not)"""
    path = Path(path)
    view = ViewInfo(name=view_name_from_path(path), path=path)
    try:
        text = path.read_text()
        data = _round_trip_yaml(text).load(text)
    except Exception as e:  # unreadable or malformed YAML: report, keep going
        view.skip_reason = f"could not read the file: {_shorten(str(e), 160)}"
        return view
    if not isinstance(data, dict):
        view.skip_reason = "file is empty or not a YAML mapping"
        return view

    dims = data.get('dimensions')
    view.dimensions = {str(k): (v if isinstance(v, dict) else {}) for k, v in dims.items()} \
        if isinstance(dims, dict) else {}
    measures = data.get('measures')
    view.measures = {str(k): (v if isinstance(v, dict) else {}) for k, v in measures.items()} \
        if isinstance(measures, dict) else {}
    view.existing_key = next((n for n, d in view.dimensions.items() if d.get('primary_key') is True), None)

    if data.get('template') is True:
        view.skip_reason = "template view (template: true)"
    elif path.name.endswith(QUERY_VIEW_SUFFIXES) or 'sql' in data or 'query' in data:
        view.skip_reason = ("SQL query view: not examined in this version "
                            "(its SQL may contain Mustache templating). Set its key by hand")
    elif not data.get('table_name'):
        if data.get('extends'):
            view.skip_reason = "only extends another view and has no table of its own"
        else:
            view.skip_reason = "no table_name in the view file"
    elif not data.get('schema'):
        view.skip_reason = "no schema in the view file"
    else:
        parts: List[str] = []
        for key in ('catalog', 'schema', 'table_name'):
            value = data.get(key)
            if value is None and key == 'catalog':
                continue
            split = split_qualified(value)
            if split is None:
                view.skip_reason = (f"unsafe identifier in {key}: {value!r} "
                                    "(only letters, digits, underscore and dollar are accepted)")
                return view
            parts += split
        view.table = TableRef(parts)
    return view


# ----------------------------------------------------------------------
# Candidates
# ----------------------------------------------------------------------

@dataclass
class Candidate:
    name: str                      # dimension name
    column: str                    # warehouse column it reads
    evidence: str
    exact: bool = False            # column was quoted in the view file: keep its case
    distinct_count: Optional[int] = None
    null_count: Optional[int] = None
    qualifies: bool = False
    note: Optional[str] = None     # advice for the person choosing, for example "likely foreign key"

    @property
    def tier(self) -> int:
        return _EVIDENCE_TIER[self.evidence]


def dimension_column(name: str, dim: Dict[str, Any]) -> Optional[Tuple[str, bool]]:
    """
    (column, exact) when the dimension maps to a plain column, else None.

    It maps when it has no sql (column = dimension name) or its sql is a bare
    column name, optionally quoted. Any other expression is not a candidate.
    """
    sql = dim.get('sql')
    if sql is None:
        return (name, False) if is_valid_identifier(name) else None
    if not isinstance(sql, str):
        return None
    text = sql.strip()
    if is_valid_identifier(text):
        return text, False
    if len(text) > 2 and text[0] == text[-1] and text[0] in '"`' and is_valid_identifier(text[1:-1]):
        return text[1:-1], True
    return None


def _tags(dim: Dict[str, Any]) -> List[str]:
    tags = dim.get('tags')
    return [str(t) for t in tags] if isinstance(tags, list) else []


def _is_key_like_name(name: str, table_name: str) -> int:
    """0 = not key-like; otherwise a sub-rank (1 best) used only to order the name tier"""
    lowered, table = name.lower(), table_name.lower()
    if lowered == 'id':
        return 1
    if lowered in (f"{table}_id", f"{table[:-1]}_id" if table.endswith('s') else f"{table}_id"):
        return 2
    return 3 if lowered.endswith(_KEY_SUFFIXES) else 0


def _looks_like_date_or_boolean(name: str, dim: Dict[str, Any]) -> bool:
    tags = set(_tags(dim))
    if tags & _DATE_TAGS or tags & _BOOLEAN_TAGS:
        return True
    if 'timeframes' in dim or 'datatype' in dim:
        return True
    lowered = name.lower()
    return bool(_DATE_NAME_RE.search(lowered) or _BOOLEAN_NAME_RE.match(lowered))


def find_candidates(view: ViewInfo, max_candidates: int = 12, include_existing_key: bool = False) -> List[Candidate]:
    """
    Dimensions worth testing, cheapest evidence first, capped at max_candidates:
    1. key-like names (id, *_id, *_key, *_uuid, *_guid, <table>_id)
    2. dimensions a count_distinct measure counts
    3. dimensions tagged table_key
    4. remaining non-date, non-boolean dimensions that read a plain column
    With include_existing_key (--recheck) the current primary key goes first.
    """
    table_name = view.table.table if view.table else view.name
    counted = set()
    for measure in view.measures.values():
        if measure.get('aggregate_type') == 'count_distinct' and isinstance(measure.get('sql'), str):
            match = _FIELD_REF_RE.match(measure['sql'])
            counted.add(match.group(1) if match else measure['sql'].strip())

    ranked = []
    for position, (name, dim) in enumerate(view.dimensions.items()):
        mapped = dimension_column(name, dim)
        if mapped is None:
            continue
        column, exact = mapped
        name_rank = _is_key_like_name(name, table_name)
        if include_existing_key and dim.get('primary_key') is True:
            evidence = EVIDENCE_EXISTING
        elif name_rank:
            evidence = EVIDENCE_NAME
        elif name in counted or column in counted:
            evidence = EVIDENCE_COUNT_DISTINCT
        elif 'table_key' in _tags(dim):
            evidence = EVIDENCE_TABLE_KEY
        elif not _looks_like_date_or_boolean(name, dim):
            evidence = EVIDENCE_PLAIN_COLUMN
        else:
            continue
        candidate = Candidate(name=name, column=column, evidence=evidence, exact=exact)
        ranked.append(((candidate.tier, name_rank if evidence == EVIDENCE_NAME else 0, position), candidate))

    candidates, seen_columns = [], set()
    for _, candidate in sorted(ranked, key=lambda item: item[0]):
        key = candidate.column if candidate.exact else candidate.column.lower()
        if key in seen_columns:
            continue  # two dimensions reading the same column: test it once
        seen_columns.add(key)
        candidates.append(candidate)
    return candidates[:max(0, max_candidates)]


class ForeignKeyHints:
    """
    Advice only: which candidate names look like a foreign key to another view.

    A candidate is a likely foreign key when another view already has a primary
    key dimension of the same name, or when its name minus `_id` / `_key`
    matches another view's name or table name (a `dim_` / `fact_` prefix and a
    plural `s` are ignored). A bare `id`, and a name built from the view's own
    name, are never flagged. It is never a reason to exclude a candidate.
    """

    _PREFIXES = ('dim_', 'fact_')
    _SUFFIXES = ('_id', '_key')
    _GENERIC = ('id', 'key', 'uuid', 'guid', 'pk')

    def __init__(self, views: Sequence[ViewInfo] = ()):
        self._views = list(views)

    @classmethod
    def _entity(cls, name: str) -> str:
        text = name.lower()
        for prefix in cls._PREFIXES:
            if text.startswith(prefix):
                text = text[len(prefix):]
                break
        return text[:-1] if text.endswith('s') and len(text) > 1 else text

    def note_for(self, candidate_name: str, view: ViewInfo) -> Optional[str]:
        lowered = candidate_name.lower()
        stem = next((lowered[:-len(s)] for s in self._SUFFIXES if lowered.endswith(s) and len(lowered) > len(s)), None)
        own = {self._entity(view.name)} | ({self._entity(view.table.table)} if view.table else set())
        if lowered in self._GENERIC or (stem is not None and self._entity(stem) in own):
            return None  # `id` says nothing about another view, and <own name>_id is this view's own key
        for other in self._views:
            if other.path == view.path:
                continue
            if other.existing_key and other.existing_key.lower() == lowered:
                return f"likely foreign key: it is the primary key of view {other.name}"
            if stem is None:
                continue
            names = {self._entity(other.name)} | ({self._entity(other.table.table)} if other.table else set())
            if self._entity(stem) in names - own:
                return f"likely foreign key: its name matches view {other.name}"
        return None


# ----------------------------------------------------------------------
# Sampling strategies, one per warehouse
# ----------------------------------------------------------------------

@dataclass
class SampleOptions:
    sample_rows: int = 1_000_000
    date_column: Optional[str] = None
    window_days: int = 30
    max_unpartitioned_rows: int = 5_000_000
    allow_full_scan: bool = False


@dataclass
class SamplePlan:
    """How the sample is taken. Exactly one of (where_sql / limit / neither = full scan), or needs_decision"""
    strategy: str
    where_sql: Optional[str] = None
    limit: Optional[int] = None
    note: Optional[str] = None
    needs_decision: Optional[str] = None

    @property
    def filter(self) -> Optional[str]:
        parts = [p for p in (self.where_sql, f"limit {self.limit}" if self.limit else None) if p]
        return ' '.join(parts) if parts else None

    def describe(self) -> str:
        return f"{self.strategy} ({self.filter})" if self.filter else self.strategy


STRATEGY_LIMIT = 'limit'
STRATEGY_DATE_WINDOW = 'date_window'
STRATEGY_PARTITION_FILTER = 'partition_filter'
STRATEGY_SORT_KEY_WINDOW = 'sort_key_window'
STRATEGY_FULL_SCAN = 'full_scan'


class Dialect:
    """
    Default strategy: a subquery with LIMIT <n>. Used for postgres, duckdb,
    motherduck and unknown dialects. To add a warehouse, subclass this,
    override what differs, and add one entry to DIALECTS.
    """
    name = 'generic'
    string_type = 'varchar'
    window_uses_limit = True   # also cap a date window with LIMIT (False where LIMIT saves nothing)

    # -- identifiers (the one place quoting is decided) ------------------

    def quote(self, identifier: str, exact: bool = False) -> str:
        if not is_valid_identifier(identifier):
            raise ValueError(f"unsafe identifier: {identifier!r}")
        return f'"{identifier}"'

    def table_sql(self, table: TableRef) -> str:
        return '.'.join(self.quote(part) for part in table.parts)

    def column_sql(self, candidate: Candidate) -> str:
        return self.quote(candidate.column, exact=candidate.exact)

    # -- sampling --------------------------------------------------------

    def plan(self, table: TableRef, client, options: SampleOptions) -> SamplePlan:
        if options.date_column:
            return self.override_plan(table, client, options)
        return self.default_plan(table, client, options)

    def default_plan(self, table: TableRef, client, options: SampleOptions) -> SamplePlan:
        return SamplePlan(strategy=STRATEGY_LIMIT, limit=options.sample_rows)

    def override_plan(self, table: TableRef, client, options: SampleOptions) -> SamplePlan:
        predicate = self.window_predicate(self.quote(options.date_column), options.window_days)
        return SamplePlan(strategy=STRATEGY_DATE_WINDOW, where_sql=predicate,
                          limit=options.sample_rows if self.window_uses_limit else None,
                          note="--date-column override")

    def window_predicate(self, column_sql: str, days: int, data_type: Optional[str] = None) -> str:
        return f"{column_sql} >= current_date - interval '{int(days)} days'"

    # -- expressions -----------------------------------------------------

    def concat_sql(self, expressions: Sequence[str], separator: str = '|') -> str:
        cast = [f"cast({e} as {self.string_type})" for e in expressions]
        return f" || '{separator}' || ".join(cast)


class SnowflakeDialect(Dialect):
    name = 'snowflake'

    def quote(self, identifier: str, exact: bool = False) -> str:
        # Unquoted Snowflake names fold to upper case. Folding here and then
        # quoting resolves the same object and is safe for reserved words.
        # A column the view file quotes keeps its exact case.
        if not is_valid_identifier(identifier):
            raise ValueError(f"unsafe identifier: {identifier!r}")
        return f'"{identifier}"' if exact else f'"{identifier.upper()}"'

    def window_predicate(self, column_sql: str, days: int, data_type: Optional[str] = None) -> str:
        return f"{column_sql} >= dateadd(day, -{int(days)}, current_date)"


class RedshiftDialect(Dialect):
    name = 'redshift'
    _DATE_TYPES = ('date', 'timestamp')

    def window_predicate(self, column_sql: str, days: int, data_type: Optional[str] = None) -> str:
        return f"{column_sql} >= dateadd(day, -{int(days)}, current_date)"

    def default_plan(self, table: TableRef, client, options: SampleOptions) -> SamplePlan:
        """Recent window on the first sort key column when it is a date or timestamp, else LIMIT"""
        fallback = SamplePlan(strategy=STRATEGY_LIMIT, limit=options.sample_rows)
        schema, name = table.parts[-2].lower(), table.table.lower()
        try:
            rows = client.run_sql(
                f"select sortkey1 from svv_table_info where \"schema\" = '{schema}' and \"table\" = '{name}'")
            sort_key = self._sort_key_column(_value(rows[0], 'sortkey1') if rows else None)
            if not sort_key:
                fallback.note = "no usable sort key found"
                return fallback
            rows = client.run_sql(
                "select data_type from svv_columns "
                f"where table_schema = '{schema}' and table_name = '{name}' and column_name = '{sort_key}'")
            data_type = str(_value(rows[0], 'data_type') or '').lower() if rows else ''
        except OmniConnectionError:
            raise
        except OmniQueryError as e:
            fallback.note = f"sort key lookup failed ({_shorten(str(e), 120)})"
            return fallback
        if not data_type.startswith(self._DATE_TYPES):
            fallback.note = f"sort key {sort_key} is not a date or timestamp"
            return fallback
        return SamplePlan(strategy=STRATEGY_SORT_KEY_WINDOW,
                          where_sql=self.window_predicate(self.quote(sort_key), options.window_days),
                          limit=options.sample_rows, note=f"sort key {sort_key} ({data_type})")

    @staticmethod
    def _sort_key_column(value: Any) -> Optional[str]:
        """svv_table_info.sortkey1 is a column name, or AUTO(SORTKEY(col)), or INTERLEAVED / empty"""
        if not value:
            return None
        text = str(value).strip()
        match = re.match(r'^AUTO\(SORTKEY\((.+)\)\)$', text, re.IGNORECASE)
        if match:
            text = match.group(1).split(',')[0].strip()
        return text if is_valid_identifier(text) else None


class BigQueryDialect(Dialect):
    """
    LIMIT does not reduce the bytes BigQuery scans, so a sample must filter on
    the partition column. Partitioning is read from INFORMATION_SCHEMA first.
    """
    name = 'bigquery'
    string_type = 'string'
    window_uses_limit = False
    _SPECIAL_PARTITIONS = ('__NULL__', '__UNPARTITIONED__')

    def quote(self, identifier: str, exact: bool = False) -> str:
        if not is_valid_identifier(identifier):
            raise ValueError(f"unsafe identifier: {identifier!r}")
        return f'`{identifier}`'

    def concat_sql(self, expressions: Sequence[str], separator: str = '|') -> str:
        cast = [f"cast({e} as {self.string_type})" for e in expressions]
        return "concat(" + f", '{separator}', ".join(cast) + ")"

    def window_predicate(self, column_sql: str, days: int, data_type: Optional[str] = None) -> str:
        kind = (data_type or '').upper()
        if kind == 'TIMESTAMP':
            return f"{column_sql} >= timestamp_sub(current_timestamp(), interval {int(days)} day)"
        if kind == 'DATETIME':
            return f"{column_sql} >= datetime_sub(current_datetime(), interval {int(days)} day)"
        if kind == 'DATE':
            return f"{column_sql} >= date_sub(current_date(), interval {int(days)} day)"
        return f"date({column_sql}) >= date_sub(current_date(), interval {int(days)} day)"

    def _metadata_table(self, table: TableRef, name: str) -> str:
        return '.'.join([self.quote(p) for p in table.container] + ['INFORMATION_SCHEMA', name])

    def override_plan(self, table: TableRef, client, options: SampleOptions) -> SamplePlan:
        data_type = None
        try:
            rows = client.run_sql(
                f"select data_type from {self._metadata_table(table, 'COLUMNS')} "
                f"where table_name = '{table.table}' and column_name = '{options.date_column}'")
            data_type = _value(rows[0], 'data_type') if rows else None
        except OmniConnectionError:
            raise
        except OmniQueryError:
            pass  # type unknown: window_predicate falls back to date(column)
        return SamplePlan(strategy=STRATEGY_DATE_WINDOW,
                          where_sql=self.window_predicate(self.quote(options.date_column),
                                                          options.window_days, data_type),
                          note="--date-column override")

    def default_plan(self, table: TableRef, client, options: SampleOptions) -> SamplePlan:
        try:
            column_rows = client.run_sql(
                f"select column_name, data_type from {self._metadata_table(table, 'COLUMNS')} "
                f"where table_name = '{table.table}' and is_partitioning_column = 'YES'")
            partition_rows = client.run_sql(
                f"select partition_id, total_rows from {self._metadata_table(table, 'PARTITIONS')} "
                f"where table_name = '{table.table}'")
        except OmniConnectionError:
            raise
        except OmniQueryError as e:
            return self._unknown_size_plan(options, f"could not read partition metadata ({_shorten(str(e), 160)})")

        partitions = []
        total_rows = 0
        for row in partition_rows:
            rows_in_partition = _to_int(_value(row, 'total_rows'))
            total_rows += rows_in_partition
            partition_id = _value(row, 'partition_id')
            if partition_id is not None and str(partition_id) not in self._SPECIAL_PARTITIONS:
                partitions.append((str(partition_id), rows_in_partition))

        column, data_type = None, None
        if column_rows:
            column = _value(column_rows[0], 'column_name')
            data_type = str(_value(column_rows[0], 'data_type') or '').upper()
            if not is_valid_identifier(column):
                return self._unknown_size_plan(options, f"partition column name {column!r} is not a safe identifier")
        elif partitions and all(re.match(r'^\d{4}(\d{2}){0,3}$', pid) for pid, _ in partitions):
            column, data_type = '_PARTITIONTIME', 'TIMESTAMP'  # ingestion-time partitioning

        if column is None:
            return self._unpartitioned_plan(options, total_rows if partition_rows else None)
        if not partitions:
            return self._unknown_size_plan(options, f"table is partitioned on {column} but no partition has rows")

        integer_range = data_type in ('INT64', 'INTEGER')
        if not all(pid.isdigit() for pid, _ in partitions):
            return self._unknown_size_plan(options, "partition ids are not in a form this script understands")
        partitions.sort(key=lambda p: int(p[0]) if integer_range else p[0], reverse=True)

        planned, oldest = 0, partitions[0][0]
        for partition_id, rows_in_partition in partitions:
            oldest = partition_id
            planned += rows_in_partition
            if planned >= options.sample_rows:
                break
        literal = oldest if integer_range else self._partition_literal(oldest, data_type)
        if literal is None:
            return self._unknown_size_plan(options, f"partition id {oldest!r} is not a date this script understands")
        return SamplePlan(strategy=STRATEGY_PARTITION_FILTER, where_sql=f"{self.quote(column)} >= {literal}",
                          note=f"most recent partitions holding about {planned:,} rows")

    def _unpartitioned_plan(self, options: SampleOptions, total_rows: Optional[int]) -> SamplePlan:
        if options.allow_full_scan:
            return SamplePlan(strategy=STRATEGY_FULL_SCAN, note="table is not partitioned; --allow-full-scan given")
        if total_rows is None:
            return self._unknown_size_plan(options, "table is not partitioned and its row count could not be read")
        if total_rows > options.max_unpartitioned_rows:
            return SamplePlan(strategy=STRATEGY_FULL_SCAN, needs_decision=(
                f"table is not partitioned and has {total_rows:,} rows, above --max-unpartitioned-rows "
                f"({options.max_unpartitioned_rows:,}). LIMIT does not reduce the bytes BigQuery scans, so it was "
                "not queried. Re-run with --allow-full-scan, or with --date-column and --window-days"))
        return SamplePlan(strategy=STRATEGY_FULL_SCAN,
                          note=f"table is not partitioned and has {total_rows:,} rows: all rows read")

    def _unknown_size_plan(self, options: SampleOptions, reason: str) -> SamplePlan:
        if options.allow_full_scan:
            return SamplePlan(strategy=STRATEGY_FULL_SCAN, note=f"{reason}; --allow-full-scan given")
        return SamplePlan(strategy=STRATEGY_FULL_SCAN, needs_decision=(
            f"{reason}, so the cost of a scan is unknown and it was not queried. "
            "Re-run with --date-column and --window-days, or with --allow-full-scan"))

    @staticmethod
    def _partition_literal(partition_id: str, data_type: Optional[str]) -> Optional[str]:
        """YYYY, YYYYMM, YYYYMMDD or YYYYMMDDHH -> a typed literal for the start of that partition"""
        if len(partition_id) not in (4, 6, 8, 10):
            return None
        year, month, day, hour = (partition_id[0:4], partition_id[4:6] or '01',
                                  partition_id[6:8] or '01', partition_id[8:10] or '00')
        if not (1 <= int(month) <= 12 and 1 <= int(day) <= 31 and 0 <= int(hour) <= 23):
            return None
        if data_type == 'DATE':
            return f"date '{year}-{month}-{day}'"
        kind = 'datetime' if data_type == 'DATETIME' else 'timestamp'
        return f"{kind} '{year}-{month}-{day} {hour}:00:00'"


DIALECTS: Dict[str, Dialect] = {
    'bigquery': BigQueryDialect(),
    'snowflake': SnowflakeDialect(),
    'redshift': RedshiftDialect(),
    'postgres': Dialect(),
    'postgresql': Dialect(),
    'duckdb': Dialect(),
    'motherduck': Dialect(),
}


def get_dialect(name: Optional[str]) -> Tuple[Dialect, bool]:
    """(strategy, known). An unknown dialect gets the LIMIT strategy and known=False (the caller warns)."""
    key = (name or '').strip().lower()
    if key in DIALECTS:
        return DIALECTS[key], True
    return Dialect(), False


def _value(row: Dict[str, Any], key: str) -> Any:
    """Column lookup that ignores case and separators.

    Snowflake returns upper-case column names, and Omni's JSON results label a
    SQL column as a title ("total_rows" comes back as "Total Rows").
    """
    if key in row:
        return row[key]
    wanted = _column_key(key)
    for k, v in row.items():
        if _column_key(k) == wanted:
            return v
    return None


def _column_key(name: Any) -> str:
    return re.sub(r'[^a-z0-9]', '', str(name).lower())


def _to_int(value: Any) -> int:
    """Whole number from a result cell.

    Omni's JSON results return large counts as text with thousands separators
    ("1,096"). A value that is not a whole number once those are removed is an
    error, never a guess: a wrong count could make a column look unique.
    """
    if value is None or value == '':
        return 0
    if isinstance(value, bool):
        raise OmniQueryError(f"expected a count in the query result, got {value!r}")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value != int(value):
            raise OmniQueryError(f"expected a whole number in the query result, got {value!r}")
        return int(value)
    text = re.sub(r'[,\s_]', '', str(value))
    if not re.fullmatch(r'-?\d+(\.0+)?', text):
        raise OmniQueryError(f"expected a whole number in the query result, got {value!r}")
    return int(text.split('.')[0])


# ----------------------------------------------------------------------
# SQL for the data queries
# ----------------------------------------------------------------------

SAMPLE_ALIAS = 'pk_sample'


def build_sample_sql(dialect: Dialect, table: TableRef, candidates: Sequence[Candidate], plan: SamplePlan) -> str:
    columns = ', '.join(dict.fromkeys(dialect.column_sql(c) for c in candidates))
    sql = f"select {columns} from {dialect.table_sql(table)}"
    if plan.where_sql:
        sql += f" where {plan.where_sql}"
    if plan.limit:
        sql += f" limit {int(plan.limit)}"
    return sql


def build_candidate_sql(dialect: Dialect, table: TableRef, candidates: Sequence[Candidate], plan: SamplePlan) -> str:
    """One query: total rows, and per candidate the distinct count and null count, over the sample"""
    selects = ["count(*) as total_rows"]
    for i, candidate in enumerate(candidates):
        column = dialect.column_sql(candidate)
        selects.append(f"count(distinct {column}) as distinct_{i}")
        selects.append(f"sum(case when {column} is null then 1 else 0 end) as nulls_{i}")
    return (f"select {', '.join(selects)} "
            f"from ({build_sample_sql(dialect, table, candidates, plan)}) as {SAMPLE_ALIAS}")


def build_pair_sql(dialect: Dialect, table: TableRef, pairs: Sequence[Tuple[Candidate, Candidate]],
                   plan: SamplePlan) -> str:
    """One query: total rows, and per pair the distinct count of the two columns together"""
    selects = ["count(*) as total_rows"]
    used: List[Candidate] = []
    for i, (a, b) in enumerate(pairs):
        expression = dialect.concat_sql([dialect.column_sql(a), dialect.column_sql(b)])
        selects.append(f"count(distinct {expression}) as pair_{i}")
        used += [a, b]
    return (f"select {', '.join(selects)} "
            f"from ({build_sample_sql(dialect, table, used, plan)}) as {SAMPLE_ALIAS}")


def assert_read_only(sql: str) -> str:
    """Belt and braces: everything this module sends must be a single SELECT"""
    if not sql.lstrip().lower().startswith('select') or ';' in sql:
        raise ValueError(f"refusing to run non-SELECT SQL: {_shorten(sql, 80)}")
    return sql


# ----------------------------------------------------------------------
# Finder
# ----------------------------------------------------------------------

@dataclass
class PairSuggestion:
    dimensions: List[str]
    sql: str            # the concatenation a person would add as a new primary key dimension

    def to_dict(self) -> Dict[str, Any]:
        return {'dimensions': list(self.dimensions), 'sql': self.sql}


@dataclass
class ViewResult:
    view: str
    file: str
    status: str
    message: str = ''
    table: Optional[str] = None
    dialect: Optional[str] = None
    strategy: Optional[str] = None
    filter: Optional[str] = None
    rows_checked: Optional[int] = None
    candidates: List[Candidate] = field(default_factory=list)
    chosen_key: Optional[str] = None
    existing_key: Optional[str] = None
    pair_suggestions: List[PairSuggestion] = field(default_factory=list)
    applied: bool = False
    key_source: Optional[str] = None   # 'only_qualifier', 'key_flag' or 'existing'
    write_blocked: bool = False        # --key named a column that did not qualify: write nothing

    @property
    def qualifying(self) -> List[str]:
        return [c.name for c in self.candidates if c.qualifies]

    def sample_description(self) -> str:
        """How the sample was taken, for report lines"""
        if self.rows_checked is None:
            return 'not queried'
        how = self.strategy or 'unknown'
        if self.filter:
            how += f": {self.filter}"
        return f"{self.rows_checked:,} rows ({how})"

    def to_dict(self) -> Dict[str, Any]:
        return {
            'view': self.view,
            'file': self.file,
            'table': self.table,
            'dialect': self.dialect,
            'strategy': self.strategy,
            'filter': self.filter,
            'rows_checked': self.rows_checked,
            'candidates': [
                {'name': c.name, 'column': c.column, 'evidence': c.evidence,
                 'distinct_count': c.distinct_count, 'null_count': c.null_count, 'qualifies': c.qualifies,
                 'note': c.note}
                for c in self.candidates
            ],
            'qualifying': self.qualifying,
            'chosen_key': self.chosen_key,
            'key_source': self.key_source,
            'existing_key': self.existing_key,
            'pair_suggestions': [p.to_dict() for p in self.pair_suggestions],
            'status': self.status,
            'message': self.message,
            'applied': self.applied,
        }


@dataclass
class FinderOptions:
    sample: SampleOptions = field(default_factory=SampleOptions)
    min_rows: int = 1000
    max_candidates: int = 12
    max_pairs: int = 10
    recheck: bool = False
    keys: Dict[str, str] = field(default_factory=dict)   # --key VIEW=DIMENSION choices


class PrimaryKeyFinder:
    """Examines one view at a time. OmniConnectionError propagates; other query errors are recorded."""

    def __init__(self, client, dialect: Dialect, options: Optional[FinderOptions] = None,
                 dialect_name: Optional[str] = None, foreign_key_hints: Optional[ForeignKeyHints] = None):
        self.client = client
        self.dialect = dialect
        self.dialect_name = dialect_name or dialect.name
        self.options = options or FinderOptions()
        self.foreign_key_hints = foreign_key_hints or ForeignKeyHints()

    def examine(self, view: ViewInfo) -> ViewResult:
        result = ViewResult(view=view.name, file=str(view.path), status=STATUS_SKIPPED,
                            dialect=self.dialect_name, existing_key=view.existing_key,
                            table=str(view.table) if view.table else None)
        if view.skip_reason:
            result.message = view.skip_reason
            return result
        if view.existing_key and not self.options.recheck:
            result.message = f"already has a primary key ({view.existing_key}); use --recheck to test it"
            return result

        result.candidates = find_candidates(view, self.options.max_candidates,
                                            include_existing_key=self.options.recheck)
        if not result.candidates:
            result.status = STATUS_NO_KEY_FOUND
            result.message = "no dimension maps to a plain column, so there was nothing to test"
            return result
        for candidate in result.candidates:
            candidate.note = self.foreign_key_hints.note_for(candidate.name, view)

        try:
            plan = self.dialect.plan(view.table, self.client, self.options.sample)
            result.strategy, result.filter = plan.strategy, plan.filter
            if plan.needs_decision:
                result.status = STATUS_NEEDS_DECISION
                result.message = plan.needs_decision
                return result
            rows = self.client.run_sql(assert_read_only(
                build_candidate_sql(self.dialect, view.table, result.candidates, plan)))
            if not rows:
                raise OmniQueryError("the query returned no rows")
            self._evaluate(view, result, rows[0], plan)
        except OmniConnectionError:
            raise
        except OmniQueryError as e:
            result.status = STATUS_QUERY_ERROR
            result.message = _shorten(str(e), 500)
        return result

    # -- evaluation ------------------------------------------------------

    def _evaluate(self, view: ViewInfo, result: ViewResult, row: Dict[str, Any], plan: SamplePlan) -> None:
        total = _to_int(_value(row, 'total_rows'))
        result.rows_checked = total
        for i, candidate in enumerate(result.candidates):
            candidate.distinct_count = _to_int(_value(row, f'distinct_{i}'))
            candidate.null_count = _to_int(_value(row, f'nulls_{i}'))
        sample = result.sample_description()

        if total < self.options.min_rows:
            result.status = STATUS_SAMPLE_TOO_SMALL
            result.message = (f"sample too small: {sample} is below --min-rows ({self.options.min_rows:,}), "
                              "so nothing was accepted as a key")
            return

        for candidate in result.candidates:
            candidate.qualifies = candidate.distinct_count == total and candidate.null_count == 0
        qualifiers = [c for c in result.candidates if c.qualifies]

        if view.existing_key:  # --recheck: report on the existing key, never replace it
            existing = next((c for c in result.candidates if c.name == view.existing_key), None)
            if existing is not None and existing.qualifies:
                result.status = STATUS_KEY_FOUND
                result.chosen_key = existing.name
                result.key_source = 'existing'
                result.message = f"existing primary key {existing.name} is unique and not null in {sample}"
            elif existing is not None:
                result.status = STATUS_NO_KEY_FOUND
                result.message = (
                    f"existing primary key {existing.name} is NOT a key: {existing.distinct_count:,} distinct "
                    f"values and {existing.null_count:,} nulls in {sample}. The file was not changed")
                if qualifiers:
                    result.message += f". Unique in the sample: {', '.join(c.name for c in qualifiers)}"
            else:
                result.status = STATUS_NO_KEY_FOUND
                result.message = (f"existing primary key {view.existing_key} is not a plain column, "
                                  "so it could not be tested")
            if view.name in self.options.keys:
                result.message += ". --key was ignored: an existing primary key is never replaced"
            return

        requested = self.options.keys.get(view.name)
        refusal = ''
        if requested is not None:
            named = next((c for c in result.candidates if c.name == requested), None)
            if named is not None and named.qualifies:
                result.status = STATUS_KEY_FOUND
                result.chosen_key = named.name
                result.key_source = 'key_flag'
                result.message = f"{named.name} (chosen with --key) is unique and not null in {sample}"
                others = [c.name for c in qualifiers if c is not named]
                if others:
                    result.message += f". Also unique in the sample: {', '.join(others)}"
                return
            result.write_blocked = True
            if named is None:
                why = "it is not one of the columns tested (not a dimension that reads a plain column, or beyond --max-candidates)"
            else:
                why = (f"it is NOT a key: {named.distinct_count:,} distinct values and "
                       f"{named.null_count:,} nulls in {sample}")
            refusal = f"--key {view.name}={requested} was refused: {why}. Nothing was written. "

        if len(qualifiers) == 1:
            result.status = STATUS_KEY_FOUND
            result.chosen_key = qualifiers[0].name
            result.key_source = 'only_qualifier'
            result.message = refusal + f"{qualifiers[0].name} is the only column unique and not null in {sample}"
            return
        if len(qualifiers) > 1:
            result.status = STATUS_MULTIPLE_CANDIDATES
            listed = ', '.join(f"{c.name} ({c.evidence}" + (f"; {c.note}" if c.note else '') + ")" for c in qualifiers)
            result.message = refusal + (
                f"{len(qualifiers)} columns are unique and not null in {sample}: {listed}. "
                "The sample cannot tell which one is the key, so nothing was chosen or written. "
                f"Choose one with --key {view.name}=DIMENSION")
            return

        self._test_pairs(view, result, plan, total)
        result.message = refusal + result.message

    def _test_pairs(self, view: ViewInfo, result: ViewResult, plan: SamplePlan, total: int) -> None:
        sample = result.sample_description()
        usable = [c for c in result.candidates if c.null_count == 0 and (c.distinct_count or 0) > 1]
        pairs = list(itertools.islice(itertools.combinations(usable, 2), max(0, self.options.max_pairs)))
        result.status = STATUS_NO_KEY_FOUND
        result.message = f"no single column is unique and not null in {sample}"
        if not pairs:
            return
        try:
            rows = self.client.run_sql(assert_read_only(build_pair_sql(self.dialect, view.table, pairs, plan)))
        except OmniConnectionError:
            raise
        except OmniQueryError as e:
            result.message += f". The pair query failed: {_shorten(str(e), 300)}"
            return
        if not rows:
            return
        pair_total = _to_int(_value(rows[0], 'total_rows'))
        for i, (a, b) in enumerate(pairs):
            if pair_total >= self.options.min_rows and _to_int(_value(rows[0], f'pair_{i}')) == pair_total:
                expression = self.dialect.concat_sql([f"${{{a.name}}}", f"${{{b.name}}}"], separator='-')
                result.pair_suggestions.append(PairSuggestion([a.name, b.name], expression))
        if result.pair_suggestions:
            result.status = STATUS_PAIR_SUGGESTED
            names = '; '.join(' + '.join(p.dimensions) for p in result.pair_suggestions)
            result.message += (f". Unique together in the sample: {names}. Nothing was written: "
                               "add a dimension with the concatenation and mark it as the primary key by hand")
        else:
            result.message += f", and none of the {len(pairs)} pair(s) tested is unique together"


# ----------------------------------------------------------------------
# Writing
# ----------------------------------------------------------------------

def render_with_primary_key(text: str, dimension: str) -> str:
    """
    The view file text with primary_key: true and the sample tag set on one dimension.

    First choice is a line edit that leaves every other byte of the file alone.
    The edit is then parsed and compared with the change it was meant to make;
    if it does not match (an unusual layout), the file is written with a ruamel
    round trip instead, which keeps comments and key order but may normalise spacing.
    """
    yaml = _round_trip_yaml(text)
    data = yaml.load(text)
    dims = data.get('dimensions') if isinstance(data, dict) else None
    if not isinstance(dims, dict) or dimension not in dims:
        raise ValueError(f"dimension {dimension} not found")
    existing = [n for n, d in dims.items() if isinstance(d, dict) and d.get('primary_key') is True]
    if existing:
        raise ValueError(f"view already has a primary key ({', '.join(map(str, existing))})")

    edited = None
    try:
        edited = _edit_lines(text, dims, dimension)
    except Exception:  # any surprise in the layout: fall back to the round trip
        edited = None

    dim = dims[dimension]
    if not isinstance(dim, dict):
        dim = CommentedMap()
        dims[dimension] = dim
    dim['primary_key'] = True
    tags = dim.get('tags')
    if isinstance(tags, list):
        if PRIMARY_KEY_TAG not in tags:
            tags.append(PRIMARY_KEY_TAG)
    else:
        new_tags = CommentedSeq([PRIMARY_KEY_TAG])
        new_tags.fa.set_flow_style()
        dim['tags'] = new_tags
    out = io.StringIO()
    yaml.dump(data, out)
    round_trip = out.getvalue()

    if edited is not None and _same_yaml(edited, round_trip):
        return edited
    return round_trip


def _same_yaml(a: str, b: str) -> bool:
    try:
        loader = YAML(typ='safe')
        return loader.load(a) == YAML(typ='safe').load(b)
    except Exception:
        return False


def _edit_lines(text: str, dims: Any, dimension: str) -> Optional[str]:
    """Insert the key (and tag) by editing lines only. None when the layout is not one this handles."""
    lines = text.splitlines(keepends=True)
    newline = '\r\n' if text.endswith('\r\n') else '\n'
    if lines and not lines[-1].endswith(('\n', '\r')):
        lines[-1] += newline
    key_line, key_col = dims.lc.key(dimension)
    dim = dims[dimension]

    if not dim:  # `id:` or `id: {}`: turn it into a block mapping
        match = re.match(r'^(\s*[^\s#:][^:]*:)[ \t]*(\{\s*\})?[ \t]*(#.*)?$', lines[key_line].rstrip('\r\n'))
        if not match:
            return None
        indent = ' ' * (key_col + 2)
        head = match.group(1) + (f"  {match.group(3)}" if match.group(3) else '')
        lines[key_line:key_line + 1] = [head + newline, f"{indent}primary_key: true{newline}",
                                        f"{indent}tags: [{PRIMARY_KEY_TAG}]{newline}"]
        return ''.join(lines)

    if not isinstance(dim, CommentedMap) or dim.fa.flow_style():
        return None
    indent = ' ' * dim.lc.key(next(iter(dim)))[1]

    # last content line of the dimension's block
    last = key_line
    for i in range(key_line + 1, len(lines)):
        stripped = lines[i].strip()
        if not stripped or stripped.startswith('#'):
            continue
        if len(lines[i]) - len(lines[i].lstrip(' ')) <= key_col:
            break
        last = i
    insert_at, added = last + 1, [f"{indent}primary_key: true{newline}"]

    tags = dim.get('tags')
    if 'tags' not in dim:
        added.append(f"{indent}tags: [{PRIMARY_KEY_TAG}]{newline}")
    elif isinstance(tags, list) and PRIMARY_KEY_TAG in tags:
        pass
    elif isinstance(tags, CommentedSeq) and tags.fa.flow_style():
        tag_line = dim.lc.key('tags')[0]
        match = re.match(r'^(\s*tags:\s*\[)(.*?)(\s*)(\][ \t]*(#.*)?)$', lines[tag_line].rstrip('\r\n'))
        if not match:
            return None
        separator = ', ' if match.group(2).strip() else ''
        lines[tag_line] = (match.group(1) + match.group(2) + separator + PRIMARY_KEY_TAG
                           + match.group(3) + match.group(4) + newline)
    elif isinstance(tags, CommentedSeq) and len(tags):
        item_line = tags.lc.item(len(tags) - 1)[0]
        match = re.match(r'^(\s*)-\s', lines[item_line])
        if not match:
            return None
        lines.insert(item_line + 1, f"{match.group(1)}- {PRIMARY_KEY_TAG}{newline}")
        if item_line < insert_at:
            insert_at += 1
    else:
        return None

    lines[insert_at:insert_at] = added
    return ''.join(lines)


def apply_primary_key(path: Path, dimension: str) -> None:
    """Write the key into the view file (atomic replace). Raises ValueError if it cannot be done safely."""
    path = Path(path)
    content = render_with_primary_key(path.read_text(), dimension)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, 'w') as f:
            f.write(content)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


# ----------------------------------------------------------------------
# Reports
# ----------------------------------------------------------------------

def count_by_status(results: Sequence[ViewResult]) -> Dict[str, int]:
    counts = {status: 0 for status in STATUS_ORDER}
    for result in results:
        counts[result.status] = counts.get(result.status, 0) + 1
    return counts


def results_to_json(results: Sequence[ViewResult], model_id: str, dialect: str, applied: bool,
                    branch_id: Optional[str] = None) -> Dict[str, Any]:
    return {
        'model_id': model_id,
        'branch_id': branch_id,
        'dialect': dialect,
        'mode': 'apply' if applied else 'dry_run',
        'note': SAMPLE_NOTE,
        'summary': count_by_status(results),
        'views': [r.to_dict() for r in results],
    }


def _cell(text: Any) -> str:
    return str(text if text is not None else '').replace('|', '\\|').replace('\n', ' ')


def render_report(results: Sequence[ViewResult], model_id: str, dialect: str, applied: bool,
                  branch_id: Optional[str] = None) -> str:
    counts = count_by_status(results)
    lines = [
        "# Primary key finder report",
        "",
        f"- **Model:** `{model_id}`" + (f" (branch `{branch_id}`)" if branch_id else ""),
        f"- **Dialect:** {dialect}",
        "- **Mode:** " + ("apply (keys were written to the view files)" if applied
                          else "dry run (no view file was changed)"),
        f"- **Views:** {len(results)}",
        "",
        f"> **About samples.** {SAMPLE_NOTE}",
        "",
        "## Summary",
        "",
        "| Status | Views |",
        "|--------|-------|",
    ]
    lines += [f"| `{status}` | {counts[status]} |" for status in STATUS_ORDER]

    found = [r for r in results if r.status == STATUS_KEY_FOUND]
    if found:
        lines += ["", "## Keys found in the sample", "",
                  "| View | Key | How it was chosen | Sample | Written |",
                  "|------|-----|-------------------|--------|---------|"]
        how_chosen = {'only_qualifier': 'the only column unique in the sample',
                      'key_flag': 'chosen with `--key` (unique in the sample)',
                      'existing': 'existing key, unique in the sample'}
        for r in found:
            if r.applied:
                written = 'yes'
            elif r.existing_key == r.chosen_key:
                written = 'already set'
            else:
                written = 'no (a refused `--key` blocked it)' if r.write_blocked else 'no'
            lines.append(f"| `{_cell(r.view)}` | `{_cell(r.chosen_key)}` | {how_chosen.get(r.key_source, '')} | "
                         f"{_cell(r.sample_description())} | {written} |")

    several = [r for r in results if r.status == STATUS_MULTIPLE_CANDIDATES]
    if several:
        lines += ["", "## Several columns qualified (nothing written)", "",
                  "More than one column was unique and not null in the sample for these views. A sample cannot tell "
                  "a real key from a column that only happens to be unique in the rows read (a foreign key in a "
                  "small table, for example), so the script does not pick one. Decide which column identifies a "
                  "row, then re-run with `--key VIEW=DIMENSION` (and `--apply` to write it). A note of "
                  "\"likely foreign key\" is advice from the names in the other view files, not a test."]
        for r in several:
            lines += ["", f"### {r.view}", "", f"Sample: {r.sample_description()}.", "",
                      "| Dimension | Evidence | Note | To choose it |",
                      "|-----------|----------|------|--------------|"]
            for c in r.candidates:
                if c.qualifies:
                    lines.append(f"| `{_cell(c.name)}` | {c.evidence} | {_cell(c.note or '')} | "
                                 f"`--key {_cell(r.view)}={_cell(c.name)}` |")

    pairs = [r for r in results if r.status == STATUS_PAIR_SUGGESTED]
    if pairs:
        lines += ["", "## Pair suggestions (not written)", "",
                  "No single column was unique in the sample for these views, but a pair of columns was. "
                  "Nothing was written. To use a pair, add a dimension like the one shown and review it first."]
        for r in pairs:
            lines += ["", f"### {r.view}", "", f"Sample: {r.sample_description()}.", ""]
            for suggestion in r.pair_suggestions:
                name = '_'.join(suggestion.dimensions) + '_key'
                lines += [f"`{suggestion.dimensions[0]}` + `{suggestion.dimensions[1]}`:", "",
                          "```yaml", f"  {name}:", f"    sql: {suggestion.sql}",
                          "    primary_key: true", "    hidden: true", "```", ""]

    other_statuses = (STATUS_NO_KEY_FOUND, STATUS_SAMPLE_TOO_SMALL, STATUS_NEEDS_DECISION, STATUS_QUERY_ERROR)
    others = [r for r in results if r.status in other_statuses]
    if others:
        lines += ["", "## Views without a key", "",
                  "| View | Status | Sample | Message |",
                  "|------|--------|--------|---------|"]
        for r in sorted(others, key=lambda r: other_statuses.index(r.status)):
            lines.append(f"| `{_cell(r.view)}` | `{r.status}` | {_cell(r.sample_description())} | {_cell(r.message)} |")

    skipped = [r for r in results if r.status == STATUS_SKIPPED]
    if skipped:
        lines += ["", "## Skipped", "", "| View | File | Reason |", "|------|------|--------|"]
        lines += [f"| `{_cell(r.view)}` | `{_cell(r.file)}` | {_cell(r.message)} |" for r in skipped]

    tested = [r for r in results if r.rows_checked is not None]
    if tested:
        lines += ["", "## Candidate detail"]
        for r in tested:
            lines += ["", f"### {r.view}", "",
                      f"Table `{r.table}`. Sample: {r.sample_description()}.", "",
                      "| Dimension | Column | Evidence | Distinct | Nulls | Unique in sample | Note |",
                      "|-----------|--------|----------|----------|-------|------------------|------|"]
            for c in r.candidates:
                lines.append(f"| `{_cell(c.name)}` | `{_cell(c.column)}` | {c.evidence} | "
                             f"{c.distinct_count:,} | {c.null_count:,} | {'yes' if c.qualifies else 'no'} | "
                             f"{_cell(c.note or '')} |")
    lines.append("")
    return '\n'.join(lines)
