"""Standalone client contract checks; no private backend source dependency.

These do NOT certify a deployed server; fake HTTP behavior is tested in test_auth_cli.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def test_client_auth_routes_are_explicit():
    source = (ROOT / "pictureme/commands/auth.py").read_text()
    for route in ("/api/v3/public/auth/sign-in/email", "/api/v3/public/oauth/device/code",
                  "/api/v3/public/oauth/device/token", "/api/v3/shared/tokens/balance"):
        assert route in source

def test_client_device_contract_keys():
    source = (ROOT / "pictureme/commands/auth.py").read_text()
    for field in ("device_code", "user_code", "verification_uri", "interval", "expires_in",
                  "access_token", "authorization_pending", "slow_down", "access_denied", "expired_token"):
        assert field in source

def test_operator_client_routes():
    source = (ROOT / "pictureme/commands/ale.py").read_text()
    assert "/api/v3/operator/ale" in source
