"""auth login / token / status / logout.

`login` uses the RFC8628-style device flow against
/api/v3/public/oauth/device/*. The CLI never sees the user's password —
the browser handles whatever auth the web app already supports, then the
user clicks Approve and the CLI receives a pmk_ API key.

`--password` is kept as a CI-friendly fallback that posts directly to
/api/v3/public/auth/sign-in/email; that path is suitable for headless
servers but should be considered legacy for human use.
"""

from __future__ import annotations

import ipaddress
import time
import webbrowser
from typing import Optional
from urllib.parse import urlsplit

import typer

from ..client import APIError, PictureMEClient
from ..config import CLIConfig, config_path, load_config, save_config
from ._common import (
    console,
    emit_json,
    emit_json_line,
    err_console,
    get_config,
    handle_api_error,
    json_mode,
)

app = typer.Typer(no_args_is_help=True)


@app.command()
def login(
    ctx: typer.Context,
    host: Optional[str] = typer.Option(
        None, "--host", help="Override the host to bind the token against."
    ),
    password: bool = typer.Option(
        False,
        "--password",
        help="Use email+password instead of the browser device flow (CI/headless).",
    ),
    email: Optional[str] = typer.Option(
        None, "--email", help="Account email — required with --password."
    ),
    scope: Optional[list[str]] = typer.Option(
        None,
        "--scope",
        help="Explicit device credential scope. Repeat for multiple scopes.",
    ),
    no_open: bool = typer.Option(
        False,
        "--no-open",
        help="Don't try to open the browser automatically; just print the URL.",
    ),
) -> None:
    """Browser device login. Prints a short code, opens the approval page,
    polls until you click Approve in the browser, then stores the token.
    """
    cfg = get_config(ctx)
    if host:
        try:
            cfg = CLIConfig(host=host.rstrip("/"), api_key=cfg.api_key, extras=cfg.extras)
        except ValueError as exc:
            if json_mode(ctx):
                emit_json({"error": "invalid_host", "message": str(exc)})
                raise typer.Exit(code=2)
            raise typer.BadParameter(str(exc), param_hint="--host") from exc

    if password:
        return _login_password(cfg, email, json_output=json_mode(ctx))

    return _login_device(
        cfg,
        open_browser=not no_open,
        json_output=json_mode(ctx),
        scopes=scope,
    )


def _login_password(
    cfg: CLIConfig, email: Optional[str], json_output: bool = False
) -> None:
    if not email:
        email = typer.prompt("Email")
    pwd = typer.prompt("Password", hide_input=True)
    with PictureMEClient(CLIConfig(host=cfg.host)) as client:
        try:
            payload = client.post(
                "/api/v3/public/auth/sign-in/email",
                json={"email": email, "password": pwd},
            )
        except APIError as exc:
            if json_output:
                if exc.status_code is None:
                    error = "network_error"
                elif exc.status_code in (400, 401, 403):
                    error = "authentication_failed"
                else:
                    error = "server_error"
                emit_json_line({"event": "authorization_failed", "error": error})
                raise typer.Exit(code=1)
            raise handle_api_error(exc)
    token = payload.get("token") if isinstance(payload, dict) else None
    if not isinstance(token, str) or not token:
        if json_output:
            emit_json_line(
                {"event": "authorization_failed", "error": "invalid_auth_response"}
            )
        else:
            err_console.print(
                "[red]Login succeeded but returned an invalid auth response.[/red]"
            )
        raise typer.Exit(code=1)
    _persist_token(cfg.host, token, json_output=json_output)


