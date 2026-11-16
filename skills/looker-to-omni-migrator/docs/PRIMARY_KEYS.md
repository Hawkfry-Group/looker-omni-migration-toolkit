# Primary Key Finder

`scripts/find_primary_keys.py` is an optional script, separate from the converter. It finds a primary key for Omni views that have none by testing candidate columns against a sample of the real data, through Omni's query API. By default it only reports. With `--apply` it writes the key into the view file.

## Why

An Omni view needs one dimension marked `primary_key: true`. Without it, aggregates fan out across joins: a sum over a view joined one-to-many is counted once per joined row. The converter copies `primary_key: yes` from LookML, but many Looker views never declared one, so many converted views have none (the migration report says `No primary key dimension in the final view`).

## When to run it

Run it after the converted views are loaded into an Omni branch and validate there, and some views still have no primary key. It is never run by the converter.

You need:

- The Omni CLI (`omni`) installed and configured with a profile or token.
- An Omni role that is allowed to run **manually written SQL**. The script sends raw `SELECT` statements; if the role may not run them, the first query fails and the run stops with exit code 1.
- The model id, and the branch id if the views only exist on a branch.

## What it does

For each view file under `--omni-repo` (searched recursively) that has no `primary_key: true` dimension:

1. **Picks candidates**, cheapest evidence first, up to `--max-candidates` (default 12):
   1. dimensions named `id`, or ending in `_id`, `_key`, `_uuid` or `_guid` (`id` first, then the table name plus `_id`)
   2. dimensions that a `count_distinct` measure in the view counts
   3. dimensions tagged `table_key`
   4. the remaining dimensions that read a plain column and do not look like a date or a boolean

   A dimension reads a plain column when it has no `sql:` (the column is the dimension name) or its `sql:` is a bare column name. A dimension with any other SQL expression is never a candidate.
2. **Plans the cheapest sample** for the warehouse (see the table below). On BigQuery and Redshift this reads table metadata first.
3. **Runs one query** over the sample: the total row count, and for every candidate its distinct count and null count.
4. **Decides.** A candidate qualifies when its distinct count equals the row count and it has no nulls, and the sample has at least `--min-rows` rows (default 1,000). A key is chosen only when exactly one column qualifies. If two or more qualify, the status is `multiple_candidates`: every qualifier is reported, nothing is chosen or written, and you choose with `--key` (see below).
5. **Falls back to pairs.** If no single column qualifies at all, one more query tests up to `--max-pairs` pairs (default 10) of the top candidates. A pair that is unique together is reported as a suggestion, with the concatenation you would add as a new dimension. A pair is never written.

## When several columns qualify

In a sample, more than one column is often unique. A fact table of a thousand rows can have a unique `transaction_id` and also a unique `date_key`, simply because the sample holds one row per day. A five row dimension can be unique on almost every column. The data cannot tell these apart, and any rule for picking one (best looking name, shortest name) will sometimes write the wrong key without anyone noticing. So the script does not pick. The evidence order above only decides the order in which candidates are tested and listed.

For a `multiple_candidates` view, the report lists each qualifying column with its evidence, a note, and the flag that would choose it. Decide which column identifies a row, then re-run with `--key VIEW=DIMENSION` (repeatable, one per view):

```bash
python scripts/find_primary_keys.py --model-id MODEL_ID --view sales --key sales=transaction_id
python scripts/find_primary_keys.py --model-id MODEL_ID --view sales --key sales=transaction_id --apply
```

- `--key` is accepted only if that dimension is unique and not null in the sample of this run. If it is not, the report says why and nothing is written for that view (not even another column that qualified).
- `--key` on its own is still a dry run. The key is written only with `--apply`.
- `--key` is ignored for a view that already has a primary key, and naming a view that is not in the run is an error (exit code 1).

