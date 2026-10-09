"""Super Admin campaign & content control.

These commands primarily target the legacy `/api/admin/*` surface.
Route availability, credential classes, roles and scopes are server-owned;
client paths and comments do not prove that API keys are accepted. Some
admin routes require a web session. Do not copy browser credentials or
bypass a server refusal. The separate `admin ale` surface uses scoped,
web-approved operator credentials; see its module and README.

The route map below describes client requests, not a guarantee about the
currently deployed backend's authorization or API compatibility.

Route map:

    GET/POST      /api/admin/marketplace/templates
    PUT/DELETE    /api/admin/marketplace/templates/:id
    GET/POST      /api/admin/content/templates/featured
    DELETE        /api/admin/content/templates/featured/:id
    POST          /api/admin/content/trending/recalculate
    GET           /api/admin/content/stats
    GET/PUT       /api/admin/content/campaign/weekly-prompt
    GET/POST      /api/admin/content/campaign/trending-tags
    PUT/DELETE    /api/admin/content/campaign/trending-tags/:id
    POST          /api/admin/content/campaign/trending-tags/generate
    GET/PUT       /api/admin/content/campaign/hero
    GET           /api/admin/content/campaign/templates
    GET           /api/creations/public          (discovery)
    GET           /api/v3/public/models          (discovery)
    POST          /api/media/upload              (image + video media helper)

Campaign responses wrap their payload in a named key — `weekly_prompt`,
`tag`, `tags`, `result`, `hero`, `templates` — which `_unwrap` / `_rows`
strip so `--json` prints the object itself.

Local media — template previews and references, featured thumbnails, hero
backgrounds — uploads through POST /api/media/upload, the one endpoint that
accepts video as well as images. A template preview additionally records
`preview_media_type` ("image"/"video") next to `preview_url`, so `--preview`
always writes the pair: for an upload the kind comes from the response's
`media_kind`, and for a remote HTTPS URL it is inferred strictly from the
extension rather than guessed. Its allowlist is keyed on the declared
Content-Type and never on the filename, so `_upload_media` sends the canonical
MIME for the extension and an unknown type is refused locally rather than
earning a 415. A `data:` URI is refused outright on every one of those flags:
these values land in durable public config that the site reads on every
render, so they must be object URLs, not inlined bytes.

No command here touches a database; every mutation goes through the API so
the backend keeps owning validation, auditing and cache invalidation. The
weekly-prompt and hero PUTs are full replacements, so those two commands
always send a complete body rather than a sparse patch.

Side effects are explicit: commands that delete published state, replace a
singleton (weekly prompt, hero), or recompute a whole tag/score set refuse
to run without `--yes`.

`admin api` is the generic escape hatch for every other Super Admin endpoint
— users, tiers, token packages, organizations, audit logs, assets, DLQ,
applications, public images — so the CLI does not need a typed command per
route to be complete. It is scoped to the admin route groups only
(/api/admin and /api/v3/admin): a superadmin token is the most powerful
credential in the system, so the one generic command that carries it must
not double as a client for creator, media or public routes. Non-GET methods
require --yes, and credential-shaped response values are redacted unless
--no-redact is passed.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote, unquote, urlsplit

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

app = typer.Typer(no_args_is_help=True, help="Super Admin campaign & content control.")

template_app = typer.Typer(no_args_is_help=True, help="Marketplace templates (campaign content).")
featured_app = typer.Typer(no_args_is_help=True, help="Featured templates on the home surface.")
weekly_app = typer.Typer(no_args_is_help=True, help="Weekly prompt campaign slot.")
trending_app = typer.Typer(no_args_is_help=True, help="Trending tags and score recalculation.")
hero_app = typer.Typer(no_args_is_help=True, help="Home hero slot.")
media_app = typer.Typer(no_args_is_help=True, help="Upload campaign media and get a CDN URL.")
creation_app = typer.Typer(no_args_is_help=True, help="Public creations discovery.")
model_app = typer.Typer(no_args_is_help=True, help="Model registry discovery.")
api_app = typer.Typer(
    no_args_is_help=True,
    help="Call any /api/admin or /api/v3/admin endpoint (generic escape hatch).",
)

app.add_typer(template_app, name="template")
app.add_typer(featured_app, name="featured")
app.add_typer(weekly_app, name="weekly-prompt")
app.add_typer(trending_app, name="trending")
app.add_typer(hero_app, name="hero")
app.add_typer(media_app, name="media")
app.add_typer(creation_app, name="creation")
app.add_typer(model_app, name="model")
app.add_typer(api_app, name="api")

ADMIN_TEMPLATES = "/api/admin/marketplace/templates"
ADMIN_CONTENT = "/api/admin/content"
CAMPAIGN = f"{ADMIN_CONTENT}/campaign"
MEDIA_UPLOAD = "/api/media/upload"

# Mirrors the backend's uploadMediaAllowlist. That allowlist is keyed on the
# declared Content-Type and never consults the filename, so the CLI must send
# the canonical MIME for the extension — an unknown type is a 415, not a guess.
UPLOAD_CONTENT_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
}

# Storage buckets the backend's normalizeMediaCategory accepts; anything else
# is silently rewritten to "media", so reject it here instead of surprising the
# operator with a file stored somewhere they did not ask for.
MEDIA_CATEGORIES = ("assets", "logos", "branding", "media", "badges")

# Extension -> "image"/"video", derived from the one allowlist above so the two
# can never drift apart.
MEDIA_KIND_BY_EXTENSION = {
    ext: ("video" if mime.startswith("video/") else "image") for ext, mime in UPLOAD_CONTENT_TYPES.items()
}


# ---------------------------------------------------------------------------
# shared helpers
# ---------------------------------------------------------------------------


def _fail(message: str) -> typer.Exit:
    err_console.print(f"[red]{message}[/red]")
    return typer.Exit(code=2)


def _require_yes(yes: bool, what: str) -> None:
    """Side-effecting commands must be asked for explicitly."""
    if not yes:
        raise _fail(f"{what} changes live content. Re-run with --yes to confirm.")


def _load_payload_json(file: Optional[str]) -> Any:
    """Read any JSON payload from a path, or from stdin when given `-`."""
    if not file:
        return None
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
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise _fail(f"{source} is not valid JSON: {exc}")


def _load_payload_file(file: Optional[str]) -> dict[str, Any]:
    """Read a JSON *object* payload — what the typed commands merge into."""
    if not file:
        return {}
    data = _load_payload_json(file)
    if not isinstance(data, dict):
        source = "stdin" if file == "-" else file
        raise _fail(f"{source} must contain a JSON object, got {type(data).__name__}")
    return data


def _parse_timestamp(value: Optional[str], flag: str) -> Optional[str]:
    if value is None:
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        raise _fail(f"{flag} must be an ISO/RFC3339 timestamp, e.g. 2026-06-01T00:00:00Z")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _tri_state(on: bool, off: bool, flag: str) -> Optional[bool]:
    if on and off:
        raise _fail(f"{flag} flags conflict; pass only one.")
    if on:
        return True
    if off:
        return False
    return None


def _put_if(payload: dict[str, Any], key: str, value: Any) -> None:
    if value is not None:
        payload[key] = value


def _upload_media(client: PictureMEClient, path: Path, category: Optional[str] = None) -> dict[str, Any]:
    """Upload one local image or video and return the backend's record.

    Both families go to POST /api/media/upload, which is the only upload
    endpoint that accepts video. The response carries `media_kind`
    ("image"/"video") for the object as actually stored, which is more
    trustworthy than the local extension.
    """
    content_type = UPLOAD_CONTENT_TYPES.get(path.suffix.lower())
    if content_type is None:
        raise _fail(
            f"unsupported media type '{path.suffix or path.name}'. "
            f"Allowed: {', '.join(sorted(UPLOAD_CONTENT_TYPES))}"
        )
    data = {"category": category} if category else None
    with path.open("rb") as fh:
        payload = client.post(
            MEDIA_UPLOAD,
            files={"file": (path.name, fh, content_type)},
            data=data,
        )
    url = payload.get("url") or payload.get("file_url") or payload.get("location")
    if not url:
        raise APIError(f"upload returned no URL: {payload}")
    if not isinstance(payload, dict):
        return {"url": url}
    return {**payload, "url": url}


def _reject_data_uri(value: str, flag: str) -> None:
    """A data: URI would be inlined into durable public config forever.

    These values are persisted into template records and hero config that the
    public site reads on every render, so the bytes belong in object storage
    with a real URL, not in the row.
    """
    if value.strip().lower().startswith("data:"):
        raise _fail(
            f"{flag} does not accept a data: URI — durable public config stores a URL. "
            "Upload the file (pass the local path, or use `admin media upload`) and use the returned URL."
        )


def _resolve_media_record(
    client: PictureMEClient, value: Optional[str], flag: str
) -> Optional[dict[str, Any]]:
    """Pass remote URLs through; upload local paths. Returns the full record."""
    if value is None:
        return None
    _reject_data_uri(value, flag)
    if value.startswith(("http://", "https://")):
        return {"url": value}
    path = Path(value)
    if not path.is_file():
        raise _fail(f"file not found: {value}")
    return _upload_media(client, path)


def _resolve_media(client: PictureMEClient, value: Optional[str], flag: str = "media") -> Optional[str]:
    """As `_resolve_media_record`, but yielding just the URL."""
    record = _resolve_media_record(client, value, flag)
    return None if record is None else record["url"]


def _remote_media_kind(url: str, flag: str) -> str:
    """Infer image/video from a remote URL's extension, strictly.

    Guessing wrong here is not cosmetic: `preview_media_type` decides whether
    every public surface renders an <img> or a <video>, so an unrecognized
    extension is an error rather than a default. The way out is to declare
    both fields in --file, or to pass the local file so the upload response
    settles the kind.
    """
    kind = MEDIA_KIND_BY_EXTENSION.get(Path(urlsplit(url).path).suffix.lower())
    if kind is None:
        raise _fail(
            f"cannot tell whether {url} is an image or a video, so preview_media_type would be a guess. "
            f"Pass a URL ending in one of {', '.join(sorted(MEDIA_KIND_BY_EXTENSION))}, "
            f"pass the local file to {flag} so the upload decides, or set preview_url and "
            "preview_media_type explicitly in --file."
        )
    return kind


def _resolve_preview(
    client: PictureMEClient, value: Optional[str], flag: str = "--preview"
) -> Optional[tuple[str, str]]:
    """Resolve `--preview` to the (url, media_type) pair the record stores.

    A local path uploads and takes its kind from the response's `media_kind`
    (authoritative for the object as stored), falling back to the local
    extension if the backend omits it. A remote URL must be durable public
    HTTPS and is typed strictly from its extension.
    """
    if value is None:
        return None
    _reject_data_uri(value, flag)
    if value.startswith("http://"):
        raise _fail(
            f"{flag} must be an HTTPS URL — preview media is durable public config and "
            "is served to every visitor."
        )
    if value.startswith("https://"):
        return value, _remote_media_kind(value, flag)

    path = Path(value)
    if not path.is_file():
        raise _fail(f"file not found: {value}")
    record = _upload_media(client, path)
    kind = record.get("media_kind")
    if kind not in ("image", "video"):
        kind = MEDIA_KIND_BY_EXTENSION.get(path.suffix.lower())
    if kind is None:
        raise _fail(f"upload did not report a media kind for {value}.")
    return record["url"], kind


def _emit(ctx: typer.Context, payload: Any, *, title: str = "Result") -> None:
    """JSON when --json, otherwise a flat field/value table."""
    if json_mode(ctx):
        emit_json(payload)
        return
    if isinstance(payload, dict):
        rows = [[k, v] for k, v in payload.items()]
        emit_table(title, ["field", "value"], rows)
    else:
        emit_json(payload)


def _unwrap(payload: Any, key: str) -> Any:
    """Campaign responses wrap their object in a named key (`{"hero": {...}}`)."""
    if isinstance(payload, dict) and key in payload:
        return payload[key]
    return payload


def _safe_destination(value: str, flag: str) -> str:
    """Mirror the backend's SafeCampaignDestination allowlist.

    Only a site-relative path or an absolute http(s) URL is accepted; these
    strings are handed straight to a browser by the Creator client, so
    javascript:, data:, mailto: and protocol-relative "//host" are rejected
    here rather than earning a 400 round-trip.
    """
    candidate = (value or "").strip()
    if not candidate:
        return ""
    if len(candidate) > 2048:
        raise _fail(f"{flag} must be at most 2048 characters.")
    if any(ch in candidate for ch in " \t\r\n"):
        raise _fail(f"{flag} must not contain whitespace or control characters.")
    if "\\" in candidate:
        raise _fail(f"{flag} must not contain backslashes.")
    if candidate.startswith("//"):
        raise _fail(f"{flag} must not be protocol-relative.")
    if candidate.startswith("/"):
        return candidate
    parsed = urlsplit(candidate)
    if parsed.scheme.lower() not in ("http", "https"):
        raise _fail(f"{flag} must be an absolute http(s) URL or a site-relative path.")
    if not parsed.netloc:
        raise _fail(f"{flag} is missing a host.")
    return candidate


def _rows(payload: Any, *keys: str) -> list[dict[str, Any]]:
    """Unwrap the backend's `{"templates": [...]}`-style envelopes."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in keys:
            value = payload.get(key)
            if isinstance(value, list):
                return value
    return []


