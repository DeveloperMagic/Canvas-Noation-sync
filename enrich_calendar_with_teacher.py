import os, re, datetime as dt, requests
from google.oauth2 import service_account
from googleapiclient.discovery import build

CAL_ID = os.environ["GOOGLE_CALENDAR_ID"]
CREDS_FILE = "credentials.json"
SCOPES = ["https://www.googleapis.com/auth/calendar"]  # write access to update events

CANVAS_BASE = os.environ.get("CANVAS_API_BASE", "").rstrip("/")
CANVAS_TOKEN = os.environ.get("CANVAS_API_TOKEN", "")

# Detect Canvas assignment URL in the event
CANVAS_URL_RE = re.compile(r"https?://[^ \n]+/courses/(\d+)/assignments/(\d+)", re.I)

def gcal():
    creds = service_account.Credentials.from_service_account_file(CREDS_FILE, scopes=SCOPES)
    return build("calendar", "v3", credentials=creds)

def canvas_teacher(course_id: str) -> str | None:
    if not (CANVAS_BASE and CANVAS_TOKEN):
        return None
    url = f"{CANVAS_BASE}/api/v1/courses/{course_id}/users"
    headers = {"Authorization": f"Bearer {CANVAS_TOKEN}"}
    params = {"enrollment_type[]": "teacher", "per_page": 50}
    r = requests.get(url, headers=headers, params=params, timeout=15)
    if r.status_code != 200:
        return None
    for u in r.json():
        name = u.get("name") or u.get("sortable_name") or u.get("short_name")
        if name:
            return name
    return None

def extract_course(ev: dict) -> str | None:
    text = (ev.get("description") or "") + "\n" + (ev.get("summary") or "")
    m = CANVAS_URL_RE.search(text)
    return m.group(1) if m else None

def main():
    if not (CANVAS_BASE and CANVAS_TOKEN):
        print("Canvas env not set; skipping enrichment.")
        return

    svc = gcal()
    now = dt.datetime.utcnow().isoformat() + "Z"
    time_max = (dt.datetime.utcnow() + dt.timedelta(days=30)).isoformat() + "Z"
    page_token = None
    updated = 0

    while True:
        res = svc.events().list(
            calendarId=CAL_ID, timeMin=now, timeMax=time_max,
            singleEvents=True, orderBy="startTime", maxResults=2500,
            pageToken=page_token
        ).execute()

        for ev in res.get("items", []):
            course_id = extract_course(ev)
            if not course_id:
                continue
            teacher = canvas_teacher(course_id)
            if not teacher:
                continue

            ext = ev.get("extendedProperties") or {}
            priv = ext.get("private") or {}
            if priv.get("Teacher") == teacher:
                continue

            priv["Teacher"] = teacher
            ext["private"] = priv
            svc.events().patch(calendarId=CAL_ID, eventId=ev["id"], body={"extendedProperties": ext}).execute()
            updated += 1
            print(f"Updated: {ev.get('summary')} → Teacher={teacher}")

        page_token = res.get("nextPageToken")
        if not page_token:
            break

    print(f"Done. Updated {updated} event(s).")

if __name__ == "__main__":
    main()