**Likely foreign key.** To help you choose, a candidate gets the note "likely foreign key" when another view under `--omni-repo` already has a primary key dimension of the same name, or when its name without `_id` or `_key` matches another view's name or table name (a `dim_` or `fact_` prefix and a plural `s` are ignored, so `store_id` matches `stores` and `customer_key` matches `dim_customer`). A bare `id`, and a name built from the view's own name, are not flagged. The note is advice from names only. It never excludes a candidate, and a flagged column can still be the single qualifier.

## Limits of sampling

A sample can prove that a column is NOT unique. It can only suggest that a column is unique: duplicates may exist in rows the sample did not read. So:

- Every report line says how many rows were checked and how the sample was taken.
- A key written by `--apply` is tagged `primary_key_from_sample`, so you can find these keys later and confirm them against the full table.
- A sample from the most recent partitions or days cannot see a key that repeats across time (for example an id that restarts each year, or a daily snapshot table where the same id appears once per day).
- Below `--min-rows` nothing qualifies, even on a small table that was read in full. For small dimension tables, lower `--min-rows`.

Treat each key as a suggestion to review, not as a fact about the table.

## Sampling strategy per warehouse

The dialect is read from the model's connection (`omni models list`, then `omni connections list`), or set with `--dialect`.

| Dialect | Metadata read first | Sample | Strategy name |
|---------|--------------------|--------|---------------|
| `bigquery`, partitioned table | `INFORMATION_SCHEMA.COLUMNS` (`is_partitioning_column`) and `INFORMATION_SCHEMA.PARTITIONS` (`partition_id`, `total_rows`) | Filter on the partition column, from the most recent partition back until `--sample-rows` is met. No `LIMIT`, because `LIMIT` does not reduce the bytes BigQuery scans. Ingestion-time partitioned tables are filtered on `_PARTITIONTIME`. | `partition_filter` |
| `bigquery`, unpartitioned, at most `--max-unpartitioned-rows` rows (default 5,000,000) | same | The candidate columns of the whole table | `full_scan` |
| `bigquery`, unpartitioned and larger, or metadata unreadable | same | Not queried. Status `needs_decision`. Re-run with `--allow-full-scan`, or with `--date-column` and `--window-days`. | none |
| `snowflake` | none | Subquery with `LIMIT <sample-rows>` | `limit` |
| `redshift`, first sort key is a date or timestamp | `svv_table_info` (`sortkey1`), then `svv_columns` (its type) | Last `--window-days` days (default 30) on the sort key, plus `LIMIT <sample-rows>` | `sort_key_window` |
| `redshift`, otherwise | same | Subquery with `LIMIT <sample-rows>` | `limit` |
| `postgres`, `duckdb`, `motherduck` | none | Subquery with `LIMIT <sample-rows>` | `limit` |
| anything else | none | Subquery with `LIMIT <sample-rows>`, and a warning is printed | `limit` |
| any dialect with `--date-column NAME` | BigQuery only: the column's type | Last `--window-days` days (default 30) on that column. Also `LIMIT <sample-rows>`, except on BigQuery. | `date_window` |

A sample taken with `LIMIT` is whatever rows the warehouse returns first, not a random sample. On a partitioned BigQuery table the sample is whole partitions, so it can be larger than `--sample-rows`.

Identifier quoting is handled per dialect: backticks on BigQuery, double quotes elsewhere. On Snowflake, names are folded to upper case before quoting (as unquoted names are), unless the dimension's `sql:` quotes the column, in which case its case is kept.

## Safety

- Every statement is a single read-only `SELECT`.
- Schema, table and column names come from YAML files, so each is checked against a conservative pattern (letters, digits, underscore and dollar, with dots only as separators) before it is used. A view with any other name in `catalog`, `schema` or `table_name` is skipped with a reason. One consequence: a BigQuery project id that contains a hyphen is not accepted, so leave `catalog` out if the connection's default project is the right one.
- Without `--apply`, no view file is changed. The report files are always written.
- `--apply` only touches a view where exactly one column qualified, or where the column named with `--key` qualified. It sets `primary_key: true` and adds the tag on that one dimension, and leaves the rest of the file byte for byte as it was. (If the dimension is written in an unusual layout, the file is rewritten with a YAML round trip instead, which keeps comments and key order but may normalise spacing.)

