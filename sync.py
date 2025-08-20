import os
from datetime import datetime, timezone
from dateutil import parser as dtparser
from dateutil.relativedelta import relativedelta

from canvas_api import list_courses, list_assignments, me_profile
from notion_api import ensure_taxonomy, upsert_page, verify_access
import re

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
    # Canvas sets quiz_id for quiz assignments
    name = (assignment.get("name") or "").lower()
    if assignment.get("quiz_id"):
        return {"name": "Quiz"}
    if re.search(r"\b(exam|midterm|final|test)\b", name):
        return {"name": "Test"}
    return {"name": "Assignment"}

def status_props(existing_status_name, submitted_at):
    # Preserve user's manual status unless we detect submission
    if submitted_at:
        return {"status": {"name": "Completed"}, "checkbox": True}
    label = (existing_status_name or "Not started")
    done = (label.lower() == "completed")
    return {"status": {"name": label}, "checkbox": done}

def format_props(assignment, course_name, teacher_names, existing_status=None):
    due_at = parse_iso(assignment.get("due_at"))
    priority = compute_priority(due_at)
    a_type = infer_type(assignment)

    submitted_at = None
    sub = assignment.get("submission") or {}
    if isinstance(sub, dict):
        submitted_at = sub.get("submitted_at")

    st = status_props(existing_status, submitted_at)

    props = {
        "Assignment Name": {
            "title": [{"text": {"content": assignment.get("name", "Untitled Assignment")}}]
        },
        "Class": {"multi_select": [{"name": course_name}] if course_name else []},
        "Teacher": {"multi_select": [{"name": t} for t in (teacher_names or [])]},
        "Type": {"select": a_type},
        "Due date": {"date": {"start": due_at.isoformat() if due_at else None}},
        "Priority": {"select": priority},
        "Status": {"status": st["status"]},
        "Done": {"checkbox": st["checkbox"]},
        "Canvas ID": {"number": assignment.get("id")},
    }
    return props

# ----- Main sync -----

def run():
    # Ensure Notion token/DB wiring is correct up front
    verify_access()

    # Touch Canvas just to verify auth early (optional, keeps nice failures)
    _ = me_profile()

    # Pull courses and build taxonomy sets (for Class/Teacher tag options)
    courses = list_courses()
    class_names = []
    teacher_names = []
    for c in courses:
        cname = c.get("name")
        if cname:
            class_names.append(cname)
        teachers = c.get("teachers") or []
        for t in teachers:
            disp = t.get("display_name") or t.get("short_name") or t.get("name")
            if disp:
                teacher_names.append(disp)

    ensure_taxonomy(class_names=class_names, teacher_names=teacher_names)

    for c in courses:
        cid = c.get("id")
        cname = c.get("name")
        teachers = c.get("teachers") or []
        tnames = []
        for t in teachers:
            disp = t.get("display_name") or t.get("short_name") or t.get("name")
            if disp:
                tnames.append(disp)

        assignments = list_assignments(cid)
        for a in assignments:
            if a.get("deleted"):
                continue
            # If you prefer to skip items with no due date, uncomment:
            # if not a.get("due_at"): continue
            props = format_props(a, cname, tnames, existing_status=None)
            upsert_page(a.get("id"), props)

if __name__ == "__main__":
    run()
