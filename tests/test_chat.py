import json


def _session(client, auth, course_id, **body):
    resp = client.post(f"/api/courses/{course_id}/sessions", json=body, headers=auth)
    assert resp.status_code == 201, resp.get_json()
    return resp.get_json()["session"]


def test_answer_has_citations(client, auth, course, ready_doc):
    session = _session(client, auth, course["id"])
    resp = client.post(f"/api/sessions/{session['id']}/messages",
                       json={"question": "What is a binary heap?"}, headers=auth)
    assert resp.status_code == 200, resp.get_json()
    message = resp.get_json()["message"]
    assert "heap" in message["content"].lower()
    assert message["citations"], message
    assert message["citations"][0]["filename"] == "lecture4.md"
    assert message["grounded"] is True


def test_repeat_question_hits_cache(client, auth, course, ready_doc):
    session = _session(client, auth, course["id"])
    url = f"/api/sessions/{session['id']}/messages"
    first = client.post(url, json={"question": "How does heapsort work?"}, headers=auth).get_json()["message"]
    other = _session(client, auth, course["id"])
    second = client.post(f"/api/sessions/{other['id']}/messages",
                         json={"question": "how does HEAPSORT work"}, headers=auth).get_json()["message"]
    assert first["cached"] is False
    assert second["cached"] is True
    assert second["content"] == first["content"]


def test_cache_invalidated_by_new_upload(client, auth, course, ready_doc):
    from tests.conftest import upload

    session = _session(client, auth, course["id"])
    url = f"/api/sessions/{session['id']}/messages"
    client.post(url, json={"question": "What is a hash table?"}, headers=auth)
    upload(client, auth, course["id"], text="A hash table stores key value pairs in buckets. " * 10,
           filename="extra.txt")
    again = client.post(url, json={"question": "What is a hash table?"}, headers=auth).get_json()["message"]
    assert again["cached"] is False


def test_out_of_scope_question_is_not_answered(client, auth, course, ready_doc):
    session = _session(client, auth, course["id"])
    resp = client.post(f"/api/sessions/{session['id']}/messages",
                       json={"question": "Who won the 1998 world cup final?"}, headers=auth)
    message = resp.get_json()["message"]
    assert message["grounded"] is False
    assert "could not find" in message["content"].lower()


def test_follow_up_uses_history(client, auth, course, ready_doc):
    session = _session(client, auth, course["id"])
    url = f"/api/sessions/{session['id']}/messages"
    client.post(url, json={"question": "What is an AVL tree?"}, headers=auth)
    resp = client.post(url, json={"question": "How does it stay balanced?"}, headers=auth)
    message = resp.get_json()["message"]
    assert message["cached"] is False
    assert "rotation" in message["content"].lower() or "balance" in message["content"].lower()
    full = client.get(f"/api/sessions/{session['id']}", headers=auth).get_json()["session"]
    assert full["message_count"] == 4
    assert full["title"].startswith("What is an AVL tree")


def test_streaming_endpoint(client, auth, course, ready_doc):
    session = _session(client, auth, course["id"])
    resp = client.post(f"/api/sessions/{session['id']}/stream",
                       json={"question": "What is a priority queue?"}, headers=auth)
    assert resp.status_code == 200
    assert resp.mimetype == "text/event-stream"
    events = []
    for block in resp.get_data(as_text=True).strip().split("\n\n"):
        name = block.split("\n")[0].replace("event: ", "")
        data = json.loads(block.split("\n")[1].replace("data: ", ""))
        events.append((name, data))
    names = [e[0] for e in events]
    assert names[0] == "meta" and names[-1] == "done" and "token" in names
    done = events[-1][1]["message"]
    assert "priority queue" in done["content"].lower()


def test_document_filter_restricts_sources(client, auth, course, ready_doc):
    from tests.conftest import upload

    other = upload(client, auth, course["id"], filename="graphs.txt",
                   text="Breadth first search visits vertices in order of distance using a queue. " * 5)
    other_id = other.get_json()["document"]["id"]
    session = _session(client, auth, course["id"], document_ids=[other_id])
    resp = client.post(f"/api/sessions/{session['id']}/messages",
                       json={"question": "What does breadth first search use?"}, headers=auth)
    cites = resp.get_json()["message"]["citations"]
    assert cites and all(c["document_id"] == other_id for c in cites)

    bad = client.post(f"/api/courses/{course['id']}/sessions", json={"document_ids": [99999]}, headers=auth)
    assert bad.status_code == 404


def test_feedback_and_export(client, auth, course, ready_doc):
    session = _session(client, auth, course["id"])
    msg = client.post(f"/api/sessions/{session['id']}/messages",
                      json={"question": "What is the load factor?"}, headers=auth).get_json()["message"]
    resp = client.post(f"/api/messages/{msg['id']}/feedback", json={"value": 1}, headers=auth)
    assert resp.get_json()["message"]["feedback"] == 1
    assert client.post(f"/api/messages/{msg['id']}/feedback", json={"value": 3}, headers=auth).status_code == 422
    export = client.get(f"/api/sessions/{session['id']}/export", headers=auth)
    assert b"load factor" in export.data.lower()


def test_ask_requires_ready_documents(client, auth, course):
    resp = client.post(f"/api/courses/{course['id']}/ask", json={"question": "anything?"}, headers=auth)
    assert resp.status_code == 422


def test_quick_ask_and_search(client, auth, course, ready_doc):
    resp = client.post(f"/api/courses/{course['id']}/ask", json={"question": "What is chaining?"}, headers=auth)
    assert resp.status_code == 200
    search = client.get(f"/api/courses/{course['id']}/search?q=red black tree", headers=auth).get_json()
    assert search["results"] and search["results"][0]["section"] == "Binary Search Trees"
    assert set(search["results"][0]["matched_by"]) <= {"vector", "keyword"}
    again = client.get(f"/api/courses/{course['id']}/search?q=red black tree", headers=auth).get_json()
    assert again["cached"] is True


def test_token_quota_enforced(app, client, auth, course, ready_doc):
    app.config["DAILY_TOKEN_BUDGET"] = 10
    session = _session(client, auth, course["id"])
    resp = client.post(f"/api/sessions/{session['id']}/messages",
                       json={"question": "Explain red black trees"}, headers=auth)
    assert resp.status_code == 429
    assert resp.get_json()["error"]["code"] == "quota_exceeded"


def test_llm_rate_limit_headers(app, client, auth, course, ready_doc):
    app.config["RATE_LIMIT_LLM"] = "2/minute"
    session = _session(client, auth, course["id"])
    url = f"/api/sessions/{session['id']}/messages"
    codes = [client.post(url, json={"question": f"What is a heap {i}?"}, headers=auth).status_code for i in range(3)]
    assert codes == [200, 200, 429]
