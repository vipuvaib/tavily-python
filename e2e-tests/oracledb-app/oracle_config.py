"""Test-only connection configuration shared by controllers and wheel consumers."""
import json
import os
from pathlib import Path
import re

import oracledb

OPTIONAL = ("ORACLE_E2E_MODE", "ORACLE_CLIENT_LIB_DIR", "ORACLE_CONNECT_KWARGS",
            "ORACLE_E2E_REQUIRE_LIVE_PLAN")
ALLOWED = {"config_dir", "wallet_location", "wallet_password", "ssl_server_dn_match",
           "ssl_server_cert_dn", "disable_oob", "stmtcachesize"}


def options(values=None):
    values = os.environ if values is None else values
    result = json.loads(values.get("ORACLE_CONNECT_KWARGS") or "{}")
    if not isinstance(result, dict) or not set(result).issubset(ALLOWED):
        raise ValueError("Unsupported test connection options")
    if result.get("ssl_server_dn_match") is False:
        raise ValueError("TLS certificate identity matching must remain enabled")
    return result


def initialize(values=None):
    values = os.environ if values is None else values
    mode = values.get("ORACLE_E2E_MODE", "thin")
    if mode not in {"thin", "thick"}:
        raise ValueError("Oracle mode must be thin or thick")
    if mode == "thick" and oracledb.is_thin_mode():
        library = Path(values.get("ORACLE_CLIENT_LIB_DIR", ""))
        if not library.is_absolute() or not library.is_dir():
            raise ValueError("Thick coverage requires an absolute native client directory")
        configuration = options(values).get("config_dir")
        oracledb.init_oracle_client(lib_dir=str(library), config_dir=configuration)
        # Build the descriptor from tested driver parameters, including TLS DN and wallet options.
        oracledb.defaults.thick_mode_dsn_passthrough = False
    if mode == "thin" and not oracledb.is_thin_mode():
        raise ValueError("Thin and thick runs must use separate processes")


def server_major(version):
    major = int(version.split(".")[0])
    if not 23 <= major <= 26:
        raise ValueError("Deployment testing accepts Oracle Database 23 through 26 only")
    return major


def server_release(version, banner):
    """Oracle 26ai can report version 23.26.x; classify the product banner too."""
    server_major(version)
    match = re.search(r"\b(\d{2})(?:ai|c)\b", banner, re.IGNORECASE)
    if match is None or not 23 <= int(match.group(1)) <= 26:
        raise ValueError("Deployment testing requires a verified Oracle 23–26 product release banner")
    return int(match.group(1))
