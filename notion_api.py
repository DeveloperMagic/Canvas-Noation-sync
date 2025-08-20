import os
from notion_client import Client
from notion_client.errors import APIResponseError
from utils import retry

NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "")
DATABASE_ID = os.environ.get("NOTION_DATABASE_ID", "")

if not NOTION_TOKEN or not DATABASE_ID:
    raise SystemExit("Missing NOTION_TOKEN or NOTION_DATABASE_ID env vars.")

client = Client(auth=NOTION_TOKEN)

def retrieve_db():
    # SDK accepts positional or keyword, this form is fine:
    return client.databases.retrieve(DATABASE_ID)

@retry(tries=4, delay=1.0, backoff=2.0)
def query_by_canvas_id(canvas_id: int):
    # Requires a Number property called "Canvas ID" in your Notion DB
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
    """
    Ensure select / multi_select properties have the needed options.
    Adds any missing ones so tags (Class, Teacher, Type, Priority) never fail.
    """
    db = retrieve_db()
    prop = db["properties"].get(prop_name)
    if not prop or prop["type"] not in ("select", "multi_select"):
        return
    have = {opt["name"] for opt in prop[prop["type"]]["options"]}
    missing = [n for n in want_names if n and n not in have]
    if not missing:
        return
    new_opts = prop[prop["type"]]["options"] + [{"name": n} for n in missing]
    client.databases.update(
        **{
            "database_id": DATABASE_ID,
            "properties": {
                prop_name: {prop["type"]: {"options": new_opts}}
            },
        }
    )

def ensure_taxonomy(
    class_names=(),
    teacher_names=(),
    type_names=("Assignment", "Quiz", "Test"),
    priority=("High", "Medium", "Low"),
):
    _ensure_select_options("Class", class_names, "multi_select")
    _ensure_select_options("Teacher", teacher_names, "multi_select")
    _ensure_select_options("Type", type_names, "select")
    _ensure_select_options("Priority", priority, "select")

def upsert_page(canvas_id, props):
    """Create or update a page identified by Canvas ID (Number)."""
    res = query_by_canvas_id(canvas_id)
    results = res.get("results", [])
    if results:
        page_id = results[0]["id"]
        client.pages.update(page_id=page_id, properties=props)
        return page_id, "updated"
    page = client.pages.create(parent={"database_id": DATABASE_ID}, properties=props)
    return page["id"], "created"

def verify_access():
    """
    Fail fast with a helpful message if the token/DB is wrong or not shared.
    """
    try:
        client.databases.retrieve(DATABASE_ID)
    except APIResponseError as e:
        code = (getattr(e, "code", "") or "").lower()
        body = getattr(e, "body", {}) or {}
        msg = body.get("message", str(e))
        if code == "unauthorized":
            raise SystemExit(
                "NOTION_TOKEN appears invalid or not usable in this workflow. "
                "Confirm the repo secret is the exact token (starts with ntn_), no quotes/spaces."
            )
        if code in ("object_not_found",):
            raise SystemExit(
                "NOTION_DATABASE_ID is wrong or the database is not accessible. "
                "Open the DB as a page and copy the 32-char ID from the URL."
            )
        if code in ("restricted_resource",):
            raise SystemExit(
                "Your integration is not invited to this database. In Notion: "
                "Share → Invite → select your integration → Can edit."
            )
        raise
