-- Recursive CTEs, UNION ALL, subqueries in FROM and WHERE, and a CTE that only copies.

-- an org chart: the recursive CTE walks down from the top manager
INSERT INTO `shop.dw.org_chart` (emp_id, name, manager_name, depth)
WITH RECURSIVE chain AS (
  SELECT emp_id, manager_id, name, 0 AS depth FROM `shop.hr.employees` WHERE manager_id IS NULL
  UNION ALL
  SELECT e.emp_id, e.manager_id, e.name, c.depth + 1
  FROM `shop.hr.employees` e JOIN chain c ON e.manager_id = c.emp_id
)
SELECT c.emp_id, c.name, m.name, c.depth
FROM chain c LEFT JOIN `shop.hr.employees` m ON m.emp_id = c.manager_id;

-- staff and contractors in one list. all_staff only copies, so it's left out of the output unless --raw
INSERT INTO `shop.dw.people` (person_id, name, kind)
WITH all_staff AS (SELECT * FROM `shop.hr.employees`)
SELECT id, name, kind FROM (
  SELECT emp_id AS id, name, 'staff' AS kind FROM all_staff
  UNION ALL
  SELECT contractor_id, name, 'contractor' FROM `shop.hr.contractors`
) people
WHERE id NOT IN (SELECT emp_id FROM `shop.hr.leavers`);
