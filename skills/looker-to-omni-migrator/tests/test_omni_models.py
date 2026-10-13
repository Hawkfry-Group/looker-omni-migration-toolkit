"""
Unit tests for lib/omni_models.py

Tests Pydantic validation models for Omni YAML structures
"""

import warnings

import pytest
from lib.omni_models import (
    OmniDimension, OmniMeasure, OmniView, OmniTopic, MigrationReport,
    AggregateType, dimension_to_yaml_dict, measure_to_yaml_dict,
    view_to_yaml_dict, topic_to_yaml_dict
)


class TestOmniDimension:
    """Test OmniDimension validation and serialization"""

    def test_valid_dimension_minimal(self):
        """Test minimal valid dimension"""
        dim = OmniDimension(
            label="User ID",
            description="Unique user identifier",
            tags=["id", "mig_phase_0", "verified"]
        )
        assert dim.label == "User ID"
        assert dim.sql is None  # Optional

    def test_valid_dimension_with_sql(self):
        """Test dimension with custom SQL"""
        dim = OmniDimension(
            sql="UPPER(first_name)",
            label="First Name",
            description="Uppercase first name",
            tags=["name", "mig_phase_0", "verified"]
        )
        assert dim.sql == "UPPER(first_name)"

    def test_primary_key_dimension(self):
        """Test dimension with primary_key"""
        dim = OmniDimension(
            label="ID",
            description="Primary key",
            primary_key=True,
            tags=["id", "mig_phase_0", "verified"]
        )
        assert dim.primary_key is True

    def test_date_dimension_with_timeframes(self):
        """Test date dimension with timeframes and datatype"""
        dim = OmniDimension(
            label="Created",
            description="Creation date",
            datatype="timestamp",
            timeframes=["raw", "date", "week", "month", "year"],
            tags=["date", "mig_phase_0", "verified"]
        )
        assert dim.datatype == "timestamp"
        assert len(dim.timeframes) == 5

    def test_missing_tags_raises_error(self):
        """Test that missing tags raises validation error"""
        with pytest.raises(ValueError, match="Tags cannot be empty"):
            OmniDimension(
                label="Test",
                description="Test",
                tags=[]
            )

    def test_missing_phase_tag_raises_error(self):
        """Test that missing mig_phase tag raises error"""
        with pytest.raises(ValueError, match="Tags must include mig_phase_X"):
            OmniDimension(
                label="Test",
                description="Test",
                tags=["test", "verified"]
            )

    def test_missing_verified_tag_raises_error(self):
        """Test that missing VERIFIED/UNVERIFIED tag raises error"""
        with pytest.raises(ValueError, match="Tags must include verified or unverified"):
            OmniDimension(
                label="Test",
                description="Test",
                tags=["test", "mig_phase_0"]
            )

    def test_date_dimension_missing_datatype_raises_error(self):
        """Test that timeframes without datatype raises error"""
        with pytest.raises(ValueError, match="Date dimensions with timeframes must include datatype"):
            OmniDimension(
                label="Created",
                description="Creation date",
                timeframes=["raw", "date"],
                tags=["date", "mig_phase_0", "verified"]
            )

    def test_dimension_to_yaml_dict(self):
        """Test dimension serialization to YAML dict"""
        dim = OmniDimension(
            sql="user_id",
            label="User ID",
            description="Unique user identifier",
            primary_key=True,
            tags=["id", "mig_phase_0", "verified"]
        )
        yaml_dict = dimension_to_yaml_dict(dim)
        assert yaml_dict["label"] == "User ID"
        assert yaml_dict["sql"] == "user_id"
        assert yaml_dict["primary_key"] is True
        assert "tags" in yaml_dict


