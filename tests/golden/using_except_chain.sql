-- The production pattern: CTE chains joined USING (...) with c1.* EXCEPT / c2.* EXCEPT, a CTE repeating
-- a column name (load_ts from both sides), all inside a parenthesized CREATE TEMP TABLE ... AS ( ... )
CREATE TEMP TABLE enriched AS (
  WITH o AS (SELECT * FROM `shop.sales.orders` WHERE status = 'PAID'),
       c AS (SELECT * FROM `shop.crm.customers`),
       r AS (SELECT order_id, SUM(refund_amount) AS refunded FROM `shop.sales.refunds` GROUP BY order_id),
       oc AS (SELECT o.* EXCEPT (items), c.* EXCEPT (segment) FROM o LEFT JOIN c USING (customer_id)),
       ocr AS (SELECT oc.*, r.refunded FROM oc LEFT JOIN r USING (order_id))
  SELECT * EXCEPT (load_ts) FROM ocr
);

INSERT INTO `shop.dw.net_revenue` (customer_id, net)
SELECT customer_id, SUM(amount - IFNULL(refunded, 0)) FROM enriched GROUP BY customer_id;
