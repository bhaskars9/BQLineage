-- Upsert a customer snapshot from a view: MERGE with WHEN ... AND, then an UPDATE ... FROM
MERGE `shop.dw.customer_snapshot` T
USING (
  SELECT c.customer_id, c.name, c.region, SUM(o.amount) AS ltv
  FROM `shop.crm.customers_v` c
  LEFT JOIN `shop.sales.orders` o ON o.customer_id = c.customer_id AND o.status = 'PAID'
  GROUP BY c.customer_id, c.name, c.region
) S
ON T.customer_id = S.customer_id
WHEN MATCHED AND S.ltv != T.lifetime_value THEN
  UPDATE SET lifetime_value = S.ltv, updated_at = CURRENT_TIMESTAMP()
WHEN NOT MATCHED THEN
  INSERT (customer_id, name, region, lifetime_value, updated_at)
  VALUES (S.customer_id, S.name, S.region, S.ltv, CURRENT_TIMESTAMP());

UPDATE `shop.dw.customer_snapshot` s
SET region = c.region
FROM `shop.crm.customers` c
WHERE s.customer_id = c.customer_id AND s.region IS NULL;
