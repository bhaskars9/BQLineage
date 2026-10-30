"""bqlineage from Python, on the worked example. Run from the repo root: python examples/api.py"""
from pathlib import Path

from bqlineage import DictSchemaProvider, analyze
from bqlineage.drawio import to_drawio
from bqlineage.openlineage import to_openlineage

here = Path(__file__).parent
schemas = DictSchemaProvider.from_json(str(here / "schema.json"))  # or Catalog / BigQuerySchemaProvider
r = analyze((here / "orders.sql").read_text(), schemas)

print("tables:", r.table_lineage())
print("revenue:", r.column_lineage(table="dw.customer_revenue", columns=["revenue"]))
details = r.column_details(table="dw.customer_revenue", columns=["revenue"])["shop.dw.customer_revenue.revenue"]
for step in details["steps"]:
    print(f"  {step['column']} = {step['expression']}")
print("rows decided by:", details["indirect_sources"])
print("issues:", r.issues)

xml = to_drawio(r, table="customer_revenue")  # the same file --drawio writes
event = to_openlineage(r, "etl/orders.sql")  # the same event --openlineage writes
print(f"draw.io: {len(xml)} characters; OpenLineage outputs: {[o['name'] for o in event['outputs']]}")
