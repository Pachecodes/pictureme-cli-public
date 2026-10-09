from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from pictureme import config
from pictureme.cli import app
from pictureme.config import CLIConfig, normalize_host


class FakeDeviceAPI:
    """An HTTP-boundary fake; the CLI still uses the real httpx client."""

    def __init__(
        self,
        *,
        start_payload: dict[str, object] | None = None,
        start_error: Exception | None = None,
        token_responses: list[tuple[int, dict[str, object]] | Exception] | None = None,
    ) -> None:
        self.requests: list[dict[str, object]] = []
        self.start_payload = start_payload or {
            "device_code": "test-device-secret",
            "user_code": "ABCD-EFGH",
            "verification_uri": "https://pictureme.now/connect",
            "verification_uri_complete": "https://pictureme.now/connect?user_code=ABCD-EFGH",
            "interval": 1,
            "expires_in": 10,
        }
        self.start_error = start_error
        self.token_responses = token_responses or [
            (200, {"access_token": "pmk_test_secret", "token_type": "bearer"})
        ]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.requests.append(
            {
                "path": request.url.path,
                "content_type": request.headers.get("Content-Type"),
                "body": body,
            }
        )

        if request.url.path == "/api/v3/public/oauth/device/code":
            if self.start_error:
                raise self.start_error
            return httpx.Response(200, json=self.start_payload)
        if request.url.path == "/api/v3/public/oauth/device/token":
            response = self.token_responses.pop(0)
            if isinstance(response, Exception):
                raise response
            status, payload = response
            return httpx.Response(status, json=payload)
        return httpx.Response(404, json={"error": "not_found"})


class FakeStatusAPI:
    def __init__(
        self,
        *,
        balance_status: int = 200,
        balance_payload: dict[str, object] | None = None,
    ) -> None:
        self.requests: list[dict[str, str | None]] = []
        self.balance_status = balance_status
        self.balance_payload = balance_payload or {"balance": 42}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(
            {
                "method": request.method,
                "path": request.url.path,
                "authorization": request.headers.get("Authorization"),
            }
        )
        if request.url.path == "/api/v3/public/health":
            return httpx.Response(200, json={"status": "ok", "version": "test"})
        if request.url.path == "/api/v3/shared/tokens/balance":
            return httpx.Response(self.balance_status, json=self.balance_payload)
        return httpx.Response(404, json={"error": "not_found"})


