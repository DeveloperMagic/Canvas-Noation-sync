import os
import requests
from urllib.parse import urljoin
from utils import retry

BASE = os.environ.get("CANVAS_API_BASE", "").rstrip("/")
TOKEN = os.environ.get("CANVAS_API_TOKEN", "")

if not BASE or not TOKEN:
    raise SystemExit("Missing CANVAS_API_BASE or CANVAS_API_TOKEN env vars.")

HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Accept": "application/json",
}

@retry((requests.HTTPError, requests.ConnectionError), tries=4, delay=1.0, backoff=2.0)
def _get(url, params=None):
    r = requests.get(url, headers=HEADERS, params=params, timeout=30)
    if r.status_code == 401:
        raise requests.HTTPError("Unauthorized (401) from Canvas")
    r.raise_for_status()
    return r

def paged_get(path, params=None):
    url = urljoin(BASE, f"/api/v1{path}")
    while url:
        r = _get(url, params=params)
        yield r.json()
        # Parse Link header for pagination
        link = r.headers.get("Link", "")
        url = None
        if link:
            parts = [p.strip() for p in link.split(",")]
            for p in parts:
                if 'rel="next"' in p:
                    url = p[p.find("<")+1:p.find(">")]
                    break

def me_profile():
    r = _get(urljoin(BASE, "/api/v1/users/self/profile"))
    return r.json()

def list_courses():
    # include teachers to tag instructor names
    params = {"enrollment_state": "active", "include[]": "teachers", "per_page": 100}
    courses = []
    for page in paged_get("/courses", params=params):
        courses.extend(page)
    return courses

def list_assignments(course_id):
    # include submission info so we can auto-complete when submitted
    params = {"include[]": ["submission"], "per_page": 100, "order_by": "due_at"}
    assigns = []
    for page in paged_get(f"/courses/{course_id}/assignments", params=params):
        assigns.extend(page)
    return assigns
