-- What `bqlineage validate` reports. It exits with 1 because of the errors; warnings alone don't change that.

-- error: orders has no column amount_usd
SELECT o.order_id, o.amount_usd FROM `shop.sales.orders` o;

-- error: three columns into a two-column list
INSERT INTO `shop.dw.net_revenue` (customer_id, net)
SELECT customer_id, amount, status FROM `shop.sales.orders`;

-- error: a table the schema doesn't have (and a warning: so its * can't be expanded)
SELECT * FROM `shop.sales.archive`;

-- warning: a template value nobody gave
SELECT order_id FROM `shop.sales.orders` WHERE status = '{{ status }}';
