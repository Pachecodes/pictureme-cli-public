"""Contract tests for the Super Admin campaign/content command group.

Every test drives the real Typer app and the real httpx client; only the
transport is faked, so the assertions cover the exact HTTP method, path,
query, body and Authorization header the backend will receive.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from pictureme import config
from pictureme.cli import app



@pytest.mark.parametrize("json_output", [True, False])
@pytest.mark.parametrize("key", ["api_keys", "credentials", "apiKey", "APIKey", "accessToken", "clientSecret"])
@pytest.mark.parametrize("value", [{"primary": "fixture-container-canary"}, ["fixture-container-canary"]])
def test_admin_masks_entire_secret_subtree(tmp_path, monkeypatch, json_output, key, value):
    fake = FakeAdminAPI()
    safe = {"items": [{"name": "public-example", "enabled": True}]}
    fake.add("GET", "/api/admin/settings", {key: value, "safe": safe})
    install(monkeypatch, fake, tmp_path)
    result = run(["admin", "api", "get", "/api/admin/settings"], json_mode=json_output)
    assert result.exit_code == 0, result.output
    assert "fixture-container-canary" not in result.output
    assert "public-example" in result.stdout
    if json_output:
        assert json.loads(result.stdout) == {key: "***redacted***", "safe": safe}

@pytest.mark.parametrize("json_output", [True, False])
def test_explicit_reveal_preserves_secret_subtree(tmp_path, monkeypatch, json_output):
    fake = FakeAdminAPI()
    payload = {"credentials": {"primary": ["fixture-reveal-canary"]}}
    fake.add("GET", "/api/admin/settings", payload)
    install(monkeypatch, fake, tmp_path)
    result = run(["admin", "api", "get", "/api/admin/settings", "--no-redact"], json_mode=json_output)
    assert result.exit_code == 0, result.output
    assert "fixture-reveal-canary" in result.stdout
    if json_output:
        assert json.loads(result.stdout) == payload

TOKEN = "pmk_admin_test_secret"
HOST = "https://go.pictureme.now"

# Campaign media goes through the strict image+video allowlist on
# POST /api/media/upload, never the image-only template uploader.
MEDIA_UPLOAD = "/api/media/upload"


class FakeAdminAPI:
    """HTTP-boundary fake: records requests, replies from a routing table."""

    def __init__(self, routes: dict[tuple[str, str], tuple[int, object]] | None = None):
        self.requests: list[dict[str, object]] = []
        self.routes: dict[tuple[str, str], tuple[int, object]] = routes or {}

    def add(self, method: str, path: str, payload: object, status: int = 200) -> None:
        self.routes[(method, path)] = (status, payload)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        raw = request.content
        body: object | None
        content_type = request.headers.get("Content-Type") or ""
        if raw and content_type.startswith("application/json"):
            body = json.loads(raw)
        elif raw:
            body = raw
        else:
            body = None

        self.requests.append(
            {
                "method": request.method,
                "path": request.url.path,
                "query": dict(request.url.params),
                "authorization": request.headers.get("Authorization"),
                "content_type": content_type,
                "body": body,
            }
        )

        route = self.routes.get((request.method, request.url.path))
        if route is None:
            return httpx.Response(404, json={"error": "not_found"})
        status, payload = route
        return httpx.Response(status, json=payload)


def install(monkeypatch, fake: FakeAdminAPI, tmp_path: Path) -> None:
    real_client = httpx.Client

    def client_with_fake_transport(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(fake)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", client_with_fake_transport)
    monkeypatch.setattr(config, "config_path", lambda: tmp_path / "config.json")
    monkeypatch.delenv("PICTUREME_HOST", raising=False)
    monkeypatch.setenv("PICTUREME_API_KEY", TOKEN)


def run(args: list[str], *, json_mode: bool = True, input: str | None = None):
    prefix = ["--host", HOST]
    if json_mode:
        prefix.append("--json")
    return CliRunner().invoke(app, prefix + args, input=input)


def only(fake: FakeAdminAPI) -> dict[str, object]:
    assert len(fake.requests) == 1, fake.requests
    return fake.requests[0]


# --------------------------------------------------------------------------
# admin template
# --------------------------------------------------------------------------

TEMPLATE_A = {
    "id": "tpl_a",
    "name": "Neon Gala",
    "template_type": "photo",
    "category": "events",
    "status": "published",
    "is_public": True,
    "tokens_cost": 4,
}
TEMPLATE_B = {"id": "tpl_b", "name": "Retro Booth", "template_type": "photo"}


def test_template_list_hits_admin_marketplace_with_auth_and_filters(tmp_path, monkeypatch):
    fake = FakeAdminAPI()
    fake.add("GET", "/api/admin/marketplace/templates", {"templates": [TEMPLATE_A], "total": 1})
    install(monkeypatch, fake, tmp_path)

    result = run(["admin", "template", "list", "--type", "photo", "--category", "events", "--search", "gala"])

    assert result.exit_code == 0, result.output
    req = only(fake)
    assert req["method"] == "GET"
    assert req["path"] == "/api/admin/marketplace/templates"
    assert req["query"] == {"type": "photo", "category": "events", "search": "gala"}
    assert req["authorization"] == f"Bearer {TOKEN}"
    assert json.loads(result.stdout) == {"templates": [TEMPLATE_A], "total": 1}


def test_template_list_renders_a_table_without_json_flag(tmp_path, monkeypatch):
    fake = FakeAdminAPI()
    fake.add("GET", "/api/admin/marketplace/templates", {"templates": [TEMPLATE_A]})
    install(monkeypatch, fake, tmp_path)

    result = run(["admin", "template", "list"], json_mode=False)

    assert result.exit_code == 0, result.output
    assert "Neon Gala" in result.output
    assert "tpl_a" in result.output


def test_template_get_selects_the_admin_visible_record(tmp_path, monkeypatch):
    fake = FakeAdminAPI()
    fake.add("GET", "/api/admin/marketplace/templates", {"templates": [TEMPLATE_A, TEMPLATE_B]})
    install(monkeypatch, fake, tmp_path)

    result = run(["admin", "template", "get", "tpl_b"])

    assert result.exit_code == 0, result.output
    assert only(fake)["path"] == "/api/admin/marketplace/templates"
    assert json.loads(result.stdout) == TEMPLATE_B


def test_template_get_exits_nonzero_when_absent(tmp_path, monkeypatch):
    fake = FakeAdminAPI()
    fake.add("GET", "/api/admin/marketplace/templates", {"templates": [TEMPLATE_A]})
    install(monkeypatch, fake, tmp_path)

    result = run(["admin", "template", "get", "tpl_missing"])

    assert result.exit_code == 1
    assert "tpl_missing" in result.output


def test_template_create_posts_flag_payload(tmp_path, monkeypatch):
    fake = FakeAdminAPI()
    fake.add("POST", "/api/admin/marketplace/templates", TEMPLATE_A, status=201)
    install(monkeypatch, fake, tmp_path)

    result = run(
        [
            "admin",
            "template",
            "create",
            "--name",
            "Neon Gala",
            "--description",
            "Campaign hero look",
            "--prompt",
            "neon gala portrait",
            "--type",
            "photo",
            "--media-type",
            "image",
            "--category",
            "events",
            "--tag",
            "neon",
            "--tag",
            "gala",
            "--ai-model",
            "nano-banana",
            "--aspect-ratio",
            "1:1",
            "--tokens-cost",
            "4",
            "--preview",
            "https://cdn.example/preview.png",
            "--public",
        ]
    )

    assert result.exit_code == 0, result.output
    req = only(fake)
    assert req["method"] == "POST"
    assert req["path"] == "/api/admin/marketplace/templates"
    assert req["content_type"].startswith("application/json")
    assert req["authorization"] == f"Bearer {TOKEN}"
    assert req["body"] == {
        "name": "Neon Gala",
        "description": "Campaign hero look",
        "prompt": "neon gala portrait",
        "template_type": "photo",
        "media_type": "image",
        "category": "events",
        "tags": ["neon", "gala"],
        "ai_model": "nano-banana",
        "aspectRatio": "1:1",
        "tokens_cost": 4,
        "preview_url": "https://cdn.example/preview.png",
        "preview_media_type": "image",
        "is_public": True,
    }
    assert json.loads(result.stdout) == TEMPLATE_A


def test_template_create_merges_file_payload_with_flag_overrides(tmp_path, monkeypatch):
    payload = tmp_path / "template.json"
    payload.write_text(
        json.dumps({"name": "From File", "category": "events", "tokens_cost": 2, "is_public": False})
    )
    fake = FakeAdminAPI()
    fake.add("POST", "/api/admin/marketplace/templates", TEMPLATE_A, status=201)
    install(monkeypatch, fake, tmp_path)

    result = run(["admin", "template", "create", "--file", str(payload), "--name", "Override"])

    assert result.exit_code == 0, result.output
    assert only(fake)["body"] == {
        "name": "Override",
        "category": "events",
        "tokens_cost": 2,
        "is_public": False,
    }


def test_template_create_reads_payload_from_stdin(tmp_path, monkeypatch):
    fake = FakeAdminAPI()
    fake.add("POST", "/api/admin/marketplace/templates", TEMPLATE_A, status=201)
    install(monkeypatch, fake, tmp_path)

    result = run(
        ["admin", "template", "create", "--file", "-"],
        input=json.dumps({"name": "Piped", "category": "events"}),
    )

    assert result.exit_code == 0, result.output
    assert only(fake)["body"] == {"name": "Piped", "category": "events"}


def test_template_create_requires_a_name(tmp_path, monkeypatch):
    fake = FakeAdminAPI()
    install(monkeypatch, fake, tmp_path)

    result = run(["admin", "template", "create", "--category", "events"])

    assert result.exit_code == 2
    assert fake.requests == []
    assert "name" in result.output.lower()


def test_template_create_rejects_a_non_object_payload_file(tmp_path, monkeypatch):
    payload = tmp_path / "bad.json"
    payload.write_text(json.dumps([{"name": "list not object"}]))
    fake = FakeAdminAPI()
    install(monkeypatch, fake, tmp_path)

    result = run(["admin", "template", "create", "--file", str(payload)])

    assert result.exit_code == 2
    assert fake.requests == []
    assert "object" in result.output.lower()


def test_template_create_rejects_malformed_json_payload_file(tmp_path, monkeypatch):
    payload = tmp_path / "bad.json"
    payload.write_text("{not json")
    fake = FakeAdminAPI()
    install(monkeypatch, fake, tmp_path)

    result = run(["admin", "template", "create", "--file", str(payload)])

    assert result.exit_code == 2
    assert fake.requests == []
    assert "not valid JSON" in result.output


def test_template_create_uploads_local_preview_and_reference_media(tmp_path, monkeypatch):
    preview = tmp_path / "preview.png"
    preview.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    reference = tmp_path / "ref.jpg"
    reference.write_bytes(b"\xff\xd8\xfffake")

    fake = FakeAdminAPI()
    fake.add("POST", MEDIA_UPLOAD, {"url": "https://cdn.example/uploaded.png"})
    fake.add("POST", "/api/admin/marketplace/templates", TEMPLATE_A, status=201)
    install(monkeypatch, fake, tmp_path)

    result = run(
        [
            "admin",
            "template",
            "create",
            "--name",
            "Neon Gala",
            "--preview",
            str(preview),
            "--reference",
            str(reference),
            "--reference",
            "https://cdn.example/keepme.png",
        ]
    )

    assert result.exit_code == 0, result.output
    assert [(r["method"], r["path"]) for r in fake.requests] == [
        ("POST", MEDIA_UPLOAD),
        ("POST", MEDIA_UPLOAD),
        ("POST", "/api/admin/marketplace/templates"),
    ]
    uploads = fake.requests[:2]
    assert all(r["content_type"].startswith("multipart/form-data") for r in uploads)
    assert all(r["authorization"] == f"Bearer {TOKEN}" for r in uploads)
    assert b'filename="preview.png"' in uploads[0]["body"]
    assert b"image/png" in uploads[0]["body"]
    assert b'filename="ref.jpg"' in uploads[1]["body"]
    assert b"image/jpeg" in uploads[1]["body"]
    assert fake.requests[2]["body"] == {
        "name": "Neon Gala",
        "preview_url": "https://cdn.example/uploaded.png",
        "preview_media_type": "image",
        "reference_images": [
            "https://cdn.example/uploaded.png",
            "https://cdn.example/keepme.png",
        ],
    }


def test_template_create_rejects_unsupported_local_media_type(tmp_path, monkeypatch):
    # .mp4 is now a supported preview; .avi is still off the allowlist.
    bad = tmp_path / "clip.avi"
    bad.write_bytes(b"RIFFfake")
    fake = FakeAdminAPI()
    install(monkeypatch, fake, tmp_path)

    result = run(["admin", "template", "create", "--name", "X", "--preview", str(bad)])

    assert result.exit_code == 2
    assert fake.requests == []
    assert "unsupported media type" in result.output.lower()


def test_template_create_rejects_a_missing_local_media_path(tmp_path, monkeypatch):
    fake = FakeAdminAPI()
    install(monkeypatch, fake, tmp_path)

    result = run(["admin", "template", "create", "--name", "X", "--preview", str(tmp_path / "nope.png")])

    assert result.exit_code == 2
    assert fake.requests == []
    assert "file not found" in result.output.lower()


def test_template_update_puts_only_supplied_fields(tmp_path, monkeypatch):
    fake = FakeAdminAPI()
    fake.add("PUT", "/api/admin/marketplace/templates/tpl_a", TEMPLATE_A)
    install(monkeypatch, fake, tmp_path)

    result = run(["admin", "template", "update", "tpl_a", "--status", "published", "--private"])

    assert result.exit_code == 0, result.output
    req = only(fake)
    assert req["method"] == "PUT"
    assert req["path"] == "/api/admin/marketplace/templates/tpl_a"
    assert req["body"] == {"status": "published", "is_public": False}


def test_template_update_requires_at_least_one_field(tmp_path, monkeypatch):
    fake = FakeAdminAPI()
    install(monkeypatch, fake, tmp_path)

    result = run(["admin", "template", "update", "tpl_a"])

    assert result.exit_code == 2
    assert fake.requests == []
    assert "nothing to update" in result.output.lower()


def test_template_delete_requires_explicit_confirmation(tmp_path, monkeypatch):
    fake = FakeAdminAPI()
    fake.add("DELETE", "/api/admin/marketplace/templates/tpl_a", {"success": True})
    install(monkeypatch, fake, tmp_path)

    blocked = run(["admin", "template", "delete", "tpl_a"])
    assert blocked.exit_code == 2
    assert fake.requests == []
    assert "--yes" in blocked.output

    confirmed = run(["admin", "template", "delete", "tpl_a", "--yes"])
    assert confirmed.exit_code == 0, confirmed.output
    req = only(fake)
    assert req["method"] == "DELETE"
    assert req["path"] == "/api/admin/marketplace/templates/tpl_a"
    assert json.loads(confirmed.stdout) == {"success": True}


# --------------------------------------------------------------------------
# admin featured
# --------------------------------------------------------------------------

FEATURED = {"id": 7, "template_id": "tpl_a", "featured_order": 1, "is_active": True}


def test_featured_list_uses_admin_content_route(tmp_path, monkeypatch):
    fake = FakeAdminAPI()
    fake.add("GET", "/api/admin/content/templates/featured", {"templates": [FEATURED]})
    install(monkeypatch, fake, tmp_path)

    result = run(["admin", "featured", "list"])

    assert result.exit_code == 0, result.output
    req = only(fake)
    assert (req["method"], req["path"]) == ("GET", "/api/admin/content/templates/featured")
    assert req["authorization"] == f"Bearer {TOKEN}"
    assert json.loads(result.stdout) == {"templates": [FEATURED]}


def test_featured_add_posts_the_create_featured_contract(tmp_path, monkeypatch):
    fake = FakeAdminAPI()
    fake.add("POST", "/api/admin/content/templates/featured", FEATURED, status=201)
    install(monkeypatch, fake, tmp_path)

    result = run(
        [
            "admin",
            "featured",
            "add",
            "tpl_a",
            "--name",
            "Neon Gala",
            "--type",
            "photo",
            "--thumbnail",
            "https://cdn.example/t.png",
            "--order",
            "3",
        ]
    )

    assert result.exit_code == 0, result.output
    assert only(fake)["body"] == {
        "template_id": "tpl_a",
        "template_name": "Neon Gala",
        "template_type": "photo",
        "thumbnail_url": "https://cdn.example/t.png",
        "featured_order": 3,
    }


def test_featured_add_uploads_a_local_thumbnail(tmp_path, monkeypatch):
    thumb = tmp_path / "thumb.webp"
    thumb.write_bytes(b"RIFFfake")
    fake = FakeAdminAPI()
    fake.add("POST", MEDIA_UPLOAD, {"url": "https://cdn.example/thumb.webp"})
    fake.add("POST", "/api/admin/content/templates/featured", FEATURED, status=201)
    install(monkeypatch, fake, tmp_path)

    result = run(["admin", "featured", "add", "tpl_a", "--thumbnail", str(thumb)])

    assert result.exit_code == 0, result.output
    assert [(r["method"], r["path"]) for r in fake.requests] == [
        ("POST", MEDIA_UPLOAD),
        ("POST", "/api/admin/content/templates/featured"),
    ]
    assert b"image/webp" in fake.requests[0]["body"]
    assert fake.requests[1]["body"] == {
        "template_id": "tpl_a",
        "thumbnail_url": "https://cdn.example/thumb.webp",
        "featured_order": 0,
    }


def test_featured_remove_requires_confirmation_then_deletes(tmp_path, monkeypatch):
    fake = FakeAdminAPI()
    fake.add("DELETE", "/api/admin/content/templates/featured/7", {"success": True})
    install(monkeypatch, fake, tmp_path)

    blocked = run(["admin", "featured", "remove", "7"])
    assert blocked.exit_code == 2
    assert fake.requests == []

    confirmed = run(["admin", "featured", "remove", "7", "--yes"])
    assert confirmed.exit_code == 0, confirmed.output
    req = only(fake)
    assert (req["method"], req["path"]) == ("DELETE", "/api/admin/content/templates/featured/7")


# --------------------------------------------------------------------------
# admin weekly-prompt — PUT is a full replacement (title + prompt required)
# --------------------------------------------------------------------------

WEEKLY = {
    "title": "Week 12",
    "description": "Shoot the light",
    "prompt": "Golden hour selfies",
    "cta_label": "Try it",
    "destination": "/studio",
    "enabled": True,
    "updated_at": "2026-06-01T00:00:00Z",
}

WEEKLY_PATH = "/api/admin/content/campaign/weekly-prompt"


def test_weekly_prompt_get(tmp_path, monkeypatch):
    fake = FakeAdminAPI()
    fake.add("GET", WEEKLY_PATH, {"weekly_prompt": WEEKLY})
    install(monkeypatch, fake, tmp_path)

    result = run(["admin", "weekly-prompt", "get"])

    assert result.exit_code == 0, result.output
    req = only(fake)
    assert (req["method"], req["path"]) == ("GET", WEEKLY_PATH)
    assert req["authorization"] == f"Bearer {TOKEN}"
    assert json.loads(result.stdout) == WEEKLY


def test_weekly_prompt_set_requires_confirmation_then_puts_every_field(tmp_path, monkeypatch):
    fake = FakeAdminAPI()
    fake.add("PUT", WEEKLY_PATH, {"weekly_prompt": WEEKLY})
    install(monkeypatch, fake, tmp_path)

    args = [
        "admin",
        "weekly-prompt",
        "set",
        "--title",
        "Week 12",
        "--description",
        "Shoot the light",
        "--prompt",
        "Golden hour selfies",
        "--cta-label",
        "Try it",
        "--destination",
        "/studio",
    ]

    blocked = run(args)
    assert blocked.exit_code == 2
    assert fake.requests == []

    confirmed = run(args + ["--yes"])
    assert confirmed.exit_code == 0, confirmed.output
    req = only(fake)
    assert (req["method"], req["path"]) == ("PUT", WEEKLY_PATH)
    assert req["content_type"].startswith("application/json")
    # PUT is a full replacement: every field is sent, `enabled` included.
    assert req["body"] == {
        "title": "Week 12",
        "description": "Shoot the light",
        "prompt": "Golden hour selfies",
        "cta_label": "Try it",
        "destination": "/studio",
        "enabled": True,
    }
    assert json.loads(confirmed.stdout) == WEEKLY


def test_weekly_prompt_set_sends_every_field_even_when_flags_are_omitted(tmp_path, monkeypatch):
    fake = FakeAdminAPI()
    fake.add("PUT", WEEKLY_PATH, {"weekly_prompt": WEEKLY})
    install(monkeypatch, fake, tmp_path)

    result = run(["admin", "weekly-prompt", "set", "--title", "T", "--prompt", "P", "--yes"])

    assert result.exit_code == 0, result.output
    assert only(fake)["body"] == {
        "title": "T",
        "description": "",
        "prompt": "P",
        "cta_label": "",
        "destination": "",
        "enabled": True,
    }


def test_weekly_prompt_set_can_disable_the_slot(tmp_path, monkeypatch):
    fake = FakeAdminAPI()
    fake.add("PUT", WEEKLY_PATH, {"weekly_prompt": WEEKLY})
    install(monkeypatch, fake, tmp_path)

    result = run(["admin", "weekly-prompt", "set", "--title", "T", "--prompt", "P", "--disabled", "--yes"])

    assert result.exit_code == 0, result.output
    assert only(fake)["body"]["enabled"] is False


def test_weekly_prompt_set_merges_a_file_payload_under_flag_overrides(tmp_path, monkeypatch):
    payload = tmp_path / "weekly.json"
    payload.write_text(
        json.dumps(
            {
                "title": "From File",
                "description": "d",
                "prompt": "p",
                "cta_label": "c",
                "destination": "/studio",
                "enabled": False,
            }
        )
    )
    fake = FakeAdminAPI()
    fake.add("PUT", WEEKLY_PATH, {"weekly_prompt": WEEKLY})
    install(monkeypatch, fake, tmp_path)

    result = run(["admin", "weekly-prompt", "set", "--file", str(payload), "--title", "Override", "--yes"])

    assert result.exit_code == 0, result.output
    assert only(fake)["body"] == {
        "title": "Override",
        "description": "d",
        "prompt": "p",
        "cta_label": "c",
        "destination": "/studio",
        "enabled": False,
    }


@pytest.mark.parametrize(
    "args,missing",
    [
        (["--prompt", "P"], "title"),
        (["--title", "T"], "prompt"),
        ([], "title"),
    ],
)
def test_weekly_prompt_set_requires_title_and_prompt(tmp_path, monkeypatch, args, missing):
    fake = FakeAdminAPI()
    install(monkeypatch, fake, tmp_path)

    result = run(["admin", "weekly-prompt", "set"] + args + ["--yes"])

    assert result.exit_code == 2
    assert fake.requests == []
    assert missing in result.output.lower()


@pytest.mark.parametrize(
    "destination",
    ["javascript:alert(1)", "//evil.example/x", "/\\evil.example/x", "data:text/html,x", "mailto:a@b.c"],
)
def test_weekly_prompt_set_rejects_an_unsafe_destination(tmp_path, monkeypatch, destination):
    fake = FakeAdminAPI()
    install(monkeypatch, fake, tmp_path)

    result = run(
        ["admin", "weekly-prompt", "set", "--title", "T", "--prompt", "P", "--destination", destination, "--yes"]
    )

    assert result.exit_code == 2
    assert fake.requests == []
    assert "destination" in result.output.lower()


# --------------------------------------------------------------------------
# admin trending — campaign tags keyed on label/slug
# --------------------------------------------------------------------------

TAG = {
    "id": 11,
    "label": "Anime Portrait",
    "slug": "anime-portrait",
    "source": "manual",
    "score": 3,
    "order": 1,
    "is_active": True,
}

TAGS_PATH = "/api/admin/content/campaign/trending-tags"


def test_trending_list_reads_campaign_tags(tmp_path, monkeypatch):
    fake = FakeAdminAPI()
    fake.add("GET", TAGS_PATH, {"tags": [TAG], "total": 1})
    install(monkeypatch, fake, tmp_path)

    result = run(["admin", "trending", "list"])

    assert result.exit_code == 0, result.output
    req = only(fake)
    assert (req["method"], req["path"]) == ("GET", TAGS_PATH)
    assert req["query"] == {}
    assert req["authorization"] == f"Bearer {TOKEN}"
    assert json.loads(result.stdout) == {"tags": [TAG], "total": 1}


def test_trending_list_active_only_sets_the_query_flag(tmp_path, monkeypatch):
    fake = FakeAdminAPI()
    fake.add("GET", TAGS_PATH, {"tags": [TAG], "total": 1})
    install(monkeypatch, fake, tmp_path)

    result = run(["admin", "trending", "list", "--active"])

    assert result.exit_code == 0, result.output
    assert only(fake)["query"] == {"active": "true"}


def test_trending_list_renders_a_table_without_json_flag(tmp_path, monkeypatch):
    fake = FakeAdminAPI()
    fake.add("GET", TAGS_PATH, {"tags": [TAG], "total": 1})
    install(monkeypatch, fake, tmp_path)

    result = run(["admin", "trending", "list"], json_mode=False)

    assert result.exit_code == 0, result.output
    assert "anime-portrait" in result.output
    assert "manual" in result.output


def test_trending_add_posts_label_slug_score_and_order(tmp_path, monkeypatch):
    fake = FakeAdminAPI()
    fake.add("POST", TAGS_PATH, {"tag": TAG}, status=201)
    install(monkeypatch, fake, tmp_path)

    result = run(
        ["admin", "trending", "add", "Anime Portrait", "--slug", "anime-portrait", "--score", "3", "--order", "1"]
    )

    assert result.exit_code == 0, result.output
    req = only(fake)
    assert (req["method"], req["path"]) == ("POST", TAGS_PATH)
    assert req["body"] == {"label": "Anime Portrait", "slug": "anime-portrait", "score": 3, "order": 1}
    assert json.loads(result.stdout) == TAG


def test_trending_add_omits_slug_so_the_backend_derives_it(tmp_path, monkeypatch):
    fake = FakeAdminAPI()
    fake.add("POST", TAGS_PATH, {"tag": TAG}, status=201)
    install(monkeypatch, fake, tmp_path)

    result = run(["admin", "trending", "add", "Anime Portrait", "--inactive"])

    assert result.exit_code == 0, result.output
    assert only(fake)["body"] == {"label": "Anime Portrait", "is_active": False}


def test_trending_update_puts_only_supplied_fields(tmp_path, monkeypatch):
    fake = FakeAdminAPI()
    fake.add("PUT", f"{TAGS_PATH}/11", {"tag": TAG})
    install(monkeypatch, fake, tmp_path)

    result = run(["admin", "trending", "update", "11", "--score", "11", "--inactive"])

    assert result.exit_code == 0, result.output
    req = only(fake)
    assert (req["method"], req["path"]) == ("PUT", f"{TAGS_PATH}/11")
    assert req["body"] == {"score": 11, "is_active": False}


def test_trending_update_can_patch_label_slug_and_order(tmp_path, monkeypatch):
    fake = FakeAdminAPI()
    fake.add("PUT", f"{TAGS_PATH}/11", {"tag": TAG})
    install(monkeypatch, fake, tmp_path)

    result = run(["admin", "trending", "update", "11", "--label", "Anime", "--slug", "anime", "--order", "0"])

    assert result.exit_code == 0, result.output
    assert only(fake)["body"] == {"label": "Anime", "slug": "anime", "order": 0}


def test_trending_update_requires_at_least_one_field(tmp_path, monkeypatch):
    fake = FakeAdminAPI()
    install(monkeypatch, fake, tmp_path)

    result = run(["admin", "trending", "update", "11"])

    assert result.exit_code == 2
    assert fake.requests == []
    assert "nothing to update" in result.output.lower()


@pytest.mark.parametrize("flag,value", [("--score", "-1"), ("--order", "-2")])
def test_trending_rejects_negative_score_and_order(tmp_path, monkeypatch, flag, value):
    fake = FakeAdminAPI()
    install(monkeypatch, fake, tmp_path)

    result = run(["admin", "trending", "add", "Anime", flag, value])

    assert result.exit_code == 2
    assert fake.requests == []
    assert "negative" in result.output.lower()


def test_trending_remove_requires_confirmation(tmp_path, monkeypatch):
    fake = FakeAdminAPI()
    fake.add("DELETE", f"{TAGS_PATH}/11", {"success": True})
    install(monkeypatch, fake, tmp_path)

    blocked = run(["admin", "trending", "remove", "11"])
    assert blocked.exit_code == 2
    assert fake.requests == []

    confirmed = run(["admin", "trending", "remove", "11", "--yes"])
    assert confirmed.exit_code == 0, confirmed.output
    req = only(fake)
    assert (req["method"], req["path"]) == ("DELETE", f"{TAGS_PATH}/11")
    assert json.loads(confirmed.stdout) == {"success": True}


def test_trending_generate_requires_confirmation_then_posts(tmp_path, monkeypatch):
    result_payload = {
        "generated": 6,
        "created": 2,
        "updated": 4,
        "deactivated": 1,
        "manual_kept": 3,
        "tags": [TAG],
    }
    fake = FakeAdminAPI()
    fake.add("POST", f"{TAGS_PATH}/generate", {"result": result_payload})
    install(monkeypatch, fake, tmp_path)

    blocked = run(["admin", "trending", "generate"])
    assert blocked.exit_code == 2
    assert fake.requests == []

    confirmed = run(["admin", "trending", "generate", "--limit", "6", "--yes"])
    assert confirmed.exit_code == 0, confirmed.output
    req = only(fake)
    assert (req["method"], req["path"]) == ("POST", f"{TAGS_PATH}/generate")
    assert req["body"] == {"limit": 6}
    assert json.loads(confirmed.stdout) == result_payload


def test_trending_generate_sends_an_empty_object_without_a_limit(tmp_path, monkeypatch):
    fake = FakeAdminAPI()
    fake.add("POST", f"{TAGS_PATH}/generate", {"result": {"generated": 0, "tags": []}})
    install(monkeypatch, fake, tmp_path)

    result = run(["admin", "trending", "generate", "--yes"])

    assert result.exit_code == 0, result.output
    assert only(fake)["body"] == {}


def test_trending_recalculate_still_targets_the_template_score_route(tmp_path, monkeypatch):
    """`recalculate` (template trending scores) is a different endpoint from
    `generate` (campaign tag set); both must keep working."""
    fake = FakeAdminAPI()
    fake.add("POST", "/api/admin/content/trending/recalculate", {"success": True})
    install(monkeypatch, fake, tmp_path)

    blocked = run(["admin", "trending", "recalculate"])
    assert blocked.exit_code == 2
    assert fake.requests == []

    confirmed = run(["admin", "trending", "recalculate", "--yes"])
    assert confirmed.exit_code == 0, confirmed.output
    req = only(fake)
    assert (req["method"], req["path"]) == ("POST", "/api/admin/content/trending/recalculate")


# --------------------------------------------------------------------------
# admin hero — PUT is a full replacement (headline required)
# --------------------------------------------------------------------------

HERO = {
    "eyebrow": "New",
    "headline": "Summer Campaign",
    "body": "Book your booth",
    "cta_label": "Start",
    "target_type": "studio",
    "destination": "/studio",
    "template_id": "",
    "button_animation": "shimmer",
    "enabled": True,
    "backgrounds": [{"position": 0, "media_type": "image", "url": "https://cdn.example/h.png"}],
}

HERO_PATH = "/api/admin/content/campaign/hero"


def test_hero_get(tmp_path, monkeypatch):
    fake = FakeAdminAPI()
    fake.add("GET", HERO_PATH, {"hero": HERO})
    install(monkeypatch, fake, tmp_path)

    result = run(["admin", "hero", "get"])

    assert result.exit_code == 0, result.output
    req = only(fake)
    assert (req["method"], req["path"]) == ("GET", HERO_PATH)
    assert req["authorization"] == f"Bearer {TOKEN}"
    assert json.loads(result.stdout) == HERO


def test_hero_set_requires_confirmation_then_puts_every_field(tmp_path, monkeypatch):
    fake = FakeAdminAPI()
    fake.add("PUT", HERO_PATH, {"hero": HERO})
    install(monkeypatch, fake, tmp_path)

    args = [
        "admin",
        "hero",
        "set",
        "--eyebrow",
        "New",
        "--headline",
        "Summer Campaign",
        "--body",
        "Book your booth",
        "--cta-label",
        "Start",
        "--target-type",
        "studio",
        "--destination",
        "/studio",
        "--button-animation",
        "shimmer",
        "--background",
        "https://cdn.example/h.png",
    ]

    blocked = run(args)
    assert blocked.exit_code == 2
    assert fake.requests == []

    confirmed = run(args + ["--yes"])
    assert confirmed.exit_code == 0, confirmed.output
    req = only(fake)
    assert (req["method"], req["path"]) == ("PUT", HERO_PATH)
    assert req["body"] == {
        "eyebrow": "New",
        "headline": "Summer Campaign",
        "body": "Book your booth",
        "cta_label": "Start",
        "target_type": "studio",
        "destination": "/studio",
        "template_id": "",
        "button_animation": "shimmer",
        "enabled": True,
        "backgrounds": [{"position": 0, "media_type": "image", "url": "https://cdn.example/h.png"}],
    }
    assert json.loads(confirmed.stdout) == HERO


def test_hero_set_defaults_target_type_and_animation(tmp_path, monkeypatch):
    fake = FakeAdminAPI()
    fake.add("PUT", HERO_PATH, {"hero": HERO})
    install(monkeypatch, fake, tmp_path)

    result = run(["admin", "hero", "set", "--headline", "H", "--yes"])

    assert result.exit_code == 0, result.output
    assert only(fake)["body"] == {
        "eyebrow": "",
        "headline": "H",
        "body": "",
        "cta_label": "",
        "target_type": "studio",
        "destination": "",
        "template_id": "",
        "button_animation": "none",
        "enabled": True,
        "backgrounds": [],
    }


def test_hero_set_uploads_local_backgrounds_and_positions_them_in_order(tmp_path, monkeypatch):
    first = tmp_path / "bg1.png"
    first.write_bytes(b"\x89PNGfake")
    fake = FakeAdminAPI()
    fake.add("POST", MEDIA_UPLOAD, {"url": "https://cdn.example/bg1.png"})
    fake.add("PUT", HERO_PATH, {"hero": HERO})
    install(monkeypatch, fake, tmp_path)

    result = run(
        [
            "admin",
            "hero",
            "set",
            "--headline",
            "H",
            "--background",
            str(first),
            "--background",
            "https://cdn.example/clip.mp4",
            "--yes",
        ]
    )

    assert result.exit_code == 0, result.output
    assert [(r["method"], r["path"]) for r in fake.requests] == [
        ("POST", MEDIA_UPLOAD),
        ("PUT", HERO_PATH),
    ]
    assert fake.requests[1]["body"]["backgrounds"] == [
        {"position": 0, "media_type": "image", "url": "https://cdn.example/bg1.png"},
        {"position": 1, "media_type": "video", "url": "https://cdn.example/clip.mp4"},
    ]


def test_hero_set_takes_rich_backgrounds_from_a_file_payload(tmp_path, monkeypatch):
    payload = tmp_path / "hero.json"
    payload.write_text(
        json.dumps(
            {
                "headline": "Summer Campaign",
                "target_type": "template",
                "template_id": "tpl_a",
                "backgrounds": [
                    {
                        "position": 0,
                        "media_type": "image",
                        "url": "https://cdn.example/a.png",
                        "public_creation_id": 42,
                    }
                ],
            }
        )
    )
    fake = FakeAdminAPI()
    fake.add("PUT", HERO_PATH, {"hero": HERO})
    install(monkeypatch, fake, tmp_path)

    result = run(["admin", "hero", "set", "--file", str(payload), "--yes"])

    assert result.exit_code == 0, result.output
    body = only(fake)["body"]
    assert body["target_type"] == "template"
    assert body["template_id"] == "tpl_a"
    assert body["backgrounds"] == [
        {"position": 0, "media_type": "image", "url": "https://cdn.example/a.png", "public_creation_id": 42}
    ]


def test_hero_set_requires_a_headline(tmp_path, monkeypatch):
    fake = FakeAdminAPI()
    install(monkeypatch, fake, tmp_path)

    result = run(["admin", "hero", "set", "--yes"])

    assert result.exit_code == 2
    assert fake.requests == []
    assert "headline" in result.output.lower()


@pytest.mark.parametrize("animation", ["none", "pulse", "shimmer", "bounce", "glow"])
def test_hero_set_accepts_every_button_animation(tmp_path, monkeypatch, animation):
    fake = FakeAdminAPI()
    fake.add("PUT", HERO_PATH, {"hero": HERO})
    install(monkeypatch, fake, tmp_path)

    result = run(["admin", "hero", "set", "--headline", "H", "--button-animation", animation, "--yes"])

    assert result.exit_code == 0, result.output
    assert only(fake)["body"]["button_animation"] == animation


@pytest.mark.parametrize("target", ["studio", "template", "offer", "external"])
def test_hero_set_accepts_every_target_type(tmp_path, monkeypatch, target):
    fake = FakeAdminAPI()
    fake.add("PUT", HERO_PATH, {"hero": HERO})
    install(monkeypatch, fake, tmp_path)

    args = ["admin", "hero", "set", "--headline", "H", "--target-type", target]
    if target == "template":
        args += ["--template-id", "tpl_a"]
    elif target == "offer":
        args += ["--destination", "/offers/summer"]
    elif target == "external":
        args += ["--destination", "https://pictureme.now/x"]

    result = run(args + ["--yes"])

    assert result.exit_code == 0, result.output
    assert only(fake)["body"]["target_type"] == target


def test_hero_set_rejects_an_unknown_button_animation(tmp_path, monkeypatch):
    fake = FakeAdminAPI()
    install(monkeypatch, fake, tmp_path)

    result = run(["admin", "hero", "set", "--headline", "H", "--button-animation", "sparkle", "--yes"])

    assert result.exit_code == 2
    assert fake.requests == []
    assert "shimmer" in result.output


def test_hero_set_rejects_an_unknown_target_type(tmp_path, monkeypatch):
    fake = FakeAdminAPI()
    install(monkeypatch, fake, tmp_path)

    result = run(["admin", "hero", "set", "--headline", "H", "--target-type", "popup", "--yes"])

    assert result.exit_code == 2
    assert fake.requests == []
    assert "studio" in result.output


def test_hero_set_requires_a_template_id_for_a_template_target(tmp_path, monkeypatch):
    fake = FakeAdminAPI()
    install(monkeypatch, fake, tmp_path)

    result = run(["admin", "hero", "set", "--headline", "H", "--target-type", "template", "--yes"])

    assert result.exit_code == 2
    assert fake.requests == []
    assert "template_id" in result.output


def test_hero_set_requires_an_absolute_url_for_an_external_target(tmp_path, monkeypatch):
    fake = FakeAdminAPI()
    install(monkeypatch, fake, tmp_path)

    result = run(
        ["admin", "hero", "set", "--headline", "H", "--target-type", "external", "--destination", "/studio", "--yes"]
    )

    assert result.exit_code == 2
    assert fake.requests == []
    assert "absolute" in result.output.lower()


def test_hero_set_rejects_a_template_id_on_a_studio_target(tmp_path, monkeypatch):
    fake = FakeAdminAPI()
    install(monkeypatch, fake, tmp_path)

    result = run(["admin", "hero", "set", "--headline", "H", "--template-id", "tpl_a", "--yes"])

    assert result.exit_code == 2
    assert fake.requests == []
    assert "template_id" in result.output


def test_hero_set_rejects_more_than_eight_backgrounds(tmp_path, monkeypatch):
    fake = FakeAdminAPI()
    install(monkeypatch, fake, tmp_path)

    args = ["admin", "hero", "set", "--headline", "H"]
    for i in range(9):
        args += ["--background", f"https://cdn.example/{i}.png"]

    result = run(args + ["--yes"])

    assert result.exit_code == 2
    assert fake.requests == []
    assert "8" in result.output


def test_hero_set_rejects_an_unsafe_background_url(tmp_path, monkeypatch):
    fake = FakeAdminAPI()
    install(monkeypatch, fake, tmp_path)

    result = run(["admin", "hero", "set", "--headline", "H", "--background", "//evil.example/x.png", "--yes"])

    assert result.exit_code == 2
    assert fake.requests == []


# --------------------------------------------------------------------------
# admin template options — the campaign picker projection
# --------------------------------------------------------------------------

OPTION = {
    "id": "tpl_a",
    "name": "Neon Gala",
    "template_type": "photo",
    "media_type": "image",
    "tags": ["neon"],
    "is_featured": True,
    "featured_order": 1,
}

OPTIONS_PATH = "/api/admin/content/campaign/templates"


def test_template_options_reads_the_campaign_picker(tmp_path, monkeypatch):
    fake = FakeAdminAPI()
    fake.add("GET", OPTIONS_PATH, {"templates": [OPTION], "total": 1})
    install(monkeypatch, fake, tmp_path)

    result = run(
        ["admin", "template", "options", "--type", "photo", "--category", "events", "--search", "gala", "--limit", "20"]
    )

    assert result.exit_code == 0, result.output
    req = only(fake)
    assert (req["method"], req["path"]) == ("GET", OPTIONS_PATH)
    assert req["query"] == {"type": "photo", "category": "events", "search": "gala", "limit": "20"}
    assert req["authorization"] == f"Bearer {TOKEN}"
    assert json.loads(result.stdout) == {"templates": [OPTION], "total": 1}


def test_template_options_renders_a_table_without_json_flag(tmp_path, monkeypatch):
    fake = FakeAdminAPI()
    fake.add("GET", OPTIONS_PATH, {"templates": [OPTION], "total": 1})
    install(monkeypatch, fake, tmp_path)

    result = run(["admin", "template", "options"], json_mode=False)

    assert result.exit_code == 0, result.output
    assert "Neon Gala" in result.output


# --------------------------------------------------------------------------
# admin discovery: media, creations, models, stats
# --------------------------------------------------------------------------


def test_media_upload_helper_prints_the_returned_url(tmp_path, monkeypatch):
    image = tmp_path / "asset.png"
    image.write_bytes(b"\x89PNGfake")
    fake = FakeAdminAPI()
    fake.add("POST", MEDIA_UPLOAD, {"url": "https://cdn.example/asset.png"})
    install(monkeypatch, fake, tmp_path)

    result = run(["admin", "media", "upload", str(image)], json_mode=False)

    assert result.exit_code == 0, result.output
    req = only(fake)
    assert (req["method"], req["path"]) == ("POST", MEDIA_UPLOAD)
    assert req["authorization"] == f"Bearer {TOKEN}"
    assert "https://cdn.example/asset.png" in result.output


def test_creation_list_reads_the_public_feed(tmp_path, monkeypatch):
    fake = FakeAdminAPI()
    fake.add("GET", "/api/creations/public", {"creations": [{"id": 1, "is_featured": True}]})
    install(monkeypatch, fake, tmp_path)

    result = run(["admin", "creation", "list", "--limit", "5", "--offset", "10", "--featured"])

    assert result.exit_code == 0, result.output
    req = only(fake)
    assert (req["method"], req["path"]) == ("GET", "/api/creations/public")
    assert req["query"] == {"limit": "5", "offset": "10", "featured": "true"}
    assert json.loads(result.stdout) == {"creations": [{"id": 1, "is_featured": True}]}


def test_model_list_reads_the_public_registry(tmp_path, monkeypatch):
    fake = FakeAdminAPI()
    fake.add("GET", "/api/v3/public/models", {"models": [{"model_id": "nano-banana"}]})
    install(monkeypatch, fake, tmp_path)

    result = run(["admin", "model", "list"])

    assert result.exit_code == 0, result.output
    assert only(fake)["path"] == "/api/v3/public/models"
    assert json.loads(result.stdout) == {"models": [{"model_id": "nano-banana"}]}


def test_stats_reads_admin_content_stats(tmp_path, monkeypatch):
    fake = FakeAdminAPI()
    fake.add("GET", "/api/admin/content/stats", {"templates": 12, "creations": 340})
    install(monkeypatch, fake, tmp_path)

    result = run(["admin", "stats"])

    assert result.exit_code == 0, result.output
    req = only(fake)
    assert (req["method"], req["path"]) == ("GET", "/api/admin/content/stats")
    assert json.loads(result.stdout) == {"templates": 12, "creations": 340}


# --------------------------------------------------------------------------
# transport + auth safety
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "args",
    [
        ["admin", "template", "list"],
        ["admin", "featured", "list"],
        ["admin", "weekly-prompt", "get"],
        ["admin", "trending", "list"],
        ["admin", "hero", "get"],
        ["admin", "template", "options"],
        ["admin", "trending", "recalculate", "--yes"],
    ],
)
def test_admin_commands_reject_bearer_tokens_over_plain_http(tmp_path, monkeypatch, args):
    fake = FakeAdminAPI()
    install(monkeypatch, fake, tmp_path)

    result = CliRunner().invoke(app, ["--host", "http://remote.example", "--json"] + args)

    assert result.exit_code == 2
    assert fake.requests == []
    assert json.loads(result.stdout) == {
        "error": "invalid_host",
        "message": "PictureME bearer tokens require HTTPS for remote hosts",
    }


@pytest.mark.parametrize(
    "args",
    [
        ["admin", "template", "list"],
        ["admin", "featured", "list"],
        ["admin", "weekly-prompt", "get"],
        ["admin", "trending", "list"],
        ["admin", "hero", "get"],
        ["admin", "template", "options"],
        ["admin", "stats"],
    ],
)
def test_admin_commands_require_authentication(tmp_path, monkeypatch, args):
    fake = FakeAdminAPI()
    install(monkeypatch, fake, tmp_path)
    monkeypatch.delenv("PICTUREME_API_KEY", raising=False)

    result = run(args)

    assert result.exit_code == 2
    assert fake.requests == []
    assert "auth login" in result.output


def test_admin_api_errors_surface_without_a_traceback(tmp_path, monkeypatch):
    fake = FakeAdminAPI()
    fake.add("GET", "/api/admin/marketplace/templates", {"error": "Insufficient permissions"}, status=403)
    install(monkeypatch, fake, tmp_path)

    result = run(["admin", "template", "list"], json_mode=False)

    assert result.exit_code == 1
    assert "HTTP 403" in result.output
    assert "Insufficient permissions" not in result.output


def test_campaign_service_unavailable_surfaces_the_backend_message(tmp_path, monkeypatch):
    """The Go handlers fail closed with 503 when no campaign store is wired."""
    fake = FakeAdminAPI()
    fake.add("GET", HERO_PATH, {"error": "campaign content service is not configured"}, status=503)
    install(monkeypatch, fake, tmp_path)

    result = run(["admin", "hero", "get"], json_mode=False)

    assert result.exit_code == 1
    assert "HTTP 503" in result.output
    assert "campaign content service is not configured" not in result.output
    assert "503" in result.output


def test_missing_content_scope_surfaces_as_a_plain_error(tmp_path, monkeypatch):
    """Campaign routes are gated on content:read / content:write for pmk_ keys."""
    fake = FakeAdminAPI()
    fake.add("PUT", WEEKLY_PATH, {"error": "Insufficient scope"}, status=403)
    install(monkeypatch, fake, tmp_path)

    result = run(["admin", "weekly-prompt", "set", "--title", "T", "--prompt", "P", "--yes"], json_mode=False)

    assert result.exit_code == 1
    assert "HTTP 403" in result.output
    assert "Insufficient scope" not in result.output


def test_admin_help_lists_every_subgroup(tmp_path, monkeypatch):
    fake = FakeAdminAPI()
    install(monkeypatch, fake, tmp_path)

    result = CliRunner().invoke(app, ["admin", "--help"])

    assert result.exit_code == 0, result.output
    for group in (
        "template", "featured", "weekly-prompt", "trending", "hero", "media", "creation", "model", "stats", "api"
    ):
        assert group in result.output


# --------------------------------------------------------------------------
# native image + video uploads (POST /api/media/upload)
# --------------------------------------------------------------------------

# The backend allowlist is keyed on the declared Content-Type and never on the
# filename, so the CLI must send the canonical MIME for each extension.
UPLOAD_MIME_CASES = [
    ("shot.jpg", "image/jpeg"),
    ("shot.jpeg", "image/jpeg"),
    ("shot.png", "image/png"),
    ("shot.gif", "image/gif"),
    ("shot.webp", "image/webp"),
    ("shot.svg", "image/svg+xml"),
    ("clip.mp4", "video/mp4"),
    ("clip.mov", "video/quicktime"),
    ("clip.webm", "video/webm"),
]


@pytest.mark.parametrize("filename,mime", UPLOAD_MIME_CASES)
def test_media_upload_declares_the_canonical_mime_for_each_allowed_type(
    tmp_path, monkeypatch, filename, mime
):
    asset = tmp_path / filename
    asset.write_bytes(b"fake-bytes")
    fake = FakeAdminAPI()
    fake.add("POST", MEDIA_UPLOAD, {"url": f"https://cdn.example/{filename}"})
    install(monkeypatch, fake, tmp_path)

    result = run(["admin", "media", "upload", str(asset)])

    assert result.exit_code == 0, result.output
    req = only(fake)
    assert (req["method"], req["path"]) == ("POST", MEDIA_UPLOAD)
    assert req["content_type"].startswith("multipart/form-data")
    assert req["authorization"] == f"Bearer {TOKEN}"
    assert f'filename="{filename}"'.encode() in req["body"]
    assert mime.encode() in req["body"]


@pytest.mark.parametrize("filename", ["clip.mp4", "clip.mov", "clip.webm"])
def test_template_create_uploads_a_local_video_preview(tmp_path, monkeypatch, filename):
    clip = tmp_path / filename
    clip.write_bytes(b"\x00\x00\x00 ftypfake")
    fake = FakeAdminAPI()
    fake.add("POST", MEDIA_UPLOAD, {"url": f"https://cdn.example/{filename}", "media_kind": "video"})
    fake.add("POST", "/api/admin/marketplace/templates", TEMPLATE_A, status=201)
    install(monkeypatch, fake, tmp_path)

    result = run(
        ["admin", "template", "create", "--name", "Clip", "--media-type", "video", "--preview", str(clip)]
    )

    assert result.exit_code == 0, result.output
    assert [(r["method"], r["path"]) for r in fake.requests] == [
        ("POST", MEDIA_UPLOAD),
        ("POST", "/api/admin/marketplace/templates"),
    ]
    assert fake.requests[1]["body"]["preview_url"] == f"https://cdn.example/{filename}"


def test_template_create_uploads_a_local_video_reference(tmp_path, monkeypatch):
    clip = tmp_path / "ref.webm"
    clip.write_bytes(b"fake")
    fake = FakeAdminAPI()
    fake.add("POST", MEDIA_UPLOAD, {"url": "https://cdn.example/ref.webm"})
    fake.add("POST", "/api/admin/marketplace/templates", TEMPLATE_A, status=201)
    install(monkeypatch, fake, tmp_path)

    result = run(["admin", "template", "create", "--name", "Clip", "--reference", str(clip)])

    assert result.exit_code == 0, result.output
    assert fake.requests[1]["body"]["reference_images"] == ["https://cdn.example/ref.webm"]


def test_hero_background_uploads_a_local_video_and_marks_it_video(tmp_path, monkeypatch):
    clip = tmp_path / "bg.mp4"
    clip.write_bytes(b"\x00\x00\x00 ftypfake")
    fake = FakeAdminAPI()
    fake.add("POST", MEDIA_UPLOAD, {"url": "https://cdn.example/bg.mp4", "media_kind": "video"})
    fake.add("PUT", HERO_PATH, {"hero": HERO})
    install(monkeypatch, fake, tmp_path)

    result = run(["admin", "hero", "set", "--headline", "H", "--background", str(clip), "--yes"])

    assert result.exit_code == 0, result.output
    assert [(r["method"], r["path"]) for r in fake.requests] == [("POST", MEDIA_UPLOAD), ("PUT", HERO_PATH)]
    assert b"video/mp4" in fake.requests[0]["body"]
    assert fake.requests[1]["body"]["backgrounds"] == [
        {"position": 0, "media_type": "video", "url": "https://cdn.example/bg.mp4"}
    ]


def test_hero_background_trusts_the_backend_media_kind_over_the_extension(tmp_path, monkeypatch):
    """The upload response is authoritative: the stored object's kind decides."""
    asset = tmp_path / "bg.webp"
    asset.write_bytes(b"RIFFfake")
    fake = FakeAdminAPI()
    fake.add("POST", MEDIA_UPLOAD, {"url": "https://cdn.example/bg.webp", "media_kind": "image"})
    fake.add("PUT", HERO_PATH, {"hero": HERO})
    install(monkeypatch, fake, tmp_path)

    result = run(["admin", "hero", "set", "--headline", "H", "--background", str(asset), "--yes"])

    assert result.exit_code == 0, result.output
    assert fake.requests[1]["body"]["backgrounds"][0]["media_type"] == "image"


