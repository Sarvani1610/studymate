"""Create a demo account with a course and some sample material.

    python scripts/seed_demo.py            (uses DATABASE_URL from the environment)
    python scripts/seed_demo.py --users 50 (extra accounts for load testing)
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from app import create_app  # noqa: E402
from app.extensions import db  # noqa: E402
from app.models import Course, Document, User  # noqa: E402
from app.services.ingestion import run_ingestion, sha256_bytes  # noqa: E402
from app.services.storage import get_storage  # noqa: E402

SAMPLE_DIR = os.path.join(os.path.dirname(__file__), "..", "sample_data")
DEMO_PASSWORD = "demo-pass-2026"


def ensure_user(email, name, role="student"):
    user = User.query.filter_by(email=email).first()
    if user is None:
        user = User(email=email, display_name=name, role=role)
        user.set_password(DEMO_PASSWORD)
        db.session.add(user)
        db.session.commit()
    return user


def ensure_course(user, code, name):
    course = Course.query.filter_by(owner_id=user.id, code=code).first()
    if course is None:
        course = Course(owner_id=user.id, code=code, name=name, description="Seeded demo course")
        db.session.add(course)
        db.session.commit()
    return course


def load_samples(user, course):
    storage = get_storage()
    for name in sorted(os.listdir(SAMPLE_DIR)):
        path = os.path.join(SAMPLE_DIR, name)
        ext = name.rsplit(".", 1)[-1].lower()
        with open(path, "rb") as fh:
            data = fh.read()
        digest = sha256_bytes(data)
        if Document.query.filter_by(course_id=course.id, sha256=digest).first():
            continue
        key = f"courses/{course.id}/{digest}.{ext}"
        storage.put(key, data)
        doc = Document(course_id=course.id, owner_id=user.id, filename=name, extension=ext,
                       size_bytes=len(data), sha256=digest, storage_key=key)
        db.session.add(doc)
        db.session.commit()
        run_ingestion(doc.id)
        print(f"  ingested {name}: {doc.chunk_count} chunks")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--users", type=int, default=0, help="extra load test accounts to create")
    args = parser.parse_args()

    app = create_app(os.getenv("APP_ENV", "development"))
    with app.app_context():
        db.create_all()
        demo = ensure_user("demo@studymate.local", "Demo Student")
        course = ensure_course(demo, "CS2028", "Data Structures")
        print(f"demo user: {demo.email} / {DEMO_PASSWORD}")
        load_samples(demo, course)

        for i in range(args.users):
            user = ensure_user(f"loadtest{i:04d}@studymate.local", f"Load Test {i}")
            load_course = ensure_course(user, "CS2028", "Data Structures")
            load_samples(user, load_course)
        if args.users:
            print(f"created {args.users} load test users (password {DEMO_PASSWORD})")


if __name__ == "__main__":
    main()