class FakePasswordAPI:
    def __init__(
        self,
        payload: dict[str, object] | None = None,
        *,
        status: int = 200,
        error: Exception | None = None,
    ) -> None:
        self.payload = payload or {}
        self.status = status
        self.error = error
        self.requests: list[dict[str, object]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(
            {
                "method": request.method,
                "path": request.url.path,
                "content_type": request.headers.get("Content-Type"),
                "body": json.loads(request.content),
            }
        )
        if request.url.path == "/api/v3/public/auth/sign-in/email":
            if self.error:
                raise self.error
            return httpx.Response(self.status, json=self.payload)
        return httpx.Response(404, json={"error": "not_found"})


def install_fake_api(monkeypatch, fake: FakeDeviceAPI) -> None:
    real_client = httpx.Client

    def client_with_fake_transport(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(fake)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", client_with_fake_transport)


def invoke_device_login(
    tmp_path: Path,
    monkeypatch,
    fake: FakeDeviceAPI,
    *,
    no_open: bool = True,
    skip_sleep: bool = True,
    host: str = "http://127.0.0.1:8765/api/v3",
    scopes: list[str] | None = None,
):
    install_fake_api(monkeypatch, fake)
    monkeypatch.setattr(config, "config_path", lambda: tmp_path / "config.json")
    monkeypatch.delenv("PICTUREME_HOST", raising=False)
    monkeypatch.delenv("PICTUREME_API_KEY", raising=False)
    if skip_sleep:
        monkeypatch.setattr("pictureme.commands.auth.time.sleep", lambda _: None)
    args = [
        "--json",
        "auth",
        "login",
        "--host",
        host,
    ]
    if no_open:
        args.append("--no-open")
    for scope in scopes or []:
        args.extend(["--scope", scope])
    return CliRunner().invoke(app, args)


def test_device_login_forwards_repeatable_explicit_ale_operator_scopes(
    tmp_path: Path, monkeypatch
) -> None:
    fake = FakeDeviceAPI()

    result = invoke_device_login(
        tmp_path,
        monkeypatch,
        fake,
        scopes=["ale-config:read", "ale-config:write"],
    )

    assert result.exit_code == 0, result.output
    assert fake.requests[0]["body"] == {
        "client_name": "pictureme-cli",
        "scopes": ["ale-config:read", "ale-config:write"],
        "credential_class": "ale_operator",
    }
    assert "pmk_test_secret" not in result.stdout
    assert "test-device-secret" not in result.stdout


def test_device_login_without_scope_preserves_backend_default_scope_contract(
    tmp_path: Path, monkeypatch
) -> None:
    fake = FakeDeviceAPI()

    result = invoke_device_login(tmp_path, monkeypatch, fake)

    assert result.exit_code == 0, result.output
    assert fake.requests[0]["body"] == {"client_name": "pictureme-cli"}


def test_device_login_normalizes_api_prefix_and_uses_exact_contract(
    tmp_path: Path, monkeypatch
) -> None:
    fake = FakeDeviceAPI()
    result = invoke_device_login(
        tmp_path,
        monkeypatch,
        fake,
        host="https://go.pictureme.now/api/v3",
    )

    assert result.exit_code == 0, result.output
    assert [request["path"] for request in fake.requests] == [
        "/api/v3/public/oauth/device/code",
        "/api/v3/public/oauth/device/token",
    ]
    assert fake.requests[0]["body"] == {"client_name": "pictureme-cli"}
    assert fake.requests[1]["body"] == {
        "device_code": "test-device-secret",
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
    }
    assert [request["content_type"] for request in fake.requests] == [
        "application/json",
        "application/json",
    ]


@pytest.mark.parametrize(
    "host",
    [
        "http://go.pictureme.now",
        "http://203.0.113.10:18131",
        "http://192.0.2.8/api/v3",
    ],
)
def test_host_normalization_rejects_insecure_remote_transport(host: str) -> None:
    with pytest.raises(ValueError, match="HTTPS"):
        normalize_host(host)


@pytest.mark.parametrize(
    "host",
    [
        "go.pictureme.now",
        "ftp://go.pictureme.now",
        "https://user:password@go.pictureme.now",
        "https://go.pictureme.now/other",
        "https://go.pictureme.now/api/v3?redirect=evil",
        "https://go.pictureme.now/#fragment",
    ],
)
def test_host_normalization_rejects_ambiguous_or_credentialed_urls(host: str) -> None:
    with pytest.raises(ValueError, match="host"):
        normalize_host(host)


def test_invalid_stored_host_fails_closed_without_reusing_token(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "host": "http://remote.example/api/v3",
                "api_key": "pmk_must_not_be_retargeted",
            }
        )
    )
    monkeypatch.setattr(config, "config_path", lambda: path)

    loaded = config.load_config()

    assert loaded == CLIConfig()


def test_cli_rejects_insecure_host_without_traceback_in_json_mode() -> None:
    result = CliRunner().invoke(
        app,
        [
            "--json",
            "--host",
            "http://remote.example/api/v3",
            "auth",
            "status",
        ],
    )

    assert result.exit_code == 2
    assert json.loads(result.stdout) == {
        "error": "invalid_host",
        "message": "PictureME bearer tokens require HTTPS for remote hosts",
    }
    assert result.exception is not None


UNSAFE_LOGIN_HOSTS = [
    (
        "http://remote.example/api/v3",
        "PictureME bearer tokens require HTTPS for remote hosts",
    ),
    (
        "https://user:password@go.pictureme.now",
        "host URL must not contain credentials",
    ),
]


@pytest.mark.parametrize(("host", "message"), UNSAFE_LOGIN_HOSTS)
def test_auth_login_subcommand_host_rejects_unsafe_host_without_traceback(
    tmp_path: Path, monkeypatch, host: str, message: str
) -> None:
    """`auth login --host <bad>` must fail as a usage error, never a traceback."""
    monkeypatch.setattr(config, "config_path", lambda: tmp_path / "config.json")
    monkeypatch.delenv("PICTUREME_HOST", raising=False)
    monkeypatch.delenv("PICTUREME_API_KEY", raising=False)

    result = CliRunner().invoke(
        app,
        ["auth", "login", "--host", host, "--no-open"],
    )

    assert result.exit_code == 2, result.output
    assert not isinstance(result.exception, ValueError)
    assert "Traceback" not in result.output
    # Rich renders the usage error in a wrapping panel with border glyphs
    # between wrapped lines, so strip the box characters and collapse
    # whitespace before comparing.
    flat = " ".join(result.output.replace("│", " ").split())
    assert "Invalid value for --host" in flat
    assert message in flat


