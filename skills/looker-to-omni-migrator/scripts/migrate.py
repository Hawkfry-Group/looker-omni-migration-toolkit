#!/usr/bin/env python3
"""
Looker to Omni Migration Tool - Main Entry Point

Run from the skill directory. Exactly one mode is required:

    # Migrate one view (fields in scope come from the CSV for this phase/owner)
    python scripts/migrate.py --view users --phase 0 --owner "Team A"

    # Preview: print the files that would be written, write nothing
    python scripts/migrate.py --view users --phase 0 --owner "Team A" --dry-run

    # Migrate an explore to an Omni topic + relationships file
    python scripts/migrate.py --explore order_items

    # List the access grants defined in the Looker project
    python scripts/migrate.py --parse-access-grants

Defaults:
    --looker-repo   ./looker
    --omni-repo     ./migration/staging/views   (explore mode: ./migration/staging/topics)
    reports         sibling reports/ dir of --omni-repo (./migration/staging/reports)
    --csv           migration/views.csv (relative to the current dir, else the skill dir)

Outputs:
    view mode     {view}.view.yaml, or {view}.query.view for derived tables,
                  in --omni-repo; {view}.migration.md in the reports dir
    explore mode  {explore}.topic.yaml and {explore}.relationships in
                  --omni-repo; {explore}_explore.md in the reports dir
    grants mode   access_grants.md in the reports dir

Exit codes:
    0  success; items needing manual action are printed and written to the report
    1  runtime/input error (repo, CSV or view not found, invalid config, a source
       or merge that cannot be converted safely). These errors are detected
       before the view's or explore's files are written, so nothing is written
       for it. Exceptions: an I/O error while writing (each file is replaced
       atomically, but files written earlier in the run stay), and a failing
       --explore-analysis, which runs after the view's files are written.
    2  invalid command-line arguments
"""

import argparse
import sys
from pathlib import Path

# Keep the guarantee that a run (and --dry-run in particular) writes nothing
# outside the output/report dirs: no __pycache__ in the skill directory
sys.dont_write_bytecode = True

# Add skill root to path so lib/ imports work from any working directory
_skill_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_skill_root))

from lib.config import MigrationConfig  # noqa: E402
from lib.orchestrator import MigrationOrchestrator  # noqa: E402

DEFAULT_VIEWS_DIR = Path('migration/staging/views')
DEFAULT_TOPICS_DIR = Path('migration/staging/topics')


