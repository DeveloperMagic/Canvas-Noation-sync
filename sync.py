# sync.py — Canvas → Notion (no enrichment, no Google Calendar)
import os
import datetime as dt
from typing import Dict, Any, Optional, Iterable, Tuple
import requests
from notion_client import Client

# ── ENV ──────────────────────────────────────────────────────
NOTION_API_KEY = os.environ["NOTION_API_KEY"]
NOTION_DATABASE_ID = os.environ["NOTION_DATABASE_ID"]

# Canvas base like "https://rutgers.instructure.com"
CANVAS_API_BASE = os.environ["CANVAS_API_BASE"].rstrip("/")
CANVAS_API_TOKEN = os.environ["CANVAS_API_TOKEN"]

# How far ahead to sync (days). Default 30.
LOOKAHEAD_DAYS = int(os.environ.get("CANVAS_LOOKAHEAD_DAYS", "30"))

# ── Clients ─────────────────────────────────────────────────
notion = Client(auth=NOTION_API_KEY)

S = requests.Session()
S.headers.update({"Authorization": f"Bearer {CANVAS_API_TOKEN}"})


def canvas_get(path: str, params: Dict[str, Any] | None = None) -> Tuple[list, Optional[str]]:
    """GET Canvas API with one page; return (json, next_url)."""
    url = f"{CANVAS_API_BASE}/api/v1{path}"
    r = S.get(url, params=params, timeout=30)
    r.raise_for_status()
    data = r.json()
    next_url = None
    if "link" in r.headers:
        for part in r.headers["link"].split(","):
            seg = part.split(";")
            if len(seg) >= 2 and 'rel="next"' in seg[1]:
                next_url = seg[0].strip().strip("<>")
                break
    return data, next_url


def canvas_list(path: str, params: Dict[str, Any] | None = None) -> Iterable[dict]:
    """Iterate all pages for a Canvas endpoint."""
    data, next_url = canvas_get(path, params)
    for x in data:
        yield x
    while next_url:
        r = S.get(next_url, timeout=30)
        r.raise_for_status()
        data = r.json()
        for x in data:
            yield x
        next_url = None
        if "link" in r.headers:
            for part in r.headers["link"].split(","):
                seg = part.split(";")
                if len(seg) >= 2 and 'rel="next"' in seg[1]:
                    next_url = seg[0].strip().strip("<>")
                    break


# ── Notion helpers (use your exact property names) ───────────
def db_schema() -> Dict[str, Any]:
    return notion.databases.retrieve(database_id=NOTION_DATABASE_ID)["properties"]


SCHEMA = db_schema()


def has_prop(name: str) -> bool:
    return name in SCHEMA


def prop_type(name: str) -> Optional[str]:
    return SCHEMA[name]["type"] if name in SCHEMA else None


def set_prop(props: Dict[str, Any], name: str, value: Any):
    """Write a value respecting the existing Notion type."""
    if not has_prop(name) or value is None:
        return
    t = prop_type(name)
    if t == "title":
        props[name] = {"title": [{"text": {"content": str(value)[:2000] or "Untitled"}}]}
    elif t == "rich_text":
        props[name] = {"rich_text": [{"text": {"content": str(value)[:2000]}}]}
    elif t == "checkbox":
        props[name] = {"checkbox": bool(value)}
    elif t == "date":
        props[name] = {"date": {"start": value}}
    elif t == "select":
        props[name] = {"select": {"name": str(value)}}
    elif t == "multi_select":
        if isinstance(value, (list, tuple)):
            props[name] = {"multi_select": [{"name": str(v)} for v in value if v]}
        else:
            props[name] = {"multi_select": [{"name": str(value)}]} if value else {"multi_select": []}
    elif t == "url":
        props[name] = {"url": str(value)}
    elif t == "number":
        try:
            props[name] = {"number": float(value)}
        except Exception:
            pass
    # people type skipped (needs Notion user IDs)


def find_page_by_event_id(event_id: str) -> Optional[str]:
    if not (has_prop("Event ID") and prop_type("Event ID") == "rich_text"):
        return None
    resp = notion.databases.query(
        **{
            "database_id": NOTION_DATABASE_ID,
            "filter": {"property": "Event ID", "rich_text": {"equals": event_id}},
            "page_size": 1,
        }
    )
    res = resp.get("results", [])
    return res[0]["id"] if res else None


def find_page_by_title_and_due(title: str, due_start: str) -> Optional[str]:
    if not (has_prop("Assignment Name") and has_prop("Due date")):
        return None
    filters = {
        "and": [
            {"property": "Assignment Name", "title": {"equals": title}},
            {"property": "Due date", "date": {"equals": due_start}},
        ]
    }
    resp = notion.databases.query(database_id=NOTION_DATABASE_ID, filter=filters, page_size=1)
    res = resp.get("results", [])
    return res[0]["id"] if res else None