def test_hero_mixes_local_video_local_image_and_remote_urls_in_order(tmp_path, monkeypatch):
    clip = tmp_path / "a.mov"
    clip.write_bytes(b"fake")
    still = tmp_path / "b.png"
    still.write_bytes(b"\x89PNGfake")
    fake = FakeAdminAPI()
    fake.routes[("POST", MEDIA_UPLOAD)] = (200, {"url": "https://cdn.example/up", "media_kind": "video"})
    fake.add("PUT", HERO_PATH, {"hero": HERO})
    install(monkeypatch, fake, tmp_path)

    result = run(
        [
            "admin",
            "hero",
            "set",
            "--headline",
            "H",
            "--background",
            str(clip),
            "--background",
            str(still),
            "--background",
            "https://cdn.example/remote.webm",
            "--yes",
        ]
    )

    assert result.exit_code == 0, result.output
    uploads = [r for r in fake.requests if r["path"] == MEDIA_UPLOAD]
    assert len(uploads) == 2
    assert b"video/quicktime" in uploads[0]["body"]
    assert b"image/png" in uploads[1]["body"]
    backgrounds = fake.requests[-1]["body"]["backgrounds"]
    assert [b["position"] for b in backgrounds] == [0, 1, 2]
    # The remote URL keeps extension-derived typing; uploads use media_kind.
    assert backgrounds[2] == {"position": 2, "media_type": "video", "url": "https://cdn.example/remote.webm"}


