"""Unit tests for lib/file_generators.py (merge, ordering, relationships, dry-run)"""

import yaml

from lib.file_generators import FileGenerator
from lib.omni_models import AggregateType, OmniDimension, OmniMeasure, OmniView

TAGS = ["mig_phase_0", "verified"]


def make_view(dimensions=None, measures=None):
    return OmniView(
        schema="thelook_ecommerce",
        folder="thelook_ecommerce/curated",
        table_name="orders",
        label="Orders",
        description="Orders view",
        tags=TAGS,
        dimensions=dimensions or {},
        measures=measures or {},
    )


def dim(**kw):
    return OmniDimension(label=kw.pop("label", "X"), description="d", tags=kw.pop("tags", TAGS), **kw)


def measure(**kw):
    return OmniMeasure(label="M", description="m", tags=TAGS, **kw)


def render(tmp_path, view, existing, csv_fields, mapping=None):
    gen = FileGenerator(output_dir=tmp_path)
    path = gen.generate_view_yaml(view, "orders.view.yaml", existing_view=existing,
                                  csv_fields=csv_fields, field_to_column_mapping=mapping or {})
    return yaml.safe_load(path.read_text())


def test_new_measures_added_when_existing_view_has_no_measures(tmp_path):
    view = make_view(
        dimensions={"order_id": dim(primary_key=True)},
        measures={"count": measure(aggregate_type=AggregateType.COUNT)},
    )
    existing = {"schema": "thelook_ecommerce", "table_name": "orders", "dimensions": {"order_id": {}, "status": {}}}
    out = render(tmp_path, view, existing, {"order_id", "count"})
    assert "count" in out["measures"]
    assert out["dimensions"]["status"]["hidden"] is True
    assert "only_omni" in out["dimensions"]["status"]["tags"]


def test_measures_only_existing_view(tmp_path):
    view = make_view(
        dimensions={"order_id": dim(primary_key=True)},
        measures={"count": measure(aggregate_type=AggregateType.COUNT)},
    )
    existing = {"table_name": "orders", "measures": {"legacy_total": {"sql": "${num_of_item}", "aggregate_type": "sum"}}}
    out = render(tmp_path, view, existing, {"order_id", "count"}, {"customer_id": "user_id"})
    assert set(out["dimensions"]) == {"order_id"}
    assert set(out["measures"]) == {"legacy_total", "count"}


def test_unknown_top_level_keys_preserved(tmp_path):
    view = make_view(dimensions={"order_id": dim(primary_key=True)})
    existing = {
        "schema": "thelook_ecommerce",
        "table_name": "orders",
        "dbt": {"name": "orders", "unique_id": "model.shop.orders"},
        "required_access_grants": ["can_view_financials"],
        "custom_setting": 3,
        "dimensions": {"order_id": {}},
    }
    out = render(tmp_path, view, existing, {"order_id"})
    assert out["dbt"]["unique_id"] == "model.shop.orders"
    assert out["required_access_grants"] == ["can_view_financials"]
    assert out["custom_setting"] == 3
    assert list(out)[-1] == "dimensions"


def test_renamed_field_merged_into_existing_column_and_refs_rewritten(tmp_path):
    view = make_view(
        dimensions={"customer_id": dim(label="Customer ID", sql="user_id", synonyms=["user_id"])},
        measures={"customer_count": measure(aggregate_type=AggregateType.COUNT_DISTINCT, sql="${customer_id}")},
    )
    existing = {"table_name": "orders", "dimensions": {"user_id": {"format": "id", "description": "Kept"}}}
    out = render(tmp_path, view, existing, {"customer_id", "customer_count"}, {"customer_id": "user_id"})
    assert "customer_id" not in out["dimensions"]
    user_id = out["dimensions"]["user_id"]
    assert user_id["label"] == "Customer ID"
    assert "format" not in user_id  # conversion-owned key replaced, not overlaid
    assert user_id["synonyms"] == ["customer_id"]
    assert "sql" not in user_id
    assert out["measures"]["customer_count"]["sql"] == "${user_id}"


def test_mapping_without_existing_column_keeps_looker_field(tmp_path):
    view = make_view(dimensions={"customer_id": dim(label="Customer ID", sql="user_id")})
    existing = {"table_name": "orders", "dimensions": {"order_id": {}}}
    out = render(tmp_path, view, existing, {"customer_id"}, {"customer_id": "user_id"})
    assert out["dimensions"]["customer_id"]["sql"] == "user_id"


def test_relationships_file_is_plain_yaml_list(tmp_path):
    gen = FileGenerator(output_dir=tmp_path)
    rels = [{"join_from_view": "order_items", "join_to_view": "users", "join_type": "always_left",
             "on_sql": "${order_items.user_id} = ${users.id}", "relationship_type": "many_to_one",
             "reversible": False}]
    path = gen.generate_relationships_yaml(rels, "x.relationships", ["Summary line", "Another"])
    text = path.read_text()
    assert text.startswith("# Summary line\n# Another\n")
    assert yaml.safe_load(text) == rels


def test_dry_run_writes_nothing(tmp_path):
    out_dir = tmp_path / "views"
    gen = FileGenerator(output_dir=out_dir, dry_run=True)
    path = gen.generate_view_yaml(make_view(dimensions={"order_id": dim()}), "orders.view.yaml")
    gen.generate_text_report("# r", "orders.migration.md")
    assert not tmp_path.joinpath("views").exists()
    assert not tmp_path.joinpath("reports").exists()
    assert gen.planned_writes == [out_dir / "orders.view.yaml", tmp_path / "reports" / "orders.migration.md"]
    assert path == out_dir / "orders.view.yaml"


def test_reports_go_to_sibling_reports_dir(tmp_path):
    gen = FileGenerator(output_dir=tmp_path / "anywhere" / "views")
    path = gen.generate_text_report("# r", "x.md")
    assert path == tmp_path / "anywhere" / "reports" / "x.md"
    assert path.exists()