# ---------------------------------------------------------------------------
# admin template
# ---------------------------------------------------------------------------

TEMPLATE_FLAG_FIELDS = (
    # (payload key, parameter name)
    ("name", "name"),
    ("description", "description"),
    ("prompt", "prompt"),
    ("negative_prompt", "negative_prompt"),
    ("template_type", "template_type"),
    ("media_type", "media_type"),
    ("style_mode", "style_mode"),
    ("category", "category"),
    ("ai_model", "ai_model"),
    ("aspectRatio", "aspect_ratio"),
    ("status", "status"),
    ("tokens_cost", "tokens_cost"),
    ("price", "price"),
)


def _template_payload(
    client: PictureMEClient,
    *,
    file: Optional[str],
    values: dict[str, Any],
    tags: Optional[list[str]],
    preview: Optional[str],
    references: Optional[list[str]],
    is_public: Optional[bool],
    is_premium: Optional[bool],
) -> dict[str, Any]:
    """File payload is the base; explicit flags win over it."""
    payload = _load_payload_file(file)
    for key, param in TEMPLATE_FLAG_FIELDS:
        _put_if(payload, key, values.get(param))
    if tags:
        payload["tags"] = list(tags)
    # preview_url and preview_media_type are one decision: --preview replaces
    # both, and when it is absent whatever --file declared stands untouched.
    resolved_preview = _resolve_preview(client, preview)
    if resolved_preview is not None:
        payload["preview_url"], payload["preview_media_type"] = resolved_preview
    if references:
        payload["reference_images"] = [_resolve_media(client, r, "--reference") for r in references]
    _put_if(payload, "is_public", is_public)
    _put_if(payload, "is_premium", is_premium)
    return payload


