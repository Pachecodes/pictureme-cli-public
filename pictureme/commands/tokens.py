"""tokens balance / stats / history — token & cost visibility.

Maps to the v3 shared token surface:

    GET /api/v3/shared/tokens/balance
    GET /api/v3/shared/tokens/stats
    GET /api/v3/shared/tokens/transactions

These endpoints require the tokens:read scope on API keys; web-session JWTs
pass automatically.
"""

from __future__ import annotations

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


@app.command()
def balance(ctx: typer.Context) -> None:
    """Show the current token balance."""
    client = make_client(ctx)
    try:
        payload = client.get("/api/v3/shared/tokens/balance")
    except APIError as exc:
        raise handle_api_error(exc)
    finally:
        client.close()

    if json_mode(ctx):
        emit_json(payload)
        return
    console.print(f"[bold]{payload.get('balance', 0)}[/bold] tokens")


@app.command()
def stats(ctx: typer.Context) -> None:
    """Show usage statistics (monthly usage, forecast, totals)."""
    client = make_client(ctx)
    try:
        payload = client.get("/api/v3/shared/tokens/stats")
    except APIError as exc:
        raise handle_api_error(exc)
    finally:
        client.close()

    if json_mode(ctx):
        emit_json(payload)
        return
    rows = [
        ["balance", payload.get("balance")],
        ["used this month", payload.get("tokens_used_month")],
        ["avg daily usage", payload.get("avg_daily_usage")],
        ["forecast (days left)", payload.get("forecast_days")],
        ["total purchased", payload.get("total_purchased")],
        ["total used", payload.get("total_used")],
        ["plan tokens", payload.get("plan_tokens")],
    ]
    emit_table("Token stats", ["metric", "value"], rows)


@app.command()
def history(
    ctx: typer.Context,
    limit: int = typer.Option(20, "--limit", "-n", help="How many transactions to show."),
) -> None:
    """List recent token transactions (charges, refunds, purchases)."""
    client = make_client(ctx)
    try:
        payload = client.get("/api/v3/shared/tokens/transactions", params={"limit": limit})
    except APIError as exc:
        raise handle_api_error(exc)
    finally:
        client.close()

    transactions = payload if isinstance(payload, list) else payload.get("transactions", [])
    if json_mode(ctx):
        emit_json({"transactions": transactions})
        return

    rows = [
        [
            t.get("created_at"),
            t.get("transaction_type"),
            t.get("amount"),
            t.get("balance_after"),
            t.get("description"),
        ]
        for t in transactions
    ]
    emit_table(
        f"Token transactions ({len(rows)})",
        ["when", "type", "amount", "balance after", "description"],
        rows,
    )
