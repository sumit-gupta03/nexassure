# Check reference

Every check type, its parameters, and when to reach for it.

`nexassure checks` prints this list from the running installation, including any
check types added by plugins. `nexassure checks --json` gives the machine-readable
form.

## Common fields

Every check accepts these, whatever its type:

| Field | Default | Meaning |
|---|---|---|
| `name` | required | Unique within the suite. Also the id under which history is tracked |
| `type` | required | One of the types below |
| `description` | — | What breaks when this fails. Shown in every report, Slack message and MCP response |
| `dataset` | — | Target table: `table`, `schema.table`, or `db.schema.table` |
| `column` / `columns` | — | Target column(s) |
| `severity` | `error` | `info`, `warn`, `error`, `critical`. Only `error` and `critical` fail the build |
| `threshold` | — | Tolerated failure. A **ratio** when ≤ 1 (`0.01` = 1%), an **absolute row count** above it (`50` = 50 rows) |
| `where` | — | SQL predicate narrowing which rows the check considers |
| `params` | `{}` | Type-specific options |
| `tags` | `[]` | For `nexassure run --tag` |
| `owner` | — | Who to notify |
| `enabled` | `true` | Set `false` to park a check without deleting it |
| `sample_limit` | `10` | Failing rows captured as evidence. Set `0` on tables holding personal data |
| `depends_on` | `[]` | Check names that must pass first. Creates an execution wave |

`description` is worth writing properly. It is the difference between an alert
that says `unique failed on customers.id` and one that says *"customer_id joins
to every downstream mart; a duplicate double-counts revenue."*

---

## Completeness

### `not_null`

No NULL values in the column.

```yaml
- name: orders_have_a_customer
  type: not_null
  description: An order with no customer cannot be attributed to a region.
  dataset: orders
  column: customer_id
```

### `not_blank`

No empty or whitespace-only strings. NULLs count as blank too — a caller asking
for "no blanks" almost never wants NULLs to slip through.

```yaml
- name: email_is_present
  type: not_blank
  dataset: customers
  column: email
  threshold: 0.01     # tolerate 1% while the CRM backfill lands
```

> On Oracle, the empty string *is* NULL, so `not_blank` and `not_null` behave
> identically there.

### `completeness`

Fraction of non-NULL values meets a floor. Where `not_null` is all-or-nothing,
this expresses an SLA.

| Param | Default | Meaning |
|---|---|---|
| `min_ratio` | `1.0` | Minimum non-NULL fraction |

```yaml
- name: phone_mostly_populated
  type: completeness
  dataset: customers
  column: phone
  params:
    min_ratio: 0.95
```

An empty table is treated as vacuously complete, so this does not fire on a
newly created partition before its first load.

---

## Uniqueness

### `unique`

No duplicate values in a column, or in a combination of columns.

| Param | Default | Meaning |
|---|---|---|
| `ignore_nulls` | `true` | Exclude NULL rows. NULL never equals NULL in SQL, so they can never be duplicates of each other |

```yaml
- name: order_id_is_unique
  type: unique
  dataset: orders
  column: order_id

- name: one_row_per_customer_per_day
  type: unique
  dataset: daily_activity
  columns: [customer_id, activity_date]
```

Reports both numbers people ask for: `observed` is how many distinct values are
duplicated, `rows_failed` is how many surplus rows would have to be deleted.

### `primary_key`

Unique **and** never NULL. Runs uniqueness with NULLs included, so a NULL key is
caught rather than quietly excluded.

```yaml
- name: customer_id_is_the_key
  type: primary_key
  dataset: customers
  column: customer_id
```

### `no_duplicate_rows`

No fully duplicated rows. The column list comes from catalog introspection, so
the check keeps working when the table gains a column.

| Param | Default | Meaning |
|---|---|---|
| `exclude_columns` | `[]` | Columns to ignore when comparing — typically a load timestamp or surrogate key |

```yaml
- name: no_replayed_rows
  type: no_duplicate_rows
  dataset: orders
  params:
    exclude_columns: [_loaded_at]
  depends_on: [orders_not_empty]   # a full GROUP BY is expensive; gate it
```

---

## Volume

### `row_count`

Row count sits inside an expected band.

| Param | Meaning |
|---|---|
| `min` | Lower bound, inclusive |
| `max` | Upper bound, inclusive |
| `equals` | Exact count |

With no parameters it degrades to "the table must not be empty".

```yaml
- name: daily_volume_is_plausible
  type: row_count
  description: Far fewer means a partial load; far more means a duplicated batch.
  dataset: orders
  params:
    min: 500
    max: 100000
```

### `not_empty`

The table has at least one row. Shorthand for `row_count` with `min: 1`.

---

## Validity

### `accepted_values`

Every value belongs to an allowed set.

