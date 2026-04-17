#!/usr/bin/env python3
"""
audit_collector.py — Microsoft 365 Management Activity API Teams Audit Log Collector
Bank of America — Teams to Splunk Integration

Collects Teams audit events from the Office 365 Management Activity API.
This is the compliance-critical pipeline for a regulated financial institution.

Events captured include:
  - Teams configuration changes (policy assignments, settings)
  - User role changes (owner/member/guest)
  - Channel and team creation/deletion
  - Meeting recordings and attendance
  - eDiscovery and information protection actions
  - Admin actions performed in Teams Admin Center

API Flow:
  1. Subscribe to content feed (one-time, idempotent)
  2. List available content blobs
  3. Retrieve each content blob
  4. Send events to Splunk

Auth scope: https://manage.office.com/.default
Required permission: ActivityFeed.Read (Office 365 Management API)

See CONFIG_VARIABLES.md for all required environment variables.
"""

import json
import logging
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Generator, Optional

import msal
import requests

import config

# ─────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("audit_collector")

WATERMARK_AUDIT    = config.STATE_DIR / "audit_teams.watermark"
CONTENT_TYPE_TEAMS = "Audit.Teams"
CONTENT_TYPE_GENERAL = "Audit.General"  # Also captures some Teams events


# ─────────────────────────────────────────────────────────────────────────────
# Authentication — Management Activity API uses different scope than Graph
# ─────────────────────────────────────────────────────────────────────────────
def get_mgmt_token() -> str:
    """
    Acquire token for the Office 365 Management Activity API.
    This uses a DIFFERENT scope than Graph API — manage.office.com.
    """
    app = msal.ConfidentialClientApplication(
        client_id=config.CLIENT_ID,             # YOUR INFORMATION GOES HERE (via env)
        client_credential=config.CLIENT_SECRET, # YOUR INFORMATION GOES HERE (via env)
        authority=f"https://login.microsoftonline.com/{config.TENANT_ID}",  # YOUR INFORMATION GOES HERE (via env)
        proxies=config.PROXIES or None,
    )
    result = app.acquire_token_for_client(
        scopes=["https://manage.office.com/.default"]
    )
    if "access_token" not in result:
        logger.error(
            f"Management API token error: {result.get('error_description', result.get('error'))}. "
            f"Ensure 'ActivityFeed.Read' permission is granted on Office 365 Management APIs "
            f"in your Entra ID App Registration (see CONFIG_VARIABLES.md Section 1)."
        )
        sys.exit(1)
    return result["access_token"]


# ─────────────────────────────────────────────────────────────────────────────
# Watermark helpers
# ─────────────────────────────────────────────────────────────────────────────
def read_watermark(path: Path, default_hours_back: int = 24) -> datetime:
    try:
        if path.exists():
            return datetime.fromisoformat(path.read_text().strip()).replace(tzinfo=timezone.utc)
    except Exception:
        pass
    return datetime.now(timezone.utc) - timedelta(hours=default_hours_back)


def write_watermark(path: Path, ts: datetime) -> None:
    try:
        path.write_text(ts.isoformat())
    except Exception as exc:
        logger.error(f"Failed to write watermark: {exc}")


# ─────────────────────────────────────────────────────────────────────────────
# Subscription management — ensures the feed subscription is active
# ─────────────────────────────────────────────────────────────────────────────
def ensure_subscription(token: str, content_type: str) -> bool:
    """
    Start or verify the O365 Management Activity feed subscription.
    This is idempotent — safe to call on every run.
    Returns True if subscription is active.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type":  "application/json",
    }
    url = f"{config.MGMT_API_BASE}/activity/feed/subscriptions/start?contentType={content_type}"

    try:
        resp = requests.post(
            url,
            headers=headers,
            proxies=config.PROXIES or None,
            timeout=30,
        )
        if resp.status_code in (200, 201):
            logger.info(f"Subscription active for content type: {content_type}")
            return True
        elif resp.status_code == 400:
            # 400 can mean already subscribed — treat as success
            body = resp.json()
            if "already subscribed" in str(body).lower() or resp.status_code == 400:
                logger.info(f"Subscription already active for: {content_type}")
                return True
        logger.error(f"Subscription start failed: {resp.status_code} — {resp.text}")
        return False
    except requests.exceptions.RequestException as exc:
        logger.error(f"Subscription request failed: {exc}")
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Content listing — get available content blobs for the time window
# ─────────────────────────────────────────────────────────────────────────────
def list_available_content(
    token: str,
    content_type: str,
    from_dt: datetime,
    to_dt: datetime,
) -> Generator[dict, None, None]:
    """
    Yield content descriptor objects (each containing a contentUri to retrieve).
    The API returns at most 24 hours of content per request.
    We iterate in 24h windows if the requested range is larger.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept":        "application/json",
    }

    # Management Activity API max window is 24 hours per request
    window_start = from_dt
    while window_start < to_dt:
        window_end = min(window_start + timedelta(hours=24), to_dt)

        url = (
            f"{config.MGMT_API_BASE}/activity/feed/subscriptions/content"
            f"?contentType={content_type}"
            f"&startTime={window_start.strftime('%Y-%m-%dT%H:%M:%S')}"
            f"&endTime={window_end.strftime('%Y-%m-%dT%H:%M:%S')}"
        )
        next_url: Optional[str] = url

        while next_url:
            try:
                resp = requests.get(
                    next_url,
                    headers=headers,
                    proxies=config.PROXIES or None,
                    timeout=60,
                )
                resp.raise_for_status()
            except requests.exceptions.RequestException as exc:
                logger.error(f"Failed to list content for {content_type}: {exc}")
                break

            items = resp.json()
            if isinstance(items, list):
                for item in items:
                    yield item
            # NextPage URI is in response headers for Management API
            next_url = resp.headers.get("NextPageUri")
            if next_url:
                time.sleep(0.5)

        window_start = window_end


