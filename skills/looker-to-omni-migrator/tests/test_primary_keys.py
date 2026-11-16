"""
Tests for the primary key finder (lib/primary_keys.py, scripts/find_primary_keys.py).

Nothing here calls the real Omni CLI. FakeClient stands in for OmniQueryClient:
it returns canned rows and records every SQL statement it was asked to run, so
the tests can assert on the SQL generated for each warehouse.
"""

import importlib.util
import json
import re
import subprocess
from pathlib import Path

import pytest

from conftest import SKILL_ROOT
from lib import primary_keys as pk

_spec = importlib.util.spec_from_file_location("find_primary_keys", SKILL_ROOT / "scripts" / "find_primary_keys.py")
cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cli)


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------

class FakeClient:
    """
    rules: list of (regex, response). The first rule whose regex matches the SQL
    answers it. A response is a list of rows, an exception to raise, or a
    callable taking the SQL. Unmatched SQL fails the test.
    """

    def __init__(self, dialect_name="snowflake", rules=None):
        self.dialect_name = dialect_name
        self.rules = list(rules or [])
        self.sql = []
        self.dialect_calls = 0

    def dialect(self):
        self.dialect_calls += 1
        if isinstance(self.dialect_name, Exception):
            raise self.dialect_name
        return self.dialect_name

    def run_sql(self, sql):
        self.sql.append(sql)
        for pattern, response in self.rules:
            if re.search(pattern, sql, re.IGNORECASE):
                if callable(response):
                    response = response(sql)
                if isinstance(response, Exception):
                    raise response
                return response
        raise AssertionError(f"FakeClient has no rule for SQL: {sql}")

    @property
    def data_sql(self):
        return [s for s in self.sql if "total_rows" in s and "INFORMATION_SCHEMA" not in s]


def stats(total, *per_candidate):
    """Canned answer to the candidate query: (distinct, nulls) per candidate, in order"""
    row = {"total_rows": total}
    for i, (distinct, nulls) in enumerate(per_candidate):
        row[f"distinct_{i}"] = distinct
        row[f"nulls_{i}"] = nulls
    return [row]


def pair_stats(total, *distinct):
    return [{"total_rows": total, **{f"pair_{i}": d for i, d in enumerate(distinct)}}]


ORDERS_VIEW = """\
# Orders, one row per order
schema: shop
table_name: orders
tags: [orders, mig_phase_0, verified]

dimensions:
  status:
    label: Status  # keep this comment
    tags: [mig_phase_0, verified]

  user_id:
    label: User
    tags: [mig_phase_0, verified]

  id:
    label: Id
    tags: [id, mig_phase_0, verified]

  created:
    sql: created_at
    tags: [date, temporal, mig_phase_0, verified]

  is_gift: {}

  total_with_tax:
    sql: total * 1.2

measures:
  count:
    aggregate_type: count
"""


def write_view(directory, name, text=ORDERS_VIEW, suffix=".view.yaml"):
    path = Path(directory) / f"{name}{suffix}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def examine(path, client, dialect="snowflake", **options):
    sample_keys = ("sample_rows", "date_column", "window_days", "max_unpartitioned_rows", "allow_full_scan")
    sample = pk.SampleOptions(**{k: options.pop(k) for k in sample_keys if k in options})
    strategy, _ = pk.get_dialect(dialect)
    finder = pk.PrimaryKeyFinder(client, strategy, pk.FinderOptions(sample=sample, **options), dialect_name=dialect)
    return finder.examine(pk.load_view(path))


def run_cli(tmp_path, client, *args):
    """Run the script's main() with a fake client; reports go under tmp_path"""
    argv = ["--model-id", "model-1", "--omni-repo", str(tmp_path / "views"),
            "--report", str(tmp_path / "out" / "primary_keys.md"),
            "--json", str(tmp_path / "out" / "primary_keys.json"), *map(str, args)]
    return cli.main(argv, client=client)


def read_json(tmp_path):
    return json.loads((tmp_path / "out" / "primary_keys.json").read_text())


# ----------------------------------------------------------------------
# Candidates
# ----------------------------------------------------------------------

CANDIDATE_VIEW = """\
schema: shop
table_name: order_items
dimensions:
  status: {}
  sku:
    tags: [table_key]
  product_code: {}
  order_id: {}
  created_at: {}
  is_returned: {}
  margin:
    sql: sale_price - cost
  line_ref:
    sql: line_reference
  quoted_col:
    sql: '"MixedCase"'
  order_item_id: {}
  id: {}
  shipped:
    tags: [date]
measures:
  products:
    aggregate_type: count_distinct
    sql: ${product_code}
  count:
    aggregate_type: count
"""


class TestCandidates:
    def test_order_is_cheapest_evidence_first(self, tmp_path):
        view = pk.load_view(write_view(tmp_path, "order_items", CANDIDATE_VIEW))
        candidates = pk.find_candidates(view)
        assert [(c.name, c.evidence) for c in candidates] == [
            ("id", pk.EVIDENCE_NAME),                       # exactly `id`
            ("order_item_id", pk.EVIDENCE_NAME),            # table name plus _id
            ("order_id", pk.EVIDENCE_NAME),                 # *_id
            ("product_code", pk.EVIDENCE_COUNT_DISTINCT),   # counted by a count_distinct measure
            ("sku", pk.EVIDENCE_TABLE_KEY),                 # tagged table_key
            ("status", pk.EVIDENCE_PLAIN_COLUMN),
            ("line_ref", pk.EVIDENCE_PLAIN_COLUMN),
            ("quoted_col", pk.EVIDENCE_PLAIN_COLUMN),
        ]

    def test_dates_booleans_and_expressions_are_not_candidates(self, tmp_path):
        view = pk.load_view(write_view(tmp_path, "order_items", CANDIDATE_VIEW))
        names = {c.name for c in pk.find_candidates(view)}
        assert names.isdisjoint({"created_at", "is_returned", "shipped", "margin"})

    def test_bare_sql_maps_to_that_column(self, tmp_path):
        view = pk.load_view(write_view(tmp_path, "order_items", CANDIDATE_VIEW))
        by_name = {c.name: c for c in pk.find_candidates(view)}
        assert by_name["line_ref"].column == "line_reference"
        assert (by_name["quoted_col"].column, by_name["quoted_col"].exact) == ("MixedCase", True)
        assert by_name["id"].column == "id"

    def test_max_candidates_caps_the_list(self, tmp_path):
        view = pk.load_view(write_view(tmp_path, "order_items", CANDIDATE_VIEW))
        assert [c.name for c in pk.find_candidates(view, max_candidates=2)] == ["id", "order_item_id"]

    def test_dimension_column_rules(self):
        assert pk.dimension_column("id", {}) == ("id", False)
        assert pk.dimension_column("x", {"sql": "user_id"}) == ("user_id", False)
        assert pk.dimension_column("x", {"sql": "`user_id`"}) == ("user_id", True)
        assert pk.dimension_column("x", {"sql": "${TABLE}.user_id"}) is None
        assert pk.dimension_column("x", {"sql": "coalesce(a, b)"}) is None
        assert pk.dimension_column("x", {"sql": "a; drop table b"}) is None
        assert pk.dimension_column("bad name", {}) is None


# ----------------------------------------------------------------------
# Qualification
# ----------------------------------------------------------------------

