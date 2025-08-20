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
    """
    Inspect the database and return a dict with the best-fit property names/types we can write to.
    Supports BOTH a real Notion date prop and a text date prop.
    """
    db = retrieve_db()
    title_prop = _first_title_prop(db)

    status_prop, status_labels = status_label_mapping(db)
    done_checkbox = _checkbox_named(db, "Done")  # optional

    class_prop    = _prop_if_type(db, "Class",    {"multi_select"})
    teacher_prop  = _prop_if_type(db, "Teacher",  {"multi_select"})
    type_prop     = _prop_if_type(db, "Type",     {"select"})
    priority_prop = _prop_if_type(db, "Priority", {"select"})

    # Recognize common date property names
    date_candidates = ["Date", "Due date", "Due Date", "Calendar Date", "Calendar"]
    due_date_prop_date = None       # Notion 'date' type prop (for calendar)
    due_date_prop_text = None       # rich_text type prop where you want "MM/DD/YYYY"

    for nm in date_candidates:
        if _prop_if_type(db, nm, {"date"}):
            due_date_prop_date = nm
            break

    if not due_date_prop_date:
        # Even if we have a 'date', we still check for a text date column to fill "MM/DD/YYYY"
        pass

    # Find any text prop among candidates (or any rich_text) to write MM/DD/YYYY
    for nm in date_candidates:
        if _prop_if_type(db, nm, {"rich_text"}):
            due_date_prop_text = nm
            break
        # As a last resort, any rich_text field can be used for the string date if present
        for nm, p in db["properties"].items():
            if p["type"] == "rich_text":
                due_date_prop_text = nm
                break

    tags_prop = _prop_if_type(db, "Tags", {"multi_select"}) or _find_multi_select(db)

    return {
        "title_prop": title_prop,
        "status_prop": status_prop,
        "status_labels": status_labels,
        "done_checkbox": done_checkbox,
        "class_prop": class_prop,
        "teacher_prop": teacher_prop,
        "type_prop": type_prop,
        "priority_prop": priority_prop,
        "due_date_prop_date": due_date_prop_date,  # date type (ISO 'YYYY-MM-DD')
        "due_date_prop_text": due_date_prop_text,  # text type (we'll write 'MM/DD/YYYY')
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

# ---------- Query helpers for de-dup ----------

@retry(tries=4, delay=1.0, backoff=2.0)
def query_by_canvas_id(canvas_id: int):
    try:
        return client.databases.query(
            database_id=DATABASE_ID,
            filter={"property": "Canvas ID", "number": {"equals": canvas_id}},
            page_size=3,
        )
    except APIResponseError as e:
        msg = (getattr(e, "body", {}) or {}).get("message", "").lower()
        if "could not find property" in msg or "validation_error" in (getattr(e, "code", "") or "").lower():
            ensure_canvas_id_property()
            return client.databases.query(
                database_id=DATABASE_ID,
                filter={"property": "Canvas ID", "number": {"equals": canvas_id}},
                page_size=3,
            )
        raise

@retry(tries=3, delay=0.8, backoff=1.8)
def query_by_title_and_date(
    title_prop: str,
    due_date_prop_date: str | None,
    due_date_prop_text: str | None,
    title_text: str,
    due_str_iso: str | None,
    due_str_mdy: str | None
):
    """
    Fallback search: Title (equals) AND (Date equals OR TextDate equals).
    If only one of the two exists, we filter on what we have.
    """
    filters = [{"property": title_prop, "title": {"equals": title_text}}]
    if due_date_prop_date and due_str_iso:
        filters.append({"property": due_date_prop_date, "date": {"equals": due_str_iso}})
    if due_date_prop_text and due_str_mdy:
        filters.append({"property": due_date_prop_text, "rich_text": {"equals": due_str_mdy}})
    if len(filters) == 1:
        f = filters[0]
    else:
        f = {"and": filters}
    return client.databases.query(database_id=DATABASE_ID, filter=f, page_size=3)

# ---------- Date normalization ----------

def _is_null_date(val) -> bool:
    if not isinstance(val, dict):
        return False
    d = val.get("date")
    if d is None:
        return True
    if isinstance(d, dict) and (d.get("start") in (None, "")):
        return True
    return False

def _normalize_date_for_update(props: dict) -> dict:
    """Convert any null-like date payloads to {'date': None} for update."""
    out = dict(props)
    for k, v in list(out.items()):
        if _is_null_date(v):
            out[k] = {"date": None}
    return out

def _drop_null_dates_for_create(props: dict) -> dict:
    """Remove any null-like date props during create to avoid 400."""
    clean = {}
    for k, v in props.items():
        if _is_null_date(v):
            continue
        clean[k] = v
    return clean

# ---------- Upsert with anti-dup ----------

def upsert_page(
    canvas_id,
    props,
    *,
    title_prop=None,
    title_text=None,
    due_date_prop_date=None,
    due_str_iso=None,
    due_date_prop_text=None,
    due_str_mdy=None
):
    """
    De-dup order:
      1) Canvas ID match
      2) Title + (Date or DateString) match → update that page and attach Canvas ID
    """
    # 1) Try by Canvas ID
    res = query_by_canvas_id(canvas_id)
    results = res.get("results", [])
    if results:
        page_id = results[0]["id"]
        props = _normalize_date_for_update(props)
        client.pages.update(page_id=page_id, properties=props)
        return page_id, "updated"

    # 2) Fallback: Title + (Date or TextDate)
    if title_prop and title_text:
        try:
            res_td = query_by_title_and_date(
                title_prop, due_date_prop_date, due_date_prop_text,
                title_text, due_str_iso, due_str_mdy
            )
            td_results = res_td.get("results", [])
            if td_results:
                page_id = td_results[0]["id"]
                props = _normalize_date_for_update(props)
                client.pages.update(page_id=page_id, properties=props)
                return page_id, "updated"
        except APIResponseError:
            pass

    # 3) Create new
    clean = _drop_null_dates_for_create(props)
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
