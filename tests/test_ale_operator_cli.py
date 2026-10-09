"""HTTP contract tests for the scoped ALE operator CLI.

These drive the real Typer app and httpx client. Only transport is faked, so
methods, paths, payloads, bearer auth, readbacks, and machine output are part of
the tested public contract.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from pictureme import config
from pictureme.cli import app

TOKEN = "pmk_ale_operator_test_secret"
HOST = "https://go.pictureme.now"
ROOT = "/api/v3/operator/ale"


class FakeALEOperatorAPI:
    def __init__(self) -> None:
        self.requests: list[dict[str, object]] = []
        self.routes: dict[tuple[str, str], list[tuple[int, object]]] = {}

    def add(
        self,
        method: str,
        path: str,
        payload: object,
        *,
        status: int = 200,
    ) -> None:
        self.routes.setdefault((method, path), []).append((status, payload))

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = None
        if request.content:
            body = json.loads(request.content)
        self.requests.append(
            {
                "method": request.method,
                "path": request.url.path,
                "authorization": request.headers.get("Authorization"),
                "content_type": request.headers.get("Content-Type"),
                "body": body,
            }
        )
        responses = self.routes.get((request.method, request.url.path))
        if not responses:
            return httpx.Response(404, json={"error": "not_found"})
        status, payload = responses.pop(0)
        return httpx.Response(status, json=payload)


def install(monkeypatch, fake: FakeALEOperatorAPI, tmp_path: Path, *, token: str | None = TOKEN) -> None:
    real_client = httpx.Client

    def client_with_fake_transport(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(fake)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", client_with_fake_transport)
    monkeypatch.setattr(config, "config_path", lambda: tmp_path / "config.json")
    monkeypatch.delenv("PICTUREME_HOST", raising=False)
    if token is None:
        monkeypatch.delenv("PICTUREME_API_KEY", raising=False)
    else:
        monkeypatch.setenv("PICTUREME_API_KEY", token)


def run(args: list[str]):
    return CliRunner().invoke(app, ["--host", HOST, "--json", "admin", "ale", *args])


def assert_auth(request: dict[str, object]) -> None:
    assert request["authorization"] == f"Bearer {TOKEN}"


RUNTIME = {
    "is_enabled": True,
    "provider": "openai",
    "model": "gpt-5-mini",
    "temperature": 0.3,
}
SPARK = {
    "tier_code": "spark",
    "is_enabled": True,
    "provider": "anthropic",
    "model": "claude-sonnet-4-5",
}
EFFECTIVE = {
    "tier_code": "spark",
    "policy_source": "tier",
    "provider": "anthropic",
    "model": "claude-sonnet-4-5",
}


def test_runtime_get_uses_scoped_operator_route_and_emits_exact_json(tmp_path, monkeypatch):
    fake = FakeALEOperatorAPI()
    fake.add("GET", f"{ROOT}/runtime", RUNTIME)
    install(monkeypatch, fake, tmp_path)

    result = run(["runtime", "get"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == RUNTIME
    assert [(r["method"], r["path"]) for r in fake.requests] == [("GET", f"{ROOT}/runtime")]
    assert_auth(fake.requests[0])


def test_runtime_patch_requires_yes_before_file_or_network_access(tmp_path, monkeypatch):
    fake = FakeALEOperatorAPI()
    install(monkeypatch, fake, tmp_path)

    result = run(["runtime", "patch", "--file", str(tmp_path / "missing.json")])

    assert result.exit_code == 2
    assert "--yes" in result.output
    assert fake.requests == []


def test_runtime_patch_sends_file_payload_then_reads_back_exact_saved_state(tmp_path, monkeypatch):
    patch_file = tmp_path / "runtime.json"
    patch = {"provider": "openai", "model": "gpt-5-mini", "temperature": 0.3}
    patch_file.write_text(json.dumps(patch))
    fake = FakeALEOperatorAPI()
    fake.add("PATCH", f"{ROOT}/runtime", {"status": "accepted"})
    fake.add("GET", f"{ROOT}/runtime", RUNTIME)
    install(monkeypatch, fake, tmp_path)

    result = run(["runtime", "patch", "--file", str(patch_file), "--yes"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == RUNTIME
    assert [(r["method"], r["path"], r["body"]) for r in fake.requests] == [
        ("PATCH", f"{ROOT}/runtime", patch),
        ("GET", f"{ROOT}/runtime", None),
    ]
    assert fake.requests[0]["content_type"] == "application/json"
    assert all(r["authorization"] == f"Bearer {TOKEN}" for r in fake.requests)


@pytest.mark.parametrize("body", [
    {"provider": "openrouter"},
    {"model": "deepseek/deepseek-v4.1-flash"},
    {"fallback_provider": "openrouter"},
    {"fallback_model": "google/gemini-3.8-flash"},
    {"provider": "openrouter", "model": ""},
    {"provider": None, "model": "deepseek/deepseek-v4.1-flash"},
])
@pytest.mark.parametrize("command", ["runtime", "tier"])
def test_routing_pair_must_be_complete_before_write(tmp_path, monkeypatch, body, command):
    payload = tmp_path / "routing.json"
    payload.write_text(json.dumps(body))
    fake = FakeALEOperatorAPI()
    install(monkeypatch, fake, tmp_path)
    args = ["runtime", "patch"] if command == "runtime" else ["tier", "set", "spark"]

    result = run([*args, "--file", str(payload), "--yes"])

    assert result.exit_code == 2
    assert "must" in result.output
    assert fake.requests == []


def test_non_routing_runtime_patch_stays_sparse(tmp_path, monkeypatch):
    payload = tmp_path / "runtime.json"
    payload.write_text(json.dumps({"temperature": 0.3}))
    fake = FakeALEOperatorAPI()
    fake.add("PATCH", f"{ROOT}/runtime", {"status": "accepted"})
    fake.add("GET", f"{ROOT}/runtime", RUNTIME)
    install(monkeypatch, fake, tmp_path)

    result = run(["runtime", "patch", "--file", str(payload), "--yes"])

    assert result.exit_code == 0, result.output
    assert [request["method"] for request in fake.requests] == ["PATCH", "GET"]


def test_runtime_patch_rejects_stale_readback_without_retrying(tmp_path, monkeypatch):
    patch_file = tmp_path / "runtime.json"
    patch_file.write_text(json.dumps({"provider": "openrouter", "model": "deepseek/deepseek-v4.1-flash"}))
    fake = FakeALEOperatorAPI()
    fake.add("PATCH", f"{ROOT}/runtime", {"status": "accepted"})
    fake.add("GET", f"{ROOT}/runtime", RUNTIME)
    install(monkeypatch, fake, tmp_path)

    result = run(["runtime", "patch", "--file", str(patch_file), "--yes"])

    assert result.exit_code == 2
    assert "readback differs for model" in result.output
    assert "deepseek/deepseek-v4.1-flash" not in result.output
    assert [request["method"] for request in fake.requests] == ["PATCH", "GET"]


def test_tier_delete_rejects_readback_with_override_still_active(tmp_path, monkeypatch):
    fake = FakeALEOperatorAPI()
    fake.add("DELETE", f"{ROOT}/tiers/spark", {"deleted": True})
    fake.add("GET", f"{ROOT}/effective/spark", EFFECTIVE)
    install(monkeypatch, fake, tmp_path)

    result = run(["tier", "delete", "spark", "--yes"])

    assert result.exit_code == 2
    assert "did not resolve to global" in result.output
    assert [request["method"] for request in fake.requests] == ["DELETE", "GET"]


def test_tier_list_get_and_effective_use_exact_read_routes(tmp_path, monkeypatch):
    fake = FakeALEOperatorAPI()
    listing = {"policies": [SPARK]}
    fake.add("GET", f"{ROOT}/tiers", listing)
    fake.add("GET", f"{ROOT}/tiers/spark", SPARK)
    fake.add("GET", f"{ROOT}/effective/spark", EFFECTIVE)
    install(monkeypatch, fake, tmp_path)

    listed = run(["tier", "list"])
    fetched = run(["tier", "get", "spark"])
    effective = run(["tier", "effective", "spark"])

    assert listed.exit_code == fetched.exit_code == effective.exit_code == 0
    assert json.loads(listed.stdout) == listing
    assert json.loads(fetched.stdout) == SPARK
    assert json.loads(effective.stdout) == EFFECTIVE
    assert [(r["method"], r["path"]) for r in fake.requests] == [
        ("GET", f"{ROOT}/tiers"),
        ("GET", f"{ROOT}/tiers/spark"),
        ("GET", f"{ROOT}/effective/spark"),
    ]
    assert all(r["authorization"] == f"Bearer {TOKEN}" for r in fake.requests)


def test_tier_set_requires_yes(tmp_path, monkeypatch):
    payload = tmp_path / "tier.json"
    payload.write_text(json.dumps(SPARK))
    fake = FakeALEOperatorAPI()
    install(monkeypatch, fake, tmp_path)

    result = run(["tier", "set", "spark", "--file", str(payload)])

    assert result.exit_code == 2
    assert "--yes" in result.output
    assert fake.requests == []


def test_tier_set_puts_payload_and_reads_back_exact_saved_policy(tmp_path, monkeypatch):
    payload = tmp_path / "tier.json"
    body = {"provider": "anthropic", "model": "claude-sonnet-4-5", "is_enabled": True}
    payload.write_text(json.dumps(body))
    fake = FakeALEOperatorAPI()
    fake.add("PUT", f"{ROOT}/tiers/spark", {"status": "accepted"})
    fake.add("GET", f"{ROOT}/tiers/spark", SPARK)
    install(monkeypatch, fake, tmp_path)

    result = run(["tier", "set", "spark", "--file", str(payload), "--yes"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == SPARK
    assert [(r["method"], r["path"], r["body"]) for r in fake.requests] == [
        ("PUT", f"{ROOT}/tiers/spark", body),
        ("GET", f"{ROOT}/tiers/spark", None),
    ]


def test_tier_set_rejects_stale_readback(tmp_path, monkeypatch):
    payload = tmp_path / "tier.json"
    payload.write_text(json.dumps({"provider": "anthropic", "model": "claude-opus-4-6"}))
    fake = FakeALEOperatorAPI()
    fake.add("PUT", f"{ROOT}/tiers/spark", {"status": "accepted"})
    fake.add("GET", f"{ROOT}/tiers/spark", SPARK)
    install(monkeypatch, fake, tmp_path)

    result = run(["tier", "set", "spark", "--file", str(payload), "--yes"])

    assert result.exit_code == 2
    assert "readback differs for model" in result.output
    assert [request["method"] for request in fake.requests] == ["PUT", "GET"]


def test_tier_delete_requires_yes(tmp_path, monkeypatch):
    fake = FakeALEOperatorAPI()
    install(monkeypatch, fake, tmp_path)

    result = run(["tier", "delete", "spark"])

    assert result.exit_code == 2
    assert "--yes" in result.output
    assert fake.requests == []


def test_tier_delete_reads_back_effective_fallback(tmp_path, monkeypatch):
    fallback = {**EFFECTIVE, "policy_source": "global", "provider": "openai", "model": "gpt-5-mini"}
    fake = FakeALEOperatorAPI()
    fake.add("DELETE", f"{ROOT}/tiers/spark", {"deleted": True, "tier_code": "spark"})
    fake.add("GET", f"{ROOT}/effective/spark", fallback)
    install(monkeypatch, fake, tmp_path)

    result = run(["tier", "delete", "spark", "--yes"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == fallback
    assert [(r["method"], r["path"]) for r in fake.requests] == [
        ("DELETE", f"{ROOT}/tiers/spark"),
        ("GET", f"{ROOT}/effective/spark"),
    ]


def test_payload_file_must_be_json_object_and_fails_before_network(tmp_path, monkeypatch):
    payload = tmp_path / "bad.json"
    payload.write_text("[]")
    fake = FakeALEOperatorAPI()
    install(monkeypatch, fake, tmp_path)

    result = run(["runtime", "patch", "--file", str(payload), "--yes"])

    assert result.exit_code == 2
    assert "JSON object" in result.output
    assert fake.requests == []


def test_operator_commands_fail_locally_without_auth(tmp_path, monkeypatch):
    fake = FakeALEOperatorAPI()
    install(monkeypatch, fake, tmp_path, token=None)

    result = run(["runtime", "get"])

    assert result.exit_code == 2
    assert "Not authenticated" in result.output
    assert fake.requests == []


def test_operator_auth_failure_is_nonzero_and_does_not_attempt_readback(tmp_path, monkeypatch):
    payload = tmp_path / "runtime.json"
    payload.write_text(json.dumps({"provider": "openai", "model": "gpt-5-mini"}))
    fake = FakeALEOperatorAPI()
    fake.add("PATCH", f"{ROOT}/runtime", {"error": "insufficient_scope"}, status=403)
    install(monkeypatch, fake, tmp_path)

    result = run(["runtime", "patch", "--file", str(payload), "--yes"])

    assert result.exit_code == 1
    assert "HTTP 403" in result.output
    assert "insufficient_scope" not in result.output
    assert [(r["method"], r["path"]) for r in fake.requests] == [("PATCH", f"{ROOT}/runtime")]


def test_admin_generic_api_does_not_accept_operator_routes(tmp_path, monkeypatch):
    fake = FakeALEOperatorAPI()
    install(monkeypatch, fake, tmp_path)

    result = CliRunner().invoke(
        app,
        ["--host", HOST, "--json", "admin", "api", "get", f"{ROOT}/runtime"],
    )

    assert result.exit_code == 2
    assert fake.requests == []
    assert "Super Admin path" in result.output
