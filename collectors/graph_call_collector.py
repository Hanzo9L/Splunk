#!/usr/bin/env python3
"""
graph_call_collector.py — Microsoft Graph API Call Records & Direct Routing Collector
Bank of America — Teams to Splunk Integration

Polls two Graph API endpoints:
  1. /communications/callRecords          — Full VoIP + PSTN call records with session/segment detail
  2. /communications/callRecords/getDirectRoutingCalls — Direct Routing SBC-level records

Data is sent to Splunk via HEC. Watermarking prevents duplicate events across runs.

Run: python graph_call_collector.py
     (or triggered automatically by Splunk inputs.conf scripted input)

See CONFIG_VARIABLES.md for all required environment variables.
"""

import json
import logging
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Generator, Optional

import msal
import requests

import config

# ─────────────────────────────────────────────────────────────────────────────
# Logging setup
# ─────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("graph_call_collector")

# Watermark file paths
WATERMARK_CALLS = config.STATE_DIR / "graph_call_records.watermark"
WATERMARK_DR    = config.STATE_DIR / "graph_dr_records.watermark"


# ─────────────────────────────────────────────────────────────────────────────
# Authentication — MSAL client credentials (app-only)
# ─────────────────────────────────────────────────────────────────────────────
def get_access_token() -> str:
    """
    Obtain an app-only OAuth2 token from Azure AD using client credentials flow.
    Uses MSAL token cache to avoid redundant token requests.
    """
    app = msal.ConfidentialClientApplication(
        client_id=config.CLIENT_ID,           # YOUR INFORMATION GOES HERE (via env)
        client_credential=config.CLIENT_SECRET,  # YOUR INFORMATION GOES HERE (via env)
        authority=f"https://login.microsoftonline.com/{config.TENANT_ID}",  # YOUR INFORMATION GOES HERE (via env)
        proxies=config.PROXIES or None,
    )
    scopes = ["https://graph.microsoft.com/.default"]
    result = app.acquire_token_silent(scopes, account=None)
    if not result:
        result = app.acquire_token_for_client(scopes=scopes)

    if "access_token" not in result:
        error = result.get("error_description", result.get("error", "Unknown error"))
        logger.error(f"Failed to acquire Graph API token: {error}")
        sys.exit(1)

    logger.debug("Successfully acquired Graph API access token.")
    return result["access_token"]


# ─────────────────────────────────────────────────────────────────────────────
# Watermark helpers
# ─────────────────────────────────────────────────────────────────────────────
def read_watermark(path: Path, default_hours_back: int = 24) -> datetime:
    """Read the last-processed timestamp from a watermark file."""
    try:
        if path.exists():
            ts = path.read_text().strip()
            return datetime.fromisoformat(ts).replace(tzinfo=timezone.utc)
    except Exception as exc:
        logger.warning(f"Could not read watermark from {path}: {exc}. Using default lookback.")
    return datetime.now(timezone.utc) - timedelta(hours=default_hours_back)


def write_watermark(path: Path, ts: datetime) -> None:
    """Persist the latest processed timestamp to a watermark file."""
    try:
        path.write_text(ts.isoformat())
    except Exception as exc:
        logger.error(f"Failed to write watermark to {path}: {exc}")


