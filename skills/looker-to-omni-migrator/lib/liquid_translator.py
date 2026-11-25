"""
Liquid Translator - Looker Liquid in SQL to Omni Mustache

Translates the Liquid constructs that have an exact Omni equivalent. The SQL
is tokenized (Liquid tags, SQL strings, quoted identifiers and comments), so
a construct is only translated when its position in the SQL is understood.

## Translated

| Looker | Omni |
|--------|------|
| `{% condition view.f %} expr {% endcondition %}` | `{{# view.f.filter }} expr {{/ view.f.filter }}` |
| `{% parameter view.p %}`, `{{ view.p._parameter_value }}` | `{{ filters.view.p.value }}` |
| `{% date_start view.f %}` / `{% date_end view.f %}` | `{{ filters.view.f.range_start }}` / `.range_end` |
| `{{ view.f._in_query }}`, `._is_selected`, `._is_filtered` | `{{ view.f.in_query }}`, `.is_selected`, `.is_filtered` |
| `{{ view._in_query }}` | `{{ view.in_query }}` |
| `{{ _user_attributes['x'] }}` | `{{ omni_attributes.x }}` |

A parameter value is injected for `string`, `number` and `unquoted`
parameters only (Omni's rendering of the other types is not confirmed).
Whitespace control (`{%- ... -%}`, `{{- ... -}}`) is accepted.

## All or nothing

If ANY Liquid construct of a SQL block is outside the table (`{% if %}`,
`{% assign %}`, loops, Liquid filters, any other `{{ ... }}`), is malformed,
is nested, sits where it cannot be translated exactly, or names a field that
cannot be resolved, the whole block is left unconverted:
`LiquidTranslation.sql` is None and `blockers` names every construct that
stopped it.

## Comments

A SQL comment (`-- ...` or `/* ... */`) that holds Liquid is removed from the
output: a comment has no effect on the query. It is only removed when the
Liquid blocks opened in it are closed in it (see `remove_liquid_comments`).
Liquid inside a quoted string or quoted identifier still follows the rules
below.

## Quotes

Omni renders a `string` value as a quoted SQL literal, a `number` bare and a
`column` as a quoted identifier, so quotes that directly wrap a parameter tag
(`'{% parameter p %}'`) are dropped. A tag embedded in a longer quoted string
or in a longer identifier is never translated. User attributes are
interpolated raw by both tools: quotes around them are kept.

## Field references

The translator does not know the LookML project. The caller passes a
`LiquidContext` whose `field` callable resolves a Liquid reference (`f` or
`view.f`) to a `LiquidField` (the Omni view and field to name in the token),
or gives the reason it cannot.

## Usage

```python
from lib.liquid_translator import LiquidContext, translate_liquid

translation = translate_liquid(sql, LiquidContext(field=resolve_field, view=resolve_view))
if translation.sql is None:
    print(translation.reason)       # names the constructs that blocked it
else:
    print(translation.sql, translation.translated)
```
"""

import re
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

_IDENT = r'[A-Za-z_][A-Za-z0-9_]*'
_REF = _IDENT + r'(?:\.' + _IDENT + r')?'

# Every Mustache form the translator writes (and the inverted section, which
# only a hand-written view holds)
OMNI_TOKEN_RE = re.compile(
    r'\{\{\s*(?:'
    r'[#/^]\s*' + _IDENT + r'\.' + _IDENT + r'\.filter'
    r'|filters\.' + _IDENT + r'\.' + _IDENT + r'\.(?:value|range_start|range_end)'
    r'|' + _IDENT + r'\.' + _IDENT + r'\.(?:in_query|is_selected|is_filtered)'
    r'|' + _IDENT + r'\.in_query'
    r'|omni_attributes\.' + _IDENT +
    r')\s*\}\}'
)
MUSTACHE_RE = re.compile(r'\{\{.*?\}\}', re.DOTALL)

# Looker filter and parameter `type` -> Omni filter-only field `type`.
# `unquoted` is decided from how the parameter is used (column or string).
FILTER_TYPE_MAP = {
    'string': 'string',
    'number': 'number',
    'date': 'timestamp',
    'date_time': 'timestamp',
    'yesno': 'boolean',
}

