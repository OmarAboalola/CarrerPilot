# CareerPilot

A local career dashboard that connects a reviewed CV, job opportunities, skill gaps,
Gmail conversations and draft outreach into next actions you can inspect.

## Run the judge demo in under five minutes

Prerequisites: **Python 3.12+ with pip**, an internet connection for the first dependency
download, and a browser. No API key, Google account, Node.js or database setup is needed.

Download/clone the repository, open a terminal in its folder, then run:

**Windows**

```powershell
py -3.12 run.py
```

Or use the included launcher, which detects an installed Python 3.12+:

```powershell
powershell -ExecutionPolicy Bypass -File .\Start.ps1
```

**macOS / Linux**

```bash
python3 run.py
```

The launcher creates `.venv`, installs pinned runtime dependencies once, creates the
local database automatically and serves **http://127.0.0.1:8765**. Keep the terminal
open; press **Ctrl+C** to stop. Subsequent launches reuse the environment.
Fresh Windows/Python 3.12 setup was verified in **84.2 seconds** with pip caching disabled; a repeat launch check took 2.2 seconds. Download speed affects first-run time. Installing Python and configuring optional live services are not included.

### Two-minute walkthrough

1. Open the address above and choose **Review profile**.
2. Choose **Load Omar CV example**, review the editable facts/goals and click **Confirm profile and goals**.
3. Click **Find my next move**. Inspect opportunities, **Why this fit?**, skill gaps and the next-action order.
4. Open **Gmail attention** and inspect the synthetic interview/recruiter examples. Ask a question about the scan.
5. Prepare a reply, edit/save it, **Approve exact draft**, then **Run draft simulation**. Check **Draft history** and **Activity**.

The quick demo uses **synthetic jobs/mail and local extraction, answers and drafts**.
Its simulation does not access Gmail. The bundled profile is an editable example;
its suggested goals must be reviewed. No live AI or real-mail success is implied by
the demo. Demo data stays in `.demo-data/`, separate from live credentials/data.

If port 8765 is busy, use `python run.py --port 8766` with your Python 3.12+ interpreter.
On Linux, a missing `ensurepip` may require your distribution's Python venv package.
If Windows reports Python 3.10, use `py -3.12` rather than an older `python` command.

## Optional live integrations

Copy `.env.example` to `.env`, configure only the integrations you need, then run:

```powershell
py -3.12 run.py --live
# Or: powershell -ExecutionPolicy Bypass -File .\Start.ps1 -Live
```

- **AI:** set `LLM_BASE_URL`, `LLM_MODEL` and `LLM_API_KEY`, or use **Model setup** to enter a Groq key locally. Approve CV/profile processing before using it; Gmail excerpts have a separate approval.
- **Jobs:** public Greenhouse boards work without a key. Optional JSearch needs your own `JSEARCH_API_KEY` and `JSEARCH_ENABLED=true`. Sources and provider quotas limit coverage.
- **Gmail:** set your Google web OAuth client ID/secret, enable Gmail API and register `http://127.0.0.1:8765/api/gmail/callback`. Connect read-only through the app. OAuth setup and provider approval are outside the five-minute demo.

Live settings and state stay in `.env` and `data/`. Default `run.py` always launches
the isolated demo, even if a live `.env` already exists. Never include either data
directory, API keys, OAuth tokens or virtual environments in a GitHub upload.

Sending and automatic job applications are disabled. Gmail draft creation requires
additional permission and review of the exact draft. Do not assume the mailbox owner
is the CV candidate. This is a loopback-only, single-user prototype: CV extraction,
eligibility, proficiency, source coverage and heuristic fit need human review.

## Development

The application uses FastAPI, a static HTML/CSS/JavaScript dashboard, SQLite/SQLAlchemy
and automatic Alembic migrations. `app/`, `static/`, `migrations/` and the runtime lock
are required. `tests/` and development dependencies are optional for running it.

After the launcher has created `.venv`:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check app tests scripts run.py
node --test tests/test_ui_state.js
node --check static/app.js
```

On macOS/Linux replace `.\.venv\Scripts\python.exe` with `.venv/bin/python`.
Node.js is needed only for JavaScript development checks. Tests use temporary storage
and controlled provider boundaries. The last application QA pass had **297 Python
and 34 JavaScript tests passing**; live read-only/model checks do not establish
production readiness or real Gmail write acceptance.

See [architecture](docs/architecture.md) and [data handling](docs/data_policy.md).