## What is skipped

| View | Reason |
|------|--------|
| Already has a `primary_key: true` dimension | Not examined, unless `--recheck` is given. With `--recheck` the existing key is tested and reported, and never replaced. |
| `template: true` | Template views have no data of their own. |
| `extends:` another view and has no `table_name` | No table of its own. |
| SQL query view (`.query.view`, or top-level `sql:` or `query:`) | Not examined in this version, because the SQL may contain Mustache templating. Listed in the report; set the key by hand. |
| Unsafe name in `catalog`, `schema` or `table_name` | See Safety. |

## Statuses

| Status | Meaning |
|--------|---------|
| `key_found` | Exactly one column was unique and not null in the sample, or the column you named with `--key` was. Written only with `--apply`. |
| `multiple_candidates` | Two or more columns were unique and not null in the sample. Nothing is chosen or written. Choose with `--key`. |
| `pair_suggested` | No single column, but a pair was unique together in the sample. Reported only. |
| `no_key_found` | Nothing was unique in the sample. This is proof for the columns tested. |
| `sample_too_small` | The sample had fewer than `--min-rows` rows. |
| `needs_decision` | The table could not be sampled cheaply (BigQuery). Choose a flag and re-run. |
| `query_error` | The query for this view failed. The run continued. |
| `skipped` | Not examined. See the reason. |

## Examples

Dry run over every generated view. Nothing is changed; read `primary_keys.md` afterwards:

```bash
python scripts/find_primary_keys.py --model-id 3f2a9c1e-0000-0000-0000-000000000000 \
  --omni-repo migration/staging/views
```

```
[1/4] orders: key_found (id)
      id is the only column unique and not null in 1,000,000 rows (limit: limit 1000000)
[2/4] sales: multiple_candidates
      2 columns are unique and not null in 1,096 rows (limit: limit 1000000): transaction_id (key_like_name), date_key (key_like_name; likely foreign key: it is the primary key of view calendar). The sample cannot tell which one is the key, so nothing was chosen or written. Choose one with --key sales=DIMENSION
[3/4] order_items: pair_suggested
      no single column is unique and not null in 1,000,000 rows (limit: limit 1000000). Unique together in the sample: order_id + line_number. Nothing was written: add a dimension with the concatenation and mark it as the primary key by hand
[4/4] user_facts: skipped
      SQL query view: not examined in this version (its SQL may contain Mustache templating). Set its key by hand
```

Two views on a branch, with a JSON report as well:

```bash
python scripts/find_primary_keys.py --model-id MODEL_ID --branch-id BRANCH_ID \
  --view orders --view users --json primary_keys.json
```

Write the keys once you have read the report:

```bash
python scripts/find_primary_keys.py --model-id MODEL_ID --branch-id BRANCH_ID --apply
```

```yaml
  id:
    label: Id
    tags: [id, mig_phase_0, verified, primary_key_from_sample]
    primary_key: true
```

A large unpartitioned BigQuery table reported as `needs_decision`: sample the last week on a date column you know is clustered or cheap to filter, or accept the scan:

```bash
python scripts/find_primary_keys.py --model-id MODEL_ID --view events \
  --date-column event_date --window-days 7

python scripts/find_primary_keys.py --model-id MODEL_ID --view events --allow-full-scan
```

A pair suggestion in the report looks like this. Add it to the view by hand if it is right:

```yaml
  order_id_line_number_key:
    sql: cast(${order_id} as varchar) || '-' || cast(${line_number} as varchar)
    primary_key: true
    hidden: true
```

All flags and exit codes: [../reference/COMMAND_REFERENCE.md](../reference/COMMAND_REFERENCE.md#primary-key-finder).