# Report line for a SQL comment removed because it holds Liquid
COMMENT_REMOVED = "SQL comment containing Liquid removed"

FILTER_KINDS = ('parameter', 'filter')
DATE_TYPES = {'date', 'date_time', 'time'}

_QUERY_STATE = {'_in_query': 'in_query', '_is_selected': 'is_selected', '_is_filtered': 'is_filtered'}
_CONDITIONAL_TAGS = {'if', 'elsif', 'else', 'endif', 'unless', 'endunless', 'case', 'when', 'endcase'}
_ASSIGN_TAGS = {'assign', 'capture', 'endcapture', 'increment', 'decrement'}
_LOOP_TAGS = {'for', 'endfor', 'break', 'continue', 'cycle', 'tablerow', 'endtablerow'}

_USER_ATTRIBUTE_RE = re.compile(r"""_user_attributes\[\s*(?:'(""" + _IDENT + r""")'|"(""" + _IDENT + r""")")\s*\]""")
_OUTPUT_FIELD_RE = re.compile(r'(' + _REF + r')\.(_parameter_value|_in_query|_is_selected|_is_filtered)')
_TABLE_SOURCE_RE = re.compile(r'\b(?:FROM|JOIN)\s+$', re.IGNORECASE)
_TABLE_PREFIX = '${TABLE}.'


@dataclass
class LiquidField:
    """The Omni field a Liquid reference points to"""
    view: str  # Omni view name to write in the token
    name: str  # Omni field name
    kind: str  # parameter, filter, dimension, dimension_group or measure
    looker_type: str  # Looker `type` of the field ('time' for a dimension group)
    origin: str  # view whose migration writes the field (a parent when inherited)
    local: bool = True  # the field belongs to the view being converted (its own or inherited)
    omni_type: Optional[str] = None  # filter-only fields: the Omni `type` the tool writes
    problem: Optional[str] = None  # why its value cannot be injected (e.g. used in two incompatible ways)
    timeframe: Optional[str] = None  # set when the reference named a dimension group timeframe
    alias: bool = False  # `view` is a join alias kept from Looker: it only exists in a topic that declares it

    @property
    def token(self) -> str:
        return f"{self.view}.{self.name}"


FieldResolver = Callable[[str], Tuple[Optional[LiquidField], Optional[str]]]
ViewResolver = Callable[[str], Tuple[Optional[str], Optional[str]]]


def _unresolved(ref: str) -> Tuple[None, str]:
    return None, f"`{ref}` cannot be resolved without the LookML project"


@dataclass
class LiquidContext:
    """Resolves the references of a SQL block: `field('view.f')` and `view('view')`"""
    field: FieldResolver = _unresolved
    view: ViewResolver = _unresolved


@dataclass
class LiquidConstruct:
    """One Liquid tag (`{% ... %}`) or output (`{{ ... }}`) found in a SQL string"""
    raw: str
    kind: str  # condition, endcondition, parameter, parameter_value, date_start, date_end,
    #            in_query, is_selected, is_filtered, user_attribute or other
    ref: Optional[str]  # field or view reference, or the user attribute name
    start: int
    end: int
    label: str  # how the report names it when it is not translated
    context: str = 'code'  # code, quoted (SQL string or quoted identifier) or comment
    quote: Optional[str] = None  # the quote character it sits in
    wrapped: bool = False  # the quotes hold this construct and nothing else
    quote_span: Optional[Tuple[int, int]] = None  # (opening quote, after the closing quote) when wrapped
    comment_span: Optional[Tuple[int, int]] = None  # (start, end) of the SQL comment it sits in


@dataclass
class LiquidTranslation:
    """Result of translating the Liquid of one SQL block"""
    sql: Optional[str]  # translated SQL, None when the block is not converted
    blockers: List[str] = field(default_factory=list)  # constructs that stopped the conversion
    translated: List[str] = field(default_factory=list)  # "`looker` → `omni`", for the report
    fields: List[LiquidField] = field(default_factory=list)  # every field referenced
    attributes: List[str] = field(default_factory=list)  # user attributes referenced

    @property
    def reason(self) -> str:
        """Why the block is not converted, naming each construct"""
        return "sql uses Liquid that is not converted automatically: " + '; '.join(dict.fromkeys(self.blockers))


