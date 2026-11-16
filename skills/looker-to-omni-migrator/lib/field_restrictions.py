"""
Field Restrictions: Looker explore and join `fields:` to Omni topic `fields:`

Looker restricts the fields of an explore in two places:

```lkml
explore: order_items {
  fields: [ALL_FIELDS*, -order_items.cost]        # explore level
  join: users {
    fields: [users.id, users.name]                # join level: only these
  }
}
```

An Omni topic has one `fields:` list of selectors, evaluated in a fixed order
by selector type (not list order): `all_views.*`, then `view.*`, then tags,
then `view.field`. A leading `-` excludes. The example becomes:

```yaml
fields: [all_views.*, -users.*, users.id, users.name, -order_items.cost]
```

## Rules

- Explore `[ALL_FIELDS*, -view.a]` -> `all_views.*, -view.a`.
- Explore list of fields only (no `ALL_FIELDS*`) -> `-all_views.*` and each
  field that is left after the join restrictions.
- Join `[view.x, view.y]` -> `-view.*, view.x, view.y`; `[]` -> `-view.*`;
  exclusions only (`[-view.x]`) -> `-view.x`.
- Names are the names the views have in the topic (aliases are kept).
- A set (`view.set*`) is expanded to its member fields, nested sets included.
- A dimension group timeframe (`view.created_date`) maps to the Omni
  dimension (`view.created`). Excluding a group needs every timeframe of its
  `timeframes:` list to be excluded.
- A field the topic will not contain is dropped from the list and noted.

A list is carried whole or not at all. When any entry cannot be resolved
with confidence (a set that is not defined, some timeframes of a group
excluded and others kept, a view that is not known), that list is not
carried and the reason is reported as a manual item.

The difference to Looker: Looker hides the restricted fields, Omni rejects
queries that use them.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from .rule_applier import MANUAL, TimeGroups, match_timeframe

ALL_FIELDS = 'ALL_FIELDS*'
INCLUDE_ALL = 'all_views.*'
EXCLUDE_ALL = '-all_views.*'


@dataclass
class TopicViewFields:
    """What is known about one view of the topic, keyed in the caller by the view's name in the topic"""
    real_view: str
    fields: Set[str]  # Looker dimensions, dimension groups and measures, own and inherited
    time_groups: TimeGroups  # `type: time` dimension groups with their timeframes
    filter_fields: Set[str]  # parameters and filter-only fields
    sets: Dict[str, List[str]]  # `set:` definitions, own and inherited
    missing: Dict[str, str] = field(default_factory=dict)  # field -> why the topic will not contain it


@dataclass
class FieldRestrictions:
    """Result of converting the `fields:` lists of an explore"""
    selectors: List[str]  # topic `fields:` (without the migrator's default tag exclusion)
    carried: List[str] = field(default_factory=list)  # "Carried Into the Topic" lines
    manual: List[Tuple[str, str]] = field(default_factory=list)  # (title, details) of lists not carried
    notes: List[str] = field(default_factory=list)  # entries dropped or mapped, for the report
    verify: List[str] = field(default_factory=list)  # things a human should check on the carried lists

    @property
    def restricted(self) -> bool:
        return self.selectors != [INCLUDE_ALL]


class _NotCarried(Exception):
    """The list cannot be converted with confidence"""


@dataclass
class _Unit:
    """One Looker `fields:` list resolved to Omni field names, per view name in the topic"""
    include: Optional[Dict[str, List[str]]]  # None: the list only excludes
    exclude: Dict[str, List[str]]


def looker_list(entries: List[str]) -> str:
    return '[' + ', '.join(str(entry) for entry in entries) + ']'


def _expand_set(info: TopicViewFields, view: str, set_name: str, path: Tuple[str, ...]) -> List[str]:
    """Member fields of a set (Looker names), nested sets expanded"""
    if set_name in path:
        raise _NotCarried(f"set `{view}.{set_name}*` is circular")
    if set_name not in info.sets:
        raise _NotCarried(f"set `{view}.{set_name}*` is not defined in view `{info.real_view}` or its parents")
    members: List[str] = []
    for member in info.sets[set_name] or []:
        member = str(member).strip()
        if member.startswith('-') or member == ALL_FIELDS:
            raise _NotCarried(f"set `{view}.{set_name}*` uses `{member}`, which is not expanded automatically")
        owner, _, name = member.rpartition('.')
        if owner and owner not in (view, info.real_view):
            raise _NotCarried(f"set `{view}.{set_name}*` holds `{member}`, a field of another view")
        if name.endswith('*'):
            members += _expand_set(info, view, name[:-1], path + (set_name,))
        else:
            members.append(name)
    return members


