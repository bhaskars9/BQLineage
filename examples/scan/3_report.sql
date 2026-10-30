-- Step 3 of 3: a report view on top of the totals (written by 2_totals.sql)
CREATE OR REPLACE VIEW `shop.rpt.region_totals` AS
SELECT region, SUM(total) AS total FROM `shop.dw.customer_totals` GROUP BY region;
