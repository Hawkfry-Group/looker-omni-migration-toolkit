"""
Static checks on the handwritten Omni example bundle (examples/omni_model) and
the generated reference outputs (examples/simple/expected).

These check consistency (YAML parses with PyYAML and the C-backed ruamel parser, names match files,
enum and format values are documented Omni values); they do not replace
validation in a real Omni instance.
"""

import re
from pathlib import Path

import pytest
import yaml
from ruamel.yaml import YAML

from conftest import SKILL_ROOT

BUNDLE = SKILL_ROOT / "examples" / "omni_model"
EXPECTED = SKILL_ROOT / "examples" / "simple" / "expected"
YAML_FILES = sorted(p for p in list(BUNDLE.rglob("*")) + list(EXPECTED.glob("*")) if p.is_file())

# Schema views Omni creates when the thelook_ecommerce schema is synced
PREREQUISITE_VIEWS = {f"thelook_ecommerce__{t}" for t in
                      ("orders", "products", "inventory_items", "distribution_centers", "events")}
JOIN_TYPES = {"always_left", "inner", "full_outer", "cross", "right_left", "left_right"}
FORMAT_RE = re.compile(
    r"(number|percent|id|billions|millions|thousands|big|"
    r"(usd|eur|gbp|aud|jpy|brl|aed)?(accounting|currency|bigcurrency|financial|bigaccounting|bigfinancial))(_[0-4])?"
)


def view_name(path: Path) -> str:
    rel = path.relative_to(BUNDLE)
    stem = rel.name.split(".")[0]
    return "__".join(list(rel.parts[:-1]) + [stem])


def bundle_views():
    return {view_name(p): p for p in BUNDLE.rglob("*.view")}


@pytest.mark.parametrize("path", YAML_FILES, ids=lambda p: str(p.relative_to(SKILL_ROOT)))
def test_parses_with_both_parsers(path):
    text = path.read_text()
    yaml.safe_load(text)
    YAML(typ="safe", pure=False).load(text)  # C-backed parser where available


def walk(obj, key=None):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k, v
            yield from walk(v, k)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk(v, key)


@pytest.mark.parametrize("path", YAML_FILES, ids=lambda p: str(p.relative_to(SKILL_ROOT)))
def test_formats_are_named_omni_formats(path):
    data = yaml.safe_load(path.read_text())
    for key, value in walk(data):
        if key == "format":
            assert FORMAT_RE.fullmatch(str(value)), f"{path.name}: format {value}"


def test_relationship_join_types_and_views_exist():
    views = set(bundle_views()) | PREREQUISITE_VIEWS
    rels = yaml.safe_load((BUNDLE / "relationships").read_text())
    for rel in rels:
        assert rel["join_type"] in JOIN_TYPES
        assert rel["join_from_view"] in views, rel
        assert rel["join_to_view"] in views, rel
    for rel in yaml.safe_load((EXPECTED / "order_items.relationships").read_text()):
        assert rel["join_type"] in JOIN_TYPES


def test_topic_and_view_references_resolve():
    views = set(bundle_views()) | PREREQUISITE_VIEWS
    topics = {p.name.split(".")[0] for p in BUNDLE.glob("*.topic")}
    aliases = {r.get("join_to_view_as") for r in yaml.safe_load((BUNDLE / "relationships").read_text())}
    for path in BUNDLE.rglob("*"):
        if not path.is_file() or path.name == "model":
            continue
        text = path.read_text()
        for topic in re.findall(r"^\s*topic:\s*(\S+)", text, re.MULTILINE):
            assert topic in topics, f"{path.name}: topic {topic}"
        for ref in re.findall(r"\$\{([a-z0-9_]+)\.[a-z0-9_]+(?::[a-z_]+)?\}", text):
            assert ref in views or ref in aliases, f"{path.name}: ${{{ref}.…}}"
        for ref in re.findall(r"filters\.([a-z0-9_]+)\.", text):
            assert ref in views, f"{path.name}: filters.{ref}"
        data = yaml.safe_load(text)
        if isinstance(data, dict):
            for key in ("base_view",):
                if key in data:
                    assert data[key] in views
            for joined in walk(data.get("joins") or {}):
                assert joined[0] in views, f"{path.name}: join {joined[0]}"


def test_selectors_with_colons_are_quoted():
    for path in [BUNDLE / "users.topic", EXPECTED / "order_items.topic.yaml"]:
        assert re.search(r'(?<!")-tag:', path.read_text()) is None, path.name