def contains_liquid(text: Optional[str]) -> bool:
    """True if the text opens a Liquid tag or output (terminated or not)"""
    return bool(text) and ('{%' in text or '{{' in text)


def find_looker_liquid(sql: Optional[str]) -> List[str]:
    """Liquid left in a SQL string: `{%` tags and any `{{ ... }}` that is not an Omni Mustache token"""
    if not sql:
        return []
    left = ['{%'] if '{%' in sql else []
    left += [match.group(0) for match in MUSTACHE_RE.finditer(sql) if not OMNI_TOKEN_RE.fullmatch(match.group(0))]
    if '{{' in MUSTACHE_RE.sub('', sql):
        left.append('{{')
    return left


def assert_no_looker_liquid(sql: Optional[str], where: str = "sql"):
    """
    Final guard before a SQL block is written: it must hold no `{%` and no
    Looker-style `{{ ... }}`

    Raises:
        ValueError: Liquid is left in the SQL
    """
    left = find_looker_liquid(sql)
    if left:
        raise ValueError(f"{where} still contains Liquid after translation ({', '.join(left)}); nothing was written")


# ----------------------------------------------------------------------
# Tokenizer
# ----------------------------------------------------------------------

def _other_tag_label(name: str) -> str:
    tag = f"`{{% {name} %}}`"
    if name in _CONDITIONAL_TAGS:
        return f"{tag} (conditional logic: Omni cannot remove SQL text)"
    if name in _ASSIGN_TAGS:
        return f"{tag} (variable assignment)"
    if name in _LOOP_TAGS:
        return f"{tag} (loop)"
    return f"{tag} (no Omni equivalent)"


def _short(raw: str) -> str:
    text = ' '.join(raw.split())
    return text if len(text) <= 80 else text[:77] + '...'


def _classify(raw: str, start: int) -> LiquidConstruct:
    """Name a Liquid tag or output and extract its reference"""
    end = start + len(raw)
    inner = raw[2:-2].strip()
    if inner.startswith('-'):
        inner = inner[1:].strip()
    if inner.endswith('-'):
        inner = inner[:-1].strip()

    def construct(kind: str, ref: Optional[str], label: str) -> LiquidConstruct:
        return LiquidConstruct(raw=raw, kind=kind, ref=ref, start=start, end=end, label=label)

    if raw.startswith('{%'):
        words = inner.split()
        name = words[0] if words else ''
        if name in ('condition', 'parameter', 'date_start', 'date_end'):
            if len(words) == 2 and re.fullmatch(_REF, words[1]):
                return construct(name, words[1], f"`{_short(raw)}`")
            return construct('other', None, f"`{_short(raw)}` (malformed `{{% {name} %}}`)")
        if name == 'endcondition' and len(words) == 1:
            return construct('endcondition', None, "`{% endcondition %}`")
        if not name:
            return construct('other', None, "`{% %}` (empty tag)")
        return construct('other', None, _other_tag_label(name))

    if '|' in inner:
        return construct('other', None, f"`{_short(raw)}` (Liquid filter)")
    attribute = _USER_ATTRIBUTE_RE.fullmatch(inner)
    if attribute:
        return construct('user_attribute', attribute.group(1) or attribute.group(2), f"`{_short(raw)}`")
    output = _OUTPUT_FIELD_RE.fullmatch(inner)
    if output:
        kind = 'parameter_value' if output.group(2) == '_parameter_value' else _QUERY_STATE[output.group(2)]
        return construct(kind, output.group(1), f"`{_short(raw)}`")
    return construct('other', None, f"`{_short(raw)}` (Liquid output with no Omni equivalent)")


def scan_liquid(sql: str) -> Tuple[List[LiquidConstruct], List[str]]:
    """
    Find every Liquid construct of a SQL string, with its SQL context

    The SQL is read once, tracking string literals (`'...'`), quoted
    identifiers (`"..."`, `` `...` ``) and comments. Liquid is recognised
    everywhere (Looker renders it before the SQL is parsed) and what a tag
    holds never changes the SQL state.

    Returns:
        (constructs in order, problems): a problem is a `{%` or `{{` that is
        never closed
    """
    constructs, problems, _ = _scan(sql)
    return constructs, problems


