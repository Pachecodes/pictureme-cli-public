"""Helpers shared by command modules: context unwrap + output formatting."""

from __future__ import annotations

import json
import sys
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from ..client import APIError, PictureMEClient
from ..config import CLIConfig

console = Console()
err_console = Console(stderr=True)


def get_config(ctx: typer.Context) -> CLIConfig:
    return ctx.obj["config"]


def json_mode(ctx: typer.Context) -> bool:
    return bool(ctx.obj.get("json"))


def require_auth(ctx: typer.Context) -> CLIConfig:
    cfg = get_config(ctx)
    if not cfg.api_key:
        err_console.print(
            "[red]Not authenticated.[/red] Run [bold]pictureme auth login[/bold] or "
            "set the PICTUREME_API_KEY environment variable."
        )
        raise typer.Exit(code=2)
    return cfg


def make_client(ctx: typer.Context, require_token: bool = True) -> PictureMEClient:
    cfg = require_auth(ctx) if require_token else get_config(ctx)
    return PictureMEClient(cfg)


def emit_json(data: Any) -> None:
    json.dump(data, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")


def emit_json_line(data: Any) -> None:
    """Emit and flush one JSON Lines event for interactive streaming commands."""
    json.dump(data, sys.stdout, separators=(",", ":"), default=str)
    sys.stdout.write("\n")
    sys.stdout.flush()


def emit_table(title: str, columns: list[str], rows: list[list[str]]) -> None:
    table = Table(title=title, header_style="bold")
    for col in columns:
        table.add_column(col)
    for row in rows:
        table.add_row(*[_render_cell(c) for c in row])
    console.print(table)


def _render_cell(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value)
    return str(value)


def handle_api_error(exc: APIError) -> typer.Exit:
    err_console.print(f"[red]API error[/red]: {exc}")
    if exc.status_code:
        err_console.print(f"  status: {exc.status_code}")
    return typer.Exit(code=1)
