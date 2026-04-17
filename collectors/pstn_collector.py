#!/usr/bin/env python3
"""
pstn_collector.py — Microsoft Graph API PSTN Call Logs Collector
Bank of America — Teams to Splunk Integration

Polls Graph API endpoint:
  /communications/callRecords/getPstnCalls

Provides: caller/callee numbers, call duration, charge, carrier name,
          trunk FQDN, call type (inbound/outbound), user UPN.

Essential for PSTN cost tracking, carrier analysis, and Direct Routing
trunk utilization reporting.

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
logger = logging.getLogger("pstn_collector")

WATERMARK_PSTN = config.STATE_DIR / "pstn_calls.watermark"


# ─────────────────────────────────────────────────────────────────────────────
# Authentication
# ─────────────────────────────────────────────────────────────────────────────
def get_access_token() -> str:
    app = msal.ConfidentialClientApplication(
        client_id=config.CLIENT_ID,             # YOUR INFORMATION GOES HERE (via env)
        client_credential=config.CLIENT_SECRET, # YOUR INFORMATION GOES HERE (via env)
        authority=f"https://login.microsoftonline.com/{config.TENANT_ID}",  # YOUR INFORMATION GOES HERE (via env)
        proxies=config.PROXIES or None,
    )
    result = app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
    if "access_token" not in result:
        logger.error(f"Token error: {result.get('error_description', result.get('error'))}")
        sys.exit(1)
    return result["access_token"]


# ─────────────────────────────────────────────────────────────────────────────
# Watermark helpers
# ─────────────────────────────────────────────────────────────────────────────
def read_watermark(path: Path, default_hours_back: int = 24) -> datetime:
    try:
        if path.exists():
            return datetime.fromisoformat(path.read_text().strip()).replace(tzinfo=timezone.utc)
    except Exception as exc:
        logger.warning(f"Could not read watermark: {exc}. Using default lookback.")
    return datetime.now(timezone.utc) - timedelta(hours=default_hours_back)


def write_watermark(path: Path, ts: datetime) -> None:
    try:
        path.write_text(ts.isoformat())
    except Exception as exc:
        logger.error(f"Failed to write watermark: {exc}")


# ─────────────────────────────────────────────────────────────────────────────
# Paginated fetch
# ─────────────────────────────────────────────────────────────────────────────
def get_all_pages(token: str, url: str) -> Generator[dict, None, None]:
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept":        "application/json",
    }
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
        except requests.exceptions.HTTPError as exc:
            logger.error(f"PSTN API HTTP error: {exc}")
            break
        except requests.exceptions.RequestException as exc:
            logger.error(f"PSTN API request failed: {exc}")
            break

        data = resp.json()
        for item in data.get("value", []):
            yield item

        next_url = data.get("@odata.nextLink")
        if next_url:
            time.sleep(0.5)


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
                "sourcetype": "teams:call:pstn",
                "index":      config.INDEX_CALLS,
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
def collect_pstn_calls(token: str) -> int:
    """
    Fetch PSTN call logs. The Graph API getPstnCalls endpoint returns calls
    within a date range (max 90 days per request). We use daily watermarking.
    """
    watermark = read_watermark(WATERMARK_PSTN)
    from_dt   = watermark - timedelta(hours=1)  # 1-hour overlap to catch late arrivals
    to_dt     = datetime.now(timezone.utc)

    # Ensure we don't exceed 90-day limit per request
    max_range = timedelta(days=89)
    if (to_dt - from_dt) > max_range:
        from_dt = to_dt - max_range
        logger.warning(f"Date range exceeded 90 days; clamped from_dt to {from_dt.isoformat()}")

    from_str = from_dt.strftime("%Y-%m-%dT%H:%M:%S.0000000Z")
    to_str   = to_dt.strftime("%Y-%m-%dT%H:%M:%S.0000000Z")

    url = (
        f"{config.GRAPH_BASE_URL}/communications/callRecords/"
        f"getPstnCalls(fromDateTime='{from_str}',toDateTime='{to_str}')"
    )

    logger.info(f"Fetching PSTN calls from {from_dt.isoformat()} to {to_dt.isoformat()}")

    events: list[dict] = []
    latest_start = watermark

    for record in get_all_pages(token, url):
        # Enrich with derived fields useful in dashboards
        start_str = record.get("startDateTime", "")
        record["_ingest_time"]  = int(time.time())
        record["_collector"]    = "pstn_calls"
        record["_tenant_id"]    = config.TENANT_ID

        # Derive call direction from caller number format
        caller = record.get("callerNumber", "")
        callee = record.get("calleeNumber", "")
        record["call_direction"] = "outbound" if caller and not caller.startswith("+1800") else "inbound"

        # Classify call type for dashboard grouping
        record["call_category"] = _classify_call(caller, callee)

        events.append(record)

        if start_str:
            try:
                start_dt = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
                if start_dt > latest_start:
                    latest_start = start_dt
            except ValueError:
                pass

    sent = send_to_splunk(events)
    logger.info(f"PSTN: fetched {len(events)}, sent {sent} events to Splunk.")

    if sent > 0 or latest_start > watermark:
        write_watermark(WATERMARK_PSTN, latest_start)

    return sent


def _classify_call(caller: str, callee: str) -> str:
    """Classify call type for dashboard grouping. Extend with BofA-specific logic."""
    if not caller and not callee:
        return "unknown"
    # Toll-free patterns — YOUR INFORMATION GOES HERE: add BofA internal prefix ranges
    if callee and callee.startswith("+1800"):
        return "toll_free"
    if callee and callee.startswith("+1"):
        return "domestic"
    if callee and not callee.startswith("+1"):
        return "international"
    return "other"


def main() -> None:
    logger.info("=== PSTN Collector starting ===")
    token = get_access_token()
    sent  = collect_pstn_calls(token)
    logger.info(f"=== PSTN Collector complete — {sent} events sent to Splunk ===")


if __name__ == "__main__":
    main()
