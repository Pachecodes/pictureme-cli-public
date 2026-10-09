"""workflow run — stub until backend Phase H lands."""

from __future__ import annotations

import typer

from ._common import err_console

app = typer.Typer(no_args_is_help=True)


@app.command("run")
def run(
    ctx: typer.Context,
    workflow_id: str = typer.Argument(..., help="Workflow id or path to a workflow JSON."),
) -> None:
    err_console.print(
        "[yellow]workflow run is not yet implemented server-side.[/yellow]\n"
        "The backend GenerationService.ExecuteWorkflow returns "
        '"workflow engine not fully implemented yet" today (Phase H).'
    )
    raise typer.Exit(code=2)
