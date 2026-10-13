"""
Pydantic models for Omni YAML structures

Provides type safety, validation, and serialization for Omni views, topics, and reports.
"""

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from typing import List, Optional, Dict, Literal, Any
from enum import Enum


class AggregateType(str, Enum):
    """Supported Omni aggregate types"""
    SUM = "sum"
    COUNT = "count"
    COUNT_DISTINCT = "count_distinct"
    AVERAGE = "average"
    MIN = "min"
    MAX = "max"
    LIST = "list"
    MEDIAN = "median"
    PERCENTILE = "percentile"
    SUM_DISTINCT_ON = "sum_distinct_on"
    AVERAGE_DISTINCT_ON = "average_distinct_on"
    PERCENTILE_DISTINCT_ON = "percentile_distinct_on"
    MEDIAN_DISTINCT_ON = "median_distinct_on"


class OmniDimension(BaseModel):
    """
    Omni dimension definition

    CRITICAL RULES:
    - NO 'type' parameter (Omni infers from SQL)
    - NO 'html' parameter
    - NO 'datatype' parameter (except for date dimensions with timeframes)
    - Use direct column names in sql: (not ${TABLE}.column)
    - Use ${field_name} only for cross-field references
    - sql is OPTIONAL - omit when field name matches schema column (Omni infers)
    """
    label: str = Field(..., description="Human-friendly label")
    description: str = Field(..., description="Concise description (1-2 sentences)")
    tags: List[str] = Field(..., description="Tags including domain, type, mig_phase_X, verified (snake_case)")

    # Optional sql - omit when field name matches schema column
    sql: Optional[str] = Field(None, description="SQL expression (optional if field name matches schema)")

    # Optional fields
    primary_key: Optional[bool] = Field(None, description="True if this is the primary key")
    hidden: Optional[bool] = Field(None, description="True if hidden from UI")
    timeframes: Optional[List[str]] = Field(None, description="For date dimensions: [raw, date, week, month, ...]")
    datatype: Optional[Literal["timestamp", "date"]] = Field(None, description="Only for date dimensions with timeframes")
    ai_context: Optional[str] = Field(None, description="Detailed context for AI")
    synonyms: Optional[List[str]] = Field(None, description="Alternative terms")
    sample_values: Optional[List[Any]] = Field(None, description="Example values")
    all_values: Optional[List[Any]] = Field(None, description="Complete list of possible values")
    group_label: Optional[str] = Field(None, description="UI grouping label")
    format: Optional[str] = Field(None, description="Display format (number, currency_2, percent_1, etc)")
    filters: Optional[Dict[str, Any]] = Field(None, description="Filter conditions")
    required_access_grants: Optional[List[str]] = Field(None, description="Access grants required to see this field")

    model_config = ConfigDict(extra="forbid")  # Raise error on unknown fields

    @field_validator('tags')
    @classmethod
    def validate_tags(cls, v):
        """Ensure tags include mig_phase_X and verified (normalized snake_case format)
        Exception: provenance-only tags (in_omni_in_looker_not_csv, only_omni) are valid without migration tags"""
        if not v:
            raise ValueError("Tags cannot be empty")

        # Provenance-only tags don't need migration/governance tags
        provenance_tags = {'in_omni_in_looker_not_csv', 'only_omni'}
        if all(tag in provenance_tags for tag in v):
            return v

        has_phase = any(tag.startswith('mig_phase') for tag in v)
        has_verified = 'verified' in v or 'unverified' in v

        if not has_phase:
            raise ValueError("Tags must include mig_phase_X (e.g., mig_phase_0)")
        if not has_verified:
            raise ValueError("Tags must include verified or unverified")

        return v

    @model_validator(mode='after')
    def validate_date_dimension(self):
        """If timeframes present, validate date dimension structure"""
        timeframes = self.timeframes
        datatype = self.datatype

        if timeframes and not datatype:
            raise ValueError("Date dimensions with timeframes must include datatype (timestamp or date)")

        if datatype and not timeframes:
            raise ValueError("datatype parameter only allowed for date dimensions with timeframes")

        return self


