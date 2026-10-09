"""Top-level Typer app wiring the command groups together.

Global flags (--host, --api-key, --json) resolve into a context object that
each subcommand reads from. We use Typer's callback so the flags work
before any subcommand is parsed.
"""

from __future__ import annotations

import json
from typing import Optional

import typer

from . import __version__
from .commands import ale as ale_cmd
from .commands import admin as admin_cmd
from .commands import api_key as api_key_cmd
from .commands import auth as auth_cmd
from .commands import booth as booth_cmd
from .commands import generate as generate_cmd
from .commands import models as models_cmd
from .commands import tokens as tokens_cmd
from .commands import upload as upload_cmd
from .commands import workflow as workflow_cmd
from .config import resolve_runtime

app = typer.Typer(
    name="pictureme",
    help="Command-line client for the PictureME v3 generation API.",
    add_completion=False,
    no_args_is_help=True,
)

app.add_typer(auth_cmd.app, name="auth", help="Authenticate and manage credentials.")
app.add_typer(
    api_key_cmd.app, name="api-key", help="Create, list, and revoke agent API keys."
)
app.add_typer(models_cmd.app, name="model", help="Inspect the model registry.")
app.add_typer(upload_cmd.app, name="upload", help="Upload media to the backend.")
app.add_typer(
    generate_cmd.app, name="generate", help="Create and track generation jobs."
)
app.add_typer(
    tokens_cmd.app,
    name="tokens",
    help="Inspect token balance, stats, and transaction history.",
)
app.add_typer(
    workflow_cmd.app, name="workflow", help="Run pipelines (stub — needs Phase H)."
)
app.add_typer(
    booth_cmd.app, name="booth", help="Inspect photo booths: list, get, analytics."
)
app.add_typer(
    admin_cmd.app,
    name="admin",
    help="Super Admin campaign & content control (templates, featured, weekly prompt, trending, hero).",
)
admin_cmd.app.add_typer(
    ale_cmd.app,
    name="ale",
    help="Scoped ALE operator runtime and tier-policy control.",
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit()


@app.callback()
def main(
    ctx: typer.Context,
    host: Optional[str] = typer.Option(None, "--host", "-H", help="Backend base URL."),
    api_key: Optional[str] = typer.Option(
        None, "--api-key", help="Override the stored bearer token."
    ),
    json_output: bool = typer.Option(
        False, "--json", help="Emit machine-readable JSON instead of tables."
    ),
    version: bool = typer.Option(
        None,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Show version and exit.",
    ),
) -> None:
    try:
        cfg = resolve_runtime(cli_host=host, cli_api_key=api_key)
    except ValueError as exc:
        if json_output:
            typer.echo(json.dumps({"error": "invalid_host", "message": str(exc)}))
            raise typer.Exit(code=2)
        raise typer.BadParameter(str(exc), param_hint="--host") from exc
    ctx.obj = {
        "config": cfg,
        "json": json_output,
    }


if __name__ == "__main__":
    app()
