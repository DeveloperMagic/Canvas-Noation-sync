# Canvas → Notion sync with:
# - Env trimming + preflight checks (clear errors for bad tokens/IDs)
# - Real Notion multi-select tags for Teacher + combined Tags (Class + Teacher)
# - Auto-creation of missing tag/select options on the database
# - Assignment type inference; Status auto "Completed" if submitted; Priority is a Notion formula

import os
import sys
import hashlib
from datetime import datetime, timezone, timedelta

import requests
from dateutil import parser
from notion_client import Client

# --------- ENV (trim + basic validation) ---------
NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "").strip()
NOTION_DB_ID = os.environ.get("NOTION_DATABASE_ID", "").strip()
CANVAS_BASE_URL = os.environ.get("CANVAS_BASE_URL", "").strip().rstrip("/")
CANVAS_TOKEN = os.environ.get("CANVAS_TOKEN", "").strip()

if not NOTION_TOKEN or not NOTION_DB_ID:
    sys.exit("Missing NOTION_TOKEN or NOTION_DATABASE_ID env vars.")
if not NOTION_TOKEN.startswith("secret_"):
    sys.exit("NOTION_TOKEN does not start with 'secret_'. Use an Internal Integration token.")
if not CANVAS_BASE_URL or not CANVAS_TOKEN:
    sys.exit("Missing CANVAS_BASE_URL or CANVAS_TOKEN env vars.")

notion = Client(auth=NOTION_TOKEN)

# ========= Canvas helpers =========

def _canvas_get(url, params=None):
    headers = {"Authorization": f"Bearer {CANVAS_TOKEN}"}
    r = requests.get(url, headers=headers, params=params or None, timeout=30)
    if r.status_code == 401:
        raise SystemExit(
            "Canvas 401 Unauthorized. Verify CANVAS_BASE_URL, token scope/expiry, and that the token belongs to you."
        )
    r.raise_for_status()
    return r

def paginate(path, params=None):
    """Iterate through all pages of a Canvas API collection."""
    url = f"{CANVAS_BASE_URL}/api/v1{path}"
    first = True
    while url:
        r = _canvas_get(url, params=params if first else None)
        first = False
        data = r.json()
        for item in data:
            yield item
        url = r.links.get("next", {}).get("url")

def get_courses():
    # Only active enrollments
    return list(paginate("/courses", {"enrollment_state": "active"}))

def get_teachers(course_id):
    teachers = []
    for u in paginate(f"/courses/{course_id}/users", {"enrollment_type[]": "teacher"}):
        name = u.get("name") or u.get("short_name")
        if name:
            teachers.append(name)
    # de-dup preserve order
    seen, uniq = set(), []
    for t in teachers:
        if t not in seen:
            uniq.append(t)
            seen.add(t)
    return uniq

def get_assignments(course_id):
    """Assignments due from 2 days ago up to 60 days ahead."""
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

def preflight_canvas():
    # Confirms token + base URL by fetching your profile
    url = f"{CANVAS_BASE_URL}/api/v1/users/self/profile"
    r = _canvas_get(url)
    if r.status_code != 200:
        raise SystemExit(f"Canvas preflight failed with status {r.status_code}")

# ========= Notion helpers =========

COLOR_POOL = ["default","blue","green","red","yellow","purple","pink","brown","gray","orange"]
_DB_SCHEMA = None

def _refresh_schema():
    global _DB_SCHEMA
    _DB_SCHEMA = notion.databases.retrieve(database_id=NOTION_DB_ID)

def _stable_color_for(name: str) -> str:
    """Stable color choice per name using md5 hash (avoids Python hash salt)."""
    h = hashlib.md5(name.encode("utf-8")).hexdigest()
    idx = int(h[:8], 16) % len(COLOR_POOL)
    return COLOR_POOL[idx]

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
    new_opts = [{"name": n, "color": _stable_color_for(n)} for n in missing]
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
    new_opts = existing + [{"name": name, "color": _stable_color_for(name)}]
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

def preflight_notion():
    try:
        notion.databases.retrieve(database_id=NOTION_DB_ID)
    except Exception as e:
        raise SystemExit(
            "Failed to open Notion database.\n"
            "- Check NOTION_TOKEN (internal 'secret_...' with no spaces/newlines)\n"
            "- Check NOTION_DATABASE_ID (32 or 36 chars)\n"
            "- Share the DB with your integration via Notion → Share → Add connections\n"
            f"Raw error: {e}"
        )

# ========= Main =========

def main():
    # Preflight credentials
    preflight_notion()
    preflight_canvas()

    # Load DB schema for option management
    _refresh_schema()

    courses = get_courses()
    for course in courses:
        course_id = course["id"]
        course_name = course.get("name") or f"Course {course_id}"
        teachers = get_teachers(course_id)

        # Ensure options exist before we write pages
        ensure_select_option("Class", course_name)
        ensure_multi_select_options("Teacher", teachers)
        ensure_multi_select_options("Tags", [course_name] + teachers)

        for a in get_assignments(course_id):
            props = build_props(a, course_name, teachers)
            upsert_by_canvas_id(props)

if __name__ == "__main__":
    main()
