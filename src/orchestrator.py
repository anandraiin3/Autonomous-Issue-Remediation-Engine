"""Remediation orchestrator — manages Devin session lifecycle.

RO-01 – RO-11: Creates sessions via Devin API **v3**, polls until terminal
state, extracts PR URL, and handles timeouts / failures.  Supports
all GitHub issue types (bug, feature, task).

API reference: https://docs.devin.ai/api-reference/overview
"""

import re
import time
from typing import Optional

import requests

from src.config import Config
from src.database import (
    create_session,
    get_session,
    reserve_issue,
    update_last_posted_message,
    update_session_status,
)
from src.logger import get_logger
from src.prompt_builder import build_prompt

logger = get_logger(__name__)

DEVIN_API_BASE = "https://api.devin.ai"

# v3 terminal statuses — the session will not transition further.
# "exit" = completed (check status_detail for "finished"),
# "error" = unrecoverable error,
# "suspended" = paused (inactivity, user request, usage limit, etc.)
TERMINAL_STATUSES = {"exit", "error", "suspended"}

# v3 running sub-states (status_detail when status == "running")
ACTIVE_DETAILS = {"working", "waiting_for_user", "waiting_for_approval"}


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {Config.DEVIN_API_KEY}",
        "Content-Type": "application/json",
    }


def _sessions_url() -> str:
    """Build the v3 sessions endpoint URL for the configured organisation."""
    return f"{DEVIN_API_BASE}/v3/organizations/{Config.DEVIN_ORG_ID}/sessions"


