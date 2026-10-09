"""Manage PictureME personal API keys for CLI/MCP/agent workflows."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import typer

from ..client import APIError
from ._common import (
    console,
    emit_json,
    emit_table,
    handle_api_error,
    json_mode,
    make_client,
)

app = typer.Typer(no_args_is_help=True)


def _parse_expires_at(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    raw = value.strip()
    try:
        # Accept both RFC3339 `Z` and Python's `+00:00` form, then send an ISO
        # timestamp back to Go's time.Time parser.
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise typer.BadParameter("expires-at must be an ISO/RFC3339 timestamp, e.g. 2026-06-30T00:00:00Z") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


@app.command("create")
def create(
    ctx: typer.Context,
    name: str = typer.Option("pictureme-cli", "--name", "-n", help="Human label for this key."),
    scope: Optional[list[str]] = typer.Option(
        None,
        "--scope",
        "-s",
        help="Allowlisted scope. Repeatable. Defaults to the standard agent scopes.",
    ),
    expires_at: Optional[str] = typer.Option(
        None,
        "--expires-at",
        help="Optional ISO/RFC3339 expiration timestamp, e.g. 2026-06-30T00:00:00Z.",
    ),
) -> None:
    """Create an API key and print its plaintext token exactly once."""
    client = make_client(ctx)
    try:
        body: dict[str, object] = {"name": name}
        if scope:
            body["scopes"] = scope
        parsed_expiry = _parse_expires_at(expires_at)
        if parsed_expiry:
            body["expires_at"] = parsed_expiry
        try:
            payload = client.post("/api/v3/shared/api-keys", json=body)
        except APIError as exc:
            raise handle_api_error(exc)
    finally:
        client.close()

    if json_mode(ctx):
        emit_json(payload)
        return

    api_key = payload.get("api_key") or {}
    console.print("[green]✓[/green] API key created. Copy the token now; it will not be shown again.")
    console.print(f"id: {api_key.get('id')}")
    console.print(f"prefix: {api_key.get('key_prefix')}")
    console.print(f"scopes: {', '.join(api_key.get('scopes') or [])}")
    console.print(f"token: [bold]{payload.get('token')}[/bold]")


@app.command("list")
def list_keys(ctx: typer.Context) -> None:
    """List active API keys for the authenticated user."""
    client = make_client(ctx)
    try:
        try:
            payload = client.get("/api/v3/shared/api-keys")
        except APIError as exc:
            raise handle_api_error(exc)
    finally:
        client.close()

    keys = payload.get("api_keys") or []
    if json_mode(ctx):
        emit_json({"api_keys": keys})
        return

    emit_table(
        f"PictureME API keys ({len(keys)})",
        ["id", "name", "prefix", "scopes", "origin", "last used", "expires"],
        [
            [
                str(k.get("id") or ""),
                str(k.get("name") or ""),
                str(k.get("key_prefix") or ""),
                ", ".join(k.get("scopes") or []),
                str(k.get("origin") or ""),
                str(k.get("last_used_at") or ""),
                str(k.get("expires_at") or ""),
            ]
            for k in keys
        ],
    )


@app.command("revoke")
def revoke(
    ctx: typer.Context,
    key_id: str = typer.Argument(..., help="API key id to revoke."),
) -> None:
    """Revoke one API key by id."""
    client = make_client(ctx)
    try:
        try:
            payload = client.delete(f"/api/v3/shared/api-keys/{key_id}")
        except APIError as exc:
            raise handle_api_error(exc)
    finally:
        client.close()

    if json_mode(ctx):
        emit_json(payload)
        return
    console.print(f"[green]✓[/green] Revoked API key {payload.get('id') or key_id}")
