from datetime import datetime, timezone
from dateutil import parser as dtparser

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
        return {"select": {"name": "Completed"}, "checkbox": True}
    label = (existing_status_name or "Not started")
    done = (label.lower() == "completed")
    return {"select": {"name": label}, "checkbox": done}

def format_props(assignment, class_name, teacher_names, existing_status=None):
    due_at = parse_iso(assignment.get("due_at"))
    a_type = infer_type(assignment)

    submitted_at = None
    sub = assignment.get("submission") or {}
    if isinstance(sub, dict):
        submitted_at = sub.get("submitted_at")

    st = status_props(existing_status, submitted_at)

    due_date = {"start": due_at.date().isoformat()} if due_at else None

    teacher_text = ", ".join(teacher_names)

    props = {
        "Assignment Name": {
            "title": [
                {"text": {"content": assignment.get("name", "Untitled Assignment")}}
            ]
        },
        "Class": {"select": {"name": class_name}} if class_name else None,
        "Teacher": {
            "rich_text": [{"text": {"content": teacher_text}}]
        } if teacher_text else None,
        "Type": {"select": a_type},
        "Due date": {"date": due_date},
        "Status": {"select": st["select"]},
        "Done": {"checkbox": st["checkbox"]},
        "Canvas ID": {
            "rich_text": [
                {"text": {"content": str(assignment.get("id", ""))}}
            ]
        },
        "NA": {"select": {"name": "Jordan"}},
    }
    return {k: v for k, v in props.items() if v is not None}

# ----- Main sync -----

def run():
    # Ensure Notion token/DB wiring is correct up front
    verify_access()

    # Touch Canvas just to verify auth early (optional, keeps nice failures)
    _ = me_profile()

    # Pull courses and build taxonomy sets (for Class/Type/Status/NA options)
    courses = list_courses()
    course_names = []
    for c in courses:
        cname = c.get("course_code") or c.get("name")
        if cname:
            course_names.append(cname)

    ensure_taxonomy(class_names=course_names)

    for c in courses:
        cid = c.get("id")
        cname = c.get("course_code") or c.get("name")
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
