"""booth list / get / analytics — read-only booth visibility for operators.

Booths are events with is_booth=true. The backend exposes two surfaces:

    GET /api/v3/business/booths/         (business role required)
    GET /api/booths/                     (any authenticated owner — creators)
    GET /api/analytics/creator-booths    (creator earnings/usage dashboard)

We try the v3 business surface first and transparently fall back to the
owner-scoped legacy route for creator accounts (the v3 creator group only
accepts web sessions, not pmk_ API keys, so the legacy route is the only
API-key-friendly read for creators today).

Booth creation stays in the web apps: it needs templates, theming, and
monetization choices that don't map cleanly onto flags yet.
"""

from __future__ import annotations

import typer

from ..client import APIError, PictureMEClient
from ._common import (
    console,
    emit_json,
    emit_table,
    err_console,
    handle_api_error,
    json_mode,
    make_client,
)

app = typer.Typer(no_args_is_help=True)


def _get_with_fallback(client: PictureMEClient, v3_path: str, legacy_path: str, **kwargs):
    """Prefer the v3 business surface; fall back to the legacy owner route
    when the account doesn't carry a business role (401/403)."""
    try:
        return client.get(v3_path, **kwargs)
    except APIError as exc:
        if exc.status_code in (401, 403, 404):
            return client.get(legacy_path, **kwargs)
        raise


def _monetization_summary(booth: dict) -> str:
    mon = booth.get("monetization") or {}
    if not isinstance(mon, dict):
        return "—"
    mode = mon.get("sale_mode") or mon.get("type") or "free"
    if mode in ("money", "revenue_share"):
        return f"money ${mon.get('fiat_price', '?')}/use"
    if mode == "tokens":
        return f"tokens {mon.get('token_price', '?')}/use"
    return "free"


@app.command("list")
def list_booths(ctx: typer.Context) -> None:
    """List your booths (title, slug, status, monetization)."""
    client = make_client(ctx)
    try:
        payload = _get_with_fallback(client, "/api/v3/business/booths/", "/api/booths/")
    except APIError as exc:
        raise handle_api_error(exc)
    finally:
        client.close()

    booths = payload if isinstance(payload, list) else payload.get("booths", payload.get("events", []))
    if json_mode(ctx):
        emit_json({"booths": booths})
        return

    rows = [
        [
            b.get("title") or b.get("name"),
            b.get("slug"),
            b.get("status"),
            _monetization_summary(b),
            b.get("_id") or b.get("id"),
        ]
        for b in booths
    ]
    emit_table(f"Booths ({len(rows)})", ["title", "slug", "status", "monetization", "id"], rows)


@app.command("get")
def get(ctx: typer.Context, slug: str = typer.Argument(..., help="Booth slug.")) -> None:
    """Fetch one booth by slug (full config as JSON)."""
    client = make_client(ctx)
    try:
        payload = _get_with_fallback(client, f"/api/v3/business/booths/{slug}", f"/api/booths/{slug}")
    except APIError as exc:
        raise handle_api_error(exc)
    finally:
        client.close()

    if json_mode(ctx):
        emit_json(payload)
        return

    rows = [
        ["title", payload.get("title") or payload.get("name")],
        ["slug", payload.get("slug")],
        ["status", payload.get("status")],
        ["monetization", _monetization_summary(payload)],
        ["id", payload.get("_id") or payload.get("id")],
    ]
    emit_table("Booth", ["field", "value"], rows)
    console.print("[dim]Tip: --json shows the full config (templates, rules, theme).[/dim]")


@app.command("analytics")
def analytics(
    ctx: typer.Context,
    days: int = typer.Option(30, "--days", "-d", help="Lookback window in days."),
) -> None:
    """Creator booth analytics: sessions, tokens spent, earnings."""
    client = make_client(ctx)
    try:
        payload = client.get("/api/analytics/creator-booths", params={"days": days})
    except APIError as exc:
        raise handle_api_error(exc)
    finally:
        client.close()

    if json_mode(ctx):
        emit_json(payload)
        return
    if isinstance(payload, dict):
        rows = [[k, v] for k, v in payload.items() if not isinstance(v, (dict, list))]
        emit_table(f"Booth analytics (last {days}d)", ["metric", "value"], rows)
        for key, value in payload.items():
            if isinstance(value, list) and value and isinstance(value[0], dict):
                cols = list(value[0].keys())
                emit_table(key, cols, [[item.get(c) for c in cols] for item in value])
    else:
        emit_json(payload)


@app.command("create")
def create(ctx: typer.Context) -> None:
    """(Not available) Booth creation stays in the Creator/Business web apps."""
    err_console.print(
        "[yellow]Booth creation isn't supported from the CLI.[/yellow]\n"
        "It requires style templates, theming, and monetization setup — use the "
        "Creator app (Studio → Booths → New) or the Business app instead."
    )
    raise typer.Exit(code=2)
