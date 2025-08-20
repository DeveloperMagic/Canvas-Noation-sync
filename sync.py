import os
import sys
import json
import math
import logging
from datetime import datetime, timedelta, timezone
from dateutil import parser as dtparser
import pytz
import requests

# ─────────────────────────────
# Config & Logging
# ─────────────────────────────
CANVAS_API_BASE = os.environ.get("CANVAS_API_BASE")
CANVAS_API_TOKEN = os.environ.get("CANVAS_API_TOKEN")
NOTION_DATABASE_ID = os.environ.get("NOTION_DATABASE_ID")
NOTION_TOKEN = os.environ.get("NOTION_TOKEN")
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

logging.basicConfig(stream=sys.stdout, level=LOG_LEVEL, format="%(levelname)s: %(message)s")
log = logging.getLogger("sync")

REQUIRED_ENV = [
    ("CANVAS_API_BASE", CANVAS_API_BASE),
    ("CANVAS_API_TOKEN", CANVAS_API_TOKEN),
    ("NOTION_DATABASE_ID", NOTION_DATABASE_ID),
    ("NOTION_TOKEN", NOTION_TOKEN),
]

missing = [k for k, v in REQUIRED_ENV if not v]
if missing:
    raise SystemExit(f"Missing required env vars: {', '.join(missing)}")

# Timezone handling
LOCAL_TZ = pytz.timezone("America/New_York")
NOW_UTC = datetime.now(timezone.utc)

# Notion API headers
NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}

# Canvas headers
CANVAS_HEADERS = {
    "Authorization": f"Bearer {CANVAS_API_TOKEN}",
}

