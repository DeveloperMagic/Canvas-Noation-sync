import os
from datetime import datetime, timezone
from dateutil import parser as dtparser
from dateutil.relativedelta import relativedelta
import re

from canvas_api import list_courses, list_assignments, me_profile
from notion_api import (
    ensure_schema,
    ensure_taxonomy,
    upsert_page,
    verify_access,
    get_flexible_schema,
)

# ----- Helpers -----

def parse_iso(iso):
    if not iso:
        return None
    try:
        return dtparser.isoparse(iso).astimezone(timezone.utc)
    except Exception:
        return None

def to_days_left(due_at):
    if not due_at:
        return None
    now = datetime.now(timezone.utc)
    delta = due_at - now
    return delta.total_seconds() / 86400.0

def compute_priority(due_at):
    days = to_days_left(due_at)
    if days is None:
        return {"name": "Low"}  # default when no due date
    if days <= 2:
        return {"name": "High"}
    if days <= 5:
        return {"name": "Medium"}
    return {"name": "Low"}

def infer_type(assignment):
    name = (assignment.get("name") or "").lower()
    if assignment.get("quiz_id"):
        return {"name": "Quiz"}
    if re.search(r"\b(exam|midterm|final|test)\b", name):
        return {"name": "Test"}
    return {"name": "Assignment"}

def to_iso_date(dt):
    """Return 'YYYY-MM-DD' for Notion date property."""
    if not dt:
        return None
    return dt.date().isoformat()

def to_mdy_date(dt):
    """Return 'MM/DD/YYYY' text string."""
    if not dt:
        return None
    m = f"{dt.month:02d}"
    d = f"{dt.day:02d}"
    y = f"{dt.year:04d}"
    return f"{m}/{d}/{y}"

def status_payload(status_prop, status_labels, submitted_at, default_to="not_started"):
    if not status_prop or not status_labels:
        return {}
    label = status_labels["completed"] if submitted_at else (status_labels.get(default_to) or status_labels["not_started"])
    if not label:
        return {}
    return {"status": {"name": label}}

def window_bounds():
    """Return (start_utc, end_utc) for +/- 5 months around now."""
    months = 5
    now = datetime.now(timezone.utc)
    start = now - relativedelta(months=months)
    end = now + relativedelta(months=months)
    return start, end

# ----- Main sync -----

def run():
    # 1) Validate access & required schema
    verify_access()
    ensure_schema()

    # 2) Discover DB shape (title, status, tags, etc.)
    schema = get_flexible_schema()
    title_prop          = schema["title_prop"]
    status_prop         = schema["status_prop"]
    status_labels       = schema["status_labels"]
    done_prop           = schema["done_checkbox"]
    class_prop          = schema["class_prop"]
    teacher_prop        = schema["teacher_prop"]
    type_prop           = schema["type_prop"]
    priority_prop       = schema["priority_prop"]
    due_date_prop_date  = schema["due_date_prop_date"]   # Notion 'date' type
    due_date_prop_text  = schema["due_date_prop_text"]   # rich_text for MM/DD/YYYY
    tags_prop           = schema["tags_prop"]

    # 3) Touch Canvas to fail early if credentials bad
    _ = me_profile()

    # 4) Build taxonomy (for options if those props exist)
    courses = list_courses()
    class_names, teacher_names = [], []
    for c in courses:
        cname = c.get("name")
        if cname: class_names.append(cname)
        for t in (c.get("teachers") or []):
            disp = t.get("display_name") or t.get("short_name") or t.get("name")
            if disp: teacher_names.append(disp)

    ensure_taxonomy(
        class_names=class_names,
        teacher_names=teacher_names,
        type_names=("Assignment","Quiz","Test"),
        priority=("High","Medium","Low"),
    )

    # 5) Determine the +/- 5-month window and log it
    start_window, end_window = window_bounds()
    print(f"[sync] Window: {start_window.isoformat()}  →  {end_window.isoformat()}")

    # 6) Upsert assignments within the window (DUE DATE REQUIRED)
    for c in courses:
        cid = c.get("id")
        cname = c.get("name")
        tnames = []
        for t in (c.get("teachers") or []):
            disp = t.get("display_name") or t.get("short_name") or t.get("name")
            if disp: tnames.append(disp)

        for a in list_assignments(cid):
            if a.get("deleted"):
                continue

            # --- RULE #1: skip if no due date ---
            due_at = parse_iso(a.get("due_at"))
            if not due_at:
                continue

            # Only keep items due within +/- 5 months of now
            if not (start_window <= due_at <= end_window):
                continue

            title_text = a.get("name", "Untitled Assignment")
            due_iso = to_iso_date(due_at)   # 'YYYY-MM-DD' for Notion date prop
            due_mdy = to_mdy_date(due_at)   # 'MM/DD/YYYY' text string

            a_type = infer_type(a)
            priority = compute_priority(due_at)
            sub = a.get("submission") or {}
            submitted_at = sub.get("submitted_at")

            props = {}

            # Title
            props[title_prop] = {"title": [{"text": {"content": title_text}}]}

            # Date (both kinds if present): date prop gets ISO; text prop gets MM/DD/YYYY
            if due_date_prop_date and due_iso:
                props[due_date_prop_date] = {"date": {"start": due_iso}}
            if due_date_prop_text and due_mdy:
                props[due_date_prop_text] = {"rich_text": [{"text": {"content": due_mdy}}]}

            # Status
            st = status_payload(status_prop, status_labels, submitted_at)
            if st:
                props[status_prop] = st["status"]

            # Done checkbox mirrors Completed
            if done_prop:
                props[done_prop] = {"checkbox": bool(submitted_at)}

            # Priority
            if priority_prop:
                props[priority_prop] = {"select": priority}
            elif tags_prop and priority and priority.get("name"):
                props.setdefault(tags_prop, {"multi_select": []})
                props[tags_prop]["multi_select"].append({"name": priority["name"]})

            # Type
            if type_prop:
                props[type_prop] = {"select": a_type}
            elif tags_prop:
                props.setdefault(tags_prop, {"multi_select": []})
                props[tags_prop]["multi_select"].append({"name": a_type["name"]})

            # Class / Teacher
            added_tags = []
            if class_prop:
                props[class_prop] = {"multi_select": [{"name": cname}]} if cname else {"multi_select": []}
            else:
                if cname and tags_prop:
                    added_tags.append({"name": cname})

            if teacher_prop:
                props[teacher_prop] = {"multi_select": [{"name": t} for t in tnames]}
            else:
                if tags_prop:
                    for t in tnames:
                        added_tags.append({"name": t})

            if tags_prop and added_tags:
                props.setdefault(tags_prop, {"multi_select": []})
                props[tags_prop]["multi_select"].extend(added_tags)

            # Canvas ID (Number)
            props["Canvas ID"] = {"number": a.get("id")}

            # Upsert with duplicate protection: CanvasID → Title + Date(Text) fallback
            upsert_page(
                a.get("id"),
                props,
                title_prop=title_prop,
                title_text=title_text,
                due_date_prop_date=due_date_prop_date,
                due_str_iso=due_iso,
                due_date_prop_text=due_date_prop_text,
                due_str_mdy=due_mdy,
            )

if __name__ == "__main__":
    run()
