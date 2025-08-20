import os
import re
from notion_client import Client
from notion_client.errors import APIResponseError
from utils import retry

NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "")
DATABASE_ID = os.environ.get("NOTION_DATABASE_ID", "")

if not NOTION_TOKEN or not DATABASE_ID:
    raise SystemExit("Missing NOTION_TOKEN or NOTION_DATABASE_ID env vars.")

client = Client(auth=NOTION_TOKEN)

# ---------- Helpers for schema detection ----------

def retrieve_db():
    return client.databases.retrieve(database_id=DATABASE_ID)

def _normalize(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").strip().lower())

def _first_title_prop(db):
    for name, prop in db["properties"].items():
        if prop["type"] == "title":
            return name
    raise SystemExit("No title property found in this Notion database.")

def _status_prop_and_options(db):
    for name, prop in db["properties"].items():
        if prop["type"] == "status":
            opts = [o["name"] for o in prop["status"]["options"]]
            return name, opts
    return None, []

def _checkbox_named(db, want_name):
    prop = db["properties"].get(want_name)
    if prop and prop["type"] == "checkbox":
        return want_name
    for name, p in db["properties"].items():
        if p["type"] == "checkbox":
            return name
    return None

def _prop_if_type(db, name, want_types):
    prop = db["properties"].get(name)
    if prop and prop["type"] in want_types:
        return name
    return None

def _find_multi_select(db, preferred_names=("Tags", "Class", "Teacher")):
    for nm in preferred_names:
        if nm in db["properties"] and db["properties"][nm]["type"] == "multi_select":
            return nm
    for nm, p in db["properties"].items():
        if p["type"] == "multi_select":
            return nm
    return None

def ensure_canvas_id_property():
    db = retrieve_db()
    prop = db["properties"].get("Canvas ID")
    if not prop or prop.get("type") != "number":
        client.databases.update(
            database_id=DATABASE_ID,
            properties={"Canvas ID": {"number": {}}},
        )

def ensure_schema():
    ensure_canvas_id_property()

def status_label_mapping(db):
    prop, options = _status_prop_and_options(db)
    if not prop:
        return None, {"not_started": None, "started": None, "completed": None}

    norm_opts = {_normalize(o): o for o in options}

    def pick(candidates):
        for c in candidates:
            nc = _normalize(c)
            if nc in norm_opts:
                return norm_opts[nc]
        return None

    not_started = pick(["Not started", "To-do", "Todo", "Backlog", "To do"])
    started     = pick(["Started", "In progress", "Doing", "In Progress"])
    completed   = pick(["Completed", "Done", "Complete", "Finished"])

    if not not_started and options:
        not_started = options[0]
    if not completed and options:
        completed = options[-1]

    return prop, {"not_started": not_started, "started": started, "completed": completed}

def get_flexible_schema():
    db = retrieve_db()
    title_prop = _first_title_prop(db)

    status_prop, status_labels = status_label_mapping(db)
    done_checkbox = _checkbox_named(db, "Done")  # optional

    class_prop   = _prop_if_type(db, "Class",   {"multi_select"})
    teacher_prop = _prop_if_type(db, "Teacher", {"multi_select"})
    type_prop    = _prop_if_type(db, "Type",    {"select"})
    priority_prop= _prop_if_type(db, "Priority",{"select"})

    # Recognize common date property names
    date_candidates = ["Date", "Due date", "Due Date", "Calendar Date", "Calendar"]
    due_prop = None
    for nm in date_candidates:
        if _prop_if_type(db, nm, {"date"}):
            due_prop = nm
            break

    tags_prop    = _prop_if_type(db, "Tags",    {"multi_select"}) or _find_multi_select(db)

    return {
        "title_prop": title_prop,
        "status_prop": status_prop,
        "status_labels": status_labels,
        "done_checkbox": done_checkbox,
        "class_prop": class_prop,
        "teacher_prop": teacher_prop,
        "type_prop": type_prop,
        "priority_prop": priority_prop,
        "due_prop": due_prop,
        "tags_prop": tags_prop,
    }

# ---------- Option management (only when the prop exists) ----------

def _ensure_select_options_for(db, prop_name, want_names, kind):
    prop = db["properties"].get(prop_name)
    if not prop or prop["type"] != kind:
        return
    have = {opt["name"] for opt in prop[kind]["options"]}
    missing = [n for n in want_names if n and n not in have]
    if not missing:
        return
    new_opts = prop[kind]["options"] + [{"name": n} for n in missing]
    client.databases.update(
        database_id=DATABASE_ID,
        properties={prop_name: {kind: {"options": new_opts}}},
    )

def ensure_taxonomy(class_names=(), teacher_names=(), type_names=("Assignment","Quiz","Test"), priority=("High","Medium","Low")):
    db = retrieve_db()
    if "Class"   in db["properties"] and db["properties"]["Class"]["type"]   == "multi_select":
        _ensure_select_options_for(db, "Class",   class_names, "multi_select")
    if "Teacher" in db["properties"] and db["properties"]["Teacher"]["type"] == "multi_select":
        _ensure_select_options_for(db, "Teacher", teacher_names, "multi_select")
    if "Type"    in db["properties"] and db["properties"]["Type"]["type"]    == "select":
        _ensure_select_options_for(db, "Type",    type_names,   "select")
    if "Priority"in db["properties"] and db["properties"]["Priority"]["type"]== "select":
        _ensure_select_options_for(db, "Priority",priority,     "select")

    if "Tags" in db["properties"] and db["properties"]["Tags"]["type"] == "multi_select":
        _ensure_select_options_for(
            db, "Tags",
            list(set(list(class_names)+list(teacher_names)+list(type_names)+list(priority))),
            "multi_select"
        )

# ---------- Query & Upsert ----------

def _is_null_date(val) -> bool:
    if not isinstance(val, dict):
        return False
    d = val.get("date")
    if d is None:
        return True
    if isinstance(d, dict) and (d.get("start") in (None, "")):
        return True
    return False

@retry(tries=4, delay=1.0, backoff=2.0)
def query_by_canvas_id(canvas_id: int):
    try:
        return client.databases.query(
            database_id=DATABASE_ID,
            filter={"property": "Canvas ID", "number": {"equals": canvas_id}},
            page_size=1,
        )
    except APIResponseError as e:
        msg = (getattr(e, "body", {}) or {}).get("message", "").lower()
        if "could not find property" in msg or "validation_error" in (getattr(e, "code", "") or "").lower():
            ensure_canvas_id_property()
            return client.databases.query(
                database_id=DATABASE_ID,
                filter={"property": "Canvas ID", "number": {"equals": canvas_id}},
                page_size=1,
            )
        raise

def upsert_page(canvas_id, props):
    """
    - On UPDATE: if a date prop has null/empty start, we send {"date": None} to clear it.
    - On CREATE: we drop any date prop whose start would be null, to avoid 400.
    """
    res = query_by_canvas_id(canvas_id)
    results = res.get("results", [])

    if results:
        # UPDATE path: normalize null date values to {"date": None}
        for k, v in list(props.items()):
            if _is_null_date(v):
                props[k] = {"date": None}
        page_id = results[0]["id"]
        client.pages.update(page_id=page_id, properties=props)
        return page_id, "updated"
    else:
        # CREATE path: drop null date props entirely
        clean = {}
        for k, v in props.items():
            if _is_null_date(v):
                continue
            clean[k] = v
        page = client.pages.create(parent={"database_id": DATABASE_ID}, properties=clean)
        return page["id"], "created"

def verify_access():
    try:
        client.databases.retrieve(database_id=DATABASE_ID)
    except APIResponseError as e:
        code = (getattr(e, "code", "") or "").lower()
        if code == "unauthorized":
            raise SystemExit("NOTION_TOKEN invalid. Paste the exact ntn_/secret_ token (no quotes/spaces) into repo secret NOTION_TOKEN.")
        if code in ("object_not_found",):
            raise SystemExit("NOTION_DATABASE_ID is wrong/inaccessible. Open the DB as a page and copy the 32-char ID from the URL.")
        if code in ("restricted_resource",):
            raise SystemExit("Invite the integration to the DB: Share → Invite → your integration → Can edit.")
        raise
