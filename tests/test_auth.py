from app.extensions import get_redis
from tests.conftest import register


def test_register_login_me(client):
    register(client)
    resp = client.post("/api/auth/login", json={"email": "SARU@example.edu", "password": "studyhard42"})
    assert resp.status_code == 200
    token = resp.get_json()["access_token"]
    me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.get_json()["user"]["email"] == "saru@example.edu"


def test_duplicate_email_rejected(client):
    register(client)
    resp = client.post("/api/auth/register", json={
        "email": "saru@example.edu", "password": "another1pass", "display_name": "Dup",
    })
    assert resp.status_code == 409


def test_weak_password_and_bad_payload(client):
    resp = client.post("/api/auth/register", json={
        "email": "x@example.edu", "password": "onlyletters", "display_name": "X",
    })
    assert resp.status_code == 422
    assert "password" in resp.get_json()["error"]["details"]

    resp = client.post("/api/auth/register", json={"email": "x@example.edu", "surprise": 1})
    body = resp.get_json()["error"]
    assert resp.status_code == 422
    assert "_unknown" in body["details"] and "password" in body["details"]


def test_wrong_password_same_message(client):
    register(client)
    a = client.post("/api/auth/login", json={"email": "saru@example.edu", "password": "wrongpass1"})
    b = client.post("/api/auth/login", json={"email": "nobody@example.edu", "password": "wrongpass1"})
    assert a.status_code == b.status_code == 401
    assert a.get_json()["error"]["message"] == b.get_json()["error"]["message"]


def test_refresh_is_single_use(client):
    tokens = register(client)
    first = client.post("/api/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert first.status_code == 200
    again = client.post("/api/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert again.status_code == 401
    assert again.get_json()["error"]["code"] == "token_revoked"


def test_access_token_cannot_refresh(client):
    tokens = register(client)
    resp = client.post("/api/auth/refresh", json={"refresh_token": tokens["access_token"]})
    assert resp.get_json()["error"]["code"] == "token_wrong_type"


def test_logout_revokes_token(client):
    tokens = register(client)
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    assert client.post("/api/auth/logout", headers=headers).status_code == 200
    assert client.get("/api/auth/me", headers=headers).status_code == 401


def test_missing_and_garbage_tokens(client):
    assert client.get("/api/courses").get_json()["error"]["code"] == "token_missing"
    resp = client.get("/api/courses", headers={"Authorization": "Bearer not.a.jwt"})
    assert resp.get_json()["error"]["code"] == "token_invalid"


def test_profile_update(client, auth):
    resp = client.patch("/api/auth/me", json={"study_level": "advanced"}, headers=auth)
    assert resp.get_json()["user"]["study_level"] == "advanced"
    resp = client.patch("/api/auth/me", json={"study_level": "genius"}, headers=auth)
    assert resp.status_code == 422


def test_auth_rate_limit(app, client):
    app.config["RATE_LIMIT_AUTH"] = "3/minute"
    get_redis().flushall()
    codes = [
        client.post("/api/auth/login", json={"email": "a@b.co", "password": "whatever1"}).status_code
        for _ in range(5)
    ]
    assert codes[:3] == [401, 401, 401]
    assert codes[3] == 429
    resp = client.post("/api/auth/login", json={"email": "a@b.co", "password": "whatever1"})
    assert int(resp.headers["Retry-After"]) >= 1
