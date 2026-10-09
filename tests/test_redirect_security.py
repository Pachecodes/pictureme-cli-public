"""Real shared-client transport boundary: synthetic bodies, no network."""
import httpx
import pytest
from pictureme.client import APIError, PictureMEClient
from pictureme.config import CLIConfig

@pytest.mark.parametrize("status", [301, 302, 303, 304, 307, 308])
@pytest.mark.parametrize("kind", ["password", "device", "multipart"])
@pytest.mark.parametrize("location", ["http://example.com/capture", "https://other.example.com/capture"])
def test_redirect_never_forwards_sensitive_body(monkeypatch, status, kind, location):
    requests = []
    def transport(request):
        requests.append((str(request.url), request.read()))
        if request.url.path == "/capture":
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(status, headers={"Location": location}, json={"echo": "fixture-private"})
    real = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: real(**kw, transport=httpx.MockTransport(transport)))
    bodies = {
        "password": {"json": {"password": "fixture-private"}},
        "device": {"json": {"device_code": "fixture-private"}},
        "multipart": {"files": {"file": ("sample.bin", b"fixture-private")}},
    }
    with PictureMEClient(CLIConfig(host="https://example.com")) as client:
        with pytest.raises(APIError) as error:
            client.post("/submit", **bodies[kind])
    assert len(requests) == 1
    assert b"fixture-private" in requests[0][1]
    assert error.value.status_code == status
    assert str(error.value) == f"HTTP {status}: redirects are disabled"
    assert error.value.payload is None

@pytest.mark.parametrize("kind", ["password", "device", "multipart"])
def test_no_redirect_success_preserves_body(monkeypatch, kind):
    requests = []
    def transport(request):
        requests.append(request.read())
        return httpx.Response(200, json={"ok": True})
    real = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: real(**kw, transport=httpx.MockTransport(transport)))
    body = {"files": {"file": ("sample.bin", b"fixture-private")}} if kind == "multipart" else {"json": {kind: "fixture-private"}}
    with PictureMEClient(CLIConfig(host="https://example.com")) as client:
        assert client.post("/submit", **body) == {"ok": True}
    assert len(requests) == 1 and b"fixture-private" in requests[0]