@template_app.command("list")
def template_list(
    ctx: typer.Context,
    template_type: Optional[str] = typer.Option(None, "--type", help="Filter by template_type."),
    category: Optional[str] = typer.Option(None, "--category", help="Filter by category."),
    search: Optional[str] = typer.Option(None, "--search", help="Free-text search."),
) -> None:
    """List every template the admin surface can see (including drafts)."""
    client = make_client(ctx)
    params = {k: v for k, v in (("type", template_type), ("category", category), ("search", search)) if v}
    try:
        payload = client.get(ADMIN_TEMPLATES, params=params)
    except APIError as exc:
        raise handle_api_error(exc)
    finally:
        client.close()

    if json_mode(ctx):
        emit_json(payload)
        return
    templates = _rows(payload, "templates")
    rows = [
        [
            t.get("id"),
            t.get("name"),
            t.get("template_type"),
            t.get("category"),
            t.get("status"),
            t.get("is_public"),
            t.get("tokens_cost"),
        ]
        for t in templates
    ]
    emit_table(
        f"Templates ({len(rows)})",
        ["id", "name", "type", "category", "status", "public", "tokens"],
        rows,
    )


@template_app.command("get")
def template_get(
    ctx: typer.Context,
    template_id: str = typer.Argument(..., help="Template id."),
) -> None:
    """Show one template.

    There is no admin GET-by-id route, and the public one hides drafts, so we
    select out of the admin listing the operator is already entitled to.
    """
    client = make_client(ctx)
    try:
        payload = client.get(ADMIN_TEMPLATES)
    except APIError as exc:
        raise handle_api_error(exc)
    finally:
        client.close()

    for template in _rows(payload, "templates"):
        if str(template.get("id")) == template_id:
            _emit(ctx, template, title="Template")
            return

    err_console.print(f"[red]template not found:[/red] {template_id}")
    raise typer.Exit(code=1)


@template_app.command("options")
def template_options(
    ctx: typer.Context,
    template_type: Optional[str] = typer.Option(None, "--type", help="Filter by template_type."),
    category: Optional[str] = typer.Option(None, "--category", help="Filter by category."),
    search: Optional[str] = typer.Option(None, "--search", help="Free-text search."),
    limit: Optional[int] = typer.Option(None, "--limit", "-n", help="Max options to return."),
) -> None:
    """List the campaign picker's template options.

    The visual projection the admin UI picker uses — carries preview media,
    capabilities and current featured state, so it is what you want when
    choosing a hero template target or curating the featured rail.
    """
    client = make_client(ctx)
    params: dict[str, Any] = {}
    for key, value in (("type", template_type), ("category", category), ("search", search), ("limit", limit)):
        if value is not None and value != "":
            params[key] = value
    try:
        payload = client.get(f"{CAMPAIGN}/templates", params=params)
    except APIError as exc:
        raise handle_api_error(exc)
    finally:
        client.close()

    if json_mode(ctx):
        emit_json(payload)
        return
    rows = [
        [
            t.get("id"),
            t.get("name"),
            t.get("template_type"),
            t.get("media_type"),
            t.get("ai_model"),
            t.get("is_featured"),
            t.get("featured_order"),
        ]
        for t in _rows(payload, "templates")
    ]
    emit_table(
        f"Campaign template options ({len(rows)})",
        ["id", "name", "type", "media", "model", "featured", "order"],
        rows,
    )


@template_app.command("create")
def template_create(
    ctx: typer.Context,
    file: Optional[str] = typer.Option(None, "--file", "-f", help="JSON payload file, or '-' for stdin."),
    name: Optional[str] = typer.Option(None, "--name"),
    description: Optional[str] = typer.Option(None, "--description"),
    prompt: Optional[str] = typer.Option(None, "--prompt"),
    negative_prompt: Optional[str] = typer.Option(None, "--negative-prompt"),
    template_type: Optional[str] = typer.Option(None, "--type", help="template_type (photo, video, ...)."),
    media_type: Optional[str] = typer.Option(None, "--media-type", help="image | video."),
    style_mode: Optional[str] = typer.Option(None, "--style-mode", help="prompt | composition."),
    category: Optional[str] = typer.Option(None, "--category"),
    tag: Optional[list[str]] = typer.Option(None, "--tag", help="Repeatable; replaces the tag list."),
    ai_model: Optional[str] = typer.Option(None, "--ai-model", help="Registry model id."),
    aspect_ratio: Optional[str] = typer.Option(None, "--aspect-ratio"),
    tokens_cost: Optional[int] = typer.Option(None, "--tokens-cost"),
    price: Optional[float] = typer.Option(None, "--price"),
    status: Optional[str] = typer.Option(None, "--status", help="draft | pending | published | rejected."),
    preview: Optional[str] = typer.Option(None, "--preview", help="Preview HTTPS URL, or a local image/video path to upload. Sets preview_url and preview_media_type together."),
    reference: Optional[list[str]] = typer.Option(
        None, "--reference", help="Reference URL, or a local image/video path to upload. Repeatable."
    ),
    public: bool = typer.Option(False, "--public", help="Mark the template public."),
    private: bool = typer.Option(False, "--private", help="Mark the template private."),
    premium: bool = typer.Option(False, "--premium", help="Mark the template premium."),
    standard: bool = typer.Option(False, "--standard", help="Mark the template non-premium."),
) -> None:
    """Create a template.

    Local `--preview` / `--reference` paths upload first — images and video
    (mp4, mov, webm) alike. `--preview` writes `preview_url` and
    `preview_media_type` together; without it, whatever `--file` declares for
    those two fields stands.
    """
    is_public = _tri_state(public, private, "--public/--private")
    is_premium = _tri_state(premium, standard, "--premium/--standard")

    client = make_client(ctx)
    try:
        payload = _template_payload(
            client,
            file=file,
            values={
                "name": name,
                "description": description,
                "prompt": prompt,
                "negative_prompt": negative_prompt,
                "template_type": template_type,
                "media_type": media_type,
                "style_mode": style_mode,
                "category": category,
                "ai_model": ai_model,
                "aspect_ratio": aspect_ratio,
                "status": status,
                "tokens_cost": tokens_cost,
                "price": price,
            },
            tags=tag,
            preview=preview,
            references=reference,
            is_public=is_public,
            is_premium=is_premium,
        )
        if not payload.get("name"):
            raise _fail("template name is required (pass --name or include \"name\" in --file).")
        created = client.post(ADMIN_TEMPLATES, json=payload)
    except APIError as exc:
        raise handle_api_error(exc)
    finally:
        client.close()

    _emit(ctx, created, title="Template created")


