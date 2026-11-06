"""Web service for recorded videos: upload, look up by company name, serve.

Run:  python web_service.py
Listens on 0.0.0.0:8000; override with the FLASK_HOST / FLASK_PORT environment variables.

Endpoints:
    POST /upload                         multipart: file, company_name -> {company_name, video_url}
    GET  /get_video_url?company_name=... -> {company_name, video_url}, 404 if unknown
    GET  /media/<filename>               the stored video file (supports HTTP Range)
    GET  /videos                         all records
"""

import hashlib
import logging
import mimetypes
import os
import sqlite3
import tempfile
import threading

from flask import Flask, g, jsonify, request, send_from_directory, url_for
from werkzeug.exceptions import HTTPException
from werkzeug.utils import secure_filename

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "videos.db")
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
MAX_UPLOAD_MB = 500
ALLOWED_EXTENSIONS = {".webm", ".mp4", ".mkv", ".mov", ".ogv"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    company_name TEXT    NOT NULL UNIQUE,
    file_path    TEXT    NOT NULL,   -- absolute path to the file on disk
    video_url    TEXT    NOT NULL,   -- public URL to the video
    created_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at   TEXT    NOT NULL DEFAULT (datetime('now'))
);
"""

UPSERT = """
INSERT INTO videos (company_name, file_path, video_url) VALUES (?, ?, ?)
ON CONFLICT(company_name) DO UPDATE SET
    file_path  = excluded.file_path,
    video_url  = excluded.video_url,
    updated_at = datetime('now')
"""

# Not every OS registers .webm; without it the file would be served as octet-stream.
mimetypes.add_type("video/webm", ".webm")

log = logging.getLogger("web_service")


def _connect(db_path):
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def init_db(db_path):
    with _connect(db_path) as conn:
        # WAL: readers do not block the writer, so parallel requests don't hit "database is locked".
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)


def _file_stem(company_name):
    """ASCII file name for a company ("ACME GmbH" -> "ACME_GmbH")."""
    stem = secure_filename(company_name)[:100]
    if not stem:
        # secure_filename drops non-ASCII, so e.g. a Cyrillic-only name becomes empty.
        stem = "video_" + hashlib.sha1(company_name.encode("utf-8")).hexdigest()[:10]
    return stem


def create_app(db_path=DB_PATH, upload_dir=UPLOAD_DIR):
    app = Flask(__name__)
    app.config.update(DB_PATH=db_path, UPLOAD_DIR=upload_dir,
                      MAX_CONTENT_LENGTH=MAX_UPLOAD_MB * 1024 * 1024)
    app.json.sort_keys = False  # keep {"company_name", "video_url"} in the documented order
    app.json.ensure_ascii = False  # "Ромашка", not "Ро..."
    os.makedirs(upload_dir, exist_ok=True)
    init_db(db_path)
    # Choosing a file name and writing the record must not interleave between requests,
    # or two companies with the same sanitised name could end up sharing one file.
    upload_lock = threading.Lock()

    def db():
        """One SQLite connection per request (the dev server is threaded)."""
        if "db" not in g:
            g.db = _connect(app.config["DB_PATH"])
        return g.db

    @app.teardown_appcontext
    def close_db(_exc):
        conn = g.pop("db", None)
        if conn is not None:
            conn.close()

    @app.errorhandler(HTTPException)
    def json_error(e):
        return jsonify(error=e.description), e.code

    def bad_request(message):
        return jsonify(error=message), 400

    def pick_filename(conn, company_name, ext):
        """File name for this company; never one that belongs to another company."""
        stem = _file_stem(company_name)
        name, n = stem + ext, 2
        while True:
            path = os.path.join(app.config["UPLOAD_DIR"], name)
            owner = conn.execute("SELECT company_name FROM videos WHERE file_path = ?",
                                 (path,)).fetchone()
            if owner is None or owner["company_name"] == company_name:
                return name
            name, n = f"{stem}_{n}{ext}", n + 1

    @app.post("/upload")
    def upload():
        file = request.files.get("file")
        company_name = (request.form.get("company_name") or "").strip()
        if file is None or not file.filename:
            return bad_request("missing 'file' (multipart/form-data)")
        if not company_name:
            return bad_request("missing 'company_name'")
        ext = os.path.splitext(file.filename)[1].lower() or ".webm"
        if ext not in ALLOWED_EXTENSIONS:
            return bad_request(f"unsupported file type {ext!r}; allowed: "
                               + ", ".join(sorted(ALLOWED_EXTENSIONS)))

        upload_dir = app.config["UPLOAD_DIR"]
        conn = db()
        with upload_lock:
            name = pick_filename(conn, company_name, ext)
            path = os.path.join(upload_dir, name)
            # Write to a temp file first, then swap it in: a re-upload never leaves
            # a half-written video behind the URL.
            fd, tmp = tempfile.mkstemp(dir=upload_dir, suffix=".part")
            os.close(fd)
            try:
                file.save(tmp)
                if os.path.getsize(tmp) == 0:
                    return bad_request("uploaded file is empty")
                os.replace(tmp, path)
            finally:
                if os.path.exists(tmp):
                    os.remove(tmp)

            old = conn.execute("SELECT file_path FROM videos WHERE company_name = ?",
                               (company_name,)).fetchone()
            video_url = url_for("media", filename=name, _external=True)  # from request.host_url
            conn.execute(UPSERT, (company_name, path, video_url))
            conn.commit()
            # Re-upload with another extension: drop the previous file of this company.
            if old and old["file_path"] != path and os.path.exists(old["file_path"]):
                os.remove(old["file_path"])

        log.info("stored %r -> %s", company_name, path)
        return jsonify(company_name=company_name, video_url=video_url)

    @app.get("/get_video_url")
    def get_video_url():
        company_name = (request.args.get("company_name") or "").strip()
        if not company_name:
            return bad_request("missing 'company_name' query parameter")
        row = db().execute("SELECT company_name, video_url FROM videos WHERE company_name = ?",
                           (company_name,)).fetchone()
        if row is None:
            return jsonify(error="company not found", company_name=company_name), 404
        return jsonify(company_name=row["company_name"], video_url=row["video_url"])

    @app.get("/media/<path:filename>")
    def media(filename):
        # send_from_directory rejects "../" escapes and answers Range requests (seeking).
        return send_from_directory(app.config["UPLOAD_DIR"], filename)

    @app.get("/videos")
    def videos():
        rows = db().execute("SELECT id, company_name, file_path, video_url, created_at,"
                            " updated_at FROM videos ORDER BY id").fetchall()
        return jsonify([dict(r) for r in rows])

    return app


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    host = os.environ.get("FLASK_HOST", "0.0.0.0")
    port = int(os.environ.get("FLASK_PORT", "8000"))
    create_app().run(host=host, port=port, threaded=True)