# ─────────────────────────────────────────────────────────────────────────────
# Content retrieval — fetch actual audit events from content URI
# ─────────────────────────────────────────────────────────────────────────────
def fetch_content_blob(token: str, content_uri: str) -> list[dict]:
    """
    Retrieve audit events from a content URI.
    Each blob can contain up to 1000 events.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept":        "application/json",
    }
    try:
        resp = requests.get(
            content_uri,
            headers=headers,
            proxies=config.PROXIES or None,
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else []
    except requests.exceptions.RequestException as exc:
        logger.warning(f"Failed to fetch content blob {content_uri}: {exc}")
        return []


# ─────────────────────────────────────────────────────────────────────────────
# Splunk HEC sender
# ─────────────────────────────────────────────────────────────────────────────
def send_to_splunk(events: list[dict], batch_size: int = 500) -> int:
    if not events:
        return 0

    hec_url = f"{config.SPLUNK_HEC_URL}/services/collector/event"  # YOUR INFORMATION GOES HERE (via env)
    headers = {"Authorization": f"Splunk {config.SPLUNK_HEC_TOKEN}"}  # YOUR INFORMATION GOES HERE (via env)
    sent    = 0

    for i in range(0, len(events), batch_size):
        batch   = events[i : i + batch_size]
        payload = "\n".join(
            json.dumps({
                "time":       ev.get("_ingest_time", int(time.time())),
                "sourcetype": "teams:audit:log",
                "index":      config.INDEX_AUDIT,
                "event":      ev,
            })
            for ev in batch
        )
        try:
            resp = requests.post(
                hec_url,
                data=payload,
                headers=headers,
                verify=config.SPLUNK_VERIFY_SSL,
                proxies={},
                timeout=30,
            )
            resp.raise_for_status()
            sent += len(batch)
        except requests.exceptions.RequestException as exc:
            logger.error(f"HEC send failed: {exc}")

    return sent


# ─────────────────────────────────────────────────────────────────────────────
# Main collection logic
# ─────────────────────────────────────────────────────────────────────────────
def collect_audit_events(token: str, content_type: str) -> int:
    """
    Full cycle: ensure subscription → list content → fetch blobs → send to Splunk.
    """
    if not ensure_subscription(token, content_type):
        logger.error(f"Cannot proceed without active subscription for {content_type}.")
        return 0

    watermark = read_watermark(WATERMARK_AUDIT)
    from_dt   = watermark - timedelta(minutes=30)  # 30-minute overlap for late arrivals
    to_dt     = datetime.now(timezone.utc)

    logger.info(f"Collecting audit events ({content_type}) from {from_dt.isoformat()} to {to_dt.isoformat()}")

    all_events: list[dict] = []
    latest_event_time = watermark
    content_count = 0

    for content_item in list_available_content(token, content_type, from_dt, to_dt):
        content_uri = content_item.get("contentUri", "")
        if not content_uri:
            continue

        content_count += 1
        events = fetch_content_blob(token, content_uri)

        for event in events:
            # Filter to Teams workload events only
            workload = event.get("Workload", "")
            if workload not in ("MicrosoftTeams", "Teams"):
                continue

            event["_ingest_time"]   = int(time.time())
            event["_collector"]     = "audit_mgmt_api"
            event["_tenant_id"]     = config.TENANT_ID
            event["_content_type"]  = content_type

            # Track latest event creation time for watermark
            creation_str = event.get("CreationTime", "")
            if creation_str:
                try:
                    dt = datetime.fromisoformat(creation_str.replace("Z", "+00:00"))
                    if dt > latest_event_time:
                        latest_event_time = dt
                except ValueError:
                    pass

            all_events.append(event)

        time.sleep(0.2)  # Avoid hammering the content API

    logger.info(
        f"Audit ({content_type}): processed {content_count} content blobs, "
        f"found {len(all_events)} Teams events."
    )

    sent = send_to_splunk(all_events)
    logger.info(f"Sent {sent} audit events to Splunk.")

    if sent > 0 or latest_event_time > watermark:
        write_watermark(WATERMARK_AUDIT, latest_event_time)

    return sent


def main() -> None:
    logger.info("=== Audit Collector starting ===")
    token = get_mgmt_token()
    total = 0

    # Collect from both Teams-specific and General audit feeds
    total += collect_audit_events(token, CONTENT_TYPE_TEAMS)
    total += collect_audit_events(token, CONTENT_TYPE_GENERAL)

    logger.info(f"=== Audit Collector complete — {total} events sent to Splunk ===")


if __name__ == "__main__":
    main()