@pytest.mark.parametrize(("host", "message"), UNSAFE_LOGIN_HOSTS)
def test_auth_login_subcommand_host_rejects_unsafe_host_with_json_error_envelope(
    tmp_path: Path, monkeypatch, host: str, message: str
) -> None:
    """JSON mode must emit the invalid_host envelope, not a raw traceback."""
    monkeypatch.setattr(config, "config_path", lambda: tmp_path / "config.json")
    monkeypatch.delenv("PICTUREME_HOST", raising=False)
    monkeypatch.delenv("PICTUREME_API_KEY", raising=False)

    result = CliRunner().invoke(
        app,
        ["--json", "auth", "login", "--host", host, "--no-open"],
    )

    assert result.exit_code == 2, result.output
    assert json.loads(result.stdout) == {
        "error": "invalid_host",
        "message": message,
    }


def test_json_device_login_streams_usable_instructions_without_secrets(
    tmp_path: Path, monkeypatch
) -> None:
    fake = FakeDeviceAPI()

    result = invoke_device_login(tmp_path, monkeypatch, fake)

    assert result.exit_code == 0, result.output
    events = [json.loads(line) for line in result.stdout.splitlines() if line]
    assert events[0] == {
        "event": "authorization_required",
        "verification_uri": "https://pictureme.now/connect",
        "verification_uri_complete": "https://pictureme.now/connect?user_code=ABCD-EFGH",
        "user_code": "ABCD-EFGH",
        "expires_in": 10,
        "interval": 1,
    }
    assert events[1]["event"] == "authenticated"
    assert events[1]["host"] == "http://127.0.0.1:8765"
    assert "config_file" in events[1]
    assert "test-device-secret" not in result.stdout
    assert "pmk_test_secret" not in result.stdout


@pytest.mark.parametrize(
    "unsafe_complete",
    ["javascript:alert(document.cookie)", "https://phishing.example/connect"],
)
def test_device_login_never_opens_an_unsafe_verification_url(
    tmp_path: Path, monkeypatch, unsafe_complete: str
) -> None:
    fake = FakeDeviceAPI(
        start_payload={
            "device_code": "test-device-secret",
            "user_code": "ABCD EFGH",
            "verification_uri": "https://pictureme.now/connect",
            "verification_uri_complete": unsafe_complete,
            "interval": 1,
            "expires_in": 10,
        }
    )
    opened: list[str] = []
    monkeypatch.setattr("pictureme.commands.auth.webbrowser.open", opened.append)

    result = invoke_device_login(tmp_path, monkeypatch, fake, no_open=False)

    assert result.exit_code == 0, result.output
    assert opened == ["https://pictureme.now/connect"]
    first_event = json.loads(result.stdout.splitlines()[0])
    assert first_event["verification_uri"] == "https://pictureme.now/connect"
    assert first_event["user_code"] == "ABCD EFGH"


def test_device_login_keeps_polling_pending_session_and_honors_slow_down(
    tmp_path: Path, monkeypatch
) -> None:
    fake = FakeDeviceAPI(
        start_payload={
            "device_code": "same-pending-session",
            "user_code": "ABCD-EFGH",
            "verification_uri": "https://pictureme.now/connect",
            "interval": 1,
            "expires_in": 60,
        },
        token_responses=[
            (400, {"error": "authorization_pending"}),
            (400, {"error": "slow_down"}),
            (200, {"access_token": "pmk_after_pending"}),
        ],
    )
    sleeps: list[int] = []
    monkeypatch.setattr("pictureme.commands.auth.time.sleep", sleeps.append)

    result = invoke_device_login(tmp_path, monkeypatch, fake, skip_sleep=False)

    assert result.exit_code == 0, result.output
    assert sleeps == [1, 1, 6]
    poll_bodies = [request["body"] for request in fake.requests[1:]]
    assert (
        poll_bodies
        == [
            {
                "device_code": "same-pending-session",
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            }
        ]
        * 3
    )


