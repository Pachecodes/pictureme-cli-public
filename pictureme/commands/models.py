"""model list / get — registry inspection."""

from __future__ import annotations

from typing import Optional

import typer

from ..client import APIError, PictureMEClient
from ..config import CLIConfig
from ._common import (
    emit_json,
    emit_table,
    get_config,
    handle_api_error,
    json_mode,
)

app = typer.Typer(no_args_is_help=True)


@app.command("list")
def list_models(
    ctx: typer.Context,
    image_only: bool = typer.Option(False, "--image", help="Show only image models."),
    video_only: bool = typer.Option(False, "--video", help="Show only video models."),
    provider: Optional[str] = typer.Option(None, "--provider", help="Filter by provider (fal, xai, ...)."),
) -> None:
    """List active models from the public registry."""
    cfg = get_config(ctx)
    try:
        with PictureMEClient(CLIConfig(host=cfg.host)) as client:
            payload = client.get("/api/v3/public/models")
    except APIError as exc:
        raise handle_api_error(exc)

    models = payload.get("models", [])
    if image_only:
        models = [m for m in models if m.get("model_type") == "image"]
    if video_only:
        models = [m for m in models if m.get("model_type") == "video"]
    if provider:
        models = [m for m in models if m.get("provider") == provider]

    if json_mode(ctx):
        emit_json({"models": models})
        return

    rows = [
        [
            m.get("model_id"),
            m.get("display_name"),
            m.get("model_type"),
            m.get("provider"),
            m.get("default_cost"),
            ", ".join(m.get("capabilities") or []),
            m.get("poll_profile"),
        ]
        for m in models
    ]
    emit_table(
        f"PictureME models ({len(rows)})",
        ["id", "name", "type", "provider", "cost", "capabilities", "poll"],
        rows,
    )


@app.command()
def get(
    ctx: typer.Context,
    model_id: str = typer.Argument(..., help="Model id (short form, e.g. nano-banana)."),
) -> None:
    """Fetch a single model's full registry entry."""
    cfg = get_config(ctx)
    try:
        with PictureMEClient(CLIConfig(host=cfg.host)) as client:
            data = client.get(f"/api/v3/public/models/{model_id}")
    except APIError as exc:
        raise handle_api_error(exc)

    emit_json(data)