class OmniMeasure(BaseModel):
    """
    Omni measure definition

    CRITICAL RULES:
    - Use 'aggregate_type' NOT 'type'
    - aggregate_type is OPTIONAL for calculated measures (e.g., ${measure1}/${measure2})
    - Use 'format' NOT 'value_format' or 'value_format_name'
    - NO 'html' parameter
    """
    aggregate_type: Optional[AggregateType] = Field(None, description="Aggregation function (optional for calculated measures)")
    sql: Optional[str] = Field(None, description="SQL expression (optional for plain row counts)")
    label: str = Field(..., description="Human-friendly label")
    description: str = Field(..., description="Concise description")
    tags: List[str] = Field(..., description="Tags including metric, domain, mig_phase_X, verified (snake_case)")

    # Optional fields
    hidden: Optional[bool] = None
    filters: Optional[Dict[str, Any]] = Field(None, description="Filter conditions")
    format: Optional[str] = Field(None, description="Display format")
    percentile: Optional[int] = Field(None, description="Percentile value (0-100) for percentile measures")
    custom_primary_key_sql: Optional[str] = Field(None, description="For *_distinct_on measures")
    ai_context: Optional[str] = None
    synonyms: Optional[List[str]] = None
    group_label: Optional[str] = None
    required_access_grants: Optional[List[str]] = Field(None, description="Access grants required to see this measure")

    model_config = ConfigDict(extra="forbid")

    @field_validator('tags')
    @classmethod
    def validate_tags(cls, v):
        """Ensure tags include mig_phase_X and verified (normalized snake_case format)
        Exception: provenance-only tags (in_omni_in_looker_not_csv, only_omni) are valid without migration tags"""
        if not v:
            raise ValueError("Tags cannot be empty")

        # Provenance-only tags don't need migration/governance tags
        provenance_tags = {'in_omni_in_looker_not_csv', 'only_omni'}
        if all(tag in provenance_tags for tag in v):
            return v

        has_phase = any(tag.startswith('mig_phase') for tag in v)
        has_verified = 'verified' in v or 'unverified' in v

        if not has_phase:
            raise ValueError("Tags must include mig_phase_X (e.g., mig_phase_0)")
        if not has_verified:
            raise ValueError("Tags must include verified or unverified")

        return v

    @model_validator(mode='after')
    def validate_sql_or_count(self):
        """A measure needs SQL unless it is a plain row count"""
        if not self.sql and self.aggregate_type != AggregateType.COUNT:
            raise ValueError("Measure must define sql unless aggregate_type is count")
        return self

    @model_validator(mode='after')
    def validate_percentile(self):
        """Validate percentile measures"""
        aggregate_type = self.aggregate_type
        percentile = self.percentile

        if aggregate_type in [AggregateType.PERCENTILE, AggregateType.PERCENTILE_DISTINCT_ON]:
            if percentile is None:
                raise ValueError(f"Percentile measures require percentile parameter")
            if not 0 <= percentile <= 100:
                raise ValueError(f"Percentile must be between 0 and 100")

        return self


class OmniQuery(BaseModel):
    """
    Omni native derived table (query-based)

    Equivalent to Looker explore_source derived tables
    """
    fields: Dict[str, str] = Field(..., description="Field mapping: source_field -> alias")
    base_view: str = Field(..., description="Base view to query from")
    filters: Optional[Dict[str, Any]] = Field(None, description="Filter conditions")
    topic: Optional[str] = Field(None, description="Topic to query from")
    sorts: Optional[List[Dict[str, Any]]] = Field(None, description="[{field, desc}] sort order")
    limit: Optional[int] = Field(None, description="Row limit")

    model_config = ConfigDict(extra="forbid")