class _Resolver:
    """Resolves the entries of `fields:` lists against the views of one topic"""

    def __init__(self, views: Dict[str, Optional[TopicViewFields]], skipped: Set[str], result: FieldRestrictions):
        self.views = views
        self.skipped = skipped
        self.result = result

    def note(self, text: str):
        if text not in self.result.notes:
            self.result.notes.append(text)

    def expand(self, entries: List[str], only_view: Optional[str]) -> Tuple[bool, List[Tuple[str, str]], List[Tuple[str, str]]]:
        """
        Split a list into (has ALL_FIELDS*, included, excluded), each a list
        of (view name in the topic, Looker field) with sets expanded.
        `only_view` is the join the list belongs to (None for the explore).
        """
        has_all = False
        included: List[Tuple[str, str]] = []
        excluded: List[Tuple[str, str]] = []
        for raw in entries:
            entry = str(raw).strip()
            negative = entry.startswith('-')
            name = entry[1:].strip() if negative else entry
            if name == ALL_FIELDS:
                if negative:
                    raise _NotCarried(f"`{entry}` is not a form translated automatically")
                has_all = True
                continue
            view, _, field_name = name.rpartition('.')
            if not view:
                if only_view is None:
                    raise _NotCarried(f"`{entry}` is not view-qualified (`view.field`)")
                view = only_view
            if only_view is not None and view != only_view:
                raise _NotCarried(f"`{entry}` names `{view}`, which is not this join (`{only_view}`)")
            if '.' in view or not field_name:
                raise _NotCarried(f"`{entry}` is not a form translated automatically")
            if view not in self.views:
                if view in self.skipped:
                    self.note(f"`{entry}`: dropped, join `{view}` was not migrated, so the topic has no such view")
                    continue
                raise _NotCarried(f"`{entry}` names `{view}`, which is not a view of the explore")
            info = self.views[view]
            if info is None:
                raise _NotCarried(
                    f"`{entry}`: the fields of `{view}` are not known (its view, or a view it extends, is not a "
                    f"single view under --looker-repo)")
            names = _expand_set(info, view, field_name[:-1], ()) if field_name.endswith('*') else [field_name]
            (excluded if negative else included).extend((view, item) for item in names)
        return has_all, list(dict.fromkeys(included)), list(dict.fromkeys(excluded))

    def _kind(self, view: str, name: str) -> Tuple[str, Optional[str], Optional[str]]:
        """('field' | 'filter' | 'timeframe' | 'unknown', dimension group, timeframe)"""
        info = self.views[view]
        if name in info.fields:
            return 'field', None, None
        if name in info.filter_fields:
            return 'filter', None, None
        found = match_timeframe(name, info.time_groups)
        if len(found) > 1:
            raise _NotCarried(
                f"`{view}.{name}` can be a timeframe of several dimension groups "
                f"({', '.join(group for group, _ in found)})")
        if found:
            return 'timeframe', found[0][0], found[0][1]
        return 'unknown', None, None

    def _present(self, view: str, looker_name: str, omni_name: str) -> bool:
        """False (with a note) when the topic will not contain the field"""
        reason = self.views[view].missing.get(omni_name)
        if reason is None:
            return True
        self.note(f"`{view}.{looker_name}`: dropped, the topic will not contain it ({reason})")
        return False

    def _other(self, kind: str, view: str, name: str):
        """Note an entry that is not a dimension or measure"""
        if kind == 'filter':
            item = (f"`{view}.{name}` is a filter-only field. It was left out of the topic `fields:` list, which is "
                    f"documented for dimensions and measures: check how the restriction should apply to it")
            if item not in self.result.verify:
                self.result.verify.append(item)
        else:
            self.note(f"`{view}.{name}`: dropped, view `{self.views[view].real_view}` has no such field")

    def included(self, pairs: List[Tuple[str, str]]) -> Dict[str, List[str]]:
        """Omni fields to expose, per view; a timeframe exposes its whole dimension"""
        out: Dict[str, List[str]] = {}
        for view, name in pairs:
            kind, group, timeframe = self._kind(view, name)
            if kind in ('filter', 'unknown'):
                self._other(kind, view, name)
                continue
            target = group if kind == 'timeframe' else name
            if not self._present(view, name, target):
                continue
            if kind == 'timeframe':
                self.note(f"`{view}.{name}` → `{view}.{target}`: the timeframe maps to the Omni dimension, which "
                          f"offers every timeframe (Looker lists them one by one)")
            if target not in out.setdefault(view, []):
                out[view].append(target)
        return out

    def excluded(self, pairs: List[Tuple[str, str]]) -> Dict[str, List[str]]:
        """Omni fields to exclude, per view; a dimension group only when all its timeframes are excluded"""
        out: Dict[str, List[str]] = {}
        timeframes: Dict[Tuple[str, str], List[str]] = {}
        for view, name in pairs:
            kind, group, timeframe = self._kind(view, name)
            if kind in ('filter', 'unknown'):
                self._other(kind, view, name)
            elif kind == 'timeframe':
                timeframes.setdefault((view, group), []).append(timeframe)
                out.setdefault(view, [])
                if group not in out[view]:
                    out[view].append(group)  # position kept; checked below
            elif self._present(view, name, name) and name not in out.setdefault(view, []):
                out[view].append(name)
        for (view, group), excluded in timeframes.items():
            offered = self.views[view].time_groups.get(group)
            if offered is None or set(offered) - set(excluded):
                kept = ', '.join(sorted(set(offered) - set(excluded))) if offered else "Looker's default timeframes"
                raise _NotCarried(
                    f"it excludes only some timeframes of dimension group `{view}.{group}` "
                    f"({', '.join(excluded)}; kept: {kept}). The Omni dimension `{view}.{group}` holds every "
                    f"timeframe, so it can only be excluded as a whole")
            self.note(f"every timeframe of `{view}.{group}` is excluded → `-{view}.{group}`")
            if not self._present(view, group, group):
                out[view].remove(group)
        return out

    def unit(self, entries: List[str], only_view: Optional[str]) -> _Unit:
        """Resolve one `fields:` list (of the explore, or of join `only_view`)"""
        has_all, included, excluded = self.expand(entries, only_view)
        if has_all or (only_view is not None and not included and excluded):
            if included:
                self.note(f"`{looker_list(entries)}`: the fields listed beside `{ALL_FIELDS}` add nothing")
            return _Unit(include=None, exclude=self.excluded(excluded))
        if only_view is None and not included:
            raise _NotCarried(
                f"the list has no `{ALL_FIELDS}` and no field to include"
                + (", only exclusions" if excluded else "") + ", so what it exposes is not known")
        kept = [pair for pair in included if pair not in set(excluded)]
        return _Unit(include=self.included(kept), exclude={})