@pytest.mark.parametrize("filename", ["clip.m4v", "clip.avi", "clip.mkv", "doc.pdf", "sheet.csv"])
def test_local_media_off_the_allowlist_is_rejected_before_any_request(tmp_path, monkeypatch, filename):
    asset = tmp_path / filename
    asset.write_bytes(b"fake")
    fake = FakeAdminAPI()
    install(monkeypatch, fake, tmp_path)

    result = run(["admin", "template", "create", "--name", "X", "--preview", str(asset)])

    assert result.exit_code == 2
    assert fake.requests == []
    assert "unsupported media type" in result.output.lower()


def test_unsupported_media_error_names_the_allowed_extensions(tmp_path, monkeypatch):
    asset = tmp_path / "clip.avi"
    asset.write_bytes(b"fake")
    fake = FakeAdminAPI()
    install(monkeypatch, fake, tmp_path)

    result = run(["admin", "media", "upload", str(asset)], json_mode=False)

    assert result.exit_code == 2
    output = result.output
    for allowed in (".png", ".webp", ".mp4", ".mov", ".webm"):
        assert allowed in output


@pytest.mark.parametrize(
    "command",
    [
        ["admin", "template", "create", "--name", "X", "--preview", "data:image/png;base64,AAAA"],
        ["admin", "template", "create", "--name", "X", "--reference", "data:image/png;base64,AAAA"],
        ["admin", "featured", "add", "tpl_a", "--thumbnail", "data:image/png;base64,AAAA"],
        ["admin", "hero", "set", "--headline", "H", "--background", "data:video/mp4;base64,AAAA", "--yes"],
    ],
)
def test_data_uris_are_rejected_for_durable_public_config(tmp_path, monkeypatch, command):
    """A data: URI would be inlined into durable public config forever; upload
    the bytes instead so the record holds a real object URL."""
    fake = FakeAdminAPI()
    install(monkeypatch, fake, tmp_path)

    result = run(command)

    assert result.exit_code == 2
    assert fake.requests == []
    assert "data:" in result.output


