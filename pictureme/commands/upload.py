"""upload create — push a local file to the backend's R2 staging bucket.

Returns the URL the generation endpoint should receive. Lists are not
implemented because there is no backend endpoint for browsing past
uploads yet.
"""

from __future__ import annotations

from pathlib import Path

import typer

from ..client import APIError
from ._common import (
    emit_json,
    handle_api_error,
    json_mode,
    make_client,
    console,
)

app = typer.Typer(no_args_is_help=True)


@app.command()
def create(
    ctx: typer.Context,
    path: Path = typer.Argument(..., exists=True, file_okay=True, dir_okay=False, readable=True),
) -> None:
    """Upload `path` and print the resulting URL.

    Uses the same /api/v3/creator/generate/upload endpoint the web Creator
    posts to, so the CLI and the browser share storage rules.
    """
    client = make_client(ctx)
    try:
        with path.open("rb") as fh:
            files = {"file": (path.name, fh, "application/octet-stream")}
            payload = client.post("/api/v3/creator/generate/upload", files=files)
    except APIError as exc:
        raise handle_api_error(exc)
    finally:
        client.close()

    if json_mode(ctx):
        emit_json(payload)
        return
    url = payload.get("url") or payload.get("file_url") or payload.get("location") or payload
    console.print(url)