def build_topic_fields(
    explore_fields: Optional[List[str]],
    join_fields: Dict[str, Optional[List[str]]],
    views: Dict[str, Optional[TopicViewFields]],
    skipped: Optional[Set[str]] = None
) -> FieldRestrictions:
    """
    Convert the `fields:` lists of a Looker explore to Omni topic selectors

    Args:
        explore_fields: the explore's `fields:` list (None when not set)
        join_fields: {join name: its `fields:` list}; None for no list,
            `[]` for a join that exposes no fields
        views: {view name in the topic: what is known about it}, base view
            first; None for a view whose fields are not known
        skipped: joins of the explore that were not migrated

    Returns:
        The selectors and everything to report. A list that cannot be
        converted with confidence is left out and described in `manual`.
    """
    result = FieldRestrictions(selectors=[INCLUDE_ALL])
    resolver = _Resolver(views, set(skipped or ()), result)

    explore_unit: Optional[_Unit] = None
    if explore_fields is not None:
        try:
            explore_unit = resolver.unit(list(explore_fields), None)
            result.carried.append(f"explore `fields: {looker_list(explore_fields)}` → topic `fields`")
        except _NotCarried as e:
            result.manual.append((
                "Explore field restriction (NOT carried)",
                f"Looker `fields: {looker_list(explore_fields)}` was not written ({MANUAL}): {e}. "
                f"Restrict the topic `fields:` manually."
            ))

    join_units: Dict[str, _Unit] = {}
    for join, entries in join_fields.items():
        if entries is None or join not in views:
            continue
        try:
            join_units[join] = resolver.unit(list(entries), join)
            result.carried.append(f"join `{join}` `fields: {looker_list(entries)}` → topic `fields`")
        except _NotCarried as e:
            result.manual.append((
                f"Join field restriction on {join} (NOT carried)",
                f"Looker `fields: {looker_list(entries)}` was not written ({MANUAL}): {e}. "
                f"Restrict the topic `fields:` manually."
            ))

    include_mode = explore_unit is not None and explore_unit.include is not None
    selectors = [EXCLUDE_ALL if include_mode else INCLUDE_ALL]
    for view in views:
        join_unit = join_units.get(view)
        join_include = None if join_unit is None or join_unit.include is None else join_unit.include.get(view, [])
        join_exclude = [] if join_unit is None else join_unit.exclude.get(view, [])
        if include_mode:
            allowed = [name for name in explore_unit.include.get(view, [])
                       if (join_include is None or name in join_include) and name not in join_exclude]
            selectors += [f"{view}.{name}" for name in allowed]
            continue
        explore_exclude = [] if explore_unit is None else explore_unit.exclude.get(view, [])
        if join_include is not None:
            selectors.append(f"-{view}.*")
            selectors += [f"{view}.{name}" for name in join_include if name not in explore_exclude]
        else:
            selectors += [f"-{view}.{name}" for name in dict.fromkeys(join_exclude + explore_exclude)]
    result.selectors = selectors
    return result
