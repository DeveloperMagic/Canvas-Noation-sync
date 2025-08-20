# Canvas → Notion Sync (every 10 minutes)

Sync your **Canvas assignments** into a **Notion database**.
Designed to run via **GitHub Actions** every 10 minutes.

## What it does

- For each active Canvas course, pulls assignments with a due date.
- Creates/updates a page in your Notion database with the following properties:

| Notion Property    | Type         | Notes |
|--------------------|--------------|-------|
| **Assignment Name**| Title        | Page title in Notion |
| **Class**          | Checkbox     | Checked for class assignments |
| **Teacher**        | Select       | Instructor (auto-added if missing) |
| **Type**           | Select       | One of: Assignment, Quiz, Test (auto-added) |
| **Due date**       | Date         | Canvas due date (UTC) |
| **Status**         | Select       | Not started / In Progress / Completed; auto-set to Completed if submitted |
| **Done**           | Checkbox     | Mirrors Completed status |
| **Canvas ID**      | Text         | Hidden helper for de-dup and updates |

> Your database can show any subset of these columns. The names must match exactly.

## Setup

1. Create a **Notion database** with the exact property names above.
   - Add a **Status** select property with options such as:
     - *Not started*, *In Progress*, *Completed* (case-insensitive match is OK).
2. In Notion, share that database with an integration and copy **NOTION_TOKEN**.
3. In **GitHub → Settings → Secrets and variables → Actions → Secrets**, add:
   - `CANVAS_API_BASE` — e.g. `https://youruniversity.instructure.com`
   - `CANVAS_API_TOKEN` — a valid Canvas API token
   - `NOTION_DATABASE_ID` — the 32-char database ID from the Notion URL
   - `NOTION_TOKEN` — your Notion integration token
   - *Alternatively, generic names like `API_TOKEN`, `TOKEN`, `API_KEY`, or `DATABASE_ID` may also be used.*
4. Push this repo to GitHub. The included workflow runs every **10 minutes**.

## Local test (optional)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Default names shown; other aliases like `API_TOKEN`, `TOKEN`, `API_KEY`, or `DATABASE_ID` also work.
export CANVAS_API_BASE="https://<your>.instructure.com"
export CANVAS_API_TOKEN="<canvas token>"
export NOTION_DATABASE_ID="<db id>"
export NOTION_TOKEN="<notion token>"

python sync.py
```

## Notes

- **Type** is inferred from Canvas (quiz) or the assignment name (contains words like "exam"/"test").
- If the **Status** is manually set by you in Notion (e.g., “Started”), the script preserves it unless Canvas explicitly reports a **submitted** timestamp, in which case it will set **Completed**.
- You can safely hide the **Canvas ID** column in your database.
