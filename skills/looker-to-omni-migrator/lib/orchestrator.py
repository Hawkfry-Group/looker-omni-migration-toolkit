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
- file_generators: YAML/Markdown output, merge, dry-run, atomic writes
- explore_analyzer: explore parsing and topic suggestions
- access_grant_parser: access grant registry

## View Migration Workflow (`migrate_view`)

1. **Load CSV scope** - rows for this owner, phase and view
2. **Parse the Looker view** - every CSV field must exist in it; missing
   fields are reported as MANUAL ACTION REQUIRED
3. **Convert fields** - every field of the view is converted. In-scope fields
   are written visible; other fields are written `hidden: true` with the tag
   `in_omni_in_looker_not_csv`. Out-of-scope fields referenced by in-scope
   fields (transitively) are written visible with the tag `auto_dependency`.
   A field whose references cannot be satisfied (an unconvertible or
   undefined dependency, or a circular reference) is not written and is
   reported. Field-level `required_access_grants` are copied onto every
   written field.
4. **Build the Omni view** - table view (`schema`, `table_name`, and
   `catalog` for 3-part names), SQL derived table (`sql:`) or native derived
   table (`query:`)
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
3. Carry `access_filter` -> `access_filters`, `sql_always_where` ->
   `always_where_sql`, `sql_always_having` -> `always_having_sql`,
   `required_access_grants`, `hidden` and `group_label`. Access-related
   items are also listed as MANUAL ACTION REQUIRED. Not carried, reported:
   `always_filter`, `conditionally_filter`, `always_join`, join
   `required_joins`, join `required_access_grants`, field restrictions.
4. Write `{explore}.topic.yaml` and `{explore}.relationships` (a plain YAML
   list; removed if it would be empty) to the output dir and
   `{explore}_explore.md` to the reports dir

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
from .context_loader import get_fields_for_view, load_migration_context
from .explore_analyzer import (
    ExploreInfo,
    JoinDefinition,
    find_explore_files,
    find_explores_using_views,
    generate_explore_report,
    parse_explore_file,
    suggest_topic_divisions,
)
from .file_generators import ConversionReportBuilder, FileGenerator, MergeError
from .looker_parser import LookerView, find_and_parse_view
from .omni_models import OmniDimension, OmniMeasure, OmniQuery, OmniTopic, OmniView
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
    translate_filter_expression,
)

PROVENANCE_TAG = 'in_omni_in_looker_not_csv'
AUTO_DEPENDENCY_TAG = 'auto_dependency'

# Looker join type -> Omni join_type (https://docs.omni.co/modeling/relationships/parameters/join-type)
JOIN_TYPE_MAP = {
    'left_outer': 'always_left',
    'inner': 'inner',
    'full_outer': 'full_outer',
    'cross': 'cross',
}
RELATIONSHIP_TYPES = {'many_to_one', 'one_to_one', 'one_to_many', 'many_to_many'}