class TestOmniMeasure:
    """Test OmniMeasure validation and serialization"""

    def test_valid_measure_with_aggregate(self):
        """Test measure with aggregate_type"""
        measure = OmniMeasure(
            aggregate_type=AggregateType.COUNT,
            sql="user_id",
            label="User Count",
            description="Count of users",
            tags=["metric", "mig_phase_0", "verified"]
        )
        assert measure.aggregate_type == AggregateType.COUNT

    def test_calculated_measure_without_aggregate(self):
        """Test calculated measure (no aggregate_type)"""
        measure = OmniMeasure(
            sql="${total_revenue} / ${total_users}",
            label="Revenue Per User",
            description="Average revenue per user",
            tags=["metric", "calculated", "mig_phase_0", "verified"]
        )
        assert measure.aggregate_type is None

    def test_measure_with_format(self):
        """Test measure with display format"""
        measure = OmniMeasure(
            aggregate_type=AggregateType.SUM,
            sql="revenue",
            label="Total Revenue",
            description="Sum of revenue",
            format="usdcurrency_2",
            tags=["metric", "revenue", "mig_phase_0", "verified"]
        )
        assert measure.format == "usdcurrency_2"

    def test_percentile_measure_requires_percentile_param(self):
        """Test that percentile measure requires percentile parameter"""
        with pytest.raises(ValueError, match="Percentile measures require percentile parameter"):
            OmniMeasure(
                aggregate_type=AggregateType.PERCENTILE,
                sql="value",
                label="90th Percentile",
                description="90th percentile value",
                tags=["metric", "mig_phase_0", "verified"]
            )

    def test_percentile_measure_valid(self):
        """Test valid percentile measure"""
        measure = OmniMeasure(
            aggregate_type=AggregateType.PERCENTILE,
            sql="value",
            label="90th Percentile",
            description="90th percentile value",
            percentile=90,
            tags=["metric", "mig_phase_0", "verified"]
        )
        assert measure.percentile == 90

    def test_measure_to_yaml_dict(self):
        """Test measure serialization to YAML dict"""
        measure = OmniMeasure(
            aggregate_type=AggregateType.SUM,
            sql="revenue",
            label="Total Revenue",
            description="Sum of revenue",
            tags=["metric", "mig_phase_0", "verified"]
        )
        yaml_dict = measure_to_yaml_dict(measure)
        assert yaml_dict["aggregate_type"] == "sum"
        assert yaml_dict["label"] == "Total Revenue"


