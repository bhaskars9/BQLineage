-- Driving tables and tables joined in. The driving table decides which rows a query has; the others add
-- columns or only filter. In the diagram, arrows from a driving table are solid, the others dashed.

-- LEFT JOIN: every order stays, so orders drives; customers and segments add columns, blocked only filters
INSERT INTO `shop.dw.order_report` (order_id, amount, customer_name, segment)
SELECT o.order_id, o.amount, c.name, s.segment
FROM `shop.sales.orders` o
LEFT JOIN `shop.crm.customers` c ON c.customer_id = o.customer_id
LEFT JOIN `shop.crm.segments` s ON s.customer_id = o.customer_id
WHERE o.customer_id NOT IN (SELECT customer_id FROM `shop.crm.blocked`);

-- RIGHT JOIN: every product appears, sold or not, so products drives
INSERT INTO `shop.dw.product_sales` (sku, product_name, units)
SELECT p.sku, p.name, IFNULL(SUM(i.qty), 0)
FROM `shop.sales.order_items` i
RIGHT JOIN `shop.catalog.products` p ON p.sku = i.sku
GROUP BY p.sku, p.name;

-- FULL JOIN: both sides drive (a budget line with no sales, sales with no budget line)
INSERT INTO `shop.dw.budget_vs_actual` (category, budget, actual)
SELECT COALESCE(b.category, a.category), b.amount, a.amount
FROM `shop.fin.budget` b
FULL JOIN (
  SELECT p.category, SUM(i.qty * i.price) AS amount
  FROM `shop.sales.order_items` i JOIN `shop.catalog.products` p USING (sku)
  GROUP BY p.category
) a ON a.category = b.category;
