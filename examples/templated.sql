-- Templates from Airflow / dbt / Composer (double braces, dollar braces) and dynamic SQL built from script
-- constants. The values come from [vars] in bqlineage.toml, or from --var KEY=VALUE.
DECLARE target STRING DEFAULT '{{ params.project }}.dw.order_lines';
DECLARE min_qty INT64 DEFAULT ${min_qty};

-- a template value in a table name and in a filter
CREATE TEMP TABLE todays_orders AS
SELECT order_id, customer_id FROM `{{ params.project }}.sales.orders` WHERE DATE(order_ts) = '{{ ds }}';

-- built at run time, but only from constants: analyzed like any other statement
EXECUTE IMMEDIATE FORMAT("""
  INSERT INTO `%s` (order_id, sku, qty, line_total)
  SELECT t.order_id, i.sku, i.qty, i.qty * i.price
  FROM todays_orders t JOIN `{{ params.project }}.sales.order_items` i USING (order_id)
  WHERE i.qty >= %d
""", target, min_qty);