# ─────────────────────────────────────────────────────────────────────────────
# Graph API pagination helper
# ─────────────────────────────────────────────────────────────────────────────
def graph_get_all_pages(
    token: str,
    url: str,
    params: Optional[dict] = None,
) -> Generator[dict, None, None]:
    """
    Yield individual items from a paginated Graph API response.
    Automatically follows @odata.nextLink until all pages are consumed.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type":  "application/json",
        "Accept":        "application/json",
    }
    next_url: Optional[str] = url
    page_count = 0

    while next_url:
        try:
            response = requests.get(
                next_url,
                headers=headers,
                params=params if page_count == 0 else None,
                proxies=config.PROXIES or None,
                timeout=60,
            )
            response.raise_for_status()
        except requests.exceptions.HTTPError as exc:
            logger.error(f"Graph API HTTP error on page {page_count+1}: {exc} — URL: {next_url}")
            break
        except requests.exceptions.RequestException as exc:
            logger.error(f"Graph API request failed on page {page_count+1}: {exc}")
            break

        data = response.json()
        for item in data.get("value", []):
            yield item

        next_url = data.get("@odata.nextLink")
        page_count += 1
        logger.debug(f"Fetched page {page_count}, {len(data.get('value', []))} items.")

        # Respect Graph API throttling — brief pause between pages
        if next_url:
            time.sleep(0.5)


# ─────────────────────────────────────────────────────────────────────────────
# Splunk HEC sender
# ─────────────────────────────────────────────────────────────────────────────
def send_to_splunk(
    events: list[dict],
    sourcetype: str,
    index: str,
    batch_size: int = 500,
) -> int:
    """
    Send a list of events to Splunk via HEC in batches.
    Returns the count of events successfully sent.
    """
    if not events:
        return 0

    hec_url   = f"{config.SPLUNK_HEC_URL}/services/collector/event"  # YOUR INFORMATION GOES HERE (via env)
    hec_token = config.SPLUNK_HEC_TOKEN                               # YOUR INFORMATION GOES HERE (via env)
    headers   = {"Authorization": f"Splunk {hec_token}"}
    sent      = 0

    for i in range(0, len(events), batch_size):
        batch = events[i : i + batch_size]
        payload = "\n".join(
            json.dumps({
                "time":       event.get("_ingest_time", int(time.time())),
                "sourcetype": sourcetype,
                "index":      index,
                "event":      event,
            })
            for event in batch
        )
        try:
            response = requests.post(
                hec_url,
                data=payload,
                headers=headers,
                verify=config.SPLUNK_VERIFY_SSL,
                proxies={},  # HEC is internal — bypass proxy
                timeout=30,
            )
            response.raise_for_status()
            sent += len(batch)
            logger.debug(f"Sent batch of {len(batch)} events to Splunk HEC (index={index}).")
        except requests.exceptions.RequestException as exc:
            logger.error(f"Failed to send batch to Splunk HEC: {exc}")

    return sent


# ─────────────────────────────────────────────────────────────────────────────
# Collector 1: Call Records (VoIP + escalated PSTN)
# ─────────────────────────────────────────────────────────────────────────────
def collect_call_records(token: str) -> int:
    """
    Fetch Graph API callRecords with full session/segment expansion.
    Watermarks on lastModifiedDateTime to catch records updated after the call ends.
    """
    watermark = read_watermark(WATERMARK_CALLS)
    # Apply lookback window: records can be modified up to 2h after call end
    from_dt = watermark - timedelta(hours=config.CALL_RECORDS_LOOKBACK_HOURS)
    to_dt   = datetime.now(timezone.utc)

    url = f"{config.GRAPH_BASE_URL}/communications/callRecords"
    params = {
        "$filter":  f"lastModifiedDateTime ge {from_dt.strftime('%Y-%m-%dT%H:%M:%SZ')} "
                    f"and lastModifiedDateTime le {to_dt.strftime('%Y-%m-%dT%H:%M:%SZ')}",
        "$expand":  "sessions($expand=segments)",
        "$select":  "id,type,startDateTime,endDateTime,lastModifiedDateTime,"
                    "joinWebUrl,modalityCount,participants,durationSeconds",
        "$top":     "100",
    }

    events: list[dict] = []
    latest_modified = watermark

    logger.info(f"Fetching call records from {from_dt.isoformat()} to {to_dt.isoformat()}")

    for record in graph_get_all_pages(token, url, params):
        record["_ingest_time"]       = int(time.time())
        record["_collector"]         = "graph_call_records"
        record["_tenant_id"]         = config.TENANT_ID
        events.append(record)

        # Track the latest modification time for watermark update
        modified_str = record.get("lastModifiedDateTime", "")
        if modified_str:
            try:
                modified_dt = datetime.fromisoformat(
                    modified_str.replace("Z", "+00:00")
                )
                if modified_dt > latest_modified:
                    latest_modified = modified_dt
            except ValueError:
                pass

    sent = send_to_splunk(events, "teams:call:record", config.INDEX_CALLS)
    logger.info(f"Call records: fetched {len(events)}, sent {sent} events to Splunk.")

    if sent > 0 or latest_modified > watermark:
        write_watermark(WATERMARK_CALLS, latest_modified)

    return sent


# ─────────────────────────────────────────────────────────────────────────────
# Collector 2: Direct Routing Calls
# ─────────────────────────────────────────────────────────────────────────────
def collect_direct_routing_calls(token: str) -> int:
    """
    Fetch Direct Routing call logs from Graph API.
    These include SBC FQDN, SIP response codes, media bypass, and routing path.
    Critical for monitoring Ribbon SBC performance during migration.
    """
    watermark = read_watermark(WATERMARK_DR)
    from_dt = watermark - timedelta(hours=config.CALL_RECORDS_LOOKBACK_HOURS)
    to_dt   = datetime.now(timezone.utc)

    # Direct Routing API uses function-style URL with datetime parameters
    from_str = from_dt.strftime("%Y-%m-%dT%H:%M:%S.0000000Z")
    to_str   = to_dt.strftime("%Y-%m-%dT%H:%M:%S.0000000Z")

    url = (
        f"{config.GRAPH_BASE_URL}/communications/callRecords/"
        f"getDirectRoutingCalls(fromDateTime='{from_str}',toDateTime='{to_str}')"
    )

    events: list[dict] = []
    latest_start = watermark

    logger.info(f"Fetching Direct Routing calls from {from_dt.isoformat()} to {to_dt.isoformat()}")

    for record in graph_get_all_pages(token, url):
        record["_ingest_time"] = int(time.time())
        record["_collector"]   = "graph_direct_routing"
        record["_tenant_id"]   = config.TENANT_ID
        events.append(record)

        start_str = record.get("startDateTime", "")
        if start_str:
            try:
                start_dt = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
                if start_dt > latest_start:
                    latest_start = start_dt
            except ValueError:
                pass

    sent = send_to_splunk(events, "teams:call:dr_record", config.INDEX_CALLS)
    logger.info(f"Direct Routing: fetched {len(events)}, sent {sent} events to Splunk.")

    if sent > 0 or latest_start > watermark:
        write_watermark(WATERMARK_DR, latest_start)

    return sent


# ─────────────────────────────────────────────────────────────────────────────
# Main entry point
# ─────────────────────────────────────────────────────────────────────────────
def main() -> None:
    logger.info("=== Graph Call Collector starting ===")
    token = get_access_token()

    total = 0
    total += collect_call_records(token)
    total += collect_direct_routing_calls(token)

    logger.info(f"=== Graph Call Collector complete — {total} total events sent to Splunk ===")


if __name__ == "__main__":
    main()