def test_device_login_retries_a_poll_network_error_without_losing_session(
    tmp_path: Path, monkeypatch
) -> None:
    fake = FakeDeviceAPI(
        start_payload={
            "device_code": "network-retry-session",
            "user_code": "ABCD-EFGH",
            "verification_uri": "https://pictureme.now/connect",
            "interval": 1,
            "expires_in": 60,
        },
        token_responses=[
            httpx.ConnectError("temporary disconnect"),
            (200, {"access_token": "pmk_after_network_retry"}),
        ],
    )

    result = invoke_device_login(tmp_path, monkeypatch, fake)

    assert result.exit_code == 0, result.output
    events = [json.loads(line) for line in result.stdout.splitlines() if line]
    assert [event["event"] for event in events] == [
        "authorization_required",
        "poll_retry",
        "authenticated",
    ]
    assert events[1] == {"event": "poll_retry", "error": "network_error"}
    assert len(fake.requests) == 3


@pytest.mark.parametrize("oauth_error", ["access_denied", "expired_token"])
def test_device_login_reports_terminal_oauth_errors_as_json(
    tmp_path: Path, monkeypatch, oauth_error: str
) -> None:
    fake = FakeDeviceAPI(
        token_responses=[(400, {"error": oauth_error})],
    )

    result = invoke_device_login(tmp_path, monkeypatch, fake)

    assert result.exit_code == 2
    events = [json.loads(line) for line in result.stdout.splitlines() if line]
    assert events[-1] == {
        "event": "authorization_failed",
        "error": oauth_error,
    }


def test_device_login_reports_invalid_grant_as_json(
    tmp_path: Path, monkeypatch
) -> None:
    fake = FakeDeviceAPI(
        token_responses=[
            (
                400,
                {"error": "invalid_grant", "message": "device code already exchanged"},
            )
        ],
    )

    result = invoke_device_login(tmp_path, monkeypatch, fake)

    assert result.exit_code == 1
    events = [json.loads(line) for line in result.stdout.splitlines() if line]
    assert events[-1] == {
        "event": "authorization_failed",
        "error": "invalid_grant",
    }


def test_device_login_reports_start_network_error_as_json(
    tmp_path: Path, monkeypatch
) -> None:
    fake = FakeDeviceAPI(start_error=httpx.ConnectError("DNS unavailable"))

    result = invoke_device_login(tmp_path, monkeypatch, fake)

    assert result.exit_code == 1
    assert [json.loads(line) for line in result.stdout.splitlines() if line] == [
        {"event": "authorization_failed", "error": "network_error"}
    ]


def test_device_login_rejects_an_incomplete_device_code_response(
    tmp_path: Path, monkeypatch
) -> None:
    fake = FakeDeviceAPI(
        start_payload={
            "device_code": "secret-without-user-code",
            "verification_uri": "https://pictureme.now/connect",
            "interval": 3,
            "expires_in": 600,
        }
    )

    result = invoke_device_login(tmp_path, monkeypatch, fake)

    assert result.exit_code == 1
    assert [json.loads(line) for line in result.stdout.splitlines() if line] == [
        {"event": "authorization_failed", "error": "invalid_device_response"}
    ]
    assert len(fake.requests) == 1


def test_device_login_rejects_success_response_without_access_token(
    tmp_path: Path, monkeypatch
) -> None:
    fake = FakeDeviceAPI(token_responses=[(200, {"token_type": "bearer"})])

    result = invoke_device_login(tmp_path, monkeypatch, fake)

    assert result.exit_code == 1
    assert [json.loads(line) for line in result.stdout.splitlines() if line][-1] == {
        "event": "authorization_failed",
        "error": "invalid_token_response",
    }
    assert len(fake.requests) == 2


def test_device_login_honors_expires_in_deadline(tmp_path: Path, monkeypatch) -> None:
    fake = FakeDeviceAPI(
        start_payload={
            "device_code": "expiring-session",
            "user_code": "ABCD-EFGH",
            "verification_uri": "https://pictureme.now/connect",
            "interval": 1,
            "expires_in": 2,
        },
        token_responses=[(400, {"error": "authorization_pending"})],
    )
    clock = iter([100.0, 100.0, 103.0])
    monkeypatch.setattr("pictureme.commands.auth.time.monotonic", lambda: next(clock))

    result = invoke_device_login(tmp_path, monkeypatch, fake)

    assert result.exit_code == 2
    events = [json.loads(line) for line in result.stdout.splitlines() if line]
    assert events[-1] == {
        "event": "authorization_failed",
        "error": "expired_token",
    }
    assert len(fake.requests) == 2


