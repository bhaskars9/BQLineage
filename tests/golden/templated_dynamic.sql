-- Orchestrator templates, and dynamic SQL built from script constants
DECLARE target STRING DEFAULT '{{ params.project }}.dw.order_items';
DECLARE min_qty INT64 DEFAULT ${min_qty};

EXECUTE IMMEDIATE FORMAT("""
  INSERT INTO `%s` (order_id, sku, qty, line_total)
  SELECT o.order_id, i.sku, i.qty, i.qty * i.price
  FROM `{{ params.project }}.sales.orders` o, UNNEST(o.items) AS i
  WHERE i.qty >= %d
""", target, min_qty);
