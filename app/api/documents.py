import os
import re

from flask import Blueprint, Response, current_app, g, jsonify, redirect, request

from ..auth.decorators import login_required
from ..errors import APIError, ValidationError
from ..extensions import db
from ..models import Chunk, Document
from ..services import cache, ingestion
from ..services.rate_limit import rate_limit
from ..services.storage import get_storage
from ..services.summarizer import summarize_document
from ..utils.pagination import paginate
from .common import owned_course, owned_document

bp = Blueprint("documents", __name__, url_prefix="/api")

MAGIC = {
    "pdf": (b"%PDF",),
    "docx": (b"PK\x03\x04",),
    "pptx": (b"PK\x03\x04",),
}
CONTENT_TYPES = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "txt": "text/plain",
    "md": "text/markdown",
}


def safe_filename(name):
    name = os.path.basename(name or "").strip()
    name = re.sub(r"[^A-Za-z0-9._\- ]+", "_", name)
    return name[:200] or "upload"


def _check_file(filename, data):
    if "." not in filename:
        raise ValidationError("File needs an extension", details={"file": "missing extension"})
    ext = filename.rsplit(".", 1)[1].lower()
    allowed = current_app.config["ALLOWED_EXTENSIONS"]
    if ext not in allowed:
        raise ValidationError(
            f"Unsupported file type .{ext}", details={"file": "allowed: " + ", ".join(sorted(allowed))}
        )
    max_bytes = current_app.config["MAX_UPLOAD_MB"] * 1024 * 1024
    if len(data) == 0:
        raise ValidationError("File is empty", details={"file": "empty"})
    if len(data) > max_bytes:
        raise APIError(f"File is larger than {current_app.config['MAX_UPLOAD_MB']} MB", status=413,
                       code="file_too_large")
    signatures = MAGIC.get(ext)
    if signatures and not any(data.startswith(sig) for sig in signatures):
        raise ValidationError("File contents do not match its extension", details={"file": "bad signature"})
    if ext in ("txt", "md") and b"\x00" in data[:4096]:
        raise ValidationError("That does not look like a text file", details={"file": "binary content"})
    return ext


@bp.get("/courses/<int:course_id>/documents")
@login_required
def list_documents(course_id):
    owned_course(course_id)
    query = Document.query.filter_by(course_id=course_id)
    status = request.args.get("status")
    if status:
        if status not in Document.STATUSES:
            raise ValidationError("Unknown status filter")
        query = query.filter_by(status=status)
    items, meta = paginate(query.order_by(Document.created_at.desc()))
    return jsonify({"items": [d.to_dict() for d in items], "meta": meta})


@bp.post("/courses/<int:course_id>/documents")
@login_required
@rate_limit("upload")
def upload_document(course_id):
    course = owned_course(course_id)
    upload = request.files.get("file")
    if upload is None:
        raise ValidationError("Attach the file as multipart field 'file'", details={"file": "is required"})
    filename = safe_filename(upload.filename)
    data = upload.read()
    ext = _check_file(filename, data)
    digest = ingestion.sha256_bytes(data)

    existing = Document.query.filter_by(course_id=course.id, sha256=digest).first()
    if existing:
        return jsonify({"document": existing.to_dict(), "duplicate": True}), 200

    key = f"courses/{course.id}/{digest}.{ext}"
    get_storage().put(key, data, CONTENT_TYPES.get(ext))
    title = (request.form.get("title") or "").strip()[:255] or None
    doc = Document(
        course_id=course.id,
        owner_id=g.user.id,
        filename=filename,
        title=title,
        extension=ext,
        content_type=CONTENT_TYPES.get(ext),
        size_bytes=len(data),
        sha256=digest,
        storage_key=key,
        status="pending",
    )
    db.session.add(doc)
    db.session.commit()
    mode = ingestion.dispatch(doc.id)
    db.session.refresh(doc)
    return jsonify({"document": doc.to_dict(), "duplicate": False, "processing": mode}), 202


@bp.get("/documents/<int:document_id>")
@login_required
def get_document(document_id):
    doc = owned_document(document_id)
    sections = (
        db.session.query(Chunk.section)
        .filter(Chunk.document_id == doc.id, Chunk.section.isnot(None))
        .distinct()
        .limit(50)
        .all()
    )
    return jsonify({"document": doc.to_dict(), "sections": [s[0] for s in sections]})


@bp.get("/documents/<int:document_id>/chunks")
@login_required
def list_chunks(document_id):
    doc = owned_document(document_id)
    query = Chunk.query.filter_by(document_id=doc.id).order_by(Chunk.ordinal)
    items, meta = paginate(query, default_per_page=25)
    return jsonify({"items": [c.to_dict() for c in items], "meta": meta})


@bp.post("/documents/<int:document_id>/reprocess")
@login_required
@rate_limit("upload")
def reprocess_document(document_id):
    doc = owned_document(document_id)
    if doc.status == "processing":
        raise APIError("Document is already being processed", status=409, code="in_progress")
    doc.version += 1
    doc.status = "pending"
    db.session.commit()
    mode = ingestion.dispatch(doc.id)
    db.session.refresh(doc)
    return jsonify({"document": doc.to_dict(), "processing": mode}), 202


@bp.get("/documents/<int:document_id>/download")
@login_required
def download_document(document_id):
    doc = owned_document(document_id)
    storage = get_storage()
    url = storage.url(doc.storage_key)
    if url:
        return redirect(url, code=302)
    data = storage.get(doc.storage_key)
    return Response(
        data,
        mimetype=doc.content_type or "application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{doc.filename}"'},
    )


@bp.delete("/documents/<int:document_id>")
@login_required
def delete_document(document_id):
    doc = owned_document(document_id)
    course_id, key = doc.course_id, doc.storage_key
    db.session.delete(doc)
    db.session.commit()
    try:
        get_storage().delete(key)
    except Exception:
        current_app.logger.warning("could not delete stored file %s", key)
    cache.bump_course_version(course_id)
    return "", 204


@bp.get("/documents/<int:document_id>/summary")
@login_required
@rate_limit("llm")
def get_summary(document_id):
    return _summary(document_id, force=False)


@bp.post("/documents/<int:document_id>/summary")
@login_required
@rate_limit("llm")
def regenerate_summary(document_id):
    return _summary(document_id, force=True)


def _summary(document_id, force):
    doc = owned_document(document_id)
    if doc.status != "ready":
        raise APIError("Document is not ready yet", status=409, code="not_ready",
                       details={"status": doc.status})
    style = request.args.get("style", "brief")
    if style not in ("brief", "detailed", "bullets"):
        raise ValidationError("style must be brief, detailed or bullets")
    summary, from_cache = summarize_document(g.user, doc, style=style, force=force)
    return jsonify({"summary": summary.to_dict(), "cached": from_cache})