class TestQualification:
    # ORDERS_VIEW candidates, in order: id, user_id, status

    def test_unique_not_null_column_qualifies(self, tmp_path):
        client = FakeClient(rules=[("total_rows", stats(5000, (5000, 0), (800, 0), (4, 0)))])
        result = examine(write_view(tmp_path, "orders"), client)
        assert result.status == pk.STATUS_KEY_FOUND
        assert result.chosen_key == "id"
        assert result.qualifying == ["id"]
        assert result.rows_checked == 5000
        assert len(client.sql) == 1, "one data query per view"

    def test_nulls_disqualify(self, tmp_path):
        # id: every non-null value distinct, but one null, so it is not a key
        client = FakeClient(rules=[("pair_0", pair_stats(5000, 4000)),
                                   ("total_rows", stats(5000, (4999, 1), (800, 0), (4, 0)))])
        result = examine(write_view(tmp_path, "orders"), client)
        assert result.status == pk.STATUS_NO_KEY_FOUND
        assert result.chosen_key is None

    def test_duplicates_disqualify(self, tmp_path):
        client = FakeClient(rules=[("pair_0", pair_stats(5000, 4990)),
                                   ("total_rows", stats(5000, (4990, 0), (800, 0), (4, 0)))])
        result = examine(write_view(tmp_path, "orders"), client)
        assert result.status == pk.STATUS_NO_KEY_FOUND
        assert "no single column is unique" in result.message

    def test_sample_too_small_qualifies_nothing(self, tmp_path):
        client = FakeClient(rules=[("total_rows", stats(999, (999, 0), (999, 0), (4, 0)))])
        result = examine(write_view(tmp_path, "orders"), client)
        assert result.status == pk.STATUS_SAMPLE_TOO_SMALL
        assert result.chosen_key is None and result.qualifying == []
        assert "sample too small" in result.message
        assert len(client.sql) == 1, "no pair query on a sample that is too small"

    def test_min_rows_is_configurable(self, tmp_path):
        client = FakeClient(rules=[("total_rows", stats(50, (50, 0), (10, 0), (4, 0)))])
        assert examine(write_view(tmp_path, "orders"), client, min_rows=10).status == pk.STATUS_KEY_FOUND

    def test_exactly_one_qualifier_is_chosen(self, tmp_path):
        # status (plain column, weakest evidence) is the only unique column: it is still the one chosen
        client = FakeClient(rules=[("total_rows", stats(5000, (4000, 0), (800, 0), (5000, 0)))])
        result = examine(write_view(tmp_path, "orders"), client)
        assert (result.status, result.chosen_key, result.key_source) == (pk.STATUS_KEY_FOUND, "status", "only_qualifier")
        assert "status is the only column unique and not null in 5,000 rows" in result.message

    def test_several_qualifiers_choose_nothing(self, tmp_path):
        client = FakeClient(rules=[("total_rows", stats(5000, (5000, 0), (5000, 0), (5000, 0)))])
        result = examine(write_view(tmp_path, "orders"), client)
        assert result.status == pk.STATUS_MULTIPLE_CANDIDATES
        assert result.chosen_key is None and result.key_source is None
        assert result.qualifying == ["id", "user_id", "status"]   # listed in evidence order
        assert "3 columns are unique and not null in 5,000 rows" in result.message
        assert "id (key_like_name), user_id (key_like_name), status (plain_column)" in result.message
        assert "--key orders=DIMENSION" in result.message
        assert len(client.sql) == 1, "no pair query when single columns qualify"

    def test_no_tie_break_by_name_or_evidence(self, tmp_path):
        # a key-like name and a plain column both qualify: neither wins
        client = FakeClient(rules=[("total_rows", stats(5000, (4000, 0), (5000, 0), (5000, 0)))])
        result = examine(write_view(tmp_path, "orders"), client)
        assert result.status == pk.STATUS_MULTIPLE_CANDIDATES and result.chosen_key is None
        assert not hasattr(pk, "choose_key")

    def test_string_counts_and_upper_case_columns_are_understood(self, tmp_path):
        # Snowflake returns upper-case column names; JSON numbers may arrive as strings
        row = {"TOTAL_ROWS": "5000", "DISTINCT_0": "5000", "NULLS_0": None,
               "DISTINCT_1": "10", "NULLS_1": "0", "DISTINCT_2": "4", "NULLS_2": "0"}
        result = examine(write_view(tmp_path, "orders"), FakeClient(rules=[("total_rows", [row])]))
        assert result.status == pk.STATUS_KEY_FOUND and result.chosen_key == "id"

    def test_result_records_how_the_sample_was_taken(self, tmp_path):
        client = FakeClient(rules=[("total_rows", stats(5000, (5000, 0), (800, 0), (4, 0)))])
        result = examine(write_view(tmp_path, "orders"), client, sample_rows=20000)
        assert (result.strategy, result.filter, result.rows_checked) == ("limit", "limit 20000", 5000)
        assert "5,000 rows (limit: limit 20000)" in result.message
        assert "verified" not in result.message


# ----------------------------------------------------------------------
# Pair fallback
# ----------------------------------------------------------------------

class TestPairFallback:
    def test_unique_pair_is_suggested_not_chosen(self, tmp_path):
        client = FakeClient(rules=[
            ("pair_0", pair_stats(5000, 5000, 900, 4100)),   # (id,user_id) unique together
            ("total_rows", stats(5000, (2500, 0), (800, 0), (4, 0))),
        ])
        result = examine(write_view(tmp_path, "orders"), client)
        assert result.status == pk.STATUS_PAIR_SUGGESTED
        assert result.chosen_key is None
        assert [p.dimensions for p in result.pair_suggestions] == [["id", "user_id"]]
        assert result.pair_suggestions[0].sql == "cast(${id} as varchar) || '-' || cast(${user_id} as varchar)"
        assert len(client.sql) == 2, "one candidate query plus one pair query"
        pair_sql = client.sql[1]
        assert pair_sql.count("count(distinct") == 3
        assert """count(distinct cast("ID" as varchar) || '|' || cast("USER_ID" as varchar)) as pair_0""" in pair_sql

    def test_bigquery_pair_uses_concat(self, tmp_path):
        client = FakeClient(rules=[
            ("INFORMATION_SCHEMA", []),
            ("pair_0", pair_stats(5000, 5000, 1, 1)),
            ("total_rows", stats(5000, (2500, 0), (800, 0), (4, 0))),
        ])
        result = examine(write_view(tmp_path, "orders"), client, dialect="bigquery", allow_full_scan=True)
        assert "concat(cast(`id` as string), '|', cast(`user_id` as string))" in client.sql[-1]
        assert result.pair_suggestions[0].sql == "concat(cast(${id} as string), '-', cast(${user_id} as string))"

    def test_max_pairs_caps_the_pair_query(self, tmp_path):
        client = FakeClient(rules=[("pair_0", pair_stats(5000, 100)),
                                   ("total_rows", stats(5000, (2500, 0), (800, 0), (4, 0)))])
        result = examine(write_view(tmp_path, "orders"), client, max_pairs=1)
        assert "pair_0" in client.sql[1] and "pair_1" not in client.sql[1]
        assert result.status == pk.STATUS_NO_KEY_FOUND
        assert "none of the 1 pair(s) tested" in result.message

    def test_no_pair_query_when_pairs_disabled_or_columns_unusable(self, tmp_path):
        counts = stats(5000, (2500, 0), (800, 3), (1, 0))  # user_id has nulls, status is constant
        client = FakeClient(rules=[("total_rows", counts)])
        assert examine(write_view(tmp_path, "orders"), client).status == pk.STATUS_NO_KEY_FOUND
        assert len(client.sql) == 1
        client = FakeClient(rules=[("total_rows", stats(5000, (2500, 0), (800, 0), (4, 0)))])
        examine(write_view(tmp_path, "orders"), client, max_pairs=0)
        assert len(client.sql) == 1

    def test_pair_is_never_written(self, tmp_path):
        path = write_view(tmp_path / "views", "orders")
        client = FakeClient(rules=[("pair_0", pair_stats(5000, 5000, 1, 1)),
                                   ("total_rows", stats(5000, (2500, 0), (800, 0), (4, 0)))])
        assert run_cli(tmp_path, client, "--apply") == 0
        assert path.read_text() == ORDERS_VIEW
        record = read_json(tmp_path)["views"][0]
        assert record["status"] == "pair_suggested" and record["applied"] is False
        report = (tmp_path / "out" / "primary_keys.md").read_text()
        assert "sql: cast(${id} as varchar) || '-' || cast(${user_id} as varchar)" in report


# ----------------------------------------------------------------------
# Sampling SQL per dialect
# ----------------------------------------------------------------------

KEY_FOUND = stats(5000, (5000, 0), (800, 0), (4, 0))

BQ_VIEW = ORDERS_VIEW.replace("schema: shop\n", "catalog: acme_project\nschema: shop\n")


def bq_client(columns, partitions, data=KEY_FOUND):
    return FakeClient("bigquery", rules=[
        (r"INFORMATION_SCHEMA\.COLUMNS", columns),
        (r"INFORMATION_SCHEMA\.PARTITIONS", partitions),
        ("total_rows", data),
    ])