@template_app.command("update")
def template_update(
    ctx: typer.Context,
    template_id: str = typer.Argument(..., help="Template id."),
    file: Optional[str] = typer.Option(None, "--file", "-f", help="JSON payload file, or '-' for stdin."),
    name: Optional[str] = typer.Option(None, "--name"),
    description: Optional[str] = typer.Option(None, "--description"),
    prompt: Optional[str] = typer.Option(None, "--prompt"),
    negative_prompt: Optional[str] = typer.Option(None, "--negative-prompt"),
    template_type: Optional[str] = typer.Option(None, "--type"),
    media_type: Optional[str] = typer.Option(None, "--media-type"),
    style_mode: Optional[str] = typer.Option(None, "--style-mode"),
    category: Optional[str] = typer.Option(None, "--category"),
    tag: Optional[list[str]] = typer.Option(None, "--tag", help="Repeatable; replaces the tag list."),
    ai_model: Optional[str] = typer.Option(None, "--ai-model"),
    aspect_ratio: Optional[str] = typer.Option(None, "--aspect-ratio"),
    tokens_cost: Optional[int] = typer.Option(None, "--tokens-cost"),
    price: Optional[float] = typer.Option(None, "--price"),
    status: Optional[str] = typer.Option(None, "--status"),
    preview: Optional[str] = typer.Option(None, "--preview", help="Preview HTTPS URL, or a local image/video path to upload. Sets preview_url and preview_media_type together."),
    reference: Optional[list[str]] = typer.Option(
        None, "--reference", help="Reference URL, or a local image/video path to upload. Repeatable."
    ),
    public: bool = typer.Option(False, "--public"),
    private: bool = typer.Option(False, "--private"),
    premium: bool = typer.Option(False, "--premium"),
    standard: bool = typer.Option(False, "--standard"),
) -> None:
    """Partially update a template — only the fields you pass are sent.

    `--preview` sends `preview_url` and `preview_media_type` as a pair, so a
    preview never ends up with a stale kind.
    """
    is_public = _tri_state(public, private, "--public/--private")
    is_premium = _tri_state(premium, standard, "--premium/--standard")

    client = make_client(ctx)
    try:
        payload = _template_payload(
            client,
            file=file,
            values={
                "name": name,
                "description": description,
                "prompt": prompt,
                "negative_prompt": negative_prompt,
                "template_type": template_type,
                "media_type": media_type,
                "style_mode": style_mode,
                "category": category,
                "ai_model": ai_model,
                "aspect_ratio": aspect_ratio,
                "status": status,
                "tokens_cost": tokens_cost,
                "price": price,
            },
            tags=tag,
            preview=preview,
            references=reference,
            is_public=is_public,
            is_premium=is_premium,
        )
        if not payload:
            raise _fail("nothing to update: pass at least one field or --file.")
        updated = client.put(f"{ADMIN_TEMPLATES}/{template_id}", json=payload)
    except APIError as exc:
        raise handle_api_error(exc)
    finally:
        client.close()

    _emit(ctx, updated, title="Template updated")