class TestOmniView:
    """Test OmniView validation and serialization"""

    def test_valid_view_with_table_name(self):
        """Test standard view with table_name"""
        dim = OmniDimension(
            label="ID",
            description="Primary key",
            primary_key=True,
            tags=["id", "mig_phase_0", "verified"]
        )
        view = OmniView(
            schema="thelook_ecommerce",
            folder="thelook_ecommerce/curated",
            table_name="users",
            label="Users",
            description="Users view",
            tags=["users", "dimension", "mig_phase_0", "verified"],
            dimensions={"user_id": dim}
        )
        assert view.table_name == "users"
        assert view.query is None

    def test_valid_view_with_sql(self):
        """Test SQL derived table view"""
        dim = OmniDimension(
            label="ID",
            description="Primary key",
            primary_key=True,
            tags=["id", "mig_phase_0", "verified"]
        )
        view = OmniView(
            schema="thelook_ecommerce",
            folder="thelook_ecommerce/curated",
            sql="SELECT * FROM users",
            label="Users",
            description="Users view",
            tags=["users", "dimension", "mig_phase_0", "verified"],
            dimensions={"user_id": dim}
        )
        assert view.sql == "SELECT * FROM users"
        assert view.table_name is None
        assert view.query is None

    def test_valid_view_with_query(self):
        """Test native derived table view (query-based)"""
        from lib.omni_models import OmniQuery

        dim = OmniDimension(
            label="ID",
            description="Primary key",
            primary_key=True,
            tags=["id", "mig_phase_0", "verified"]
        )
        query = OmniQuery(
            fields={"main__users.user_id": "user_id"},
            base_view="main__users"
        )
        view = OmniView(
            schema="thelook_ecommerce",
            folder="thelook_ecommerce/curated",
            query=query,
            label="Users",
            description="Users view",
            tags=["users", "dimension", "mig_phase_0", "verified"],
            dimensions={"user_id": dim}
        )
        assert view.query is not None
        assert view.query.base_view == "main__users"
        assert view.table_name is None
        assert view.sql is None

    def test_view_requires_table_or_sql_or_query(self):
        """Test that view must have exactly one of table_name, sql, or query"""
        dim = OmniDimension(
            label="ID",
            description="Primary key",
            primary_key=True,
            tags=["id", "mig_phase_0", "verified"]
        )
        with pytest.raises(ValueError, match="View must have exactly one of: table_name, sql, or query"):
            OmniView(
                schema="thelook_ecommerce",
                folder="thelook_ecommerce/curated",
                label="Users",
                description="Users view",
                tags=["users", "dimension", "mig_phase_0", "verified"],
                dimensions={"user_id": dim}
            )

    def test_view_allows_no_primary_key(self):
        """Test that view allows zero primary keys (will be flagged as HIGH RISK by orchestrator)"""
        dim_no_pk = OmniDimension(
            label="Name",
            description="First name",
            tags=["name", "mig_phase_0", "verified"]
        )
        # Should not raise - validation relaxed to allow CSV scope filtering
        view = OmniView(
            schema="thelook_ecommerce",
            folder="thelook_ecommerce/curated",
            table_name="users",
            label="Users",
            description="Users view",
            tags=["users", "dimension", "mig_phase_0", "verified"],
            dimensions={"first_name": dim_no_pk}
        )
        assert view.dimensions["first_name"].primary_key is None

    def test_view_rejects_multiple_primary_keys(self):
        """Test that view rejects multiple primary keys"""
        dim_pk1 = OmniDimension(
            label="ID",
            description="Primary key 1",
            primary_key=True,
            tags=["id", "mig_phase_0", "verified"]
        )
        dim_pk2 = OmniDimension(
            label="Alt ID",
            description="Primary key 2",
            primary_key=True,
            tags=["id", "mig_phase_0", "verified"]
        )
        with pytest.raises(ValueError, match="View has multiple primary keys"):
            OmniView(
                schema="thelook_ecommerce",
                folder="thelook_ecommerce/curated",
                table_name="users",
                label="Users",
                description="Users view",
                tags=["users", "dimension", "mig_phase_0", "verified"],
                dimensions={"user_id": dim_pk1, "alt_id": dim_pk2}
            )

    def test_view_to_yaml_dict(self):
        """Test view serialization to YAML dict"""
        dim = OmniDimension(
            label="ID",
            description="Primary key",
            primary_key=True,
            tags=["id", "mig_phase_0", "verified"]
        )
        view = OmniView(
            schema="thelook_ecommerce",
            folder="thelook_ecommerce/curated",
            table_name="users",
            label="Users",
            description="Users view",
            tags=["users", "dimension", "mig_phase_0", "verified"],
            dimensions={"user_id": dim}
        )
        yaml_dict = view_to_yaml_dict(view)
        assert yaml_dict["schema"] == "thelook_ecommerce"
        assert yaml_dict["table_name"] == "users"
        assert "dimensions" in yaml_dict


class TestOmniTopic:
    """Test OmniTopic validation"""

    def test_valid_topic(self):
        """Test valid migration topic"""
        topic = OmniTopic(
            base_view="users",
            label="Users Migration",
            group_label="Migration Test",
            description="User data for testing",
            fields=["users.user_id", "users.count"],
            joins={},
            ai_context="This topic provides user data."
        )
        assert topic.base_view == "users"
        assert len(topic.fields) == 2

    def test_topic_allows_free_form_label(self):
        """Topic labels are free-form; no naming convention is enforced"""
        topic = OmniTopic(
            base_view="users",
            label="Users Data",
            description="User data",
            fields=["users.user_id"],
            joins={},
            ai_context="Test"
        )
        assert topic.label == "Users Data"

    def test_topic_group_label_is_optional(self):
        """group_label is optional and accepts any value"""
        topic = OmniTopic(
            base_view="users",
            label="Users Migration",
            description="User data",
            fields=["users.user_id"],
            joins={},
            ai_context="Test"
        )
        assert topic.group_label is None

    def test_topic_accepts_joins(self):
        """Topics generated from explores carry join definitions"""
        topic = OmniTopic(
            base_view="users",
            label="Users Migration",
            description="User data",
            fields=["users.user_id"],
            joins={"orders": {}},
            ai_context="Test"
        )
        assert "orders" in topic.joins

    def test_topic_accepts_wildcard_fields(self):
        """Wildcard field selectors are valid Omni topic syntax"""
        topic = OmniTopic(
            base_view="users",
            label="Users Migration",
            description="User data",
            fields=["users.*"],
            joins={},
            ai_context="Test"
        )
        assert topic.fields == ["users.*"]

    def test_topic_requires_at_least_one_field(self):
        """Empty field lists are rejected"""
        with pytest.raises(ValueError, match="Topic must have at least one field"):
            OmniTopic(
                base_view="users",
                label="Users Migration",
                description="User data",
                fields=[],
                joins={},
                ai_context="Test"
            )