class TestSnowflake:
    def test_limit_subquery(self, tmp_path):
        client = FakeClient(rules=[("total_rows", KEY_FOUND)])
        result = examine(write_view(tmp_path, "orders"), client, sample_rows=250000)
        assert client.sql == [
            'select count(*) as total_rows, '
            'count(distinct "ID") as distinct_0, sum(case when "ID" is null then 1 else 0 end) as nulls_0, '
            'count(distinct "USER_ID") as distinct_1, sum(case when "USER_ID" is null then 1 else 0 end) as nulls_1, '
            'count(distinct "STATUS") as distinct_2, sum(case when "STATUS" is null then 1 else 0 end) as nulls_2 '
            'from (select "ID", "USER_ID", "STATUS" from "SHOP"."ORDERS" limit 250000) as pk_sample'
        ]
        assert (result.strategy, result.filter) == ("limit", "limit 250000")

    def test_column_quoted_in_the_view_keeps_its_case(self, tmp_path):
        text = "schema: shop\ntable_name: orders\ndimensions:\n  id:\n    sql: '\"OrderId\"'\n"
        client = FakeClient(rules=[("total_rows", stats(5000, (5000, 0)))])
        examine(write_view(tmp_path, "orders", text), client)
        assert 'count(distinct "OrderId")' in client.sql[0]


class TestLimitDialects:
    @pytest.mark.parametrize("dialect", ["postgres", "duckdb", "motherduck", "some_new_warehouse"])
    def test_limit_subquery_with_double_quotes(self, tmp_path, dialect):
        client = FakeClient(dialect, rules=[("total_rows", KEY_FOUND)])
        result = examine(write_view(tmp_path, "orders"), client, dialect=dialect)
        assert client.sql[0].endswith(
            'from (select "id", "user_id", "status" from "shop"."orders" limit 1000000) as pk_sample')
        assert result.strategy == "limit"

    def test_unknown_dialect_is_flagged(self):
        assert pk.get_dialect("some_new_warehouse")[1] is False
        assert pk.get_dialect("BigQuery") == (pk.DIALECTS["bigquery"], True)

    def test_catalog_is_part_of_the_table_name(self, tmp_path):
        client = FakeClient("postgres", rules=[("total_rows", KEY_FOUND)])
        examine(write_view(tmp_path, "orders", BQ_VIEW), client, dialect="postgres")
        assert 'from "acme_project"."shop"."orders" limit' in client.sql[0]


class TestBigQuery:
    PARTITIONS = [
        {"partition_id": "20260101", "total_rows": 400000},
        {"partition_id": "20260103", "total_rows": 300000},
        {"partition_id": "20260102", "total_rows": 500000},
        {"partition_id": "__NULL__", "total_rows": 7},
        {"partition_id": "20251231", "total_rows": 900000},
    ]

    def test_partition_filter_on_most_recent_partitions(self, tmp_path):
        client = bq_client([{"column_name": "created_at", "data_type": "TIMESTAMP"}], self.PARTITIONS)
        result = examine(write_view(tmp_path, "orders", BQ_VIEW), client, dialect="bigquery", sample_rows=1000000)
        # 0103 (300k) + 0102 (500k) + 0101 (400k) reaches the 1,000,000 row target
        assert client.sql[0] == ("select column_name, data_type from `acme_project`.`shop`.INFORMATION_SCHEMA.COLUMNS "
                                 "where table_name = 'orders' and is_partitioning_column = 'YES'")
        assert client.sql[1] == ("select partition_id, total_rows from "
                                 "`acme_project`.`shop`.INFORMATION_SCHEMA.PARTITIONS where table_name = 'orders'")
        assert client.sql[2].endswith(
            "from (select `id`, `user_id`, `status` from `acme_project`.`shop`.`orders` "
            "where `created_at` >= timestamp '2026-01-01 00:00:00') as pk_sample")
        assert result.strategy == "partition_filter"
        assert result.filter == "`created_at` >= timestamp '2026-01-01 00:00:00'"
        assert result.status == pk.STATUS_KEY_FOUND

    def test_never_a_bare_limit(self, tmp_path):
        client = bq_client([{"column_name": "created_at", "data_type": "TIMESTAMP"}], self.PARTITIONS)
        examine(write_view(tmp_path, "orders", BQ_VIEW), client, dialect="bigquery")
        client2 = bq_client([], [{"partition_id": None, "total_rows": 1000}])
        examine(write_view(tmp_path, "orders", BQ_VIEW), client2, dialect="bigquery")
        for sql in client.sql + client2.sql:
            assert "limit" not in sql.lower()

    def test_one_partition_is_enough_when_it_meets_the_target(self, tmp_path):
        client = bq_client([{"column_name": "order_date", "data_type": "DATE"}], self.PARTITIONS)
        result = examine(write_view(tmp_path, "orders", BQ_VIEW), client, dialect="bigquery", sample_rows=1000)
        assert result.filter == "`order_date` >= date '2026-01-03'"

    @pytest.mark.parametrize("data_type, partition_id, literal", [
        ("DATE", "202601", "date '2026-01-01'"),
        ("DATETIME", "2026010215", "datetime '2026-01-02 15:00:00'"),
        ("TIMESTAMP", "2026", "timestamp '2026-01-01 00:00:00'"),
        ("INT64", "4000", "4000"),
    ])
    def test_partition_granularities(self, tmp_path, data_type, partition_id, literal):
        client = bq_client([{"column_name": "part_col", "data_type": data_type}],
                           [{"partition_id": partition_id, "total_rows": 5000}])
        result = examine(write_view(tmp_path, "orders", BQ_VIEW), client, dialect="bigquery")
        assert result.filter == f"`part_col` >= {literal}"

    def test_ingestion_time_partitioning_uses_partitiontime(self, tmp_path):
        client = bq_client([], self.PARTITIONS[:3])
        result = examine(write_view(tmp_path, "orders", BQ_VIEW), client, dialect="bigquery", sample_rows=1000)
        assert result.filter == "`_PARTITIONTIME` >= timestamp '2026-01-03 00:00:00'"

    def test_unpartitioned_above_threshold_needs_a_decision(self, tmp_path):
        client = bq_client([], [{"partition_id": None, "total_rows": 5000001}])
        result = examine(write_view(tmp_path, "orders", BQ_VIEW), client, dialect="bigquery")
        assert result.status == pk.STATUS_NEEDS_DECISION
        assert "5,000,001 rows" in result.message and "--allow-full-scan" in result.message
        assert client.data_sql == [], "the table must not be scanned"
        assert result.rows_checked is None

    def test_threshold_is_configurable(self, tmp_path):
        client = bq_client([], [{"partition_id": None, "total_rows": 5001}])
        result = examine(write_view(tmp_path, "orders", BQ_VIEW), client, dialect="bigquery",
                         max_unpartitioned_rows=5000)
        assert result.status == pk.STATUS_NEEDS_DECISION

    def test_unpartitioned_below_threshold_is_read_in_full(self, tmp_path):
        client = bq_client([], [{"partition_id": None, "total_rows": 5000}])
        result = examine(write_view(tmp_path, "orders", BQ_VIEW), client, dialect="bigquery")
        assert client.sql[2].endswith(
            "from (select `id`, `user_id`, `status` from `acme_project`.`shop`.`orders`) as pk_sample")
        assert (result.strategy, result.filter, result.status) == ("full_scan", None, pk.STATUS_KEY_FOUND)

    def test_allow_full_scan_overrides_the_guard(self, tmp_path):
        client = bq_client([], [{"partition_id": None, "total_rows": 90000000}])
        result = examine(write_view(tmp_path, "orders", BQ_VIEW), client, dialect="bigquery", allow_full_scan=True)
        assert result.status == pk.STATUS_KEY_FOUND and result.strategy == "full_scan"

    def test_unreadable_metadata_needs_a_decision(self, tmp_path):
        client = FakeClient("bigquery", rules=[("INFORMATION_SCHEMA", pk.OmniQueryError("Access Denied")),
                                               ("total_rows", KEY_FOUND)])
        result = examine(write_view(tmp_path, "orders", BQ_VIEW), client, dialect="bigquery")
        assert result.status == pk.STATUS_NEEDS_DECISION
        assert "Access Denied" in result.message and client.data_sql == []

    def test_dataset_without_catalog(self, tmp_path):
        client = bq_client([], [{"partition_id": None, "total_rows": 5000}])
        examine(write_view(tmp_path, "orders"), client, dialect="bigquery")
        assert "from `shop`.INFORMATION_SCHEMA.COLUMNS" in client.sql[0]
        assert "from `shop`.`orders`)" in client.sql[2]


