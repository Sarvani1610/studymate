import io

import pytest

from app import create_app
from app.extensions import db

LECTURE = """# Data Structures Lecture 4: Trees and Heaps

## Binary Search Trees

A binary search tree is a binary tree where every node's left subtree holds smaller keys and the right subtree
holds larger keys. Search, insert and delete run in O(h) time where h is the height of the tree. When the tree
is balanced the height is O(log n), but a sorted insertion order degrades it into a linked list with height n.

Self balancing trees such as AVL trees and red black trees keep the height logarithmic. An AVL tree is a binary
search tree that stores a balance factor at each node and performs rotations after inserts and deletes.
A red black tree is a binary search tree that colors nodes red or black and guarantees no path is more than
twice as long as any other.

## Heaps and Priority Queues

A binary heap is a complete binary tree that satisfies the heap property: in a min heap every parent is less
than or equal to its children. Heaps are usually stored in an array, where the children of index i live at
2i + 1 and 2i + 2. Insert and extract-min both run in O(log n) because they sift an element up or down one level
at a time.

A priority queue is an abstract data type that always returns the highest priority element first. Priority
queues are commonly implemented with binary heaps. Dijkstra's algorithm uses a priority queue to pick the
closest unvisited vertex on every step.

Heapsort builds a max heap from the input and repeatedly extracts the maximum. Heapsort runs in O(n log n)
time in the worst case and sorts in place, but it is not a stable sort.

## Hash Tables

A hash table maps keys to buckets using a hash function. Collisions are handled by chaining or by open
addressing. The load factor is the number of stored entries divided by the number of buckets. When the load
factor gets too high the table is resized, which keeps expected lookup time at O(1).
"""


@pytest.fixture()
def app(tmp_path):
    app = create_app("testing", overrides={"UPLOAD_DIR": str(tmp_path / "uploads")})
    with app.app_context():
        yield app
        db.session.remove()
        db.drop_all()


@pytest.fixture()
def client(app):
    return app.test_client()


def register(client, email="saru@example.edu", password="studyhard42", name="Saru", level="intermediate"):
    resp = client.post("/api/auth/register", json={
        "email": email, "password": password, "display_name": name, "study_level": level,
    })
    assert resp.status_code == 201, resp.get_json()
    return resp.get_json()


@pytest.fixture()
def auth(client):
    data = register(client)
    return {"Authorization": f"Bearer {data['access_token']}"}


@pytest.fixture()
def course(client, auth):
    resp = client.post("/api/courses", json={"name": "Data Structures", "code": "cs2028"}, headers=auth)
    assert resp.status_code == 201, resp.get_json()
    return resp.get_json()["course"]


def upload(client, auth, course_id, text=LECTURE, filename="lecture4.md"):
    return client.post(
        f"/api/courses/{course_id}/documents",
        data={"file": (io.BytesIO(text.encode()), filename)},
        headers=auth,
        content_type="multipart/form-data",
    )


@pytest.fixture()
def ready_doc(client, auth, course):
    resp = upload(client, auth, course["id"])
    assert resp.status_code == 202, resp.get_json()
    doc = resp.get_json()["document"]
    assert doc["status"] == "ready", doc
    return doc
