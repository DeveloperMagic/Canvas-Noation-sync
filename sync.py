# Canvas → Notion sync with auto tag creation for Teacher/Class/Tags
# - Adds/uses real Notion multi-select tags for Teacher and Tags, and a select for Class.
# - If a tag/option doesn't exist yet, it creates it on the database first.
# - Priority is expected to be a Formula property in Notion (see README note).

import os
import requests
from datetime import datetime, timezone, timedelta
from notion_client import Client
from dateutil import parser

# --------- ENV ---------
NOTION_TOKEN = os.environ["NOTION_TOKEN"]
NOTION_DB_ID = os.environ["NOTION_DATABASE_ID"]
CANVAS_BASE_URL = os.environ["CANVAS_BASE_URL"].rstrip("/")
CANVAS_TOKEN = os.environ["CANVAS_TOKEN"]

notion = Client(auth=NOTION_TOKEN)

# ========= Canvas helpers =========

def paginate(path, params=None):
    """Iterate through all pages of a Canvas API collection."""
    url = f"{CANVAS_BASE_URL}/api/v1{path}"
    headers = {"Authorization": f"Bearer {CANVAS_TOKEN}"}
    first = True
    while url:
        r = requests.get(url, headers=headers, params=params if first else None, timeout=30)
        if r.status_code == 401:
            raise SystemExit("Canvas 401 Unauthorized. Check CANVAS_BASE_URL and CANVAS_TOKEN.")
        r.raise_for_status()
        first = False
        data = r.json()
        for item in data:
            yield item
        url = r.links.get("next", {}).get("url")

def get_courses():
    # Only active enrollments; you can add enrollment_type=student if you want to restrict further
    return list(paginate("/courses", {"enrollment_state": "active"}))

def get_teachers(course_id):
    teachers = []
    for u in paginate(f"/courses/{course_id}/users", {"enrollment_type[]": "teacher"}):
        name = u.get("name") or u.get("short_name")
        if name:
            teachers.append(name)
    # de-dup while preserving order
    seen, uniq = set(), []
    for t in teachers:
        if t not in seen:
            uniq.append(t); seen.add(t)
    return uniq

def get_assignments(course_id):
    # Returns assignments due from 2 days ago to 60 days ahead (adjust as needed)
    now = datetime.now(timezone.utc)
    items = list(paginate(
        f"/courses/{course_id}/assignments",
        {"include[]": "submission", "order_by": "due_at", "per_page": 100}
    ))
    out = []
    for a in items:
        due = a.get("due_at")
        if not due:
            continue
        try:
            d = parser.parse(due)
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if (now - timedelta(days=2)) <= d <= (now + timedelta(days=60)):
            out.append(a)
    return out

# ========= Notion helpers =========

COLOR_POOL = ["default","blue","green","red","yellow","purple","pink","brown","gray","orange"]
_DB_SCHEMA = None

def _refresh_schema():
    global _DB_SCHEMA
    _DB_SCHEMA = notion.databases.retrieve(database_id=NOTION_DB_ID)

def _color_for(name: str) -> str:
    return COLOR_POOL[hash(name) % len(COLOR_POOL)]

def _existing_options(prop_name: str, kind: str):
    """
    Return list of existing options for a property.
    kind in {"multi_select", "select"}.
    """
    if _DB_SCHEMA is None:
        _refresh_schema()
    prop = _DB_SCHEMA["properties"].get(prop_name)
    if not prop or kind not in prop:
        return []
    return prop[kind].get("options", [])

def ensure_multi_select_options(prop_name: str, names):
    names = [n for n in (names or []) if n]
    if not names:
        return
    existing = _existing_options(prop_name, "multi_select")
    existing_names = {o["name"] for o in existing}
    missing = [n for n in names if n not in existing_names]
    if not missing:
        return
    new_opts = [{"name": n, "color": _color_for(n)} for n in missing]
    notion.databases.update(
        database_id=NOTION_DB_ID,
        properties={prop_name: {"multi_select": {"options": existing + new_opts}}}
    )
    _refresh_schema()

def ensure_select_option(prop_name: str, name: str):
    if not name:
        return
    existing = _existing_options(prop_name, "select")
    existing_names = {o["name"] for o in existing}
    if name in existing_names:
        return
    new_opts = existing + [{"name": name, "color": _color_for(name)}]
    notion.databases.update(
        database_id=NOTION_DB_ID,
        properties={prop_name: {"select": {"options": new_opts}}}
    )
    _refresh_schema()

def infer_type(a):
    name = (a.get("name") or "").lower()
    url = (a.get("html_url") or "").lower()
    subs = [s.lower() for s in a.get("submission_types", [])]
    if "quiz" in name or "/quizzes/" in url or "online_quiz" in subs:
        return "Quiz"
    if any(w in name for w in ["exam","midterm","final","test"]):
        return "Test"
    return "Assignment"

def to_status(a):
    # Use Canvas submission state if present; otherwise default Not started.
    sub = a.get("submission") or {}
    state = (sub.get("workflow_state") or "").lower()
    if state in {"submitted", "graded"} or a.get("has_submitted_submissions"):
        return "Completed"
    return "Not started"

def ms(names):
    return {"multi_select": [{"name": n} for n in names if n]}

def sel(name):
    return {"select": {"name": name}} if name else None

def date_prop(iso_str):
    if not iso_str:
        return None
    try:
        d = parser.parse(iso_str)
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return {"date": {"start": d.isoformat()}}
    except Exception:
        return None

def build_props(a, course_name, teacher_names):
    # Unified "Tags" includes both Class and Teacher(s)
    tag_values = [course_name] + (teacher_names or [])
    status_name = to_status(a)
    done = status_name == "Completed"

    props = {
        "Assignment Name": {"title": [{"text": {"content": a["name"]}}]},
        "Class": sel(course_name) or {"select": None},
        "Teacher": ms(teacher_names or []),
        "Tags": ms(tag_values),
        "Assignment type": sel(infer_type(a)),
        "Status": {"status": {"name": status_name}},
        "Done": {"checkbox": done},
        "Due date": date_prop(a.get("due_at")),
        "Canvas ID": {"number": a["id"]},
        "URL": {"url": a.get("html_url") or None},
    }
    return {k: v for k, v in props.items() if v is not None}

def upsert_by_canvas_id(props):
    res = notion.databases.query(
        database_id=NOTION_DB_ID,
        filter={"property": "Canvas ID", "number": {"equals": props["Canvas ID"]["number"]}},
        page_size=1,
    )
    items = res.get("results", [])
    if items:
        notion.pages.update(page_id=items[0]["id"], properties=props)
    else:
        notion.pages.create(parent={"database_id": NOTION_DB_ID}, properties=props)

# ========= Main =========

def main():
    _refresh_schema()  # load DB schema once (used by ensure_* helpers)

    courses = get_courses()
    for course in courses:
        course_id = course["id"]
        course_name = course.get("name") or f"Course {course_id}"
        teachers = get_teachers(course_id)

        # Make sure options exist before we write pages
        ensure_select_option("Class", course_name)
        ensure_multi_select_options("Teacher", teachers)
        ensure_multi_select_options("Tags", [course_name] + teachers)

        for a in get_assignments(course_id):
            props = build_props(a, course_name, teachers)
            upsert_by_canvas_id(props)

if __name__ == "__main__":
    main()