def test_media_upload_forwards_an_allowlisted_category(tmp_path, monkeypatch):
    asset = tmp_path / "logo.png"
    asset.write_bytes(b"\x89PNGfake")
    fake = FakeAdminAPI()
    fake.add("POST", MEDIA_UPLOAD, {"url": "https://cdn.example/logo.png"})
    install(monkeypatch, fake, tmp_path)

    result = run(["admin", "media", "upload", str(asset), "--category", "branding"])

    assert result.exit_code == 0, result.output
    body = only(fake)["body"]
    assert b'name="category"' in body
    assert b"branding" in body


def test_media_upload_rejects_an_unknown_category(tmp_path, monkeypatch):
    asset = tmp_path / "logo.png"
    asset.write_bytes(b"\x89PNGfake")
    fake = FakeAdminAPI()
    install(monkeypatch, fake, tmp_path)

    result = run(["admin", "media", "upload", str(asset), "--category", "nonsense"])

    assert result.exit_code == 2
    assert fake.requests == []
    assert "branding" in result.output


def test_media_upload_reports_kind_and_url_in_json_mode(tmp_path, monkeypatch):
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"fake")
    fake = FakeAdminAPI()
    fake.add(
        "POST",
        MEDIA_UPLOAD,
        {"url": "https://cdn.example/clip.mp4", "media_kind": "video", "content_type": "video/mp4"},
    )
    install(monkeypatch, fake, tmp_path)

    result = run(["admin", "media", "upload", str(clip)])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["url"] == "https://cdn.example/clip.mp4"
    assert payload["media_kind"] == "video"