def _scan(sql: str) -> Tuple[List[LiquidConstruct], List[str], List[Tuple[int, int]]]:
    """`scan_liquid`, plus the (start, end) span of every SQL comment"""
    constructs: List[LiquidConstruct] = []
    problems: List[str] = []
    comments: List[Tuple[int, int]] = []
    state: Optional[str] = None  # None (code), a quote character, '--' or '/*'
    quote_open = -1
    comment_open = -1
    in_quote: List[LiquidConstruct] = []
    in_comment: List[LiquidConstruct] = []
    i, n = 0, len(sql)

    def close_quote(position: int):
        for item in in_quote:
            item.wrapped = len(in_quote) == 1 and item.start == quote_open + 1 and item.end == position
            if item.wrapped:
                item.quote_span = (quote_open, position + 1)

    def close_comment(end: int):
        comments.append((comment_open, end))
        for item in in_comment:
            item.comment_span = (comment_open, end)

    while i < n:
        if sql.startswith('{%', i) or sql.startswith('{{', i):
            closer = '%}' if sql[i + 1] == '%' else '}}'
            j = sql.find(closer, i + 2)
            if j < 0:
                problems.append(f"`{_short(sql[i:i + 40])}` (a `{sql[i:i + 2]}` that is never closed)")
                break
            item = _classify(sql[i:j + 2], i)
            if state in ('--', '/*'):
                item.context = 'comment'
                in_comment.append(item)
            elif state is not None:
                item.context, item.quote = 'quoted', state
                in_quote.append(item)
            constructs.append(item)
            i = j + 2
            continue

        ch = sql[i]
        if state is None:
            if ch in '\'"`':
                state, quote_open, in_quote = ch, i, []
            elif sql.startswith('--', i):
                state, comment_open, in_comment = '--', i, []
            elif sql.startswith('/*', i):
                state, comment_open, in_comment = '/*', i, []
                i += 2  # `/*/` opens a comment, it does not also close it
                continue
        elif state in ('\'', '"'):
            if ch == '\\':
                i += 2
                continue
            if ch == state:
                if state == '\'' and sql.startswith("''", i):
                    i += 2
                    continue
                close_quote(i)
                state = None
        elif state == '`':
            if ch == '`':
                close_quote(i)
                state = None
        elif state == '--':
            if ch == '\n':
                close_comment(i)
                state = None
        elif state == '/*' and sql.startswith('*/', i):
            close_comment(i + 2)
            state = None
            i += 2
            continue
        i += 1
    if state in ('--', '/*'):
        close_comment(n)
    return constructs, problems, comments


# Liquid tags that open a block closed by `end<name>`, and the tags that only
# make sense inside one
_BLOCK_OPENERS = {'if', 'unless', 'case', 'for', 'capture', 'tablerow', 'condition', 'comment', 'raw'}
_BLOCK_BRANCHES = {'else', 'elsif', 'when', 'break', 'continue'}
_TAG_NAME_RE = re.compile(r'\{%-?\s*([A-Za-z_]+)')


def _unbalanced_block(items: List[LiquidConstruct]) -> Optional[str]:
    """
    The Liquid block tag that `items` (the Liquid of one run of comments)
    leaves open or closes without opening it; None when every block opened
    in the run is closed in it
    """
    open_tags: List[str] = []
    for item in items:
        match = _TAG_NAME_RE.match(item.raw)
        if not match:
            continue
        name = match.group(1)
        if name in _BLOCK_OPENERS:
            open_tags.append(name)
        elif name.startswith('end'):
            if not open_tags or open_tags[-1] != name[3:]:
                return f"`{{% {name} %}}`"
            open_tags.pop()
        elif name in _BLOCK_BRANCHES and not open_tags:
            return f"`{{% {name} %}}`"
    return f"`{{% {open_tags[-1]} %}}`" if open_tags else None