class OmniView(BaseModel):
    """
    Omni view definition

    Supports three types:
    1. Standard table view: uses table_name
    2. SQL derived table: uses sql
    3. Native derived table: uses query (from Looker explore_source)

    File extension: .view.yaml or .query.view
    """
    # Stored as `schema_` because `schema` shadows a BaseModel attribute;
    # it is read and written as `schema` via the alias.
    schema_: str = Field(..., alias="schema", description="Schema name (e.g., thelook_ecommerce)")
    catalog: Optional[str] = Field(None, description="Database/project when the connection spans several (3-part table names)")
    folder: str = Field(..., description="Folder path (e.g., thelook_ecommerce/curated)")
    label: str = Field(..., description="Human-friendly view label")
    description: str = Field(..., description="Concise view description")
    tags: List[str] = Field(..., description="View-level tags")

    # EXACTLY ONE of these three must be present:
    table_name: Optional[str] = Field(None, description="Base table name (for standard views)")
    sql: Optional[str] = Field(None, description="Raw SQL query (for SQL derived tables)")
    query: Optional[OmniQuery] = Field(None, description="Query definition (for native derived tables)")

    # Optional view-level fields
    ai_context: Optional[str] = None
    extends: Optional[str] = Field(None, description="View to extend from")
    template: Optional[bool] = Field(None, description="True if this is a template view (abstract)")

    # Fields
    dimensions: Optional[Dict[str, OmniDimension]] = Field(default_factory=dict)
    measures: Optional[Dict[str, OmniMeasure]] = Field(default_factory=dict)

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    @property
    def schema_name(self) -> str:
        """Warehouse schema of the view"""
        return self.schema_

    @field_validator('tags')
    @classmethod
    def validate_tags(cls, v):
        """Ensure tags include mig_phase_X and verified (normalized snake_case format)"""
        if not v:
            raise ValueError("Tags cannot be empty")

        has_phase = any(tag.startswith('mig_phase') for tag in v)
        has_verified = 'verified' in v or 'unverified' in v

        if not has_phase:
            raise ValueError("View tags must include mig_phase_X (e.g., mig_phase_0)")
        if not has_verified:
            raise ValueError("View tags must include verified or unverified")

        return v

    @model_validator(mode='after')
    def validate_table_or_sql_or_query(self):
        """Ensure exactly one of table_name, sql, or query is present"""
        table_name = self.table_name
        sql = self.sql
        query = self.query

        sources = [table_name, sql, query]
        non_none = [s for s in sources if s is not None]

        if len(non_none) == 0:
            raise ValueError("View must have exactly one of: table_name, sql, or query")

        if len(non_none) > 1:
            raise ValueError("View cannot have multiple sources - choose one of: table_name, sql, or query")

        return self

    @model_validator(mode='after')
    def validate_primary_key(self):
        """Warn if primary key is missing or multiple exist"""
        dimensions = self.dimensions or {}

        if not dimensions:
            return self  # No dimensions yet (building incrementally)

        primary_keys = [
            name for name, dim in dimensions.items()
            if dim.primary_key is True
        ]

        # RELAXED: Allow zero primary keys (orchestrator will flag as HIGH RISK)
        # This happens when primary key field is not in CSV scope

        if len(primary_keys) > 1:
            raise ValueError(
                f"View has multiple primary keys: {primary_keys}. "
                "Only one dimension can be primary_key: true"
            )

        return self


class OmniTopic(BaseModel):
    """
    Omni topic definition

    File extension: .topic.yaml
    Supports both test topics (single view, no joins) and real topics (from explores, with joins)
    """
    base_view: str = Field(..., description="Base view name (e.g., order_items)")
    label: str = Field(..., description="Topic label")
    group_label: Optional[str] = Field(None, description="Optional group label for organizing topics")
    description: str = Field(..., description="Concise topic description")
    fields: List[str] = Field(..., description="List of fields (supports wildcards like all_views.*)")
    joins: Dict = Field(default_factory=dict, description="Join definitions (empty for single-view topics)")
    ai_context: str = Field(..., description="Detailed context for AI")

    # Row/topic-level security carried over from the Looker explore
    access_filters: Optional[List[Dict[str, Any]]] = Field(None, description="Row-level access filters (field + user_attribute)")
    always_where_sql: Optional[str] = Field(None, description="SQL filter applied to every query (Looker sql_always_where)")
    always_having_sql: Optional[str] = Field(None, description="HAVING filter applied to every query (Looker sql_always_having)")
    hidden: Optional[bool] = Field(None, description="Hide the topic from the topic picker")
    required_access_grants: Optional[List[str]] = Field(None, description="Access grants required to use the topic")

    model_config = ConfigDict(extra="forbid")

    @field_validator('fields')
    @classmethod
    def validate_fields_not_empty(cls, v):
        """Ensure fields list is not empty"""
        if not v:
            raise ValueError("Topic must have at least one field")
        return v