# ─────────────────────────────
# Helpers
# ─────────────────────────────
def iso_to_dt(s):
    if not s:
        return None
    try:
        d = dtparser.isoparse(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.astimezone(timezone.utc)
    except Exception:
        return None

def dt_to_notion_date(dt_obj):
    if not dt_obj:
        return None
    return {
        "start": dt_obj.astimezone(LOCAL_TZ).isoformat(),
    }

def days_until(due_dt):
    if not due_dt:
        return math.inf
    delta = (due_dt - NOW_UTC).total_seconds() / 86400.0
    return delta

def calc_priority(due_dt):
    if not due_dt:
        return "Later"
    d = days_until(due_dt)
    if d < 0:
        return "Overdue"
    if d <= 2:
        return "High"
    if d <= 5:
        return "Medium"
    if d <= 7:
        return "Low"
    return "Later"

# ─────────────────────────────
# Canvas API wrappers
# ─────────────────────────────
def canvas_get(path, params=None):
    if path.startswith("http"):
        url = path
    else:
        base = CANVAS_API_BASE.rstrip("/")
        if not base.endswith("/api/v1"):
            log.warning("CANVAS_API_BASE usually ends with /api/v1; current=%s", base)
        url = f"{base}{path if path.startswith('/') else '/' + path}"
    s = requests.Session()
    s.headers.update(CANVAS_HEADERS)

    all_items = []
    while True:
        r = s.get(url, params=params)
        if r.status_code == 401:
            raise SystemExit("Canvas API returned 401 Unauthorized. Check CANVAS_API_TOKEN and CANVAS_API_BASE.")
        r.raise_for_status()
        data = r.json()
        if isinstance(data, list):
            all_items.extend(data)
        else:
            return data
        # pagination via Link header
        link = r.headers.get('Link', '')
        next_url = None
        for part in link.split(','):
            if 'rel="next"' in part:
                next_url = part[part.find('<')+1:part.find('>')]
        if next_url:
            url, params = next_url, None
        else:
            break
    return all_items

def fetch_active_courses_with_teachers():
    # include[]=teachers to get teacher list
    params = {
        "enrollment_state": "active",
        "include[]": "teachers",
        "per_page": 100,
    }
    courses = canvas_get("/courses", params)
    course_map = {}
    for c in courses:
        cid = c.get("id")
        name = c.get("name") or c.get("course_code") or f"Course {cid}"
        teachers = []
        for t in (c.get("teachers") or []):
            nm = t.get("display_name") or t.get("name")
            if nm:
                teachers.append(nm)
        course_map[cid] = {
            "name": name,
            "teachers": teachers or ["Unknown Teacher"],
        }
    return course_map

def fetch_course_assignments(course_id):
    params = {
        "order_by": "due_at",
        "per_page": 100,
        "bucket": "upcoming",
        "include[]": ["submission"],
    }
    return canvas_get(f"/courses/{course_id}/assignments", params)

def fetch_submission(course_id, assignment_id):
    try:
        data = canvas_get(f"/courses/{course_id}/assignments/{assignment_id}/submissions/self")
        return data or {}
    except Exception as e:
        log.debug("submission fetch failed: %s", e)
        return {}

# ─────────────────────────────
# Notion API wrappers
# ─────────────────────────────
NOTION_BASE = "https://api.notion.com/v1"

def notion_get_database(db_id):
    r = requests.get(f"{NOTION_BASE}/databases/{db_id}", headers=NOTION_HEADERS)
    r.raise_for_status()
    return r.json()

def notion_update_database(db_id, payload):
    r = requests.patch(f"{NOTION_BASE}/databases/{db_id}", headers=NOTION_HEADERS, data=json.dumps(payload))
    r.raise_for_status()
    return r.json()

def ensure_select_options(db_id, prop_name, new_option_names_with_colors):
    """Safely add select/multi-select options without deleting existing ones."""
    db = notion_get_database(db_id)
    props = db.get("properties", {})
    if prop_name not in props:
        log.warning("Property '%s' not found in Notion DB. Please create it with correct type.", prop_name)
        return

    prop = props[prop_name]
    if "select" in prop:
        kind = "select"
    elif "multi_select" in prop:
        kind = "multi_select"
    else:
        return

    existing = {opt["name"] for opt in prop[kind].get("options", [])}
    merged = list(prop[kind].get("options", []))

    for name, color in new_option_names_with_colors:
        if name not in existing:
            merged.append({"name": name, "color": color})

    payload = {
        "properties": {
            prop_name: {
                kind: {
                    "options": merged
                }
            }
        }
    }
    notion_update_database(db_id, payload)

def notion_query_by_unique(notion_db_id, canvas_id=None, name=None, class_name=None, due_date=None):
    """Try to find an existing page by Canvas ID; fallback to (Assignment Name + Class + Due Date)."""
    filters = []
    if canvas_id is not None:
        filters.append({
            "property": "Canvas ID",
            "number": {"equals": int(canvas_id)}
        })
    else:
        if name:
            filters.append({"property": "Assignment Name", "title": {"equals": name}})
        if class_name:
            filters.append({"property": "Class", "select": {"equals": class_name}})
        if due_date:
            filters.append({"property": "Due Date", "date": {"equals": due_date.astimezone(LOCAL_TZ).date().isoformat()}})

    if not filters:
        return []

    payload = {"filter": {"and": filters}}
    r = requests.post(f"{NOTION_BASE}/databases/{notion_db_id}/query", headers=NOTION_HEADERS, data=json.dumps(payload))
    r.raise_for_status()
    return r.json().get("results", [])

def notion_create_page(notion_db_id, props):
    payload = {
        "parent": {"database_id": notion_db_id},
        "properties": props,
    }
    r = requests.post(f"{NOTION_BASE}/pages", headers=NOTION_HEADERS, data=json.dumps(payload))
    try:
        r.raise_for_status()
    except Exception as e:
        log.error("Notion create failed: %s\nPayload: %s\nResp: %s", e, json.dumps(payload, indent=2), r.text)
        raise
    return r.json()

def notion_update_page(page_id, props):
    payload = {"properties": props}
    r = requests.patch(f"{NOTION_BASE}/pages/{page_id}", headers=NOTION_HEADERS, data=json.dumps(payload))
    try:
        r.raise_for_status()
    except Exception as e:
        log.error("Notion update failed: %s\nPayload: %s\nResp: %s", e, json.dumps(payload, indent=2), r.text)
        raise
    return r.json()

# ─────────────────────────────
# Property builders
# ─────────────────────────────
def make_title(name):
    return {"title": [{"type": "text", "text": {"content": name or "(untitled)"}}]}

def make_select(val):
    if not val:
        return None
    return {"select": {"name": val}}

def make_multi_select(options):
    opts = []
    for o in options:
        if o:
            opts.append({"name": o})
    return {"multi_select": opts}

def make_date(dt_obj):
    if not dt_obj:
        return {"date": None}
    return {"date": dt_to_notion_date(dt_obj)}

def make_url(url):
    return {"url": url} if url else {"url": None}

def make_number(n):
    return {"number": n} if n is not None else {"number": None}

# ─────────────────────────────
# Assignment type heuristic
# ─────────────────────────────
def infer_assignment_type(assignment):
    if assignment.get("quiz_id"):
        return "Quiz"
    name = (assignment.get("name") or "").lower()
    if any(k in name for k in ["exam", "midterm", "final", "test"]):
        return "Test"
    return "Assignment"

# ─────────────────────────────
# Sync logic
# ─────────────────────────────
def build_props(record):
    props = {}
    props["Assignment Name"] = make_title(record["name"])

    if record.get("class") is not None:
        props["Class"] = make_select(record["class"]) or {"rich_text": [{"text": {"content": record["class"]}}]}

    if record.get("teacher") is not None:
        props["Teacher"] = make_select(record["teacher"]) or {"rich_text": [{"text": {"content": record["teacher"]}}]}

    props["Tags"] = make_multi_select([record.get("class"), record.get("teacher")])
    props["Assignment Type"] = make_select(record.get("assignment_type")) or {"rich_text": [{"text": {"content": record.get("assignment_type") or ''}}]}
    props["Due Date"] = make_date(record.get("due_at"))
    props["Status"] = make_select(record.get("status")) or {"rich_text": [{"text": {"content": record.get("status") or ''}}]}
    props["Priority"] = make_select(record.get("priority")) or {"rich_text": [{"text": {"content": record.get("priority") or ''}}]}

    if record.get("canvas_id") is not None:
        props["Canvas ID"] = make_number(record["canvas_id"])
    props["Canvas URL"] = make_url(record.get("html_url"))

    return props

def ensure_base_options(class_name, teacher_name):
    ensure_select_options(NOTION_DATABASE_ID, "Status", [
        ("Not started", "gray"),
        ("Started", "blue"),
        ("Completed", "green"),
    ])
    ensure_select_options(NOTION_DATABASE_ID, "Assignment Type", [
        ("Quiz", "purple"),
        ("Assignment", "blue"),
        ("Test", "red"),
    ])
    ensure_select_options(NOTION_DATABASE_ID, "Priority", [
        ("Overdue", "red"),
        ("High", "orange"),
        ("Medium", "yellow"),
        ("Low", "green"),
        ("Later", "gray"),
    ])
    if class_name:
        ensure_select_options(NOTION_DATABASE_ID, "Class", [(class_name, "brown")])
        ensure_select_options(NOTION_DATABASE_ID, "Tags", [(class_name, "brown")])
    if teacher_name:
        ensure_select_options(NOTION_DATABASE_ID, "Teacher", [(teacher_name, "blue")])
        ensure_select_options(NOTION_DATABASE_ID, "Tags", [(teacher_name, "blue")])

def sync():
    log.info("Fetching courses…")
    course_map = fetch_active_courses_with_teachers()
    if not course_map:
        log.warning("No active courses returned by Canvas.")

    total_processed = 0
    for course_id, meta in course_map.items():
        class_name = meta["name"]
        teacher_name = meta["teachers"][0] if meta.get("teachers") else "Unknown Teacher"

        log.info("Course %s (%s) — teacher: %s", course_id, class_name, teacher_name)
        try:
            assignments = fetch_course_assignments(course_id)
        except Exception as e:
            log.error("Failed to fetch assignments for course %s: %s", course_id, e)
            continue

        for a in assignments:
            due_at = iso_to_dt(a.get("due_at"))
            # Window: past 30d to next 180d
            if due_at and not (NOW_UTC - timedelta(days=30) <= due_at <= NOW_UTC + timedelta(days=180)):
                continue

            submission = a.get("submission") or fetch_submission(course_id, a.get("id"))
            submitted_at = iso_to_dt(submission.get("submitted_at")) if submission else None
            status = "Completed" if submitted_at else "Not started"

            record = {
                "canvas_id": a.get("id"),
                "name": a.get("name") or f"Assignment {a.get('id')}",
                "class": class_name,
                "teacher": teacher_name,
                "assignment_type": infer_assignment_type(a),
                "due_at": due_at,
                "status": status,
                "priority": calc_priority(due_at),
                "html_url": a.get("html_url") or a.get("url"),
            }

            ensure_base_options(record["class"], record["teacher"])
            props = build_props(record)

            results = notion_query_by_unique(
                NOTION_DATABASE_ID,
                canvas_id=record["canvas_id"],
                name=record["name"],
                class_name=record["class"],
                due_date=record["due_at"],
            )

            if results:
                page_id = results[0]["id"]
                log.info("Updating: %s (%s)", record["name"], page_id)
                notion_update_page(page_id, props)
            else:
                log.info("Creating: %s", record["name"])
                notion_create_page(NOTION_DATABASE_ID, props)

            total_processed += 1

    log.info("Done. %d assignments processed.", total_processed)

if __name__ == "__main__":
    sync()