def remove_liquid_comments(sql: str) -> Tuple[Optional[str], List[str], List[str]]:
    """
    Remove every SQL comment that holds Liquid (a comment has no effect on
    the query)

    Looker renders Liquid before the SQL is read, so Liquid in a comment can
    still reach the code around it: `-- {% if x %}` on one line and
    `-- {% endif %}` three lines below make the code between them
    conditional. A comment is therefore only removed when the Liquid blocks
    opened in it are closed in it, or in the comments that follow it with
    nothing but whitespace between them (a commented-out multi-line block).
    An optimizer hint (`/*+ ... */`) is not removed either.

    Returns:
        (sql without those comments, blockers, removed): `sql` is None when
        a comment cannot be removed safely, and `blockers` says why;
        `removed` holds a short form of each comment removed.
    """
    constructs, problems, comments = _scan(sql)
    liquid: Dict[Tuple[int, int], List[LiquidConstruct]] = {}
    for item in constructs:
        if item.context == 'comment' and item.comment_span is not None:
            liquid.setdefault(item.comment_span, []).append(item)
    if problems or not liquid:
        return sql, [], []

    # Runs of comments with only whitespace between them
    runs: List[List[Tuple[int, int]]] = []
    for span in comments:
        if runs and not sql[runs[-1][-1][1]:span[0]].strip():
            runs[-1].append(span)
        else:
            runs.append([span])

    blockers: List[str] = []
    doomed: List[Tuple[int, int]] = []
    for run in runs:
        spans = [span for span in run if span in liquid]
        if not spans:
            continue
        unbalanced = _unbalanced_block([item for span in spans for item in liquid[span]])
        if unbalanced:
            blockers.append(
                f"`{_short(sql[spans[0][0]:spans[0][1]])}`: SQL comment with a Liquid block tag ({unbalanced}) that "
                f"is not opened and closed inside the comment, so it can change the SQL outside it"
            )
            continue
        for span in spans:
            if sql.startswith('/*+', span[0]):
                blockers.append(f"`{_short(sql[span[0]:span[1]])}`: optimizer hint that holds Liquid")
            else:
                doomed.append(span)
    if blockers:
        return None, blockers, []

    out, position, removed = [], 0, []
    for start, end in doomed:
        removed.append(_short(sql[start:end]))
        line_start = sql.rfind('\n', 0, start) + 1
        before = sql[line_start:start]
        newline = sql.find('\n', end)
        after = sql[end:newline if newline >= 0 else len(sql)]
        if not before.strip() and not after.strip() and line_start >= position:
            # The comment is the whole line: drop the line
            out.append(sql[position:line_start])
            position = newline + 1 if newline >= 0 else len(sql)
            continue
        kept = sql[position:start]
        if sql.startswith('--', start):
            kept = kept.rstrip(' \t')
        out.append(kept)
        # A block comment separates the tokens around it
        if sql.startswith('/*', start) and kept[-1:].strip() and sql[end:end + 1].strip():
            out.append(' ')
        position = end
    out.append(sql[position:])
    return ''.join(out), [], removed


def unquoted_use_form(sql: str, item: LiquidConstruct) -> Tuple[Optional[str], Optional[str]]:
    """
    How a `type: unquoted` parameter is used at one place of a SQL string

    Returns:
        ('literal', None): directly wrapped in single quotes, a string value
        ('identifier', None): a whole identifier: directly wrapped in double
            quotes or backticks, qualified with a dot on either side
            (`${TABLE}.<p>`, `<p>.table`), or the table after FROM or JOIN
        (None, reason): anything else, which is never translated
    """
    if item.context == 'comment':
        return None, "it sits inside a SQL comment"
    if item.context == 'quoted':
        if not item.wrapped:
            return None, ("it is embedded in a longer quoted string or identifier; each allowed value must hold "
                          "the full identifier")
        return ('literal' if item.quote == '\'' else 'identifier'), None

    before = sql[item.start - 1] if item.start > 0 else ''
    after = sql[item.end] if item.end < len(sql) else ''
    if re.fullmatch(r'[A-Za-z0-9_$}]', before) or re.fullmatch(r'[A-Za-z0-9_${]', after):
        return None, "it is embedded in a longer identifier; each allowed value must hold the full identifier"
    if before == '.' or after == '.' or _TABLE_SOURCE_RE.search(sql[:item.start]):
        return 'identifier', None
    return None, ("the SQL does not show whether the value is a column, a keyword or a number (it is not quoted, "
                  "not qualified with a dot and not the table after FROM or JOIN)")


# ----------------------------------------------------------------------
# Translation
# ----------------------------------------------------------------------