def _login_device(
    cfg: CLIConfig,
    open_browser: bool,
    json_output: bool = False,
    scopes: Optional[list[str]] = None,
) -> None:
    """Drive the device-flow start → approve-in-browser → poll loop."""
    with PictureMEClient(CLIConfig(host=cfg.host)) as client:
        # 1. Start the session.
        try:
            request_body: dict[str, object] = {"client_name": "pictureme-cli"}
            if scopes:
                request_body["scopes"] = list(scopes)
                if set(scopes).issubset({"ale-config:read", "ale-config:write"}):
                    request_body["credential_class"] = "ale_operator"
            start = client.post(
                "/api/v3/public/oauth/device/code",
                json=request_body,
            )
        except APIError as exc:
            if json_output:
                emit_json_line(
                    {
                        "event": "authorization_failed",
                        "error": (
                            "network_error"
                            if exc.status_code is None
                            else "server_error"
                        ),
                    }
                )
                raise typer.Exit(code=1)
            raise handle_api_error(exc)

        try:
            device_code = _required_string(start, "device_code")
            user_code = _required_string(start, "user_code")
            verification_uri = _required_string(start, "verification_uri")
            verification_complete = (
                start.get("verification_uri_complete") or verification_uri
            )
            if not isinstance(verification_complete, str):
                raise ValueError("verification_uri_complete must be a string")
            interval = int(start.get("interval", 3))
            expires_in = int(start.get("expires_in", 600))
            if interval < 1 or expires_in < 1:
                raise ValueError("interval and expires_in must be positive")
        except (AttributeError, KeyError, TypeError, ValueError):
            if json_output:
                emit_json_line(
                    {
                        "event": "authorization_failed",
                        "error": "invalid_device_response",
                    }
                )
            else:
                err_console.print(
                    "[red]The server returned an invalid device-login response.[/red]"
                )
            raise typer.Exit(code=1)
        if not _is_safe_browser_url(verification_uri):
            raise handle_api_error(
                APIError("server returned an unsafe verification_uri")
            )
        if not _is_safe_browser_url(verification_complete) or not _same_origin(
            verification_uri, verification_complete
        ):
            verification_complete = verification_uri
        deadline = time.monotonic() + expires_in

        # 2. Surface the code to the user and open the browser.
        if json_output:
            emit_json_line(
                {
                    "event": "authorization_required",
                    "verification_uri": verification_uri,
                    "verification_uri_complete": verification_complete,
                    "user_code": user_code,
                    "expires_in": expires_in,
                    "interval": interval,
                }
            )
        else:
            console.print()
            console.print("[bold]Approve this device in your browser.[/bold]")
            console.print(f"  URL : [cyan]{verification_uri}[/cyan]")
            console.print(f"  Code: [bold magenta]{user_code}[/bold magenta]")
            console.print()
        if open_browser:
            try:
                webbrowser.open(verification_complete)
                if not json_output:
                    console.print("[dim]Opening browser…[/dim]")
            except Exception:
                pass

        # 3. Poll until approved / denied / expired.
        if not json_output:
            console.print("[dim]Waiting for approval…[/dim]")
        while time.monotonic() < deadline:
            time.sleep(interval)
            try:
                resp = client.post(
                    "/api/v3/public/oauth/device/token",
                    json={
                        "device_code": device_code,
                        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    },
                )
            except APIError as exc:
                # The backend reports pending/denied/expired as 4xx with a
                # readable error string in `error` — that goes through our
                # APIError wrapper as exc.payload.
                if exc.status_code is None:
                    if json_output:
                        emit_json_line(
                            {"event": "poll_retry", "error": "network_error"}
                        )
                    else:
                        err_console.print(
                            "[yellow]Network interrupted; retrying device login…[/yellow]"
                        )
                    continue
                payload = exc.payload if isinstance(exc.payload, dict) else {}
                err = str(payload.get("error") or exc)
                if err == "authorization_pending":
                    continue
                if err == "slow_down":
                    interval = min(interval + 5, 30)
                    continue
                if err == "access_denied":
                    if json_output:
                        emit_json_line({"event": "authorization_failed", "error": err})
                    else:
                        err_console.print("[red]Access denied in browser.[/red]")
                    raise typer.Exit(code=2)
                if err == "expired_token":
                    if json_output:
                        emit_json_line({"event": "authorization_failed", "error": err})
                    else:
                        err_console.print(
                            "[red]The code expired before approval. Run `pictureme auth login` again.[/red]"
                        )
                    raise typer.Exit(code=2)
                if json_output:
                    emit_json_line(
                        {
                            "event": "authorization_failed",
                            "error": (
                                "invalid_grant"
                                if err == "invalid_grant"
                                else "server_error"
                            ),
                        }
                    )
                    raise typer.Exit(code=1)
                raise handle_api_error(exc)

            token = resp.get("access_token") if isinstance(resp, dict) else None
            if not isinstance(token, str) or not token:
                if json_output:
                    emit_json_line(
                        {
                            "event": "authorization_failed",
                            "error": "invalid_token_response",
                        }
                    )
                else:
                    err_console.print(
                        "[red]The server returned an invalid token response.[/red]"
                    )
                raise typer.Exit(code=1)
            _persist_token(cfg.host, token, json_output=json_output)
            return

        if json_output:
            emit_json_line({"event": "authorization_failed", "error": "expired_token"})
        else:
            err_console.print("[red]Timed out waiting for approval.[/red]")
        raise typer.Exit(code=2)


