---
name: development-workflow
description: Local development setup, architecture overview, env var reference, and common operations for the Autonomous Issue Remediation Engine.
---

# Development Workflow — Autonomous Issue Remediation Engine

## Architecture
```
GitHub Issue → Webhook POST → src/webhook.py → src/orchestrator.py → Devin API v3
                                    ↓                    ↓
                              src/database.py      Auto-comment on
                                    ↓              GitHub issue
                              templates/dashboard.html
```

## Key Source Files
| File | Purpose |
|---|---|
| `app.py` | Flask app factory, wires blueprints, runs `register_webhook()` on startup |
| `src/config.py` | All config from env vars via `Config` class, includes `validate()` |
| `src/webhook.py` | `/webhook` endpoint, HMAC validation, issue type detection, dispatches remediation |
| `src/orchestrator.py` | Creates Devin sessions, polls status, extracts PRs, auto-closes |
| `src/database.py` | SQLite with WAL, `init_db()`, `reserve_issue()` for atomic dedup |
| `src/prompt_builder.py` | Type-specific Devin prompt templates (Bug/Feature/Task) |
| `src/dashboard.py` | `/dashboard` blueprint, stat cards, status filters |
| `src/webhook_registration.py` | Idempotent GitHub webhook auto-registration |
| `templates/dashboard.html` | Jinja2 dashboard with design token system |

## Environment Variables

### Required
| Var | Description |
|---|---|
| `GITHUB_WEBHOOK_SECRET` | HMAC-SHA256 secret for webhook validation |
| `DEVIN_API_KEY` | Service user key (prefix `cog_`), needs `ManageOrgSessions` |
| `DEVIN_ORG_ID` | Found at app.devin.ai → Settings → Service Users |
| `REPOSITORY_URL` | Target repo, e.g. `https://github.com/anandraiin3/superset` |

### Optional
| Var | Default | Description |
|---|---|---|
| `GITHUB_TOKEN` | — | Needed for webhook auto-registration + auto-commenting |
| `APP_BASE_URL` | — | Public URL for webhook registration (e.g. ngrok URL) |
| `ISSUE_TYPES` | `bug,feature,task` | Comma-separated supported types |
| `POLLING_INTERVAL_SECONDS` | `30` | How often to poll Devin API |
| `SESSION_TIMEOUT_MINUTES` | `45` | Max session duration before timeout |
| `DATABASE_PATH` | `/data/sessions.db` | SQLite file location |
| `DASHBOARD_PORT` | `5000` | Port for Flask/gunicorn |

## Local Dev Setup
```bash
# Install deps
pip install -r requirements.txt pytest

# Run tests
python -m pytest tests/ -v

# Run dev server (with dummy secrets for local testing)
DATABASE_PATH=/tmp/test.db \
GITHUB_WEBHOOK_SECRET=test \
DEVIN_API_KEY=test \
DEVIN_ORG_ID=test \
REPOSITORY_URL=https://github.com/test/repo \
python -m flask --app app:create_app run --host 0.0.0.0 --port 5050
```

## Linting
```bash
ruff check src/ tests/ app.py
ruff format --check src/ tests/ app.py
```

## CI Pipeline
GitHub Actions at `.github/workflows/ci.yml` runs 3 jobs:
1. **lint** — `ruff check` + `ruff format --check`
2. **test** — `pytest` with dummy env vars
3. **docker-build** — verifies Dockerfile builds

## Docker / Podman Build
```bash
# Build
podman build -t superset-remediation .

# Run with env file
podman run --rm -p 5000:5000 -v superset-session-data:/data --env-file .env superset-remediation
```

## Key Design Decisions
- **Issue type detection**: Native `issue.type.name` (org repos) → title prefix `[Bug]`/`[Feature]`/`[Task]` fallback (personal repos)
- **Atomic dedup**: `reserve_issue()` uses SQLite UNIQUE constraint to prevent duplicate sessions
- **Auto-close**: Sessions auto-archived after PR is detected + session is suspended/waiting
- **Infrastructure filtering**: Devin questions about tokens/access are NOT posted back to GitHub issues
- **Gunicorn**: Uses `--preload` to avoid duplicate webhook registration from multiple workers
