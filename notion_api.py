import os
from notion_client import Client
from utils import retry

NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "")
DATABASE_ID = os.environ.get("NOTION_DATABASE_ID", "")

if not NOTION_TOKEN or not DATABASE_ID:
    raise SystemExit("Missing NOTION_TOKEN or NOTION_DATABASE_ID env vars.")

client = Client(auth=NOTION_TOKEN)

def retrieve_db():
    return client.databases.retrieve(DATABASE_ID)

def _prop(db, name):
    return retrieve_db()["properties"].get(name)

@retry(tries=4, delay=1.0, backoff=2.0)
def query_by_canvas_id(canvas_id):
    # If the property doesn't exist yet, this will 400; ensure your DB has it.
    return client.databases.query(
        **{
            "database_id": DATABASE_ID,
            "filter": {
                "property": "Canvas ID",
                "number": {"equals": canvas_id},
            },
            "page_size": 1,
        }
    )

def _ensure_select_options(prop_name, want_names, prop_kind="select"):
    # Add missing options to select / multi_select properties
    db = retrieve_db()
    prop = db["properties"].get(prop_name)
    if not prop or prop["type"] not in ("select", "multi_select"):
        return
    have = {opt["name"] for opt in prop[prop["type"]]["options"]}
    missing = [n for n in want_names if n not in have]
    if not missing:
        return
    # Append missing options (simple colors)
    new_opts = prop[prop["type"]]["options"] + [{"name": n} for n in missing]
    client.databases.update(
        **{
            "database_id": DATABASE_ID,
            "properties": {
                prop_name: {prop["type"]: {"options": new_opts}}
            },
        }
    )

def ensure_taxonomy(class_names=(), teacher_names=(), type_names=("Assignment", "Quiz", "Test"), priority=("High","Medium","Low")):
    _ensure_select_options("Class", class_names, "multi_select")
    _ensure_select_options("Teacher", teacher_names, "multi_select")
    _ensure_select_options("Type", type_names, "select")
    _ensure_select_options("Priority", priority, "select")

def upsert_page(canvas_id, props):
    """Create or update a page by Canvas ID."""
    res = query_by_canvas_id(canvas_id)
    results = res.get("results", [])
    if results:
        page_id = results[0]["id"]
        client.pages.update(page_id=page_id, properties=props)
        return page_id, "updated"
    else:
        page = client.pages.create(parent={"database_id": DATABASE_ID}, properties=props)
        return page["id"], "created"
