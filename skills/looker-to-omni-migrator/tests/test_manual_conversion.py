"""
Hand-built conversion: the Pydantic models serialize a thelook `users` view and
topic to loadable Omni YAML.

Source Looker view (abridged):

    view: users {
      sql_table_name: thelook_ecommerce.users ;;
      dimension: id { primary_key: yes type: number sql: ${TABLE}.id ;; }
      dimension: email { type: string sql: ${TABLE}.email ;; }
      dimension: country { type: string sql: ${TABLE}.country ;; }
      measure: count { type: count }
    }
"""

import yaml

from lib.file_generators import FileGenerator
from lib.omni_models import (
    AggregateType, OmniDimension, OmniMeasure, OmniTopic, OmniView,
    topic_to_yaml_dict, view_to_yaml_dict,
)

TAGS = ["mig_phase_0", "verified"]


def build_users_view() -> OmniView:
    return OmniView(
        schema="thelook_ecommerce",
        folder="thelook_ecommerce/curated",
        table_name="users",
        label="Users",
        description="Shoppers registered on the store. One row per user.",
        ai_context="Grain: one row per user. Join order_items on users.id = order_items.user_id.",
        tags=["users", "fact"] + TAGS,
        dimensions={
            "id": OmniDimension(
                label="User ID",
                description="Unique identifier for the user.",
                primary_key=True,
                tags=["id"] + TAGS,
            ),
            "email": OmniDimension(
                label="Email",
                description="Email address of the user.",
                tags=["pii"] + TAGS,
                required_access_grants=["can_view_pii"],
            ),
            "country": OmniDimension(
                label="Country",
                description="Country the user registered from.",
                sample_values=["Brasil", "Japan"],
                tags=TAGS,
            ),
        },
        measures={
            "count": OmniMeasure(
                aggregate_type=AggregateType.COUNT,
                label="User Count",
                description="Number of users.",
                format="number",
                tags=["metric"] + TAGS,
            ),
        },
    )


def build_users_topic() -> OmniTopic:
    return OmniTopic(
        base_view="users",
        label="Shoppers",
        description="Users of the store with their orders.",
        fields=["all_views.*", "-tag:table_key"],
        joins={"order_items": {}},
        ai_context="Use for questions about shoppers, e.g. 'How many users signed up from Japan?'",
    )


def test_view_serializes_to_valid_yaml():
    view_dict = view_to_yaml_dict(build_users_view())
    loaded = yaml.safe_load(yaml.dump(view_dict, sort_keys=False))

    assert loaded["schema"] == "thelook_ecommerce"
    assert loaded["table_name"] == "users"
    assert loaded["dimensions"]["id"]["primary_key"] is True
    assert loaded["dimensions"]["email"]["required_access_grants"] == ["can_view_pii"]
    assert loaded["measures"]["count"]["aggregate_type"] == "count"
    assert "sql" not in loaded["measures"]["count"]
    for section in ("dimensions", "measures"):
        for field in loaded[section].values():
            assert "mig_phase_0" in field["tags"] and "verified" in field["tags"]
            assert "type" not in field


def test_file_generator_writes_view_and_topic(tmp_path):
    generator = FileGenerator(output_dir=tmp_path / "views")
    view_path = generator.generate_view_yaml(build_users_view(), "users.view.yaml")
    topic_path = generator.generate_topic_yaml(build_users_topic(), "users.topic.yaml")

    view_yaml = yaml.safe_load(view_path.read_text())
    topic_yaml = yaml.safe_load(topic_path.read_text())

    assert view_path == tmp_path / "views" / "users.view.yaml"
    assert view_yaml["dimensions"]["country"]["sample_values"] == ["Brasil", "Japan"]
    assert topic_yaml["base_view"] == "users"
    assert topic_yaml["fields"] == ["all_views.*", "-tag:table_key"]
    assert topic_yaml["joins"] == {"order_items": {}}
    # Tags are written inline
    assert "tags: [id, mig_phase_0, verified]" in view_path.read_text()


def test_topic_dict_round_trip():
    topic_dict = topic_to_yaml_dict(build_users_topic())
    assert yaml.safe_load(yaml.dump(topic_dict)) == topic_dict