def _is_safe_browser_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname or ""
    except (TypeError, ValueError):
        return False
    if parsed.username or parsed.password or not hostname:
        return False
    if parsed.scheme.lower() == "https":
        return True
    if parsed.scheme.lower() != "http":
        return False
    if hostname.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _required_string(payload: object, key: str) -> str:
    if not isinstance(payload, dict):
        raise TypeError("device response must be an object")
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return value


def _same_origin(left: str, right: str) -> bool:
    try:
        a = urlsplit(left)
        b = urlsplit(right)
        a_port = a.port or (443 if a.scheme.lower() == "https" else 80)
        b_port = b.port or (443 if b.scheme.lower() == "https" else 80)
    except ValueError:
        return False
    return (
        a.scheme.lower(),
        (a.hostname or "").lower(),
        a_port,
    ) == (
        b.scheme.lower(),
        (b.hostname or "").lower(),
        b_port,
    )


def _persist_token(host: str, token: str, json_output: bool = False) -> None:
    extras = load_config().extras
    new_cfg = CLIConfig(host=host, api_key=token, extras=extras)
    path = save_config(new_cfg)
    if json_output:
        emit_json_line(
            {
                "event": "authenticated",
                "host": new_cfg.host,
                "config_file": str(path),
            }
        )
    else:
        console.print(f"[green]✓[/green] Logged in. Token stored at [dim]{path}[/dim]")


@app.command()
def token(
    ctx: typer.Context,
    show: bool = typer.Option(
        False, "--show", help="Print the raw bearer token. Default redacts it."
    ),
) -> None:
    """Show the currently stored token (redacted by default)."""
    cfg = get_config(ctx)
    if json_mode(ctx) and show:
        emit_json(
            {
                "error": "unsafe_flag_combination",
                "message": "--show cannot be used with --json",
            }
        )
        raise typer.Exit(code=2)

    if not cfg.api_key:
        if json_mode(ctx):
            emit_json(
                {
                    "error": "not_authenticated",
                    "host": cfg.host,
                    "has_token": False,
                }
            )
        else:
            err_console.print("[yellow]No token stored.[/yellow]")
        raise typer.Exit(code=2)

    if json_mode(ctx):
        emit_json(
            {
                "host": cfg.host,
                "has_token": True,
                "redacted": True,
            }
        )
        return

    if show:
        console.print(cfg.api_key)
        return

    # Security: never print any part of the stored bearer token — not even a
    # prefix. A truncated prefix is still credential material (key-id oracle +
    # partial disclosure). `--show` is the only path that reveals it.
    console.print(f"host: {cfg.host}")
    console.print(f"token: {'*' * 8}  (use --show to reveal)")


@app.command()
def status(ctx: typer.Context) -> None:
    """Probe the backend and report whether the stored token works."""
    cfg = get_config(ctx)
    info: dict = {
        "host": cfg.host,
        "config_file": str(config_path()),
        "has_token": bool(cfg.api_key),
    }

    try:
        with PictureMEClient(CLIConfig(host=cfg.host)) as client:
            health = client.get("/api/v3/public/health")
            info["backend_status"] = health.get("status", "unknown")
            info["backend_version"] = health.get("version", "?")
    except APIError as exc:
        info["backend_status"] = "unreachable"
        info["backend_error"] = str(exc)

    if cfg.api_key:
        try:
            with PictureMEClient(cfg) as client:
                # Device login stores a pmk_ API key. Use an API-key-supported
                # endpoint for the auth probe; /shared/users/me is intentionally
                # web-session-only now that scoped API keys fail closed.
                balance = client.get("/api/v3/shared/tokens/balance")
                info["authenticated_as"] = "ok"
                if isinstance(balance, dict):
                    info["token_balance"] = balance.get("balance")
        except APIError as exc:
            if exc.status_code in (401, 403):
                info["auth_error"] = "authentication_failed"
            elif exc.status_code is None:
                info["auth_error"] = "network_error"
            else:
                info["auth_error"] = "server_error"
            info["authenticated_as"] = None

    if json_mode(ctx):
        emit_json(info)
        return
    for k, v in info.items():
        console.print(f"[bold]{k}[/bold]: {v}")


@app.command()
def logout(ctx: typer.Context) -> None:
    """Forget the stored token. The remote session/api_key is not invalidated."""
    cfg = get_config(ctx)
    cleared = CLIConfig(host=cfg.host, api_key=None, extras=cfg.extras)
    path = save_config(cleared)
    if json_mode(ctx):
        emit_json(
            {"status": "logged_out", "host": cleared.host, "config_file": str(path)}
        )
    else:
        console.print(
            "[green]✓[/green] Local token cleared. "
            "(Use the web Settings page to revoke the key remotely.)"
        )