class TestRedshift:
    def rules(self, sortkey, data_type="timestamp without time zone"):
        return [
            ("svv_table_info", [{"sortkey1": sortkey}]),
            ("svv_columns", [{"data_type": data_type}]),
            ("total_rows", KEY_FOUND),
        ]

    def test_sort_key_window(self, tmp_path):
        client = FakeClient("redshift", rules=self.rules("created_at"))
        result = examine(write_view(tmp_path, "orders"), client, dialect="redshift", sample_rows=200000)
        assert client.sql[0] == """select sortkey1 from svv_table_info where "schema" = 'shop' and "table" = 'orders'"""
        assert client.sql[1] == ("select data_type from svv_columns where table_schema = 'shop' "
                                 "and table_name = 'orders' and column_name = 'created_at'")
        assert client.sql[2].endswith(
            'from (select "id", "user_id", "status" from "shop"."orders" '
            'where "created_at" >= dateadd(day, -30, current_date) limit 200000) as pk_sample')
        assert result.strategy == "sort_key_window"
        assert result.filter == '"created_at" >= dateadd(day, -30, current_date) limit 200000'

    def test_window_days_changes_the_window(self, tmp_path):
        client = FakeClient("redshift", rules=self.rules("AUTO(SORTKEY(order_date))", "date"))
        result = examine(write_view(tmp_path, "orders"), client, dialect="redshift", window_days=7)
        assert '"order_date" >= dateadd(day, -7, current_date)' in result.filter

    @pytest.mark.parametrize("sortkey, data_type", [("user_id", "bigint"), (None, ""), ("INTERLEAVED", "")])
    def test_falls_back_to_limit_without_a_date_sort_key(self, tmp_path, sortkey, data_type):
        client = FakeClient("redshift", rules=self.rules(sortkey, data_type))
        result = examine(write_view(tmp_path, "orders"), client, dialect="redshift")
        assert client.sql[-1].endswith('from "shop"."orders" limit 1000000) as pk_sample')
        assert result.strategy == "limit"

    def test_falls_back_to_limit_when_the_lookup_fails(self, tmp_path):
        client = FakeClient("redshift", rules=[("svv_table_info", pk.OmniQueryError("permission denied")),
                                               ("total_rows", KEY_FOUND)])
        result = examine(write_view(tmp_path, "orders"), client, dialect="redshift")
        assert result.strategy == "limit" and result.status == pk.STATUS_KEY_FOUND


class TestDateColumnOverride:
    def test_snowflake(self, tmp_path):
        client = FakeClient(rules=[("total_rows", KEY_FOUND)])
        result = examine(write_view(tmp_path, "orders"), client, date_column="created_at", window_days=14,
                         sample_rows=5000)
        assert client.sql[0].endswith(
            'from "SHOP"."ORDERS" where "CREATED_AT" >= dateadd(day, -14, current_date) limit 5000) as pk_sample')
        assert result.strategy == "date_window"

    def test_postgres(self, tmp_path):
        client = FakeClient("postgres", rules=[("total_rows", KEY_FOUND)])
        examine(write_view(tmp_path, "orders"), client, dialect="postgres", date_column="created_at", window_days=3)
        assert """where "created_at" >= current_date - interval '3 days' limit 1000000)""" in client.sql[0]

    def test_redshift_override_wins_over_the_sort_key(self, tmp_path):
        client = FakeClient("redshift", rules=[("total_rows", KEY_FOUND)])
        result = examine(write_view(tmp_path, "orders"), client, dialect="redshift", date_column="loaded_at")
        assert len(client.sql) == 1, "no sort key lookup when the user chose the column"
        assert '"loaded_at" >= dateadd(day, -30, current_date)' in client.sql[0]
        assert result.strategy == "date_window"

    @pytest.mark.parametrize("data_type, predicate", [
        ("TIMESTAMP", "`event_time` >= timestamp_sub(current_timestamp(), interval 7 day)"),
        ("DATE", "`event_time` >= date_sub(current_date(), interval 7 day)"),
        ("DATETIME", "`event_time` >= datetime_sub(current_datetime(), interval 7 day)"),
        (None, "date(`event_time`) >= date_sub(current_date(), interval 7 day)"),
    ])
    def test_bigquery_override_wins_over_partition_lookup(self, tmp_path, data_type, predicate):
        columns = [{"data_type": data_type}] if data_type else pk.OmniQueryError("nope")
        client = FakeClient("bigquery", rules=[(r"INFORMATION_SCHEMA\.COLUMNS", columns), ("total_rows", KEY_FOUND)])
        result = examine(write_view(tmp_path, "orders", BQ_VIEW), client, dialect="bigquery",
                         date_column="event_time", window_days=7)
        assert not any("PARTITIONS" in s or "is_partitioning_column" in s for s in client.sql)
        assert client.sql[-1].endswith(f"from `acme_project`.`shop`.`orders` where {predicate}) as pk_sample")
        assert "limit" not in client.sql[-1]
        assert (result.strategy, result.filter) == ("date_window", predicate)


# ----------------------------------------------------------------------
# Skips and identifier safety
# ----------------------------------------------------------------------

class TestSkips:
    def skip_reason(self, tmp_path, text, name="orders", suffix=".view.yaml"):
        client = FakeClient()
        result = examine(write_view(tmp_path, name, text, suffix), client)
        assert result.status == pk.STATUS_SKIPPED
        assert client.sql == [], "a skipped view is never queried"
        return result.message

    def test_template_view(self, tmp_path):
        assert "template" in self.skip_reason(tmp_path, "template: true\n" + ORDERS_VIEW)

    def test_extends_only_view(self, tmp_path):
        text = "extends: [orders]\ndimensions:\n  region: {}\n"
        assert "extends another view" in self.skip_reason(tmp_path, text, name="orders_emea")

    def test_query_view_by_file_name(self, tmp_path):
        text = "sql: select user_id, count(*) as orders from shop.orders group by 1\ndimensions:\n  user_id: {}\n"
        assert "SQL query view" in self.skip_reason(tmp_path, text, name="user_facts", suffix=".query.view")

    def test_query_view_by_content(self, tmp_path):
        text = "schema: shop\ntable_name: x\nsql: select 1 as id\ndimensions:\n  id: {}\n"
        assert "SQL query view" in self.skip_reason(tmp_path, text)

    def test_view_with_a_key_is_not_examined(self, tmp_path):
        text = ORDERS_VIEW.replace("    label: Id\n", "    label: Id\n    primary_key: true\n")
        assert "already has a primary key (id)" in self.skip_reason(tmp_path, text)

    @pytest.mark.parametrize("key, value", [
        ("table_name", "orders; drop table users"),
        ("table_name", "orders where 1=1"),
        ("schema", 'shop"."x'),
        ("schema", "shop-prod"),
        ("catalog", "`proj`"),
        ("table_name", "orders..x"),
    ])
    def test_bad_identifier(self, tmp_path, key, value):
        lines = {"schema": "shop", "table_name": "orders", key: value}
        text = "".join(f"{k}: {json.dumps(v)}\n" for k, v in lines.items()) + "dimensions:\n  id: {}\n"
        assert f"unsafe identifier in {key}" in self.skip_reason(tmp_path, text)

    def test_dotted_schema_is_split_and_accepted(self, tmp_path):
        text = "schema: analytics.shop\ntable_name: orders\ndimensions:\n  id: {}\n"
        assert pk.load_view(write_view(tmp_path, "orders", text)).table.parts == ["analytics", "shop", "orders"]

    def test_unsafe_column_names_never_reach_sql(self, tmp_path):
        text = ('schema: shop\ntable_name: orders\ndimensions:\n  id: {}\n'
                '  "x) from t; --": {}\n  evil:\n    sql: "id from other; --"\n')
        client = FakeClient(rules=[("total_rows", stats(5000, (5000, 0)))])
        examine(write_view(tmp_path, "orders", text), client)
        assert client.sql[0].endswith('from (select "ID" from "SHOP"."ORDERS" limit 1000000) as pk_sample')

    def test_quote_refuses_unsafe_names(self):
        for dialect in set(pk.DIALECTS.values()):
            with pytest.raises(ValueError):
                dialect.quote("a`b")

    def test_malformed_yaml_is_skipped(self, tmp_path):
        assert "could not read" in self.skip_reason(tmp_path, "schema: [unclosed\n")

    def test_view_without_plain_columns(self, tmp_path):
        text = "schema: shop\ntable_name: orders\ndimensions:\n  total:\n    sql: a + b\n"
        client = FakeClient()
        result = examine(write_view(tmp_path, "orders", text), client)
        assert result.status == pk.STATUS_NO_KEY_FOUND and client.sql == []

    def test_all_sql_is_read_only_select(self, tmp_path):
        client = FakeClient("redshift", rules=[
            ("svv_table_info", [{"sortkey1": "created_at"}]), ("svv_columns", [{"data_type": "date"}]),
            ("pair_0", pair_stats(5000, 1, 1, 1)), ("total_rows", stats(5000, (2500, 0), (800, 0), (4, 0)))])
        examine(write_view(tmp_path, "orders"), client, dialect="redshift")
        assert len(client.sql) == 4
        for sql in client.sql:
            assert sql.lower().startswith("select ") and ";" not in sql
        with pytest.raises(ValueError):
            pk.assert_read_only("delete from orders")