def test_logout_json_clears_only_local_token_without_leaking_it(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "config.json"
    monkeypatch.setattr(config, "config_path", lambda: path)
    config.save_config(
        CLIConfig(host="https://go.pictureme.now/api/v3", api_key="pmk_logout_secret")
    )

    result = CliRunner().invoke(app, ["--json", "auth", "logout"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "status": "logged_out",
        "host": "https://go.pictureme.now",
        "config_file": str(path),
    }
    assert "pmk_logout_secret" not in result.stdout
    assert config.load_config().api_key is None


def test_saved_token_is_0600_and_status_uses_harmless_authenticated_read(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "config.json"
    token = "pmk_status_secret"
    monkeypatch.setattr(config, "config_path", lambda: path)
    config.save_config(CLIConfig(host="https://go.pictureme.now/api", api_key=token))
    fake = FakeStatusAPI()
    install_fake_api(monkeypatch, fake)

    result = CliRunner().invoke(app, ["--json", "auth", "status"])

    assert result.exit_code == 0, result.output
    assert path.stat().st_mode & 0o777 == 0o600
    status = json.loads(result.stdout)
    assert status["host"] == "https://go.pictureme.now"
    assert status["has_token"] is True
    assert status["backend_status"] == "ok"
    assert status["authenticated_as"] == "ok"
    assert status["token_balance"] == 42
    assert token not in result.stdout
    assert fake.requests == [
        {
            "method": "GET",
            "path": "/api/v3/public/health",
            "authorization": None,
        },
        {
            "method": "GET",
            "path": "/api/v3/shared/tokens/balance",
            "authorization": f"Bearer {token}",
        },
    ]


def test_status_json_does_not_echo_server_error_payloads(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "config.json"
    token = "pmk_echoed_by_host"
    monkeypatch.setattr(config, "config_path", lambda: path)
    config.save_config(CLIConfig(host="https://go.pictureme.now", api_key=token))
    fake = FakeStatusAPI(balance_status=401, balance_payload={"error": token})
    install_fake_api(monkeypatch, fake)

    result = CliRunner().invoke(app, ["--json", "auth", "status"])

    assert result.exit_code == 0, result.output
    status = json.loads(result.stdout)
    assert status["authenticated_as"] is None
    assert status["auth_error"] == "authentication_failed"
    assert token not in result.stdout


def test_token_json_never_includes_token_material(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "config.json"
    token = "pmk_token_must_not_be_in_json"
    monkeypatch.setattr(config, "config_path", lambda: path)
    config.save_config(CLIConfig(host="https://go.pictureme.now", api_key=token))

    result = CliRunner().invoke(app, ["--json", "auth", "token"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "host": "https://go.pictureme.now",
        "has_token": True,
        "redacted": True,
    }
    assert token not in result.stdout
    assert token[:8] not in result.stdout


def test_token_json_rejects_show(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "config.json"
    token = "pmk_token_must_not_be_in_json"
    monkeypatch.setattr(config, "config_path", lambda: path)
    config.save_config(CLIConfig(host="https://go.pictureme.now", api_key=token))

    result = CliRunner().invoke(app, ["--json", "auth", "token", "--show"])

    assert result.exit_code == 2
    assert json.loads(result.stdout) == {
        "error": "unsafe_flag_combination",
        "message": "--show cannot be used with --json",
    }
    assert token not in result.stdout
    assert token[:8] not in result.stdout


def test_token_human_default_outputs_no_token_material(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "config.json"
    token = "pmk_human_output_must_stay_hidden"
    monkeypatch.setattr(config, "config_path", lambda: path)
    config.save_config(CLIConfig(host="https://go.pictureme.now", api_key=token))

    result = CliRunner().invoke(app, ["auth", "token"])

    assert result.exit_code == 0, result.output
    # No meaningful prefix of the stored bearer may appear (single chars like
    # "p" collide with the hostname, so check prefixes of 4+ chars).
    assert all(token[:i] not in result.stdout for i in range(4, len(token) + 1))
    assert "pmk_" not in result.stdout


def test_token_human_show_is_explicit_opt_in(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "config.json"
    token = "pmk_human_show_expected_once"
    monkeypatch.setattr(config, "config_path", lambda: path)
    config.save_config(CLIConfig(host="https://go.pictureme.now", api_key=token))

    result = CliRunner().invoke(app, ["auth", "token", "--show"])

    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == token


def test_token_json_without_credentials_is_machine_readable(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "missing-config.json"
    monkeypatch.setattr(config, "config_path", lambda: path)

    result = CliRunner().invoke(app, ["--json", "auth", "token"])

    assert result.exit_code == 2
    assert json.loads(result.stdout) == {
        "error": "not_authenticated",
        "host": "https://go.pictureme.now",
        "has_token": False,
    }


def test_password_fallback_matches_go_better_auth_contract(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "config.json"
    token = "better_auth_session_secret"
    fake = FakePasswordAPI(
        {
            "user": {"id": "user-id", "email": "person@example.com"},
            "session": {"id": "session-id", "userId": "user-id"},
            "token": token,
        }
    )
    install_fake_api(monkeypatch, fake)
    monkeypatch.setattr(config, "config_path", lambda: path)
    monkeypatch.setattr(
        "pictureme.commands.auth.typer.prompt", lambda *args, **kwargs: "password123"
    )

    result = CliRunner().invoke(
        app,
        [
            "--json",
            "auth",
            "login",
            "--host",
            "http://localhost:8765/api",
            "--password",
            "--email",
            "person@example.com",
        ],
    )

    assert result.exit_code == 0, result.output
    assert [json.loads(line) for line in result.stdout.splitlines() if line] == [
        {
            "event": "authenticated",
            "host": "http://localhost:8765",
            "config_file": str(path),
        }
    ]
    assert token not in result.stdout
    assert config.load_config().api_key == token
    assert fake.requests == [
        {
            "method": "POST",
            "path": "/api/v3/public/auth/sign-in/email",
            "content_type": "application/json",
            "body": {"email": "person@example.com", "password": "password123"},
        }
    ]


def test_password_fallback_rejects_non_contract_token_shapes_without_leaking(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "config.json"
    nested_secret = "nested_session_secret"
    fake = FakePasswordAPI({"session": {"token": nested_secret}})
    install_fake_api(monkeypatch, fake)
    monkeypatch.setattr(config, "config_path", lambda: path)
    monkeypatch.setattr(
        "pictureme.commands.auth.typer.prompt", lambda *args, **kwargs: "password123"
    )

    result = CliRunner().invoke(
        app,
        [
            "--json",
            "auth",
            "login",
            "--host",
            "http://localhost:8765",
            "--password",
            "--email",
            "person@example.com",
        ],
    )

    assert result.exit_code == 1
    assert json.loads(result.stdout) == {
        "event": "authorization_failed",
        "error": "invalid_auth_response",
    }
    assert nested_secret not in result.stdout
    assert not path.exists()


@pytest.mark.parametrize(
    ("fake", "error_code"),
    [
        (
            FakePasswordAPI({"error": "invalid email or password"}, status=401),
            "authentication_failed",
        ),
        (FakePasswordAPI(error=httpx.ConnectError("offline")), "network_error"),
    ],
)
def test_password_fallback_failures_are_machine_readable_and_safe(
    tmp_path: Path, monkeypatch, fake: FakePasswordAPI, error_code: str
) -> None:
    path = tmp_path / "config.json"
    install_fake_api(monkeypatch, fake)
    monkeypatch.setattr(config, "config_path", lambda: path)
    monkeypatch.setattr(
        "pictureme.commands.auth.typer.prompt", lambda *args, **kwargs: "wrong-password"
    )

    result = CliRunner().invoke(
        app,
        [
            "--json",
            "auth",
            "login",
            "--host",
            "http://localhost:8765",
            "--password",
            "--email",
            "person@example.com",
        ],
    )

    assert result.exit_code == 1
    assert json.loads(result.stdout) == {
        "event": "authorization_failed",
        "error": error_code,
    }
    assert "wrong-password" not in result.stdout
    assert not path.exists()
