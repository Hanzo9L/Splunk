#!/usr/bin/env python3
"""
cqd_collector.py — Call Quality Dashboard (CQD) + Graph API Media Quality Collector
Bank of America — Teams to Splunk Integration

Extracts stream-level call quality metrics from TWO sources:

  Source A (Primary): Microsoft Graph API callRecords with session/segment expansion
    - Endpoint: /communications/callRecords/{id}?$expand=sessions($expand=segments)
    - Provides: audio MOS, jitter, packet loss, RTT, codec, caller/callee subnet
    - Best for: per-call quality correlation

  Source B (Supplemental): CQD OData API
    - Endpoint: https://data.cqd.teams.microsoft.com/RunQuery
    - Provides: aggregate stream-level metrics across all calls
    - Best for: trend analysis, building/subnet heatmaps

Quality thresholds (ITU-T G.107 / Microsoft CQD defaults):
  Excellent:  MOS >= 4.0
  Good:       MOS >= 3.5
  Fair:       MOS >= 3.0
  Poor:       MOS >= 2.5
  Bad:        MOS <  2.5

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
logger = logging.getLogger("cqd_collector")

WATERMARK_CQD = config.STATE_DIR / "cqd_streams.watermark"

# CQD API base URL — change for GCC High: YOUR INFORMATION GOES HERE
CQD_API_BASE = "https://data.cqd.teams.microsoft.com"


# ─────────────────────────────────────────────────────────────────────────────
# Authentication — two separate scopes needed
# ─────────────────────────────────────────────────────────────────────────────
def get_graph_token() -> str:
    """Token for Graph API (Source A)."""
    app = msal.ConfidentialClientApplication(
        client_id=config.CLIENT_ID,             # YOUR INFORMATION GOES HERE (via env)
        client_credential=config.CLIENT_SECRET, # YOUR INFORMATION GOES HERE (via env)
        authority=f"https://login.microsoftonline.com/{config.TENANT_ID}",  # YOUR INFORMATION GOES HERE (via env)
        proxies=config.PROXIES or None,
    )
    result = app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
    if "access_token" not in result:
        logger.error(f"Graph token error: {result.get('error_description', result.get('error'))}")
        sys.exit(1)
    return result["access_token"]


def get_cqd_token() -> Optional[str]:
    """
    Token for CQD API (Source B).
    CQD API uses a separate scope. If unavailable, Source B is skipped gracefully.
    """
    app = msal.ConfidentialClientApplication(
        client_id=config.CLIENT_ID,             # YOUR INFORMATION GOES HERE (via env)
        client_credential=config.CLIENT_SECRET, # YOUR INFORMATION GOES HERE (via env)
        authority=f"https://login.microsoftonline.com/{config.TENANT_ID}",  # YOUR INFORMATION GOES HERE (via env)
        proxies=config.PROXIES or None,
    )
    # CQD API scope — may require additional permissions in Azure AD
    result = app.acquire_token_for_client(
        scopes=["https://api.interfaces.records.teams.microsoft.com/.default"]
    )
    if "access_token" not in result:
        logger.warning(
            "CQD API token could not be acquired — Source B (aggregate CQD) will be skipped. "
            "Ensure 'CallQuality.ReadBasic.All' or equivalent permission is granted in Entra ID."
        )
        return None
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
                "sourcetype": "teams:cqd:stream",
                "index":      config.INDEX_QUALITY,
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
# Source A: Extract quality from Graph API call record sessions/segments
# ─────────────────────────────────────────────────────────────────────────────
def collect_quality_from_graph(token: str) -> int:
    """
    Fetch recent call record IDs, then expand each to get session/segment
    media quality data. Extracts audio quality streams into individual events.
    """
    watermark = read_watermark(WATERMARK_CQD)
    from_dt   = watermark - timedelta(hours=config.CALL_RECORDS_LOOKBACK_HOURS)
    to_dt     = datetime.now(timezone.utc)

    url = f"{config.GRAPH_BASE_URL}/communications/callRecords"
    params = {
        "$filter":  f"startDateTime ge {from_dt.strftime('%Y-%m-%dT%H:%M:%SZ')}",
        "$select":  "id,startDateTime,type",
        "$top":     "100",
    }
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    logger.info(f"Fetching call quality from Graph API (from {from_dt.isoformat()})")

    # Step 1: get call record IDs
    call_ids: list[tuple[str, str]] = []  # (id, startDateTime)
    next_url: Optional[str] = url
    page = 0
    while next_url:
        try:
            resp = requests.get(
                next_url,
                headers=headers,
                params=params if page == 0 else None,
                proxies=config.PROXIES or None,
                timeout=60,
            )
            resp.raise_for_status()
        except requests.exceptions.RequestException as exc:
            logger.error(f"Failed to list call records: {exc}")
            break
        data = resp.json()
        for rec in data.get("value", []):
            call_ids.append((rec["id"], rec.get("startDateTime", "")))
        next_url = data.get("@odata.nextLink")
        page += 1
        if next_url:
            time.sleep(0.3)

    logger.info(f"Found {len(call_ids)} call records to expand for quality data.")

    # Step 2: expand each record for session/segment quality
    events: list[dict] = []
    latest_start = watermark

    for call_id, start_str in call_ids:
        expand_url = (
            f"{config.GRAPH_BASE_URL}/communications/callRecords/{call_id}"
            f"?$expand=sessions($expand=segments)"
        )
        try:
            resp = requests.get(
                expand_url,
                headers=headers,
                proxies=config.PROXIES or None,
                timeout=60,
            )
            resp.raise_for_status()
        except requests.exceptions.RequestException as exc:
            logger.warning(f"Failed to expand call {call_id}: {exc}")
            continue

        record = resp.json()
        stream_events = _extract_quality_streams(record)
        events.extend(stream_events)

        if start_str:
            try:
                dt = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
                if dt > latest_start:
                    latest_start = dt
            except ValueError:
                pass

        time.sleep(0.2)  # Respect Graph API rate limits

    sent = send_to_splunk(events)
    logger.info(f"Graph quality: extracted {len(events)} stream events, sent {sent}.")

    if sent > 0:
        write_watermark(WATERMARK_CQD, latest_start)

    return sent


def _extract_quality_streams(record: dict) -> list[dict]:
    """
    Walk the callRecord → sessions → segments → media structure
    and extract one event per audio/video media stream.
    """
    streams: list[dict] = []
    call_id    = record.get("id", "")
    start_time = record.get("startDateTime", "")
    call_type  = record.get("type", "")

    for session in record.get("sessions", []):
        session_id = session.get("id", "")
        caller     = session.get("caller", {})
        callee     = session.get("callee", {})

        for segment in session.get("segments", []):
            for media in segment.get("media", []):
                label = media.get("label", "")
                if label not in ("main-audio", "main-video", "vbss"):
                    continue

                caller_network = media.get("callerNetwork", {})
                callee_network = media.get("calleeNetwork", {})
                caller_device  = media.get("callerDevice",  {})

                # Flatten quality data into a single event
                stream_event: dict = {
                    "call_id":               call_id,
                    "session_id":            session_id,
                    "call_type":             call_type,
                    "stream_start_time":     start_time,
                    "media_label":           label,
                    "caller_upn":            caller.get("userPrincipalName", ""),
                    "callee_upn":            callee.get("userPrincipalName", ""),
                    "caller_subnet":         caller_network.get("subnet", ""),
                    "caller_ip":             caller_network.get("ipAddress", ""),
                    "caller_wifi":           caller_network.get("wifiBand", ""),
                    "caller_device_type":    caller_device.get("captureDeviceDriver", ""),
                    "callee_subnet":         callee_network.get("subnet", ""),
                    "audio_mos":             media.get("averageAudioNetworkJitter", {}).get("audioMos"),
                    "audio_jitter_ms":       _parse_duration_ms(
                                                media.get("averageAudioNetworkJitter", {}).get("audioNetworkJitter")
                                            ),
                    "packet_loss_rate":      media.get("averageAudioPacketLossRate"),
                    "round_trip_time_ms":    _parse_duration_ms(media.get("averageAudioRoundTripTime")),
                    "codec":                 media.get("callerCodec", ""),
                    "_ingest_time":          int(time.time()),
                    "_collector":            "cqd_graph_quality",
                    "_tenant_id":            config.TENANT_ID,
                }
                streams.append(stream_event)

    return streams


def _parse_duration_ms(iso_duration: Optional[str]) -> Optional[float]:
    """Convert ISO 8601 duration (PT0.123S) to milliseconds."""
    if not iso_duration:
        return None
    try:
        # Simple parser for PT#.###S format
        val = iso_duration.lstrip("PT").rstrip("S")
        return float(val) * 1000
    except (ValueError, AttributeError):
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Source B: CQD OData API (aggregate stream metrics)
# ─────────────────────────────────────────────────────────────────────────────
def collect_quality_from_cqd(cqd_token: str) -> int:
    """
    Query the CQD OData API for aggregate stream-level quality metrics.
    This provides building/subnet/device breakdowns not available in Graph API.

    CQD API Query format — modify dimensions/measures per your reporting needs.
    See: https://learn.microsoft.com/en-us/microsoftteams/cqd-data-and-reports
    """
    # CQD data has a ~3-hour lag; query last 24h with overlap
    watermark = read_watermark(Path(str(WATERMARK_CQD) + ".cqd"), default_hours_back=24)
    from_dt   = watermark - timedelta(hours=3)
    to_dt     = datetime.now(timezone.utc)

    # OData query for CQD — requesting key quality dimensions
    # YOUR INFORMATION GOES HERE — customize Dimensions and Measures for your reporting requirements
    query_payload = {
        "DataModelName": "QoEReport",
        "UserQuery": {
            "Dimensions": [
                {"DataModelName": "CallerSubnet"},
                {"DataModelName": "CallerBuildingName"},
                {"DataModelName": "CallerDeviceType"},
                {"DataModelName": "MediaType"},
                {"DataModelName": "NetworkTransportProtocol"},
            ],
            "Measurements": [
                {"DataModelName": "Avg(AudioStreamMOS-LQO)"},
                {"DataModelName": "Avg(JitterMin)"},
                {"DataModelName": "Avg(PacketLossRateMax)"},
                {"DataModelName": "Avg(RoundTripMax)"},
                {"DataModelName": "Count(TotalStreamCount)"},
                {"DataModelName": "Poor Stream Count(%)"},
            ],
            "Filters": [
                {
                    "DataModelName": "StartTime",
                    "Value":         from_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "Operand":       "ge",
                },
                {
                    "DataModelName": "StartTime",
                    "Value":         to_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "Operand":       "le",
                },
            ],
        },
    }

    headers = {
        "Authorization": f"Bearer {cqd_token}",
        "Content-Type":  "application/json",
        "Accept":        "application/json",
    }

    try:
        resp = requests.post(
            f"{CQD_API_BASE}/RunQuery",
            json=query_payload,
            headers=headers,
            proxies=config.PROXIES or None,
            timeout=120,
        )
        resp.raise_for_status()
    except requests.exceptions.RequestException as exc:
        logger.error(f"CQD API query failed: {exc}")
        return 0

    data     = resp.json()
    rows     = data.get("DataResult", [])
    columns  = data.get("DataModelName", [])
    events: list[dict] = []

    for row in rows:
        event = dict(zip(columns, row)) if isinstance(row, list) else row
        event["_ingest_time"] = int(time.time())
        event["_collector"]   = "cqd_aggregate"
        event["_tenant_id"]   = config.TENANT_ID
        event["query_from"]   = from_dt.isoformat()
        event["query_to"]     = to_dt.isoformat()
        events.append(event)

    sent = send_to_splunk(events)
    logger.info(f"CQD aggregate: fetched {len(events)} rows, sent {sent} events.")

    if sent > 0:
        write_watermark(Path(str(WATERMARK_CQD) + ".cqd"), to_dt)

    return sent


# ─────────────────────────────────────────────────────────────────────────────
# Main entry point
# ─────────────────────────────────────────────────────────────────────────────
def main() -> None:
    logger.info("=== CQD Collector starting ===")
    total = 0

    # Source A — always available if Graph API is configured
    graph_token = get_graph_token()
    total += collect_quality_from_graph(graph_token)

    # Source B — best-effort; skipped gracefully if CQD API perms not granted
    cqd_token = get_cqd_token()
    if cqd_token:
        total += collect_quality_from_cqd(cqd_token)
    else:
        logger.info("Skipping CQD OData API (no token — see CONFIG_VARIABLES.md Section 1).")

    logger.info(f"=== CQD Collector complete — {total} total events sent to Splunk ===")


if __name__ == "__main__":
    main()
