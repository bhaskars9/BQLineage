# Reading the output

## Format

`bqlineage lineage` prints JSON:

```json
{
  "version": 1,
  "tables":  { "target table": ["source table", ...] },
  "columns": { "target table.column": { "sources": [...], "indirect_sources": [...], "steps": [...] } },
  "issues":  ["ERROR [statement 2] [line 5, col 10]: ...", "WARNING ..."]
}
```

`version` changes only when this shape does.

| Key | What it says |
|---|---|
| `tables` | Which tables feed which. Temp tables and views are traced through to real tables. |
| `sources` | The columns whose values flow into this column. |
| `indirect_sources` | The columns read by the JOIN / WHERE / GROUP BY / HAVING / QUALIFY conditions along the way, including subqueries such as `NOT IN (SELECT …)`. These decide which rows and groups exist. |
| `steps` | How the column was built: from the target back through CTEs, subqueries, temp tables and views. Each step has its `expression` and that query's `from`, `joins`, `where`, `group_by`, `having` and `qualify`. A step from a CTE also says which statement it's `in`. |
| `unresolved` | Only when lineage got stuck: the references whose source couldn't be found, usually because a schema is unknown. |
| `issues` | Errors are definite problems in the SQL. Warnings are things the tool couldn't know, such as a missing table schema. Each issue has its statement number and line. |

Plain `SELECT` statements appear as `<query>`, or `<query#N>` in a multi-statement script.

## A worked example

[`examples/orders.sql`](../examples/orders.sql) builds daily revenue per EU customer in two steps: a temp table of paid orders, then an INSERT that joins it to EU customers.

```sql
DECLARE run_date DATE DEFAULT CURRENT_DATE();

CREATE TEMP TABLE paid_orders AS
SELECT o.order_id, o.customer_id, o.amount * 1.2 AS gross
FROM `shop.sales.orders` o
WHERE o.status = 'PAID' AND DATE(o.order_ts) = run_date;

INSERT INTO `shop.dw.customer_revenue` (customer_id, customer_name, region, revenue, loaded_at)
WITH eu_customers AS (
  SELECT customer_id, name, region FROM `shop.crm.customers` WHERE region = 'EU'
)
SELECT customer_id, c.name, c.region, SUM(p.gross), CURRENT_TIMESTAMP()
FROM paid_orders p
JOIN eu_customers c USING (customer_id)
GROUP BY customer_id, c.name, c.region;
```

```bash
bqlineage lineage examples/orders.sql --schema examples/schema.json
```

The full output is in [`examples/orders.lineage.json`](../examples/orders.lineage.json). The part for `revenue`:

```json
"shop.dw.customer_revenue.revenue": {
  "sources": ["shop.sales.orders.amount"],
  "indirect_sources": ["shop.crm.customers.customer_id", "shop.crm.customers.name", "shop.crm.customers.region",
                       "shop.sales.orders.customer_id", "shop.sales.orders.order_ts", "shop.sales.orders.status"],
  "steps": [
    {"column": "shop.dw.customer_revenue.revenue", "expression": "SUM(p.gross)",
     "from": ["_SESSION.paid_orders AS p", "eu_customers AS c"],
     "joins": ["JOIN c ON p.customer_id = c.customer_id"],
     "group_by": ["COALESCE(p.customer_id, c.customer_id)", "c.name", "c.region"]},
    {"column": "_SESSION.paid_orders.gross", "expression": "o.amount * 1.2",
     "from": ["shop.sales.orders AS o"], "where": "o.status = 'PAID' AND DATE(o.order_ts) = @run_date"}
  ]
}
```

**`tables`:** `customer_revenue` is built from `orders` and `customers`. The temp table `paid_orders` doesn't appear, because lineage is traced through it. `--raw` shows it as its own hop.

**`sources`:**

| Target column | `sources` | Why |
|---|---|---|
| `revenue` | `orders.amount` | `SUM(p.gross)`, and `gross` is `o.amount * 1.2` |
| `customer_id` | `orders.customer_id`, `customers.customer_id` | `USING (customer_id)` merges the key from both sides: `COALESCE(p.customer_id, c.customer_id)` |
| `customer_name` | `customers.name` | through the CTE `eu_customers` |
| `region` | `customers.region` | through the CTE `eu_customers` |
| `loaded_at` | *(empty)* | `CURRENT_TIMESTAMP()`: computed, not read from any table |

An empty `sources` list is never a silent failure. The first step's expression says what the value is: a constant, a function, or a script variable (`@run_date`).

**`steps`:** read them top to bottom, going back in time:

1. **`revenue = SUM(p.gross)`**
   - `p` is the temp table, joined to `eu_customers AS c` on `customer_id`.
   - Rows are grouped per customer.
2. **`paid_orders.gross = o.amount * 1.2`**
   - computed in the temp table
   - keeps only orders with `status = 'PAID'` placed on `@run_date`

**`indirect_sources`:** these decide which rows exist:
- the temp table's WHERE (`status`, `order_ts`)
- the CTE's WHERE (`region`)
- the join keys
- the GROUP BY columns

## Compaction

Steps that only copy a column one-to-one are left out: a CTE or temp table that passes it through with no expression, filter, join or grouping. The chain then shows only where something happens. The target's own step always stays. `--raw` shows every hop.

## Names

- **`sources`** keep each column's spelling from the table schema.
- **Step text** shows names lower-cased, because BigQuery column names and aliases ignore case.
- **Temp tables** are `_SESSION.name`. A re-created one is versioned (`_SESSION.name#2`), so each reader keeps its own lineage.

## Limits

- **Struct fields:** lineage of `address.city` stops at the column `address`.
- **`ALTER TABLE`:** schema changes made inside the script aren't tracked.
- **Run-time SQL and variable values:** see [variables.md](variables.md#limits).

## From Python

```python
from bqlineage import Catalog, BigQuerySchemaProvider, analyze

with Catalog("bqlineage.db", BigQuerySchemaProvider("my-proj")) as schemas:
    r = analyze(sql, schemas, dataset="sales", vars={"env": "prod"})
r.table_lineage()                                     # {"proj.dw.fact": ["proj.sales.customers", ...]}
r.column_lineage(table="dw.fact", columns=["total"])  # {"proj.dw.fact.total": ["proj.sales.orders.amount"]}
r.column_details(table="dw.fact")                     # sources + indirect_sources + steps, as in the JSON
r.issues                                              # [Issue(severity, message, statement, line, col)]
r.to_dict()                                           # the JSON above
```
