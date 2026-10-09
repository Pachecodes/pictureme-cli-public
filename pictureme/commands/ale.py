"""Typed, least-privilege ALE operator commands.

This surface is intentionally separate from ``admin api``. Scoped
``ale_operator`` device credentials can only call ``/api/v3/operator/ale``;
the generic admin escape hatch remains limited to web-session-only admin
routes.

Every mutation requires ``--yes`` and performs a GET readback. JSON mode emits
that exact saved/effective readback object, not the mutation acknowledgement.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

import typer

from ..client import APIError, PictureMEClient
from ._common import emit_json, emit_table, err_console, handle_api_error, json_mode, make_client

app = typer.Typer(no_args_is_help=True, help="Operate ALE runtime and tier policy configuration.")
runtime_app = typer.Typer(no_args_is_help=True, help="Read or patch global ALE runtime settings.")
tier_app = typer.Typer(no_args_is_help=True, help="List, read, set, delete, or resolve ALE tier policies.")
app.add_typer(runtime_app, name="runtime")
app.add_typer(tier_app, name="tier")

ROOT = "/api/v3/operator/ale"
TIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _fail(message: str) -> typer.Exit:
    err_console.print(f"[red]{message}[/red]")
    return typer.Exit(code=2)


def _require_yes(yes: bool, action: str) -> None:
    if not yes:
        raise _fail(f"{action} changes ALE configuration. Re-run with --yes to confirm.")


def _load_object(file: str) -> dict[str, Any]:
    if file == "-":
        raw = sys.stdin.read()
        source = "stdin"
    else:
        path = Path(file)
        if not path.is_file():
            raise _fail(f"file not found: {file}")
        raw = path.read_text()
        source = file
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise _fail(f"{source} is not valid JSON: {exc}")
    if not isinstance(payload, dict):
        raise _fail(f"{source} must contain a JSON object, got {type(payload).__name__}")
    return payload


def _validate_routing_pairs(payload: dict[str, Any]) -> None:
    """Routing changes are atomic pairs; other policy fields remain sparse.

    Fail locally before sending a write that the Go API rejects. Explicit
    blanks clear both fields of a pair; null means no change to a field.
    """
    for provider_key, model_key in (("provider", "model"), ("fallback_provider", "fallback_model")):
        provider, model = payload.get(provider_key), payload.get(model_key)
        if provider is None and model is None:
            continue
        if not isinstance(provider, str) or not isinstance(model, str):
            raise _fail(f"{provider_key} and {model_key} must be supplied together as strings.")
        if bool(provider.strip()) != bool(model.strip()):
            raise _fail(f"{provider_key} and {model_key} must both be set or both be cleared.")


def _verify_saved(requested: dict[str, Any], saved: Any, *, ignore: frozenset[str] = frozenset()) -> None:
    """A successful PATCH/PUT response alone is not evidence that it took effect.

    Never print requested values: system instructions can be sensitive. A mismatch
    may mean the write committed but another edit won the race; do not auto-retry.
    """
    if not isinstance(saved, dict):
        raise _fail("ALE write readback was not an object; inspect the target before retrying.")
    mismatched = []
    for key, value in requested.items():
        if key in ignore or value is None:
            continue
        if isinstance(value, str):
            value = value.strip()
            if key in {"provider", "fallback_provider", "reasoning_profile"}:
                value = value.lower()
        if saved.get(key) != value:
            mismatched.append(key)
    if mismatched:
        raise _fail("ALE write readback differs for " + ", ".join(sorted(mismatched))
                    + "; inspect the target before retrying.")


def _tier(value: str) -> str:
    tier = value.strip()
    if not TIER_RE.fullmatch(tier):
        raise _fail("tier must contain only letters, numbers, '.', '_' or '-'.")
    return tier


def _request(client: PictureMEClient, method: str, path: str, **kwargs: Any) -> Any:
    try:
        _, payload = client.request_with_status(method, path, **kwargs)
        return payload
    except APIError as exc:
        raise handle_api_error(exc)


def _emit(ctx: typer.Context, payload: Any, title: str) -> None:
    if json_mode(ctx):
        emit_json(payload)
    elif isinstance(payload, dict):
        emit_table(title, ["field", "value"], [[key, value] for key, value in payload.items()])
    else:
        emit_json(payload)


@runtime_app.command("get")
def runtime_get(ctx: typer.Context) -> None:
    """Read the global ALE runtime settings."""
    client = make_client(ctx)
    try:
        payload = _request(client, "GET", f"{ROOT}/runtime")
    finally:
        client.close()
    _emit(ctx, payload, "ALE runtime")


@runtime_app.command("patch")
def runtime_patch(
    ctx: typer.Context,
    file: str = typer.Option(..., "--file", "-f", help="JSON object file, or '-' for stdin."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Confirm the runtime mutation."),
) -> None:
    """Patch runtime settings, then GET and print the exact saved state."""
    _require_yes(yes, "Runtime patch")
    body = _load_object(file)
    _validate_routing_pairs(body)
    client = make_client(ctx)
    try:
        _request(client, "PATCH", f"{ROOT}/runtime", json=body)
        saved = _request(client, "GET", f"{ROOT}/runtime")
        _verify_saved(body, saved)
    finally:
        client.close()
    _emit(ctx, saved, "Saved ALE runtime")


@tier_app.command("list")
def tier_list(ctx: typer.Context) -> None:
    """List ALE tier policies."""
    client = make_client(ctx)
    try:
        payload = _request(client, "GET", f"{ROOT}/tiers")
    finally:
        client.close()
    _emit(ctx, payload, "ALE tier policies")


@tier_app.command("get")
def tier_get(ctx: typer.Context, tier: str = typer.Argument(..., help="Tier code.")) -> None:
    """Read one saved tier policy."""
    tier = _tier(tier)
    client = make_client(ctx)
    try:
        payload = _request(client, "GET", f"{ROOT}/tiers/{tier}")
    finally:
        client.close()
    _emit(ctx, payload, f"ALE tier policy: {tier}")


@tier_app.command("effective")
def tier_effective(ctx: typer.Context, tier: str = typer.Argument(..., help="Tier code.")) -> None:
    """Read the effective policy for a tier after global fallback is applied."""
    tier = _tier(tier)
    client = make_client(ctx)
    try:
        payload = _request(client, "GET", f"{ROOT}/effective/{tier}")
    finally:
        client.close()
    _emit(ctx, payload, f"Effective ALE policy: {tier}")


@tier_app.command("set")
def tier_set(
    ctx: typer.Context,
    tier: str = typer.Argument(..., help="Tier code."),
    file: str = typer.Option(..., "--file", "-f", help="JSON object file, or '-' for stdin."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Confirm the tier mutation."),
) -> None:
    """Create/update a tier policy, then GET and print the exact saved policy."""
    _require_yes(yes, "Tier policy set")
    tier = _tier(tier)
    body = _load_object(file)
    _validate_routing_pairs(body)
    client = make_client(ctx)
    try:
        _request(client, "PUT", f"{ROOT}/tiers/{tier}", json=body)
        saved = _request(client, "GET", f"{ROOT}/tiers/{tier}")
        _verify_saved(body, saved, ignore=frozenset({"tier_code"}))
    finally:
        client.close()
    _emit(ctx, saved, f"Saved ALE tier policy: {tier}")


@tier_app.command("delete")
def tier_delete(
    ctx: typer.Context,
    tier: str = typer.Argument(..., help="Tier code."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Confirm the tier deletion."),
) -> None:
    """Delete a tier override, then print its exact effective fallback policy."""
    _require_yes(yes, "Tier policy delete")
    tier = _tier(tier)
    client = make_client(ctx)
    try:
        _request(client, "DELETE", f"{ROOT}/tiers/{tier}")
        effective = _request(client, "GET", f"{ROOT}/effective/{tier}")
        if not isinstance(effective, dict) or effective.get("policy_source") != "global":
            raise _fail("ALE tier delete readback did not resolve to global; inspect the target before retrying.")
    finally:
        client.close()
    _emit(ctx, effective, f"Effective ALE policy after delete: {tier}")