| Param | Default | Meaning |
|---|---|---|
| `values` | required | The allowed set |
| `ignore_nulls` | `true` | Whether NULL is acceptable |

```yaml
- name: status_is_a_known_state
  type: accepted_values
  dataset: orders
  column: status
  params:
    values: [pending, shipped, delivered, cancelled]
```

### `rejected_values`

No value appears in a forbidden set. The inverse.

```yaml
- name: no_test_accounts_in_prod
  type: rejected_values
  dataset: customers
  column: email
  params:
    values: [test@example.com, qa@example.com]
```

### `range`

Numeric or date values fall inside a bound.

| Param | Default | Meaning |
|---|---|---|
| `min` | — | Lower bound |
| `max` | — | Upper bound |
| `inclusive` | `true` | Whether the endpoints themselves pass |

At least one of `min` / `max` is required. NULLs are out of scope — use
`not_null` to assert presence.

```yaml
- name: totals_are_never_negative
  type: range
  dataset: orders
  column: total
  params:
    min: 0
```

### `regex`

Values match a regular expression.

| Param | Default | Meaning |
|---|---|---|
| `pattern` | required | The expression |
| `ignore_nulls` | `true` | Whether NULL passes |
| `negate` | `false` | Invert: values must *not* match |

```yaml
- name: email_looks_like_an_email
  type: regex
  dataset: customers
  column: email
  params:
    pattern: '^[^@\s]+@[^@\s]+\.[^@\s]+$'
```

> **SQL Server and Synapse have no regex engine.** The pattern is handed to
> `LIKE` there, so anchored wildcard patterns work and richer expressions do
> not. Write those as `custom_sql` on T-SQL engines.
>
> **Snowflake anchors implicitly.** `REGEXP_LIKE` matches the whole string, so
> a partial-match pattern needs explicit `.*` on both ends.

### `length`

String length falls inside a bound.

| Param | Meaning |
|---|---|
| `min_length` | Minimum |
| `max_length` | Maximum |
| `equals` | Exact length |

```yaml
- name: country_code_is_iso
  type: length
  dataset: customers
  column: country_code
  params:
    equals: 2
```

---

## Timeliness

### `freshness`

The newest row is recent enough. The age is computed against the **warehouse
clock**, so a CI runner in a different timezone cannot skew it.

| Param | Meaning |
|---|---|
| `max_age_hours` | Staleness limit in hours |
| `max_age_minutes` | Staleness limit in minutes |

```yaml
- name: orders_are_fresh
  type: freshness
  description: The hourly pipeline is late once the newest order is over 90 minutes old.
  dataset: orders
  column: created_at
  params:
    max_age_minutes: 90
```

A table with no timestamps fails, because freshness cannot be established — not
passing by default, which would hide a broken pipeline.

---

## Consistency

### `referential_integrity`

Every value exists in a parent table. Warehouses rarely enforce foreign keys,
so orphaned facts are the single most common real defect this framework catches.

| Param | Default | Meaning |
|---|---|---|
| `to` | required | Parent table. Inherits the child schema if unqualified |
| `field` | required | Parent column |
| `ignore_nulls` | `true` | Whether a NULL child value is acceptable |

```yaml
- name: orders_reference_real_customers
  type: referential_integrity
  dataset: orders
  column: customer_id
  params:
    to: customers
    field: customer_id
```

Generated as `NOT EXISTS`, not `NOT IN` — `NOT IN` silently returns zero rows
when the parent column contains a single NULL, which would make the check pass
on a table full of orphans.

### `schema`

The table exposes the columns the contract promises. Catches the two failure
modes that break downstream jobs silently: a column disappearing, and a column
changing type.

| Param | Default | Meaning |
|---|---|---|
| `columns` | required | Names, or `{name, type}` mappings |
| `strict` | `false` | Also fail on unexpected columns |
| `check_types` | `true` | Compare declared types |

```yaml
- name: orders_schema_is_stable
  type: schema
  dataset: orders
  params:
    columns:
      - name: order_id
        type: varchar
      - name: total
        type: decimal
      - name: created_at
```

Extra columns are tolerated unless `strict: true`, because additive changes are
usually safe. Type matching is a substring comparison, so `decimal` matches
`DECIMAL(12,2)`.

### `column_exists`

A named column is present. Cheaper than `schema` when that is all you need.

---

## Statistical

### `aggregate`

An aggregate over a column sits inside a band.

| Param | Default | Meaning |
|---|---|---|
| `function` | `avg` | `avg`, `sum`, `min`, `max`, `count`, `count_distinct`, `stddev` |
| `min` / `max` / `equals` | — | At least one required |

```yaml
- name: average_order_value_is_sane
  type: aggregate
  description: AOV outside this band has always meant a pricing or currency bug.
  dataset: orders
  column: total
  params:
    function: avg
    min: 10
    max: 500
```

---

## Custom SQL

