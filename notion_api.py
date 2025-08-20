from notion_client import Client
from notion_client.errors import APIResponseError
from utils import retry, get_env

# Support multiple env var names for flexibility across platforms
NOTION_TOKEN = get_env("NOTION_TOKEN", "NOTION_API_KEY", "API_TOKEN", "TOKEN", "API_KEY")
DATABASE_ID = get_env("NOTION_DATABASE_ID", "DATABASE_ID", "DB_ID", "NOTION_DB")

if not NOTION_TOKEN or not DATABASE_ID:
    raise SystemExit("Missing Notion token or database ID environment variables.")

client = Client(auth=NOTION_TOKEN)

def retrieve_db():
    # SDK accepts positional or keyword, this form is fine:
    return client.databases.retrieve(DATABASE_ID)

@retry(tries=4, delay=1.0, backoff=2.0)
def query_by_canvas_id(canvas_id: int):
    """Lookup a page by Canvas ID stored as a text property."""
    return client.databases.query(
        **{
            "database_id": DATABASE_ID,
            "filter": {
                "property": "Canvas ID",
                "rich_text": {"equals": str(canvas_id)},
            },
            "page_size": 1,
        }
    )

def _ensure_select_options(prop_name, want_names):
    """Ensure select or multi-select properties include required options.

    If updating the database is not permitted (e.g. read-only access), the
    function silently skips without raising.
    """
    try:
        db = retrieve_db()
    except APIResponseError:
        return
    prop = db["properties"].get(prop_name)
    if not prop or prop["type"] not in ("select", "multi_select"):
        return
    have = {opt["name"] for opt in prop[prop["type"]]["options"]}
    missing = [n for n in want_names if n and n not in have]
    if not missing:
        return
    new_opts = prop[prop["type"]]["options"] + [{"name": n} for n in missing]
    try:
        client.databases.update(
            **{
                "database_id": DATABASE_ID,
                "properties": {
                    prop_name: {prop["type"]: {"options": new_opts}}
                },
            }
        )
    except APIResponseError:
        # Lack of permission to edit the DB should not abort the run
        pass

def ensure_taxonomy(
    class_names=(),
    teacher_names=(),
    type_names=("Assignment", "Quiz", "Test"),
    status_names=("Not started", "In Progress", "Completed"),
):
    """Add any missing select options for Class/Teacher/Type/Status."""
    _ensure_select_options("Class", class_names)
    _ensure_select_options("Teacher", teacher_names)
    _ensure_select_options("Type", type_names)
    _ensure_select_options("Status", status_names)

def upsert_page(canvas_id, props):
    """Create or update a page identified by Canvas ID (text)."""
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
                "The provided Notion token appears invalid or unusable in this workflow. "
                "Confirm the token is correct and contains no extra quotes or spaces."
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