# ----------------------------------------------------------------------
# Writing
# ----------------------------------------------------------------------

class TestWriting:
    def test_dry_run_writes_no_view_file(self, tmp_path):
        path = write_view(tmp_path / "views", "orders")
        client = FakeClient(rules=[("total_rows", KEY_FOUND)])
        assert run_cli(tmp_path, client) == 0
        assert path.read_text() == ORDERS_VIEW
        assert sorted(p.name for p in (tmp_path / "views").iterdir()) == ["orders.view.yaml"]
        record = read_json(tmp_path)["views"][0]
        assert (record["status"], record["chosen_key"], record["applied"]) == ("key_found", "id", False)

    def test_apply_changes_only_the_key_dimension(self, tmp_path):
        path = write_view(tmp_path / "views", "orders")
        client = FakeClient(rules=[("total_rows", KEY_FOUND)])
        assert run_cli(tmp_path, client, "--apply") == 0
        expected = ORDERS_VIEW.replace(
            "    label: Id\n    tags: [id, mig_phase_0, verified]\n",
            "    label: Id\n    tags: [id, mig_phase_0, verified, primary_key_from_sample]\n    primary_key: true\n")
        assert expected != ORDERS_VIEW
        assert path.read_text() == expected  # comments, blank lines and key order survive
        assert read_json(tmp_path)["views"][0]["applied"] is True
        assert not list((tmp_path / "views").glob(".*")), "no temp files left behind"

    def test_apply_to_an_empty_dimension(self, tmp_path):
        text = "# header\nschema: shop\ntable_name: orders\n\ndimensions:\n  status: {}\n  id: {}\n  # trailing\n"
        out = pk.render_with_primary_key(text, "id")
        assert out == text.replace("  id: {}\n", "  id:\n    primary_key: true\n    tags: [primary_key_from_sample]\n")

    def test_apply_to_a_null_dimension_and_block_tags(self, tmp_path):
        text = "schema: shop\ntable_name: orders\ndimensions:\n  id:\n  ref:\n    tags:\n      - a\n      - b\n"
        assert pk.render_with_primary_key(text, "id").endswith(
            "  id:\n    primary_key: true\n    tags: [primary_key_from_sample]\n"
            "  ref:\n    tags:\n      - a\n      - b\n")
        assert pk.render_with_primary_key(text, "ref").endswith(
            "    tags:\n      - a\n      - b\n      - primary_key_from_sample\n    primary_key: true\n")

    def test_hand_written_layout_is_left_byte_for_byte(self):
        # spacing a YAML round trip would normalise: `[ pii ]`, `not: null`, a wrapped description
        text = (
            "schema: shop\ntable_name: users\n\ndimensions:\n"
            "  email:\n    description: Customer email address,\n      wrapped onto a second line\n"
            "    tags: [ pii ]   # sensitive\n    required_access_grants: [ can_view_pii ]\n\n"
            "  # the natural key\n  id:\n    format: id\n\n"
            "measures:\n  with_email:\n    aggregate_type: count\n    filters:\n      email:\n        not: null\n"
        )
        assert pk.render_with_primary_key(text, "id") == text.replace(
            "    format: id\n", "    format: id\n    primary_key: true\n    tags: [primary_key_from_sample]\n")
        assert pk.render_with_primary_key(text, "email") == text.replace(
            "    tags: [ pii ]   # sensitive\n    required_access_grants: [ can_view_pii ]\n",
            "    tags: [ pii, primary_key_from_sample ]   # sensitive\n"
            "    required_access_grants: [ can_view_pii ]\n    primary_key: true\n")

    def test_unusual_layout_falls_back_to_a_round_trip(self):
        text = "schema: s\ntable_name: t\ndimensions:\n  # keep me\n  id: {format: id}\n  ref: {}\n"
        out = pk.render_with_primary_key(text, "id")
        assert "# keep me" in out and out.index("id:") < out.index("ref:")
        assert pk.load_view(write_view(Path(self._tmp), "t", out)).existing_key == "id"

    @pytest.fixture(autouse=True)
    def _keep_tmp(self, tmp_path):
        self._tmp = tmp_path

    def test_apply_is_idempotent_about_the_tag(self):
        text = "schema: s\ntable_name: t\ndimensions:\n  id:\n    tags: [primary_key_from_sample]\n"
        assert pk.render_with_primary_key(text, "id").count("primary_key_from_sample") == 1

    def test_never_adds_a_second_key(self, tmp_path):
        text = "schema: s\ntable_name: t\ndimensions:\n  id:\n    primary_key: true\n  ref: {}\n"
        with pytest.raises(ValueError):
            pk.render_with_primary_key(text, "ref")

    def test_nothing_written_when_nothing_qualified(self, tmp_path):
        path = write_view(tmp_path / "views", "orders")
        for data in (stats(5000, (4000, 0), (800, 0), (1, 0)),      # no key
                     stats(10, (10, 0), (10, 0), (1, 0))):          # sample too small
            client = FakeClient(rules=[("pair_0", pair_stats(5000, 1)), ("total_rows", data)])
            assert run_cli(tmp_path, client, "--apply") == 0
            assert path.read_text() == ORDERS_VIEW

    def test_recheck_reports_and_never_replaces_an_existing_key(self, tmp_path):
        text = ORDERS_VIEW.replace("    label: User\n", "    label: User\n    primary_key: true\n")
        path = write_view(tmp_path / "views", "orders", text)
        # candidates with --recheck: user_id (existing key) first, then id, status
        client = FakeClient(rules=[("total_rows", stats(5000, (800, 0), (5000, 0), (4, 0)))])
        assert run_cli(tmp_path, client, "--apply", "--recheck") == 0
        assert path.read_text() == text
        record = read_json(tmp_path)["views"][0]
        assert record["status"] == "no_key_found" and record["existing_key"] == "user_id"
        assert "existing primary key user_id is NOT a key" in record["message"]
        assert record["qualifying"] == ["id"] and record["applied"] is False

        client = FakeClient(rules=[("total_rows", stats(5000, (5000, 0), (5000, 0), (4, 0)))])
        assert run_cli(tmp_path, client, "--apply", "--recheck") == 0
        record = read_json(tmp_path)["views"][0]
        assert (record["status"], record["chosen_key"], record["applied"]) == ("key_found", "user_id", False)
        assert path.read_text() == text


# ----------------------------------------------------------------------
# CLI: runs, reports, exit codes
# ----------------------------------------------------------------------

@pytest.fixture
def repo(tmp_path):
    """orders (key), events (query error), users (has key), a template, an extends-only view, a query view"""
    views = tmp_path / "views"
    write_view(views, "orders")
    write_view(views / "nested", "events", ORDERS_VIEW.replace("table_name: orders", "table_name: events"))
    write_view(views, "users", "schema: shop\ntable_name: users\ndimensions:\n  id:\n    primary_key: true\n")
    write_view(views, "base", "template: true\nschema: shop\ntable_name: base\ndimensions:\n  id: {}\n")
    write_view(views, "orders_emea", "extends: [orders]\n")
    write_view(views, "user_facts", "sql: select 1 as id\ndimensions:\n  id: {}\n", suffix=".query.view")
    write_view(views, "bad", 'schema: shop\ntable_name: "x; drop table y"\ndimensions:\n  id: {}\n')
    (views / "orders.topic.yaml").write_text("base_view: orders\n")
    return tmp_path