_FIELD_REF_RE = re.compile(r'\$\{([a-zA-Z_][a-zA-Z0-9_]*)\.([a-zA-Z_][a-zA-Z0-9_]*)\}')
_IDENT_RE = re.compile(r'[A-Za-z_][A-Za-z0-9_\-]*')


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

        self._view_cache: Dict[str, Optional[LookerView]] = {}

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
                view=view_name
            )
            csv_fields = get_fields_for_view(context.csv_rows, view_name)
            print(f"      ✓ CSV fields in scope: {len(csv_fields)}")

            # ---------------- Step 2: parse ----------------
            print("\n[2/5] Parsing Looker view...")
            looker_view = find_and_parse_view(str(self.looker_repo), view_name)
            print(f"      ✓ Found: {looker_view.file_path}")
            print(f"      ✓ Dimensions: {len(looker_view.dimensions)}")
            print(f"      ✓ Dimension groups: {len(looker_view.dimension_groups)}")
            print(f"      ✓ Measures: {len(looker_view.measures)}")

            omni_view_name = view_name
            report_builder = ConversionReportBuilder(
                looker_view_name=view_name,
                omni_view_name=omni_view_name,
                phase=phase
            )

            kinds = {
                'dimension': looker_view.dimensions,
                'dimension_group': looker_view.dimension_groups,
                'measure': looker_view.measures,
            }
            scope = self._resolve_scope(csv_fields, kinds, looker_view, view_name, report_builder, issues)
            in_scope_names: Set[str] = set().union(*scope.values())

            self._flag_view_features(looker_view, report_builder, issues)

            # Fail fast on a source that cannot be converted
            schema, view_source = self._determine_view_source(looker_view, report_builder, issues)

            # ---------------- Step 3: convert ----------------
            print("\n[3/5] Applying conversion rules...")

            is_derived = bool(looker_view.derived_table)
            view_filename = f"{omni_view_name}.{'query.view' if is_derived else 'view.yaml'}"
            existing_view, existing_view_source = self._find_existing_view(omni_view_name, view_filename, is_derived)
            existing_fields = {}
            if existing_view:
                existing_fields.update(existing_view.get('dimensions') or {})
                existing_fields.update(existing_view.get('measures') or {})

            rule_applier = RuleApplier(
                phase=phase,
                verified=verified,
                view_name=view_name,
                canonical_mappings=self.config.canonical_mappings,
                column_fields=self._column_fields(looker_view, existing_fields),
                field_types=self._field_types(looker_view),
                type_resolver=self._resolve_cross_view_type
            )
            all_dimension_names = set(looker_view.dimensions) | set(looker_view.dimension_groups)
            all_measure_names = set(looker_view.measures)

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

            blocked = self._blocked_fields(results, set(existing_fields))

            # Out-of-scope fields needed by written in-scope fields
            auto_deps: Set[str] = set()
            queue = sorted(n for n in in_scope_names if n in results and n not in blocked
                           and results[n][1].omni_field)
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
                in_scope = name in in_scope_names
                if name in blocked and result.omni_field:
                    # Converted on its own, but a reference cannot be satisfied
                    result.omni_field = None
                    result.risk_level = "HIGH"
                    result.warnings.append(f"{MANUAL}: {blocked[name]} - not written")
                if in_scope or name in auto_deps:
                    self._add_result(report_builder, kind, name, result)
                if not result.omni_field:
                    reason = '; '.join(result.warnings)
                    if in_scope:
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

                target = omni_measures if kind == 'measure' else omni_dimensions
                target[name] = result.omni_field
                if result.db_column_name:
                    field_to_column_mapping[name] = result.db_column_name
                cross_view_refs |= result.cross_view_refs

            if cross_view_refs:
                views = sorted({ref.split('.')[0] for ref in cross_view_refs})
                report_builder.add_advanced_feature(
                    feature="Cross-View References",
                    status="INFO",
                    details="Fields reference other views (kept as ${view.field}): " + ', '.join(sorted(cross_view_refs)),
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
                'schema': schema,
                'folder': f"{schema}/{self.curated_folder}",
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
            if looker_view.is_extension:
                view_kwargs['template'] = True

            view = OmniView(**view_kwargs)
            print(f"      ✓ Schema: {schema}")
            print(f"      ✓ Dimensions: {len(omni_dimensions)}  Measures: {len(omni_measures)}")

            # ---------------- Step 5: render, validate, write ----------------
            print("\n[5/5] Generating files...")
            if is_derived:
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
            if not any(isinstance(d, dict) and d.get('primary_key') is True for d in final_dims.values()):
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

    def _resolve_scope(self, csv_fields, kinds, looker_view, view_name, report_builder, issues) -> Dict[str, Set[str]]:
        """Map CSV rows to Looker fields; report rows that do not match"""
        scope: Dict[str, Set[str]] = {kind: set() for kind in kinds}
        missing_rows = []
        for row in csv_fields:
            name, csv_type = row.get('field'), row.get('field_type')
            actual = next((k for k, fields in kinds.items() if name in fields), None)
            if actual is None and name in looker_view.parameters:
                issues.append(f"CSV field '{name}' is a Looker parameter; parameters are not migrated")
                report_builder.add_not_converted(name, f"{MANUAL}: Looker parameter - recreate as an Omni templated filter")
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
        return scope

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
        other = self._cached_view(view_name)
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
    def _blocked_fields(results: Dict[str, Tuple[str, ConversionResult]], existing_names: Set[str]) -> Dict[str, str]:
        """
        Fields that converted but cannot be written because a same-view
        reference cannot be satisfied: undefined, not convertible, or circular.
        Returns {field: reason}.
        """
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
                    reason = (f"references ${{{dep}}}, which is not defined in the view "
                              f"(dimension_group timeframes such as created_date must be rewritten for Omni)")
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

    def _flag_view_features(self, looker_view: LookerView, report_builder: ConversionReportBuilder, issues: List[str]):
        """Report view-level LookML features; access-related ones are also issues"""
        if looker_view.refinement_files:
            issues.append(f"View is refined in {', '.join(looker_view.refinement_files)}; refinements are not applied")
            report_builder.add_advanced_feature(
                feature="Refinements (view: +name)",
                status=MANUAL,
                details="Refinements found in: " + ', '.join(looker_view.refinement_files),
                action_required="Apply the refinement's changes (fields, access settings) to the Omni view by hand."
            )
        if looker_view.extends:
            issues.append(f"View extends {', '.join(looker_view.extends)}; inherited fields are not included")
            report_builder.add_advanced_feature(
                feature="View Inheritance (extends)",
                status=MANUAL,
                details=f"View extends: {', '.join(looker_view.extends)}. Only fields declared in this file were migrated.",
                action_required="Migrate the base view(s) and add the inherited fields this view needs."
            )
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
                details="View uses _user_attributes in Liquid templates",
                action_required="Rewrite the logic with Omni user attributes; fields using Liquid were not converted."
            )
        if looker_view.required_access_grants:
            issues.append(f"View-level required_access_grants not carried: {', '.join(looker_view.required_access_grants)}")
            report_builder.add_advanced_feature(
                feature="View-Level Access Grants",
                status=MANUAL,
                details=f"View requires access grants:\n{self._format_grant_details(looker_view.required_access_grants)}",
                action_required="Define the grants in the Omni model and add required_access_grants to the view or the topics that use it."
            )
        if looker_view.parameters:
            names = ', '.join(sorted(looker_view.parameters))
            issues.append(f"Parameters not migrated: {names}")
            report_builder.add_advanced_feature(
                feature="Parameters",
                status=MANUAL,
                details=f"Looker parameter(s) not migrated: {names}",
                action_required="Recreate as Omni templated filters if still needed."
            )
        if looker_view.drill_fields or looker_view.sets:
            report_builder.add_not_converted(
                'view drill_fields/sets',
                "Not migrated: view-level drill_fields and sets have no automatic Omni conversion"
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

    def migrate_explore(self, explore_name: str, topic_name: Optional[str] = None) -> MigrationResult:
        """
        Migrate a Looker explore to an Omni topic + relationships file

        Args:
            explore_name: Name of the Looker explore (e.g., "order_items")
            topic_name: Optional output name (defaults to explore_name)
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
            print(f"      ✓ Base view: {explore_info.base_view}")
            print(f"      ✓ Joins: {len(explore_info.joins)}")

            print("\n[2/4] Building Omni topic...")
            topic_name = topic_name or explore_name
            base_view = explore_info.base_view
            manual: List[Tuple[str, str]] = []  # (title, details) -> report + issues
            carried: List[str] = []

            if refinement_files:
                manual.append((
                    "Refinements (explore: +name) NOT applied",
                    "Refinements found in: " + ', '.join(refinement_files)
                ))
            if base_view != explore_name:
                manual.append((
                    "Aliased base view",
                    f"Explore `{explore_name}` uses view `{base_view}` (from:/view_name:). The topic's base_view is "
                    f"`{base_view}`; references to `${{{explore_name}.field}}` must be rewritten to `${{{base_view}.field}}`."
                ))

            relationships, join_tree, skipped = self._build_relationships(explore_info, manual)

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
                manual.append((
                    "always_filter (NOT carried)",
                    f"Looker always_filter: `{self._describe_filter_block(explore_info.always_filter)}`. "
                    f"Recreate with Omni `default_filters` or `always_where_filters` as appropriate."
                ))
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
            if explore_info.fields:
                manual.append((
                    "Explore field restriction (NOT carried)",
                    f"Looker `fields: {explore_info.fields}`. The topic exposes `all_views.*`; restrict `fields:` manually."
                ))
            for join in explore_info.joins:
                if join.fields:
                    manual.append((
                        f"Join field restriction on {join.view} (NOT carried)",
                        f"Looker `fields: {join.fields}`. Restrict the topic `fields:` manually."
                    ))
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
            if explore_info.extends:
                manual.append((
                    "Explore inheritance (extends)",
                    f"Explore extends {', '.join(explore_info.extends)}; inherited joins and settings were not merged."
                ))

            ai_context_lines = [
                f"This topic provides access to {explore_info.label or explore_name} data.",
                "",
                f"Base view: {base_view}",
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
                base_view=base_view,
                label=explore_info.label or explore_name.replace('_', ' ').title(),
                description=explore_info.description or f"Migrated from Looker explore: {explore_name}",
                fields=["all_views.*", "-tag:table_key"],
                joins=join_tree,
                ai_context='\n'.join(ai_context_lines),
                **topic_extra
            )
            print(f"      ✓ Topic: {topic_name} ({len(relationships)} join(s), {len(skipped)} skipped)")

            # Same view pair joined differently by another explore's relationships file
            relationships_path = self.omni_repo / f"{topic_name}.relationships"
            for conflict in self._relationship_conflicts(relationships, exclude=relationships_path):
                manual.append(("Conflicting relationship", conflict))

            report = self._build_explore_report(
                explore_name, topic_name, explore_info, relationships, skipped, carried, manual
            )

            # Render everything before writing anything
            topic_content = self.file_generator.render_topic_yaml(topic)
            relationships_content = None
            if relationships:
                header = [
                    f"Relationships migrated from Looker explore: {explore_name}",
                    f"Base view: {base_view}. Joins: {len(relationships)}.",
                ]
                if skipped:
                    header.append(f"Not migrated (see {topic_name}_explore.md): {', '.join(skipped)}")
                relationships_content = self.file_generator.render_relationships_yaml(relationships, header)

            print("\n[3/4] Writing topic and relationships files...")
            topic_path = self.file_generator.write_file(self.omni_repo / f"{topic_name}.topic.yaml", topic_content)
            print(f"      ✓ Topic: {topic_path}")

            written_relationships = None
            if relationships_content is not None:
                written_relationships = self.file_generator.write_file(relationships_path, relationships_content)
                print(f"      ✓ Relationships: {written_relationships}")
            else:
                # A previous run's file would keep joins that no longer exist
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

    def _find_explore(self, explore_name: str) -> Tuple[Optional[ExploreInfo], List[str]]:
        """
        The explore definition and any refinement files (`explore: +name`).

        Raises:
            ValueError: the explore is defined in more than one file
        """
        found: List[ExploreInfo] = []
        refinements: List[str] = []
        for file_path in find_explore_files(str(self.looker_repo)):
            try:
                explores = parse_explore_file(file_path)
            except Exception as e:
                print(f"      ⚠️  Could not parse {file_path}: {e}")
                continue
            for exp in explores:
                if exp.name == explore_name:
                    found.append(exp)
                elif exp.name == f"+{explore_name}":
                    refinements.append(file_path)
        if len(found) > 1:
            raise ValueError(
                f"Explore '{explore_name}' is defined more than once: "
                + ', '.join(sorted({e.file_path for e in found}))
                + ". Point --looker-repo at a single project."
            )
        return (found[0] if found else None), refinements

    def _build_relationships(
        self,
        explore_info: ExploreInfo,
        manual: List[Tuple[str, str]]
    ) -> Tuple[List[Dict[str, Any]], Dict, List[str]]:
        """
        Convert joins to Omni relationships and a nested topic join tree

        Each join hangs off the one joined view its sql_on references (besides
        itself); referencing only the base view puts it at the top level. The
        graph is resolved independently of declaration order. A join is
        skipped (and reported) when it cannot be converted, references an
        unknown view, references more than one other joined view, is part of
        a cycle, or hangs off a skipped join.
        """
        base_view = explore_info.base_view
        base_names = {base_view, explore_info.name}
        joins: Dict[str, JoinDefinition] = {}
        for join in explore_info.joins:
            joins[join.view] = join

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
                manual.append((
                    f"Aliased join {name}",
                    f"`join: {name} {{ from: {join.source_view} }}` became `join_to_view: {join.source_view}` "
                    f"with `join_to_view_as: {name}`. Verify field references to `{name}`."
                ))
            relationship.update({
                'join_type': JOIN_TYPE_MAP[join.type or 'left_outer'],
                'on_sql': join.sql_on,
                'relationship_type': join.relationship or 'many_to_one',
                'reversible': False,
            })
            relationships.append(relationship)
            if self._rename_candidates(join.sql_on):
                manual.append((
                    f"Join {name}: renamed fields",
                    "sql_on uses Looker fields that read a differently named column ("
                    + ', '.join(self._rename_candidates(join.sql_on))
                    + "). If those views were merged into existing Omni views, the fields are named after the column."
                ))

        # Nested join tree for the topic
        tree: Dict[str, Dict] = {}
        nodes: Dict[str, Dict] = {name: {} for name in joins if name not in skipped}
        for name in nodes:
            parent = parents[name]
            (nodes[parent] if parent is not None else tree)[name] = nodes[name]
        return relationships, tree, skipped

    def _rename_candidates(self, sql_on: str) -> List[str]:
        out = []
        for view_name, field_name in _FIELD_REF_RE.findall(sql_on):
            column = self._get_db_column_for_field(view_name, field_name)
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
        manual: List[Tuple[str, str]]
    ) -> str:
        lines = [
            f"# Explore Migration: {explore_name}",
            "",
            "## Summary",
            f"- **Explore**: {explore_name}",
            f"- **Source file**: `{explore_info.file_path}`",
            f"- **Base View**: {explore_info.base_view}",
            f"- **Topic**: {topic_name}",
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

        lines += ["## Join Definitions", ""]
        for join in explore_info.joins:
            status = "NOT migrated" if join.view in skipped else "migrated"
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
        lines.append(f"1. **{topic_name}.topic.yaml** - topic with field list and join tree")
        if relationships:
            lines.append(f"2. **{topic_name}.relationships** - join definitions (YAML list)")
        lines += [
            "",
            "## Notes",
            "",
            "- Topic uses `all_views.*` and excludes fields tagged `table_key`",
            "- Looker join types mapped: left_outer → always_left, inner → inner, "
            "full_outer → full_outer, cross → cross (other types are not migrated)",
            "- Relationships files are model-wide in Omni: check for conflicting joins between the same views",
            "- Cross-view references stay `${view.field}`",
            "",
        ]
        return '\n'.join(lines)

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
        if view_name not in self._view_cache:
            try:
                self._view_cache[view_name] = find_and_parse_view(str(self.looker_repo), view_name)
            except Exception:
                self._view_cache[view_name] = None
        return self._view_cache[view_name]

    def _get_db_column_for_field(self, view_name: str, field_name: str) -> Optional[str]:
        """Warehouse column a Looker dimension reads, if it differs from its name"""
        looker_view = self._cached_view(view_name)
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
        issues: List[str]
    ) -> Tuple[str, Dict[str, Any]]:
        """
        Build the view source: {'table_name'[, 'catalog']}, {'sql'} or {'query'}

        Returns:
            (schema, source_dict)

        Raises:
            ValueError: the source cannot be converted without guessing
        """
        if looker_view.derived_table:
            derived = looker_view.derived_table

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
                schema = self._extract_schema_from_sql(sql)
                if has_liquid(sql) or re.search(r'\$\{[^}]+\}', sql):
                    raise ValueError(
                        f"{MANUAL}: the derived table SQL contains Liquid or ${{...}} references "
                        f"(e.g. ${{other_view.SQL_TABLE_NAME}}); migrate this view manually"
                    )
                report_builder.add_advanced_feature(
                    feature="SQL Derived Table",
                    status="CONVERTED",
                    details="Converted to an Omni query view with a top-level sql: block.",
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

        if looker_view.sql_table_name:
            return self._parse_table_name(looker_view.sql_table_name)

        # Looker defaults the table to the view name
        issues.append("View has no sql_table_name or derived_table; using the view name as table")
        report_builder.add_advanced_feature(
            feature="Missing View Source",
            status=MANUAL,
            details="View has no sql_table_name or derived_table; Looker would use the view name as the table.",
            action_required="Confirm the table name and schema."
        )
        return self.default_schema, {'table_name': looker_view.view_name}

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
        same name. The query's base_view is the explore's base view (resolved
        from `from:`/`view_name:`), its topic the explore name.

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
        # explore/join aliases -> underlying Looker views, for filter field types
        aliases = {explore: explore_info.base_view if explore_info else explore}
        for join in (explore_info.joins if explore_info else []):
            aliases[join.view] = join.source_view

        filters: Dict[str, Any] = {}
        for filter_field, expression in extract_measure_filters(explore_source):
            view, _, name = filter_field.partition('.')
            looker_type = self._resolve_cross_view_type(aliases.get(view, view), name) if name else None
            condition = translate_filter_expression(expression, filter_kind(looker_type)) if looker_type else None
            if condition is None:
                unsupported.append(f"filter {filter_field}: \"{expression}\"")
                continue
            filters[filter_field] = condition

        # explore: sales { from: orders } -> Omni query fields are view-qualified
        # (orders.x); rewrite every sales.x consistently
        base_view = explore_info.base_view if explore_info else explore
        if base_view != explore:
            def requalify(ref: str) -> str:
                return base_view + ref[len(explore):] if ref.startswith(explore + '.') else ref
            fields = {requalify(k): v for k, v in fields.items()}
            filters = {requalify(k): v for k, v in filters.items()}
            issues.append(f"explore_source explore '{explore}' uses view '{base_view}'; "
                          f"field references rewritten from {explore}.* to {base_view}.*")
        else:
            def requalify(ref: str) -> str:
                return ref

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