@dataclass
class _Replacement:
    start: int
    end: int
    text: str


def _resolve(item: LiquidConstruct, context: LiquidContext, kinds: Tuple[str, ...],
             blockers: List[str]) -> Optional[LiquidField]:
    """The field a construct names, when it resolves to one of `kinds`; else a blocker is recorded"""
    target, reason = context.field(item.ref)
    if target is None:
        blockers.append(f"{item.label}: {reason}")
        return None
    if target.kind not in kinds:
        blockers.append(f"{item.label}: `{item.ref}` is a {target.kind.replace('_', ' ')}, which this tag cannot use")
        return None
    return target


def _parameter_replacement(sql: str, item: LiquidConstruct, target: LiquidField,
                           blockers: List[str]) -> Optional[_Replacement]:
    """Where and how a parameter value is injected, quotes included; None (with a blocker) when it cannot be"""
    token = f"{{{{ filters.{target.token}.value }}}}"
    if target.problem:
        blockers.append(f"{item.label}: {target.problem}")
        return None
    if target.omni_type not in ('string', 'number', 'column'):
        blockers.append(
            f"{item.label}: a `type: {target.looker_type}` parameter becomes `type: {target.omni_type}`, and how "
            f"Omni renders such a value in SQL is not confirmed"
        )
        return None

    if target.looker_type == 'unquoted':
        form, reason = unquoted_use_form(sql, item)
        if form is None:
            blockers.append(f"{item.label}: `type: unquoted` parameter, and {reason}")
            return None
        expected = 'column' if form == 'identifier' else 'string'
        if target.omni_type != expected:
            blockers.append(
                f"{item.label}: `type: unquoted` parameter used here as "
                f"{'an identifier' if form == 'identifier' else 'a quoted string'}, but the filter-only field is "
                f"written with `type: {target.omni_type}`"
            )
            return None
        start, end = item.quote_span if item.wrapped else (item.start, item.end)
        # A column of the view's own table: Omni reads a bare column name
        if form == 'identifier' and sql[:start].endswith(_TABLE_PREFIX):
            start -= len(_TABLE_PREFIX)
        return _Replacement(start, end, token)

    if item.context == 'code':
        return _Replacement(item.start, item.end, token)
    if item.context == 'quoted' and item.wrapped and item.quote == '\'' and target.omni_type == 'string':
        # Omni renders a string value already quoted
        return _Replacement(item.quote_span[0], item.quote_span[1], token)
    if item.context == 'quoted' and item.wrapped:
        blockers.append(
            f"{item.label}: a `type: {target.looker_type}` parameter wrapped in {item.quote} quotes; Omni renders "
            f"the value as a `{target.omni_type}`, so the quoted form has no exact equivalent"
        )
    elif item.context == 'quoted':
        blockers.append(f"{item.label}: embedded in a longer quoted string or identifier (Omni injects the whole "
                        f"value, already quoted)")
    else:
        blockers.append(f"{item.label}: inside a SQL comment")
    return None