class MigrationReport(BaseModel):
    """
    Migration report structure

    File extension: .md
    """
    looker_view: str
    omni_view: str
    phase: int

    pr_summary: str
    view_file_decision: Dict[str, str]
    metadata_for_standardization: List[str]
    changes_made: Dict[str, List[str]]
    redundancy_candidates: List[str]
    not_converted: List[Dict[str, str]]
    notes_risks: List[Dict[str, str]]
    advanced_features: Optional[List[Dict[str, str]]] = None  # NEW: templated views, access grants, etc.
    merge_info: Optional[Dict[str, Any]] = None  # Merge/enrich stats when existing Omni view was found

    def to_markdown(self) -> str:
        """Convert to markdown report"""
        md = f"# Migration Report: {self.looker_view}\n\n"

        md += "## PR Summary\n"
        md += f"{self.pr_summary}\n\n"

        # Merge/enrich info
        if self.merge_info:
            mi = self.merge_info
            md += "## Merge/Enrich Summary\n"
            md += f"- **Source**: `{mi.get('source', 'N/A')}`\n"
            md += f"- **Existing fields**: {mi.get('existing_dims', 0)} dimensions, {mi.get('existing_measures', 0)} measures\n"
            md += f"- **Updated (in scope)**: {mi.get('enriched', 0)} fields\n"
            md += f"- **Merged under a warehouse column name**: {mi.get('renamed', 0)} fields\n"
            md += f"- **Added from Looker** (not in the Omni view): {mi.get('added', 0)} fields\n"
            md += f"- **Added hidden** (Looker fields not in scope, not in the Omni view): {mi.get('added_hidden', 0)} fields\n"
            md += f"- **Hidden** (existing, not in scope): {mi.get('hidden_out_of_scope', 0)} fields\n"
            md += f"- **Kept visible** (migrated by an earlier run): {mi.get('kept_previous_migration', 0)} fields\n"
            for note in mi.get('notes') or []:
                md += f"- {note}\n"
            md += "\n"

        md += "## View File Decision\n"
        for key, value in self.view_file_decision.items():
            md += f"- **{key}**: {value}\n"
        md += "\n"

        md += "## Metadata for Standardization\n"
        if self.metadata_for_standardization:
            for item in self.metadata_for_standardization:
                md += f"- {item}\n"
        else:
            md += "(none)\n"
        md += "\n"

        md += "## Changes Made\n"
        for category, items in self.changes_made.items():
            md += f"- **{category}**: {', '.join(items)} ({len(items)} total)\n"
        md += "\n"

        md += "## Redundancy Candidates\n"
        if self.redundancy_candidates:
            for item in self.redundancy_candidates:
                md += f"- {item}\n"
        else:
            md += "(none)\n"
        md += "\n"

        md += "## Not Converted\n"
        if self.not_converted:
            for item in self.not_converted:
                md += f"- **{item['field']}** - Reason: {item['reason']}\n"
        else:
            md += "(none)\n"
        md += "\n"

        md += "## Notes / Risks\n"
        if self.notes_risks:
            for item in self.notes_risks:
                md += f"- **{item['level']}**: {item['note']}\n"
        else:
            md += "(none)\n"
        md += "\n"

        # NEW: Advanced Features section
        if self.advanced_features:
            md += "## ⚠️ Advanced Features Detected\n"
            md += "**These features require special handling:**\n\n"
            for feature in self.advanced_features:
                md += f"### {feature['feature']}\n"
                md += f"- **Status**: {feature['status']}\n"
                md += f"- **Details**: {feature['details']}\n"
                if 'action_required' in feature:
                    md += f"- **Action Required**: {feature['action_required']}\n"
                md += "\n"

        return md


# Helper functions for YAML serialization

def dimension_to_yaml_dict(dimension: OmniDimension) -> Dict:
    """Convert OmniDimension to dict for YAML serialization"""
    return dimension.model_dump(exclude_none=True, mode='json')


def measure_to_yaml_dict(measure: OmniMeasure) -> Dict:
    """Convert OmniMeasure to dict for YAML serialization"""
    d = measure.model_dump(exclude_none=True, mode='json')
    # aggregate_type is already converted to string by mode='json'
    return d


def view_to_yaml_dict(view: OmniView) -> Dict:
    """Convert OmniView to dict for YAML serialization"""
    d = view.model_dump(exclude_none=True, mode='json', by_alias=True)

    # Dimensions and measures are already converted to dicts by model_dump
    # No need for additional conversion

    return d


def topic_to_yaml_dict(topic: OmniTopic) -> Dict:
    """Convert OmniTopic to dict for YAML serialization"""
    return topic.model_dump(exclude_none=True, mode='json')
