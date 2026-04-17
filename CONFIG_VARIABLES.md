# CONFIG_VARIABLES.md
## Bank of America — Teams to Splunk Integration
### Master Configuration Variable Reference

> **How to use this file:**
> Every variable marked <span style="color:red">**`YOUR INFORMATION GOES HERE`**</span> must be replaced with your
> environment-specific value before running any collector or deploying any configuration.
> In all code files, these variables are accompanied by the comment `# YOUR INFORMATION GOES HERE`.
> Do not commit real secrets to source control.

---

## 1. Azure AD / Entra ID (App Registration)

These values come from the Azure Portal → Azure Active Directory → App Registrations.

| Variable Name | Value | Description | Used In |
|---|---|---|---|
| `TEAMS_TENANT_ID` | <span style="color:red">**`YOUR INFORMATION GOES HERE`**</span> | Your Azure AD / Entra ID Tenant (Directory) ID — found in Azure Portal → Overview | All collectors, all API calls |
| `TEAMS_CLIENT_ID` | <span style="color:red">**`YOUR INFORMATION GOES HERE`**</span> | Application (Client) ID of the registered app in Entra ID | All collectors, OAuth2 token requests |
| `TEAMS_CLIENT_SECRET` | <span style="color:red">**`YOUR INFORMATION GOES HERE`**</span> | Client Secret value generated under Certificates & Secrets — store in vault | All collectors, OAuth2 token requests |

### Required API Permissions (Grant admin consent for all)

| Permission | Type | API | Purpose |
|---|---|---|---|
| `CallRecords.Read.All` | Application | Microsoft Graph | Read all Teams call records and stream data |
| `Reports.Read.All` | Application | Microsoft Graph | Read Teams usage and activity reports |
| `AuditLog.Read.All` | Application | Microsoft Graph | Read Teams audit log entries |
| `OnlineMeetings.Read.All` | Application | Microsoft Graph | Read all meeting details |
| `User.Read.All` | Application | Microsoft Graph | Resolve UPNs to user/department details |
| `ActivityFeed.Read` | Application | Office 365 Management API | Read Teams audit event feed |

### Azure AD Setup Steps

1. Navigate to **Azure Portal** → **Azure Active Directory** → **App registrations** → **New registration**
2. Name: `SplunkTeamsCollector` (or your naming convention)
3. Supported account types: **Accounts in this organizational directory only**
4. No redirect URI needed (daemon/service app)
5. After creation, go to **API permissions** → **Add a permission** and add all permissions listed above
6. Click **Grant admin consent for [your org]**
7. Go to **Certificates & secrets** → **New client secret** → set expiry per BofA policy
8. Copy the **Secret Value** immediately (it will not be shown again)

---

## 2. Splunk Enterprise (On-Premises)

These values come from your Splunk admin or Splunk Web configuration.

| Variable Name | Value | Description | Used In |
|---|---|---|---|
| `SPLUNK_HEC_URL` | <span style="color:red">**`YOUR INFORMATION GOES HERE`**</span> | Full HEC URL — e.g., `https://splunk-indexer.yourdomain.com:8088` | All collectors |
| `SPLUNK_HEC_TOKEN` | <span style="color:red">**`YOUR INFORMATION GOES HERE`**</span> | HEC token generated in Splunk Web → Settings → Data Inputs → HTTP Event Collector | All collectors |
| `SPLUNK_VERIFY_SSL` | `true` | Set to `false` only if using self-signed cert on HEC endpoint (not recommended for prod) | All collectors |
| `SPLUNK_INDEX_CALLS` | `teams_calls` | <span style="color:red">**`YOUR INFORMATION GOES HERE`**</span> — change if your org uses a different index name | graph_call_collector.py, pstn_collector.py |
| `SPLUNK_INDEX_QUALITY` | `teams_quality` | <span style="color:red">**`YOUR INFORMATION GOES HERE`**</span> — change if your org uses a different index name | cqd_collector.py |
| `SPLUNK_INDEX_AUDIT` | `teams_audit` | <span style="color:red">**`YOUR INFORMATION GOES HERE`**</span> — change if your org uses a different index name | audit_collector.py |
| `SPLUNK_INDEX_USAGE` | `teams_usage` | <span style="color:red">**`YOUR INFORMATION GOES HERE`**</span> — change if your org uses a different index name | graph_call_collector.py (reports) |

### HEC Setup Steps (Splunk Web)

1. **Settings** → **Data Inputs** → **HTTP Event Collector** → **New Token**
2. Name: `teams_collector`
3. Enable indexer acknowledgement: recommended for production
4. Source type: Leave blank (set per-event by collectors)
5. Assign to indexes: `teams_calls`, `teams_quality`, `teams_audit`, `teams_usage`
6. Copy the generated token value into `SPLUNK_HEC_TOKEN`

---

## 3. Network / Proxy

| Variable Name | Value | Description | Used In |
|---|---|---|---|
| `HTTPS_PROXY` | <span style="color:red">**`YOUR INFORMATION GOES HERE`**</span> | Proxy URL if the Heavy Forwarder cannot reach `graph.microsoft.com` directly — e.g., `http://proxy.bofa.com:8080`. Leave blank if no proxy required. | All collectors |
| `NO_PROXY` | <span style="color:red">**`YOUR INFORMATION GOES HERE`**</span> | Comma-separated list of hosts to bypass proxy — e.g., `splunk-indexer.bofa.com,localhost` | All collectors |

---

## 4. Collector Runtime Settings