### `custom_sql`

Description + query + expected output. The escape hatch for business rules no
built-in check expresses.

| Param | Default | Meaning |
|---|---|---|
| `bind` | `{}` | Bind values for `:name` placeholders |
| `max_rows` | 1,000 (10,000 for table shapes) | Cap; exceeding it errors rather than silently truncating |
| `allow_write` | `false` | Opt out of the read-only guard for this check |

```yaml
- name: revenue_reconciles_with_ledger
  type: custom_sql
  description: Daily revenue must match the finance ledger to within a cent.
  query: |
    SELECT ABS(SUM(o.total) - SUM(l.amount))
    FROM orders o JOIN ledger l ON o.order_date = l.entry_date
    WHERE o.order_date = CURRENT_DATE - 1
  expect:
    operator: lte
    value: 0.01
```

`{{ table }}` and `{{ column }}` are substituted from `dataset` and `column`, so
one query can be reused across datasets.

#### The `expect` block

`shape` reduces the result set; `operator` compares that reduction to `value`.

| `shape` | Reduces to |
|---|---|
| `scalar` *(default)* | First column of the first row |
| `row` | The first row, as a list |
| `column` | The first column, as a list |
| `table` | All rows |
| `row_count` | Just the number of rows |

| `operator` | Meaning |
|---|---|
| `eq` / `ne` | Equal / not equal |
| `gt` / `gte` / `lt` / `lte` | Ordering |
| `between` | `value: [low, high]`, inclusive |
| `in` / `not_in` | Membership |
| `matches` / `not_matches` | Regex, applied in Python to the returned value |
| `contains` | Substring, or list membership |
| `approx` | Numeric, with `tolerance` and `relative_tolerance` |
| `set_equals` | Same values, order-insensitive |
| `rows_equal` | Full result set match |
| `empty` / `not_empty` | Result is empty or not |
| `is_null` / `is_not_null` | Value presence |

Extra options: `tolerance`, `relative_tolerance`, `ignore_row_order` (default
`true`), `ignore_case`.

Comparison is deliberately tolerant of the types databases return
inconsistently: `Decimal("3")`, `3` and `3.0` compare equal, and a `date`
compares equal to the ISO string you wrote in YAML. Being strict there produces
failures that teach people to distrust the tool.

With no `expect` block, the default is "the query returns no rows".

### `sql_returns_no_rows`

Every returned row is a violation, and those rows become the failure evidence.
The dbt-style convention, without needing an `expect` block.

```yaml
- name: no_future_dated_orders
  type: sql_returns_no_rows
  description: A future-dated order means the loader wrote local time, not UTC.
  query: SELECT order_id, created_at FROM orders WHERE created_at > CURRENT_TIMESTAMP
```

### `sql_returns_rows`

The inverse: the query must return at least `min_rows` (default 1). For rules
shaped as "this thing should exist".

### `compare_queries`

Two queries must agree. The workhorse of migration and reconciliation testing.

| Param | Meaning |
|---|---|
| `other_query` | required. The query to compare against |
| `max_rows` | Cap on both sides |

```yaml
- name: staging_matches_legacy
  type: compare_queries
  description: Per-day row counts must be identical after the migration.
  query: SELECT day, COUNT(*) FROM staging.orders GROUP BY day
  params:
    other_query: SELECT day, COUNT(*) FROM legacy.orders GROUP BY day
```

Row order is ignored by default.

---

## Thresholds and severity

These two fields are what stop a data quality suite from becoming noise.

**`threshold`** tolerates a known, bounded amount of bad data:

```yaml
threshold: 0.001    # up to 0.1% of rows may fail
threshold: 50       # up to 50 rows may fail
```

Values at or below 1 are read as ratios; above 1 as absolute row counts.

**`severity`** decides whether a failure stops the pipeline:

| Severity | Status on failure | Exit code |
|---|---|---|
| `info` | `failed` | 1 |
| `warn` | `warned` | **0** |
| `error` *(default)* | `failed` | 1 |
| `critical` | `failed` | 1 |

`warn` is the right setting for a check you are still calibrating, and for
everything `nexassure suggest` generates.

## Dependencies

`depends_on` splits execution into sequential waves. Use it when a cheap check
should gate an expensive one:

```yaml
- name: orders_not_empty
  type: not_empty
  dataset: orders

- name: orders_deep_scan
  type: no_duplicate_rows
  dataset: orders
  depends_on: [orders_not_empty]
```

If the dependency fails, the dependent is reported `skipped` — never silently
dropped. Cycles are caught by `nexassure validate`, offline.

## Adding your own

See [CONTRIBUTING.md](https://github.com/sumit-gupta03/nexassure/blob/main/CONTRIBUTING.md#adding-a-check-type). Most check types
are a few lines, because `RowPredicateCheck` already handles counting and
sampling failing rows.
