"""
Configuration - Organization-specific settings for the migrator

Everything in this module is a setting that varies between organizations:
warehouse schema names, the curated folder layout, and the canonical term
mappings used to rename business concepts during migration.

Defaults are deliberately generic so the toolkit works out of the box on any
Looker/Omni pair. Override them in one of three ways, in order of precedence:

1. CLI flags: `--default-schema`, `--curated-folder`, `--config`
2. A YAML config file (see `config/migration_config.example.yaml`)
3. The defaults below

## Example config file

```yaml
# migration_config.yaml
default_schema: thelook_ecommerce
curated_folder: curated

canonical_mappings:
  sku: product
```

## Canonical mappings

Canonical mappings rename business terms in the labels the migrator writes
(view labels and field labels), matched as whole words, case-insensitively.
When a field name contains the new term (e.g. `product_id`), the same name
spelled with the old term (`sku_id`) is added as a synonym. Field names, SQL, descriptions and ai_context are NOT rewritten.

They are EMPTY by default, because the right mappings are specific to your
business vocabulary, and they are only ever loaded from a `--config` file.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional

import yaml


# Fallback schema used when a view's source schema cannot be inferred from
# its `sql_table_name`. Override with --default-schema.
DEFAULT_SCHEMA = "analytics"

# Folder (relative to the schema) where curated view files are written.
# Produces `folder: <schema>/<curated_folder>` in generated YAML.
DEFAULT_CURATED_FOLDER = "curated"

# Canonical term mappings: {looker_term: omni_term}.
# Empty by default - see the module docstring.
DEFAULT_CANONICAL_MAPPINGS: Dict[str, str] = {}


@dataclass
class MigrationConfig:
    """Organization-specific migration settings"""

    default_schema: str = DEFAULT_SCHEMA
    curated_folder: str = DEFAULT_CURATED_FOLDER
    canonical_mappings: Dict[str, str] = field(
        default_factory=lambda: dict(DEFAULT_CANONICAL_MAPPINGS)
    )

    @classmethod
    def load(cls, config_path: Optional[Path] = None) -> "MigrationConfig":
        """
        Load configuration from a YAML file.

        Args:
            config_path: Path to a YAML config file. If None (no --config),
                         the built-in defaults are used. Settings absent
                         from the file keep their defaults.

        Returns:
            MigrationConfig instance

        Raises:
            FileNotFoundError: config_path is given but does not exist
            ValueError: the file cannot be read, is not valid YAML, or is not
                a mapping
        """
        config = cls()

        if not config_path:
            return config

        config_path = Path(config_path)
        if not config_path.exists():
            raise FileNotFoundError(f"Config file not found: {config_path}")

        try:
            with open(config_path, "r") as f:
                data = yaml.safe_load(f) or {}
        except (OSError, yaml.YAMLError) as e:
            raise ValueError(f"Config file {config_path} could not be read: {e}")

        if not isinstance(data, dict):
            raise ValueError(f"Config file must contain a YAML mapping: {config_path}")

        if "default_schema" in data:
            config.default_schema = str(data["default_schema"])

        if "curated_folder" in data:
            config.curated_folder = str(data["curated_folder"])

        if "canonical_mappings" in data:
            mappings = data["canonical_mappings"] or {}
            if not isinstance(mappings, dict):
                raise ValueError(
                    f"canonical_mappings must be a mapping of term: replacement "
                    f"in {config_path}"
                )
            config.canonical_mappings = {str(k): str(v) for k, v in mappings.items()}

        return config