def test_backend_415_surfaces_without_a_traceback(tmp_path, monkeypatch):
    """The backend rejects an off-allowlist declared type with 415."""
    asset = tmp_path / "asset.png"
    asset.write_bytes(b"\x89PNGfake")
    fake = FakeAdminAPI()
    fake.add(
        "POST",
        MEDIA_UPLOAD,
        {"error": "Unsupported media type", "allowed_types": ["image/png", "video/mp4"]},
        status=415,
    )
    install(monkeypatch, fake, tmp_path)

    result = run(["admin", "media", "upload", str(asset)], json_mode=False)

    assert result.exit_code == 1
    assert "HTTP 415" in result.output
    assert "Unsupported media type" not in result.output
    assert "415" in result.output


# --------------------------------------------------------------------------
# admin api — generic authenticated escape hatch
# --------------------------------------------------------------------------
#
# The typed commands cover the campaign surface; this covers everything else a
# Super Admin can reach, without the CLI having to grow a command per endpoint.
# Its safety properties are the point, so they are pinned here: the request can
# only ever go to the configured PictureME host, mutations are explicit, and a
# response never prints a provider key to a terminal or CI log.


def test_api_get_issues_an_authenticated_request(tmp_path, monkeypatch):
    fake = FakeAdminAPI()
    fake.add("GET", "/api/admin/users", {"users": [{"id": 1}]})
    install(monkeypatch, fake, tmp_path)

    result = run(["admin", "api", "get", "/api/admin/users"])

    assert result.exit_code == 0, result.output
    req = only(fake)
    assert (req["method"], req["path"]) == ("GET", "/api/admin/users")
    assert req["authorization"] == f"Bearer {TOKEN}"
    assert json.loads(result.stdout) == {"users": [{"id": 1}]}


