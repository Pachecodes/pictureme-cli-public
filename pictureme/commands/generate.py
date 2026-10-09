"""generate create / get / wait / list / cost.

This is the main agent surface — everything else is supporting cast. The
flag set is chosen to match the JSON shape of POST /api/v3/shared/generate/jobs
so a 1:1 mapping is obvious to anyone reading code.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Optional

import typer

from ..client import APIError, PictureMEClient
from ..config import CLIConfig
from ._common import (
    console,
    emit_json,
    err_console,
    get_config,
    handle_api_error,
    json_mode,
    make_client,
)

app = typer.Typer(no_args_is_help=True)


def _ensure_remote_url(client: PictureMEClient, value: str) -> str:
    """If `value` is a local file path, upload it and return the resulting URL.

    Otherwise (already https://... or http://...) return it unchanged. This
    is what makes `--image ./foo.jpg` ergonomic.
    """
    if value.startswith(("http://", "https://", "data:")):
        return value
    p = Path(value)
    if not p.exists():
        raise typer.BadParameter(f"file not found: {value}")
    with p.open("rb") as fh:
        files = {"file": (p.name, fh, "application/octet-stream")}
        payload = client.post("/api/v3/creator/generate/upload", files=files)
    url = (
        payload.get("url")
        or payload.get("file_url")
        or payload.get("location")
    )
    if not url:
        raise APIError(f"upload returned no URL: {payload}")
    return url


def _parse_seconds(s: str) -> int:
    s = s.strip().lower()
    if s.endswith("ms"):
        return max(1, int(float(s[:-2]) / 1000))
    if s.endswith("s"):
        return int(float(s[:-1]))
    if s.endswith("m"):
        return int(float(s[:-1]) * 60)
    if s.endswith("h"):
        return int(float(s[:-1]) * 3600)
    return int(float(s))


@app.command("create")
def create(
    ctx: typer.Context,
    model: str = typer.Argument(..., help="Model id from the registry, e.g. nano-banana."),
    prompt: str = typer.Option(..., "--prompt", "-p", help="Text prompt for the model."),
    image: list[str] = typer.Option(
        None, "--image", "-i", help="Path to a local image or a URL. Repeatable.",
    ),
    first_frame: Optional[str] = typer.Option(None, "--first-frame", help="Video first/start frame (path or URL)."),
    last_frame: Optional[str] = typer.Option(None, "--last-frame", help="Video last/end frame (path or URL)."),
    aspect_ratio: Optional[str] = typer.Option(None, "--aspect-ratio", help="e.g. 1:1, 16:9."),
    resolution: Optional[str] = typer.Option(None, "--resolution", help="Model-specific tag (0.5K, 1K, 2K, 4K, 720p, 1080p, ...)."),
    duration: Optional[str] = typer.Option(None, "--duration", help="Video duration in seconds, model-specific."),
    output_format: Optional[str] = typer.Option(None, "--output-format", help="jpeg, png, webp (model-dependent)."),
    seed: Optional[int] = typer.Option(None, "--seed", help="Deterministic seed when the model supports it."),
    num_images: Optional[int] = typer.Option(None, "--num-images", help="Number of outputs (tier-capped server-side)."),
    generate_audio: Optional[bool] = typer.Option(None, "--audio/--no-audio", help="Generate audio when supported."),
    visibility: Optional[str] = typer.Option(None, "--visibility", help="public or private."),
    parent_id: Optional[int] = typer.Option(None, "--parent-id", help="Source job id for remix attribution."),
    wait: bool = typer.Option(False, "--wait", help="Block until the job completes (polls)."),
    timeout: str = typer.Option("5m", "--timeout", help="Wait timeout when --wait is set."),
    interval: str = typer.Option("3s", "--interval", help="Poll interval when --wait is set."),
) -> None:
    """Create a generation job. Uploads local file paths automatically."""
    client = make_client(ctx)
    try:
        images: list[str] = []
        for v in image or []:
            images.append(_ensure_remote_url(client, v))

        media: dict[str, Any] = {}
        if images:
            media["images"] = images
        if first_frame:
            media["first_frame"] = _ensure_remote_url(client, first_frame)
        if last_frame:
            media["last_frame"] = _ensure_remote_url(client, last_frame)

        params: dict[str, Any] = {}
        if aspect_ratio is not None:
            params["aspect_ratio"] = aspect_ratio
        if resolution is not None:
            params["resolution"] = resolution
        if duration is not None:
            params["duration"] = duration
        if output_format is not None:
            params["output_format"] = output_format
        if seed is not None:
            params["seed"] = seed
        if num_images is not None:
            params["num_images"] = num_images
        if generate_audio is not None:
            params["generate_audio"] = generate_audio

        body: dict[str, Any] = {
            "model_id": model,
            "prompt": prompt,
            "media": media,
            "params": params,
        }
        if visibility is not None:
            body["visibility"] = visibility
        if parent_id is not None:
            body["parent_id"] = parent_id

        try:
            job = client.post("/api/v3/shared/generate/jobs", json=body)
        except APIError as exc:
            raise handle_api_error(exc)

        if not wait:
            _print_job(ctx, job)
            return

        job_id = job.get("job_id")
        if not job_id:
            err_console.print("[red]create response missing job_id[/red]")
            raise typer.Exit(code=1)

        final = _poll(client, int(job_id), timeout=timeout, interval=interval)
        _print_job(ctx, final)
    finally:
        client.close()


@app.command()
def get(
    ctx: typer.Context,
    job_id: int = typer.Argument(..., help="Job id returned by `generate create`."),
) -> None:
    """Fetch one job's current state."""
    client = make_client(ctx)
    try:
        try:
            job = client.get(f"/api/v3/shared/generate/jobs/{job_id}")
        except APIError as exc:
            raise handle_api_error(exc)
        _print_job(ctx, job)
    finally:
        client.close()


@app.command("list")
def list_jobs(
    ctx: typer.Context,
    kind: Optional[str] = typer.Option(None, "--kind", help="Filter by kind: image or video."),
    status: Optional[str] = typer.Option(None, "--status", help="Filter by status: processing, completed, failed, cancelled."),
) -> None:
    """List recent jobs for the authenticated user."""
    client = make_client(ctx)
    try:
        try:
            params: dict[str, Any] = {}
            if kind:
                params["kind"] = kind
            if status:
                params["status"] = status
            payload = client.get("/api/v3/shared/generate/jobs", params=params)
        except APIError as exc:
            raise handle_api_error(exc)
    finally:
        client.close()

    jobs = payload.get("jobs", [])
    if json_mode(ctx):
        emit_json({"jobs": jobs})
        return
    from ._common import emit_table

    rows = [
        [
            j.get("job_id"),
            j.get("status"),
            j.get("kind"),
            j.get("model_id"),
            (j.get("prompt") or "")[:60],
            len(j.get("outputs") or []),
        ]
        for j in jobs
    ]
    emit_table(
        f"PictureME jobs ({len(rows)})",
        ["id", "status", "kind", "model", "prompt", "#out"],
        rows,
    )


@app.command()
def wait(
    ctx: typer.Context,
    job_id: int = typer.Argument(...),
    timeout: str = typer.Option("5m", "--timeout"),
    interval: str = typer.Option("3s", "--interval"),
) -> None:
    """Block until the job reaches a terminal state."""
    client = make_client(ctx)
    try:
        final = _poll(client, job_id, timeout=timeout, interval=interval)
        _print_job(ctx, final)
    finally:
        client.close()


@app.command()
def cancel(
    ctx: typer.Context,
    job_id: int = typer.Argument(...),
) -> None:
    """Mark a job cancelled locally. Upstream provider may still complete it."""
    client = make_client(ctx)
    try:
        try:
            job = client.post(f"/api/v3/shared/generate/jobs/{job_id}/cancel")
        except APIError as exc:
            raise handle_api_error(exc)
        _print_job(ctx, job)
    finally:
        client.close()


@app.command()
def cost(
    ctx: typer.Context,
    model: str = typer.Argument(...),
    resolution: Optional[str] = typer.Option(None, "--resolution"),
    audio: Optional[bool] = typer.Option(None, "--audio/--no-audio"),
    duration: Optional[str] = typer.Option(None, "--duration"),
    num_images: int = typer.Option(1, "--num-images"),
) -> None:
    """Estimate token cost locally without submitting a generation.

    This nonbinding estimate uses public catalog fields, not an authoritative
    quote. Server-owned billing rules and final charges may differ.
    """
    cfg = get_config(ctx)
    try:
        with PictureMEClient(CLIConfig(host=cfg.host)) as client:
            m = client.get(f"/api/v3/public/models/{model}")
    except APIError as exc:
        raise handle_api_error(exc)

    base = int(m.get("default_cost", 0))
    rules = m.get("cost_rules") or {}
    extra = 0

    def _rule_for(param: str, value: Any) -> int:
        if value is None:
            return 0
        bucket = rules.get(param)
        if not isinstance(bucket, dict):
            return 0
        cell = bucket.get(str(value))
        if isinstance(cell, (int, float)):
            return int(cell)
        return 0

    extra += _rule_for("resolution", resolution)
    if audio is not None:
        extra += _rule_for("audio", audio)
    extra += _rule_for("duration", duration)

    per_call = base + extra
    total = per_call * max(1, num_images)
    payload = {
        "model_id": m.get("model_id"),
        "base_cost": base,
        "extra_cost": extra,
        "per_call_cost": per_call,
        "num_images": num_images,
        "total_cost": total,
        "applied_rules": {
            "resolution": resolution,
            "audio": audio,
            "duration": duration,
        },
    }
    if json_mode(ctx):
        emit_json(payload)
        return
    console.print(
        f"[bold]{payload['model_id']}[/bold]: "
        f"{base} (base) + {extra} (rules) = [bold]{per_call}[/bold] tokens × "
        f"{num_images} = [bold]{total}[/bold]"
    )


# ----- helpers -----

TERMINAL_STATUSES = {"completed", "failed", "cancelled"}


def _poll(client: PictureMEClient, job_id: int, timeout: str, interval: str) -> dict[str, Any]:
    deadline = time.monotonic() + _parse_seconds(timeout)
    step = max(1, _parse_seconds(interval))
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        try:
            last = client.get(f"/api/v3/shared/generate/jobs/{job_id}")
        except APIError as exc:
            raise handle_api_error(exc)
        if last.get("status") in TERMINAL_STATUSES:
            return last
        time.sleep(step)
    err_console.print(f"[yellow]timeout waiting for job {job_id}[/yellow]")
    return last


def _print_job(ctx: typer.Context, job: dict[str, Any]) -> None:
    if json_mode(ctx):
        emit_json(job)
        return
    console.print(f"[bold]job {job.get('job_id')}[/bold]  status={job.get('status')}  kind={job.get('kind')}")
    if job.get("model_id"):
        console.print(f"  model: {job.get('model_id')}")
    if job.get("prompt"):
        console.print(f"  prompt: {job.get('prompt')}")
    if job.get("error"):
        console.print(f"  [red]error[/red]: {job.get('error')}")
    for o in job.get("outputs") or []:
        console.print(f"  [green]{o.get('type')}[/green]: {o.get('url')}")
    if job.get("cost"):
        console.print(f"  cost: {job.get('cost')}")
