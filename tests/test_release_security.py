import json
import os
import stat
import httpx
import pytest
from pictureme import config
from pictureme.client import APIError, PictureMEClient

def test_atomic_private_write_and_symlink(monkeypatch, tmp_path):
    path = tmp_path / "config.json"
    target = tmp_path / "other.json"
    target.write_text("untouched")
    path.symlink_to(target)
    monkeypatch.setattr(config, "config_path", lambda: path)
    config.save_config(config.CLIConfig(api_key="fixture-token"))
    assert target.read_text() == "untouched"
    assert not path.is_symlink()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text())["api_key"] == "fixture-token"

def test_permission_failure_does_not_write(monkeypatch, tmp_path):
    path = tmp_path / "config.json"
    monkeypatch.setattr(config, "config_path", lambda: path)
    def deny(*args):
        raise PermissionError("denied")
    monkeypatch.setattr(os, "fchmod", deny)
    with pytest.raises(PermissionError):
        config.save_config(config.CLIConfig(api_key="fixture-token"))
    assert not path.exists()
    assert not list(tmp_path.glob(".config-*"))

def test_host_override_never_rebinds_stored_key(monkeypatch):
    monkeypatch.delenv("PICTUREME_API_KEY", raising=False)
    monkeypatch.delenv("PICTUREME_HOST", raising=False)
    monkeypatch.setattr(config, "load_config", lambda: config.CLIConfig(host="https://api.example.com", api_key="fixture-token"))
    assert config.resolve_runtime(cli_host="https://other.example.com").api_key is None
    assert config.resolve_runtime(cli_host="https://api.example.com/api").api_key == "fixture-token"

def test_server_error_body_not_disclosed():
    with PictureMEClient(config.CLIConfig(host="https://api.example.com")) as client:
        client._client.close()
        client._client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(401, json={"error": "fixture-sensitive-echo"})), base_url="https://api.example.com")
        with pytest.raises(APIError) as error:
            client.get("/test")
    assert str(error.value) == "HTTP 401"
    assert "fixture-sensitive-echo" not in str(error.value)
