# Code Standards

Guidelines for changing or extending the migrator's Python code.

## Layout

```
scripts/migrate.py           # CLI entry point; argument parsing only
lib/
├── orchestrator.py          # Coordinates a view or explore migration
├── looker_parser.py         # LookML parsing (lkml)
├── explore_analyzer.py      # Explore parsing and topic suggestions
├── access_grant_parser.py   # access_grant definitions
├── rule_applier.py          # Looker → Omni field conversion
├── file_generators.py       # YAML and report output
├── context_loader.py        # CSV scope and rule files
├── config.py                # Defaults and --config loading
└── omni_models.py           # Pydantic models for Omni output
tests/                       # pytest suite
```

Keep each module to its job: parsing in parsers, conversion in `rule_applier.py`, writing in `file_generators.py`.

## Style

- Type hints on every function.
- A docstring on every public function: what it does, arguments, return value.
- Comments only where the logic is not obvious.
- Naming: `PascalCase` classes, `snake_case` functions, `UPPER_SNAKE_CASE` constants, leading underscore for private helpers.
- Import order: standard library, third-party, local (package-relative, for example `from .omni_models import OmniView`).
- Raise specific exceptions (`FileNotFoundError`, `ValueError`), not bare `Exception`.

## Behaviour rules

- `--dry-run` must not create, change or delete any file or directory, including reports (the CLI also disables bytecode caching).
- Exit codes: 0 success (manual items allowed), 1 input/runtime error, 2 argument error.
- Render and validate a view completely before writing anything for it.
- Never drop a Looker construct silently. If it is not converted, add it to the report with a reason.
- Keep organisation-specific vocabulary out of code. Canonical mappings and schema names come from `--config`.
- Validate output through the Pydantic models in `omni_models.py` before writing.

## YAML output

- Use `ruamel.yaml` for round-trip formatting; set a wide line width so SQL is not wrapped.
- Format SQL with `sqlparse` and write it as a literal block (`|-`).
- Write tags inline: `tags: [mig_phase_0, verified]`.
- Preserve unknown top-level keys when merging into an existing Omni view.

## Tests

- One `tests/test_<module>.py` per module; descriptive names such as `test_convert_dimension_omits_standard_sql`.
- Tests must not write outside a temporary directory.
- Use the thelook fixtures in `examples/` for end-to-end cases.
- Run from the skill directory: `pip install -r requirements.txt && pytest`.

## When adding a feature

1. Implement it with type hints, docstrings and tests.
2. Update `rules/migration_rules.md` if conversion behaviour changes.
3. Update `SKILL.md` and `README.md` if user-facing behaviour or flags change.
4. Add or update an example in `examples/` if useful.
