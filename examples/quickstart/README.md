# Quickstart

A complete NexAssure project you can run in under a minute. It uses a local DuckDB
file, so there are no credentials, no containers and no warehouse involved.

```bash
pip install "nexassure[duckdb]"
cd examples/quickstart
python seed.py
nexassure run
```

## What the demo data contains

`seed.py` builds 504 customers and 1,203 orders, with one planted defect per
check family — so every failure you see is a real one the checks caught:

| Defect | Caught by |
|---|---|
| `c0007` appears twice | `customer_id_is_the_key` |
| One customer has a NULL email, one has `"   "` | `customer_email_is_present` |
| One customer is in region `mars` | `customer_region_is_a_known_region` |
| One order has status `refunded` | `order_status_is_a_known_state` |
| One order has a total of `-19.99` | `order_totals_are_never_negative` |
| One order references customer `c9999`, who does not exist | `orders_reference_real_customers` |

Everything else passes, which matters just as much: a suite that fails on
everything teaches you nothing.

## Try these

```bash
nexassure validate                       # lint the suite offline, no database
nexassure test-connection --all
nexassure tables demo

nexassure profile demo main.orders       # see what is actually in the data
nexassure profile demo main.customers --percentiles

nexassure run                            # exits 1 - five checks fail
nexassure run --tag critical             # just the ones that block a pipeline
nexassure run --select orders_are_fresh  # exits 0
nexassure run --show-sql                 # print the SQL behind each failure
nexassure run --dry-run                  # the plan, without executing

nexassure run -o report.html             # a self-contained page you can share
nexassure run -o results.xml -f junit    # what CI consumes

nexassure history                        # every run is recorded
nexassure metastore info
nexassure metastore catalog              # the tables it catalogued on connect
```

## Generate a suite from the data

```bash
nexassure suggest demo --schema main -o suites/generated.yml
```

Compare `suites/generated.yml` with the handwritten `suites/warehouse.yml`. The
generated one covers the mechanical checks — nulls, cardinality, ranges. The
handwritten one adds the knowledge that is not in the data: which statuses the
BI layer understands, what a plausible cancellation rate is, how late the
pipeline is allowed to be.

That gap is the point. Generation gets you to 60% in a minute; the remaining
40% is where the value is.

## Point an agent at it

```bash
pip install "nexassure[mcp]"
nexassure mcp --config "$(pwd)/nexassure.yml"
```

Then ask your assistant *"what data quality problems does the orders table
have?"* and watch it profile, hypothesise and verify — read-only throughout.
See [docs/mcp.md](../../docs/mcp.md).

## Files

```
nexassure.yml               project config: connection, metastore, suite globs
seed.py                  builds warehouse.duckdb
suites/warehouse.yml     17 checks across every family
```

`warehouse.duckdb` and `nexassure-demo.db` are generated and gitignored. Delete
them and re-run `seed.py` to start over.