def repo_client():
    return FakeClient(rules=[
        (r'"EVENTS"', pk.OmniQueryError("SQL compilation error: object does not exist")),
        ("total_rows", KEY_FOUND),
    ])


class TestCli:
    def test_query_error_on_one_view_does_not_stop_the_run(self, repo, capsys):
        client = repo_client()
        assert run_cli(repo, client) == 0
        by_view = {r["view"]: r for r in read_json(repo)["views"]}
        assert by_view["events"]["status"] == "query_error"
        assert "object does not exist" in by_view["events"]["message"]
        assert by_view["orders"]["status"] == "key_found"
        assert "events: query_error" in capsys.readouterr().out

    def test_statuses_and_skips(self, repo):
        assert run_cli(repo, repo_client()) == 0
        data = read_json(repo)
        assert {r["view"]: r["status"] for r in data["views"]} == {
            "orders": "key_found", "events": "query_error", "users": "skipped", "base": "skipped",
            "orders_emea": "skipped", "user_facts": "skipped", "bad": "skipped",
        }
        assert data["summary"] == {"key_found": 1, "multiple_candidates": 0, "pair_suggested": 0, "no_key_found": 0, "sample_too_small": 0,
                                   "needs_decision": 0, "query_error": 1, "skipped": 5}

    def test_json_report_shape(self, repo):
        assert run_cli(repo, repo_client(), "--branch-id", "branch-9") == 0
        data = read_json(repo)
        assert set(data) == {"model_id", "branch_id", "dialect", "mode", "note", "summary", "views"}
        assert (data["model_id"], data["branch_id"], data["dialect"], data["mode"]) == (
            "model-1", "branch-9", "snowflake", "dry_run")
        record = next(r for r in data["views"] if r["view"] == "orders")
        assert set(record) == {"view", "file", "table", "dialect", "strategy", "filter", "rows_checked", "candidates",
                               "qualifying", "chosen_key", "key_source", "existing_key", "pair_suggestions", "status", "message",
                               "applied"}
        assert record["file"].endswith("orders.view.yaml")
        assert (record["table"], record["dialect"], record["strategy"], record["filter"], record["rows_checked"]) == (
            "shop.orders", "snowflake", "limit", "limit 1000000", 5000)
        assert record["candidates"][0] == {"name": "id", "column": "id", "evidence": "key_like_name",
                                           "distinct_count": 5000, "null_count": 0, "qualifies": True, "note": None}
        assert record["chosen_key"] == "id"
        skipped = next(r for r in data["views"] if r["view"] == "user_facts")
        assert skipped["chosen_key"] is None and skipped["rows_checked"] is None and skipped["candidates"] == []

    def test_markdown_report(self, repo):
        assert run_cli(repo, repo_client()) == 0
        report = (repo / "out" / "primary_keys.md").read_text()
        assert "A sample can prove that a column is NOT unique" in report
        assert "| `orders` | `id` | the only column unique in the sample | 5,000 rows (limit: limit 1000000) | no |" in report
        assert "| `user_facts` |" in report and "SQL query view" in report
        assert "| `events` | `query_error` | not queried |" in report
        assert "dry run" in report
        assert "verified" not in report.lower()
        assert chr(0x2014) not in report and " - " not in report

    def test_view_flag_selects_views(self, repo):
        client = repo_client()
        assert run_cli(repo, client, "--view", "orders", "--view", "users") == 0
        assert [r["view"] for r in read_json(repo)["views"]] == ["orders", "users"]
        assert len(client.sql) == 1

    def test_default_report_path_is_the_current_directory(self, repo, monkeypatch):
        monkeypatch.chdir(repo)
        code = cli.main(["--model-id", "m", "--omni-repo", str(repo / "views"), "--view", "orders"],
                        client=repo_client())
        assert code == 0 and (repo / "primary_keys.md").exists()

    def test_dialect_flag_skips_the_lookup(self, repo):
        client = FakeClient(pk.OmniConnectionError("should not be called"), rules=[("total_rows", KEY_FOUND)])
        assert run_cli(repo, client, "--view", "orders", "--dialect", "postgres") == 0
        assert client.dialect_calls == 0 and '"shop"."orders"' in client.sql[0]

    def test_unknown_dialect_warns(self, repo, capsys):
        client = FakeClient("newdb", rules=[("total_rows", KEY_FOUND)])
        assert run_cli(repo, client, "--view", "orders") == 0
        assert "Warning: unknown dialect 'newdb'" in capsys.readouterr().out

    def test_cli_flags_reach_the_sql(self, repo):
        client = repo_client()
        assert run_cli(repo, client, "--view", "orders", "--date-column", "created_at", "--window-days", "7",
                       "--sample-rows", "1234", "--max-candidates", "2") == 0
        assert client.sql[0].endswith('from (select "ID", "USER_ID" from "SHOP"."ORDERS" '
                                      'where "CREATED_AT" >= dateadd(day, -7, current_date) limit 1234) as pk_sample')


class TestExitCodes:
    def test_exit_0_even_when_no_view_has_a_key(self, repo):
        client = FakeClient(rules=[("pair_0", pair_stats(5000, 1, 1, 1)),
                                   ("total_rows", stats(5000, (10, 0), (10, 0), (4, 0)))])
        assert run_cli(repo, client) == 0

    def test_exit_1_missing_repo(self, tmp_path, capsys):
        assert run_cli(tmp_path, FakeClient()) == 1
        assert "not found" in capsys.readouterr().out

    def test_exit_1_no_view_files(self, tmp_path):
        (tmp_path / "views").mkdir()
        assert run_cli(tmp_path, FakeClient()) == 1

    def test_exit_1_unknown_view(self, repo, capsys):
        assert run_cli(repo, FakeClient(), "--view", "nope") == 1
        assert "view(s) not found" in capsys.readouterr().out

    def test_exit_1_when_the_dialect_cannot_be_read(self, repo):
        assert run_cli(repo, FakeClient(pk.OmniConnectionError("401 unauthorized"))) == 1
        assert not (repo / "out").exists()

    def test_exit_1_when_manual_sql_is_restricted(self, repo, capsys):
        client = FakeClient(rules=[("total_rows", pk.OmniConnectionError("manually written SQL is restricted"))])
        assert run_cli(repo, client, "--apply") == 1
        assert "manually written SQL" in capsys.readouterr().out
        assert (repo / "views" / "orders.view.yaml").read_text() == ORDERS_VIEW

    @pytest.mark.parametrize("argv", [
        [],                                              # --model-id is required
        ["--model-id", "m", "--bogus"],
        ["--model-id", "m", "--sample-rows", "0"],
        ["--model-id", "m", "--sample-rows", "many"],
        ["--model-id", "m", "--date-column", "created_at; drop table x"],
        ["--model-id", "m", "--min-rows", "-1"],
    ])
    def test_exit_2_invalid_arguments(self, argv):
        with pytest.raises(SystemExit) as excinfo:
            cli.main(argv, client=FakeClient())
        assert excinfo.value.code == 2

    def test_script_runs_as_a_subprocess(self, tmp_path):
        """The real entry point, stopped before any Omni call by a missing repo (exit 1) or bad flags (exit 2)"""
        import sys
        script = SKILL_ROOT / "scripts" / "find_primary_keys.py"
        missing = subprocess.run([sys.executable, str(script), "--model-id", "m", "--omni-repo", "nope"],
                                 cwd=str(tmp_path), capture_output=True, text=True)
        assert missing.returncode == 1 and "not found" in missing.stdout
        bad = subprocess.run([sys.executable, str(script)], cwd=str(tmp_path), capture_output=True, text=True)
        assert bad.returncode == 2
        assert list(tmp_path.iterdir()) == []


# ----------------------------------------------------------------------
# OmniQueryClient (the CLI wrapper), with a fake subprocess runner
# ----------------------------------------------------------------------

class FakeRunner:
    def __init__(self, *outputs):
        self.outputs = list(outputs)
        self.commands = []

    def __call__(self, command, **kwargs):
        self.commands.append(command)
        output = self.outputs.pop(0)
        if isinstance(output, Exception):
            raise output
        stdout, stderr, code = output
        return subprocess.CompletedProcess(command, code, stdout=stdout, stderr=stderr)


def ok(payload):
    return json.dumps(payload), "", 0