# ── Priority helpers ─────────────────────────────────────────
def to_datetime_utc(value: str) -> dt.datetime:
    if "T" in value:
        v = value.replace("Z", "+00:00")
        return dt.datetime.fromisoformat(v).astimezone(dt.timezone.utc)
    else:
        d = dt.date.fromisoformat(value)
        return dt.datetime(d.year, d.month, d.day, 23, 59, 59, tzinfo=dt.timezone.utc)


def compute_priority_label(due_start_value: str) -> str:
    """High ≤48h, Medium ≤120h, Low ≥168h; gap 5–7 days defaults to Medium."""
    now_utc = dt.datetime.now(dt.timezone.utc)
    due_dt_utc = to_datetime_utc(due_start_value)
    hours_left = (due_dt_utc - now_utc).total_seconds() / 3600.0
    if hours_left <= 48:
        return "High"
    elif hours_left <= 120:
        return "Medium"
    elif hours_left >= 168:
        return "Low"
    else:
        return "Medium"


# ── Canvas fetch ─────────────────────────────────────────────
def fetch_courses() -> list[dict]:
    # include teachers so we can tag "Teacher"
    params = {"enrollment_state": "active", "include[]": "teachers", "per_page": 100}
    return list(canvas_list("/courses", params))


def fetch_upcoming_assignments(course_id: int) -> list[dict]:
    # try upcoming; some instances don't support bucket filter well
    items = list(canvas_list(f"/courses/{course_id}/assignments", {"bucket": "upcoming", "per_page": 100}))
    if items:
        return items
    # fallback: fetch all and filter client-side by due_at in the future
    items = list(canvas_list(f"/courses/{course_id}/assignments", {"per_page": 100}))
    now = dt.datetime.now(dt.timezone.utc)
    out = []
    for a in items:
        due = a.get("due_at")
        if due:
            try:
                if to_datetime_utc(due) >= now:
                    out.append(a)
            except Exception:
                pass
    return out


# ── Notion mapping from Canvas assignment ───────────────────
def upsert_assignment(course: dict, assignment: dict):
    title = assignment.get("name") or "Untitled Assignment"
    due = assignment.get("due_at")
    if not due:
        # skip items without due date (change if you prefer to create anyway)
        return

    # Class tag = course_code or course name
    class_tag = course.get("course_code") or course.get("name")

    # Teacher = first teacher name on the course (if any)
    teacher = None
    for t in (course.get("teachers") or []):
        teacher = t.get("display_name") or t.get("name") or t.get("short_name")
        if teacher:
            break

    priority = compute_priority_label(due)

    # Stable idempotency key
    event_id = f"canvas:assign:{course.get('id')}:{assignment.get('id')}"

    props: Dict[str, Any] = {}
    set_prop(props, "Assignment Name", title)
    set_prop(props, "Due date", due)            # Canvas due_at is RFC3339; Notion accepts it
    set_prop(props, "Done", False)
    set_prop(props, "Status", "Not Started")
    if class_tag:
        set_prop(props, "Class", class_tag)
    if has_prop("Teacher") and prop_type("Teacher") == "rich_text" and teacher:
        set_prop(props, "Teacher", teacher)
    set_prop(props, "Priority", priority)
    if has_prop("Event ID"):
        set_prop(props, "Event ID", event_id)
    if has_prop("Event Link"):
        set_prop(props, "Event Link", assignment.get("html_url"))

    page_id = find_page_by_event_id(event_id) or find_page_by_title_and_due(title, due)
    if page_id:
        notion.pages.update(page_id=page_id, properties=props)
        print(f"[update] {title}")
    else:
        notion.pages.create(parent={"database_id": NOTION_DATABASE_ID}, properties=props)
        print(f"[create] {title}")


def sync_from_canvas():
    print("[init] Fetching Canvas courses…")
    courses = fetch_courses()
    print(f"[init] {len(courses)} course(s)")

    horizon_utc = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=LOOKAHEAD_DAYS)
    total = 0

    for course in courses:
        cid = course.get("id")
        if not cid:
            continue
        assignments = fetch_upcoming_assignments(cid)
        for a in assignments:
            due = a.get("due_at")
            if not due:
                continue
            try:
                if to_datetime_utc(due) > horizon_utc:
                    continue
            except Exception:
                continue
            upsert_assignment(course, a)
            total += 1

    print(f"[done] processed {total} assignment(s)")


if __name__ == "__main__":
    # quick token sanity check (prints your Canvas profile name/id)
    me, _ = canvas_get("/users/self/profile")
    print(f"[auth] Canvas OK for: {me.get('name') or me.get('short_name') or me.get('id')}")
    sync_from_canvas()
