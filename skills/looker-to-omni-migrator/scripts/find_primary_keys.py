#!/usr/bin/env python3
"""
Primary key finder for Omni views (optional, separate from the converter)

A view needs one dimension marked `primary_key: true`, or aggregates fan out
across joins. This script looks at view files that have no primary key, tests
candidate columns against a SAMPLE of the real data through Omni's query API
(using the Omni CLI, which must be installed and configured), and reports
what it finds. With --apply it writes the key into the view file.

    # Dry run over every generated view (writes only the report)
    python scripts/find_primary_keys.py --model-id <model-id> --omni-repo migration/staging/views

    # One view, on a branch, and write the key
    python scripts/find_primary_keys.py --model-id <model-id> --branch-id <branch-id> \\
        --view orders --apply

A sample can prove a column is NOT unique. It can only suggest that it is.
A key is chosen only when exactly one column is unique in the sample. When
several are, nothing is written until you choose with --key VIEW=DIMENSION.

Exit codes:
    0  the run completed (some views may have no key, or several candidates to choose from)
    1  an input or connection error stopped the run
    2  invalid command-line arguments
"""

import argparse
import json
import sys
from pathlib import Path

sys.dont_write_bytecode = True

_skill_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_skill_root))

from lib import primary_keys as pk  # noqa: E402

DEFAULT_VIEWS_DIR = Path('migration/staging/views')


def positive_int(value: str) -> int:
    try:
        number = int(str(value).replace('_', '').replace(',', ''))
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid whole number '{value}'")
    if number < 1:
        raise argparse.ArgumentTypeError(f"must be 1 or more, got '{value}'")
    return number


def non_negative_int(value: str) -> int:
    try:
        number = int(str(value).replace('_', '').replace(',', ''))
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid whole number '{value}'")
    if number < 0:
        raise argparse.ArgumentTypeError(f"must be 0 or more, got '{value}'")
    return number


def identifier(value: str) -> str:
    if not pk.is_valid_identifier(value):
        raise argparse.ArgumentTypeError(
            f"invalid column name '{value}' (letters, digits, underscore and dollar only)")
    return value


