"""
Orchestrator - Main Migration Workflow Coordinator

This module runs the Looker-to-Omni migration for views and explores.

Guiding rule: never silently change data access or query semantics. A
construct is either converted (and covered by a test) or skipped and listed
as MANUAL ACTION REQUIRED in the report, the console and
`MigrationResult.issues`.

## Architecture

- context_loader: CSV scope filtering
- looker_parser: LookML view parsing (stops on duplicate view definitions,
  reports refinements)
- rule_applier: field conversion rules
- liquid_translator: Liquid in SQL to Omni Mustache
- file_generators: YAML/Markdown output, merge, dry-run, atomic writes
- explore_analyzer: explore parsing and topic suggestions
- access_grant_parser: access grant registry

## View Migration Workflow (`migrate_view`)

1. **Load CSV scope** - rows for this owner, phase and view
2. **Parse the Looker view** - every CSV field must exist in it; missing
   fields are reported as MANUAL ACTION REQUIRED. Every view of the repo is
   parsed once per run (`load_views`). `extends:` is resolved across the
   repo: the view gets `extends:`, references to inherited fields are valid
   when the parent view migrates them, and a field two parents define
   differently is written into the view with the definition Looker uses
   (tag `resolved_from_extends`). A view with `extension: required`, or one
   with no table that is only extended, becomes a template (`template: true`,
   no `schema`, no `table_name`). A parent that cannot be found leaves the
   view unresolved and reported.
3. **Convert fields** - every field of the view is converted. Liquid in a
   field's `sql` (and in a derived table's SQL) is translated to Omni
   Mustache when every construct of the block has an exact equivalent
   (`liquid_translator`); otherwise the field is not written (a derived view
   stops) and the report names the construct. Parameters and filter-only
   fields are written under the view's `filters:` key: visible when the CSV
   lists them (type `filter`) or SQL names them, hidden otherwise. In-scope fields
   are written visible; other fields are written `hidden: true` with the tag
   `in_omni_in_looker_not_csv`. Out-of-scope fields referenced by in-scope
   fields (transitively) are written visible with the tag `auto_dependency`.
   A field whose references cannot be satisfied (an unconvertible or
   undefined dependency, or a circular reference) is not written and is
   reported. References to the `date` and `raw` timeframes of a dimension
   group become `${group[date]}`; other timeframes stop the field. A field
   that references another view through a join alias is left out of the view
   file and listed as topic-scoped (explore mode writes it in the topic).
   Field-level `required_access_grants` are copied onto every written field.
4. **Build the Omni view** - table view (`schema`, `table_name`, and
   `catalog` for 3-part names), SQL derived table (`sql:`), native derived
   table (`query:`) or template view (no source)
5. **Generate files** - everything is rendered and validated first, then
   written: `{view}.view.yaml` or `{view}.query.view` in the output dir and
   `{view}.migration.md` in the reports dir

## Explore Migration Workflow (`migrate_explore`)

1. Find the explore in any `.lkml` file (stops on duplicates)
2. Resolve joins as a dependency graph: each join hangs off the joined view
   its `sql_on` references; joins with unresolved, ambiguous or circular
   references, Liquid, `foreign_key`/`sql` joins or unsupported join or
   relationship types are left out and reported (with everything that
   depends on them)
   When the explore has `extends:`, the parent chain is resolved first
   (parents may live in any `.lkml` file) and the merged explore is migrated:
   the topic gets `extends:` plus the complete merged joins and settings.
   A parent that cannot be found leaves the explore unmerged and reported.
3. Carry `access_filter` -> `access_filters`, `sql_always_where` ->
   `always_where_sql`, `sql_always_having` -> `always_having_sql`,
   `required_access_grants`, `hidden` and `group_label`. `always_filter`
   becomes required `default_filters` entries for the expressions that
   translate with confidence. Access-related and carried items are also
   listed as MANUAL ACTION REQUIRED. Not carried, reported: any other
   `always_filter` entry, `conditionally_filter`, `always_join`, join
   `required_joins`, join `required_access_grants`. Explore and join
   `fields:` restrictions become topic `fields:` selectors.
   An explore with `extension: required` (or `template=True`) gets
   `template: true`.
   Looker aliases are kept and scoped to the topic: an aliased join uses
   `join_to_view_as`, and a base alias (`from:`/`view_name:`) is the topic's
   `base_view`, declared in its `views:` block with `extends: [real_view]`.
   References through an alias are checked against the real view and only
   the unresolved ones are reported. Fields of the topic's views that
   reference a join alias (left out of the view files by view mode) are
   converted with the view rules and written in the `views:` block.
4. Write `{explore}.topic.yaml` (`{prefix}__{explore}` with a topic prefix)
   to the output dir, with the join definitions
   under its `relationships:` key, and `{explore}_explore.md` to the reports
   dir. With `relationships_file=True` the join definitions go to
   `{explore}.relationships` instead (a plain YAML list; removed if it would
   be empty), and conflicts with other relationships files are reported.

## Usage

```python
orchestrator = MigrationOrchestrator(
    skill_path=Path('.'),
    looker_repo=Path('./looker'),
    omni_repo=Path('./migration/staging/views'),
    csv_path=Path('migration/views.csv')
)

result = orchestrator.migrate_view(view_name='users', phase=0, owner='Team A')
result = orchestrator.migrate_explore(explore_name='order_items')
```

With `dry_run=True` nothing is written: planned files are printed and
returned in `MigrationResult.planned_files`.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from .access_grant_parser import load_access_grants
from .config import MigrationConfig
from .context_loader import get_fields_for_view, load_and_filter_csv, load_migration_context
from .explore_analyzer import (
    ExploreInfo,
    JoinDefinition,
    ExtendsResolution,
    find_explores_using_views,
    find_extending_explores,
    find_models_including,
    generate_explore_report,
    load_explores,
    resolve_explore_extends,
    suggest_topic_divisions,
)
from .file_generators import ConversionReportBuilder, FileGenerator, MergeError
from .liquid_translator import (
    FILTER_KINDS,
    FILTER_TYPE_MAP,
    LiquidContext,
    LiquidField,
    LiquidTranslation,
    scan_liquid,
    translate_liquid,
    unquoted_use_form,
)
from .looker_parser import (
    LookerView,
    ViewExtendsResolution,
    ViewSource,
    effective_view,
    load_views,
    own_fields,
    own_filter_fields,
    resolve_view_extends,
    view_from_index,
)
from .field_restrictions import (
    INCLUDE_ALL,
    FieldRestrictions,
    TopicViewFields,
    build_topic_fields,
    looker_list,
)
from .omni_models import (
    OmniDimension,
    OmniFilter,
    OmniMeasure,
    OmniQuery,
    OmniTopic,
    OmniView,
    dimension_to_yaml_dict,
    measure_to_yaml_dict,
)
from .rule_applier import (
    MANUAL,
    ConversionResult,
    RuleApplier,
    extract_measure_filters,
    filter_kind,
    has_liquid,
    normalize_tag,
    phase_tag,
    status_tag,
    time_groups_of,
    match_timeframe,
    translate_date_filter_expression,
    translate_filter_expression,
    TimeGroups,
    CONFIRMED_DATE_PERIODS,
    NAMED_DATE_PERIODS,
)

PROVENANCE_TAG = 'in_omni_in_looker_not_csv'
AUTO_DEPENDENCY_TAG = 'auto_dependency'
# A field two parents of `extends:` define differently, written into the child
# with the definition Looker uses (Omni would take the first parent's)
RESOLVED_FROM_EXTENDS_TAG = 'resolved_from_extends'

# Looker join type -> Omni join_type (https://docs.omni.co/modeling/relationships/parameters/join-type)
JOIN_TYPE_MAP = {
    'left_outer': 'always_left',
    'inner': 'inner',
    'full_outer': 'full_outer',
    'cross': 'cross',
}
# Every topic leaves out the fields tagged as table keys
DEFAULT_FIELD_EXCLUSION = '-tag:table_key'
RELATIONSHIP_TYPES = {'many_to_one', 'one_to_one', 'one_to_many', 'many_to_many'}

# always_filter on a date: dimension types, and the dimension_group timeframes
# that filter on the underlying timestamp (week, month, ... filter a truncated value)
DATE_DIMENSION_TYPES = {'date', 'date_time', 'time'}
DATE_FILTER_TIMEFRAMES = ('date', 'time', 'raw')

_FIELD_REF_RE = re.compile(r'\$\{([a-zA-Z_][a-zA-Z0-9_]*)\.([a-zA-Z_][a-zA-Z0-9_]*)\}')
# The same reference as Omni writes it, with an optional timeframe: ${view.field[date]}
_OMNI_REF_RE = re.compile(r'\$\{([a-zA-Z_][a-zA-Z0-9_]*)\.([a-zA-Z_][a-zA-Z0-9_]*)(?:\[[a-z0-9_]+\])?\}')
_SQL_TABLE_NAME_RE = re.compile(r'\$\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\.SQL_TABLE_NAME\s*\}')
_LOCAL_REF_RE = re.compile(r'\$\{([a-zA-Z_][a-zA-Z0-9_]*)\}')
_IDENT_RE = re.compile(r'[A-Za-z_][A-Za-z0-9_\-]*')


@dataclass
class ExposedField:
    """A field a view offers to the views that extend it, as the migrator would write it"""
    kind: str  # dimension, dimension_group or measure
    looker_type: str  # as in `_field_types`: picks the filter grammar
    origin: str  # view whose migration writes (or fails to write) the field
    reason: Optional[str] = None  # why it is not available in Omni (None: it is)
    primary_key: bool = False


@dataclass
class MigrationResult:
    """Result of migrating a single view or explore"""
    looker_view_name: str
    omni_view_name: str
    view_file_path: Path
    topic_file_path: Path
    report_file_path: Path
    success: bool
    error_message: Optional[str] = None
    # Items that need a human (also written to the report)
    issues: List[str] = field(default_factory=list)
    # Every file written (or, in dry-run, that would be written)
    planned_files: List[Path] = field(default_factory=list)
    relationships_file_path: Optional[Path] = None


class MigrationOrchestrator:
    """Coordinates view and explore migration"""

    def __init__(
        self,
        skill_path: Path,
        looker_repo: Path,
        omni_repo: Path,
        csv_path: Optional[Path] = None,
        omni_source_repo: Optional[Path] = None,
        config: Optional[MigrationConfig] = None,
        reports_dir: Optional[Path] = None,
        dry_run: bool = False
    ):
        self.skill_path = Path(skill_path)
        self.looker_repo = Path(looker_repo)
        self.omni_repo = Path(omni_repo)
        self.csv_path = Path(csv_path) if csv_path else None
        # Existing Omni views to merge/enrich (only used when given)
        self.omni_source_repo = Path(omni_source_repo) if omni_source_repo else None
        # Reports go to the sibling reports/ dir of the output dir by default
        self.reports_dir = Path(reports_dir) if reports_dir else self.omni_repo.parent / 'reports'
        self.dry_run = dry_run

        # Organization-specific settings (schema names, canonical mappings)
        self.config = config or MigrationConfig()
        self.default_schema = self.config.default_schema
        self.curated_folder = self.config.curated_folder

        self.file_generator = FileGenerator(
            output_dir=self.omni_repo,
            reports_dir=self.reports_dir,
            dry_run=dry_run
        )

        # Repo-wide caches: every .lkml file is parsed once per run
        self._view_index: Optional[Tuple[Dict[str, List[LookerView]], Dict[str, List[str]]]] = None
        self._extends_cache: Dict[str, Any] = {}
        self._resolution_cache: Dict[str, Optional[ViewExtendsResolution]] = {}
        self._full_view_cache: Dict[str, Optional[LookerView]] = {}
        self._exposed_cache: Dict[str, Dict[str, ExposedField]] = {}
        self._explore_views_cache: Optional[Set[str]] = None
        self._explore_cache: Optional[Tuple[Dict[str, List[ExploreInfo]], Dict[str, List[str]]]] = None
        self._alias_cache: Optional[Dict[str, Set[str]]] = None
        self._liquid_usage_cache: Optional[Dict[Tuple[str, str], List[Tuple[str, Optional[str]]]]] = None
        self._field_info_cache: Dict[str, Optional[Tuple[Set[str], TimeGroups]]] = {}
        self._topic_scoped_cache: Dict[str, Dict[str, Set[str]]] = {}

        # Raises ValueError on duplicate grant names
        self.access_grants = load_access_grants(str(looker_repo))
        if self.access_grants:
            print(f"✓ Loaded {len(self.access_grants)} access grant definitions")

    # ==================================================================
    # View migration
    # ==================================================================

    def migrate_view(
        self,
        view_name: str,
        phase: int,
        owner: str,
        verified: bool = True
    ) -> MigrationResult:
        """
        Migrate a single Looker view to Omni

        Args:
            view_name: Looker view name (e.g., "users")
            phase: Migration phase number
            owner: Owner filter for the CSV (exact match)
            verified: Tag fields `verified` (True) or `unverified` (False)
        """
        writes_before = len(self.file_generator.planned_writes)
        issues: List[str] = []
        try:
            print(f"\n{'='*60}")
            print(f"MIGRATING: {view_name}")
            print(f"{'='*60}\n")

            # ---------------- Step 1: CSV scope ----------------
            print("[1/5] Loading CSV context...")
            if not self.csv_path:
                raise ValueError("A CSV scope file is required for view migration")
            context = load_migration_context(
                skill_path=str(self.skill_path),
                csv_path=str(self.csv_path),
                owner=owner,
                phase=phase,
                view=view_name,
                allow_empty=True
            )
            csv_fields = get_fields_for_view(context.csv_rows, view_name)
            empty_scope_reason = None
            if not csv_fields:
                empty_scope_reason = self._empty_scope_reason(view_name)
                if empty_scope_reason is None:
                    raise ValueError(f"No CSV rows found for owner={owner}, phase={phase}, view={view_name}")
            print(f"      ✓ CSV fields in scope: {len(csv_fields)}")

            # ---------------- Step 2: parse ----------------
            print("\n[2/5] Parsing Looker view...")
            declared_view = view_from_index(self._load_views()[0], str(self.looker_repo), view_name)
            # Inheritance: from here on looker_view is the view as Looker sees
            # it after `extends:` (own fields with the parameters they inherit,
            # plus the fields two parents define differently)
            resolution = self._view_resolution(view_name)
            inheritance = resolution if resolution is not None and not resolution.problem else None
            inherited = self._inherited_exposure(inheritance) if inheritance else {}
            # Inherited fields whose Mustache tokens must name this view, not the parent
            retargeted = self._retargeted_fields(declared_view, inheritance, inherited) if inheritance else set()
            looker_view = effective_view(declared_view, inheritance, also=retargeted) if inheritance else declared_view
            declared_names = set(own_fields(declared_view)) | set(own_filter_fields(declared_view))
            resolved_fields = {name: f for name, f in inheritance.conflicts.items()
                               if name not in declared_names} if inheritance else {}
            # Parameters and filter-only fields this view writes, and the ones a parent writes
            filter_fields = own_filter_fields(looker_view)
            inherited_filters = {name: f.origin for name, f in inheritance.fields.items()
                                 if f.kind in FILTER_KINDS and name not in filter_fields} if inheritance else {}
            print(f"      ✓ Found: {looker_view.file_path}")
            print(f"      ✓ Dimensions: {len(looker_view.dimensions)}")
            print(f"      ✓ Dimension groups: {len(looker_view.dimension_groups)}")
            print(f"      ✓ Measures: {len(looker_view.measures)}")
            if filter_fields:
                print(f"      ✓ Filter-only fields and parameters: {len(filter_fields)}")
            if inheritance:
                print(f"      ✓ Extends: {', '.join(inheritance.parents)} ({len(inherited)} inherited field(s))")

            omni_view_name = view_name
            report_builder = ConversionReportBuilder(
                looker_view_name=view_name,
                omni_view_name=omni_view_name,
                phase=phase
            )

            kinds = self._field_kinds(looker_view)
            scope = self._resolve_scope(csv_fields, kinds, looker_view, view_name, report_builder, issues, inherited,
                                        filter_fields, inherited_filters)
            in_scope_names: Set[str] = set().union(*scope.values())
            # A field rewritten from a parent keeps the visibility the CSV gives it there
            parent_scope: Dict[str, Set[str]] = {}
            for name in sorted(retargeted):
                origin = inherited[name].origin
                if origin not in parent_scope:
                    parent_scope[origin] = {row.get('field') for row in load_and_filter_csv(
                        str(self.csv_path), owner, phase, origin)}
                if name in parent_scope[origin]:
                    in_scope_names.add(name)

            self._flag_view_features(looker_view, report_builder, issues, resolution)

            source_liquid: List[LiquidTranslation] = []  # Liquid translated in the derived table SQL
            is_template, template_reason = self._view_template_decision(declared_view)
            if is_template:
                # A template has no table: the views that extend it supply one
                schema, view_source = None, {}
                report_builder.add_advanced_feature(
                    feature="Template View",
                    status="CONVERTED",
                    details=f"Written with `template: true`, without `schema` and `table_name`: {template_reason}.",
                    action_required="A template view cannot be queried on its own. Migrate the views that extend it."
                )
                if empty_scope_reason and any(kinds.values()):
                    issues.append(
                        f"No CSV rows for template view {view_name}: every field is written hidden, and the views "
                        f"that extend it inherit the fields hidden. List the fields to expose in the CSV under "
                        f"this view"
                    )
            else:
                # Fail fast on a source that cannot be converted
                schema, view_source = self._determine_view_source(
                    looker_view, report_builder, issues,
                    inherited_source=inheritance.source if inheritance else None,
                    translations=source_liquid
                )

            # ---------------- Step 3: convert ----------------
            print("\n[3/5] Applying conversion rules...")

            is_derived = bool(view_source) and 'table_name' not in view_source
            view_filename = f"{omni_view_name}.{'query.view' if is_derived else 'view.yaml'}"
            existing_view, existing_view_source = self._find_existing_view(omni_view_name, view_filename, is_derived)
            existing_fields = {}
            if existing_view:
                existing_fields.update(existing_view.get('dimensions') or {})
                existing_fields.update(existing_view.get('measures') or {})

            rule_applier, results, blocked = self._convert_view_fields(
                looker_view, inherited, existing_fields, phase, verified
            )

            # Fields that need a join alias live in the topics, not in the view file
            topic_scoped = self._topic_scoped_fields(view_name)
            self._report_topic_scoped(topic_scoped, results, inherited, report_builder)
            for name, (_, result) in results.items():
                needs = sorted(dep for dep in result.dependencies if dep in topic_scoped)
                if needs and name not in topic_scoped and name not in blocked:
                    blocked[name] = (f"depends on {', '.join(needs)}, which is topic-scoped (written by explore "
                                     f"mode into the topics that declare its join alias)")

            # Out-of-scope fields needed by written in-scope fields
            auto_deps: Set[str] = set()
            queue = sorted(n for n in in_scope_names if n in results and n not in blocked
                           and n not in topic_scoped and results[n][1].omni_field)
            seen: Set[str] = set()
            while queue:
                name = queue.pop(0)
                if name in seen:
                    continue
                seen.add(name)
                for dep in sorted(results[name][1].dependencies):
                    if dep in results and dep not in in_scope_names:
                        auto_deps.add(dep)
                    if dep in results:
                        queue.append(dep)

            omni_dimensions: Dict[str, OmniDimension] = {}
            omni_measures: Dict[str, OmniMeasure] = {}
            field_to_column_mapping: Dict[str, str] = {}
            converted_count = hidden_count = dep_count = 0
            cross_view_refs: Set[str] = set()

            for name, (kind, result) in results.items():
                if name in topic_scoped:
                    continue
                in_scope = name in in_scope_names
                if name in blocked and result.omni_field:
                    # Converted on its own, but a reference cannot be satisfied
                    result.omni_field = None
                    result.risk_level = "HIGH"
                    result.warnings.append(f"{MANUAL}: {blocked[name]} - not written")
                if not result.omni_field and name in inherited:
                    # Without its own definition the view shows the inherited one
                    shown = (', '.join(f"`{p}`" for p in resolved_fields[name].conflict[:1])
                             if name in resolved_fields else f"`{inherited[name].origin}`")
                    result.warnings.append(
                        f"{MANUAL}: until it is written by hand, Omni inherits the definition of {shown} for "
                        f"this field, which is not the one Looker uses"
                    )
                if in_scope or name in auto_deps:
                    self._add_result(report_builder, kind, name, result)
                if not result.omni_field:
                    reason = '; '.join(result.warnings)
                    if in_scope or name in inherited:
                        print(f"      ✗ NOT CONVERTED: {name}")
                        issues.append(f"{name}: {reason}")
                        report_builder.add_not_converted(name, reason)
                    else:
                        report_builder.add_not_converted(name, f"Not in CSV scope and not convertible ({reason}) - omitted")
                    continue

                if in_scope:
                    converted_count += 1
                    issues.extend(f"{name}: {w}" for w in result.warnings)
                    print(f"      ✓ {kind}: {name}")
                elif name in auto_deps:
                    result.omni_field.tags = list(result.omni_field.tags) + [AUTO_DEPENDENCY_TAG]
                    dep_count += 1
                    print(f"      ↳ Auto-included dependency: {name} ({kind})")
                    report_builder.add_not_converted(name, "Auto-included as a dependency of in-scope fields")
                else:
                    result.omni_field.hidden = True
                    result.omni_field.tags = [PROVENANCE_TAG]
                    hidden_count += 1
                    report_builder.add_not_converted(name, "Not in CSV scope - added with hidden: true")
                if name in resolved_fields or name in retargeted:
                    result.omni_field.tags = list(result.omni_field.tags) + [RESOLVED_FROM_EXTENDS_TAG]

                target = omni_measures if kind == 'measure' else omni_dimensions
                target[name] = result.omni_field
                if result.db_column_name:
                    field_to_column_mapping[name] = result.db_column_name
                cross_view_refs |= result.cross_view_refs
                if result.liquid:
                    report_builder.add_liquid_translation(f"{kind.replace('_', ' ')} `{name}`", result.liquid)
                if result.timeframes:
                    report_builder.add_timeframe_rewrites(f"{kind.replace('_', ' ')} `{name}`", result.timeframes)

            for translation in source_liquid:
                self._check_source_liquid_fields(translation, results, omni_dimensions, omni_measures, inherited)
                cross_view_refs |= {t.token for t in translation.fields if not t.local}
                report_builder.add_liquid_translation("derived table `sql`", translation)

            omni_filters = self._convert_filter_fields(
                view_name, filter_fields, scope['filter'], rule_applier, report_builder, issues, is_template
            )
            self._report_liquid(report_builder, issues, retargeted, omni_dimensions, omni_measures, inherited)

            if resolved_fields:
                self._report_extends_conflicts(resolved_fields, omni_dimensions, omni_measures, report_builder,
                                               omni_filters)

            cross_view_refs = {ref for ref in cross_view_refs if ref.split('.')[0] != view_name}
            if cross_view_refs:
                views = sorted({ref.split('.')[0] for ref in cross_view_refs})
                unknown = [view for view in views if view not in self._load_views()[0]]
                details = "Fields reference other views (kept as ${view.field}): " + ', '.join(sorted(cross_view_refs))
                if unknown:
                    details += (". Not found under --looker-repo as a view or a join alias: "
                                + ', '.join(f"`{view}`" for view in unknown)
                                + ". Omni model validation fails on a reference to a view that does not exist")
                report_builder.add_advanced_feature(
                    feature="Cross-View References",
                    status="INFO",
                    details=details,
                    action_required=f"Join {', '.join(views)} in every topic that uses these fields."
                )

            # Field-level access grants on every written field
            for prefixed_name, grants in (looker_view.field_access_grants or {}).items():
                name = prefixed_name.split(':', 1)[-1]
                target = omni_dimensions.get(name) or omni_measures.get(name)
                if target is not None:
                    target.required_access_grants = list(grants)

            print("\n      Summary:")
            print(f"      ✓ Converted (CSV scope): {converted_count}")
            if dep_count:
                print(f"      ↳ Auto-included dependencies: {dep_count}")
            print(f"      ⊗ Hidden (not in CSV): {hidden_count}")

            # ---------------- Step 4: build view ----------------
            print("\n[4/5] Building Omni view...")
            view_description = looker_view.description or looker_view.file_comment or \
                f"{omni_view_name.replace('_', ' ').title()} view"
            view_label = self._apply_canonical_to_label(
                looker_view.label or omni_view_name.replace('_', ' ').title()
            )
            view_kwargs: Dict[str, Any] = {
                'folder': f"{schema or self.default_schema}/{self.curated_folder}",
                'label': view_label,
                'description': view_description,
                'tags': list(dict.fromkeys([
                    normalize_tag(omni_view_name.split('_')[0]),
                    'dimension' if view_name.startswith('dim_') else 'fact',
                    phase_tag(phase),
                    status_tag(verified),
                ])),
                'dimensions': omni_dimensions,
                'measures': omni_measures,
            }
            if looker_view.file_comment:
                view_kwargs['ai_context'] = looker_view.file_comment
            view_kwargs.update(view_source)
            if omni_filters:
                view_kwargs['filters'] = omni_filters
            if is_template:
                view_kwargs['template'] = True
            else:
                view_kwargs['schema'] = schema
            if inheritance:
                view_kwargs['extends'] = list(inheritance.parents)

            view = OmniView(**view_kwargs)
            print(f"      ✓ Schema: {schema or '(none, template view)'}")
            print(f"      ✓ Dimensions: {len(omni_dimensions)}  Measures: {len(omni_measures)}"
                  f"  Filter-only fields: {len(omni_filters)}")

            # ---------------- Step 5: render, validate, write ----------------
            print("\n[5/5] Generating files...")
            if is_template:
                report_builder.set_view_file_decision(
                    'Template view (.view.yaml, template: true)', str(self.omni_repo / view_filename),
                    f"Template view: {template_reason}"
                )
            elif is_derived:
                report_builder.set_view_file_decision(
                    'Query view (.query.view)', str(self.omni_repo / view_filename),
                    'Looker derived table converted to an Omni query view'
                )
            else:
                report_builder.set_view_file_decision(
                    'Standard table-based view (.view.yaml)', str(self.omni_repo / view_filename),
                    'Lift-and-shift migration of the in-scope fields'
                )

            if existing_view:
                print(f"      ℹ️  Merging into existing view: {existing_view_source}")
            field_grants = {prefixed.split(':', 1)[-1]: list(grants)
                            for prefixed, grants in (looker_view.field_access_grants or {}).items()}
            unconverted = {name: '; '.join(res.warnings) or 'not converted'
                           for name, (_, res) in results.items() if not res.omni_field}
            unconverted.update({name: self._topic_scoped_reason(aliases)
                                for name, aliases in topic_scoped.items() if name in results})
            grant_columns = {}
            for name in field_grants:
                data = looker_view.dimensions.get(name) or looker_view.dimension_groups.get(name)
                if data is not None:
                    column = rule_applier.extract_db_column_name(data.get('sql') or '${TABLE}.' + name, name)
                    if column:
                        grant_columns[name] = column
            view_content, render_info = self.file_generator.render_view_yaml(
                view,
                existing_view=existing_view,
                csv_fields=set(in_scope_names) if existing_view is not None else None,
                field_to_column_mapping=field_to_column_mapping,
                view_name=view_name,
                looker_fields=set(results),
                field_grants=field_grants,
                unconverted=unconverted,
                grant_columns=grant_columns
            )
            for item in render_info.get('manual', []):
                issues.append(f"Merge: {MANUAL}: {item}")
                report_builder.add_advanced_feature(feature="Merge", status=MANUAL, details=item)
            self._report_field_grants(field_grants, render_info['final'], report_builder, issues,
                                      render_info.get('renames'))
            if existing_view:
                report_builder.set_merge_info(existing_view_source, render_info['stats'], render_info['notes'])
                for note in render_info['notes']:
                    if note.startswith('Replaced') or note.startswith('Removed'):
                        issues.append(f"Merge: {note}")

            final_dims = render_info['final'].get('dimensions') or {}
            final_names = set(final_dims) | set(render_info['final'].get('measures') or {})
            # A key a parent defines (and that converts) reaches this view through `extends:`
            inherited_key = any(exposed.primary_key and exposed.reason is None and name not in final_names
                                for name, exposed in inherited.items())
            if not inherited_key and not any(
                    isinstance(d, dict) and d.get('primary_key') is True for d in final_dims.values()):
                print("      ⚠️  WARNING: No primary key in the final view!")
                issues.append("No primary key dimension in the final view")
                report_builder.add_advanced_feature(
                    feature="Missing Primary Key",
                    status="HIGH RISK",
                    details="The final view has no dimension with primary_key: true.",
                    action_required="Add the primary key field to the CSV and re-migrate, or set primary_key: true manually."
                )

            report_content = report_builder.build_report().to_markdown()
            view_path = self.file_generator.write_file(self.omni_repo / view_filename, view_content)
            print(f"      ✓ View: {view_path}")
            report_path = self.file_generator.generate_text_report(report_content, f"{omni_view_name}.migration.md")
            print(f"      ✓ Report: {report_path}")

            self._print_issues(view_name, issues)
            return MigrationResult(
                looker_view_name=view_name,
                omni_view_name=omni_view_name,
                view_file_path=view_path,
                topic_file_path=Path(''),
                report_file_path=report_path,
                success=True,
                issues=issues,
                planned_files=self.file_generator.planned_writes[writes_before:]
            )

        except Exception as e:
            return self._failure(view_name, e, issues, writes_before, "MIGRATION FAILED")

    def _failure(self, name: str, error: Exception, issues: List[str], writes_before: int, title: str) -> MigrationResult:
        print(f"\n{'='*60}")
        print(f"❌ {title}: {name}")
        print(f"{'='*60}")
        print(f"Error: {error}")
        return MigrationResult(
            looker_view_name=name,
            omni_view_name='',
            view_file_path=Path(''),
            topic_file_path=Path(''),
            report_file_path=Path(''),
            success=False,
            error_message=str(error),
            issues=issues,
            planned_files=self.file_generator.planned_writes[writes_before:]
        )

    def _report_topic_scoped(
        self,
        topic_scoped: Dict[str, Set[str]],
        results: Dict[str, Tuple[str, ConversionResult]],
        inherited: Dict[str, ExposedField],
        report_builder: ConversionReportBuilder
    ):
        """List the fields left out of the view file because they need a join alias"""
        for name, aliases in sorted(topic_scoped.items()):
            needs = ', '.join(f"`{alias}`" for alias in sorted(aliases))
            if name in results:
                report_builder.add_topic_scoped(name, results[name][0], needs)
                print(f"      ↷ Topic-scoped (not in the view file): {name}")
            elif name in inherited:
                report_builder.add_topic_scoped(name, inherited[name].kind, needs, inherited[name].origin)

    def _resolve_scope(self, csv_fields, kinds, looker_view, view_name, report_builder, issues,
                       inherited: Optional[Dict[str, ExposedField]] = None,
                       filter_fields: Optional[Dict[str, Tuple[str, Dict]]] = None,
                       inherited_filters: Optional[Dict[str, str]] = None) -> Dict[str, Set[str]]:
        """
        Map CSV rows to Looker fields; report rows that do not match

        A row naming a field the view inherits through `extends:` is not
        written here (the parent view carries it) and is reported apart.
        Rows naming a parameter or a filter-only field (`filter_fields`, CSV
        type `filter`) land under the `filter` key of the result.
        """
        scope: Dict[str, Set[str]] = {kind: set() for kind in kinds}
        scope['filter'] = set()
        filter_fields = filter_fields or {}
        inherited_filters = inherited_filters or {}
        missing_rows = []
        inherited_rows: List[str] = []
        for row in csv_fields:
            name, csv_type = row.get('field'), row.get('field_type')
            actual = next((k for k, fields in kinds.items() if name in fields), None)
            if actual is None and name in filter_fields:
                if csv_type not in ('filter', 'parameter'):
                    issues.append(f"CSV field '{name}' is listed as {csv_type} but is a {filter_fields[name][0]} in "
                                  f"LookML; migrated as a filter-only field (CSV type `filter`)")
                scope['filter'].add(name)
                continue
            if actual is None and name in inherited_filters:
                inherited_rows.append(f"{name} (from `{inherited_filters[name]}`)")
                continue
            if actual is None and inherited and name in inherited:
                exposed = inherited[name]
                inherited_rows.append(f"{name} (from `{exposed.origin}`)")
                if exposed.reason:
                    issues.append(
                        f"CSV field '{name}' is inherited from view {exposed.origin}, where it is not migrated "
                        f"({exposed.reason})"
                    )
                    report_builder.add_not_converted(
                        name, f"{MANUAL}: inherited from `{exposed.origin}`, where it is not migrated ({exposed.reason})"
                    )
                continue
            if actual is None:
                missing_rows.append(f"{name} ({csv_type})")
                issues.append(f"CSV field '{name}' ({csv_type}) not found in Looker view {view_name}")
                report_builder.add_not_converted(name, f"{MANUAL}: listed in the CSV but not defined in the Looker view")
                continue
            if csv_type not in kinds:
                issues.append(f"CSV field '{name}' has unsupported type '{csv_type}'; migrated as {actual}")
            elif csv_type != actual:
                issues.append(f"CSV field '{name}' is listed as {csv_type} but is a {actual} in LookML; migrated as {actual}")
            scope[actual].add(name)

        if missing_rows:
            print(f"      ⚠️  {len(missing_rows)} CSV field(s) not found in LookML: {', '.join(missing_rows)}")
            report_builder.add_advanced_feature(
                feature="CSV Fields Not Found in LookML",
                status=MANUAL,
                details="These CSV rows have no matching field in the Looker view: " + ', '.join(missing_rows),
                action_required="Fix the field name/type in the CSV (dimension_groups use the group name, e.g. `created`) or remove the row."
            )
        if inherited_rows:
            issues.append(
                f"CSV fields inherited through extends are not written in {view_name}: {', '.join(inherited_rows)}. "
                f"Whether they are visible follows the parent view's own CSV scope"
            )
            report_builder.add_advanced_feature(
                feature="CSV Fields Inherited Through extends",
                status=MANUAL,
                details="These CSV rows name fields the view inherits; they are defined in the parent view, not here: "
                        + ', '.join(inherited_rows),
                action_required="List each field in the CSV under the parent view that defines it. A field the parent "
                                "writes hidden (out of its scope) is hidden in this view too."
            )
        return scope

    # ------------------------------------------------------------------
    # View inheritance (extends) and template views
    # ------------------------------------------------------------------

    def _load_views(self) -> Tuple[Dict[str, List[LookerView]], Dict[str, List[str]]]:
        """Every view under --looker-repo (parsed once per run), see `load_views`"""
        if self._view_index is None:
            self._view_index = load_views(str(self.looker_repo))
        return self._view_index

    def _view_resolution(self, view_name: str) -> Optional[ViewExtendsResolution]:
        """The view's resolved `extends:` chain; None when it extends nothing or is not found"""
        if view_name not in self._resolution_cache:
            view = self._cached_view(view_name)
            self._resolution_cache[view_name] = (
                resolve_view_extends(view, self._load_views()[0], self._extends_cache)
                if view is not None and view.extends else None
            )
        return self._resolution_cache[view_name]

    def _full_view(self, view_name: str) -> Optional[LookerView]:
        """The view with every field it has in Looker: its own and the inherited ones"""
        if view_name not in self._full_view_cache:
            view = self._cached_view(view_name)
            resolution = self._view_resolution(view_name)
            if view is not None and resolution is not None and not resolution.problem:
                view = effective_view(view, resolution, include_inherited=True)
            self._full_view_cache[view_name] = view
        return self._full_view_cache[view_name]

    @staticmethod
    def _field_kinds(looker_view: LookerView) -> Dict[str, Dict[str, Dict]]:
        return {
            'dimension': looker_view.dimensions,
            'dimension_group': looker_view.dimension_groups,
            'measure': looker_view.measures,
        }

    def _convert_view_fields(
        self,
        looker_view: LookerView,
        inherited: Dict[str, ExposedField],
        existing_fields: Dict,
        phase: int,
        verified: bool,
        view_resolver: Optional[Any] = None
    ) -> Tuple[RuleApplier, Dict[str, Tuple[str, ConversionResult]], Dict[str, str]]:
        """
        Convert every field of the view

        `inherited` holds the fields the view receives through `extends:`.
        They are not converted here (the parent view carries them), but a
        reference to one follows the same rules as a reference to a field of
        the view itself.

        `view_resolver` maps a name used in `${name.field}` to the real view
        it stands for (None when unknown). Explore mode passes the topic's
        own names; the default is the repo-wide alias map, where a name must
        stand for a single view.

        Returns:
            (rule applier, {field: (kind, result)}, {field: reason it cannot be written})
        """
        kinds = self._field_kinds(looker_view)
        field_types = {name: exposed.looker_type for name, exposed in inherited.items()}
        field_types.update(self._field_types(looker_view))
        current = looker_view.view_name
        if view_resolver is None:
            def view_resolver(name: str) -> Optional[str]:
                return self._resolve_liquid_view(name, current)[0]
        # Time groups of the whole chain: a timeframe of an inherited group is a timeframe reference too
        time_groups = dict((self._view_field_info(current) or (set(), {}))[1])
        time_groups.update(time_groups_of(looker_view.dimension_groups))
        rule_applier = RuleApplier(
            phase=phase,
            verified=verified,
            view_name=looker_view.view_name,
            canonical_mappings=self.config.canonical_mappings,
            column_fields=self._column_fields(looker_view, existing_fields),
            field_types=field_types,
            type_resolver=lambda view, name: self._resolve_cross_view_type(view_resolver(view) or view, name),
            liquid_context=self._liquid_context(looker_view.view_name),
            time_groups=time_groups,
            view_fields=lambda view: self._view_field_info(view_resolver(view))
        )
        inherited_measures = {name for name, exposed in inherited.items() if exposed.kind == 'measure'}
        all_dimension_names = (set(looker_view.dimensions) | set(looker_view.dimension_groups)
                               | (set(inherited) - inherited_measures))
        all_measure_names = set(looker_view.measures) | inherited_measures

        def convert(kind: str, name: str) -> ConversionResult:
            data = kinds[kind][name]
            if kind == 'dimension':
                return rule_applier.convert_dimension(name, data)
            if kind == 'dimension_group':
                return rule_applier.convert_dimension_group(name, data)
            return rule_applier.convert_measure(
                name, data, all_dimensions=all_dimension_names, all_measures=all_measure_names
            )

        results: Dict[str, Tuple[str, ConversionResult]] = {}
        for kind, fields in kinds.items():
            for name in fields:
                results[name] = (kind, convert(kind, name))
        return rule_applier, results, self._blocked_fields(results, set(existing_fields), inherited)

    def _inherited_exposure(self, resolution: ViewExtendsResolution) -> Dict[str, ExposedField]:
        """
        The fields a view receives through a resolved `extends:` chain, as
        Omni sees them: for a name several parents supply, the first parent
        in the list wins.
        """
        inherited: Dict[str, ExposedField] = {}
        for parent in resolution.parents:
            for name, exposed in self._exposed_fields(parent).items():
                inherited.setdefault(name, exposed)
        return inherited

    def _exposed_fields(self, view_name: str) -> Dict[str, ExposedField]:
        """
        Every field the Omni view of `view_name` offers to the views that
        extend it: what its own migration writes, plus what it inherits.

        Each field is judged in the view that defines it, with the rules its
        own migration applies, so a field that view cannot migrate is known
        to be missing (with the reason) wherever it is referenced. Computed
        once per view and run; a resolved chain has no cycle.
        """
        if view_name in self._exposed_cache:
            return self._exposed_cache[view_name]
        view = self._cached_view(view_name)
        if view is None:
            self._exposed_cache[view_name] = {}
            return {}
        resolution = self._view_resolution(view_name)
        inheritance = resolution if resolution is not None and not resolution.problem else None
        inherited = self._inherited_exposure(inheritance) if inheritance else {}
        retargeted = self._retargeted_fields(view, inheritance, inherited) if inheritance else set()
        effective = effective_view(view, inheritance, also=retargeted) if inheritance else view
        _, results, blocked = self._convert_view_fields(effective, inherited, {}, phase=0, verified=True)

        types = self._field_types(effective)
        exposed = dict(inherited)
        topic_scoped = self._topic_scoped_fields(view_name)
        for name, (kind, result) in results.items():
            reason = None
            if name in topic_scoped:
                reason = self._topic_scoped_reason(topic_scoped[name])
            elif name in blocked:
                reason = blocked[name]
            elif not result.omni_field:
                reason = '; '.join(result.warnings) or 'not convertible'
            exposed[name] = ExposedField(
                kind=kind,
                looker_type=types.get(name, 'string'),
                origin=view_name,
                reason=reason,
                primary_key=reason is None and getattr(result.omni_field, 'primary_key', None) is True
            )
        self._exposed_cache[view_name] = exposed
        return exposed

    @staticmethod
    def _topic_scoped_reason(aliases: Set[str]) -> str:
        return (f"topic-scoped: it needs the join alias {', '.join(f'`{a}`' for a in sorted(aliases))}, so explore "
                f"mode writes it into the topics that declare the alias, not into the view file")

    def _views_used_by_explores(self) -> Set[str]:
        """Every view an explore under --looker-repo uses as its base or as a join target"""
        if self._explore_views_cache is None:
            definitions, _ = self._load_explores()
            used: Set[str] = set()
            for explores in definitions.values():
                for explore in explores:
                    merged = explore
                    if explore.extends:
                        resolved = resolve_explore_extends(explore, definitions)
                        if not resolved.problem:
                            merged = resolved.explore
                    used.add(merged.base_view)
                    used.update(join.source_view for join in merged.joins)
            self._explore_views_cache = used
        return self._explore_views_cache

    def _extending_views(self, view_name: str) -> List[str]:
        """Names of the views that list `view_name` in their `extends:`"""
        return sorted({
            view.view_name
            for views in self._load_views()[0].values()
            for view in views
            if view_name in view.extends
        })

    def _view_template_decision(self, looker_view: LookerView) -> Tuple[bool, Optional[str]]:
        """
        Whether the view is a template (`template: true`), and why

        A view with `extension: required` is one. So is a view that has no
        `sql_table_name` and no `derived_table`, is extended by another view
        and is never the base or a join target of an explore.
        """
        extenders = self._extending_views(looker_view.view_name)
        extended_by = ', '.join(f"`{name}`" for name in extenders)
        if looker_view.is_extension:
            return True, ("the view has `extension: required` and "
                          + (f"is extended by {extended_by}" if extenders
                             else "no view under --looker-repo extends it"))
        if looker_view.sql_table_name or looker_view.derived_table or not extenders:
            return False, None
        if looker_view.view_name in self._views_used_by_explores():
            return False, None
        return True, (f"the view has no sql_table_name and no derived_table, is extended by {extended_by} and is "
                      f"never used directly as an explore base or join target under --looker-repo")

    def _empty_scope_reason(self, view_name: str) -> Optional[str]:
        """
        Why a view may be migrated with no CSV rows: it has no fields of its
        own (it only extends other views), or it is a template. None when
        the missing rows are an error.
        """
        view = self._cached_view(view_name)
        if view is None:
            return None
        if not own_fields(view):
            return "the view has no fields of its own"
        if self._view_template_decision(view)[0]:
            return "the view is a template"
        return None

    # ------------------------------------------------------------------
    # Liquid, parameters and filter-only fields
    # ------------------------------------------------------------------

    def _liquid_context(self, view_name: str) -> LiquidContext:
        """Resolver of the Liquid references found in the SQL of `view_name`"""
        return LiquidContext(
            field=lambda ref: self._resolve_liquid_ref(view_name, ref),
            view=lambda name: self._resolve_liquid_view(name, view_name)
        )

    def _alias_targets(self) -> Dict[str, Set[str]]:
        """
        {name: views it stands for} across every explore under --looker-repo

        Every name an explore uses for a view that is not the view's own
        name: the base alias (`from:` and/or `view_name:` make the name the
        explore's fields go by differ from the real view) and each join
        alias (`join: a { from: v }`). This one map serves Liquid, filters
        and cross-view fields. A name that stands for several views is
        ambiguous outside the explore that declares it.
        """
        if self._alias_cache is None:
            definitions, _ = self._load_explores()
            aliases: Dict[str, Set[str]] = {}
            for explores in definitions.values():
                for explore in explores:
                    merged = explore
                    if explore.extends:
                        resolved = resolve_explore_extends(explore, definitions)
                        if not resolved.problem:
                            merged = resolved.explore
                    for name, real in merged.view_names().items():
                        if name != real:
                            aliases.setdefault(name, set()).add(real)
            self._alias_cache = aliases
        return self._alias_cache

    def _is_join_alias(self, name: str) -> bool:
        """True when `name` is an alias an explore declares and not a view under --looker-repo"""
        return name not in self._load_views()[0] and name in self._alias_targets()

    def _view_field_info(self, view_name: Optional[str]) -> Optional[Tuple[Set[str], TimeGroups]]:
        """
        (field names, `type: time` dimension groups with their timeframes) of
        a view, own and inherited; None when the view is not a single view
        under --looker-repo. Computed once per view and run.
        """
        if not view_name:
            return None
        if view_name not in self._field_info_cache:
            full = self._full_view(view_name)
            self._field_info_cache[view_name] = None if full is None else (
                set(full.dimensions) | set(full.dimension_groups) | set(full.measures),
                time_groups_of(full.dimension_groups)
            )
        return self._field_info_cache[view_name]

    def _field_problem(self, view_name: str, field_name: str) -> Optional[str]:
        """
        Why `field_name` is not a field of `view_name` (own, inherited, or a
        timeframe of a dimension group); None when it is one
        """
        info = self._view_field_info(view_name)
        if info is None:
            return f"view `{view_name}` is not a single view under --looker-repo"
        names, groups = info
        if field_name in names or match_timeframe(field_name, groups):
            return None
        return f"view `{view_name}` has no field `{field_name}`"

    def _topic_scoped_fields(self, view_name: str) -> Dict[str, Set[str]]:
        """
        {field: join aliases it needs} for the fields of a view (own and
        inherited) that cannot live in the view file

        A field whose sql or filters name another view through a join alias
        (a name that is not a view under --looker-repo) fails Omni model
        validation in a view file, so it belongs to the topics that declare
        the alias. A field built on such a field needs the same aliases.
        Read from the LookML alone, so view mode and explore mode agree.
        Computed once per view and run.
        """
        if view_name in self._topic_scoped_cache:
            return self._topic_scoped_cache[view_name]
        scoped: Dict[str, Set[str]] = {}
        full = self._full_view(view_name)
        local: Dict[str, Set[str]] = {}
        for fields in self._field_kinds(full).values() if full is not None else ():
            for name, data in fields.items():
                sql = data.get('sql') if isinstance(data.get('sql'), str) else ''
                filter_keys = [str(key) for key, _ in extract_measure_filters(data)]
                views = {view for view, _ in _FIELD_REF_RE.findall(sql)}
                views |= {key.split('.')[0] for key in filter_keys if '.' in key}
                aliases = {view for view in views if view != view_name and self._is_join_alias(view)}
                if aliases:
                    scoped[name] = aliases
                local[name] = set(_LOCAL_REF_RE.findall(sql)) | {key for key in filter_keys if '.' not in key}
                if data.get('list_field'):
                    local[name].add(str(data['list_field']))
        changed = bool(scoped)
        while changed:
            changed = False
            for name, deps in local.items():
                needed = set().union(*(scoped[dep] for dep in deps if dep in scoped and dep != name))
                if needed - scoped.get(name, set()):
                    scoped[name] = scoped.get(name, set()) | needed
                    changed = True
        self._topic_scoped_cache[view_name] = scoped
        return scoped

    def _resolve_liquid_view(self, name: str, current_view: str) -> Tuple[Optional[str], Optional[str]]:
        """
        The view a name used in Liquid points to, as (Omni view name, None)
        or (None, reason)

        The name is the view being converted, a view defined under
        --looker-repo, or an alias that stands for exactly one view across
        all explores. Anything else is not resolved.
        """
        if name == current_view:
            return current_view, None
        if len(self._load_views()[0].get(name, [])) == 1:
            return name, None
        targets = sorted(self._alias_targets().get(name, ()))
        if len(targets) == 1:
            if len(self._load_views()[0].get(targets[0], [])) == 1:
                return targets[0], None
            return None, f"`{name}` is an alias of `{targets[0]}`, which is not a single view under --looker-repo"
        if targets:
            return None, (f"`{name}` is an alias of several views ({', '.join(targets)}), so the view it "
                          f"names here is not known")
        return None, f"`{name}` is not a view under --looker-repo nor a join alias of an explore"

    def _resolve_liquid_ref(self, current_view: str, ref: str,
                            typed: bool = True) -> Tuple[Optional[LiquidField], Optional[str]]:
        """
        The field a Liquid reference (`field` or `view.field`) names in the
        SQL of `current_view`, as the tool writes it in Omni

        An unqualified name is a field the view defines or inherits; the
        token then names the view itself. A dimension group timeframe
        (`created_date`) names the group's Omni dimension (`created`).
        With `typed=False` the Omni type of a filter-only field is not
        worked out (used while collecting how parameters are used).
        """
        view_ref, _, field_name = ref.rpartition('.')
        target_view = current_view
        if view_ref:
            target_view, reason = self._resolve_liquid_view(view_ref, current_view)
            if target_view is None:
                return None, reason
        full = self._full_view(target_view)
        if full is None:
            return None, f"view `{target_view}` was not found under --looker-repo"
        local = target_view == current_view

        for kind, fields in (('parameter', full.parameters), ('filter', full.filters)):
            if field_name not in fields:
                continue
            data = fields[field_name]
            origin = self._filter_origin(target_view, field_name)
            target = LiquidField(view=target_view, name=field_name, kind=kind,
                                 looker_type=data.get('type') or 'string', origin=origin, local=local)
            if kind == 'filter' and data.get('sql'):
                return None, f"filter-only field `{ref}` is not migrated (it has `sql:`)"
            if target.looker_type != 'unquoted' and target.looker_type not in FILTER_TYPE_MAP:
                return None, f"{kind} `{ref}` is not migrated (type `{target.looker_type}`)"
            if typed:
                target.omni_type, target.problem = self._filter_omni_type(origin, field_name, target.looker_type)
            return target, None

        if field_name in full.dimensions:
            return LiquidField(view=target_view, name=field_name, kind='dimension', origin=target_view, local=local,
                               looker_type=full.dimensions[field_name].get('type', 'string')), None
        if field_name in full.measures:
            return LiquidField(view=target_view, name=field_name, kind='measure', origin=target_view, local=local,
                               looker_type='measure'), None
        for group, data in full.dimension_groups.items():
            if not field_name.startswith(f"{group}_") or data.get('type', 'time') != 'time':
                continue
            timeframe = field_name[len(group) + 1:]
            if data.get('timeframes') and timeframe not in data['timeframes']:
                continue
            if timeframe not in DATE_FILTER_TIMEFRAMES:
                return None, (f"`{ref}` is the `{timeframe}` timeframe of dimension group `{group}`; only the "
                              f"{', '.join(DATE_FILTER_TIMEFRAMES)} timeframes map to the Omni dimension")
            return LiquidField(view=target_view, name=group, kind='dimension_group', origin=target_view, local=local,
                               looker_type='time', timeframe=timeframe), None
        return None, f"`{ref}` is not a field of view `{target_view}`"

    def _filter_origin(self, view_name: str, field_name: str) -> str:
        """The view whose migration writes a parameter or filter-only field `view_name` has"""
        declared = self._cached_view(view_name)
        resolution = self._view_resolution(view_name)
        if declared is None or field_name in own_filter_fields(declared):
            return view_name
        if resolution is None or resolution.problem or field_name not in resolution.fields:
            return view_name
        inherited = resolution.fields[field_name]
        # Parents that disagree: the view writes the definition Looker uses itself
        return view_name if inherited.conflict else inherited.origin

    def _filter_omni_type(self, origin: str, field_name: str, looker_type: str) -> Tuple[str, Optional[str]]:
        """
        Omni `type` of a parameter or filter-only field, and why its value
        cannot be injected (None when it can)

        A `type: unquoted` parameter becomes `column` when the SQL of the
        repository uses it as a whole identifier, and `string` when it is
        used wrapped in single quotes (or not used in SQL at all). Used both
        ways, no single type fits.
        """
        if looker_type != 'unquoted':
            return FILTER_TYPE_MAP[looker_type], None
        forms = {form for _, form in self._liquid_usage().get((origin, field_name), [])}
        if {'identifier', 'literal'} <= forms:
            return 'string', ("the `type: unquoted` parameter is used both as an identifier and as a quoted "
                              "string, and an Omni filter-only field has one type")
        return ('column' if 'identifier' in forms else 'string'), None

    @staticmethod
    def _sql_blocks(view: LookerView) -> List[str]:
        """Every SQL block of a view that Liquid is translated in"""
        blocks = [(view.derived_table or {}).get('sql')]
        for fields in (view.dimensions, view.dimension_groups, view.measures):
            blocks.extend(data.get('sql') for data in fields.values())
        return [sql for sql in blocks if isinstance(sql, str)]

    def _liquid_usage(self) -> Dict[Tuple[str, str], List[Tuple[str, Optional[str]]]]:
        """
        Where the SQL of the repository names each parameter and filter-only
        field in a Liquid tag that can be translated:
        {(view that writes the field, field): [(view using it, form)]}

        `form` says how a `type: unquoted` parameter is used at that place:
        'identifier', 'literal' or None (see `unquoted_use_form`). Every view
        is read with the fields it inherits. Computed once per run.
        """
        if self._liquid_usage_cache is None:
            usage: Dict[Tuple[str, str], List[Tuple[str, Optional[str]]]] = {}
            for name in sorted(self._load_views()[0]):
                view = self._full_view(name)
                if view is None:
                    continue
                for sql in self._sql_blocks(view):
                    if not has_liquid(sql):
                        continue
                    for item in scan_liquid(sql)[0]:
                        if item.kind in ('other', 'endcondition', 'user_attribute') or not item.ref:
                            continue
                        if item.context == 'comment':
                            continue  # the comment is removed from the SQL, or blocks it
                        target, _ = self._resolve_liquid_ref(name, item.ref, typed=False)
                        if target is None or target.kind not in FILTER_KINDS:
                            continue
                        form = None
                        if target.looker_type == 'unquoted' and item.kind in ('parameter', 'parameter_value'):
                            form, _ = unquoted_use_form(sql, item)
                        usage.setdefault((target.origin, target.name), []).append((name, form))
            self._liquid_usage_cache = usage
        return self._liquid_usage_cache

    def _retargeted_fields(
        self,
        declared_view: LookerView,
        resolution: ViewExtendsResolution,
        inherited: Dict[str, ExposedField]
    ) -> Set[str]:
        """
        Inherited fields the view has to write itself because their sql
        holds Liquid that names "this view" (an unqualified reference)

        A Mustache token names one view. The parent's field names the
        parent, so the copy a child inherits would read the parent's filter,
        not the child's. Writing the field into the child, converted there,
        keeps the Looker meaning.
        """
        own = own_fields(declared_view)
        names: Set[str] = set()
        for name, field_data in resolution.fields.items():
            if name in own or field_data.conflict or field_data.kind in FILTER_KINDS:
                continue
            exposed = inherited.get(name)
            if exposed is None or exposed.reason is not None:
                continue  # the parent does not write it: nothing is inherited
            sql = field_data.data.get('sql')
            if not isinstance(sql, str) or not has_liquid(sql):
                continue
            if any(item.ref and item.kind != 'user_attribute' and '.' not in item.ref
                   for item in scan_liquid(sql)[0]):
                names.add(name)
        return names

    @staticmethod
    def _check_source_liquid_fields(
        translation: LiquidTranslation,
        results: Dict[str, Tuple[str, ConversionResult]],
        omni_dimensions: Dict[str, OmniDimension],
        omni_measures: Dict[str, OmniMeasure],
        inherited: Dict[str, ExposedField]
    ):
        """
        A derived table whose SQL names a dimension or measure of its own
        view in a Mustache token needs that field in the Omni view

        Raises:
            ValueError: a field it names is not written
        """
        for target in translation.fields:
            if target.kind in FILTER_KINDS or not target.local:
                continue
            name = target.name
            if name in omni_dimensions or name in omni_measures:
                continue
            exposed = inherited.get(name)
            if name not in results and exposed is not None and exposed.reason is None:
                continue
            raise ValueError(
                f"{MANUAL}: the derived table SQL names field `{name}` in a Liquid tag, and that field is not "
                f"migrated, so the Mustache token would point at nothing. Migrate this view manually"
            )

    def _convert_filter_fields(
        self,
        view_name: str,
        filter_fields: Dict[str, Tuple[str, Dict]],
        in_scope: Set[str],
        rule_applier: RuleApplier,
        report_builder: ConversionReportBuilder,
        issues: List[str],
        is_template: bool = False
    ) -> Dict[str, OmniFilter]:
        """
        Convert the view's parameters and filter-only fields to Omni
        filter-only fields (the view's `filters:` key)

        Every one is written. It is visible when the CSV lists it (type
        `filter`) or a Liquid tag in the repository's SQL names it, and
        `hidden: true` otherwise.
        """
        omni_filters: Dict[str, OmniFilter] = {}
        lines: List[str] = []
        for name, (kind, data) in filter_fields.items():
            looker_type = data.get('type') or 'string'
            omni_type, problem = (self._filter_omni_type(view_name, name, looker_type)
                                  if looker_type == 'unquoted' else (None, None))
            result = rule_applier.convert_filter_field(name, data, kind, omni_type)
            users = sorted({user for user, _ in self._liquid_usage().get((view_name, name), [])})
            if not result.omni_field:
                reason = '; '.join(result.warnings)
                print(f"      ✗ NOT CONVERTED: {name} ({kind})")
                issues.append(f"{name}: {reason}")
                report_builder.add_not_converted(name, reason)
                continue
            if name in in_scope:
                visibility = "visible (in the CSV scope)"
            elif users:
                visibility = f"visible (named in the SQL of {', '.join(f'`{user}`' for user in users)})"
            else:
                result.omni_field.hidden = True
                visibility = "hidden (not in the CSV scope and no SQL names it)"
            suggest_from = result.omni_field.suggest_from_field or ''
            if is_template and suggest_from.startswith(f"{view_name}."):
                # A template has no rows: its own fields cannot supply suggestions
                result.omni_field.suggest_from_field = None
                result.warnings.append(
                    f"suggest_dimension `{suggest_from}` not carried: a template view cannot supply suggestions. "
                    f"Set `suggest_from_field` on the views that extend it"
                )
            omni_filters[name] = result.omni_field
            print(f"      ✓ {kind}: {name}")
            line = f"  {name} (Looker {kind}, type {looker_type} → `type: {result.omni_field.type}`): {visibility}"
            notes = list(result.warnings) + ([f"{MANUAL}: {problem}"] if problem else [])
            if looker_type == 'unquoted' and not problem:
                notes.append(
                    "`type: column` quotes the value as an identifier: each allowed value must be a full column "
                    "or `schema.table` name" if result.omni_field.type == 'column' else
                    "`type: unquoted` parameter written as `type: string`: Omni renders the value as a quoted "
                    "string and cannot inject raw SQL"
                )
            if notes:
                line += ". " + '. '.join(notes)
                issues.extend(f"{name}: {note}" for note in notes)
            lines.append(line)
        if lines:
            report_builder.add_advanced_feature(
                feature="Parameters and Filter-Only Fields",
                status="CONVERTED, REVIEW",
                details="Looker `parameter:` and `filter:` written as Omni filter-only fields (the view's "
                        "`filters:` key):\n" + '\n'.join(lines),
                action_required="Check each one in Omni. A parameter is single select "
                                "(`filter_single_select_only: true`). To show a hidden one, list it in the CSV "
                                "with type `filter`."
            )
        return omni_filters

    @staticmethod
    def _report_liquid(
        report_builder: ConversionReportBuilder,
        issues: List[str],
        retargeted: Set[str],
        omni_dimensions: Dict[str, OmniDimension],
        omni_measures: Dict[str, OmniMeasure],
        inherited: Dict[str, ExposedField]
    ):
        """Report what the Liquid translation needs from a human"""
        attributes = report_builder.liquid_attributes()
        if attributes:
            issues.append("User attributes used in translated SQL; create them in Omni: " + ', '.join(attributes))
            report_builder.add_advanced_feature(
                feature="User Attributes (translated)",
                status=f"CARRIED - {MANUAL}",
                details="Translated SQL reads these user attributes through `{{ omni_attributes.name }}`: "
                        + ', '.join(f"`{name}`" for name in attributes),
                action_required="Create each user attribute in Omni under the same name before deploying."
            )
        written = sorted(name for name in retargeted if name in omni_dimensions or name in omni_measures)
        if written:
            report_builder.add_advanced_feature(
                feature="Templated Fields Rewritten From extends",
                status="CONVERTED, REVIEW",
                details="These inherited fields use Liquid that names the view itself. A Mustache token names one "
                        "view, so they were written into this view with tokens that name it, tagged "
                        f"`{RESOLVED_FROM_EXTENDS_TAG}`: "
                        + ', '.join(f"{name} (from `{inherited[name].origin}`)" for name in written),
                action_required="Check each field against Looker. A field is visible when the CSV lists it under "
                                "this view or under the view it comes from, and hidden otherwise."
            )

    @staticmethod
    def _report_extends_conflicts(
        resolved_fields: Dict[str, Any],
        omni_dimensions: Dict[str, OmniDimension],
        omni_measures: Dict[str, OmniMeasure],
        report_builder: ConversionReportBuilder,
        omni_filters: Optional[Dict[str, OmniFilter]] = None
    ):
        """Report the fields two parents define differently and what was written for them"""
        lines = []
        for name, inherited in sorted(resolved_fields.items()):
            written = name in omni_dimensions or name in omni_measures or name in (omni_filters or {})
            parents = ', '.join(f"`{parent}`" for parent in inherited.conflict)
            lines.append(
                f"  {name} ({inherited.kind}): defined differently by {parents}. Looker uses the last "
                f"(`{inherited.conflict[-1]}`), Omni would use the first (`{inherited.conflict[0]}`). "
                + (f"Written into this view with the Looker definition, tagged `{RESOLVED_FROM_EXTENDS_TAG}`."
                   if written else "NOT written: the Looker definition could not be converted (see Not Converted).")
            )
        report_builder.add_advanced_feature(
            feature="Extends Conflict (fields resolved from extends)",
            status="CONVERTED, REVIEW",
            details="Parents in `extends:` define these fields differently:\n" + '\n'.join(lines),
            action_required="Check each field against Looker. A field outside the CSV scope is written hidden."
        )

    @staticmethod
    def _column_fields(looker_view: LookerView, existing_fields: Dict) -> Dict[str, str]:
        """
        {warehouse column: field} for fields whose FINAL definition is exactly
        that column, so `${TABLE}.col` can safely become `${field}`:

        - Looker dimensions whose sql is `${TABLE}.col` (or absent), unless an
          existing Omni field of the same name has a different sql (an
          out-of-scope existing field keeps its own definition when merging)
        - fields of an existing Omni view that are not Looker fields at all
          and have no sql (or sql equal to their name). An existing field that
          is also a Looker field is never used: its definition may be
          replaced by the Looker one (e.g. `${TABLE}.amount * 100`).
        """
        def plain(name: str, data: Any) -> bool:
            sql = data.get('sql') if isinstance(data, dict) else None
            return sql in (None, name, f'"{name}"', f'${{{name}}}')

        looker_names = set(looker_view.dimensions) | set(looker_view.dimension_groups) | set(looker_view.measures)
        mapping: Dict[str, str] = {}
        for name, dim in sorted(looker_view.dimensions.items()):
            sql = (dim.get('sql') or '${TABLE}.' + name).strip()
            match = re.fullmatch(r'\$\{TABLE\}\.([A-Za-z_][A-Za-z0-9_]*)', sql)
            if not match or dim.get('type') in ('tier', 'location', 'distance') or dim.get('case'):
                continue
            if name in existing_fields and not plain(name, existing_fields[name]):
                continue
            column = match.group(1)
            if column not in mapping or column == name:
                mapping[column] = name
        for name, data in existing_fields.items():
            if name in mapping.values() or name in mapping or name in looker_names:
                continue
            if plain(name, data):
                mapping[name] = name
        return mapping

    @staticmethod
    def _field_types(looker_view: LookerView) -> Dict[str, str]:
        types = {name: d.get('type', 'string') for name, d in looker_view.dimensions.items()}
        types.update({name: 'time' for name in looker_view.dimension_groups})
        types.update({name: 'measure' for name in looker_view.measures})
        return types

    def _resolve_cross_view_type(self, view_name: str, field_name: str) -> Optional[str]:
        """Looker type of view.field in another view (None if unknown)"""
        other = self._full_view(view_name)
        if not other:
            return None
        if field_name in other.dimensions:
            return other.dimensions[field_name].get('type', 'string')
        if field_name in other.dimension_groups:
            return 'time'
        if field_name in other.measures:
            return 'measure'
        return None

    @staticmethod
    def _blocked_fields(
        results: Dict[str, Tuple[str, ConversionResult]],
        existing_names: Set[str],
        inherited: Optional[Dict[str, ExposedField]] = None
    ) -> Dict[str, str]:
        """
        Fields that converted but cannot be written because a same-view
        reference cannot be satisfied: undefined, not convertible, or circular.
        A reference to an inherited field (`inherited`, from `extends:`) is
        satisfied when the parent view migrates that field.
        Returns {field: reason}.
        """
        inherited = inherited or {}
        status: Dict[str, Optional[str]] = {}
        stack: List[str] = []

        def check(name: str) -> Optional[str]:
            if name in status:
                return status[name]
            if name in stack:
                return "circular reference"
            _, result = results[name]
            if not result.omni_field:
                status[name] = "not convertible"
                return status[name]
            stack.append(name)
            reason = None
            for dep in sorted(result.dependencies):
                if dep not in results:
                    if dep in existing_names:
                        continue
                    exposed = inherited.get(dep)
                    if exposed is not None and exposed.reason is None:
                        continue
                    if exposed is not None:
                        reason = (f"depends on inherited field {dep} (from view {exposed.origin}), "
                                  f"which is not migrated ({exposed.reason})")
                        break
                    reason = f"references ${{{dep}}}, which is not defined in the view"
                    break
                if dep in stack:
                    reason = f"circular reference through {dep}"
                    break
                dep_reason = check(dep)
                if dep_reason:
                    reason = f"depends on {dep}, which is not migrated ({dep_reason})"
                    break
            stack.pop()
            status[name] = reason
            return reason

        blocked = {}
        for name, (_, result) in results.items():
            if result.omni_field:
                reason = check(name)
                if reason:
                    blocked[name] = reason
        return blocked

    @staticmethod
    def _add_result(report_builder: ConversionReportBuilder, kind: str, name: str, result: ConversionResult):
        if kind == 'measure':
            report_builder.add_measure_result(name, result)
        else:
            report_builder.add_dimension_result(name, result)

    def _find_existing_view(self, view_name: str, view_filename: str, is_derived: bool) -> Tuple[Optional[Dict], Optional[str]]:
        """
        Existing Omni view to merge into.

        --omni-source-repo is searched recursively for `{view}.view.yaml`,
        `{view}.view` (and `{view}.query.view` for derived tables); more than
        one match is an error. Otherwise a previous run's output file in the
        output dir is used.
        """
        if self.omni_source_repo:
            names = [f"{view_name}.view.yaml", f"{view_name}.view"]
            if is_derived:
                names.insert(0, f"{view_name}.query.view")
            matches = sorted({p for n in names for p in self.omni_source_repo.rglob(n)})
            if len(matches) > 1:
                raise MergeError(
                    f"More than one existing Omni view for {view_name} in {self.omni_source_repo}: "
                    + ', '.join(str(m) for m in matches)
                )
            if matches:
                existing = self.file_generator.read_view_from_path(matches[0])
                if existing is not None:
                    return existing, str(matches[0])
        existing = self.file_generator.read_existing_omni_view(view_filename)
        if existing is not None:
            return existing, str(self.omni_repo / view_filename)
        return None, None

    def _report_field_grants(self, field_grants: Dict[str, List[str]], final_view: Dict,
                             report_builder: ConversionReportBuilder, issues: List[str],
                             renames: Optional[Dict[str, str]] = None):
        """
        Report field grants from the FINAL output. Each Looker field with
        grants is resolved to its final field (same name, or the field it was
        merged into by this or an earlier run) and classified as present and
        protected, present but unprotected (MANUAL), or absent.
        """
        if not field_grants:
            return
        renames = renames or {}
        final_fields = {**(final_view.get('dimensions') or {}), **(final_view.get('measures') or {})}
        protected, unprotected, absent = [], [], []
        for name, grants in sorted(field_grants.items()):
            target = renames.get(name, name)
            label = target if target == name else f"{target} (Looker {name})"
            data = final_fields.get(target)
            if not isinstance(data, dict) and target in final_fields:
                data = {}
            if data is None:
                absent.append(name)
            elif set(grants) <= set(data.get('required_access_grants') or []):
                protected.append(f"  {label}:\n"
                                 + '\n'.join('  ' + line for line in self._format_grant_details(grants).split('\n')))
            else:
                unprotected.append(f"{label} is in the output without required_access_grants {grants}")
        all_grants = sorted({g for gs in field_grants.values() for g in gs})
        if protected:
            issues.append(f"Field access grants copied; define them in the Omni model: {', '.join(all_grants)}")
            report_builder.add_advanced_feature(
                feature="Field-Level Access Grants",
                status=f"CARRIED - {MANUAL}",
                details="required_access_grants present on these fields in the output:\n" + '\n'.join(protected),
                action_required="Define each grant in the Omni model file (access_grants:) before deploying."
            )
        if unprotected:
            issues.extend(f"{MANUAL}: {u}" for u in unprotected)
            report_builder.add_advanced_feature(
                feature="Field-Level Access Grants (NOT applied)",
                status=MANUAL,
                details='\n'.join(unprotected),
                action_required="Add the grants to these fields before deploying."
            )
        if absent:
            report_builder.add_advanced_feature(
                feature="Field-Level Access Grants (fields not in the output)",
                status="INFO",
                details="These Looker fields have grants but are not in the output view: " + ', '.join(absent),
                action_required="If you add them by hand, add their required_access_grants too."
            )

    def _flag_view_features(self, looker_view: LookerView, report_builder: ConversionReportBuilder, issues: List[str],
                            resolution: Optional[ViewExtendsResolution] = None):
        """Report view-level LookML features; access-related ones are also issues"""
        if looker_view.refinement_files:
            issues.append(f"View is refined in {', '.join(looker_view.refinement_files)}; refinements are not applied")
            report_builder.add_advanced_feature(
                feature="Refinements (view: +name)",
                status=MANUAL,
                details="Refinements found in: " + ', '.join(looker_view.refinement_files),
                action_required="Apply the refinement's changes (fields, access settings) to the Omni view by hand."
            )
        if looker_view.extends and (resolution is None or resolution.problem):
            problem = f" ({resolution.problem})" if resolution is not None else ""
            issues.append(f"View extends {', '.join(looker_view.extends)}; inherited fields are not included{problem}")
            report_builder.add_advanced_feature(
                feature="View Inheritance (extends)",
                status=MANUAL,
                details=f"View extends: {', '.join(looker_view.extends)}. Only fields declared in this file were "
                        f"migrated and `extends:` was not written{problem}.",
                action_required="Migrate the base view(s) and add the inherited fields this view needs."
            )
        elif looker_view.extends:
            self._report_view_inheritance(looker_view, resolution, report_builder, issues)
        if looker_view.access_filters:
            issues.append(f"View declares {len(looker_view.access_filters)} access_filter(s); not carried")
            report_builder.add_advanced_feature(
                feature="Access Filters",
                status=MANUAL,
                details=f"View has {len(looker_view.access_filters)} access_filter(s)",
                action_required="Recreate as Omni topic access_filters."
            )
        if looker_view.has_user_attributes:
            issues.append("View uses _user_attributes in Liquid")
            report_builder.add_advanced_feature(
                feature="User Attributes (Liquid Templates)",
                status=MANUAL,
                details="View uses _user_attributes in Liquid templates. `{{ _user_attributes['x'] }}` used as a "
                        "value in sql is translated to `{{ omni_attributes.x }}` (see Liquid translated); any other "
                        "use (for example inside `{% if %}`) is not converted.",
                action_required="Create each user attribute in Omni under the same name, and rewrite by hand the "
                                "logic of the fields that were not converted."
            )
        if looker_view.required_access_grants:
            issues.append(f"View-level required_access_grants not carried: {', '.join(looker_view.required_access_grants)}")
            report_builder.add_advanced_feature(
                feature="View-Level Access Grants",
                status=MANUAL,
                details=f"View requires access grants:\n{self._format_grant_details(looker_view.required_access_grants)}",
                action_required="Define the grants in the Omni model and add required_access_grants to the view or the topics that use it."
            )
        if looker_view.drill_fields or looker_view.sets:
            report_builder.add_not_converted(
                'view drill_fields/sets',
                "Not migrated: view-level drill_fields and sets have no automatic Omni conversion"
            )

    def _report_view_inheritance(
        self,
        looker_view: LookerView,
        resolution: ViewExtendsResolution,
        report_builder: ConversionReportBuilder,
        issues: List[str]
    ):
        """Report a resolved `extends:` chain and what it brings that is not carried"""
        parents = ', '.join(resolution.parents)
        origins: Dict[str, List[str]] = {}
        for name, inherited in sorted(resolution.fields.items()):
            origins.setdefault(inherited.origin, []).append(name)
        details = [
            f"`extends: [{parents}]` written to the view (same order as Looker).",
            f"Views in the chain: {', '.join(resolution.ancestors)}.",
            "Inherited fields are defined in the parent views and are not copied here: "
            + ('; '.join(f"from `{origin}`: {', '.join(names)}" for origin, names in origins.items()) or '(none)')
            + ".",
        ]
        report_builder.add_advanced_feature(
            feature="View Inheritance (extends)",
            status="CONVERTED",
            details='\n'.join(details),
            action_required=f"Migrate the views in the chain too ({', '.join(resolution.ancestors)}): `extends:` "
                            f"needs a view of each name in the Omni model."
        )

        refinements = self._load_views()[1]
        for ancestor in resolution.ancestors:
            parent = self._cached_view(ancestor)
            if refinements.get(ancestor):
                issues.append(f"Parent view {ancestor} is refined in {', '.join(refinements[ancestor])}; "
                              f"refinements are not applied")
                report_builder.add_advanced_feature(
                    feature=f"Refinements of parent view {ancestor}",
                    status=MANUAL,
                    details="Refinements found in: " + ', '.join(refinements[ancestor]),
                    action_required="Apply the refinement's changes to the Omni view by hand."
                )
            if parent is None:
                continue
            if parent.required_access_grants and not looker_view.required_access_grants:
                issues.append(
                    f"View-level required_access_grants inherited from {ancestor} not carried: "
                    f"{', '.join(parent.required_access_grants)}"
                )
                report_builder.add_advanced_feature(
                    feature=f"View-Level Access Grants inherited from {ancestor}",
                    status=MANUAL,
                    details=f"Parent view `{ancestor}` requires access grants, which this view inherits in Looker:\n"
                            f"{self._format_grant_details(parent.required_access_grants)}",
                    action_required="Define the grants in the Omni model and add required_access_grants to the view "
                                    "or the topics that use it."
                )
            if parent.access_filters and not looker_view.access_filters:
                issues.append(f"Parent view {ancestor} declares {len(parent.access_filters)} access_filter(s); "
                              f"not carried")
                report_builder.add_advanced_feature(
                    feature=f"Access Filters inherited from {ancestor}",
                    status=MANUAL,
                    details=f"Parent view `{ancestor}` has {len(parent.access_filters)} access_filter(s)",
                    action_required="Recreate as Omni topic access_filters."
                )

    # ==================================================================
    # Explore analysis (view mode, --explore-analysis)
    # ==================================================================

    def analyze_explores_for_view(self, view_name: str) -> Optional[Path]:
        """Write {view}_explore_analysis.md for explores that use the view"""
        explores = find_explores_using_views(str(self.looker_repo), {view_name})
        print(f"Found {len(explores)} explore(s) using {view_name}:")
        for explore in explores:
            print(f"  - {explore.name} (base: {explore.base_view}, joins: {len(explore.joins)})")
        if not explores:
            return None
        suggestions = suggest_topic_divisions(explores, {view_name})
        report = generate_explore_report(explores, {view_name}, suggestions)
        return self.file_generator.generate_text_report(report, f"{view_name}_explore_analysis.md")

    # ==================================================================
    # Explore migration
    # ==================================================================

    def migrate_explore(
        self,
        explore_name: str,
        topic_name: Optional[str] = None,
        relationships_file: bool = False,
        template: bool = False,
        topic_prefix: Optional[str] = None,
        phase: Optional[int] = None,
        verified: bool = True
    ) -> MigrationResult:
        """
        Migrate a Looker explore to an Omni topic

        Args:
            explore_name: Name of the Looker explore (e.g., "order_items")
            topic_name: Optional output name (defaults to explore_name)
            relationships_file: Write the joins to a separate
                `{topic}.relationships` file instead of the topic's own
                `relationships:` key
            template: Force `template: true` on the topic
            topic_prefix: Prefix of the topic file (`{prefix}__{explore}`),
                also applied to the topics named in `extends:`. The value
                `model` uses the name of the Looker model that holds the
                explore
            phase: Migration phase to tag the fields written into the
                topic's `views:` block with (no phase tag when None)
            verified: Tag those fields `verified` (True) or `unverified`
        """
        writes_before = len(self.file_generator.planned_writes)
        issues: List[str] = []
        try:
            print(f"\n{'='*60}")
            print(f"MIGRATING EXPLORE: {explore_name}")
            print(f"{'='*60}\n")

            print("[1/4] Finding explore...")
            explore_info, refinement_files = self._find_explore(explore_name)
            if not explore_info:
                raise ValueError(f"Explore '{explore_name}' not found in Looker repository")
            print(f"      ✓ Found: {explore_info.file_path}")
            manual: List[Tuple[str, str]] = []  # (title, details) -> report + issues
            carried: List[str] = []

            # Inheritance: from here on explore_info is the merged explore
            declared = explore_info
            explore_info, inheritance = self._resolve_inheritance(declared, manual)
            print(f"      ✓ Base view: {explore_info.base_view}")
            print(f"      ✓ Joins: {len(explore_info.joins)}")

            print("\n[2/4] Building Omni topic...")
            prefix = self._topic_prefix(topic_prefix, declared)
            topic_name = topic_name or (f"{prefix}__{explore_name}" if prefix else explore_name)
            # The real view, and the name the explore's fields go by (the topic's base_view)
            base_view = explore_info.base_view
            base_alias = explore_info.base_alias

            if refinement_files:
                manual.append((
                    "Refinements (explore: +name) NOT applied",
                    "Refinements found in: " + ', '.join(refinement_files)
                ))
            topic_view_blocks: Dict[str, Dict[str, Any]] = {}
            if explore_info.base_is_aliased:
                if base_alias in self._load_views()[0]:
                    raise ValueError(
                        f"{MANUAL}: the explore's base is view `{base_view}` under the name `{base_alias}`, and "
                        f"`{base_alias}` is also a view under --looker-repo. Declaring the alias in the topic "
                        f"would change that view inside the topic. Migrate this explore manually"
                    )
                topic_view_blocks[base_alias] = {'extends': [base_view]}
                carried.append(
                    f"base view alias `{base_alias}` (view `{base_view}`) → topic `base_view: {base_alias}`, "
                    f"declared in the topic's `views:` block with `extends: [{base_view}]`"
                )
                if relationships_file:
                    manual.append((
                        "Base view alias with --relationships-file",
                        f"`{base_alias}` is declared in this topic only, and a relationships file is model-wide: "
                        f"its `join_from_view: {base_alias}` does not resolve outside this topic. Use topic-scoped "
                        f"relationships (the default) for this explore."
                    ))

            relationships, join_tree, skipped = self._build_relationships(explore_info, manual, carried)
            # {name a view goes by in the topic: the real view}
            topic_views = {name: real for name, real in explore_info.view_names().items() if name not in skipped}

            # Row-level restrictions and presentation metadata
            topic_extra: Dict[str, Any] = {}
            if explore_info.access_filters:
                topic_extra['access_filters'] = [
                    {'field': af.get('field'), 'user_attribute': af.get('user_attribute')}
                    for af in explore_info.access_filters
                ]
                for af in explore_info.access_filters:
                    carried.append(f"access_filter on `{af.get('field')}` (user attribute `{af.get('user_attribute')}`) → topic `access_filters`")
                manual.append((
                    "Access filters (carried)",
                    "Create these user attributes in Omni before deploying: "
                    + ', '.join(f"`{af.get('user_attribute')}`" for af in explore_info.access_filters)
                ))
            for looker_key, omni_key in (('sql_always_where', 'always_where_sql'), ('sql_always_having', 'always_having_sql')):
                value = getattr(explore_info, looker_key)
                if not value:
                    continue
                if has_liquid(value):
                    manual.append((f"{looker_key} (NOT carried)", f"Uses Liquid, which Omni does not support: `{value}`"))
                else:
                    topic_extra[omni_key] = value
                    carried.append(f"{looker_key} → topic `{omni_key}`")
                    manual.append((f"{looker_key} (carried)", f"Verify `{value}` returns the same rows in Omni."))
            if explore_info.always_filter:
                default_filters = self._build_default_filters(explore_info, set(skipped), manual, carried)
                if default_filters:
                    topic_extra['default_filters'] = default_filters
            if explore_info.conditionally_filter:
                manual.append((
                    "conditionally_filter (NOT carried)",
                    f"Looker conditionally_filter: `{self._describe_filter_block(explore_info.conditionally_filter)}`."
                ))
            if explore_info.always_join:
                manual.append((
                    "always_join (NOT carried)",
                    f"Looker always joins {', '.join(explore_info.always_join)}, which can change row counts. "
                    f"Omni only joins views a query needs; reproduce the restriction (e.g. with always_where_sql) by hand."
                ))
            if explore_info.access_grants:
                topic_extra['required_access_grants'] = list(explore_info.access_grants)
                carried.append(f"required_access_grants → topic `required_access_grants` ({', '.join(explore_info.access_grants)})")
                manual.append((
                    "Access grants (carried)",
                    "Define these grants in the Omni model file (`access_grants:`):\n"
                    + self._format_grant_details(explore_info.access_grants)
                ))
            if explore_info.hidden:
                topic_extra['hidden'] = True
                carried.append("hidden: yes → topic `hidden: true`")
            if explore_info.group_label:
                topic_extra['group_label'] = explore_info.group_label
                carried.append("group_label → topic `group_label`")
            for join in explore_info.joins:
                if join.required_access_grants:
                    manual.append((
                        f"Join access grants on {join.view} (NOT carried)",
                        f"Looker only joins `{join.view}` for users with: {', '.join(join.required_access_grants)}. "
                        f"Omni topics have no per-join grants; restrict the view (required_access_grants) or "
                        f"split the topic.\n" + self._format_grant_details(join.required_access_grants)
                    ))
                if join.required_joins:
                    manual.append((
                        f"required_joins on {join.view} (NOT carried)",
                        f"Looker always adds {', '.join(join.required_joins)} when `{join.view}` is used. "
                        f"Check the Omni join path produces the same rows."
                    ))
            if inheritance is not None:
                topic_extra['extends'] = [f"{prefix}__{parent}" if prefix else parent for parent in declared.extends]
                carried.append(
                    f"extends: [{', '.join(declared.extends)}] → topic `extends: "
                    f"[{', '.join(topic_extra['extends'])}]`, "
                    f"with the merged joins and settings written into this topic"
                )

            # References through an alias must name a field of the view the alias stands for
            self._verify_alias_references(explore_info, topic_views, relationships, topic_extra, manual)

            # The topic's `views:` block: base alias, view labels, fields that need a join alias
            for name, label in [(base_alias, explore_info.view_label)] + [
                    (join.view, join.view_label) for join in explore_info.joins]:
                if label and name in topic_views and label != self._own_view_label(topic_views[name]):
                    topic_view_blocks.setdefault(name, {})['label'] = label
                    carried.append(f"view_label of `{name}` → `label: {label}` in the topic's `views:` block")
            topic_fields, topic_field_lines = self._topic_scoped_for_topic(topic_views, phase, verified, manual)
            for name, sections in topic_fields.items():
                topic_view_blocks.setdefault(name, {}).update(sections)

            # Explore and join `fields:` restrictions → topic `fields:` selectors
            restrictions = self._field_restrictions(explore_info, topic_views, topic_fields, set(skipped))
            carried.extend(restrictions.carried)
            manual.extend(restrictions.manual)
            if restrictions.carried:
                manual.append((
                    "Field restrictions (carried)",
                    "Written to the topic `fields:` list. Looker hides the restricted fields; Omni rejects queries "
                    "that use them. Verify the fields exist under these names in Omni."
                    + ''.join(f"\n- {item}" for item in restrictions.verify)
                ))
            if topic_view_blocks:
                topic_extra['views'] = {
                    name: {key: topic_view_blocks[name][key]
                           for key in ('label', 'extends', 'dimensions', 'measures') if key in topic_view_blocks[name]}
                    for name in topic_views if name in topic_view_blocks
                }

            extenders = find_extending_explores(explore_name, self._load_explores()[0])
            is_template, topic_type = self._template_decision(declared, extenders, template, manual)
            if is_template:
                topic_extra['template'] = True

            ai_context_lines = [
                f"This topic provides access to {explore_info.label or explore_name} data.",
                "",
                f"Base view: {base_alias}",
                f"Joined views: {', '.join(r.get('join_to_view_as', r['join_to_view']) for r in relationships) or '(none)'}",
                "",
            ]
            if explore_info.description:
                ai_context_lines += [explore_info.description, ""]
            ai_context_lines += [
                "This topic was migrated from a Looker explore.",
                f"Original explore: {explore_name}",
            ]

            topic = OmniTopic(
                base_view=base_alias,
                label=explore_info.label or explore_name.replace('_', ' ').title(),
                description=explore_info.description or f"Migrated from Looker explore: {explore_name}",
                fields=restrictions.selectors + [DEFAULT_FIELD_EXCLUSION],
                joins=join_tree,
                ai_context='\n'.join(ai_context_lines),
                **topic_extra
            )
            print(f"      ✓ Topic: {topic_name} ({len(relationships)} join(s), {len(skipped)} skipped)")

            relationships_path = self.omni_repo / f"{topic_name}.relationships"
            if relationships_file:
                # Same view pair joined differently by another explore's relationships file.
                # Topic-scoped relationships only apply to their own topic, so no check there.
                for conflict in self._relationship_conflicts(relationships, exclude=relationships_path):
                    manual.append(("Conflicting relationship", conflict))
            elif relationships:
                topic.relationships = relationships

            report = self._build_explore_report(
                explore_name, topic_name, explore_info, relationships, skipped, carried, manual,
                relationships_file=relationships_file, topic_type=topic_type, inheritance=inheritance,
                skipped_views=skipped, topic_field_lines=topic_field_lines, prefix=prefix,
                has_views_block=bool(topic_view_blocks), restrictions=restrictions
            )

            # Render everything before writing anything
            topic_content = self.file_generator.render_topic_yaml(topic)
            relationships_content = None
            if relationships and relationships_file:
                header = [
                    f"Relationships migrated from Looker explore: {explore_name}",
                    f"Base view: {base_alias}. Joins: {len(relationships)}.",
                ]
                if skipped:
                    header.append(f"Not migrated (see {topic_name}_explore.md): {', '.join(skipped)}")
                relationships_content = self.file_generator.render_relationships_yaml(relationships, header)

            print("\n[3/4] Writing topic file...")
            topic_path = self.file_generator.write_file(self.omni_repo / f"{topic_name}.topic.yaml", topic_content)
            print(f"      ✓ Topic: {topic_path}")

            written_relationships = None
            if relationships_content is not None:
                written_relationships = self.file_generator.write_file(relationships_path, relationships_content)
                print(f"      ✓ Relationships: {written_relationships}")
            else:
                # A previous run's file would keep joins that no longer exist,
                # or repeat the joins that now live in the topic file
                self.file_generator.remove_file(relationships_path)

            print("\n[4/4] Writing explore migration report...")
            report_path = self.file_generator.generate_text_report(report, f"{topic_name}_explore.md")
            print(f"      ✓ Report: {report_path}")

            issues.extend(f"{title}: {details.splitlines()[0]}" for title, details in manual)
            self._print_issues(explore_name, issues)
            return MigrationResult(
                looker_view_name=explore_name,
                omni_view_name=topic_name,
                view_file_path=Path(''),
                topic_file_path=topic_path,
                report_file_path=report_path,
                success=True,
                issues=issues,
                planned_files=self.file_generator.planned_writes[writes_before:],
                relationships_file_path=written_relationships
            )

        except Exception as e:
            return self._failure(explore_name, e, issues, writes_before, "EXPLORE MIGRATION FAILED")

    def _load_explores(self) -> Tuple[Dict[str, List[ExploreInfo]], Dict[str, List[str]]]:
        """Every explore under --looker-repo (parsed once per run), see `load_explores`"""
        if self._explore_cache is None:
            self._explore_cache = load_explores(str(self.looker_repo))
        return self._explore_cache

    def _find_explore(self, explore_name: str) -> Tuple[Optional[ExploreInfo], List[str]]:
        """
        The explore definition and any refinement files (`explore: +name`).

        Raises:
            ValueError: the explore is defined in more than one file
        """
        definitions, refinements = self._load_explores()
        found = definitions.get(explore_name, [])
        if len(found) > 1:
            raise ValueError(
                f"Explore '{explore_name}' is defined more than once: "
                + ', '.join(sorted({e.file_path for e in found}))
                + ". Point --looker-repo at a single project."
            )
        return (found[0] if found else None), list(refinements.get(explore_name, []))

    def _resolve_inheritance(
        self,
        declared: ExploreInfo,
        manual: List[Tuple[str, str]]
    ) -> Tuple[ExploreInfo, Optional[ExtendsResolution]]:
        """
        Merge the explore with its `extends:` chain

        Returns the explore to migrate (the merged one, or `declared` when it
        extends nothing or the chain cannot be resolved) and the resolution
        when a merge happened. Everything that needs a human goes to `manual`.
        """
        if not declared.extends:
            return declared, None
        definitions, refinements = self._load_explores()
        parents = ', '.join(declared.extends)
        resolution = resolve_explore_extends(declared, definitions)
        if resolution.problem:
            manual.append((
                "Explore inheritance (extends)",
                f"Explore extends {parents}; inherited joins and settings were not merged: {resolution.problem}. "
                f"`extends:` was not written to the topic."
            ))
            return declared, None

        manual.append((
            "Explore inheritance (extends, merged)",
            f"The topic extends {parents} and also carries the merged joins and settings. "
            f"Migrate the parent explore(s) too ({', '.join(f'`--explore {p}`' for p in declared.extends)}): "
            f"`extends:` needs a topic of that name in the Omni model."
        ))
        if len(declared.extends) > 1:
            manual.append((
                "Multiple parents in extends",
                f"Looker gives priority to the last explore in `extends: [{parents}]`, and the merged result written "
                f"to this topic follows that. Omni documents `extends` with a single topic: verify the topic "
                f"validates, or keep only one parent in `extends:`."
            ))
        for name in resolution.ancestors:
            if refinements.get(name):
                manual.append((
                    "Refinements (explore: +name) NOT applied",
                    f"Parent explore `{name}` has refinements that were not merged: " + ', '.join(refinements[name])
                ))
        for join, by, inherited_from, partial in resolution.overridden_joins:
            if partial:
                manual.append((
                    f"Join {join}: partial override of an inherited join",
                    f"`{by}` redeclares join `{join}` inherited from `{inherited_from}` without all of its parameters. "
                    f"The parameters it leaves out were kept from `{inherited_from}`; verify the merged join."
                ))
        for attribute, by, inherited_from in resolution.overridden_settings:
            if attribute == 'access_filters':
                manual.append((
                    "Access filters: child replaces the parent's",
                    f"Both `{by}` and `{inherited_from}` define access_filter. Only those of `{by}` were written; "
                    f"verify that Looker does not also apply those of `{inherited_from}`."
                ))
        return resolution.explore, resolution

    @staticmethod
    def _template_decision(
        declared: ExploreInfo,
        extenders: List[str],
        forced: bool,
        manual: List[Tuple[str, str]]
    ) -> Tuple[bool, str]:
        """Whether the topic is a template (`template: true`), and the reason for the report"""
        extended_by = ', '.join(f"`{name}`" for name in extenders)
        if forced:
            return True, "template (`template: true`), forced with `--template`"
        if declared.extension_required and extenders:
            return True, (f"template (`template: true`): the explore has `extension: required` "
                          f"and is extended by {extended_by}")
        if declared.extension_required:
            manual.append((
                "Template topic is not extended",
                "The explore has `extension: required`, so the topic was written with `template: true`, but no "
                "explore under --looker-repo extends it. A template topic cannot be used on its own."
            ))
            return True, ("template (`template: true`): the explore has `extension: required` "
                          "(no explore under --looker-repo extends it)")
        if extenders:
            return False, (f"normal topic: extended by {extended_by}, but without `extension: required` the explore "
                           f"is usable on its own in Looker. Pass `--template` if it is only a base")
        return False, "normal topic"

    def _build_default_filters(
        self,
        explore_info: ExploreInfo,
        skipped: Set[str],
        manual: List[Tuple[str, str]],
        carried: List[str]
    ) -> Dict[str, Dict[str, Any]]:
        """
        Looker always_filter -> required Omni default filters

        A filter is written only when its field resolves to a view of the
        topic and its expression is one of the forms translated with
        confidence; every other filter is reported with the Looker expression.
        """
        filters: Dict[str, Dict[str, Any]] = {}
        not_carried: List[str] = []
        pairs = extract_measure_filters(explore_info.always_filter) \
            if isinstance(explore_info.always_filter, dict) else []
        if not pairs:
            not_carried.append(f"`{explore_info.always_filter}`: no `filters:` could be read from the block")

        not_carried_fields: List[str] = []
        date_keys: Set[str] = set()  # filters on a date field
        for looker_field, expression in pairs:
            quoted = f'`{looker_field}: "{"" if expression is None else expression}"`'
            key, condition, reason, is_date = self._translate_always_filter(
                explore_info, skipped, looker_field, expression)
            if reason is None and key in filters:
                reason = f"`{key}` already has a filter from another always_filter entry"
            if reason:
                not_carried.append(f"{quoted}: {reason}")
                not_carried_fields.append(quoted)
                continue
            filters[key] = {**condition, 'required': True}
            if is_date:
                date_keys.add(key)
            shown = ', '.join(
                f"{operator}: " + (f"[{', '.join(map(str, value))}]" if isinstance(value, list) else str(value))
                for operator, value in condition.items()
            )
            carried.append(f"always_filter {quoted} → topic `default_filters` on `{key}` (`{shown}`, `required: true`)")

        unconfirmed = [
            f"`{key}`: `is: {condition['is']}`" for key, condition in filters.items()
            if condition.get('is') in NAMED_DATE_PERIODS and condition['is'] not in CONFIRMED_DATE_PERIODS
            and key in date_keys
        ]
        if unconfirmed:
            manual.append((
                "Named date periods (verify)",
                "These required filters use a named period. `is: "
                + CONFIRMED_DATE_PERIODS[0] + "` was confirmed on a live Omni instance; the periods below use the "
                "same mechanism and were not confirmed one by one. Verify each returns the Looker date range: "
                + ', '.join(unconfirmed) + "."
            ))
        if filters:
            manual.append((
                "always_filter (carried)",
                "Written as required `default_filters` (users can change the value, not remove the filter) on "
                + ', '.join(f"`{key}`" for key in filters)
                + ". Verify the fields exist under these names in Omni and the filters return the same rows."
            ))
        if not_carried:
            manual.append((
                "always_filter (NOT carried)",
                "Looker always_filter not written"
                + (": " + ', '.join(not_carried_fields) if not_carried_fields else "")
                + ". Recreate each one as an Omni `default_filters` entry with `required: true` (it needs a value).\n"
                + '\n'.join(f"- {item}" for item in not_carried)
            ))
        return filters

    def _translate_always_filter(
        self,
        explore_info: ExploreInfo,
        skipped: Set[str],
        looker_field: str,
        expression: Any
    ) -> Tuple[Optional[str], Optional[Dict[str, Any]], Optional[str], bool]:
        """
        (Omni filter key, condition, None, the field is a date) or
        (None, None, reason it is not translated, False)
        """
        view_ref, _, field_name = str(looker_field).partition('.')
        if not field_name:
            return None, None, "the field is not view-qualified (`view.field`)", False

        joins = {join.view: join for join in explore_info.joins}
        if view_ref == explore_info.base_alias:
            # The key keeps the name the topic declares (the base alias); the type comes from the real view
            looker_view, omni_view = explore_info.base_view, explore_info.base_alias
        elif view_ref in joins:
            if view_ref in skipped:
                return None, None, f"join `{view_ref}` was not migrated, so the field is not in the topic", False
            looker_view, omni_view = joins[view_ref].source_view, view_ref
        else:
            return None, None, f"`{view_ref}` is not part of the explore", False

        if expression is None or not str(expression).strip():
            return None, None, "it has no value (a required default filter needs one)", False

        parsed_view = self._full_view(looker_view)
        if parsed_view is None:
            return None, None, (f"view `{looker_view}` was not found under --looker-repo, so the field type is "
                                f"unknown"), False
        omni_field, kind, reason = self._filter_target(parsed_view, field_name)
        if reason:
            return None, None, reason, False

        if kind == 'date':
            condition, reason = translate_date_filter_expression(expression)
            if condition is None:
                return None, None, reason, False
        else:
            condition = translate_filter_expression(expression, kind)
            # Only "is <value>" (strings: also a list of values) is carried
            value = condition.get('is') if condition and set(condition) == {'is'} else None
            if value is None or (kind == 'number' and isinstance(value, list)):
                return None, None, "the expression is outside the forms translated automatically", False
        return f"{omni_view}.{omni_field}", condition, None, kind == 'date'

    @staticmethod
    def _filter_target(looker_view: LookerView, field_name: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        """
        Omni field name and filter grammar ('string', 'number' or 'date') for
        a Looker field used in always_filter, or the reason it is not handled.
        A dimension_group timeframe (`created_date`) maps to the group's Omni
        name (`created`).
        """
        if field_name in looker_view.dimensions:
            looker_type = looker_view.dimensions[field_name].get('type')
            if looker_type in DATE_DIMENSION_TYPES:
                return field_name, 'date', None
            kind = filter_kind(looker_type)
            if kind in ('string', 'number'):
                return field_name, kind, None
            return None, None, f"dimension type `{looker_type}` is not translated automatically"
        if field_name in looker_view.measures:
            return None, None, "the field is a measure (a filter on a measure is applied after aggregation)"
        for group, data in looker_view.dimension_groups.items():
            if not field_name.startswith(f"{group}_") or data.get('type') != 'time':
                continue
            timeframe = field_name[len(group) + 1:]
            if timeframe not in DATE_FILTER_TIMEFRAMES:
                return None, None, (f"timeframe `{timeframe}` of dimension group `{group}`: only the "
                                    f"{', '.join(DATE_FILTER_TIMEFRAMES)} timeframes are translated")
            if data.get('timeframes') and timeframe not in data['timeframes']:
                return None, None, f"timeframe `{timeframe}` is not in the `timeframes:` of dimension group `{group}`"
            return group, 'date', None
        return None, None, (f"the field is not defined in view `{looker_view.view_name}` as a dimension or a "
                            f"dimension group timeframe (it may be inherited or a filter-only field)")

    def _build_relationships(
        self,
        explore_info: ExploreInfo,
        manual: List[Tuple[str, str]],
        carried: Optional[List[str]] = None
    ) -> Tuple[List[Dict[str, Any]], Dict, List[str]]:
        """
        Convert joins to Omni relationships and a nested topic join tree

        Looker aliases are kept: the base view goes by the explore's base
        alias and an aliased join by `join_to_view_as`, so `on_sql` is
        written as in Looker. A reference to the `date` or `raw` timeframe
        of a dimension group is rewritten to `${view.group[date]}`; any
        other timeframe leaves the join out.

        Each join hangs off the one joined view its sql_on references (besides
        itself); referencing only the base view puts it at the top level. The
        graph is resolved independently of declaration order. A join is
        skipped (and reported) when it cannot be converted, references an
        unknown view, references more than one other joined view, is part of
        a cycle, or hangs off a skipped join.
        """
        base_view = explore_info.base_alias
        base_names = {base_view}
        joins: Dict[str, JoinDefinition] = {}
        for join in explore_info.joins:
            joins[join.view] = join
        names = explore_info.view_names()
        timeframes = RuleApplier(phase=0, view_fields=lambda view: self._view_field_info(names.get(view)))
        on_sql: Dict[str, str] = {}

        problems: Dict[str, str] = {}
        parents: Dict[str, Optional[str]] = {}
        for name, join in joins.items():
            if join.sql_on is None:
                if join.foreign_key:
                    problems[name] = f"uses foreign_key: {join.foreign_key}; write the on_sql manually"
                elif join.sql:
                    problems[name] = "uses a raw sql: join; write the on_sql manually"
                else:
                    problems[name] = "has no sql_on"
                continue
            if has_liquid(join.sql_on):
                problems[name] = f"sql_on uses Liquid, which Omni does not support. Original: `{join.sql_on}`"
                continue
            if join.type is not None and join.type not in JOIN_TYPE_MAP:
                problems[name] = f"join type `{join.type}` has no Omni join_type equivalent"
                continue
            if join.relationship is not None and join.relationship not in RELATIONSHIP_TYPES:
                problems[name] = f"relationship `{join.relationship}` is not supported"
                continue
            referenced = {v for v, _ in _FIELD_REF_RE.findall(join.sql_on)} - {name}
            unknown = referenced - base_names - set(joins)
            if unknown:
                problems[name] = f"sql_on references {', '.join(sorted(unknown))}, which is not part of the explore"
                continue
            join_parents = sorted(referenced - base_names)
            if len(join_parents) > 1:
                problems[name] = (f"sql_on references several joined views ({', '.join(join_parents)}); "
                                  f"Omni needs one parent per join")
                continue
            rewrites: List[str] = []
            warnings: List[str] = []
            rewritten = timeframes.rewrite_timeframe_references(join.sql_on, [], warnings, rewrites)
            if rewritten is None:
                problems[name] = "sql_on: " + '; '.join(w.replace(f"{MANUAL}: ", '') for w in warnings)
                continue
            on_sql[name] = rewritten
            if rewrites and carried is not None:
                carried.append(f"join `{name}` sql_on: timeframe reference rewritten: {', '.join(rewrites)}")
            parents[name] = join_parents[0] if join_parents else None

        # Resolve: a join is usable if its parent chain is usable and acyclic
        state: Dict[str, Optional[str]] = {}

        def resolve(name: str, path: Tuple[str, ...]) -> Optional[str]:
            if name in problems:
                return problems[name]
            if name in state:
                return state[name]
            if name in path:
                return "circular join dependency: " + ' → '.join(path + (name,))
            parent = parents[name]
            reason = None
            if parent is not None:
                parent_reason = resolve(parent, path + (name,))
                if parent_reason:
                    reason = f"depends on join {parent}, which is not migrated ({parent_reason})"
            state[name] = reason
            return reason

        skipped = []
        for name in joins:
            reason = resolve(name, ())
            if reason:
                skipped.append(name)
                manual.append((f"Join {name} (NOT migrated)", reason))

        relationships: List[Dict[str, Any]] = []
        for name, join in joins.items():
            if name in skipped:
                continue
            parent = parents[name]
            relationship: Dict[str, Any] = {}
            if parent is None:
                relationship['join_from_view'] = base_view
            else:
                parent_join = joins[parent]
                relationship['join_from_view'] = parent_join.source_view
                if parent_join.is_aliased:
                    relationship['join_from_view_as'] = parent
            relationship['join_to_view'] = join.source_view
            if join.is_aliased:
                relationship['join_to_view_as'] = name
                if name in self._load_views()[0]:
                    manual.append((
                        f"Join alias {name} has the name of another view",
                        f"`join: {name} {{ from: {join.source_view} }}` became `join_to_view: {join.source_view}` "
                        f"with `join_to_view_as: {name}`, and `{name}` is also a view under --looker-repo. Verify "
                        f"that `{name}` means `{join.source_view}` inside this topic in Omni."
                    ))
            relationship.update({
                'join_type': JOIN_TYPE_MAP[join.type or 'left_outer'],
                'on_sql': on_sql[name],
                'relationship_type': join.relationship or 'many_to_one',
                'reversible': False,
            })
            relationships.append(relationship)
            if self._rename_candidates(join.sql_on, names):
                manual.append((
                    f"Join {name}: renamed fields",
                    "sql_on uses Looker fields that read a differently named column ("
                    + ', '.join(self._rename_candidates(join.sql_on, names))
                    + "). If those views were merged into existing Omni views, the fields are named after the column."
                ))

        # Nested join tree for the topic
        tree: Dict[str, Dict] = {}
        nodes: Dict[str, Dict] = {name: {} for name in joins if name not in skipped}
        for name in nodes:
            parent = parents[name]
            (nodes[parent] if parent is not None else tree)[name] = nodes[name]
        return relationships, tree, skipped

    def _topic_prefix(self, topic_prefix: Optional[str], explore: ExploreInfo) -> Optional[str]:
        """
        The prefix of the topic file (None for no prefix)

        The value `model` is the name of the one Looker model file that
        holds the explore (see `find_models_including`).

        Raises:
            ValueError: `model` was asked for and no single model holds the explore
        """
        if not topic_prefix:
            return None
        if topic_prefix != 'model':
            if not re.fullmatch(r'[A-Za-z0-9_]+', topic_prefix):
                raise ValueError(f"--topic-prefix `{topic_prefix}` must be letters, digits and underscores only")
            return topic_prefix
        models = find_models_including(str(self.looker_repo), explore.file_path)
        if len(models) != 1:
            found = (f"it is in several models ({', '.join(models)})" if models
                     else "no *.model.lkml file under --looker-repo defines it or includes its file")
            raise ValueError(
                f"--topic-prefix model: no single Looker model holds explore `{explore.name}` "
                f"(`{explore.file_path}`): {found}. Pass the prefix by name, e.g. --topic-prefix my_model"
            )
        return models[0]

    def _own_view_label(self, view_name: str) -> Optional[str]:
        """The label view mode writes on a view (None when the view is not found)"""
        view = self._cached_view(view_name)
        if view is None:
            return None
        return self._apply_canonical_to_label(view.label or view_name.replace('_', ' ').title())

    def _verify_alias_references(
        self,
        explore_info: ExploreInfo,
        topic_views: Dict[str, str],
        relationships: List[Dict[str, Any]],
        topic_extra: Dict[str, Any],
        manual: List[Tuple[str, str]]
    ):
        """
        Check every `${alias.field}` the topic writes against the view the
        alias stands for (own fields, inherited fields, dimension group
        timeframes) and report the ones that do not resolve

        Covers the join conditions, `access_filters`, `always_where_sql` and
        `always_having_sql`. A name that is not a view of the topic at all
        is reported too. References by a view's own name are not checked.
        """
        references: List[Tuple[str, str, str]] = []  # (where, view name in the topic, field)
        for relationship in relationships:
            where = f"join `{relationship.get('join_to_view_as', relationship['join_to_view'])}` on_sql"
            references += [(where, view, name) for view, name in _OMNI_REF_RE.findall(relationship['on_sql'])]
        for key in ('always_where_sql', 'always_having_sql'):
            references += [(key, view, name) for view, name in _OMNI_REF_RE.findall(topic_extra.get(key) or '')]
        for access_filter in topic_extra.get('access_filters') or []:
            view, _, name = str(access_filter.get('field') or '').partition('.')
            if name:
                references.append(("access_filters", view, name))

        unresolved: List[str] = []
        unverified: Dict[str, str] = {}
        for where, view, name in references:
            if view not in topic_views:
                if view in explore_info.view_names():
                    reason = f"join `{view}` was not migrated"
                else:
                    reason = f"`{view}` is not a view of this topic"
                unresolved.append(f"{where}: `${{{view}.{name}}}`: {reason}")
                continue
            real = topic_views[view]
            if real == view:
                continue
            if self._view_field_info(real) is None:
                unverified[view] = real
                continue
            problem = self._field_problem(real, name)
            if problem:
                unresolved.append(f"{where}: `${{{view}.{name}}}`: `{view}` is `{real}` in this topic, and {problem}")
        if unresolved:
            manual.append((
                "Unresolved field references",
                "These references do not resolve in the topic. Fix them before deploying.\n"
                + '\n'.join(f"- {item}" for item in dict.fromkeys(unresolved))
            ))
        if unverified:
            manual.append((
                "Alias references not verified",
                "References through these aliases could not be checked, because the view is not a single view "
                "under --looker-repo: "
                + ', '.join(f"`{alias}` (view `{real}`)" for alias, real in unverified.items())
                + ". Verify the field references to them."
            ))

    def _topic_scoped_for_topic(
        self,
        topic_views: Dict[str, str],
        phase: Optional[int],
        verified: bool,
        manual: List[Tuple[str, str]]
    ) -> Tuple[Dict[str, Dict[str, Dict[str, Dict]]], List[str]]:
        """
        The fields view mode leaves out of the view files because they need
        a join alias (`_topic_scoped_fields`), converted for this topic

        Each view of the topic is converted with the rules of view mode,
        with the topic's own names for the other views. A field is written
        under the view's name in the topic when every view it names is in
        the topic and has the field it reads. A field that needs an alias
        this topic does not have is left out (it does not work in the
        Looker explore either). A field that cannot be converted, or whose
        alias cannot be checked, is a manual item.

        These fields have no scope CSV row here: they are written visible
        unless Looker hides them.

        Returns:
            ({view name in the topic: {'dimensions' | 'measures': {field: yaml dict}}}, report lines)
        """
        blocks: Dict[str, Dict[str, Dict[str, Dict]]] = {}
        lines: List[str] = []
        not_written: List[str] = []
        for topic_name, real in topic_views.items():
            scoped = self._topic_scoped_fields(real)
            if not scoped:
                continue
            full = self._full_view(real)
            _, results, blocked = self._convert_view_fields(
                full, {}, {}, phase if phase is not None else 0, verified, view_resolver=topic_views.get
            )
            grants = {prefixed.split(':', 1)[-1]: list(names)
                      for prefixed, names in (full.field_access_grants or {}).items()}
            kinds = self._field_kinds(full)
            outcome: Dict[str, Tuple[str, str]] = {}

            def decide(name: str) -> Tuple[str, str]:
                if name not in outcome:
                    outcome[name] = ('manual', 'circular reference')  # replaced below; guards recursion
                    outcome[name] = self._topic_field_outcome(
                        name, kinds[results[name][0]][name], results[name][1], blocked, scoped, topic_views, decide)
                return outcome[name]

            for name, (kind, result) in results.items():
                if name not in scoped:
                    continue
                state, reason = decide(name)
                label = f"`{topic_name}.{name}` ({kind.replace('_', ' ')}, view `{real}`)"
                needs = ', '.join(f"`{alias}`" for alias in sorted(scoped[name]))
                if state == 'left_out':
                    lines.append(f"- {label}: left out, {reason}. The field does not work in the Looker explore "
                                 f"either.")
                    continue
                if state == 'manual':
                    lines.append(f"- {label}: NOT written ({MANUAL}): {reason}")
                    not_written.append(f"- `{topic_name}.{name}`: {reason}")
                    continue
                data = (measure_to_yaml_dict if kind == 'measure' else dimension_to_yaml_dict)(result.omni_field)
                if phase is None:
                    data['tags'] = [tag for tag in data.get('tags', []) if not tag.startswith('mig_phase')]
                if name in grants:
                    data['required_access_grants'] = grants[name]
                section = 'measures' if kind == 'measure' else 'dimensions'
                blocks.setdefault(topic_name, {}).setdefault(section, {})[name] = data
                notes = [f"timeframe reference rewritten: {', '.join(result.timeframes)}"] if result.timeframes else []
                notes += [f"Liquid translated: {', '.join(result.liquid.translated)}"] if result.liquid else []
                notes += list(result.warnings)
                lines.append(f"- {label}: written, uses {needs}" + ''.join(f". {note}" for note in notes))
                if name in grants:
                    not_written.append(
                        f"- `{topic_name}.{name}` was written with required_access_grants {grants[name]}: define "
                        f"the grants in the Omni model"
                    )
        if not_written:
            manual.append((
                "Cross-view fields needing attention",
                "Fields that reference a join alias are written in the topic's `views:` block. These need a "
                "human:\n" + '\n'.join(not_written)
            ))
        return blocks, lines

    def _topic_field_outcome(
        self,
        name: str,
        looker_field: Dict,
        result: ConversionResult,
        blocked: Dict[str, str],
        scoped: Dict[str, Set[str]],
        topic_views: Dict[str, str],
        decide
    ) -> Tuple[str, str]:
        """
        What happens to one topic-scoped field in a topic:
        ('written', ''), ('left_out', reason) or ('manual', reason)

        The views the field names are checked first, in the LookML and in
        the converted field: a field this topic cannot serve is left out
        whether or not it converts.
        """
        looker_sql = looker_field.get('sql') if isinstance(looker_field.get('sql'), str) else ''
        references = set(_FIELD_REF_RE.findall(looker_sql))
        references |= {tuple(str(key).split('.', 1)) for key, _ in extract_measure_filters(looker_field)
                       if '.' in str(key)}
        if result.omni_field:
            references |= set(_OMNI_REF_RE.findall(result.omni_field.sql or ''))
            references |= {tuple(key.split('.', 1)) for key in (result.omni_field.filters or {}) if '.' in key}
        for view, field_name in sorted(references):
            if view not in topic_views:
                return 'left_out', f"`{view}` is not a view of this topic"
            real = topic_views[view]
            if self._view_field_info(real) is None:
                # The view behind the name is not known here: do not guess
                return 'manual', (f"`{view}` is view `{real}` in this topic, which is not a single view under "
                                  f"--looker-repo, so `${{{view}.{field_name}}}` could not be checked")
            problem = self._field_problem(real, field_name)
            if problem:
                return 'left_out', f"`{view}` is `{real}` in this topic, and {problem}"
        for dep in sorted(result.dependencies):
            if dep in scoped:
                state, _ = decide(dep)
                if state != 'written':
                    return state, f"it depends on `{dep}`, which is not written in this topic"
        if not result.omni_field:
            return 'manual', '; '.join(result.warnings) or 'not convertible'
        if name in blocked:
            return 'manual', blocked[name]
        for target in (result.liquid.fields if result.liquid else []):
            if topic_views.get(target.view) != target.view:
                return 'manual', (f"its translated Liquid names view `{target.view}`, which this topic does not "
                                  f"have under that name")
        return 'written', ''

    def _rename_candidates(self, sql_on: str, names: Optional[Dict[str, str]] = None) -> List[str]:
        """Fields of sql_on that read a differently named column; `names` maps the explore's names to real views"""
        out = []
        for view_name, field_name in _FIELD_REF_RE.findall(sql_on):
            column = self._get_db_column_for_field((names or {}).get(view_name, view_name), field_name)
            if column:
                out.append(f"{view_name}.{field_name} → column {column}")
        return out

    def _relationship_conflicts(self, relationships: List[Dict[str, Any]], exclude: Path) -> List[str]:
        """Relationships in other .relationships files of the output dir for the same views, with different SQL"""
        import yaml as pyyaml
        conflicts = []
        if not self.omni_repo.exists():
            return conflicts

        def key(rel):
            return (rel.get('join_from_view'), rel.get('join_to_view'), rel.get('join_to_view_as'))

        mine = {key(r): r for r in relationships}
        for path in sorted(self.omni_repo.glob('*.relationships')):
            if path.resolve() == Path(exclude).resolve():
                continue
            try:
                others = pyyaml.safe_load(path.read_text()) or []
            except Exception:
                continue
            for other in others if isinstance(others, list) else []:
                if not isinstance(other, dict):
                    continue
                rel = mine.get(key(other))
                if rel and (rel['on_sql'] != other.get('on_sql') or rel['join_type'] != other.get('join_type')
                            or rel['relationship_type'] != other.get('relationship_type')):
                    conflicts.append(
                        f"{path.name} joins {other.get('join_from_view')} → {other.get('join_to_view')} differently "
                        f"(`{other.get('on_sql')}`). Relationships are model-wide in Omni; keep one definition "
                        f"or use topic-level relationships."
                    )
        return conflicts

    @staticmethod
    def _describe_filter_block(block: Any) -> str:
        pairs = []
        for group in (block.get('filters__all') or []) if isinstance(block, dict) else []:
            for item in (group if isinstance(group, list) else [group]):
                if isinstance(item, dict):
                    pairs.extend(f"{k}: {v}" for k, v in item.items())
        return ', '.join(pairs) if pairs else str(block)

    def _build_explore_report(
        self,
        explore_name: str,
        topic_name: str,
        explore_info: ExploreInfo,
        relationships: List[Dict[str, Any]],
        skipped: List[str],
        carried: List[str],
        manual: List[Tuple[str, str]],
        relationships_file: bool = False,
        topic_type: str = "normal topic",
        inheritance: Optional[ExtendsResolution] = None,
        skipped_views: Optional[List[str]] = None,
        topic_field_lines: Optional[List[str]] = None,
        prefix: Optional[str] = None,
        has_views_block: bool = False,
        restrictions: Optional[FieldRestrictions] = None
    ) -> str:
        base = explore_info.base_view
        if explore_info.base_is_aliased:
            base = f"{explore_info.base_alias} (alias of view `{explore_info.base_view}`)"
        lines = [
            f"# Explore Migration: {explore_name}",
            "",
            "## Summary",
            f"- **Explore**: {explore_name}",
            f"- **Source file**: `{explore_info.file_path}`",
            f"- **Base View**: {base}",
            f"- **Topic**: {topic_name}" + (f" (prefix `{prefix}`, from `--topic-prefix`)" if prefix else ""),
            f"- **Topic type**: {topic_type}",
            f"- **Joins migrated**: {len(relationships)} of {len(explore_info.joins)}",
            "",
        ]

        if manual:
            lines += ["## ⚠️ MANUAL ACTION REQUIRED", ""]
            for title, details in manual:
                lines.append(f"### {title}")
                lines.append(details)
                lines.append("")

        lines += ["## Carried Into the Topic", ""]
        lines += [f"- {item}" for item in carried] or ["(no row-level restrictions, access grants or presentation settings)"]
        lines.append("")

        if inheritance is not None:
            lines += self._inheritance_report_lines(explore_name, explore_info, inheritance)

        lines += self._alias_map_lines(explore_info, skipped_views or [], relationships_file)
        if topic_field_lines:
            lines += [
                "## Topic-scoped fields",
                "",
                "Fields whose SQL references another view through a join alias. They are left out of the view "
                "files (Omni model validation rejects them there) and written in this topic's `views:` block when "
                "the topic has every alias they need. They have no scope CSV row in explore mode: each is written "
                "visible unless Looker hides it.",
                "",
            ] + topic_field_lines + [""]

        lines += self._field_restriction_lines(explore_info, restrictions)

        lines += ["## Join Definitions", ""]
        for join in explore_info.joins:
            status = "NOT migrated" if join.view in skipped else "migrated"
            origin = explore_info.sources.get(f"join:{join.view}", explore_name)
            if origin != explore_name:
                status += f", inherited from {origin}"
            lines.append(f"### {join.view} ({status})")
            if join.is_aliased:
                lines.append(f"- **From**: {join.source_view}")
            if join.type:
                lines.append(f"- **Type**: {join.type}")
            if join.relationship:
                lines.append(f"- **Relationship**: {join.relationship}")
            if join.sql_on:
                lines.append(f"- **SQL On**: `{join.sql_on}`")
            lines.append("")

        lines += ["## Generated Files", ""]
        if relationships_file:
            lines.append(f"1. **{topic_name}.topic.yaml**: topic with field list and join tree")
            if relationships:
                lines.append(f"2. **{topic_name}.relationships**: join definitions (YAML list)")
            relationships_note = ("- Relationships files are model-wide in Omni: check for conflicting joins "
                                  "between the same views")
        else:
            lines.append(f"1. **{topic_name}.topic.yaml**: topic with field list, join tree and the join "
                         f"definitions (top-level `relationships:`)"
                         + (", plus the topic-scoped view definitions (`views:`)" if has_views_block else ""))
            relationships_note = ("- Relationships are topic-scoped (the topic's own `relationships:` key): they apply "
                                  "to this topic only. Use `--relationships-file` to write them to a separate file "
                                  "for the model-wide relationships file instead")
        lines += [
            "",
            "## Notes",
            "",
            ("- Topic `fields:` carries the Looker field restrictions (see Field restrictions) and excludes fields "
             "tagged `table_key`" if restrictions is not None and restrictions.restricted
             else "- Topic uses `all_views.*` and excludes fields tagged `table_key`"),
            "- Looker join types mapped: left_outer → always_left, inner → inner, "
            "full_outer → full_outer, cross → cross (other types are not migrated)",
            relationships_note,
            "- Cross-view references stay `${view.field}`",
            "",
        ]
        return '\n'.join(lines)

    def _view_sets(self, view_name: str) -> Dict[str, List[str]]:
        """The `set:` definitions of a view: its own and the inherited ones (a nearer definition wins)"""
        view = self._cached_view(view_name)
        resolution = self._view_resolution(view_name)
        sets: Dict[str, List[str]] = {}
        for ancestor in (resolution.ancestors if resolution is not None and not resolution.problem else []):
            parent = self._cached_view(ancestor)
            sets.update(parent.sets if parent is not None else {})
        sets.update(view.sets if view is not None else {})
        return sets

    def _field_restrictions(
        self,
        explore_info: ExploreInfo,
        topic_views: Dict[str, str],
        topic_fields: Dict[str, Dict[str, Dict[str, Dict]]],
        skipped: Set[str]
    ) -> FieldRestrictions:
        """
        Looker explore and join `fields:` → Omni topic `fields:` selectors
        (see `lib.field_restrictions`)

        `topic_fields` holds the fields this topic writes in its `views:`
        block: they are in the topic although the view file leaves them out.
        """
        join_fields = {join.view: join.fields for join in explore_info.joins}
        if explore_info.fields is None and all(entries is None for entries in join_fields.values()):
            return FieldRestrictions(selectors=[INCLUDE_ALL])
        views: Dict[str, Optional[TopicViewFields]] = {}
        for name, real in topic_views.items():
            full = self._full_view(real)
            info = self._view_field_info(real)
            resolution = self._view_resolution(real)
            if full is None or info is None or (resolution is not None and resolution.problem):
                views[name] = None  # fields not known: a list that names this view is not carried
                continue
            written_here = {field for section in topic_fields.get(name, {}).values() for field in section}
            views[name] = TopicViewFields(
                real_view=real,
                fields=info[0],
                time_groups=info[1],
                filter_fields=set(full.parameters) | set(full.filters),
                sets=self._view_sets(real),
                missing={field: exposed.reason for field, exposed in self._exposed_fields(real).items()
                         if exposed.reason and field not in written_here}
            )
        return build_topic_fields(explore_info.fields, join_fields, views, skipped)

    @staticmethod
    def _field_restriction_lines(explore_info: ExploreInfo, restrictions: Optional[FieldRestrictions]) -> List[str]:
        """Report section: the Looker `fields:` lists and the topic selectors they became"""
        looker = [("explore", explore_info.fields)] + [(f"join `{join.view}`", join.fields)
                                                       for join in explore_info.joins]
        looker = [(where, entries) for where, entries in looker if entries is not None]
        if restrictions is None or not looker:
            return []
        lines = [
            "## Field restrictions",
            "",
            "Looker `fields:` lists of the explore and its joins, written as Omni topic `fields:` selectors. Omni "
            "evaluates selectors by type, not list order: `all_views.*`, then `view.*`, then tags, then "
            "`view.field`. Looker hides a restricted field; Omni rejects queries that use it. A field listed by "
            "name stays in the topic even when it is tagged `table_key`.",
            "",
        ]
        lines += [f"- Looker {where}: `fields: {looker_list(entries)}`" for where, entries in looker]
        lines += [f"- Topic: `fields: [{', '.join(restrictions.selectors + [DEFAULT_FIELD_EXCLUSION])}]`", ""]
        not_carried = [title for title, _ in restrictions.manual]
        if not_carried:
            lines += ["Not carried (see MANUAL ACTION REQUIRED): " + '; '.join(not_carried), ""]
        if restrictions.notes:
            lines += ["Entries dropped or mapped:", ""] + [f"- {note}" for note in restrictions.notes] + [""]
        return lines

    @staticmethod
    def _alias_map_lines(explore_info: ExploreInfo, skipped: List[str], relationships_file: bool) -> List[str]:
        """Report section: every alias of the explore, the view behind it and where the topic declares it"""
        rows = []
        if explore_info.base_is_aliased:
            rows.append((explore_info.base_alias, explore_info.base_view,
                         f"topic `views:` block (`extends: [{explore_info.base_view}]`), used as `base_view`"))
        for join in explore_info.joins:
            if not join.is_aliased:
                continue
            if join.view in skipped:
                where = "not declared (the join was not migrated)"
            elif relationships_file:
                where = "`join_to_view_as` in the relationships file"
            else:
                where = "`join_to_view_as` in the topic's `relationships:`"
            rows.append((join.view, join.source_view, where))
        lines = ["## Alias map", ""]
        if not rows:
            return lines + ["(no aliases: every view goes by its own name)", ""]
        lines += [
            "Aliases are kept from Looker and scoped to this topic: another topic can use the same alias for a "
            "different view.",
            "",
            "| Alias | Real view | Where declared |",
            "|---|---|---|",
        ]
        lines += [f"| `{alias}` | `{real}` | {where} |" for alias, real, where in rows]
        return lines + [""]

    @staticmethod
    def _inheritance_report_lines(explore_name: str, merged: ExploreInfo, inheritance: ExtendsResolution) -> List[str]:
        """Report section: what the topic inherited through `extends:` and from where"""
        labels = {
            'access_grants': 'required_access_grants', 'access_filters': 'access_filter',
            'from_view': 'from',
        }
        lines = [
            "## Inheritance (extends)",
            "",
            f"- **Extends**: {', '.join(merged.extends)}",
            f"- **Explores merged** (Looker order, later wins, this explore last): "
            f"{', '.join(inheritance.ancestors + [explore_name])}",
            "- The topic carries the complete merged result because a child topic's `joins:` replaces the "
            "parent's in Omni (Looker adds them).",
            "",
        ]
        inherited = {item: origin for item, origin in merged.sources.items() if origin != explore_name}
        joins = [f"`{item[5:]}` (from {origin})" for item, origin in inherited.items() if item.startswith('join:')]
        settings = [f"{labels.get(item, item)} (from {origin})" for item, origin in inherited.items()
                    if not item.startswith('join:')]
        lines.append("**Inherited joins**: " + (', '.join(joins) or '(none)'))
        lines.append("")
        lines.append("**Inherited settings**: " + (', '.join(settings) or '(none)'))
        lines.append("")
        overrides = [
            f"join `{join}`: `{by}` overrides `{origin}`" + (" (partial, parameters merged)" if partial else "")
            for join, by, origin, partial in inheritance.overridden_joins
        ] + [
            f"{labels.get(attribute, attribute)}: `{by}` overrides `{origin}`"
            for attribute, by, origin in inheritance.overridden_settings
        ]
        if overrides:
            lines.append("**Overrides**:")
            lines += [f"- {item}" for item in overrides]
            lines.append("")
        return lines

    # ==================================================================
    # Helpers
    # ==================================================================

    @staticmethod
    def _print_issues(name: str, issues: List[str]):
        print(f"\n{'='*60}")
        if issues:
            print(f"⚠️  COMPLETED WITH {len(issues)} ITEM(S) NEEDING MANUAL ACTION: {name}")
            for issue in issues:
                print(f"   - {issue}")
        else:
            print(f"✅ MIGRATION COMPLETE: {name}")
        print(f"{'='*60}\n")

    def _cached_view(self, view_name: str) -> Optional[LookerView]:
        """The view as declared, from the repo-wide index; None if missing or defined in several files"""
        try:
            return view_from_index(self._load_views()[0], str(self.looker_repo), view_name)
        except (FileNotFoundError, ValueError):
            return None

    def _get_db_column_for_field(self, view_name: str, field_name: str) -> Optional[str]:
        """Warehouse column a Looker dimension reads, if it differs from its name"""
        looker_view = self._full_view(view_name)
        if not looker_view or field_name not in looker_view.dimensions:
            return None
        sql = looker_view.dimensions[field_name].get('sql')
        return RuleApplier(phase=0).extract_db_column_name(sql, field_name)

    def _apply_canonical_to_label(self, label: str) -> str:
        """Apply the configured canonical mappings (empty by default) to a label"""
        mapped, _ = RuleApplier(phase=0, canonical_mappings=self.config.canonical_mappings).apply_canonical_mapping(label)
        return mapped

    def _determine_view_source(
        self,
        looker_view: LookerView,
        report_builder: ConversionReportBuilder,
        issues: List[str],
        inherited_source: Optional[ViewSource] = None,
        translations: Optional[List[LiquidTranslation]] = None
    ) -> Tuple[str, Dict[str, Any]]:
        """
        Build the view source: {'table_name'[, 'catalog']}, {'sql'} or {'query'}

        A view that declares no source of its own uses the one it inherits
        through `extends:` (`inherited_source`), written out explicitly.

        Liquid in a derived table's SQL is translated to Omni Mustache when
        every construct in it has an exact equivalent; the translation is
        appended to `translations` so the caller can report it and check the
        fields it names.

        Returns:
            (schema, source_dict)

        Raises:
            ValueError: the source cannot be converted without guessing
        """
        derived_table, sql_table_name = looker_view.derived_table, looker_view.sql_table_name
        if not derived_table and not sql_table_name and inherited_source is not None:
            derived_table, sql_table_name = inherited_source.derived_table, inherited_source.sql_table_name
            report_builder.add_advanced_feature(
                feature="Inherited View Source",
                status="CONVERTED",
                details=f"The view declares no sql_table_name or derived_table. It inherits the "
                        f"{'derived table' if derived_table else 'table'} of `{inherited_source.origin}` through "
                        f"`extends:`, which was written into this view explicitly.",
                action_required="Review the table or SQL."
            )

        if derived_table:
            derived = derived_table

            persistence = [k for k in ('datagroup_trigger', 'sql_trigger_value', 'persist_for',
                                       'indexes', 'partition_keys', 'cluster_keys', 'increment_key')
                           if derived.get(k)]
            if persistence:
                report_builder.add_advanced_feature(
                    feature="Persisted Derived Table Settings",
                    status="NOT MIGRATED",
                    details=f"Looker persistence settings not migrated: {', '.join(persistence)}",
                    action_required="Configure caching/materialization in Omni or the warehouse if needed."
                )

            if derived.get('sql'):
                raw_sql = derived['sql']
                print(f"      ℹ️  SQL derived table detected ({len(raw_sql)} chars)")
                sql = self._clean_sql_for_omni(raw_sql)
                templated = ""
                if has_liquid(sql):
                    translation = translate_liquid(sql, self._liquid_context(looker_view.view_name))
                    if translation.sql is None:
                        raise ValueError(
                            f"{MANUAL}: the derived table {translation.reason}. Migrate this view manually"
                        )
                    sql = translation.sql
                    if translations is not None:
                        translations.append(translation)
                    templated = " Liquid was translated to Omni Mustache (see Liquid translated)."
                # After the Liquid: a SQL comment removed with its Liquid takes its references with it
                sql, substitutions = self._substitute_table_references(sql, looker_view.view_name)
                if substitutions:
                    report_builder.add_advanced_feature(
                        feature="Table References Substituted",
                        status="CONVERTED",
                        details="`${view.SQL_TABLE_NAME}` references to views with a literal `sql_table_name` were "
                                "replaced by that table name: " + '; '.join(substitutions),
                        action_required="Check each table name. If the referenced view changes its table later, "
                                        "this SQL does not follow."
                    )
                if re.search(r'\$\{[^}]+\}', sql):
                    raise ValueError(
                        f"{MANUAL}: the derived table SQL contains ${{...}} references that are not "
                        f"`${{view.SQL_TABLE_NAME}}` references to a plain table; migrate this view manually"
                    )
                schema = self._extract_schema_from_sql(sql)
                report_builder.add_advanced_feature(
                    feature="SQL Derived Table",
                    status="CONVERTED",
                    details="Converted to an Omni query view with a top-level sql: block." + templated,
                    action_required="Review the SQL and table references."
                )
                return schema, {'sql': sql}

            if derived.get('explore_source'):
                explore_source = derived['explore_source']
                print("      ℹ️  Native derived table detected (explore_source)")
                query = self._convert_explore_source_to_query(explore_source, report_builder, issues)
                report_builder.add_advanced_feature(
                    feature="Native Derived Table (explore_source)",
                    status="NEEDS MANUAL REVIEW",
                    details="Converted explore_source to an Omni query: block (fields, base_view, topic, filters, sorts, limit).",
                    action_required="Check that the topic and field names exist in Omni and that results match Looker."
                )
                return self.default_schema, {'query': query}

            raise ValueError("Derived table has neither sql nor explore_source; migrate this view manually")

        if sql_table_name:
            return self._parse_table_name(sql_table_name)

        # Looker defaults the table to the view name
        issues.append("View has no sql_table_name or derived_table; using the view name as table")
        report_builder.add_advanced_feature(
            feature="Missing View Source",
            status=MANUAL,
            details="View has no sql_table_name or derived_table; Looker would use the view name as the table.",
            action_required="Confirm the table name and schema."
        )
        return self.default_schema, {'table_name': looker_view.view_name}

    def _plain_table_of(self, view_name: str, current_view: str) -> Tuple[Optional[str], Optional[str], str]:
        """
        The literal `sql_table_name` a `${view.SQL_TABLE_NAME}` reference
        stands for, as (table, view it is inherited from or None, '') or
        (None, None, reason it is not substituted)
        """
        definitions = self._load_views()[0].get(view_name, [])
        if not definitions:
            return None, None, f"view `{view_name}` was not found under --looker-repo"
        if len(definitions) > 1:
            return None, None, f"view `{view_name}` is defined more than once under --looker-repo"
        view = definitions[0]
        if view.refinement_files:
            return None, None, (f"view `{view_name}` has refinements (`view: +{view_name}`) that are not applied, "
                                f"so its table is not certain")
        table, derived, origin = view.sql_table_name, view.derived_table, None
        if not table and not derived and view.extends:
            resolution = self._view_resolution(view_name)
            if resolution is None or resolution.problem:
                return None, None, (f"the `extends:` of view `{view_name}` could not be resolved"
                                    + (f" ({resolution.problem})" if resolution is not None else ""))
            if resolution.source is not None:
                table, derived = resolution.source.sql_table_name, resolution.source.derived_table
                origin = resolution.source.origin
        if derived or view_name == current_view:
            return None, None, (f"view `{view_name}` is itself a derived table"
                                + (f" (inherited from `{origin}`)" if origin else ""))
        if not table or not str(table).strip():
            return None, None, (f"view `{view_name}` has no `sql_table_name` (Looker would use the view name as "
                                f"the table; that is not assumed here)")
        table = str(table).strip()
        if has_liquid(table) or '${' in table or '@{' in table:
            return None, None, f"the table name of view `{view_name}` is templated (`{table}`)"
        return table, origin, ''

    def _substitute_table_references(self, sql: str, current_view: str) -> Tuple[str, List[str]]:
        """
        Replace each `${view.SQL_TABLE_NAME}` that points at a plain table
        with that table name, verbatim

        Returns:
            (sql, ["`${view.SQL_TABLE_NAME}` → `table`"]) for the report

        Raises:
            ValueError: a reference points at a derived table, a view that
                is not found, or a templated table name
        """
        substitutions: List[str] = []
        problems: List[str] = []

        def substitute(match):
            name = match.group(1)
            reference = f"${{{name}.SQL_TABLE_NAME}}"
            table, origin, reason = self._plain_table_of(name, current_view)
            if table is None:
                problems.append(f"`{reference}`: {reason}")
                return match.group(0)
            shown = f"`` {table} ``" if '`' in table else f"`{table}`"
            item = f"`{reference}` → {shown}" + (f" (inherited from `{origin}`)" if origin else "")
            if item not in substitutions:
                substitutions.append(item)
            return table

        out = _SQL_TABLE_NAME_RE.sub(substitute, sql)
        if problems:
            raise ValueError(
                f"{MANUAL}: the derived table SQL references a table through SQL_TABLE_NAME that cannot be "
                f"substituted: " + '; '.join(dict.fromkeys(problems)) + ". Migrate this view manually"
            )
        return out, substitutions

    def _clean_sql_for_omni(self, sql: str) -> str:
        """Remove `${TABLE}.` prefixes from derived table SQL"""
        return re.sub(r'\$\{TABLE\}\.', '', sql)

    def _extract_schema_from_sql(self, sql: str) -> str:
        """Schema of the first schema-qualified table in the SQL, else the default"""
        match = re.search(r'FROM\s+[`"\[]?([\w\-]+)[`"\]]?\.[`"\[]?([\w\-]+)[`"\]]?(?:\.[`"\[]?([\w\-]+))?', sql, re.IGNORECASE)
        if match:
            # project.dataset.table -> dataset; schema.table -> schema
            return match.group(2) if match.group(3) else match.group(1)
        return self.default_schema

    def _convert_explore_source_to_query(
        self,
        explore_source: Dict,
        report_builder: ConversionReportBuilder,
        issues: List[str]
    ) -> OmniQuery:
        """
        Convert a Looker explore_source block to an Omni query

        lkml parses `column:` blocks as `columns` and `filters: [...]` as
        `filters__all`. A column without `field:` reads the explore field of the
        same name. The query's base_view is the name the explore's own fields
        go by (the base alias the topic declares), its topic the explore name.

        Raises:
            ValueError: for constructs that would change the query result
                (bind_filters, bind_all_filters, the same field selected twice,
                untranslatable filters, derived_column)
        """
        explore = explore_source.get('name', '')
        unsupported = []
        if explore_source.get('bind_filters__all') or explore_source.get('bind_filters'):
            unsupported.append("bind_filters")
        if str(explore_source.get('bind_all_filters', 'no')).lower() == 'yes':
            unsupported.append("bind_all_filters")
        if explore_source.get('derived_columns') or explore_source.get('derived_column'):
            unsupported.append("derived_column")
        if explore_source.get('expression_custom_filter'):
            unsupported.append("expression_custom_filter")

        fields: Dict[str, str] = {}
        columns = explore_source.get('columns') or explore_source.get('column') or []
        if isinstance(columns, dict):
            columns = [columns]
        for col in columns:
            alias = col.get('name')
            source = col.get('field') or (f"{explore}.{alias}" if alias else None)
            if not source:
                continue
            if source in fields:
                unsupported.append(f"field {source} selected twice ({fields[source]}, {alias})")
                continue
            fields[source] = alias or source.split('.')[-1]
        if not fields:
            unsupported.append("no columns")

        explore_info, _ = self._find_explore(explore)
        # names the explore uses -> underlying Looker views, for filter field types
        aliases = explore_info.view_names() if explore_info else {}
        aliases.setdefault(explore, explore_info.base_view if explore_info else explore)

        filters: Dict[str, Any] = {}
        for filter_field, expression in extract_measure_filters(explore_source):
            view, _, name = filter_field.partition('.')
            looker_type = self._resolve_cross_view_type(aliases.get(view, view), name) if name else None
            condition = translate_filter_expression(expression, filter_kind(looker_type)) if looker_type else None
            if condition is None:
                unsupported.append(f"filter {filter_field}: \"{expression}\"")
                continue
            filters[filter_field] = condition

        # The topic keeps the Looker names: its base_view is the explore's base
        # alias (declared in the topic's `views:` block when it is not the real
        # view), so field references stay as written in Looker
        base_view = explore_info.base_alias if explore_info else explore
        if explore_info is not None and explore_info.base_is_aliased:
            issues.append(f"explore_source explore '{explore}' names view '{explore_info.base_view}' "
                          f"'{base_view}'; the query keeps {base_view}.*, which topic {explore} declares "
                          f"(migrate the explore with --explore {explore})")

        # `explore: sales { view_name: orders }`: the fields go by `orders`
        def requalify(ref: str) -> str:
            return base_view + ref[len(explore):] if ref.startswith(explore + '.') else ref
        if base_view != explore:
            fields = {requalify(k): v for k, v in fields.items()}
            filters = {requalify(k): v for k, v in filters.items()}

        if unsupported:
            raise ValueError(
                f"{MANUAL}: native derived table uses constructs that are not converted: "
                + '; '.join(unsupported) + ". Migrate this view manually."
            )

        sorts = []
        for item in explore_source.get('sorts') or []:
            if isinstance(item, dict):
                for sort_field, direction in item.items():
                    sorts.append({'field': requalify(sort_field), 'desc': str(direction).lower() == 'desc'})

        limit = explore_source.get('limit')
        if explore_info is None:
            issues.append(f"explore_source explore '{explore}' not found; base_view assumed to be '{explore}'")

        return OmniQuery(
            fields=fields,
            base_view=base_view,
            topic=explore,
            filters=filters or None,
            sorts=sorts or None,
            limit=int(limit) if limit is not None else None
        )

    def _parse_table_name(self, sql_table_name: str) -> Tuple[str, Dict[str, Any]]:
        """
        Parse a Looker sql_table_name into (schema, {'table_name'[, 'catalog']})

        `schema.table`, `catalog.schema.table` (catalog kept as Omni
        `catalog`), and quoted forms such as `` `project.dataset.table` `` or
        `"schema"."table"`.

        Raises:
            ValueError: Liquid or ${...} references, or an unparseable name
        """
        text = sql_table_name.strip().rstrip(';').strip()
        if has_liquid(text) or '${' in text:
            raise ValueError(
                f"{MANUAL}: sql_table_name `{text}` uses Liquid or a ${{...}} reference; migrate this view manually"
            )
        parts: List[str] = []
        for token in re.findall(r'"[^"]*"|`[^`]*`|\[[^\]]*\]|[^.]+', text):
            if token.startswith('`'):
                parts.extend(token.strip('`').split('.'))
            else:
                parts.append(token.strip('"[]').strip())
        if not parts or not all(_IDENT_RE.fullmatch(p) for p in parts) or len(parts) > 3:
            raise ValueError(f"{MANUAL}: sql_table_name `{text}` could not be parsed; migrate this view manually")
        if len(parts) == 1:
            return self.default_schema, {'table_name': parts[0]}
        if len(parts) == 2:
            return parts[0], {'table_name': parts[1]}
        return parts[1], {'catalog': parts[0], 'table_name': parts[2]}

    def _format_grant_details(self, grant_names: List[str]) -> str:
        """Grant names with user_attribute and allowed_values from the registry"""
        lines = []
        for grant_name in grant_names:
            if self.access_grants and grant_name in self.access_grants:
                grant = self.access_grants[grant_name]
                values_str = ', '.join(f"`{v}`" for v in grant.allowed_values)
                lines.append(f"  - `{grant_name}`")
                lines.append(f"    - User Attribute: `{grant.user_attribute}`")
                lines.append(f"    - Allowed Values: {values_str}")
            else:
                lines.append(f"  - `{grant_name}` (definition not found - check model files)")
        return '\n'.join(lines)
