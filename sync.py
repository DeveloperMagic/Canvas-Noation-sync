import os
import datetime as dt
from typing import Optional, Dict, Any

from notion_client import Client
from google.oauth2 import service_account
from googleapiclient.discovery import build

# -----------------------------
# ENV (same as your original)
# -----------------------------
NOTION_API_KEY = os.environ["NOTION_API_KEY"]
NOTION_DATABASE_ID = os.environ["NOTION_DATABASE_ID"]
GOOGLE_CALENDAR_ID = os.environ["GOOGLE_CALENDAR_ID"]

# -----------------------------
# Clients
# -----------------------------
notion = Client(auth=NOTION_API_KEY)

creds = service_account.Credentials.from_service_account_file(
    "credentials.json",
    scopes=["https://www.googleapis.com/auth/calendar.readonly"],
)
gcal = build("calendar", "v3", credentials=creds)

# -----------------------------
# Notion schema helpers
# -----------------------------
def db_schema() -> Dict[str, Any]:
    return notion.databases.retrieve(database_id=NOTION_DATABASE_ID)["properties"]

SCHEMA = db_schema()

def has_prop(name: str) -> bool:
    return name in SCHEMA

def prop_type(name: str) -> Optional[str]:
    return SCHEMA[name]["type"] if name in SCHEMA else None

def set_prop(props: Dict[str, Any], name: str, value: Any):
    """Set property while respecting existing Notion types and your names."""
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
    elif t == "people":
        # Requires Notion user IDs; skipping to avoid external lookups.
        pass

# -----------------------------
# De-dup / upsert helpers
# -----------------------------
def find_page_by_event_id(event_id: str) -> Optional[str]:
    """Upsert by Event ID if your DB has that property as rich_text."""
    if not (has_prop("Event ID") and prop_type("Event ID") == "rich_text"):
        return None
    q = notion.databases.query(
        **{
            "database_id": NOTION_DATABASE_ID,
            "filter": {"property": "Event ID", "rich_text": {"equals": event_id}},
            "page_size": 1,
        }
    )
    res = q.get("results", [])
    return res[0]["id"] if res else None

def find_page_by_title_and_due(title: str, due_start: str) -> Optional[str]:
    """Fallback match when Event ID property isn't present in your DB."""
    if not (has_prop("Assignment Name") and has_prop("Due date")):
        return None
    filters = {"and": [
        {"property": "Assignment Name", "title": {"equals": title}},
        {"property": "Due date", "date": {"equals": due_start}},
    ]}
    q = notion.databases.query(database_id=NOTION_DATABASE_ID, filter=filters, page_size=1)
    res = q.get("results", [])
    return res[0]["id"] if res else None

# -----------------------------
# Time & priority helpers
# -----------------------------
def parse_event_due_start(event: Dict[str, Any]) -> str:
    """Return RFC3339 for timed events or YYYY-MM-DD for all-day events (for Notion)."""
    start = event.get("start", {})
    if start.get("dateTime"):
        return start["dateTime"]  # already RFC3339 with TZ
    elif start.get("date"):
        return start["date"]      # all-day date
    else:
        # Fallback: now (UTC)
        return dt.datetime.now(dt.timezone.utc).isoformat()

def to_datetime_utc(value: str) -> dt.datetime:
    """Convert RFC3339 or YYYY-MM-DD to timezone-aware UTC datetime for math."""
    if "T" in value:
        # RFC3339 like '2025-08-19T15:04:05Z' or with +hh:mm
        v = value.replace("Z", "+00:00")
        return dt.datetime.fromisoformat(v).astimezone(dt.timezone.utc)
    else:
        # All-day date -> treat as end-of-day 23:59:59 UTC to give full day
        d = dt.date.fromisoformat(value)
        return dt.datetime(d.year, d.month, d.day, 23, 59, 59, tzinfo=dt.timezone.utc)

def compute_priority_label(due_start_value: str) -> str:
    """High ≤48h, Medium ≤120h, Low ≥168h; 5–7 day gap defaults to Medium."""
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
        # Between >120h and <168h (5–7 days)
        return "Medium"

# -----------------------------
# Derive tags from calendar event (no external data)
# -----------------------------
def derive_teacher(event: dict) -> str | None:
    # Prefer enriched Teacher from extendedProperties.private
    ext_priv = (event.get("extendedProperties") or {}).get("private") or {}
    if ext_priv.get("Teacher"):
        return ext_priv["Teacher"]

    # Fallbacks (service account creator / first attendee)
    creator = event.get("creator") or {}
    teacher = creator.get("displayName") or creator.get("email")
    if not teacher:
        for a in (event.get("attendees") or []):
            name = a.get("displayName") or a.get("email")
            if name:
                teacher = name
                break
    return teacher
# -----------------------------
# Build Notion properties from one event
# -----------------------------
def build_properties_from_event(event: Dict[str, Any]) -> Dict[str, Any]:
    props: Dict[str, Any] = {}

    title = event.get("summary") or "Untitled Assignment"
    due_start = parse_event_due_start(event)
    class_tag = derive_class(event)
    teacher_text = derive_teacher(event)
    priority = compute_priority_label(due_start)

    # Required fields (your exact names)
    set_prop(props, "Assignment Name", title)
    set_prop(props, "Due date", due_start)

    # Defaults / tags based on calendar
    set_prop(props, "Done", False)
    set_prop(props, "Status", "Not Started")
    if class_tag:
        set_prop(props, "Class", class_tag)   # works for select or multi_select
    if has_prop("Teacher") and prop_type("Teacher") == "rich_text":
        if teacher_text:
            set_prop(props, "Teacher", teacher_text)
    set_prop(props, "Priority", priority)

    # Optional extras if you already have these fields
    if has_prop("Event ID"):
        set_prop(props, "Event ID", event.get("id"))
    if has_prop("Event Link"):
        set_prop(props, "Event Link", event.get("htmlLink"))

    return props

def upsert_event(event: Dict[str, Any]):
    title = event.get("summary") or "Untitled Assignment"
    due_start = parse_event_due_start(event)

    # Prefer Event ID upsert if available
    page_id = None
    ev_id = event.get("id")
    if ev_id:
        page_id = find_page_by_event_id(ev_id)
    if not page_id:
        page_id = find_page_by_title_and_due(title, due_start)

    props = build_properties_from_event(event)

    if page_id:
        notion.pages.update(page_id=page_id, properties=props)
    else:
        notion.pages.create(parent={"database_id": NOTION_DATABASE_ID}, properties=props)

# -----------------------------
# Main sync (pagination; upcoming events)
# -----------------------------
def sync_from_calendar():
    now = dt.datetime.utcnow().isoformat() + "Z"
    page_token = None
    while True:
        res = gcal.events().list(
            calendarId=GOOGLE_CALENDAR_ID,
            timeMin=now,               # upcoming only
            singleEvents=True,
            orderBy="startTime",
            maxResults=2500,
            pageToken=page_token,
        ).execute()

        for ev in res.get("items", []):
            upsert_event(ev)

        page_token = res.get("nextPageToken")
        if not page_token:
            break

if __name__ == "__main__":
    sync_from_calendar()