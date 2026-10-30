-- Step 1 of 3: stage today's paid orders
CREATE OR REPLACE TABLE `shop.stage.paid_orders` AS
SELECT order_id, customer_id, amount FROM `shop.sales.orders` WHERE status = 'PAID';
