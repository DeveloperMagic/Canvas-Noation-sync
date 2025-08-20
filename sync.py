from datetime import datetime, timezone
from dateutil import parser as dtparser

from canvas_api import list_courses, list_assignments, me_profile
from notion_api import ensure_taxonomy, upsert_page, verify_access, retrieve_db
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
        return {"name": "Completed", "done": True}
    label = (existing_status_name or "Not started")
    done = (label.lower() == "completed")
    return {"name": label, "done": done}

def format_props(assignment, teacher_names, class_name, db_props, existing_status=None):
    due_at = parse_iso(assignment.get("due_at"))
    a_type = infer_type(assignment)

    submitted_at = None
    sub = assignment.get("submission") or {}
    if isinstance(sub, dict):
        submitted_at = sub.get("submitted_at")

    st = status_props(existing_status, submitted_at)

    due_date = {"start": due_at.date().isoformat()} if due_at else None

    props = {
        "Assignment Name": {
            "title": [
                {"text": {"content": assignment.get("name", "Untitled Assignment")}}
            ]
        }
    }

    cprop = db_props.get("Class", {})
    if cprop.get("type") == "select" and class_name:
        props["Class"] = {"select": {"name": class_name}}
    elif cprop.get("type") == "checkbox":
        props["Class"] = {"checkbox": True}

    tprop = db_props.get("Teacher", {})
    if tprop.get("type") == "select" and teacher_names:
        props["Teacher"] = {"select": {"name": teacher_names[0]}}

    typrop = db_props.get("Type", {})
    if typrop.get("type") == "select":
        props["Type"] = {"select": a_type}

    dprop = db_props.get("Due date", {})
    if dprop.get("type") == "date":
        props["Due date"] = {"date": due_date}

    sprop = db_props.get("Status", {})
    if sprop.get("type") == "status":
        props["Status"] = {"status": {"name": st["name"]}}
    elif sprop.get("type") == "select":
        props["Status"] = {"select": {"name": st["name"]}}

    dprop = db_props.get("Done", {})
    if dprop.get("type") == "checkbox":
        props["Done"] = {"checkbox": st["done"]}

    cid_prop = db_props.get("Canvas ID", {})
    if cid_prop.get("type") == "rich_text":
        props["Canvas ID"] = {
            "rich_text": [
                {"text": {"content": str(assignment.get("id", ""))}}
            ]
        }

    return props

# ----- Main sync -----

def run():
    # Ensure Notion token/DB wiring is correct up front
    verify_access()

    # Touch Canvas just to verify auth early (optional, keeps nice failures)
    _ = me_profile()

    # Pull courses and build taxonomy sets (for Teacher/Type/Status options)
    courses = list_courses()
    teacher_names = []
    for c in courses:
        teachers = c.get("teachers") or []
        for t in teachers:
            disp = t.get("display_name") or t.get("short_name") or t.get("name")
            if disp:
                teacher_names.append(disp)

    ensure_taxonomy(teacher_names=teacher_names)
    db_props = retrieve_db().get("properties", {})

    for c in courses:
        cid = c.get("id")
        course_name = c.get("name")
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
            props = format_props(a, tnames, course_name, db_props, existing_status=None)
            upsert_page(a.get("id"), props)

if __name__ == "__main__":
    run()
