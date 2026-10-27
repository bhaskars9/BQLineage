-- A view over a view, a wildcard table with _TABLE_SUFFIX, and a plain SELECT reading the new view
CREATE OR REPLACE VIEW `shop.dw.eu_customers` AS
SELECT customer_id, name FROM `shop.crm.customers_v` WHERE region = 'EU';

INSERT INTO `shop.dw.daily_events` (day, event_name, users)
SELECT PARSE_DATE('%Y%m%d', _TABLE_SUFFIX), event_name, COUNT(DISTINCT user_id)
FROM `shop.web.events_*`
WHERE _TABLE_SUFFIX BETWEEN '20260101' AND '20260131'
GROUP BY 1, 2;

SELECT e.name, COUNT(*) AS orders
FROM `shop.dw.eu_customers` e JOIN `shop.sales.orders` o USING (customer_id)
GROUP BY e.name;