def parse_phase(value: str) -> int:
    """Accept 0, 1, ... or P0, P1, ..."""
    text = str(value).strip().upper()
    if text.startswith('P'):
        text = text[1:]
    if not text.isdigit():
        raise argparse.ArgumentTypeError(f"invalid phase '{value}' (use e.g. 0 or P0)")
    return int(text)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='scripts/migrate.py',
        description="Migrate Looker views and explores to Omni YAML",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python scripts/migrate.py --view users --phase 0 --owner "Team A"
  python scripts/migrate.py --view users --phase 0 --owner "Team A" --dry-run
  python scripts/migrate.py --explore order_items
  python scripts/migrate.py --parse-access-grants
        """
    )

    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--view', type=str, help='Looker view to migrate (requires --phase and --owner)')
    mode.add_argument('--explore', type=str, help='Looker explore to migrate to an Omni topic')
    mode.add_argument('--parse-access-grants', action='store_true',
                      help='List the access grants defined in the Looker project and write access_grants.md')

    parser.add_argument('--phase', type=parse_phase,
                        help='Migration phase to select from the CSV (0 or P0). Required for --view')
    parser.add_argument('--owner', type=str,
                        help='CSV owner value to select (exact match). Required for --view')

    parser.add_argument('--looker-repo', type=str, default='./looker',
                        help='Looker project root; searched recursively for .lkml files (default: ./looker)')
    parser.add_argument('--omni-repo', type=str, default=None,
                        help=f'Output dir for generated Omni files (default: ./{DEFAULT_VIEWS_DIR}, '
                             f'or ./{DEFAULT_TOPICS_DIR} with --explore). Reports go to its sibling reports/ dir')
    parser.add_argument('--omni-source-repo', type=str, default=None,
                        help='Existing Omni views to merge Looker metadata into (e.g. ./omni/<model>/<schema>), '
                             'searched recursively for {view}.view.yaml / {view}.view / {view}.query.view. '
                             'Without it, a file of the same name already in --omni-repo (from an earlier run) '
                             'is merged into; otherwise the view is generated from scratch')
    parser.add_argument('--config', type=str, default=None,
                        help='YAML config (default_schema, curated_folder, canonical_mappings). '
                             'See config/migration_config.example.yaml')
    parser.add_argument('--default-schema', type=str, default=None,
                        help='Schema to use when it cannot be read from sql_table_name (overrides --config)')
    parser.add_argument('--curated-folder', type=str, default=None,
                        help='Folder under the schema written to `folder:` (overrides --config)')
    parser.add_argument('--csv', type=str, default='migration/views.csv',
                        help='Scope CSV (default: migration/views.csv; sample: examples/migration.csv)')

    parser.add_argument('--unverified', action='store_true',
                        help='Tag output `unverified` instead of `verified`')
    parser.add_argument('--dry-run', action='store_true',
                        help='Print the files that would be written; write nothing')
    parser.add_argument('--explore-analysis', action='store_true',
                        help='With --view: also write {view}_explore_analysis.md for explores using the view')
    return parser


def resolve_csv(csv_arg: str) -> Path:
    """Absolute path as-is; relative path: current dir first, then the skill dir"""
    path = Path(csv_arg)
    if path.is_absolute() or path.exists():
        return path.resolve()
    return _skill_root / csv_arg


def run_parse_access_grants(looker_repo: Path, reports_dir: Path, dry_run: bool) -> int:
    from lib.access_grant_parser import find_access_grants_files, load_access_grants

    print(f"\n{'='*60}\nPARSING ACCESS GRANTS\n{'='*60}\n")
    files = find_access_grants_files(str(looker_repo))
    if not files:
        print(f"❌ No access_grant definitions found in {looker_repo}")
        return 1

    print(f"Found {len(files)} file(s) with access grants:")
    for f in files:
        print(f"  - {f}")
    print()

    try:
        grants = load_access_grants(str(looker_repo)) or {}
    except ValueError as e:
        print(f"❌ Error: {e}")
        return 1
    if not grants:
        print("❌ No access grants could be parsed")
        return 1

    print(f"{'='*60}\nPARSED {len(grants)} ACCESS GRANTS\n{'='*60}\n")
    for grant_name, grant in sorted(grants.items()):
        print(f"📋 {grant_name}")
        print(f"   User Attribute: {grant.user_attribute}")
        values = ', '.join(f'"{v}"' for v in grant.allowed_values) if grant.allowed_values else '(empty)'
        print(f"   Allowed Values: [{values}]")
        print()

    lines = [
        "# Looker Access Grants — Migration Reference",
        "",
        f"**Total grants:** {len(grants)}",
        "**Source files:** " + ', '.join(f"`{f}`" for f in files),
        "",
        "## Grants",
        "",
        "| Grant Name | User Attribute | Allowed Values | Omni Action |",
        "|------------|---------------|----------------|-------------|",
    ]
    for grant_name, grant in sorted(grants.items()):
        values = ', '.join(f'`{v}`' for v in grant.allowed_values) if grant.allowed_values else '(empty)'
        lines.append(f"| `{grant_name}` | `{grant.user_attribute}` | {values} | Define in the Omni model `access_grants:` |")
    lines += [
        "",
        "## Omni Setup Required",
        "",
        "For each grant:",
        "1. Create the user attribute in Omni and assign values to users/groups",
        "2. Define the grant in the Omni model file under `access_grants:` (user_attribute + allowed_values)",
        "3. Explore migration copies explore-level `required_access_grants` onto the topic, and view",
        "   migration copies field-level `required_access_grants` onto the fields; both need step 2",
        "",
    ]
    report_path = reports_dir / 'access_grants.md'
    content = '\n'.join(lines)
    if dry_run:
        print(f"[dry-run] would write {report_path} ({len(content.encode())} bytes)")
    else:
        reports_dir.mkdir(parents=True, exist_ok=True)
        report_path.write_text(content)
        print(f"✅ Report saved: {report_path}")
    return 0


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.view and (args.phase is None or not args.owner):
        parser.error("--view requires both --phase and --owner")
    if args.explore_analysis and not args.view:
        parser.error("--explore-analysis can only be used with --view")

    looker_repo = Path(args.looker_repo)
    if not looker_repo.exists():
        print(f"❌ Error: Looker repository not found: {looker_repo}")
        return 1

    if args.omni_repo:
        omni_repo = Path(args.omni_repo)
    else:
        omni_repo = DEFAULT_TOPICS_DIR if args.explore else DEFAULT_VIEWS_DIR
    reports_dir = omni_repo.parent / 'reports'

    if args.parse_access_grants:
        return run_parse_access_grants(looker_repo, reports_dir, args.dry_run)

    try:
        migration_config = MigrationConfig.load(Path(args.config) if args.config else None)
    except (FileNotFoundError, ValueError) as e:
        print(f"❌ Error: {e}")
        return 1
    if args.default_schema:
        migration_config.default_schema = args.default_schema
    if args.curated_folder:
        migration_config.curated_folder = args.curated_folder

    csv_path = None
    if args.view:
        csv_path = resolve_csv(args.csv)
        if not csv_path.exists():
            print(f"❌ Error: CSV file not found: {csv_path}")
            return 1

    omni_source_repo = None
    if args.omni_source_repo:
        omni_source_repo = Path(args.omni_source_repo)
        if not omni_source_repo.exists():
            print(f"❌ Error: --omni-source-repo not found: {omni_source_repo}")
            return 1

    verified = not args.unverified

    print(f"\n{'='*60}")
    print("LOOKER TO OMNI MIGRATION")
    print(f"{'='*60}")
    print(f"Looker Repo: {looker_repo}")
    print(f"Omni Output: {omni_repo}")
    print(f"Reports:     {reports_dir}")
    print(f"Omni Source: {omni_source_repo or '(none) - merges into an existing output file if present, else from scratch'}")
    if args.view:
        print(f"CSV Scope:   {csv_path}")
        print(f"Owner:       {args.owner}")
        print(f"Phase:       {args.phase}")
        print(f"Verified:    {verified}")
    if migration_config.canonical_mappings:
        print(f"Canonical:   {migration_config.canonical_mappings}")
    if args.dry_run:
        print("Mode:        DRY RUN (nothing will be written)")
    print(f"{'='*60}\n")

    try:
        orchestrator = MigrationOrchestrator(
            skill_path=_skill_root,
            looker_repo=looker_repo,
            omni_repo=omni_repo,
            csv_path=csv_path,
            omni_source_repo=omni_source_repo,
            config=migration_config,
            reports_dir=reports_dir,
            dry_run=args.dry_run
        )
    except ValueError as e:
        print(f"❌ Error: {e}")
        return 1

    if args.view:
        result = orchestrator.migrate_view(
            view_name=args.view,
            phase=args.phase,
            owner=args.owner,
            verified=verified
        )
        if not result.success:
            print(f"\n❌ Migration failed: {result.error_message}")
            return 1
        print(f"{'Planned' if args.dry_run else 'Generated'} files:")
        print(f"   View:   {result.view_file_path}")
        print(f"   Report: {result.report_file_path}")

        if args.explore_analysis:
            print(f"\n{'='*60}\nANALYZING EXPLORES\n{'='*60}\n")
            try:
                analysis_path = orchestrator.analyze_explores_for_view(args.view)
            except Exception as e:
                print(f"❌ Explore analysis failed (the view's files were already written): {e}")
                return 1
            if analysis_path:
                print(f"   Explore analysis: {analysis_path}")
            else:
                print(f"⚠️  No explores found using {args.view}")
    else:
        result = orchestrator.migrate_explore(explore_name=args.explore)
        if not result.success:
            print(f"\n❌ Explore migration failed: {result.error_message}")
            return 1
        print(f"{'Planned' if args.dry_run else 'Generated'} files:")
        print(f"   Topic:         {result.topic_file_path}")
        if result.relationships_file_path:
            print(f"   Relationships: {result.relationships_file_path}")
        print(f"   Report:        {result.report_file_path}")

    if args.dry_run:
        print("\nDry run: no files were written.")
    return 0


if __name__ == '__main__':
    sys.exit(main())
