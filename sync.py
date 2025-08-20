from datetime import datetime, timezone
from dateutil import parser as dtparser
from dateutil.relativedelta import relativedelta

from canvas_api import list_courses, list_assignments, me_profile
from notion_api import ensure_taxonomy, upsert_page, verify_access
from utils import get_env
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
    done = label.lower() == "completed"
    return {"name": label, "done": done}

JORDAN_ID = get_env("JORDAN_ID", "JORDAN_PERSON_ID")


def format_props(assignment, class_name, teacher_names, existing_status=None):
    due_at = parse_iso(assignment.get("due_at"))
    a_type = infer_type(assignment)

    submitted_at = None
    sub = assignment.get("submission") or {}
    if isinstance(sub, dict):
        submitted_at = sub.get("submitted_at")

    st = status_props(existing_status, submitted_at)

    due_date = {"start": due_at.date().isoformat()} if due_at else None

    teacher_text = ", ".join(teacher_names) if teacher_names else ""

    props = {
        # The database keeps a regular text column for the assignment name
        "Name": {
            "title": [
                {"text": {"content": assignment.get("name", "Untitled Assignment")}}
            ]
        },
        "Assignment Name": {
            "rich_text": [
                {"text": {"content": assignment.get("name", "")}}
            ]
        },
        "Class": {"select": {"name": class_name}} if class_name else None,
        "Teacher": {
            "rich_text": [{"text": {"content": teacher_text}}]
        } if teacher_text else {"rich_text": []},
        "Type": {"select": a_type},
        "Due date": {"date": due_date},
        "Status": {"select": {"name": st["name"]}},
        "Done": {"checkbox": st["done"]},
        "Canvas ID": {
            "rich_text": [
                {"text": {"content": str(assignment.get("id", ""))}}
            ]
        },
        "NA": {"people": [{"id": JORDAN_ID}]} if JORDAN_ID else None,
    }
    return {k: v for k, v in props.items() if v is not None}

# ----- Main sync -----

def run():
    # Ensure Notion token/DB wiring is correct up front
    verify_access()

    # Touch Canvas just to verify auth early (optional, keeps nice failures)
    _ = me_profile()

    # Only consider assignments within an eight-month window around now
    now = datetime.now(timezone.utc)
    start_window = now - relativedelta(months=8)
    end_window = now + relativedelta(months=8)

    # Pull courses and build taxonomy sets (for Class/Type/Status options)
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
            due_at = parse_iso(a.get("due_at"))
            if not due_at:
                continue
            if due_at < start_window or due_at > end_window:
                continue
            props = format_props(a, cname, tnames, existing_status=None)
            upsert_page(a.get("id"), props)

if __name__ == "__main__":
    run()