def key_choice(value: str):
    """VIEW=DIMENSION"""
    view, separator, dimension = str(value).partition('=')
    view, dimension = view.strip(), dimension.strip()
    if not separator or not view or not dimension:
        raise argparse.ArgumentTypeError(f"invalid --key '{value}' (use VIEW=DIMENSION, for example orders=order_id)")
    return view, dimension


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='scripts/find_primary_keys.py',
        description="Find primary keys for Omni views by testing candidate columns on a sample of the data",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python scripts/find_primary_keys.py --model-id MODEL_ID
  python scripts/find_primary_keys.py --model-id MODEL_ID --view orders --view users
  python scripts/find_primary_keys.py --model-id MODEL_ID --date-column created_at --window-days 7
  python scripts/find_primary_keys.py --model-id MODEL_ID --branch-id BRANCH_ID --apply
  python scripts/find_primary_keys.py --model-id MODEL_ID --key sales=transaction_id --apply
        """
    )
    parser.add_argument('--model-id', required=True, help='Omni model id the SQL runs against')
    parser.add_argument('--branch-id', default=None, help='Omni branch id (optional)')
    parser.add_argument('--omni-repo', default=None,
                        help=f'Directory of Omni view files, searched recursively (default: ./{DEFAULT_VIEWS_DIR})')
    parser.add_argument('--view', action='append', default=None, metavar='NAME',
                        help='Only examine this view (file name without .view.yaml). Repeatable')
    parser.add_argument('--key', action='append', default=None, type=key_choice, metavar='VIEW=DIMENSION',
                        help='Your choice of key for a view where several columns are unique in the sample. '
                             'Used only if that dimension is unique and not null in the sample. '
                             'Written only with --apply. Repeatable')
    parser.add_argument('--recheck', action='store_true',
                        help='Also test views that already have a primary key (the key is reported, never replaced)')

    sampling = parser.add_argument_group('sampling')
    sampling.add_argument('--sample-rows', type=positive_int, default=1_000_000,
                          help='Target number of rows to sample per view (default: 1000000)')
    sampling.add_argument('--date-column', type=identifier, default=None, metavar='NAME',
                          help='Sample a recent window on this column instead of the plan chosen for the warehouse')
    sampling.add_argument('--window-days', type=positive_int, default=None, metavar='N',
                          help='Days in the date window (default: 30). Used with --date-column, '
                               'and for the Redshift sort key window')
    sampling.add_argument('--max-unpartitioned-rows', type=non_negative_int, default=5_000_000, metavar='N',
                          help='BigQuery: do not scan an unpartitioned table with more rows than this '
                               '(default: 5000000)')
    sampling.add_argument('--allow-full-scan', action='store_true',
                          help='BigQuery: scan unpartitioned tables whatever their size')
    sampling.add_argument('--min-rows', type=non_negative_int, default=1000, metavar='N',
                          help='Smallest sample that can qualify a key (default: 1000)')
    sampling.add_argument('--max-candidates', type=positive_int, default=12, metavar='N',
                          help='Most columns tested per view (default: 12)')
    sampling.add_argument('--max-pairs', type=non_negative_int, default=10, metavar='N',
                          help='Most column pairs tested when no single column is unique (default: 10)')

    omni = parser.add_argument_group('Omni CLI')
    omni.add_argument('--profile', default=None, help='Omni CLI profile (passed as -p)')
    omni.add_argument('--base-url', default=None, help='Omni API base URL (passed as --base-url)')
    omni.add_argument('--dialect', default=None,
                      help='Warehouse dialect (bigquery, snowflake, redshift, postgres, duckdb, motherduck). '
                           'Default: read from the model\'s connection')

    output = parser.add_argument_group('output')
    output.add_argument('--apply', action='store_true',
                        help='Write primary_key: true into the view files (default: dry run, report only)')
    output.add_argument('--report', default='primary_keys.md', metavar='PATH',
                        help='Markdown report path (default: primary_keys.md)')
    output.add_argument('--json', default=None, metavar='PATH', help='Also write a machine-readable JSON report')
    return parser


def select_view_files(omni_repo: Path, names):
    """(files, error). With names, only those views; every name must exist."""
    files = pk.discover_view_files(omni_repo)
    if not files:
        return [], f"no Omni view files (*.view.yaml, *.view, *.query.view) found under {omni_repo}"
    if not names:
        return files, None
    wanted = list(dict.fromkeys(names))
    selected = [f for f in files if pk.view_name_from_path(f) in wanted]
    missing = [n for n in wanted if n not in {pk.view_name_from_path(f) for f in selected}]
    if missing:
        return [], f"view(s) not found under {omni_repo}: {', '.join(missing)}"
    return selected, None


def main(argv=None, client=None) -> int:
    """client: an object with run_sql(sql) and dialect(); tests pass a fake. Default: the Omni CLI."""
    parser = build_parser()
    args = parser.parse_args(argv)

    keys = {}
    for view_name, dimension in args.key or []:
        if keys.get(view_name, dimension) != dimension:
            parser.error(f"--key given twice for view {view_name} with different dimensions")
        keys[view_name] = dimension

    omni_repo = Path(args.omni_repo) if args.omni_repo else DEFAULT_VIEWS_DIR
    if not omni_repo.is_dir():
        print(f"Error: --omni-repo directory not found: {omni_repo}")
        return 1
    files, error = select_view_files(omni_repo, args.view)
    if error:
        print(f"Error: {error}")
        return 1

    if client is None:
        client = pk.OmniQueryClient(model_id=args.model_id, branch_id=args.branch_id,
                                    profile=args.profile, base_url=args.base_url)

    views = [pk.load_view(path) for path in files]
    unknown_keys = sorted(set(keys) - {v.name for v in views})
    if unknown_keys:
        print(f"Error: --key names view(s) that are not in this run: {', '.join(unknown_keys)}")
        return 1
    # Foreign key advice looks at every view in the repo, not only the ones selected with --view
    selected = {v.path: v for v in views}
    all_views = [selected.get(path) or pk.load_view(path) for path in pk.discover_view_files(omni_repo)]
    needs_queries = any(v.skip_reason is None and (args.recheck or not v.existing_key) for v in views)

    dialect_name = args.dialect.lower() if args.dialect else None
    if dialect_name is None and needs_queries:
        try:
            dialect_name = client.dialect()
        except pk.OmniQueryError as e:
            print(f"Error: {e}")
            return 1
    dialect, known = pk.get_dialect(dialect_name)
    dialect_label = dialect_name or 'not needed'

    options = pk.FinderOptions(
        sample=pk.SampleOptions(
            sample_rows=args.sample_rows,
            date_column=args.date_column,
            window_days=args.window_days or 30,
            max_unpartitioned_rows=args.max_unpartitioned_rows,
            allow_full_scan=args.allow_full_scan,
        ),
        min_rows=args.min_rows,
        max_candidates=args.max_candidates,
        max_pairs=args.max_pairs,
        recheck=args.recheck,
        keys=keys,
    )

    print(f"\n{'='*60}\nPRIMARY KEY FINDER\n{'='*60}")
    print(f"Omni views:  {omni_repo} ({len(files)} file(s))")
    print(f"Model:       {args.model_id}" + (f" (branch {args.branch_id})" if args.branch_id else ""))
    print(f"Dialect:     {dialect_label}")
    print(f"Sample:      {args.sample_rows:,} rows target, at least {args.min_rows:,} to accept a key")
    if args.date_column:
        print(f"Override:    last {options.sample.window_days} day(s) on {args.date_column}")
    print("Mode:        " + ("APPLY (keys will be written)" if args.apply else "DRY RUN (no view file is changed)"))
    print(f"{'='*60}\n")
    if needs_queries and not known:
        print(f"Warning: unknown dialect '{dialect_label}'. Using a plain LIMIT sample with double-quoted "
              "names; check the cost of that on your warehouse.\n")
    if args.window_days and not args.date_column and dialect.name != 'redshift':
        print("Warning: --window-days has no effect without --date-column on this dialect.\n")

    finder = pk.PrimaryKeyFinder(client, dialect, options, dialect_name=dialect_label,
                                 foreign_key_hints=pk.ForeignKeyHints(all_views))
    results = []
    for index, view in enumerate(views, 1):
        try:
            result = finder.examine(view)
        except pk.OmniConnectionError as e:
            print(f"\nError: {e}")
            print("The run stopped. No report was written.")
            return 1
        if (args.apply and result.status == pk.STATUS_KEY_FOUND and result.chosen_key
                and not view.existing_key and not result.write_blocked):
            try:
                pk.apply_primary_key(view.path, result.chosen_key)
                result.applied = True
            except (OSError, ValueError) as e:
                result.message += f". NOT written: {e}"
        results.append(result)
        print(f"[{index}/{len(views)}] {view.name}: {result.status}"
              + (f" ({result.chosen_key})" if result.chosen_key else ""))
        print(f"      {result.message}")
        if result.applied:
            print(f"      wrote primary_key: true on {result.chosen_key} in {view.path}")

    report = pk.render_report(results, args.model_id, dialect_label, args.apply, args.branch_id)
    try:
        report_path = Path(args.report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(report)
        if args.json:
            json_path = Path(args.json)
            json_path.parent.mkdir(parents=True, exist_ok=True)
            json_path.write_text(json.dumps(
                pk.results_to_json(results, args.model_id, dialect_label, args.apply, args.branch_id), indent=2) + '\n')
    except OSError as e:
        print(f"Error: could not write the report: {e}")
        return 1

    counts = pk.count_by_status(results)
    print(f"\n{'='*60}\nSUMMARY\n{'='*60}")
    for status in pk.STATUS_ORDER:
        if counts[status]:
            print(f"  {status:<20} {counts[status]}")
    written = sum(1 for r in results if r.applied)
    print(f"  {'keys written':<20} {written}" + ("" if args.apply else " (dry run; use --apply to write)"))
    if counts[pk.STATUS_MULTIPLE_CANDIDATES]:
        print("\nSeveral columns were unique in the sample for some views, so no key was chosen for them.")
        print("Read the report, then choose with --key VIEW=DIMENSION.")
    print(f"\nReport: {report_path}")
    if args.json:
        print(f"JSON:   {args.json}")
    print("\nNote: keys were tested on a sample. A sample can prove a column is NOT unique;")
    print("it can only suggest that a column is unique. Review each key before relying on it.")
    return 0


if __name__ == '__main__':
    sys.exit(main())