class TestOmniQueryClient:
    def test_run_sql_builds_the_request(self):
        runner = FakeRunner(ok([{"total_rows": 3}]))
        client = pk.OmniQueryClient("model-1", branch_id="branch-9", profile="staging",
                                    base_url="https://example.omniapp.co", runner=runner)
        assert client.run_sql("select 1") == [{"total_rows": 3}]
        command = runner.commands[0]
        assert command[:4] == ["omni", "query", "run", "--body"]
        assert command[5:] == ["-p", "staging", "--base-url", "https://example.omniapp.co"]
        assert json.loads(command[4]) == {
            "query": {"modelId": "model-1", "fields": [], "userEditedSQL": "select 1", "rewriteSql": False},
            "branchId": "branch-9", "resultType": "json",
        }

    def test_branch_and_flags_are_optional(self):
        runner = FakeRunner(ok([]))
        pk.OmniQueryClient("model-1", runner=runner).run_sql("select 1")
        assert len(runner.commands[0]) == 5 and "branchId" not in json.loads(runner.commands[0][4])

    def test_error_body_is_a_query_error(self):
        runner = FakeRunner(ok({"detail": "Table not found", "status": 400}))
        with pytest.raises(pk.OmniQueryError, match="Table not found") as excinfo:
            pk.OmniQueryClient("m", runner=runner).run_sql("select 1")
        assert not isinstance(excinfo.value, pk.OmniConnectionError)

    def test_error_on_stderr_with_exit_code(self):
        runner = FakeRunner(("", json.dumps({"detail": "bad sql", "status": 400}), 1))
        with pytest.raises(pk.OmniQueryError, match="bad sql"):
            pk.OmniQueryClient("m", runner=runner).run_sql("select 1")

    def test_job_body_with_error(self):
        runner = FakeRunner(ok({"jobs_submitted": {}, "error": "Syntax error at line 1", "status": "FAILED"}))
        with pytest.raises(pk.OmniQueryError, match="Syntax error"):
            pk.OmniQueryClient("m", runner=runner).run_sql("select 1")

    def test_manual_sql_restriction_stops_the_run(self):
        runner = FakeRunner(ok({"detail": "Manually written SQL is restricted for this role", "status": 400}))
        with pytest.raises(pk.OmniConnectionError, match="allowed to run manually written SQL"):
            pk.OmniQueryClient("m", runner=runner).run_sql("select 1")

    def test_auth_failure_and_missing_cli_stop_the_run(self):
        with pytest.raises(pk.OmniConnectionError):
            pk.OmniQueryClient("m", runner=FakeRunner(ok({"detail": "Forbidden", "status": 403}))).run_sql("select 1")
        with pytest.raises(pk.OmniConnectionError, match="Omni CLI not found"):
            pk.OmniQueryClient("m", runner=FakeRunner(FileNotFoundError("omni"))).run_sql("select 1")

    def test_non_json_output_is_an_error(self):
        with pytest.raises(pk.OmniQueryError, match="not JSON"):
            pk.OmniQueryClient("m", runner=FakeRunner(("oops", "", 0))).run_sql("select 1")

    @pytest.mark.parametrize("models_key, connections_key", [("records", "connections"), ("models", "records")])
    def test_dialect_lookup(self, models_key, connections_key):
        runner = FakeRunner(
            ok({models_key: [{"id": "other", "connectionId": "c0"}, {"id": "model-1", "connectionId": "c2"}]}),
            ok({connections_key: [{"id": "c1", "dialect": "snowflake", "name": "a"},
                                  {"id": "c2", "dialect": "BigQuery", "name": "b"}]}),
        )
        assert pk.OmniQueryClient("model-1", profile="p", runner=runner).dialect() == "bigquery"
        assert runner.commands[0] == ["omni", "models", "list", "--modelid", "model-1", "-p", "p"]
        assert runner.commands[1] == ["omni", "connections", "list", "-p", "p"]

    def test_dialect_lookup_failures_are_connection_errors(self):
        with pytest.raises(pk.OmniConnectionError, match="model model-1 not found"):
            pk.OmniQueryClient("model-1", runner=FakeRunner(ok({"records": []}))).dialect()
        runner = FakeRunner(ok({"records": [{"id": "model-1", "connectionId": "c9"}]}), ok({"connections": []}))
        with pytest.raises(pk.OmniConnectionError, match="connection c9"):
            pk.OmniQueryClient("model-1", runner=runner).dialect()
        with pytest.raises(pk.OmniConnectionError, match="dialect"):
            pk.OmniQueryClient("model-1", runner=FakeRunner(("boom", "", 1))).dialect()

    def test_cli_exit_1_when_the_omni_cli_is_missing(self, tmp_path):
        write_view(tmp_path / "views", "orders")
        client = pk.OmniQueryClient("model-1", runner=FakeRunner(FileNotFoundError("omni")))
        assert run_cli(tmp_path, client) == 1


class TestResultColumnNames:
    """Omni's JSON results label SQL columns as titles, and return counts as strings"""

    def test_title_cased_labels_are_matched(self):
        row = {"Total Rows": "5", "Distinct 0": "5", "Nulls 0": 0}
        assert pk._to_int(pk._value(row, "total_rows")) == 5
        assert pk._to_int(pk._value(row, "distinct_0")) == 5
        assert pk._to_int(pk._value(row, "nulls_0")) == 0

    def test_upper_case_names_are_matched(self):
        assert pk._value({"TOTAL_ROWS": 7}, "total_rows") == 7

    def test_a_missing_column_is_none(self):
        assert pk._value({"Total Rows": 1}, "distinct_0") is None

    def test_counts_with_thousands_separators_are_read(self):
        assert pk._to_int("1,096") == 1096
        assert pk._to_int("12,345,678,901,234") == 12345678901234
        assert pk._to_int("1096") == 1096
        assert pk._to_int(1096) == 1096
        assert pk._to_int(5.0) == 5
        assert pk._to_int(None) == 0

    @pytest.mark.parametrize("value", ["1.2M", "abc", "1.5", 1.5, True])
    def test_anything_that_is_not_a_whole_number_is_an_error(self, value):
        with pytest.raises(pk.OmniQueryError):
            pk._to_int(value)


# ----------------------------------------------------------------------
# Several qualifying columns: the script does not pick, the user does (--key)
# ----------------------------------------------------------------------

ALL_UNIQUE = stats(5000, (5000, 0), (5000, 0), (5000, 0))   # id, user_id and status all unique
ONLY_ID = stats(5000, (5000, 0), (800, 0), (4, 0))


