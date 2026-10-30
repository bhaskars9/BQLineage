-- Views and wildcard tables. A view is traced through to the tables under it, using its SQL from the catalog
-- (here "view_sql" in schema.json); --no-expand-views stops at the view instead.

-- a view over a view: shop.crm.customers_v is itself a view over shop.crm.customers
CREATE OR REPLACE VIEW `shop.dw.eu_customers` AS
SELECT customer_id, name FROM `shop.crm.customers_v` WHERE region = 'EU';

-- a wildcard table, and _TABLE_SUFFIX as a column of it
INSERT INTO `shop.dw.daily_events` (day, event_name, users)
SELECT PARSE_DATE('%Y%m%d', _TABLE_SUFFIX), event_name, COUNT(DISTINCT user_id)
FROM `shop.web.events_*`
WHERE _TABLE_SUFFIX BETWEEN '20260101' AND '20260131'
GROUP BY 1, 2;

-- reading the view the script just created
INSERT INTO `shop.dw.eu_order_counts` (name, orders)
SELECT e.name, COUNT(*)
FROM `shop.dw.eu_customers` e JOIN `shop.sales.orders` o USING (customer_id)
GROUP BY e.name;
