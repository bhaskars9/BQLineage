-- Daily revenue per EU customer
DECLARE run_date DATE DEFAULT CURRENT_DATE();

-- 1. Today's paid orders, with tax added
CREATE TEMP TABLE paid_orders AS
SELECT o.order_id, o.customer_id, o.amount * 1.2 AS gross
FROM `shop.sales.orders` o
WHERE o.status = 'PAID' AND DATE(o.order_ts) = run_date;

-- 2. Revenue per EU customer
INSERT INTO `shop.dw.customer_revenue` (customer_id, customer_name, region, revenue, loaded_at)
WITH eu_customers AS (
  SELECT customer_id, name, region
  FROM `shop.crm.customers`
  WHERE region = 'EU'
)
SELECT customer_id, c.name, c.region, SUM(p.gross), CURRENT_TIMESTAMP()
FROM paid_orders p
JOIN eu_customers c USING (customer_id)
GROUP BY customer_id, c.name, c.region;