class TestMultipleCandidates:
    def test_nothing_is_written_even_with_apply(self, tmp_path, capsys):
        path = write_view(tmp_path / "views", "orders")
        assert run_cli(tmp_path, FakeClient(rules=[("total_rows", ALL_UNIQUE)]), "--apply") == 0
        assert path.read_text() == ORDERS_VIEW
        data = read_json(tmp_path)
        record = data["views"][0]
        assert (record["status"], record["chosen_key"], record["applied"]) == ("multiple_candidates", None, False)
        assert record["qualifying"] == ["id", "user_id", "status"]
        assert data["summary"]["multiple_candidates"] == 1 and data["summary"]["key_found"] == 0
        out = capsys.readouterr().out
        assert "orders: multiple_candidates" in out and "--key VIEW=DIMENSION" in out

    def test_report_lists_every_qualifier_with_evidence(self, tmp_path):
        write_view(tmp_path / "views", "orders")
        assert run_cli(tmp_path, FakeClient(rules=[("total_rows", ALL_UNIQUE)])) == 0
        report = (tmp_path / "out" / "primary_keys.md").read_text()
        assert "## Several columns qualified (nothing written)" in report
        assert "| `multiple_candidates` | 1 |" in report
        assert "| `id` | key_like_name |  | `--key orders=id` |" in report
        assert "| `status` | plain_column |  | `--key orders=status` |" in report
        assert "## Keys found in the sample" not in report

    def test_key_flag_picks_one_and_dry_run_writes_nothing(self, tmp_path):
        path = write_view(tmp_path / "views", "orders")
        assert run_cli(tmp_path, FakeClient(rules=[("total_rows", ALL_UNIQUE)]), "--key", "orders=user_id") == 0
        assert path.read_text() == ORDERS_VIEW
        record = read_json(tmp_path)["views"][0]
        assert (record["status"], record["chosen_key"], record["key_source"], record["applied"]) == (
            "key_found", "user_id", "key_flag", False)
        assert "user_id (chosen with --key) is unique and not null in 5,000 rows" in record["message"]
        assert "Also unique in the sample: id, status" in record["message"]

    def test_key_flag_with_apply_writes_the_chosen_key(self, tmp_path):
        path = write_view(tmp_path / "views", "orders")
        client = FakeClient(rules=[("total_rows", ALL_UNIQUE)])
        assert run_cli(tmp_path, client, "--key", "orders=user_id", "--apply") == 0
        assert path.read_text() == ORDERS_VIEW.replace(
            "    label: User\n    tags: [mig_phase_0, verified]\n",
            "    label: User\n    tags: [mig_phase_0, verified, primary_key_from_sample]\n    primary_key: true\n")
        assert read_json(tmp_path)["views"][0]["applied"] is True
        report = (tmp_path / "out" / "primary_keys.md").read_text()
        assert "| `orders` | `user_id` | chosen with `--key` (unique in the sample) |" in report

    def test_key_that_did_not_qualify_is_refused(self, tmp_path):
        path = write_view(tmp_path / "views", "orders")
        # id and status unique, user_id has duplicates
        client = FakeClient(rules=[("total_rows", stats(5000, (5000, 0), (800, 0), (5000, 0)))])
        assert run_cli(tmp_path, client, "--key", "orders=user_id", "--apply") == 0
        assert path.read_text() == ORDERS_VIEW
        record = read_json(tmp_path)["views"][0]
        assert (record["status"], record["chosen_key"], record["applied"]) == ("multiple_candidates", None, False)
        assert "--key orders=user_id was refused: it is NOT a key: 800 distinct values and 0 nulls" in record["message"]

    def test_refused_key_also_blocks_the_only_qualifier(self, tmp_path):
        # the user asked for user_id; id is the only qualifier. Writing id would not be what they asked for
        path = write_view(tmp_path / "views", "orders")
        client = FakeClient(rules=[("total_rows", ONLY_ID)])
        assert run_cli(tmp_path, client, "--key", "orders=user_id", "--apply") == 0
        assert path.read_text() == ORDERS_VIEW
        record = read_json(tmp_path)["views"][0]
        assert (record["status"], record["chosen_key"], record["applied"]) == ("key_found", "id", False)
        assert "was refused" in record["message"] and "Nothing was written" in record["message"]

    def test_key_naming_an_untested_dimension_is_refused(self, tmp_path):
        path = write_view(tmp_path / "views", "orders")
        client = FakeClient(rules=[("total_rows", ALL_UNIQUE)])
        assert run_cli(tmp_path, client, "--key", "orders=total_with_tax", "--apply") == 0
        assert path.read_text() == ORDERS_VIEW
        assert "it is not one of the columns tested" in read_json(tmp_path)["views"][0]["message"]

    def test_key_never_qualifies_a_sample_that_is_too_small(self, tmp_path):
        path = write_view(tmp_path / "views", "orders")
        client = FakeClient(rules=[("total_rows", stats(10, (10, 0), (10, 0), (10, 0)))])
        assert run_cli(tmp_path, client, "--key", "orders=id", "--apply") == 0
        assert path.read_text() == ORDERS_VIEW
        assert read_json(tmp_path)["views"][0]["status"] == "sample_too_small"

    def test_key_for_a_view_not_in_the_run_is_an_input_error(self, tmp_path, capsys):
        write_view(tmp_path / "views", "orders")
        assert run_cli(tmp_path, FakeClient(), "--key", "nope=id") == 1
        assert "--key names view(s) that are not in this run: nope" in capsys.readouterr().out

    @pytest.mark.parametrize("extra", [["--key", "orders"], ["--key", "=id"],
                                       ["--key", "orders=id", "--key", "orders=user_id"]])
    def test_malformed_key_is_an_argument_error(self, extra):
        with pytest.raises(SystemExit) as excinfo:
            cli.main(["--model-id", "m", *extra], client=FakeClient())
        assert excinfo.value.code == 2


FACT_VIEW = """\
schema: shop
table_name: fact_sales
dimensions:
  transaction_id: {}
  date_key: {}
  store_id: {}
  customer_key: {}
  amount: {}
"""


class TestLikelyForeignKeyNote:
    @pytest.fixture
    def star(self, tmp_path):
        views = tmp_path / "views"
        write_view(views, "sales", FACT_VIEW)
        # another view whose primary key dimension has the same name
        write_view(views, "calendar",
                   "schema: shop\ntable_name: dim_date\ndimensions:\n  date_key:\n    primary_key: true\n")
        # name minus _id matches another view's name (plural ignored)
        write_view(views, "stores", "schema: shop\ntable_name: store_list\ndimensions:\n  code: {}\n")
        # name minus _key matches another view's table name with the dim_ prefix stripped
        write_view(views, "people", "schema: shop\ntable_name: dim_customer\ndimensions:\n  code: {}\n")
        return tmp_path

    def notes(self, tmp_path):
        record = next(r for r in read_json(tmp_path)["views"] if r["view"] == "sales")
        return record, {c["name"]: c["note"] for c in record["candidates"]}

    def test_notes(self, star):
        # transaction_id, date_key, store_id, customer_key unique; amount not
        data = stats(5000, (5000, 0), (5000, 0), (5000, 0), (5000, 0), (900, 0))
        assert run_cli(star, FakeClient(rules=[("total_rows", data)]), "--view", "sales") == 0
        record, notes = self.notes(star)
        assert notes == {
            "transaction_id": None,
            "date_key": "likely foreign key: it is the primary key of view calendar",
            "store_id": "likely foreign key: its name matches view stores",
            "customer_key": "likely foreign key: its name matches view people",
            "amount": None,
        }

    def test_note_is_advice_never_an_exclusion(self, star):
        data = stats(5000, (5000, 0), (5000, 0), (5000, 0), (5000, 0), (900, 0))
        assert run_cli(star, FakeClient(rules=[("total_rows", data)]), "--view", "sales") == 0
        record, _ = self.notes(star)
        # the flagged columns still count as qualifiers, so nothing is picked for the user
        assert record["status"] == "multiple_candidates"
        assert record["qualifying"] == ["transaction_id", "date_key", "store_id", "customer_key"]
        report = (star / "out" / "primary_keys.md").read_text()
        assert ("| `date_key` | key_like_name | likely foreign key: it is the primary key of view calendar | "
                "`--key sales=date_key` |") in report
        assert "| `transaction_id` | key_like_name |  | `--key sales=transaction_id` |" in report
        assert "| Dimension | Column | Evidence | Distinct | Nulls | Unique in sample | Note |" in report

    def test_a_flagged_column_can_still_be_the_only_qualifier_or_be_chosen(self, star):
        only_date_key = stats(5000, (900, 0), (5000, 0), (900, 0), (900, 0), (900, 0))
        assert run_cli(star, FakeClient(rules=[("pair_0", pair_stats(5000, 1)), ("total_rows", only_date_key)]),
                       "--view", "sales") == 0
        record, _ = self.notes(star)
        assert (record["status"], record["chosen_key"]) == ("key_found", "date_key")

    def test_a_bare_id_is_never_flagged(self, tmp_path):
        views = tmp_path / "views"
        write_view(views, "orders")
        write_view(views, "users", "schema: shop\ntable_name: users\ndimensions:\n  id:\n    primary_key: true\n")
        assert run_cli(tmp_path, FakeClient(rules=[("total_rows", ONLY_ID)]), "--view", "orders") == 0
        notes = {c["name"]: c["note"] for c in read_json(tmp_path)["views"][0]["candidates"]}
        assert notes == {"id": None, "user_id": "likely foreign key: its name matches view users", "status": None}

    def test_a_view_does_not_flag_its_own_key_name(self, tmp_path):
        views = tmp_path / "views"
        write_view(views, "stores", "schema: shop\ntable_name: dim_store\ndimensions:\n  store_key: {}\n  store_id: {}\n")
        write_view(views, "sales", FACT_VIEW)
        data = stats(5000, (5000, 0), (5000, 0))
        assert run_cli(tmp_path, FakeClient(rules=[("total_rows", data)]), "--view", "stores") == 0
        record = read_json(tmp_path)["views"][0]
        assert [c["note"] for c in record["candidates"]] == [None, None]