def translate_liquid(sql: str, context: Optional[LiquidContext] = None) -> LiquidTranslation:
    """
    Translate the Liquid of one SQL block to Omni Mustache

    The block converts only when every construct in it converts. Otherwise
    `sql` is None and `blockers` lists each construct that stopped it.
    """
    context = context or LiquidContext()
    cleaned, comment_blockers, removed = remove_liquid_comments(sql)
    if cleaned is None:
        return LiquidTranslation(sql=None, blockers=comment_blockers)
    if removed:
        translation = translate_liquid(cleaned, context)
        if translation.sql is not None:
            translation.translated = [f"{COMMENT_REMOVED}: `{comment}`" for comment in removed] \
                + translation.translated
        return translation
    constructs, blockers = scan_liquid(sql)
    replacements: List[_Replacement] = []
    translated: List[str] = []
    fields: List[LiquidField] = []
    attributes: List[str] = []

    def replace(item: LiquidConstruct, text: str, replacement: Optional[_Replacement] = None,
                target: Optional[LiquidField] = None):
        replacements.append(replacement or _Replacement(item.start, item.end, text))
        translated.append(f"`{_short(item.raw)}` → `{text}`")
        if target is not None:
            fields.append(target)

    def in_code(item: LiquidConstruct) -> bool:
        if item.context != 'code':
            blockers.append(f"{item.label}: inside a quoted string, quoted identifier or SQL comment")
        return item.context == 'code'

    index = 0
    while index < len(constructs):
        item = constructs[index]
        index += 1

        if item.kind == 'other':
            blockers.append(item.label)

        elif item.kind == 'condition':
            closing = constructs[index] if index < len(constructs) else None
            if closing is None or closing.kind != 'endcondition':
                blockers.append(f"{item.label}: no `{{% endcondition %}}` directly closes it "
                                f"(missing, or other Liquid is nested inside)")
                continue
            index += 1
            if not sql[item.end:closing.start].strip():
                blockers.append(f"{item.label}: nothing between the tag and `{{% endcondition %}}`")
                continue
            if not (in_code(item) and in_code(closing)):
                continue
            target = _resolve(item, context, ('filter', 'dimension', 'dimension_group'), blockers)
            if target is None:
                continue
            replace(item, f"{{{{# {target.token}.filter }}}}", target=target)
            replace(closing, f"{{{{/ {target.token}.filter }}}}")

        elif item.kind == 'endcondition':
            blockers.append("`{% endcondition %}` without a `{% condition %}` before it")

        elif item.kind in ('parameter', 'parameter_value'):
            target = _resolve(item, context, ('parameter',), blockers)
            if target is None:
                continue
            replacement = _parameter_replacement(sql, item, target, blockers)
            if replacement is not None:
                replace(item, replacement.text, replacement, target)

        elif item.kind in ('date_start', 'date_end'):
            if not in_code(item):
                continue
            target = _resolve(item, context, ('filter', 'dimension', 'dimension_group'), blockers)
            if target is None:
                continue
            if target.looker_type not in DATE_TYPES:
                blockers.append(f"{item.label}: `{item.ref}` is not a date field (type `{target.looker_type}`)")
                continue
            bound = 'range_start' if item.kind == 'date_start' else 'range_end'
            replace(item, f"{{{{ filters.{target.token}.{bound} }}}}", target=target)

        elif item.kind in ('in_query', 'is_selected', 'is_filtered'):
            if not in_code(item):
                continue
            token, target = _query_state_token(item, context, blockers)
            if token is not None:
                replace(item, f"{{{{ {token}.{item.kind} }}}}", target=target)

        elif item.kind == 'user_attribute':
            # Interpolated raw by both tools: any quotes around it stay
            replace(item, f"{{{{ omni_attributes.{item.ref} }}}}")
            attributes.append(item.ref)

    if blockers:
        return LiquidTranslation(sql=None, blockers=blockers)

    out, position = [], 0
    for replacement in sorted(replacements, key=lambda r: r.start):
        out.append(sql[position:replacement.start])
        out.append(replacement.text)
        position = replacement.end
    out.append(sql[position:])
    result = ''.join(out)
    left = find_looker_liquid(result)
    if left:
        return LiquidTranslation(sql=None, blockers=[f"Liquid left after translation ({', '.join(left)})"])
    return LiquidTranslation(sql=result, translated=translated, fields=fields,
                             attributes=list(dict.fromkeys(attributes)))


def _query_state_token(item: LiquidConstruct, context: LiquidContext,
                       blockers: List[str]) -> Tuple[Optional[str], Optional[LiquidField]]:
    """
    `view.field` or `view` for a query-state variable

    A single name is a field of the view when it resolves to one, else a
    view (only `_in_query` exists at view level). A name that is both is
    ambiguous and is not translated.
    """
    target, field_reason = context.field(item.ref)
    if '.' in item.ref:
        view_name = None
    else:
        view_name, _ = context.view(item.ref)
        if item.kind != 'in_query':
            view_name = None
    if target is not None and view_name is not None:
        blockers.append(f"{item.label}: `{item.ref}` is both a field of the view and a view name")
        return None, None
    if target is not None:
        if target.timeframe:
            blockers.append(f"{item.label}: `{item.ref}` is one timeframe of dimension group `{target.name}`; Omni "
                            f"tracks the whole group")
            return None, None
        return target.token, target
    if view_name is not None:
        return view_name, None
    blockers.append(f"{item.label}: {field_reason}")
    return None, None