def test_api_request_takes_the_method_as_an_argument(tmp_path, monkeypatch):
    fake = FakeAdminAPI()
    fake.add("GET", "/api/admin/stats", {"ok": True})
    install(monkeypatch, fake, tmp_path)

    result = run(["admin", "api", "request", "get", "/api/admin/stats"])

    assert result.exit_code == 0, result.output
    assert only(fake)["method"] == "GET"


def test_api_merges_query_options_and_an_inline_query_string(tmp_path, monkeypatch):
    fake = FakeAdminAPI()
    fake.add("GET", "/api/admin/users", {"users": []})
    install(monkeypatch, fake, tmp_path)

    result = run(
        ["admin", "api", "get", "/api/admin/users?role=admin", "--query", "limit=5", "--query", "offset=10"]
    )

    assert result.exit_code == 0, result.output
    req = only(fake)
    assert req["path"] == "/api/admin/users"
    assert req["query"] == {"role": "admin", "limit": "5", "offset": "10"}


def test_api_rejects_a_malformed_query_option(tmp_path, monkeypatch):
    fake = FakeAdminAPI()
    install(monkeypatch, fake, tmp_path)

    result = run(["admin", "api", "get", "/api/admin/users", "--query", "limit"])

    assert result.exit_code == 2
    assert fake.requests == []
    assert "key=value" in result.output


@pytest.mark.parametrize("verb,method", [("post", "POST"), ("put", "PUT"), ("patch", "PATCH"), ("delete", "DELETE")])
def test_api_mutations_require_confirmation_then_send_the_body(tmp_path, monkeypatch, verb, method):
    fake = FakeAdminAPI()
    fake.add(method, "/api/admin/tiers/3", {"success": True})
    install(monkeypatch, fake, tmp_path)

    args = ["admin", "api", verb, "/api/admin/tiers/3", "--data", '{"name":"Pro"}']

    blocked = run(args)
    assert blocked.exit_code == 2
    assert fake.requests == []
    assert "--yes" in blocked.output

    confirmed = run(args + ["--yes"])
    assert confirmed.exit_code == 0, confirmed.output
    req = only(fake)
    assert (req["method"], req["path"]) == (method, "/api/admin/tiers/3")
    assert req["content_type"].startswith("application/json")
    assert req["body"] == {"name": "Pro"}


def test_api_get_never_requires_confirmation(tmp_path, monkeypatch):
    fake = FakeAdminAPI()
    fake.add("GET", "/api/admin/audit-logs", {"logs": []})
    install(monkeypatch, fake, tmp_path)

    result = run(["admin", "api", "get", "/api/admin/audit-logs"])

    assert result.exit_code == 0, result.output
    assert len(fake.requests) == 1


def test_api_sends_no_body_when_none_is_given(tmp_path, monkeypatch):
    fake = FakeAdminAPI()
    fake.add("POST", "/api/admin/queue/dlq/purge", {"purged": 0})
    install(monkeypatch, fake, tmp_path)

    result = run(["admin", "api", "post", "/api/admin/queue/dlq/purge", "--yes"])

    assert result.exit_code == 0, result.output
    assert only(fake)["body"] is None


def test_api_reads_a_body_from_a_file(tmp_path, monkeypatch):
    payload = tmp_path / "body.json"
    payload.write_text(json.dumps({"role": "creator"}))
    fake = FakeAdminAPI()
    fake.add("PUT", "/api/admin/users/7", {"ok": True})
    install(monkeypatch, fake, tmp_path)

    result = run(["admin", "api", "put", "/api/admin/users/7", "--file", str(payload), "--yes"])

    assert result.exit_code == 0, result.output
    assert only(fake)["body"] == {"role": "creator"}


def test_api_reads_a_body_from_stdin(tmp_path, monkeypatch):
    fake = FakeAdminAPI()
    fake.add("POST", "/api/admin/users", {"ok": True})
    install(monkeypatch, fake, tmp_path)

    result = run(
        ["admin", "api", "post", "/api/admin/users", "--file", "-", "--yes"],
        input=json.dumps({"email": "someone@example.com"}),
    )

    assert result.exit_code == 0, result.output
    assert only(fake)["body"] == {"email": "someone@example.com"}


def test_api_accepts_a_json_array_body(tmp_path, monkeypatch):
    fake = FakeAdminAPI()
    fake.add("POST", "/api/admin/assets/bulk", {"updated": 2})
    install(monkeypatch, fake, tmp_path)

    result = run(["admin", "api", "post", "/api/admin/assets/bulk", "--data", '[{"id":1},{"id":2}]', "--yes"])

    assert result.exit_code == 0, result.output
    assert only(fake)["body"] == [{"id": 1}, {"id": 2}]


def test_api_rejects_malformed_inline_json(tmp_path, monkeypatch):
    fake = FakeAdminAPI()
    install(monkeypatch, fake, tmp_path)

    result = run(["admin", "api", "post", "/api/admin/users", "--data", "{not json", "--yes"])

    assert result.exit_code == 2
    assert fake.requests == []
    assert "not valid JSON" in result.output


def test_api_rejects_both_data_and_file(tmp_path, monkeypatch):
    payload = tmp_path / "body.json"
    payload.write_text("{}")
    fake = FakeAdminAPI()
    install(monkeypatch, fake, tmp_path)

    result = run(["admin", "api", "post", "/api/admin/users", "--data", "{}", "--file", str(payload), "--yes"])

    assert result.exit_code == 2
    assert fake.requests == []


# --- path safety: the token must only ever reach the configured host ---


@pytest.mark.parametrize(
    "path",
    [
        "//evil.example/steal",             # protocol-relative: resolves off-host
        "https://evil.example/steal",       # absolute URL to another origin
        "http://evil.example/steal",
        "api/admin/users",                  # no leading slash: relative to base
        "/api/admin/../../etc/passwd",      # traversal
        "/api/admin/users\n/x",             # control character
        "/api/admin/users with space",
        "/healthz",                         # outside the API surface
        "",
    ],
)
def test_api_rejects_unsafe_paths_before_any_request(tmp_path, monkeypatch, path):
    fake = FakeAdminAPI()
    install(monkeypatch, fake, tmp_path)

    result = run(["admin", "api", "get", path])

    assert result.exit_code == 2
    assert fake.requests == []


def test_api_path_error_explains_the_rule(tmp_path, monkeypatch):
    fake = FakeAdminAPI()
    install(monkeypatch, fake, tmp_path)

    result = run(["admin", "api", "get", "https://evil.example/steal"], json_mode=False)

    assert result.exit_code == 2
    assert "/api/" in result.output


