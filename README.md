# Bank of America — Microsoft Teams to Splunk Integration

## Overview

This project delivers a full data pipeline from **Microsoft Teams** into **Splunk Enterprise (on-premises)**, purpose-built for Bank of America's Direct Routing migration from Ribbon SBCs.

Data is collected via the **Microsoft Graph API**, **CQD API**, and **M365 Management Activity API** — providing far greater depth than Teams Admin Center (TAC) exports — including stream-level call quality, Direct Routing SBC telemetry, PSTN call records, user adoption metrics, and compliance audit logs.

---

## Repository Structure

```
Splunk/
├── CONFIG_VARIABLES.md          ← REQUIRED: Fill in all red-marked values before running anything
├── README.md
│
├── collectors/                  ← Python data pipeline (runs on Splunk Heavy Forwarder)
│   ├── config.py                ← Shared config loader (reads from environment variables)
│   ├── requirements.txt         ← Python dependencies
│   ├── graph_call_collector.py  ← Graph API: call records + Direct Routing logs
│   ├── cqd_collector.py         ← CQD API: stream-level quality (MOS, jitter, packet loss)
│   ├── pstn_collector.py        ← Graph API: PSTN call logs
│   └── audit_collector.py       ← M365 Management Activity API: audit & compliance events
│
├── splunk/                      ← Splunk configuration files (deploy to Heavy Forwarder)
│   ├── indexes.conf             ← Index definitions (deploy to Indexer)
│   ├── inputs.conf              ← Modular inputs wiring Python scripts to Splunk
│   ├── props.conf               ← Sourcetype definitions and field extractions
│   ├── transforms.conf          ← Lookups and field aliases
│   └── savedsearches.conf       ← Alerts and scheduled reports
│
└── dashboards/                  ← Splunk Dashboard Studio JSON (import via Splunk Web)
    ├── executive_overview.json
    ├── call_quality.json
    ├── direct_routing_health.json
    ├── user_adoption.json
    └── audit_compliance.json
```

---

## Prerequisites

| Requirement | Details |
|---|---|
| Splunk Enterprise | Version 9.0+ recommended (Dashboard Studio requires 8.1+) |
| Splunk Heavy Forwarder | Separate instance from Indexer; requires internet egress to `graph.microsoft.com` |
| Python | 3.9+ on the Heavy Forwarder host |
| Azure AD App Registration | App-only permissions — see `CONFIG_VARIABLES.md` Section 1 |
| Microsoft Teams | Teams Phone / Direct Routing enabled tenant |

---

## Quick Start

### Step 1 — Fill in CONFIG_VARIABLES.md
Open `CONFIG_VARIABLES.md` and replace every <span style="color:red">**`YOUR INFORMATION GOES HERE`**</span> value with your environment-specific credentials and settings.

### Step 2 — Set environment variables
Use the commands in `CONFIG_VARIABLES.md` Section 7 to set all required environment variables on the Heavy Forwarder.

### Step 3 — Install Python dependencies
```bash
cd collectors
pip install -r requirements.txt
```

### Step 4 — Deploy Splunk config files
Copy the contents of `splunk/` to your Splunk app directory:
```
$SPLUNK_HOME/etc/apps/TA_teams_collector/local/
```
Restart Splunk or reload the app.

### Step 5 — Import dashboards
In Splunk Web: **Search & Reporting** → **Dashboards** → **Create New Dashboard** → **Import JSON** → paste each file from `dashboards/`.

---

## Data Sources & Indexes

| Index | Sourcetypes | Data Description |
|---|---|---|
| `teams_calls` | `teams:call:record`, `teams:call:dr_record` | Full call records, Direct Routing logs, session/segment data |
| `teams_quality` | `teams:cqd:stream` | Stream-level: MOS, jitter, packet loss, RTT, codec, subnet |
| `teams_audit` | `teams:audit:log` | Admin actions, config changes, compliance events |
| `teams_usage` | `teams:report:activity`, `teams:report:device` | User adoption, meeting trends, device distribution |

---

## Dashboard Suite

| Dashboard | Purpose |
|---|---|
| Executive Overview | KPI tiles, call volume trends, quality score, active users |
| Call Quality Deep Dive | MOS heatmap, jitter/packet loss over time, worst subnets |
| Direct Routing & PSTN Health | SBC response codes, trunk utilization, failed call analysis |
| User Adoption | Department adoption rates, device types, meeting vs. call split |
| Audit & Compliance | Admin actions, Teams config changes, anomaly detection |

---

## Phase Roadmap

- [x] Phase 1 — Azure AD App Registration & CONFIG_VARIABLES.md
- [x] Phase 2 — Splunk infrastructure (indexes, props, transforms, HEC)
- [x] Phase 3 — Python data pipeline (4 collector scripts)
- [x] Phase 4 — Dashboard suite (5 Dashboard Studio dashboards)
- [x] Phase 5 — Alerts & saved searches
- [ ] Phase 6 — Ribbon SBC syslog integration (deferred)

---

## Security Notes

- **Never commit real secrets** — `TEAMS_CLIENT_SECRET` and `SPLUNK_HEC_TOKEN` must be stored in a vault or environment variables, not in code
- All collectors support **Azure Key Vault** secret retrieval via the `USE_AZURE_KEYVAULT` flag
- HEC traffic should be TLS-encrypted (`SPLUNK_VERIFY_SSL=true`)
- The Graph API app uses **app-only** (non-delegated) permissions — no user credentials required
