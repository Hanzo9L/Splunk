"""
config.py — Shared configuration loader for all Teams → Splunk collectors.
Bank of America — Teams to Splunk Integration

Reads all configuration from environment variables.
See CONFIG_VARIABLES.md for the full list of required values and setup instructions.

Optional: Set USE_AZURE_KEYVAULT=true and AZURE_KEYVAULT_URL to retrieve secrets
from Azure Key Vault at runtime instead of environment variables (recommended for prod).
"""

import os
import sys
import logging
import platform
from pathlib import Path

logger = logging.getLogger(__name__)


def _get_required(key: str) -> str:
    """Read a required environment variable; exit with a clear error if missing."""
    val = os.environ.get(key, "").strip()
    if not val:
        logger.error(
            f"Required environment variable '{key}' is not set. "
            f"See CONFIG_VARIABLES.md for setup instructions."
        )
        sys.exit(1)
    return val


def _get_optional(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def _load_from_keyvault() -> None:
    """
    If USE_AZURE_KEYVAULT=true, retrieve TEAMS_CLIENT_SECRET and SPLUNK_HEC_TOKEN
    from Azure Key Vault and inject them into the environment at runtime.
    The Heavy Forwarder's managed identity or service principal is used for auth.
    """
    kv_url = _get_optional("AZURE_KEYVAULT_URL")
    if not kv_url:
        logger.error("USE_AZURE_KEYVAULT=true but AZURE_KEYVAULT_URL is not set.")
        sys.exit(1)

    try:
        from azure.identity import DefaultAzureCredential
        from azure.keyvault.secrets import SecretClient

        credential = DefaultAzureCredential()
        client = SecretClient(vault_url=kv_url, credential=credential)

        # Secret names in Key Vault — adjust to match your BofA naming convention
        # YOUR INFORMATION GOES HERE — update these secret names to match your Key Vault
        secret_map = {
            "TEAMS_CLIENT_SECRET": "teams-splunk-client-secret",   # YOUR INFORMATION GOES HERE
            "SPLUNK_HEC_TOKEN":    "teams-splunk-hec-token",        # YOUR INFORMATION GOES HERE
        }
        for env_key, kv_secret_name in secret_map.items():
            secret = client.get_secret(kv_secret_name)
            os.environ[env_key] = secret.value
            logger.info(f"Loaded {env_key} from Key Vault secret '{kv_secret_name}'")

    except Exception as exc:
        logger.error(f"Failed to load secrets from Azure Key Vault: {exc}")
        sys.exit(1)


# ─────────────────────────────────────────────────────────────────────────────
# Key Vault (optional — recommended for BofA production)
# ─────────────────────────────────────────────────────────────────────────────
USE_AZURE_KEYVAULT: bool = _get_optional("USE_AZURE_KEYVAULT", "false").lower() == "true"
if USE_AZURE_KEYVAULT:
    _load_from_keyvault()

# ─────────────────────────────────────────────────────────────────────────────
# Azure AD / Entra ID — YOUR INFORMATION GOES HERE for all three values
# ─────────────────────────────────────────────────────────────────────────────
TENANT_ID:     str = _get_required("TEAMS_TENANT_ID")      # YOUR INFORMATION GOES HERE
CLIENT_ID:     str = _get_required("TEAMS_CLIENT_ID")       # YOUR INFORMATION GOES HERE
CLIENT_SECRET: str = _get_required("TEAMS_CLIENT_SECRET")   # YOUR INFORMATION GOES HERE

# ─────────────────────────────────────────────────────────────────────────────
# Splunk HEC — YOUR INFORMATION GOES HERE for URL and token
# ─────────────────────────────────────────────────────────────────────────────
SPLUNK_HEC_URL:    str  = _get_required("SPLUNK_HEC_URL")    # YOUR INFORMATION GOES HERE
SPLUNK_HEC_TOKEN:  str  = _get_required("SPLUNK_HEC_TOKEN")  # YOUR INFORMATION GOES HERE
SPLUNK_VERIFY_SSL: bool = _get_optional("SPLUNK_VERIFY_SSL", "true").lower() == "true"

# ─────────────────────────────────────────────────────────────────────────────
# Index names — change if your org uses different index names (see CONFIG_VARIABLES.md)
# ─────────────────────────────────────────────────────────────────────────────
INDEX_CALLS:   str = _get_optional("SPLUNK_INDEX_CALLS",   "teams_calls")    # YOUR INFORMATION GOES HERE
INDEX_QUALITY: str = _get_optional("SPLUNK_INDEX_QUALITY", "teams_quality")  # YOUR INFORMATION GOES HERE
INDEX_AUDIT:   str = _get_optional("SPLUNK_INDEX_AUDIT",   "teams_audit")    # YOUR INFORMATION GOES HERE
INDEX_USAGE:   str = _get_optional("SPLUNK_INDEX_USAGE",   "teams_usage")    # YOUR INFORMATION GOES HERE

# ─────────────────────────────────────────────────────────────────────────────
# Proxy — YOUR INFORMATION GOES HERE if Heavy Forwarder requires proxy
# Leave blank if direct internet access is available
# ─────────────────────────────────────────────────────────────────────────────
HTTPS_PROXY: str = _get_optional("HTTPS_PROXY", "")   # YOUR INFORMATION GOES HERE
NO_PROXY:    str = _get_optional("NO_PROXY",    "")    # YOUR INFORMATION GOES HERE

PROXIES: dict = {}
if HTTPS_PROXY:
    PROXIES = {"https": HTTPS_PROXY, "http": HTTPS_PROXY}
    if NO_PROXY:
        PROXIES["no_proxy"] = NO_PROXY

# ─────────────────────────────────────────────────────────────────────────────
# Microsoft API endpoints
# Change to GCC High variants if BofA operates in a sovereign cloud.
# See CONFIG_VARIABLES.md Section 5 for GCC High URLs.
# ─────────────────────────────────────────────────────────────────────────────
GRAPH_TOKEN_URL: str = f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token"
GRAPH_BASE_URL:  str = _get_optional("GRAPH_BASE_URL", "https://graph.microsoft.com/v1.0")
MGMT_API_BASE:   str = _get_optional(
    "MGMT_API_BASE_URL",
    f"https://manage.office.com/api/v1.0/{TENANT_ID}"
)

# ─────────────────────────────────────────────────────────────────────────────
# Watermark / state file directory
# ─────────────────────────────────────────────────────────────────────────────
_default_state_dir = (
    r"C:\ProgramData\SplunkTeamsCollector\state"
    if platform.system() == "Windows"
    else "/opt/splunk/var/lib/teams_collector_state"
)
STATE_DIR: Path = Path(_get_optional("COLLECTOR_STATE_DIR", _default_state_dir))
STATE_DIR.mkdir(parents=True, exist_ok=True)

# ─────────────────────────────────────────────────────────────────────────────
# Poll intervals (seconds)
# ─────────────────────────────────────────────────────────────────────────────
POLL_INTERVAL_CALLS:   int = int(_get_optional("POLL_INTERVAL_CALLS",   "300"))
POLL_INTERVAL_QUALITY: int = int(_get_optional("POLL_INTERVAL_QUALITY", "600"))
POLL_INTERVAL_AUDIT:   int = int(_get_optional("POLL_INTERVAL_AUDIT",   "300"))
POLL_INTERVAL_USAGE:   int = int(_get_optional("POLL_INTERVAL_USAGE",   "3600"))

# Graph API call records can be updated up to 2 hours after the call ends
CALL_RECORDS_LOOKBACK_HOURS: int = int(_get_optional("CALL_RECORDS_LOOKBACK_HOURS", "2"))

# ─────────────────────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────────────────────
LOG_LEVEL: str = _get_optional("LOG_LEVEL", "INFO").upper()