@pytest.mark.parametrize("method", ["trace", "connect", "options", "head", "fetch"])
def test_api_request_rejects_methods_off_the_allowlist(tmp_path, monkeypatch, method):
    fake = FakeAdminAPI()
    install(monkeypatch, fake, tmp_path)

    result = run(["admin", "api", "request", method, "/api/admin/users", "--yes"])

    assert result.exit_code == 2
    assert fake.requests == []


# --- secret hygiene: a response must not print provider keys ---

SECRET_RESPONSE = {
    "fal_key": "fixture-value-0",
    "api_key": "pmk_live_zzz",
    "access_token": "eyJhbGciOi",
    "client_secret": "cs_live_1",
    "password": "hunter2",
    "public_url": "https://cdn.example/x.png",
    "name": "monkey",
    "nested": {"stripe_secret_key": "sk_live_1", "count": 3},
    "items": [{"token": "t1"}, {"id": 2}],
}


def test_api_redacts_secret_looking_values_by_default(tmp_path, monkeypatch):
    fake = FakeAdminAPI()
    fake.add("GET", "/api/admin/settings", SECRET_RESPONSE)
    install(monkeypatch, fake, tmp_path)

    result = run(["admin", "api", "get", "/api/admin/settings"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    for secret in ("fixture-value-0", "pmk_live_zzz", "eyJhbGciOi", "cs_live_1", "hunter2", "sk_live_1", "t1"):
        assert secret not in result.stdout
    # Non-secret fields survive untouched, including a key that merely
    # contains the substring "key".
    assert payload["public_url"] == "https://cdn.example/x.png"
    assert payload["name"] == "monkey"
    assert payload["nested"]["count"] == 3
    assert payload["items"][1]["id"] == 2


def test_api_redaction_can_be_disabled_explicitly(tmp_path, monkeypatch):
    fake = FakeAdminAPI()
    fake.add("GET", "/api/admin/settings", {"fal_key": "fixture-value-0"})
    install(monkeypatch, fake, tmp_path)

    result = run(["admin", "api", "get", "/api/admin/settings", "--no-redact"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {"fal_key": "fixture-value-0"}


def test_api_redaction_warns_on_stderr_without_polluting_json_stdout(tmp_path, monkeypatch):
    fake = FakeAdminAPI()
    fake.add("GET", "/api/admin/settings", {"fal_key": "fixture-value-0", "ok": True})
    install(monkeypatch, fake, tmp_path)

    # This runner keeps stderr separate, so the note cannot corrupt piped JSON.
    result = CliRunner().invoke(app, ["--host", HOST, "--json", "admin", "api", "get", "/api/admin/settings"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["ok"] is True
    assert "redact" in result.stderr.lower()


# --- response plumbing ---


def test_api_include_wraps_the_status_and_body(tmp_path, monkeypatch):
    fake = FakeAdminAPI()
    fake.add("POST", "/api/admin/users", {"id": 9}, status=201)
    install(monkeypatch, fake, tmp_path)

    result = run(["admin", "api", "post", "/api/admin/users", "--data", "{}", "--include", "--yes"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {"status": 201, "body": {"id": 9}}


def test_api_surfaces_backend_errors_without_a_traceback(tmp_path, monkeypatch):
    fake = FakeAdminAPI()
    fake.add("GET", "/api/admin/settings", {"error": "Insufficient permissions"}, status=403)
    install(monkeypatch, fake, tmp_path)

    result = run(["admin", "api", "get", "/api/admin/settings"], json_mode=False)

    assert result.exit_code == 1
    assert "HTTP 403" in result.output
    assert "Insufficient permissions" not in result.output
    assert "403" in result.output


def test_api_requires_authentication(tmp_path, monkeypatch):
    fake = FakeAdminAPI()
    install(monkeypatch, fake, tmp_path)
    monkeypatch.delenv("PICTUREME_API_KEY", raising=False)

    result = run(["admin", "api", "get", "/api/admin/users"])

    assert result.exit_code == 2
    assert fake.requests == []
    assert "auth login" in result.output


def test_api_rejects_bearer_tokens_over_plain_http(tmp_path, monkeypatch):
    fake = FakeAdminAPI()
    install(monkeypatch, fake, tmp_path)

    result = CliRunner().invoke(
        app, ["--host", "http://remote.example", "--json", "admin", "api", "get", "/api/admin/users"]
    )

    assert result.exit_code == 2
    assert fake.requests == []
    assert json.loads(result.stdout)["error"] == "invalid_host"


def test_api_help_documents_the_safety_rules(tmp_path, monkeypatch):
    fake = FakeAdminAPI()
    install(monkeypatch, fake, tmp_path)

    result = CliRunner().invoke(app, ["admin", "api", "--help"])

    assert result.exit_code == 0, result.output
    for verb in ("request", "get", "post", "put", "patch", "delete"):
        assert verb in result.output


# --------------------------------------------------------------------------
# template preview media type
# --------------------------------------------------------------------------
#
# MarketplaceTemplate carries preview_media_type ('image'|'video') alongside
# preview_url, so a client knows whether to render an <img> or a <video>
# without sniffing the URL. `--preview` therefore always sets both fields
# together: they are one decision, and a preview_url without a kind is what
# makes a video preview render as a broken image.


def test_template_create_preview_sets_media_type_from_the_upload_kind(tmp_path, monkeypatch):
    still = tmp_path / "preview.png"
    still.write_bytes(b"\x89PNGfake")
    fake = FakeAdminAPI()
    fake.add("POST", MEDIA_UPLOAD, {"url": "https://cdn.example/preview.png", "media_kind": "image"})
    fake.add("POST", "/api/admin/marketplace/templates", TEMPLATE_A, status=201)
    install(monkeypatch, fake, tmp_path)

    result = run(["admin", "template", "create", "--name", "Still", "--preview", str(still)])

    assert result.exit_code == 0, result.output
    body = fake.requests[-1]["body"]
    assert body["preview_url"] == "https://cdn.example/preview.png"
    assert body["preview_media_type"] == "image"


@pytest.mark.parametrize("filename", ["reel.mp4", "reel.mov", "reel.webm"])
def test_template_create_local_video_preview_is_marked_video(tmp_path, monkeypatch, filename):
    clip = tmp_path / filename
    clip.write_bytes(b"\x00\x00\x00 ftypfake")
    fake = FakeAdminAPI()
    fake.add("POST", MEDIA_UPLOAD, {"url": f"https://cdn.example/{filename}", "media_kind": "video"})
    fake.add("POST", "/api/admin/marketplace/templates", TEMPLATE_A, status=201)
    install(monkeypatch, fake, tmp_path)

    result = run(["admin", "template", "create", "--name", "Reel", "--preview", str(clip)])

    assert result.exit_code == 0, result.output
    body = fake.requests[-1]["body"]
    assert body["preview_url"] == f"https://cdn.example/{filename}"
    assert body["preview_media_type"] == "video"


def test_template_preview_trusts_the_upload_kind_over_the_local_extension(tmp_path, monkeypatch):
    """The stored object's kind is authoritative — the local name is not."""
    asset = tmp_path / "preview.webp"
    asset.write_bytes(b"RIFFfake")
    fake = FakeAdminAPI()
    fake.add("POST", MEDIA_UPLOAD, {"url": "https://cdn.example/converted.mp4", "media_kind": "video"})
    fake.add("POST", "/api/admin/marketplace/templates", TEMPLATE_A, status=201)
    install(monkeypatch, fake, tmp_path)

    result = run(["admin", "template", "create", "--name", "X", "--preview", str(asset)])

    assert result.exit_code == 0, result.output
    assert fake.requests[-1]["body"]["preview_media_type"] == "video"


def test_template_preview_falls_back_to_extension_when_upload_omits_the_kind(tmp_path, monkeypatch):
    clip = tmp_path / "reel.mp4"
    clip.write_bytes(b"fake")
    fake = FakeAdminAPI()
    fake.add("POST", MEDIA_UPLOAD, {"url": "https://cdn.example/reel.mp4"})
    fake.add("POST", "/api/admin/marketplace/templates", TEMPLATE_A, status=201)
    install(monkeypatch, fake, tmp_path)

    result = run(["admin", "template", "create", "--name", "X", "--preview", str(clip)])

    assert result.exit_code == 0, result.output
    assert fake.requests[-1]["body"]["preview_media_type"] == "video"


@pytest.mark.parametrize(
    "url,kind",
    [
        ("https://cdn.example/a.jpg", "image"),
        ("https://cdn.example/a.jpeg", "image"),
        ("https://cdn.example/a.png", "image"),
        ("https://cdn.example/a.gif", "image"),
        ("https://cdn.example/a.webp", "image"),
        ("https://cdn.example/a.svg", "image"),
        ("https://cdn.example/a.mp4", "video"),
        ("https://cdn.example/a.mov", "video"),
        ("https://cdn.example/a.webm", "video"),
        ("https://cdn.example/a.MP4", "video"),
        ("https://cdn.example/path/to/a.mp4?sig=abc&x=1", "video"),
    ],
)
def test_template_remote_preview_infers_the_kind_from_the_extension(tmp_path, monkeypatch, url, kind):
    fake = FakeAdminAPI()
    fake.add("POST", "/api/admin/marketplace/templates", TEMPLATE_A, status=201)
    install(monkeypatch, fake, tmp_path)

    result = run(["admin", "template", "create", "--name", "X", "--preview", url])

    assert result.exit_code == 0, result.output
    req = only(fake)  # no upload: the URL is already remote
    assert req["body"]["preview_url"] == url
    assert req["body"]["preview_media_type"] == kind


@pytest.mark.parametrize(
    "url",
    [
        "https://cdn.example/preview",           # no extension
        "https://cdn.example/preview.txt",       # unknown extension
        "https://cdn.example/preview.avi",       # off the allowlist
        "https://cdn.example/preview.m4v",
        "https://cdn.example/download?id=42",
    ],
)
def test_template_remote_preview_refuses_an_unrecognized_extension(tmp_path, monkeypatch, url):
    """Strict: guessing 'image' for a video URL renders a broken preview on
    every public surface, so an unrecognized extension is an error."""
    fake = FakeAdminAPI()
    install(monkeypatch, fake, tmp_path)

    result = run(["admin", "template", "create", "--name", "X", "--preview", url])

    assert result.exit_code == 2
    assert fake.requests == []
    assert "preview_media_type" in result.output


def test_template_remote_preview_requires_https(tmp_path, monkeypatch):
    fake = FakeAdminAPI()
    install(monkeypatch, fake, tmp_path)

    result = run(["admin", "template", "create", "--name", "X", "--preview", "http://cdn.example/a.png"])

    assert result.exit_code == 2
    assert fake.requests == []
    assert "https" in result.output.lower()


def test_template_file_payload_preview_fields_are_authoritative(tmp_path, monkeypatch):
    """No --preview: whatever the JSON says stands, kind included."""
    payload = tmp_path / "template.json"
    payload.write_text(
        json.dumps(
            {
                "name": "From File",
                "preview_url": "https://cdn.example/opaque-object-id",
                "preview_media_type": "video",
            }
        )
    )
    fake = FakeAdminAPI()
    fake.add("POST", "/api/admin/marketplace/templates", TEMPLATE_A, status=201)
    install(monkeypatch, fake, tmp_path)

    result = run(["admin", "template", "create", "--file", str(payload)])

    assert result.exit_code == 0, result.output
    body = only(fake)["body"]
    # An extensionless URL is fine here — the file declared the kind itself.
    assert body["preview_url"] == "https://cdn.example/opaque-object-id"
    assert body["preview_media_type"] == "video"


def test_template_preview_flag_overrides_both_file_preview_fields(tmp_path, monkeypatch):
    payload = tmp_path / "template.json"
    payload.write_text(
        json.dumps(
            {"name": "From File", "preview_url": "https://cdn.example/old.mp4", "preview_media_type": "video"}
        )
    )
    fake = FakeAdminAPI()
    fake.add("POST", "/api/admin/marketplace/templates", TEMPLATE_A, status=201)
    install(monkeypatch, fake, tmp_path)

    result = run(
        ["admin", "template", "create", "--file", str(payload), "--preview", "https://cdn.example/new.png"]
    )

    assert result.exit_code == 0, result.output
    body = only(fake)["body"]
    assert body["preview_url"] == "https://cdn.example/new.png"
    assert body["preview_media_type"] == "image"


def test_template_without_a_preview_sends_no_media_type(tmp_path, monkeypatch):
    fake = FakeAdminAPI()
    fake.add("POST", "/api/admin/marketplace/templates", TEMPLATE_A, status=201)
    install(monkeypatch, fake, tmp_path)

    result = run(["admin", "template", "create", "--name", "X", "--category", "events"])

    assert result.exit_code == 0, result.output
    body = only(fake)["body"]
    assert "preview_url" not in body
    assert "preview_media_type" not in body


def test_template_update_preview_sends_both_fields_and_nothing_else(tmp_path, monkeypatch):
    clip = tmp_path / "reel.mov"
    clip.write_bytes(b"fake")
    fake = FakeAdminAPI()
    fake.add("POST", MEDIA_UPLOAD, {"url": "https://cdn.example/reel.mov", "media_kind": "video"})
    fake.add("PUT", "/api/admin/marketplace/templates/tpl_a", TEMPLATE_A)
    install(monkeypatch, fake, tmp_path)

    result = run(["admin", "template", "update", "tpl_a", "--preview", str(clip)])

    assert result.exit_code == 0, result.output
    assert [(r["method"], r["path"]) for r in fake.requests] == [
        ("POST", MEDIA_UPLOAD),
        ("PUT", "/api/admin/marketplace/templates/tpl_a"),
    ]
    assert fake.requests[-1]["body"] == {
        "preview_url": "https://cdn.example/reel.mov",
        "preview_media_type": "video",
    }


def test_template_update_remote_preview_sets_both_fields(tmp_path, monkeypatch):
    fake = FakeAdminAPI()
    fake.add("PUT", "/api/admin/marketplace/templates/tpl_a", TEMPLATE_A)
    install(monkeypatch, fake, tmp_path)

    result = run(["admin", "template", "update", "tpl_a", "--preview", "https://cdn.example/still.webp"])

    assert result.exit_code == 0, result.output
    assert only(fake)["body"] == {
        "preview_url": "https://cdn.example/still.webp",
        "preview_media_type": "image",
    }


def test_reference_images_are_unaffected_by_preview_typing(tmp_path, monkeypatch):
    """References are a plain URL list — they carry no media-type field."""
    fake = FakeAdminAPI()
    fake.add("POST", "/api/admin/marketplace/templates", TEMPLATE_A, status=201)
    install(monkeypatch, fake, tmp_path)

    result = run(
        [
            "admin",
            "template",
            "create",
            "--name",
            "X",
            "--reference",
            "https://cdn.example/ref.mp4",
            "--reference",
            "https://cdn.example/ref.png",
        ]
    )

    assert result.exit_code == 0, result.output
    body = only(fake)["body"]
    assert body["reference_images"] == ["https://cdn.example/ref.mp4", "https://cdn.example/ref.png"]
    assert "preview_media_type" not in body


# --------------------------------------------------------------------------
# admin api path allowlist — Super Admin surfaces only
# --------------------------------------------------------------------------
#
# The escape hatch is scoped to the admin route groups, not to /api/ at large.
# A superadmin token is the most powerful credential in the system, so the one
# generic command that carries it must not be usable as a general-purpose
# client for creator, media or public routes.


@pytest.mark.parametrize(
    "path,expected",
    [
        ("/api/admin/users", "/api/admin/users"),
        ("/api/admin", "/api/admin"),
        ("/api/admin/", "/api/admin/"),
        ("/api/v3/admin/stats", "/api/v3/admin/stats"),
        ("/api/v3/admin", "/api/v3/admin"),
        ("/api/admin/organizations/123/transfer-owner", "/api/admin/organizations/123/transfer-owner"),
        ("/api/admin/content/campaign/trending-tags/11", "/api/admin/content/campaign/trending-tags/11"),
    ],
)
def test_api_allows_admin_surfaces(tmp_path, monkeypatch, path, expected):
    fake = FakeAdminAPI()
    fake.add("GET", expected, {"ok": True})
    install(monkeypatch, fake, tmp_path)

    result = run(["admin", "api", "get", path])

    assert result.exit_code == 0, result.output
    assert only(fake)["path"] == expected


@pytest.mark.parametrize(
    "path",
    [
        # Other API surfaces — reachable with a normal token, not this command's job.
        "/api/media/upload",
        "/api/creations/public",
        "/api/v3/public/models",
        "/api/v3/creator/generate/upload",
        "/api/templates/upload-image",
        "/api/marketplace/templates",
        "/api/booths/",
        # Prefix lookalikes: /api/admin must match on a segment boundary.
        "/api/administrator/users",
        "/api/adminx",
        "/api/admin2/users",
        "/api/adminsettings",
        "/api/v3/administrator",
        "/api/v3/adminx/users",
        # Not deep enough / not the API at all.
        "/api",
        "/api/",
        "/healthz",
        "/",
        # Case matters: the backend routes are case-sensitive.
        "/API/admin/users",
        "/api/ADMIN/users",
    ],
)
def test_api_rejects_paths_outside_the_admin_surfaces(tmp_path, monkeypatch, path):
    fake = FakeAdminAPI()
    install(monkeypatch, fake, tmp_path)

    result = run(["admin", "api", "get", path])

    assert result.exit_code == 2
    assert fake.requests == []


@pytest.mark.parametrize(
    "path",
    [
        # Absolute / protocol-relative: would send the token to another origin.
        "https://evil.example/api/admin/users",
        "http://evil.example/api/admin/users",
        "//evil.example/api/admin/users",
        "HTTPS://evil.example/api/admin/users",
        # Backslashes: some servers and proxies fold these into "/".
        "\\api\\admin\\users",
        "/api/admin\\..\\..\\media/upload",
        "/api/admin/users\\..",
        # Raw dot traversal.
        "/api/admin/../media/upload",
        "/api/admin/../../etc/passwd",
        "/api/admin/users/..",
        "/api/admin/./users",
        # Percent-encoded dot traversal, including mixed and upper case.
        "/api/admin/%2e%2e/media/upload",
        "/api/admin/%2E%2E/media/upload",
        "/api/admin/.%2e/media/upload",
        "/api/admin/%2e./media/upload",
        "/api/admin/%2e%2E/x",
        # Double encoding must not survive as a live traversal either.
        "/api/admin/%252e%252e/media/upload",
        # Encoded separators: would re-expand into a different path server-side.
        "/api/admin%2fusers",
        "/api/admin%2Fusers",
        "/api%2fadmin/users",
        "/api/admin/us%5cers",
        "/api/admin/us%5Cers",
        # Fragments are never sent and only ever hide intent.
        "/api/admin/users#/api/media",
        "/api/admin/users#frag",
        # Empty internal segments.
        "/api//admin/users",
        "/api/admin//users",
        # Whitespace and control characters.
        "/api/admin/users\n",
        "/api/admin/us ers",
        "/api/admin/users\t",
        "",
        "   ",
    ],
)
def test_api_rejects_traversal_and_off_host_paths(tmp_path, monkeypatch, path):
    fake = FakeAdminAPI()
    install(monkeypatch, fake, tmp_path)

    result = run(["admin", "api", "get", path])

    assert result.exit_code == 2
    assert fake.requests == []


def test_api_path_error_names_the_allowed_surfaces(tmp_path, monkeypatch):
    fake = FakeAdminAPI()
    install(monkeypatch, fake, tmp_path)

    result = run(["admin", "api", "get", "/api/media/upload"], json_mode=False)

    assert result.exit_code == 2
    assert "/api/admin" in result.output
    assert "/api/v3/admin" in result.output


def test_api_percent_encoding_that_decodes_into_the_allowlist_is_still_rejected(tmp_path, monkeypatch):
    """`/api/admin%2fusers` decodes to a legal path, but an encoded separator
    is exactly how a validator and a router are made to disagree."""
    fake = FakeAdminAPI()
    fake.add("GET", "/api/admin/users", {"ok": True})
    install(monkeypatch, fake, tmp_path)

    result = run(["admin", "api", "get", "/api/admin%2fusers"])

    assert result.exit_code == 2
    assert fake.requests == []


def test_api_traversal_escaping_the_admin_prefix_is_rejected_even_though_it_starts_legal(
    tmp_path, monkeypatch
):
    fake = FakeAdminAPI()
    install(monkeypatch, fake, tmp_path)

    result = run(["admin", "api", "post", "/api/admin/../media/upload", "--yes"])

    assert result.exit_code == 2
    assert fake.requests == []


# --- query handling survives the tightened validation ---


def test_api_inline_query_values_are_forwarded_literally(tmp_path, monkeypatch):
    fake = FakeAdminAPI()
    fake.add("GET", "/api/admin/users", {"ok": True})
    install(monkeypatch, fake, tmp_path)

    result = run(["admin", "api", "get", "/api/admin/users?filter=a%20b&tag=x%2By&empty="])

    assert result.exit_code == 0, result.output
    req = only(fake)
    assert req["path"] == "/api/admin/users"
    assert req["query"] == {"filter": "a b", "tag": "x+y", "empty": ""}


def test_api_query_option_values_are_encoded_not_mangled(tmp_path, monkeypatch):
    fake = FakeAdminAPI()
    fake.add("GET", "/api/admin/audit-logs", {"ok": True})
    install(monkeypatch, fake, tmp_path)

    result = run(
        [
            "admin",
            "api",
            "get",
            "/api/admin/audit-logs",
            "--query",
            "q=needs review & more",
            "--query",
            "path=/api/media/upload",
        ]
    )

    assert result.exit_code == 0, result.output
    req = only(fake)
    assert req["path"] == "/api/admin/audit-logs"
    assert req["query"] == {"q": "needs review & more", "path": "/api/media/upload"}


def test_api_query_value_naming_a_forbidden_path_does_not_affect_validation(tmp_path, monkeypatch):
    """Only the path is allowlisted; a query value is just data."""
    fake = FakeAdminAPI()
    fake.add("GET", "/api/admin/audit-logs", {"ok": True})
    install(monkeypatch, fake, tmp_path)

    result = run(["admin", "api", "get", "/api/admin/audit-logs?target=/api/v3/public/models"])

    assert result.exit_code == 0, result.output
    assert only(fake)["query"] == {"target": "/api/v3/public/models"}