@template_app.command("delete")
def template_delete(
    ctx: typer.Context,
    template_id: str = typer.Argument(..., help="Template id."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Confirm the deletion."),
) -> None:
    """Delete a template. Requires --yes."""
    _require_yes(yes, f"Deleting template {template_id}")
    client = make_client(ctx)
    try:
        payload = client.delete(f"{ADMIN_TEMPLATES}/{template_id}")
    except APIError as exc:
        raise handle_api_error(exc)
    finally:
        client.close()
    _emit(ctx, payload, title="Template deleted")


# ---------------------------------------------------------------------------
# admin featured
# ---------------------------------------------------------------------------

FEATURED_PATH = f"{ADMIN_CONTENT}/templates/featured"


@featured_app.command("list")
def featured_list(ctx: typer.Context) -> None:
    """List featured templates (active and inactive)."""
    client = make_client(ctx)
    try:
        payload = client.get(FEATURED_PATH)
    except APIError as exc:
        raise handle_api_error(exc)
    finally:
        client.close()

    if json_mode(ctx):
        emit_json(payload)
        return
    rows = [
        [
            f.get("id"),
            f.get("template_id"),
            f.get("template_name"),
            f.get("featured_order"),
            f.get("is_active"),
        ]
        for f in _rows(payload, "templates", "featured")
    ]
    emit_table(f"Featured templates ({len(rows)})", ["id", "template", "name", "order", "active"], rows)


@featured_app.command("add")
def featured_add(
    ctx: typer.Context,
    template_id: str = typer.Argument(..., help="Marketplace template id to feature."),
    name: Optional[str] = typer.Option(None, "--name", help="Display name override."),
    template_type: Optional[str] = typer.Option(None, "--type", help="template_type label."),
    thumbnail: Optional[str] = typer.Option(None, "--thumbnail", help="Thumbnail URL, or a local image/video path to upload."),
    order: int = typer.Option(0, "--order", help="Sort order on the home surface."),
) -> None:
    """Feature a template on the home surface."""
    client = make_client(ctx)
    try:
        payload: dict[str, Any] = {"template_id": template_id}
        _put_if(payload, "template_name", name)
        _put_if(payload, "template_type", template_type)
        _put_if(payload, "thumbnail_url", _resolve_media(client, thumbnail, "--thumbnail"))
        payload["featured_order"] = order
        created = client.post(FEATURED_PATH, json=payload)
    except APIError as exc:
        raise handle_api_error(exc)
    finally:
        client.close()
    _emit(ctx, created, title="Featured")


@featured_app.command("remove")
def featured_remove(
    ctx: typer.Context,
    featured_id: str = typer.Argument(..., help="Featured row id (from `admin featured list`)."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Confirm the removal."),
) -> None:
    """Unfeature a template. Requires --yes."""
    _require_yes(yes, f"Unfeaturing {featured_id}")
    client = make_client(ctx)
    try:
        payload = client.delete(f"{FEATURED_PATH}/{featured_id}")
    except APIError as exc:
        raise handle_api_error(exc)
    finally:
        client.close()
    _emit(ctx, payload, title="Unfeatured")


# ---------------------------------------------------------------------------
# admin weekly-prompt
# ---------------------------------------------------------------------------

WEEKLY_PATH = f"{CAMPAIGN}/weekly-prompt"

WEEKLY_FIELDS = ("title", "description", "prompt", "cta_label", "destination")


@weekly_app.command("get")
def weekly_get(ctx: typer.Context) -> None:
    """Show the current weekly prompt."""
    client = make_client(ctx)
    try:
        payload = client.get(WEEKLY_PATH)
    except APIError as exc:
        raise handle_api_error(exc)
    finally:
        client.close()
    _emit(ctx, _unwrap(payload, "weekly_prompt"), title="Weekly prompt")


@weekly_app.command("set")
def weekly_set(
    ctx: typer.Context,
    file: Optional[str] = typer.Option(None, "--file", "-f", help="JSON payload file, or '-' for stdin."),
    title: Optional[str] = typer.Option(None, "--title", help="Required (here or in --file)."),
    description: Optional[str] = typer.Option(None, "--description"),
    prompt: Optional[str] = typer.Option(None, "--prompt", help="Required (here or in --file)."),
    cta_label: Optional[str] = typer.Option(None, "--cta-label"),
    destination: Optional[str] = typer.Option(
        None, "--destination", help="Site-relative path (/studio) or absolute http(s) URL."
    ),
    enabled: bool = typer.Option(False, "--enabled", help="Show the slot (the default)."),
    disabled: bool = typer.Option(False, "--disabled", help="Hide the slot."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Confirm replacing the live weekly prompt."),
) -> None:
    """Replace the weekly prompt.

    The backend route is a PUT full replacement: every field is authoritative,
    so anything you do not pass is sent as empty rather than left alone. Read
    the current value with `admin weekly-prompt get` first if you only mean to
    change one field. `enabled` defaults to true — pass --disabled to hide the
    slot. Requires --yes.
    """
    _require_yes(yes, "Setting the weekly prompt")
    is_enabled = _tri_state(enabled, disabled, "--enabled/--disabled")

    payload = _load_payload_file(file)
    for key, value in (
        ("title", title),
        ("description", description),
        ("prompt", prompt),
        ("cta_label", cta_label),
        ("destination", destination),
    ):
        _put_if(payload, key, value)
    if is_enabled is not None:
        payload["enabled"] = is_enabled

    body = {key: payload.get(key, "") or "" for key in WEEKLY_FIELDS}
    body["enabled"] = bool(payload.get("enabled", True))

    if not body["title"]:
        raise _fail("title is required (pass --title or include \"title\" in --file).")
    if not body["prompt"]:
        raise _fail("prompt is required (pass --prompt or include \"prompt\" in --file).")
    body["destination"] = _safe_destination(body["destination"], "--destination")

    client = make_client(ctx)
    try:
        result = client.put(WEEKLY_PATH, json=body)
    except APIError as exc:
        raise handle_api_error(exc)
    finally:
        client.close()
    _emit(ctx, _unwrap(result, "weekly_prompt"), title="Weekly prompt")


# ---------------------------------------------------------------------------
# admin trending
# ---------------------------------------------------------------------------

TAGS_PATH = f"{CAMPAIGN}/trending-tags"


def _check_non_negative(value: Optional[int], flag: str) -> None:
    if value is not None and value < 0:
        raise _fail(f"{flag} must not be negative.")


@trending_app.command("list")
def trending_list(
    ctx: typer.Context,
    active: bool = typer.Option(False, "--active", help="Only tags the public home would show."),
) -> None:
    """List campaign trending tags (manual and generated)."""
    client = make_client(ctx)
    params = {"active": "true"} if active else {}
    try:
        payload = client.get(TAGS_PATH, params=params)
    except APIError as exc:
        raise handle_api_error(exc)
    finally:
        client.close()

    if json_mode(ctx):
        emit_json(payload)
        return
    rows = [
        [t.get("id"), t.get("label"), t.get("slug"), t.get("source"), t.get("score"), t.get("order"), t.get("is_active")]
        for t in _rows(payload, "tags")
    ]
    emit_table(
        f"Trending tags ({len(rows)})",
        ["id", "label", "slug", "source", "score", "order", "active"],
        rows,
    )


@trending_app.command("add")
def trending_add(
    ctx: typer.Context,
    label: str = typer.Argument(..., help="Display label, e.g. 'Anime Portrait'."),
    slug: Optional[str] = typer.Option(None, "--slug", help="URL slug. Derived from the label when omitted."),
    score: Optional[int] = typer.Option(None, "--score", help="Ranking score (>= 0)."),
    order: Optional[int] = typer.Option(None, "--order", help="Display order (>= 0)."),
    active: bool = typer.Option(False, "--active"),
    inactive: bool = typer.Option(False, "--inactive"),
) -> None:
    """Add a hand-curated trending tag.

    Admin-created tags are always recorded with source=manual, so
    `admin trending generate` can never delete them.
    """
    is_active = _tri_state(active, inactive, "--active/--inactive")
    _check_non_negative(score, "--score")
    _check_non_negative(order, "--order")

    payload: dict[str, Any] = {"label": label}
    _put_if(payload, "slug", slug)
    _put_if(payload, "score", score)
    _put_if(payload, "order", order)
    _put_if(payload, "is_active", is_active)

    client = make_client(ctx)
    try:
        created = client.post(TAGS_PATH, json=payload)
    except APIError as exc:
        raise handle_api_error(exc)
    finally:
        client.close()
    _emit(ctx, _unwrap(created, "tag"), title="Trending tag")


@trending_app.command("update")
def trending_update(
    ctx: typer.Context,
    tag_id: str = typer.Argument(..., help="Trending tag id."),
    label: Optional[str] = typer.Option(None, "--label"),
    slug: Optional[str] = typer.Option(None, "--slug"),
    score: Optional[int] = typer.Option(None, "--score", help="Ranking score (>= 0)."),
    order: Optional[int] = typer.Option(None, "--order", help="Display order (>= 0)."),
    active: bool = typer.Option(False, "--active"),
    inactive: bool = typer.Option(False, "--inactive"),
) -> None:
    """Patch a trending tag — only the fields you pass are sent."""
    is_active = _tri_state(active, inactive, "--active/--inactive")
    _check_non_negative(score, "--score")
    _check_non_negative(order, "--order")

    payload: dict[str, Any] = {}
    _put_if(payload, "label", label)
    _put_if(payload, "slug", slug)
    _put_if(payload, "score", score)
    _put_if(payload, "order", order)
    _put_if(payload, "is_active", is_active)
    if not payload:
        raise _fail("nothing to update: pass at least one field.")

    client = make_client(ctx)
    try:
        updated = client.put(f"{TAGS_PATH}/{tag_id}", json=payload)
    except APIError as exc:
        raise handle_api_error(exc)
    finally:
        client.close()
    _emit(ctx, _unwrap(updated, "tag"), title="Trending tag")


@trending_app.command("remove")
def trending_remove(
    ctx: typer.Context,
    tag_id: str = typer.Argument(..., help="Trending tag id."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Confirm the removal."),
) -> None:
    """Remove a trending tag. Requires --yes."""
    _require_yes(yes, f"Removing trending tag {tag_id}")
    client = make_client(ctx)
    try:
        payload = client.delete(f"{TAGS_PATH}/{tag_id}")
    except APIError as exc:
        raise handle_api_error(exc)
    finally:
        client.close()
    _emit(ctx, payload, title="Trending tag removed")


@trending_app.command("generate")
def trending_generate(
    ctx: typer.Context,
    limit: Optional[int] = typer.Option(None, "--limit", "-n", help="How many tags to generate."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Confirm regenerating the tag set."),
) -> None:
    """Recalculate the generated tag set from the catalog and public creations.

    Manual tags are preserved; only generated rows are written or retired.
    Requires --yes.
    """
    _require_yes(yes, "Generating trending tags")
    payload: dict[str, Any] = {}
    _put_if(payload, "limit", limit)

    client = make_client(ctx)
    try:
        result = client.post(f"{TAGS_PATH}/generate", json=payload)
    except APIError as exc:
        raise handle_api_error(exc)
    finally:
        client.close()
    _emit(ctx, _unwrap(result, "result"), title="Trending generated")


@trending_app.command("recalculate")
def trending_recalculate(
    ctx: typer.Context,
    yes: bool = typer.Option(False, "--yes", "-y", help="Confirm the recalculation."),
) -> None:
    """Recompute per-template trending scores.

    Distinct from `generate`: this drives the template trending rail
    (/admin/content/trending/recalculate), not the campaign tag chips.
    Requires --yes.
    """
    _require_yes(yes, "Recalculating trending scores")
    client = make_client(ctx)
    try:
        payload = client.post(f"{ADMIN_CONTENT}/trending/recalculate")
    except APIError as exc:
        raise handle_api_error(exc)
    finally:
        client.close()
    _emit(ctx, payload, title="Trending recalculated")


# ---------------------------------------------------------------------------
# admin hero
# ---------------------------------------------------------------------------

HERO_PATH = f"{CAMPAIGN}/hero"

HERO_TARGET_TYPES = ("studio", "template", "offer", "external")
HERO_ANIMATIONS = ("none", "pulse", "shimmer", "bounce", "glow")
HERO_MAX_BACKGROUNDS = 8

VIDEO_EXTENSIONS = {ext for ext, mime in UPLOAD_CONTENT_TYPES.items() if mime.startswith("video/")}


def _media_type_from_extension(value: str) -> str:
    """Best-effort typing for a remote URL, where all we have is the path."""
    suffix = Path(value.split("?", 1)[0]).suffix.lower()
    return "video" if suffix in VIDEO_EXTENSIONS else "image"


def _hero_backgrounds(client: PictureMEClient, values: Optional[list[str]]) -> list[dict[str, Any]]:
    """Build the ordered backgrounds array; local images and videos upload first.

    For an uploaded file the backend's `media_kind` is authoritative — it
    describes the object as stored. For a remote URL there is nothing to ask,
    so the extension decides.
    """
    backgrounds: list[dict[str, Any]] = []
    for position, value in enumerate(values or []):
        record = _resolve_media_record(client, value, "--background")
        media_type = record.get("media_kind") or _media_type_from_extension(record["url"])
        if media_type not in ("image", "video"):
            media_type = _media_type_from_extension(record["url"])
        backgrounds.append(
            {
                "position": position,
                "media_type": media_type,
                "url": _safe_destination(record["url"], "--background"),
            }
        )
    return backgrounds


@hero_app.command("get")
def hero_get(ctx: typer.Context) -> None:
    """Show the current creator hero."""
    client = make_client(ctx)
    try:
        payload = client.get(HERO_PATH)
    except APIError as exc:
        raise handle_api_error(exc)
    finally:
        client.close()
    _emit(ctx, _unwrap(payload, "hero"), title="Hero")


@hero_app.command("set")
def hero_set(
    ctx: typer.Context,
    file: Optional[str] = typer.Option(None, "--file", "-f", help="JSON payload file, or '-' for stdin."),
    eyebrow: Optional[str] = typer.Option(None, "--eyebrow"),
    headline: Optional[str] = typer.Option(None, "--headline", help="Required (here or in --file)."),
    body: Optional[str] = typer.Option(None, "--body"),
    cta_label: Optional[str] = typer.Option(None, "--cta-label"),
    target_type: Optional[str] = typer.Option(
        None, "--target-type", help=f"One of: {', '.join(HERO_TARGET_TYPES)}. Defaults to studio."
    ),
    destination: Optional[str] = typer.Option(
        None, "--destination", help="Site-relative path (/studio) or absolute http(s) URL."
    ),
    template_id: Optional[str] = typer.Option(None, "--template-id", help="Only valid with --target-type template."),
    button_animation: Optional[str] = typer.Option(
        None, "--button-animation", help=f"One of: {', '.join(HERO_ANIMATIONS)}. Defaults to none."
    ),
    background: Optional[list[str]] = typer.Option(
        None,
        "--background",
        help="Background URL, or a local image/video path to upload. Repeatable, max 8, ordered as given.",
    ),
    enabled: bool = typer.Option(False, "--enabled", help="Show the hero (the default)."),
    disabled: bool = typer.Option(False, "--disabled", help="Hide the hero."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Confirm replacing the live hero."),
) -> None:
    """Replace the creator hero.

    The backend route is a PUT full replacement: every field is authoritative,
    so anything you do not pass is sent as empty rather than left alone — read
    the current value with `admin hero get` first if you only mean to change
    one field. `--background` takes a URL or a local image/video path (mp4,
    mov, webm upload natively); use --file when a background needs
    `public_creation_id`. Requires --yes.
    """
    _require_yes(yes, "Setting the hero")
    is_enabled = _tri_state(enabled, disabled, "--enabled/--disabled")

    payload = _load_payload_file(file)
    for key, value in (
        ("eyebrow", eyebrow),
        ("headline", headline),
        ("body", body),
        ("cta_label", cta_label),
        ("target_type", target_type),
        ("destination", destination),
        ("template_id", template_id),
        ("button_animation", button_animation),
    ):
        _put_if(payload, key, value)
    if is_enabled is not None:
        payload["enabled"] = is_enabled

    resolved_target = (payload.get("target_type") or "studio").strip()
    if resolved_target not in HERO_TARGET_TYPES:
        raise _fail(f"--target-type must be one of {', '.join(HERO_TARGET_TYPES)}.")
    resolved_animation = (payload.get("button_animation") or "none").strip()
    if resolved_animation not in HERO_ANIMATIONS:
        raise _fail(f"--button-animation must be one of {', '.join(HERO_ANIMATIONS)}.")

    resolved_headline = (payload.get("headline") or "").strip()
    if not resolved_headline:
        raise _fail("headline is required (pass --headline or include \"headline\" in --file).")

    resolved_template = (payload.get("template_id") or "").strip()
    resolved_destination = _safe_destination(payload.get("destination") or "", "--destination")

    # Mirror the backend's target/destination rules so an operator gets the
    # error before the request goes out, not after a 400.
    if resolved_target == "template" and not resolved_template:
        raise _fail("template_id is required when --target-type is template.")
    if resolved_target != "template" and resolved_template:
        raise _fail("template_id is only valid when --target-type is template.")
    if resolved_target in ("offer", "external") and not resolved_destination:
        raise _fail(f"destination is required when --target-type is {resolved_target}.")
    if resolved_target == "external" and not resolved_destination.lower().startswith(("http://", "https://")):
        raise _fail("destination must be an absolute http(s) URL when --target-type is external.")

    if background and payload.get("backgrounds"):
        raise _fail("pass backgrounds through --background or --file, not both.")
    if background is not None and len(background) > HERO_MAX_BACKGROUNDS:
        raise _fail(f"at most {HERO_MAX_BACKGROUNDS} hero backgrounds are allowed.")

    client = make_client(ctx)
    try:
        if background:
            backgrounds = _hero_backgrounds(client, background)
        else:
            backgrounds = payload.get("backgrounds") or []
        if len(backgrounds) > HERO_MAX_BACKGROUNDS:
            raise _fail(f"at most {HERO_MAX_BACKGROUNDS} hero backgrounds are allowed.")

        result = client.put(
            HERO_PATH,
            json={
                "eyebrow": (payload.get("eyebrow") or "").strip(),
                "headline": resolved_headline,
                "body": (payload.get("body") or "").strip(),
                "cta_label": (payload.get("cta_label") or "").strip(),
                "target_type": resolved_target,
                "destination": resolved_destination,
                "template_id": resolved_template,
                "button_animation": resolved_animation,
                "enabled": bool(payload.get("enabled", True)),
                "backgrounds": backgrounds,
            },
        )
    except APIError as exc:
        raise handle_api_error(exc)
    finally:
        client.close()
    _emit(ctx, _unwrap(result, "hero"), title="Hero")


# ---------------------------------------------------------------------------
# discovery + media helper
# ---------------------------------------------------------------------------


@media_app.command("upload")
def media_upload(
    ctx: typer.Context,
    path: Path = typer.Argument(..., exists=True, file_okay=True, dir_okay=False, readable=True),
    category: Optional[str] = typer.Option(
        None, "--category", help=f"Storage category. One of: {', '.join(MEDIA_CATEGORIES)}."
    ),
) -> None:
    """Upload a local image or video and print its URL.

    Images (jpg, jpeg, png, gif, webp, svg) and video (mp4, mov, webm) both go
    to POST /api/media/upload — the same endpoint the campaign flags use for
    local paths, so a URL produced here can be pasted into `--preview`,
    `--reference`, `--thumbnail` or `--background` later.
    """
    if category is not None and category not in MEDIA_CATEGORIES:
        raise _fail(f"--category must be one of {', '.join(MEDIA_CATEGORIES)}.")

    client = make_client(ctx)
    try:
        record = _upload_media(client, path, category)
    except APIError as exc:
        raise handle_api_error(exc)
    finally:
        client.close()

    if json_mode(ctx):
        emit_json(record)
        return
    console.print(record["url"])


@creation_app.command("list")
def creation_list(
    ctx: typer.Context,
    limit: int = typer.Option(10, "--limit", "-n", help="Page size."),
    offset: int = typer.Option(0, "--offset", help="Page offset."),
    featured: bool = typer.Option(False, "--featured", help="Only featured creations."),
) -> None:
    """Browse the public creations feed (campaign source material)."""
    client = make_client(ctx)
    params: dict[str, Any] = {"limit": limit, "offset": offset}
    if featured:
        params["featured"] = "true"
    try:
        payload = client.get("/api/creations/public", params=params)
    except APIError as exc:
        raise handle_api_error(exc)
    finally:
        client.close()

    if json_mode(ctx):
        emit_json(payload)
        return
    rows = [
        [c.get("id"), c.get("title") or c.get("prompt"), c.get("is_featured"), c.get("likes"), c.get("created_at")]
        for c in _rows(payload, "creations")
    ]
    emit_table(f"Public creations ({len(rows)})", ["id", "title", "featured", "likes", "created"], rows)


@model_app.command("list")
def admin_model_list(ctx: typer.Context) -> None:
    """List registry models — the `ai_model` values templates may reference."""
    client = make_client(ctx)
    try:
        payload = client.get("/api/v3/public/models")
    except APIError as exc:
        raise handle_api_error(exc)
    finally:
        client.close()

    if json_mode(ctx):
        emit_json(payload)
        return
    rows = [
        [m.get("model_id"), m.get("display_name"), m.get("model_type"), m.get("provider"), m.get("default_cost")]
        for m in _rows(payload, "models")
    ]
    emit_table(f"Models ({len(rows)})", ["id", "name", "type", "provider", "cost"], rows)


@app.command("stats")
def stats(ctx: typer.Context) -> None:
    """Content statistics for the admin dashboard."""
    client = make_client(ctx)
    try:
        payload = client.get(f"{ADMIN_CONTENT}/stats")
    except APIError as exc:
        raise handle_api_error(exc)
    finally:
        client.close()
    _emit(ctx, payload, title="Content stats")


# ---------------------------------------------------------------------------
# admin api — generic authenticated escape hatch
# ---------------------------------------------------------------------------
#
# The typed commands above cover the campaign surface. This covers the rest of
# what a Super Admin can reach — users, tiers, token packages, organizations,
# audit logs, assets, DLQ, applications, public images — without the CLI having
# to grow and then maintain a command per endpoint.
#
# It is deliberately narrow in three ways:
#
#   1. The request can only go to the configured PictureME host, and only to a
#      Super Admin surface. The path must be a site-relative path under
#      /api/admin or /api/v3/admin, matched on a segment boundary against the
#      percent-decoded value. A pasted absolute URL, a protocol-relative
#      `//host`, a backslash, an encoded separator or a `..` traversal can
#      neither redirect the bearer token to somebody else's server nor reach
#      a non-admin route with it.
#   2. Mutations are explicit. Anything other than GET refuses to run without
#      --yes, because an escape hatch is exactly where a typo is expensive.
#   3. Responses are redacted by default. `GET /api/admin/settings` has
#      historically returned provider keys, and printing one into a terminal
#      or CI log is the thing the repo's secret rules forbid outright.

API_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
API_MUTATING_METHODS = ("POST", "PUT", "PATCH", "DELETE")
# The escape hatch is scoped to the Super Admin route groups, not to /api/ at
# large. A superadmin token is the most powerful credential in the system, so
# the one generic command that carries it must not double as a client for
# creator, media or public routes.
API_ADMIN_ROOTS = ("/api/admin", "/api/v3/admin")

REDACTED = "***redacted***"

# Key segments that mark a value as a credential. Matching is segment-wise on
# "_" so `fal_key` and `access_token` redact while `monkey` and `public_url`
# are left alone.
SECRET_KEY_SEGMENTS = frozenset(
    {
        "key",
        "keys",
        "secret",
        "secrets",
        "token",
        "tokens",
        "password",
        "passwd",
        "credential",
        "credentials",
        "authorization",
        "signature",
    }
)


def _is_secret_key(key: str) -> bool:
    normalized = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", key)
    normalized = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", normalized)
    return any(segment in SECRET_KEY_SEGMENTS for segment in re.split(r"[^a-z0-9]+", normalized.lower()))


def _redact(payload: Any) -> tuple[Any, bool]:
    """Recursively mask credential-shaped values. Returns (payload, changed)."""
    if isinstance(payload, dict):
        out: dict[str, Any] = {}
        changed = False
        for key, value in payload.items():
            if isinstance(key, str) and _is_secret_key(key):
                out[key] = REDACTED if value is not None else None
                changed = changed or value is not None
                continue
            out[key], nested = _redact(value)
            changed = changed or nested
        return out, changed
    if isinstance(payload, list):
        items = [_redact(item) for item in payload]
        return [item for item, _ in items], any(nested for _, nested in items)
    return payload, False


def _api_path(raw: str) -> tuple[str, str]:
    """Validate an operator-supplied path; return (path, raw query string).

    Two properties matter here and they are enforced separately:

      * The request cannot leave the configured host — no scheme, no netloc,
        no protocol-relative "//host", no backslashes (which some servers and
        proxies fold into "/").
      * The request cannot leave the Super Admin surfaces — the *decoded* path
        must sit under /api/admin or /api/v3/admin on a segment boundary, so
        `/api/administrator` and `/api/adminx` are not smuggled in by prefix.

    Validation runs on the percent-decoded path, because that is what the
    router sees. Encoded separators (%2f, %5c) and residual encoding after one
    decode (double encoding) are refused outright rather than decoded further:
    the only way a validator and a router disagree is if one of them keeps
    unwrapping and the other stops.

    The query string is returned untouched so literal values survive.
    """
    value = raw or ""
    roots = " or ".join(API_ADMIN_ROOTS)
    rule = f"path must be a site-relative Super Admin path under {roots}, e.g. /api/admin/users"

    if not value.strip():
        raise _fail(f"path is required — {rule}")
    if value != value.strip() or any(ch in value for ch in " \t\r\n\f\v"):
        raise _fail(f"path must not contain whitespace or control characters — {rule}")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        raise _fail(f"path must not contain control characters — {rule}")
    if "\\" in value:
        raise _fail(f"path must not contain backslashes — {rule}")
    if value.startswith("//"):
        raise _fail(f"path must not be protocol-relative — {rule}")

    parts = urlsplit(value)
    if parts.scheme or parts.netloc:
        raise _fail(f"path must not be an absolute URL — {rule}")
    if parts.fragment or "#" in value:
        raise _fail(f"path must not contain a fragment — {rule}")

    encoded = parts.path
    if not encoded.startswith("/"):
        raise _fail(f"refusing to call {value!r} — {rule}")
    if re.search(r"%(?:2f|5c)", encoded, re.IGNORECASE):
        raise _fail(f"path must not contain an encoded '/' or '\\' — {rule}")

    decoded = unquote(encoded)
    if "%" in decoded:
        raise _fail(f"path must not be double percent-encoded — {rule}")
    if "\\" in decoded:
        raise _fail(f"path must not contain backslashes — {rule}")

    # Segment check on the decoded path: no traversal, no empty interior
    # segments. A single trailing slash is preserved — some routes are
    # registered with one and are distinct from the bare path.
    segments = decoded.split("/")
    body = segments[1:-1] if len(segments) > 2 and segments[-1] == "" else segments[1:]
    for segment in body:
        if segment in ("", ".", ".."):
            raise _fail(f"path must not contain '.', '..' or empty segments — {rule}")

    if not any(decoded == root or decoded.startswith(root + "/") for root in API_ADMIN_ROOTS):
        raise _fail(f"refusing to call {decoded!r} — {rule}")

    return decoded, parts.query


def _api_query(inline: str, query: Optional[list[str]]) -> str:
    """Append --query key=value options to an inline query string.

    The inline part is carried through byte for byte — re-encoding it would
    change literal values the caller wrote deliberately. Only the option
    values are encoded, since those arrive as raw shell strings.
    """
    chunks = [inline] if inline else []
    for item in query or []:
        key, sep, value = item.partition("=")
        if not sep or not key:
            raise _fail(f"--query expects key=value, got {item!r}")
        chunks.append(f"{quote(key, safe='')}={quote(value, safe='')}")
    return "&".join(chunks)


def _api_body(data: Optional[str], file: Optional[str]) -> Any:
    """Resolve the request body from --data or --file (or neither)."""
    if data is not None and file is not None:
        raise _fail("pass a body through --data or --file, not both.")
    if file is not None:
        return _load_payload_json(file)
    if data is None:
        return None
    try:
        return json.loads(data)
    except json.JSONDecodeError as exc:
        raise _fail(f"--data is not valid JSON: {exc}")


def _api_call(
    ctx: typer.Context,
    method: str,
    path: str,
    *,
    query: Optional[list[str]],
    data: Optional[str],
    file: Optional[str],
    include: bool,
    no_redact: bool,
    yes: bool,
) -> None:
    verb = method.strip().upper()
    if verb not in API_METHODS:
        raise _fail(f"method must be one of {', '.join(API_METHODS)}.")
    if verb in API_MUTATING_METHODS:
        _require_yes(yes, f"{verb} {path}")

    target, inline_query = _api_path(path)
    query_string = _api_query(inline_query, query)
    body = _api_body(data, file)
    if query_string:
        target = f"{target}?{query_string}"

    kwargs: dict[str, Any] = {}
    if body is not None:
        kwargs["json"] = body

    client = make_client(ctx)
    try:
        status, payload = client.request_with_status(verb, target, **kwargs)
    except APIError as exc:
        raise handle_api_error(exc)
    finally:
        client.close()

    if not no_redact:
        payload, redacted = _redact(payload)
        if redacted:
            err_console.print(
                "[yellow]Note:[/yellow] credential-shaped values were redacted. "
                "Pass --no-redact if you genuinely need them (they will be printed in the clear)."
            )

    if include:
        emit_json({"status": status, "body": payload})
        return
    if json_mode(ctx):
        emit_json(payload)
        return
    _emit(ctx, payload, title=f"{verb} {target}")


@api_app.command("request")
def api_request(
    ctx: typer.Context,
    method: str = typer.Argument(..., help=f"HTTP method: {', '.join(m.lower() for m in API_METHODS)}."),
    path: str = typer.Argument(..., help="Super Admin path under /api/admin or /api/v3/admin, e.g. /api/admin/users."),
    query: Optional[list[str]] = typer.Option(None, "--query", "-q", help="key=value. Repeatable."),
    data: Optional[str] = typer.Option(None, "--data", "-d", help="Inline JSON request body."),
    file: Optional[str] = typer.Option(None, "--file", "-f", help="JSON body file, or '-' for stdin."),
    include: bool = typer.Option(False, "--include", "-i", help="Emit {status, body} instead of the body."),
    no_redact: bool = typer.Option(False, "--no-redact", help="Print credential-shaped values in the clear."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Confirm a mutating request."),
) -> None:
    """Call any Super Admin API endpoint with the stored credentials.

    The path must be a site-relative path under /api/admin or /api/v3/admin,
    so the bearer token can reach neither another host nor a non-admin route.
    Every method except GET requires --yes. Credential-shaped response values
    are redacted unless --no-redact is passed.
    """
    _api_call(
        ctx,
        method,
        path,
        query=query,
        data=data,
        file=file,
        include=include,
        no_redact=no_redact,
        yes=yes,
    )


def _verb_command(name: str, method: str, summary: str):
    """Register `admin api <verb>` as a thin alias of `admin api request`."""

    @api_app.command(name)
    def _command(  # noqa: D401 — help text comes from `summary`
        ctx: typer.Context,
        path: str = typer.Argument(..., help="Super Admin path under /api/admin or /api/v3/admin, e.g. /api/admin/users."),
        query: Optional[list[str]] = typer.Option(None, "--query", "-q", help="key=value. Repeatable."),
        data: Optional[str] = typer.Option(None, "--data", "-d", help="Inline JSON request body."),
        file: Optional[str] = typer.Option(None, "--file", "-f", help="JSON body file, or '-' for stdin."),
        include: bool = typer.Option(False, "--include", "-i", help="Emit {status, body} instead of the body."),
        no_redact: bool = typer.Option(False, "--no-redact", help="Print credential-shaped values in the clear."),
        yes: bool = typer.Option(False, "--yes", "-y", help="Confirm a mutating request."),
    ) -> None:
        _api_call(
            ctx,
            method,
            path,
            query=query,
            data=data,
            file=file,
            include=include,
            no_redact=no_redact,
            yes=yes,
        )

    _command.__doc__ = summary
    return _command


_verb_command("get", "GET", "GET an admin endpoint. Read-only, so no --yes needed.")
_verb_command("post", "POST", "POST to an admin endpoint. Requires --yes.")
_verb_command("put", "PUT", "PUT to an admin endpoint. Requires --yes.")
_verb_command("patch", "PATCH", "PATCH an admin endpoint. Requires --yes.")
_verb_command("delete", "DELETE", "DELETE an admin endpoint. Requires --yes.")
