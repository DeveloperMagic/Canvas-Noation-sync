import datetime
import os
from notion_client import Client
from google.oauth2 import service_account
from googleapiclient.discovery import build

# Load secrets from environment
NOTION_API_KEY = os.environ["NOTION_API_KEY"]
NOTION_DATABASE_ID = os.environ["NOTION_DATABASE_ID"]
GOOGLE_CALENDAR_ID = os.environ["GOOGLE_CALENDAR_ID"]

# Connect to Notion
notion = Client(auth=NOTION_API_KEY)

# Connect to Google Calendar
creds_json = "credentials.json"
creds = service_account.Credentials.from_service_account_file(
    creds_json, scopes=["https://www.googleapis.com/auth/calendar.readonly"]
)
service = build("calendar", "v3", credentials=creds)

# Get events
now = datetime.datetime.utcnow().isoformat() + "Z"
events_result = service.events().list(
    calendarId=GOOGLE_CALENDAR_ID,
    timeMin=now,
    maxResults=10,
    singleEvents=True,
    orderBy="startTime",
).execute()
events = events_result.get("items", [])

# Push into Notion
for event in events:
    title = event["summary"]
    due_date = event["start"].get("dateTime", event["start"].get("date"))
    
    notion.pages.create(
        parent={"database_id": NOTION_DATABASE_ID},
        properties={
            "Name": {"title": [{"text": {"content": title}}]},
            "Due Date": {"date": {"start": due_date}},
            "Source": {"rich_text": [{"text": {"content": "Canvas"}}]},
        },
    )