class TestMigrationReport:
    """Test MigrationReport model and markdown generation"""

    def test_valid_report(self):
        """Test valid migration report"""
        report = MigrationReport(
            looker_view="users",
            omni_view="users",
            phase=0,
            pr_summary="Migrated users",
            view_file_decision={"File Type": "Standard view"},
            metadata_for_standardization=["Added ai_context"],
            changes_made={"Canonical Mappings": ["sku → product"]},
            redundancy_candidates=[],
            not_converted=[],
            notes_risks=[]
        )
        assert report.looker_view == "users"
        assert report.phase == 0

    def test_report_to_markdown(self):
        """Test markdown generation"""
        report = MigrationReport(
            looker_view="users",
            omni_view="users",
            phase=0,
            pr_summary="Migrated users",
            view_file_decision={"File Type": "Standard view"},
            metadata_for_standardization=["Added ai_context"],
            changes_made={"Canonical Mappings": ["sku → product"]},
            redundancy_candidates=[],
            not_converted=[],
            notes_risks=[{"level": "INFO", "note": "Test note"}]
        )
        markdown = report.to_markdown()
        assert "# Migration Report: users" in markdown
        assert "## PR Summary" in markdown
        assert "## Notes / Risks" in markdown

    def test_report_with_advanced_features(self):
        """Test report with advanced features section"""
        report = MigrationReport(
            looker_view="users",
            omni_view="users",
            phase=0,
            pr_summary="Migrated users",
            view_file_decision={"File Type": "Standard view"},
            metadata_for_standardization=[],
            changes_made={},
            redundancy_candidates=[],
            not_converted=[],
            notes_risks=[],
            advanced_features=[
                {
                    "feature": "Templated View",
                    "status": "DETECTED",
                    "details": "View extends: base_view",
                    "action_required": "Migrate base view first"
                }
            ]
        )
        markdown = report.to_markdown()
        assert "## ⚠️ Advanced Features Detected" in markdown
        assert "Templated View" in markdown


class TestModelHygiene:
    """Regression tests for model definition issues"""

    def test_view_schema_field_does_not_shadow_basemodel(self):
        """Defining and using OmniView emits no pydantic shadowing warning"""
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            view = OmniView(
                schema="thelook_ecommerce",
                folder="thelook_ecommerce/curated",
                table_name="users",
                label="Users",
                description="Users view",
                tags=["mig_phase_0", "verified"],
            )
        assert view.schema_name == "thelook_ecommerce"
        assert view_to_yaml_dict(view)["schema"] == "thelook_ecommerce"
        assert "schema_" not in view_to_yaml_dict(view)

    def test_count_measure_without_sql(self):
        """A plain row count needs no sql"""
        measure = OmniMeasure(
            aggregate_type=AggregateType.COUNT,
            label="Count",
            description="Row count",
            tags=["metric", "mig_phase_0", "verified"],
        )
        assert "sql" not in measure_to_yaml_dict(measure)

    def test_non_count_measure_requires_sql(self):
        with pytest.raises(ValueError, match="Measure must define sql"):
            OmniMeasure(
                aggregate_type=AggregateType.SUM,
                label="Total",
                description="Sum",
                tags=["metric", "mig_phase_0", "verified"],
            )

    def test_topic_accepts_row_level_restrictions(self):
        topic = OmniTopic(
            base_view="order_items",
            label="Orders",
            description="Orders",
            fields=["all_views.*"],
            joins={"users": {}},
            ai_context="Test",
            access_filters=[{"field": "users.country", "user_attribute": "allowed_country"}],
            always_where_sql="${order_items.status} != 'Cancelled'",
            required_access_grants=["can_view_financials"],
        )
        d = topic_to_yaml_dict(topic)
        assert d["access_filters"][0]["user_attribute"] == "allowed_country"
        assert d["required_access_grants"] == ["can_view_financials"]
