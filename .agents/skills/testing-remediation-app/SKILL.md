---
name: testing-remediation-app
description: How to run unit tests, integration tests, and dashboard UI tests for the Autonomous Issue Remediation Engine. Covers webhook payload testing, HMAC signing, DB seeding, and common pitfalls.
---

# Testing the Autonomous Issue Remediation Engine

## Overview
This app receives GitHub webhook events for issues, detects the issue type, and creates Devin AI sessions to remediate them. Testing involves verifying webhook payload handling, type detection logic, and dashboard rendering.

## Devin Secrets Needed
- `GITHUB_WEBHOOK_SECRET` — used for HMAC-SHA256 signature validation on webhook payloads
- `DEVIN_API_KEY` — Devin API key (can use dummy value for webhook-only tests)
- `DEVIN_ORG_ID` — Devin org ID (can use dummy value for webhook-only tests)
- `REPOSITORY_URL` — target GitHub repo URL

## Running Unit Tests
```bash
cd /home/ubuntu/repos/Autonomous-Issue-Remediation-Engine
python -m pytest tests/ -v --tb=short
```
All tests should pass (76+ tests as of v1.9).

## Testing Webhook Handler (Integration)

### Key Concepts
- The webhook handler at `src/webhook.py` has a `_get_issue_type()` function that detects issue type via:
  1. Native `issue.type.name` (org repos only)
  2. Title prefix fallback `[Bug]`/`[Feature]`/`[Task]` (personal repos)
- Use `@patch("src.webhook._process_job")` to mock the remediation thread and avoid real Devin API calls
- Use `assertLogs("src.webhook", level="INFO")` to capture log output
- Log messages use format `INFO:src.webhook:Accepted issue #N: title (type=X)` — check for `"Accepted issue"` and `"type=bug"`, NOT for `"issue_accepted"` (that's in the `extra` dict, not the message text)
- For skipped issues, check for `"not in configured types"` in logs

### HMAC Signing for Test Payloads
```python
import hashlib, hmac, json

def sign(payload_bytes, secret):
    sig = hmac.new(secret.encode(), payload_bytes, hashlib.sha256).hexdigest()
    return f"sha256={sig}"
```

### Minimal Test Setup
```python
import os
os.environ["GITHUB_WEBHOOK_SECRET"] = "test-secret"
os.environ["DEVIN_API_KEY"] = "test-key"
os.environ["DEVIN_ORG_ID"] = "test-org"
os.environ["REPOSITORY_URL"] = "https://github.com/test/repo"
os.environ["DATABASE_PATH"] = "/tmp/test_sessions.db"

from app import create_app
app = create_app()
client = app.test_client()
```

## Testing Dashboard UI

### Seeding Test Data
The database schema uses these column names (NOT `devin_session_id`):
- `session_id` (TEXT UNIQUE) — the Devin session ID
- `issue_number` (INTEGER)
- `issue_title` (TEXT)
- `repository_url` (TEXT) — NOT `repo_url`
- `status`, `status_detail`, `pr_url`, `devin_url`
- `acus_consumed` (REAL) — NOT `cost_acu`
- `created_at`, `updated_at` (TIMESTAMP)
- `devin_started_at`, `pr_raised_at` (TIMESTAMP)
- `time_to_remediation_seconds` (INTEGER)

Use `src.database.init_db()` to create tables, then insert directly via sqlite3.

### Running Flask Dev Server
```bash
DATABASE_PATH=/tmp/test.db \
GITHUB_WEBHOOK_SECRET=test \
DEVIN_API_KEY=test \
DEVIN_ORG_ID=test \
REPOSITORY_URL=https://github.com/test/repo \
DASHBOARD_PORT=5050 \
python -m flask --app app:create_app run --host 0.0.0.0 --port 5050
```
Dashboard is at `http://localhost:5050/dashboard`.

### Dashboard Assertions
- Title should be "Remediation Operations Dashboard"
- Stat cards: Total Sessions, Active, Completed, Failed, Success Rate, Avg Overall Time, Total Cost
- Status filter buttons: All, Created, Running, Completed, Failed, Timed Out
- Issue titles with `[Bug]`/`[Feature]`/`[Task]` prefixes display correctly
- The `0` falsy bug was fixed — zero durations render as `0`, not `—`
- `None` values render as `—`
- "Overall (ms)" should always be ≥ "Devin (ms)"
- Cost column shows `0.0` (not `—`) when ACU data is unavailable

## Testing Issue Generator
```bash
cd tests/issue-generator
# Build
docker build -t issue-generator .
# Run (creates random issues from issues.md)
docker run --env-file ../../.env issue-generator --batch 3
```
- Uses `random.sample()` — each run picks different issues
- Duplicate title detection prevents re-processing identical issues

## Common Pitfalls
- `assertLogs` captures messages as `INFO:logger.name:message text`, not the `extra` dict keys. Check message text, not event_type.
- Using `:memory:` for DATABASE_PATH may cause issues with multi-threaded Flask test client — use a temp file path instead.
- The `APP_BASE_URL` env var is optional. When not set, webhook auto-registration is skipped (this is normal for local testing).
- Personal GitHub repos do NOT have native issue types — `issue.type` is always `None`. The title prefix fallback handles this.
- Gunicorn runs multiple workers by default; use `--preload` flag to avoid duplicate webhook registration.
- The Devin API returns `acus_consumed: 0.0` on self-serve plans — this is expected, not a code bug.