def _create_devin_session(
    prompt: str,
    issue_number: int,
    issue_title: str,
    issue_type: str = "task",
) -> dict:
    """Call Devin API v3 to create a new session (RO-03 / RO-04).

    v3 endpoint: POST /v3/organizations/{org_id}/sessions
    Idempotency is handled at the application level (DB check in
    remediate_issue) since v3 does not expose an idempotent flag.
    """
    payload: dict = {
        "prompt": prompt,
        "title": f"Resolve #{issue_number} [{issue_type}]: {issue_title}",
        "tags": ["auto-remediation", f"issue-{issue_number}", issue_type],
    }

    resp = requests.post(
        _sessions_url(),
        json=payload,
        headers=_headers(),
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def _poll_session(session_id: str) -> dict:
    """Retrieve current session details from Devin API v3 (RO-05).

    v3 endpoint: GET /v3/organizations/{org_id}/sessions/{session_id}
    """
    resp = requests.get(
        f"{_sessions_url()}/{session_id}",
        headers=_headers(),
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Infrastructure keywords — if a Devin "waiting_for_user" message contains
# any of these, it is classified as an infrastructure/access problem rather
# than a clarification about the issue itself.
# ---------------------------------------------------------------------------
_INFRA_KEYWORDS = [
    "api key",
    "api token",
    "token expired",
    "credentials",
    "authentication",
    "permission",
    "permissions",
    "access denied",
    "forbidden",
    "401",
    "403",
    "ssh key",
    "deploy key",
    "secret",
    "contents:write",
    "push access",
    "write access",
    "install the",
    "rate limit",
    "quota",
    "billing",
    "npm login",
    "docker login",
    "registry",
]


def _is_infrastructure_question(message: str) -> bool:
    """Return True if the message is about infrastructure, not the issue."""
    lower = message.lower()
    return any(kw in lower for kw in _INFRA_KEYWORDS)


def _fetch_latest_devin_message(
    session_id: str,
    after_event_id: Optional[str] = None,
) -> Optional[dict]:
    """Fetch the most recent *devin*-authored message from the session.

    Returns ``{"event_id": ..., "message": ...}`` or *None*.
    Uses the v3 messages endpoint with reverse pagination.
    """
    url = (
        f"{DEVIN_API_BASE}/v3/organizations/{Config.DEVIN_ORG_ID}"
        f"/sessions/devin-{session_id}/messages"
    )
    try:
        resp = requests.get(
            url,
            headers=_headers(),
            params={"first": 20},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException:
        return None

    # Walk items in reverse (newest first) and find first devin message
    items = data.get("items", [])
    for msg in reversed(items):
        if msg.get("source") != "devin":
            continue
        eid = msg.get("event_id", "")
        if after_event_id and eid == after_event_id:
            return None  # latest devin message was already posted
        return {"event_id": eid, "message": msg.get("message", "")}
    return None


def _post_github_issue_comment(
    repo_url: str,
    issue_number: int,
    body: str,
) -> bool:
    """Post a comment on the GitHub issue using GITHUB_TOKEN.

    ``repo_url`` is e.g. ``https://github.com/owner/repo``.
    Returns True on success.
    """
    if not Config.GITHUB_TOKEN:
        return False

    # Extract owner/repo from URL
    match = re.search(r"github\.com/([^/]+/[^/]+)", repo_url)
    if not match:
        return False
    owner_repo = match.group(1).rstrip("/")

    api_url = (
        f"https://api.github.com/repos/{owner_repo}/issues/{issue_number}/comments"
    )
    try:
        resp = requests.post(
            api_url,
            json={"body": body},
            headers={
                "Authorization": f"Bearer {Config.GITHUB_TOKEN}",
                "Accept": "application/vnd.github+json",
            },
            timeout=30,
        )
        return resp.status_code in (200, 201)
    except requests.RequestException:
        return False


def _finish_devin_session(session_id: str) -> bool:
    """Send a completion message and archive the Devin session.

    Returns True if the session was successfully terminated.
    """
    url = (
        f"{DEVIN_API_BASE}/v3/organizations/{Config.DEVIN_ORG_ID}"
        f"/sessions/devin-{session_id}"
    )
    try:
        resp = requests.delete(
            url,
            headers=_headers(),
            params={"archive": "true"},
            timeout=30,
        )
        return resp.status_code in (200, 204)
    except requests.RequestException:
        return False


def _extract_pr_url(session_data: dict) -> Optional[str]:
    """Extract the first PR URL from session response (RO-06).

    v3 returns ``pull_requests``: a list of ``{pr_url, pr_state}`` objects
    (changed from v1's single ``pull_request.url``).
    """
    prs = session_data.get("pull_requests")
    if prs and isinstance(prs, list):
        for pr in prs:
            url = pr.get("pr_url")
            if url:
                return url
    return None


def remediate_issue(
    repo_url: str,
    issue_number: int,
    issue_title: str,
    issue_body: str,
    issue_type: str = "task",
) -> None:
    """End-to-end remediation: create session, poll, extract result.

    This function runs in a background thread — it must not raise
    unhandled exceptions.
    """
    log_extra = {"issue_number": issue_number}

    try:
        # RO-01 / NF-05: Atomic idempotency — reserve the issue number
        # in the DB *before* calling the Devin API.  The UNIQUE index on
        # issue_number prevents two concurrent threads from both passing
        # the check and creating duplicate sessions.
        if not reserve_issue(issue_number, issue_title, repo_url):
            logger.info(
                f"Session already exists for issue #{issue_number} — skipping",
                extra={**log_extra, "event_type": "idempotency_skip"},
            )
            return

        # Build prompt (PR-01 – PR-04)
        prompt = build_prompt(
            repo_url, issue_number, issue_title, issue_body, issue_type
        )

        # RO-03 / RO-04: Create Devin session (v3)
        logger.info(
            f"Creating Devin session for issue #{issue_number} (type={issue_type})",
            extra={**log_extra, "event_type": "session_creating"},
        )
        result = _create_devin_session(prompt, issue_number, issue_title, issue_type)
        session_id = result["session_id"]
        log_extra["session_id"] = session_id

        # Update the placeholder row with real Devin session details
        devin_url = result.get("url", "")
        create_session(session_id, issue_number, issue_title, repo_url, devin_url)
        update_session_status(session_id, "running", status_detail="working")

        logger.info(
            f"Devin session created: {session_id} — url: {result.get('url', 'N/A')}",
            extra={**log_extra, "event_type": "session_started"},
        )

        # RO-05: Poll until terminal state
        timeout_seconds = Config.SESSION_TIMEOUT_MINUTES * 60
        start = time.monotonic()
        last_known_pr: Optional[str] = None

        while True:
            elapsed = time.monotonic() - start

            # RO-08: Timeout guard — if a PR was already created,
            # the remediation goal is met even if the session hasn't
            # reached a terminal state yet (e.g. waiting for CI).
            if elapsed > timeout_seconds:
                if last_known_pr:
                    update_session_status(
                        session_id,
                        "completed",
                        status_detail="timed_out_with_pr",
                        pr_url=last_known_pr,
                    )
                    logger.info(
                        f"Session {session_id} timed out but PR exists — marking completed (PR: {last_known_pr})",
                        extra={**log_extra, "event_type": "session_completed"},
                    )
                else:
                    logger.warning(
                        f"Session {session_id} timed out after {Config.SESSION_TIMEOUT_MINUTES}m",
                        extra={**log_extra, "event_type": "session_timed_out"},
                    )
                    update_session_status(
                        session_id,
                        "timed_out",
                        error_message=f"Session exceeded {Config.SESSION_TIMEOUT_MINUTES} minute timeout",
                        error_type="timeout",
                    )
                return

            time.sleep(Config.POLLING_INTERVAL_SECONDS)

            try:
                session_data = _poll_session(session_id)
            except requests.RequestException as exc:
                logger.warning(
                    f"Poll request failed for {session_id}: {exc}",
                    extra={**log_extra, "event_type": "poll_error"},
                )
                continue

            status = session_data.get("status", "")
            status_detail = session_data.get("status_detail", "")
            logger.info(
                f"Session {session_id} status: {status} (detail: {status_detail})",
                extra={**log_extra, "event_type": "poll_status"},
            )

            # Always update the granular status_detail + PR URL + cost in DB
            pr_url = _extract_pr_url(session_data)
            if pr_url:
                last_known_pr = pr_url
            acus = session_data.get("acus_consumed")

            # Derive the display detail: if a PR exists and Devin is
            # just "working", surface a more informative "pr_ready" so
            # the dashboard conveys that a PR is available for review.
            display_detail = status_detail or None
            if pr_url and status_detail == "working":
                display_detail = "pr_ready"

            update_session_status(
                session_id,
                "running",
                status_detail=display_detail,
                pr_url=pr_url,
                acus_consumed=acus,
            )

            # Auto-close: once a PR is created, if the session is idle
            # (waiting_for_user, waiting_for_approval) terminate it and
            # mark as completed.  This prevents sessions from lingering
            # after their remediation goal (a PR) has been achieved.
            effective_pr = pr_url or last_known_pr
            if effective_pr and status_detail in (
                "waiting_for_user",
                "waiting_for_approval",
            ):
                terminated = _finish_devin_session(session_id)
                update_session_status(
                    session_id,
                    "completed",
                    status_detail="auto_closed_with_pr",
                    pr_url=effective_pr,
                    acus_consumed=acus,
                )
                logger.info(
                    f"Session {session_id} auto-closed after PR created "
                    f"(terminated={terminated}, PR: {effective_pr})",
                    extra={**log_extra, "event_type": "session_auto_closed"},
                )
                return

            # v3: "exit" with status_detail "finished" means task completed
            if status == "exit" and status_detail == "finished":
                update_session_status(
                    session_id,
                    "completed",
                    status_detail="finished",
                    pr_url=pr_url,
                    acus_consumed=acus,
                )
                logger.info(
                    f"Session {session_id} completed — PR: {pr_url or 'none'}",
                    extra={**log_extra, "event_type": "session_completed"},
                )
                return

            # v3: "error" is always a terminal failure
            if status == "error":
                reason = status_detail or status
                update_session_status(
                    session_id,
                    "failed",
                    status_detail=reason,
                    error_message=f"Devin session reached terminal status: {status} ({reason})",
                    error_type="devin_terminal",
                    acus_consumed=acus,
                )
                logger.warning(
                    f"Session {session_id} ended with status: {status} ({reason})",
                    extra={**log_extra, "event_type": "session_failed"},
                )
                return

            # v3: "suspended" — if a PR was already created, the
            # remediation goal is met; mark as completed.  Common cause:
            # Devin goes idle while waiting for CI checks after pushing
            # a PR, triggering an inactivity suspension.
            if status == "suspended":
                reason = status_detail or status
                suspended_pr = pr_url or last_known_pr
                if suspended_pr:
                    update_session_status(
                        session_id,
                        "completed",
                        status_detail="suspended_with_pr",
                        pr_url=suspended_pr,
                        acus_consumed=acus,
                    )
                    logger.info(
                        f"Session {session_id} suspended but PR exists — marking completed (PR: {suspended_pr})",
                        extra={**log_extra, "event_type": "session_completed"},
                    )
                else:
                    update_session_status(
                        session_id,
                        "failed",
                        status_detail=reason,
                        error_message=f"Devin session suspended without producing a PR: {reason}",
                        error_type="devin_terminal",
                        acus_consumed=acus,
                    )
                    logger.warning(
                        f"Session {session_id} suspended without PR — marking failed ({reason})",
                        extra={**log_extra, "event_type": "session_failed"},
                    )
                return

            # v3: "exit" without "finished" is also terminal
            if status == "exit":
                update_session_status(
                    session_id,
                    "completed",
                    status_detail=status_detail,
                    pr_url=pr_url,
                    acus_consumed=acus,
                )
                logger.info(
                    f"Session {session_id} exited (detail: {status_detail}) — PR: {pr_url or 'none'}",
                    extra={**log_extra, "event_type": "session_completed"},
                )
                return

            # ----------------------------------------------------------
            # Auto-post: when Devin is waiting_for_user and the question
            # is about the *issue* (not infrastructure), post it back to
            # the GitHub issue so the reporter can respond.
            # ----------------------------------------------------------
            if status_detail == "waiting_for_user":
                db_row = get_session(session_id)
                last_posted = db_row.get("last_posted_message_id") if db_row else None
                latest_msg = _fetch_latest_devin_message(
                    session_id, after_event_id=last_posted
                )
                if latest_msg and not _is_infrastructure_question(
                    latest_msg["message"]
                ):
                    comment_body = (
                        f"🤖 **Devin AI** is working on this issue and has a question:\n\n"
                        f"> {latest_msg['message']}\n\n"
                        f"[View Devin session](https://app.devin.ai/sessions/{session_id})"
                    )
                    posted = _post_github_issue_comment(
                        repo_url,
                        issue_number,
                        comment_body,
                    )
                    if posted:
                        update_last_posted_message(session_id, latest_msg["event_id"])
                        logger.info(
                            f"Posted Devin question to issue #{issue_number}",
                            extra={**log_extra, "event_type": "github_comment_posted"},
                        )
                    else:
                        logger.warning(
                            f"Failed to post comment to issue #{issue_number}",
                            extra={**log_extra, "event_type": "github_comment_failed"},
                        )

            # Non-terminal: new, creating, claimed, running, resuming — keep polling

    except requests.HTTPError as exc:
        logger.error(
            f"Devin API error for issue #{issue_number}: {exc}",
            extra={**log_extra, "event_type": "api_error"},
            exc_info=True,
        )
        if "session_id" in log_extra:
            update_session_status(
                log_extra["session_id"],
                "failed",
                error_message=str(exc),
                error_type="api_error",
            )
    except Exception as exc:
        logger.error(
            f"Unexpected error remediating issue #{issue_number}: {exc}",
            extra={**log_extra, "event_type": "unexpected_error"},
            exc_info=True,
        )
        if "session_id" in log_extra:
            update_session_status(
                log_extra["session_id"],
                "failed",
                error_message=str(exc),
                error_type="unexpected",
            )
