"""
Test that Google Calendar -> sync.py -> Notion link works.

What it does:
1) Inserts 3-4 distinctive test events into your GOOGLE_CALENDAR_ID.
2) (Optionally) runs your sync.py to push them to Notion.
3) Queries your Notion database to verify the pages exist.
4) (Optional) cleans up by deleting seeded events and archiving created pages.

Usage examples:
  python test_link_notion.py --run-sync
  python test_link_notion.py --run-sync --cleanup
"""

import os
import sys
import time
import json
import argparse
import datetime as dt
import subprocess
from typing import List, Dict

from google.oauth2 import service_account
from googleapiclient.discovery import build
from notion_client import Client as NotionClient

# ---------- Env & constants ----------
CAL_ID = os.environ["GOOGLE_CALENDAR_ID"]
NOTION_DB = os.environ["NOTION_DATABASE_ID"]
NOTION_KEY = os.environ["NOTION_API_KEY"]
CREDS_FILE = "credentials.json"

# Write scope needed to create test events
GCAL_SCOPES = ["https://www.googleapis.com/auth/calendar"]

def gcal_service():
    creds = service_account.Credentials.from_service_account_file(CREDS_FILE, scopes=GCAL_SCOPES)
    return build("calendar", "v3", credentials=creds)

def notion_client():
    return NotionClient(auth=NOTION_KEY)

def iso(dtobj: dt.datetime) -> str:
    return dtobj.isoformat()

def seed_events(prefix: str) -> List[Dict]:
    """Insert 4 events to exercise timed vs all-day and priority windows."""
    svc = gcal_service()
    now = dt.datetime.now(dt.timezone.utc)

    events = [
        # High (≤ 48h): in 6 hours, 1-hour duration
        {
            "summary": f"{prefix} High in 6h",
            "description": "Automated test event (timed).",
            "start": {"dateTime": iso(now + dt.timedelta(hours=6))},
            "end":   {"dateTime": iso(now + dt.timedelta(hours=7))},
            # Help your sync fill Teacher if you implemented the extendedProperties check:
            "extendedProperties": {"private": {"Teacher": "Test Teacher"}},
        },
        # Medium (≤120h): in 3 days
        {
            "summary": f"{prefix} Medium in 3d",
            "description": "Automated test event (timed).",
            "start": {"dateTime": iso(now + dt.timedelta(days=3, hours=2))},
            "end":   {"dateTime": iso(now + dt.timedelta(days=3, hours=3))},
            "extendedProperties": {"private": {"Teacher": "Test Teacher"}},
        },
        # Gap (6 days -> Medium by your rule)
        {
            "summary": f"{prefix} Gap 6d (Medium)",
            "description": "Automated test event (timed).",
            "start": {"dateTime": iso(now + dt.timedelta(days=6, hours=2))},
            "end":   {"dateTime": iso(now + dt.timedelta(days=6, hours=3))},
            "extendedProperties": {"private": {"Teacher": "Test Teacher"}},
        },
        # All-day in 8 days (Low)
        {
            "summary": f"{prefix} Low in 8d (all-day)",
            "description": "Automated test event (all-day).",
            "start": {"date": (now + dt.timedelta(days=8)).date().isoformat()},
            "end":   {"date": (now + dt.timedelta(days=9)).date().isoformat()},
            "extendedProperties": {"private": {"Teacher": "Test Teacher"}},
        },
    ]

    created = []
    for body in events:
        ev = svc.events().insert(calendarId=CAL_ID, body=body, sendUpdates="none").execute()
        created.append(ev)
        print(f"[seed] Created: {ev.get('summary')} -> {ev.get('id')}")

    return created

def run_sync_script():
    print("[sync] Running: python sync.py")
    # Run your existing sync script as a subprocess so we don't import or modify it.
    subprocess.run([sys.executable, "sync.py"], check=True)

def find_notion_pages_by_prefix(prefix: str) -> List[Dict]:
    n = notion_client()
    results = []
    cursor = None
    while True:
        payload = {
            "database_id": NOTION_DB,
            "filter": {"property": "Assignment Name", "title": {"contains": prefix}},
            "page_size": 25,
        }
        if cursor:
            payload["start_cursor"] = cursor
        resp = n.databases.query(**payload)
        results.extend(resp.get("results", []))
        if not resp.get("has_more"):
            break
        cursor = resp.get("next_cursor")
    return results

def summarize_page(page: Dict) -> str:
    props = page.get("properties", {})
    def get_title():
        t = props.get("Assignment Name", {}).get("title", [])
        return t[0]["plain_text"] if t else "(no title)"
    def get_date():
        d = props.get("Due date", {}).get("date", {}) or {}
        return d.get("start")
    def get_priority():
        p = props.get("Priority", {}).get("select")
        return p["name"] if p else None
    def get_class():
        c = props.get("Class", {}).get("select") or {}
        return c.get("name")
    def get_teacher():
        rt = props.get("Teacher", {}).get("rich_text", [])
        return rt[0]["plain_text"] if rt else None

    return json.dumps({
        "page_id": page["id"],
        "title": get_title(),
        "due": get_date(),
        "priority": get_priority(),
        "class": get_class(),
        "teacher": get_teacher(),
    }, ensure_ascii=False)

def delete_seeded_events(event_ids: List[str]):
    svc = gcal_service()
    for eid in event_ids:
        try:
            svc.events().delete(calendarId=CAL_ID, eventId=eid).execute()
            print(f"[cleanup] Deleted GCal event {eid}")
        except Exception as e:
            print(f"[cleanup] Could not delete event {eid}: {e}")

def archive_notion_pages(pages: List[Dict]):
    n = notion_client()
    for p in pages:
        try:
            n.pages.update(page_id=p["id"], archived=True)
            print(f"[cleanup] Archived Notion page {p['id']}")
        except Exception as e:
            print(f"[cleanup] Could not archive Notion page {p['id']}: {e}")

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-sync", action="store_true", help="Run sync.py after seeding events")
    parser.add_argument("--cleanup", action="store_true", help="Delete test events and archive created Notion pages")
    parser.add_argument("--prefix", default=f"SYNC_TEST_{int(time.time())}", help="Prefix for test event titles")
    args = parser.parse_args()

    print(f"[info] Using calendar: {CAL_ID}")
    print(f"[info] Using Notion DB: {NOTION_DB}")
    print(f"[info] Test prefix: {args.prefix}")

    # 1) Seed events
    created = seed_events(args.prefix)
    created_ids = [e["id"] for e in created]

    # (Optional) give Google a
