from app.extensions import db
from app.models import User
from tests.conftest import register


def test_liveness_and_readiness(client):
    assert client.get("/health/live").get_json()["status"] == "ok"
    ready = client.get("/health/ready")
    assert ready.status_code == 200
    body = ready.get_json()
    assert body["checks"]["database"]["ok"] and body["checks"]["redis"]["ok"]


def test_request_id_and_metrics(client):
    resp = client.get("/api/version", headers={"X-Request-ID": "abc123"})
    assert resp.headers["X-Request-ID"] == "abc123"
    metrics = client.get("/metrics").get_data(as_text=True)
    assert "studymate_http_requests_total" in metrics


def test_error_shape_for_unknown_route(client):
    resp = client.get("/api/nope")
    assert resp.status_code == 404
    assert "request_id" in resp.get_json()["error"]


def test_admin_endpoints_need_admin(client, auth):
    assert client.get("/api/admin/stats", headers=auth).status_code == 403
    user = User.query.filter_by(email="saru@example.edu").first()
    user.role = "admin"
    db.session.commit()
    stats = client.get("/api/admin/stats", headers=auth).get_json()
    assert stats["users"] == 1
    purge = client.post("/api/admin/cache/purge", json={"namespace": "answer"}, headers=auth)
    assert purge.status_code == 200


def test_disabled_user_is_locked_out(client):
    tokens = register(client, email="gone@example.edu")
    user = User.query.filter_by(email="gone@example.edu").first()
    user.is_active = False
    db.session.commit()
    resp = client.get("/api/auth/me", headers={"Authorization": f"Bearer {tokens['access_token']}"})
    assert resp.status_code == 401


def test_course_delete_cascades(client, auth, course, ready_doc):
    client.post(f"/api/courses/{course['id']}/sessions", json={}, headers=auth)
    assert client.delete(f"/api/courses/{course['id']}", headers=auth).status_code == 204
    from app.models import ChatSession, Chunk

    assert Chunk.query.count() == 0
    assert ChatSession.query.count() == 0
