-- Step 2 of 3: total per customer, from the staged orders (written by 1_stage.sql)
INSERT INTO `shop.dw.customer_totals` (customer_id, region, total)
SELECT p.customer_id, c.region, SUM(p.amount)
FROM `shop.stage.paid_orders` p JOIN `shop.crm.customers` c USING (customer_id)
GROUP BY p.customer_id, c.region;
