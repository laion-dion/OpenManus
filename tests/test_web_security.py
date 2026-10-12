import os

os.environ["OPENMANUS_WEB_TOKEN"] = "teste123"
os.environ.pop("OPENMANUS_CORS_ALLOW_REGEX", None)
os.environ.pop("OPENMANUS_TRUST_CF_ACCESS", None)

from fastapi.testclient import TestClient  # noqa: E402

from app.web.auth import AUTH_COOKIE_NAME  # noqa: E402
from app.web.redaction import redact, redact_and_truncate  # noqa: E402
from app.web.server import app  # noqa: E402


def client():
    return TestClient(app)


def test_health_is_public():
    assert client().get("/api/health").status_code == 200


def test_chat_requires_login():
    assert client().post("/api/chat", json={"prompt": "oi"}).status_code == 401


def test_log_stream_requires_login():
    assert client().get("/api/stream/logs").status_code == 401


def test_chat_stream_requires_login():
    assert client().get("/api/stream/chat").status_code == 401


def test_login_wrong_token_is_rejected():
    r = client().post("/api/login", json={"token": "errado"})
    assert r.status_code == 401


def test_login_sets_secure_cookie_and_unlocks_api():
    c = client()
    r = c.post("/api/login", json={"token": "teste123"})
    assert r.status_code == 200
    sc = r.headers.get("set-cookie", "").lower()
    assert AUTH_COOKIE_NAME in sc
    assert "httponly" in sc
    assert "samesite=strict" in sc
    # corpo invalido: se passou da autenticacao vira 422, nao 401 (nao inicia o agente)
    assert c.post("/api/chat", json={}).status_code == 422


def test_cors_blocks_random_origin():
    r = client().get("/api/health", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in r.headers


def test_cors_allows_localhost_only_by_default():
    r = client().get("/api/health", headers={"Origin": "http://localhost:5173"})
    assert r.headers.get("access-control-allow-origin") == "http://localhost:5173"


def test_redacts_openai_key():
    out = redact("export OPENAI_API_KEY=sk-abc123456789xyzABC")
    assert "sk-abc123456789xyzABC" not in out


def test_redacts_github_pat():
    out = redact("git push https://x:github_pat_11ABCDEFG0123456789abcdef@github.com/a/b")
    assert "github_pat_11ABCDEFG0123456789abcdef" not in out


def test_redacts_bearer():
    out = redact("curl -H 'Authorization: Bearer abcdef1234567890abcdef' https://x")
    assert "abcdef1234567890abcdef" not in out


def test_redacts_env_secret_value(monkeypatch):
    monkeypatch.setenv("MINHA_SECRET_KEY", "valor-super-secreto-9876")
    assert "valor-super-secreto-9876" not in redact("echo valor-super-secreto-9876")


def test_truncates_large_output():
    assert len(redact_and_truncate("a" * 10000)) < 5000