| Variable Name | Default | Description | Used In |
|---|---|---|---|
| `COLLECTOR_STATE_DIR` | `C:\ProgramData\SplunkTeamsCollector\state` (Windows) or `/opt/splunk/var/lib/teams_collector_state` (Linux) | Directory where watermark/state files are stored to prevent duplicate events | All collectors |
| `POLL_INTERVAL_CALLS` | `300` | Seconds between call record polling cycles | inputs.conf, graph_call_collector.py |
| `POLL_INTERVAL_QUALITY` | `600` | Seconds between CQD quality data polling | inputs.conf, cqd_collector.py |
| `POLL_INTERVAL_AUDIT` | `300` | Seconds between audit log polling cycles | inputs.conf, audit_collector.py |
| `POLL_INTERVAL_USAGE` | `3600` | Seconds between usage report polling (reports lag by ~48h) | inputs.conf, graph_call_collector.py |
| `CALL_RECORDS_LOOKBACK_HOURS` | `2` | Extra hours to look back on each poll (call records can update up to 2h after call ends) | graph_call_collector.py |
| `LOG_LEVEL` | `INFO` | Logging verbosity: `DEBUG`, `INFO`, `WARNING`, `ERROR` | All collectors |

---

## 5. Microsoft Graph API Endpoints

These are standard Microsoft endpoints — only change if operating in a sovereign cloud (GCC, GCC High, DoD).

| Variable Name | Default Value | Description | Change For |
|---|---|---|---|
| `GRAPH_TOKEN_URL` | `https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token` | OAuth2 token endpoint | GCC High: `login.microsoftonline.us` |
| `GRAPH_BASE_URL` | `https://graph.microsoft.com/v1.0` | Microsoft Graph base URL | GCC High: `https://graph.microsoft.us/v1.0` |
| `MGMT_API_BASE_URL` | `https://manage.office.com/api/v1.0/{TENANT_ID}` | M365 Management Activity API | GCC High: `https://manage.office365.us` |

> **BofA Note:** If operating in **Microsoft GCC High** (common for regulated financial institutions), all three endpoints above <span style="color:red">**`MUST BE CHANGED`**</span> to the GCC High variants shown above.

---

## 6. Direct Routing / SBC (Phase 6 — Deferred)

These will be populated during SBC integration phase.

| Variable Name | Value | Description |
|---|---|---|
| `SBC_FQDN_PRIMARY` | <span style="color:red">**`YOUR INFORMATION GOES HERE`**</span> | Primary Ribbon SBC FQDN — e.g., `sbc1.voice.bofa.com` |
| `SBC_FQDN_SECONDARY` | <span style="color:red">**`YOUR INFORMATION GOES HERE`**</span> | Secondary/failover Ribbon SBC FQDN |
| `SBC_SYSLOG_PORT` | <span style="color:red">**`YOUR INFORMATION GOES HERE`**</span> | UDP/TCP port for SBC syslog output (typically 514 or 5514) |
| `SBC_SPLUNK_UF_HOST` | <span style="color:red">**`YOUR INFORMATION GOES HERE`**</span> | IP/hostname of Splunk Universal Forwarder receiving SBC syslog |

---

## 7. Environment Variable Setup (Production Deployment)

Set these on the Heavy Forwarder host before running collectors. **Never hardcode secrets in scripts.**

### Windows (PowerShell — run as Administrator)
```powershell
[System.Environment]::SetEnvironmentVariable("TEAMS_TENANT_ID",     "YOUR INFORMATION GOES HERE", "Machine")
[System.Environment]::SetEnvironmentVariable("TEAMS_CLIENT_ID",     "YOUR INFORMATION GOES HERE", "Machine")
[System.Environment]::SetEnvironmentVariable("TEAMS_CLIENT_SECRET", "YOUR INFORMATION GOES HERE", "Machine")
[System.Environment]::SetEnvironmentVariable("SPLUNK_HEC_URL",      "YOUR INFORMATION GOES HERE", "Machine")
[System.Environment]::SetEnvironmentVariable("SPLUNK_HEC_TOKEN",    "YOUR INFORMATION GOES HERE", "Machine")
[System.Environment]::SetEnvironmentVariable("HTTPS_PROXY",         "YOUR INFORMATION GOES HERE", "Machine")
```

### Linux (add to `/etc/environment` or Splunk service unit)
```bash
TEAMS_TENANT_ID=YOUR INFORMATION GOES HERE
TEAMS_CLIENT_ID=YOUR INFORMATION GOES HERE
TEAMS_CLIENT_SECRET=YOUR INFORMATION GOES HERE
SPLUNK_HEC_URL=YOUR INFORMATION GOES HERE
SPLUNK_HEC_TOKEN=YOUR INFORMATION GOES HERE
HTTPS_PROXY=YOUR INFORMATION GOES HERE
```

### Recommended: Azure Key Vault Integration (BofA Standard)
For BofA production environments, retrieve secrets from Azure Key Vault at runtime rather than storing in environment variables. The `collectors/config.py` includes a `USE_AZURE_KEYVAULT` flag — set `AZURE_KEYVAULT_URL` to your vault URI.

| Variable Name | Value | Description |
|---|---|---|
| `USE_AZURE_KEYVAULT` | `false` | Set to `true` to enable Key Vault secret retrieval |
| `AZURE_KEYVAULT_URL` | <span style="color:red">**`YOUR INFORMATION GOES HERE`**</span> | e.g., `https://bofa-splunk-kv.vault.azure.net/` |

---

## Changelog

| Date | Phase | Change |
|---|---|---|
| 2026-04-16 | Phase 1 | Initial CONFIG_VARIABLES.md created |
