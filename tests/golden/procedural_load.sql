-- A procedure-style load: blocks, IF, a loop, an exception handler, a re-created temp table
BEGIN
  DECLARE batch_size INT64 DEFAULT 1000;
  DECLARE done BOOL DEFAULT FALSE;
  CREATE TEMP TABLE refunds AS
  SELECT order_id, SUM(refund_amount) AS refunded FROM `shop.sales.refunds` GROUP BY order_id;
  IF (SELECT COUNT(*) FROM refunds) > batch_size THEN
    CREATE OR REPLACE TEMP TABLE refunds AS SELECT * FROM refunds WHERE refunded > 0;
  END IF;
  WHILE NOT done DO
    INSERT INTO `shop.dw.net_revenue` (customer_id, net)
    SELECT o.customer_id, SUM(o.amount - IFNULL(r.refunded, 0))
    FROM `shop.sales.orders` o LEFT JOIN refunds r USING (order_id)
    WHERE o.status = 'PAID'
    GROUP BY o.customer_id;
    SET done = TRUE;
  END WHILE;
EXCEPTION WHEN ERROR THEN
  SELECT @@error.message AS failure;
END;
