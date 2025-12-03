from __future__ import annotations

import base64
import json
import os
import re
import sqlite3
import threading
from datetime import datetime, timezone
from email.message import EmailMessage
from functools import wraps
from pathlib import Path
from typing import Any

from flask import (
    Flask,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials as UserCredentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from jinja2 import Template
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
TEMPLATE_DIR = DATA_DIR / "templates"
DRAFT_DIR = BASE_DIR / "drafts"
HISTORY_DIR = BASE_DIR / "history"
DB_PATH = BASE_DIR / "app.db"


def get_db_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_db_connection()
    with conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS teachers (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                subject TEXT,
                email TEXT,
                employee_code TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS students (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                grade TEXT,
                memo TEXT,
                drive_folder_id TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS guardians (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                relationship TEXT,
                email TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS student_guardians (
                student_id TEXT NOT NULL,
                guardian_id TEXT NOT NULL,
                PRIMARY KEY (student_id, guardian_id),
                FOREIGN KEY (student_id) REFERENCES students(id) ON DELETE CASCADE,
                FOREIGN KEY (guardian_id) REFERENCES guardians(id) ON DELETE CASCADE
            )
            """
        )
    conn.close()


def bootstrap_db():
    conn = get_db_connection()
    try:
        bootstrap_teachers(conn)
        bootstrap_students(conn)
        bootstrap_guardians(conn)
        bootstrap_student_guardians(conn)
        conn.commit()
    finally:
        conn.close()


def bootstrap_teachers(conn: sqlite3.Connection):
    data = load_json(TEACHERS_FILE, [])
    if not data:
        return
    existing_ids = {
        row["id"] for row in conn.execute("SELECT id FROM teachers").fetchall()
    }
    for teacher in data:
        if teacher["id"] in existing_ids:
            continue
        employee_code = teacher.get("employeeCode") or teacher["id"]
        password_plain = teacher.get("password") or "password123"
        password_hash = generate_password_hash(password_plain)
        conn.execute(
            """
            INSERT INTO teachers (id, name, subject, email, employee_code, password_hash)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                teacher["id"],
                teacher["name"],
                teacher.get("subject", ""),
                teacher.get("email", ""),
                employee_code,
                password_hash,
            ),
        )


def bootstrap_students(conn: sqlite3.Connection):
    data = load_json(STUDENTS_FILE, [])
    if not data:
        return
    existing_ids = {
        row["id"] for row in conn.execute("SELECT id FROM students").fetchall()
    }
    for student in data:
        if student["id"] in existing_ids:
            continue
        conn.execute(
            """
            INSERT INTO students (id, name, grade, memo, drive_folder_id)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                student["id"],
                student["name"],
                student.get("grade", ""),
                student.get("memo", ""),
                student.get("driveFolderId", ""),
            ),
        )


def bootstrap_guardians(conn: sqlite3.Connection):
    data = load_json(GUARDIANS_FILE, [])
    if not data:
        return
    existing_ids = {
        row["id"] for row in conn.execute("SELECT id FROM guardians").fetchall()
    }
    for guardian in data:
        if guardian["id"] in existing_ids:
            continue
        conn.execute(
            """
            INSERT INTO guardians (id, name, relationship, email)
            VALUES (?, ?, ?, ?)
            """,
            (
                guardian["id"],
                guardian["name"],
                guardian.get("relationship", ""),
                guardian.get("email", ""),
            ),
        )


def bootstrap_student_guardians(conn: sqlite3.Connection):
    data = load_json(STUDENT_GUARDIANS_FILE, {})
    if not isinstance(data, dict):
        return
    existing_links = {
        (row["student_id"], row["guardian_id"])
        for row in conn.execute(
            "SELECT student_id, guardian_id FROM student_guardians"
        ).fetchall()
    }
    for student_id, guardian_ids in data.items():
        for guardian_id in guardian_ids:
            if (student_id, guardian_id) in existing_links:
                continue
            conn.execute(
                """
                INSERT OR IGNORE INTO student_guardians (student_id, guardian_id)
                VALUES (?, ?)
                """,
                (student_id, guardian_id),
            )

for directory in (DRAFT_DIR, HISTORY_DIR):
    directory.mkdir(parents=True, exist_ok=True)

TEACHERS_FILE = DATA_DIR / "teachers.json"
STUDENTS_FILE = DATA_DIR / "students.json"
DRIVE_TARGETS_FILE = DATA_DIR / "drive_targets.json"
GUARDIANS_FILE = DATA_DIR / "guardians.json"
STUDENT_GUARDIANS_FILE = DATA_DIR / "student_guardians.json"

DEFAULT_TEMPLATE_NAME = os.getenv("DEFAULT_TEMPLATE_NAME", "reflection_template.md")
TEMPLATE_PATH = TEMPLATE_DIR / DEFAULT_TEMPLATE_NAME
ASSET_VERSION = os.getenv("ASSET_VERSION", "20251127")
FIXED_DRIVE_PARENT_ID = os.getenv(
    "FIXED_DRIVE_PARENT_ID", "1o8Zxmet43AdIaSrUbVevhdBWeqsKjY-l"
)

OAUTH_SCOPES = [
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/gmail.send",
]
OAUTH_TOKEN_FILE = BASE_DIR / "oauth_token.json"

app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("FLASK_SECRET_KEY", "dev-secret")

history_lock = threading.Lock()


def get_oauth_client_secret_path() -> Path:
    candidates: list[str | Path | None] = [
        os.getenv("GOOGLE_OAUTH_CLIENT_SECRETS"),
        BASE_DIR / "credentials" / "oauth_client_secret.json",
    ]
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate).expanduser()
        if path.exists():
            return path
    raise FileNotFoundError(
        "OAuthクライアントシークレットが見つかりません。環境変数 "
        "GOOGLE_OAUTH_CLIENT_SECRETS を設定するか、credentials/oauth_client_secret.json "
        "を配置してください。"
    )


def save_user_credentials(creds: UserCredentials):
    OAUTH_TOKEN_FILE.write_text(creds.to_json())


def load_user_credentials(auto_refresh: bool = True) -> UserCredentials | None:
    if not OAUTH_TOKEN_FILE.exists():
        return None
    try:
        data = json.loads(OAUTH_TOKEN_FILE.read_text())
    except json.JSONDecodeError:
        return None
    creds = UserCredentials.from_authorized_user_info(data, scopes=OAUTH_SCOPES)
    if auto_refresh and creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        save_user_credentials(creds)
    if creds and creds.valid:
        return creds
    return None


def require_user_credentials() -> UserCredentials:
    creds = load_user_credentials()
    if creds is None:
        raise RuntimeError(
            "Googleアカウントと未連携です。「Googleと接続」ボタンからOAuth認証を完了してください。"
        )
    return creds


def oauth_status() -> dict:
    return {"connected": load_user_credentials(auto_refresh=False) is not None}


def pop_oauth_flash() -> str | None:
    return session.pop("oauth_message", None)


def current_teacher() -> dict | None:
    teacher_id = session.get("teacher_id")
    if not teacher_id:
        return None
    return find_teacher(teacher_id)


def require_current_teacher() -> dict:
    teacher = current_teacher()
    if not teacher:
        raise RuntimeError("講師アカウントでログインしてください。")
    return teacher


def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if "teacher_id" not in session:
            next_url = request.full_path if request.method == "GET" else url_for("index")
            return redirect(url_for("login_form", next=next_url))
        return view(*args, **kwargs)

    return wrapper


@app.context_processor
def inject_current_teacher():
    return {"current_teacher": current_teacher()}


class GoogleWorkspaceClient:
    """Wrapper around Drive / Docs APIs."""

    def __init__(self, credentials: UserCredentials):
        self.credentials = credentials
        self.drive = build(
            "drive", "v3", credentials=self.credentials, cache_discovery=False
        )
        self.docs = build("docs", "v1", credentials=self.credentials, cache_discovery=False)
        self.gmail = build("gmail", "v1", credentials=self.credentials, cache_discovery=False)

    def ensure_student_folder(
        self,
        student_name: str,
        existing_folder_id: str | None,
        parent_folder_id: str | None,
    ) -> str:
        """Return a folder ID that matches the student name."""
        if existing_folder_id:
            return existing_folder_id
        folder = self._find_folder_by_name(student_name, parent_folder_id)
        if folder:
            return folder["id"]
        metadata = {
            "name": student_name,
            "mimeType": "application/vnd.google-apps.folder",
        }
        if parent_folder_id:
            metadata["parents"] = [parent_folder_id]
        created = (
            self.drive.files()
            .create(body=metadata, fields="id,name,webViewLink")
            .execute()
        )
        return created["id"]

    def _find_folder_by_name(
        self, name: str, parent_folder_id: str | None
    ) -> dict | None:
        escaped = name.replace("'", "\\'")
        query_parts = [
            "mimeType = 'application/vnd.google-apps.folder'",
            "trashed = false",
            f"name = '{escaped}'",
        ]
        if parent_folder_id:
            query_parts.append(f"'{parent_folder_id}' in parents")
        query = " and ".join(query_parts)
        response = (
            self.drive.files()
            .list(q=query, fields="files(id,name,webViewLink)", pageSize=1)
            .execute()
        )
        files = response.get("files", [])
        if files:
            return files[0]
        return None

    def create_document(self, title: str, blocks: list[dict], folder_id: str) -> dict:
        """Create Google Doc with prepared content and move into folder."""
        document = self.docs.documents().create(body={"title": title}).execute()
        doc_id = document["documentId"]
        requests = build_doc_requests_from_blocks(blocks)
        if requests:
            self.docs.documents().batchUpdate(
                documentId=doc_id, body={"requests": requests}
            ).execute()
        try:
            file_meta = (
                self.drive.files()
                .get(fileId=doc_id, fields="parents")
                .execute()
            )
            previous_parents = ",".join(file_meta.get("parents", []))
        except HttpError:
            previous_parents = ""
        self.drive.files().update(
            fileId=doc_id,
            addParents=folder_id,
            removeParents=previous_parents,
            fields="id,name,webViewLink,webContentLink,modifiedTime",
        ).execute()
        metadata = (
            self.drive.files()
            .get(
                fileId=doc_id,
                fields="id,name,webViewLink,webContentLink,modifiedTime",
            )
            .execute()
        )
        return metadata

    def send_email(self, message: EmailMessage) -> dict:
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")
        body = {"raw": raw}
        return (
            self.gmail.users()
            .messages()
            .send(userId="me", body=body)
            .execute()
        )

    def share_document_with_guardian(
        self, file_id: str, email: str, allow_comment: bool = True
    ):
        """Grant viewer/commenter access to a guardian email."""
        role = "commenter" if allow_comment else "reader"
        body = {
            "type": "user",
            "role": role,
            "emailAddress": email,
        }
        try:
            (
                self.drive.permissions()
                .create(
                    fileId=file_id,
                    body=body,
                    fields="id",
                    sendNotificationEmail=False,
                )
                .execute()
            )
        except HttpError as exc:  # noqa: BLE001
            app.logger.warning(
                "Failed to add permission for %s on %s: %s", email, file_id, exc
            )


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def save_json(path: Path, payload: Any):
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


init_db()
bootstrap_db()


def load_teachers() -> list[dict]:
    conn = get_db_connection()
    try:
        rows = conn.execute(
            "SELECT id, name, subject, email, employee_code FROM teachers ORDER BY name"
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()

def load_students() -> list[dict]:
    conn = get_db_connection()
    try:
        rows = conn.execute(
            "SELECT id, name, grade, memo, drive_folder_id FROM students ORDER BY name"
        ).fetchall()
        students = []
        for row in rows:
            data = dict(row)
            data["driveFolderId"] = data.pop("drive_folder_id", "")
            students.append(data)
        return students
    finally:
        conn.close()


def load_drive_targets() -> list[dict]:
    return load_json(DRIVE_TARGETS_FILE, [])


def find_guardians_for_student(student_id: str) -> list[dict]:
    conn = get_db_connection()
    try:
        rows = conn.execute(
            """
            SELECT g.id, g.name, g.relationship, g.email
            FROM guardians g
            INNER JOIN student_guardians sg ON sg.guardian_id = g.id
            WHERE sg.student_id = ?
            ORDER BY g.name
            """,
            (student_id,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def read_template_text() -> str:
    if not TEMPLATE_PATH.exists():
        raise FileNotFoundError(
            f"テンプレートファイルが見つかりません: {TEMPLATE_PATH}"
        )
    return TEMPLATE_PATH.read_text(encoding="utf-8")


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def slugify(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    value = value.strip("-")
    return value or "student"


def new_student_id(name: str, existing: set[str]) -> str:
    base = f"student-{slugify(name)}"
    candidate = base
    index = 1
    while candidate in existing:
        candidate = f"{base}-{index}"
        index += 1
    return candidate


def build_student_key(teacher_id: str, identifier: str) -> str:
    return f"{teacher_id}__{identifier}"


def find_teacher(teacher_id: str) -> dict | None:
    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT id, name, subject, email, employee_code FROM teachers WHERE id = ?",
            (teacher_id,),
        ).fetchone()
        if not row:
            return None
        return dict(row)
    finally:
        conn.close()


def find_teacher_by_employee_code(employee_code: str) -> dict | None:
    conn = get_db_connection()
    try:
        row = conn.execute(
            """
            SELECT id, name, subject, email, employee_code, password_hash
            FROM teachers
            WHERE employee_code = ?
            """,
            (employee_code,),
        ).fetchone()
        if not row:
            return None
        return dict(row)
    finally:
        conn.close()


def find_student(student_id: str) -> dict | None:
    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT id, name, grade, memo, drive_folder_id FROM students WHERE id = ?",
            (student_id,),
        ).fetchone()
        if not row:
            return None
        data = dict(row)
        data["driveFolderId"] = data.pop("drive_folder_id", "")
        return data
    finally:
        conn.close()


def upsert_student_record(student: dict):
    conn = get_db_connection()
    try:
        existing = conn.execute(
            "SELECT id, name, grade, memo, drive_folder_id FROM students WHERE id = ?",
            (student["id"],),
        ).fetchone()
        next_payload = {
            "name": student.get("name") or (existing["name"] if existing else ""),
            "grade": student.get("grade") or (existing["grade"] if existing else ""),
            "memo": student.get("memo") or (existing["memo"] if existing else ""),
            "drive_folder_id": student.get("driveFolderId")
            or student.get("drive_folder_id")
            or (existing["drive_folder_id"] if existing else ""),
        }
        if existing:
            conn.execute(
                """
                UPDATE students
                SET name = ?, grade = ?, memo = ?, drive_folder_id = ?
                WHERE id = ?
                """,
                (
                    next_payload["name"],
                    next_payload["grade"],
                    next_payload["memo"],
                    next_payload["drive_folder_id"],
                    student["id"],
                ),
            )
        else:
            conn.execute(
                """
                INSERT INTO students (id, name, grade, memo, drive_folder_id)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    student["id"],
                    next_payload["name"],
                    next_payload["grade"],
                    next_payload["memo"],
                    next_payload["drive_folder_id"],
                ),
            )
        conn.commit()
    finally:
        conn.close()


def store_draft(student_key: str, payload: dict):
    draft_path = DRAFT_DIR / f"{student_key}.json"
    content = {
        "studentKey": student_key,
        "payload": payload,
        "updatedAt": timestamp(),
    }
    save_json(draft_path, content)


def load_draft(student_key: str) -> dict | None:
    path = DRAFT_DIR / f"{student_key}.json"
    if not path.exists():
        return None
    return load_json(path, None)


def delete_draft(student_key: str):
    path = DRAFT_DIR / f"{student_key}.json"
    path.unlink(missing_ok=True)


def append_history(student_key: str, payload: dict):
    path = HISTORY_DIR / f"{student_key}.json"
    with history_lock:
        history = load_json(path, {"entries": []})
        entries = history.get("entries", [])
        entries.append({"payload": payload, "savedAt": timestamp()})
        history["entries"] = entries[-20:]  # keep recent 20
        save_json(path, history)


def load_last_submission(student_key: str) -> dict | None:
    path = HISTORY_DIR / f"{student_key}.json"
    history = load_json(path, None)
    if not history:
        return None
    entries = history.get("entries") or []
    if not entries:
        return None
    return entries[-1]


def base_context(**overrides) -> dict:
    context = {
        "teachers": load_teachers(),
        "students": load_students(),
        "driveTargets": load_drive_targets(),
        "currentTeacher": current_teacher(),
    }
    context.update(overrides)
    return context


def build_form_payload(form: dict, teacher: dict, student_name: str) -> dict:
    default_date = datetime.now().strftime("%Y-%m-%d")
    return {
        "teacher_id": teacher["id"],
        "teacher_name": teacher["name"],
        "teacher_subject": teacher.get("subject", ""),
        "student_name": student_name,
        "lesson_date": form.get("lesson_date") or default_date,
        "lesson_goal": form.get("lesson_goal", "").strip(),
        "lesson_summary": form.get("lesson_summary", "").strip(),
        "student_reaction": form.get("student_reaction", "").strip(),
        "next_actions": form.get("next_actions", "").strip(),
        "memo": form.get("memo", "").strip(),
    }


def render_document_blocks(payload: dict) -> list[dict]:
    template_text = read_template_text()
    template = Template(template_text)
    rendered = template.render(**payload)
    return parse_markdown_blocks(rendered)


def parse_markdown_blocks(text: str) -> list[dict]:
    blocks: list[dict] = []
    paragraph_buffer: list[str] = []
    consecutive_empty = 0

    def flush_paragraph():
        nonlocal consecutive_empty
        if paragraph_buffer:
            paragraph_text = "\n".join(paragraph_buffer).strip()
            if paragraph_text:
                blocks.append({"type": "paragraph", "text": paragraph_text})
            paragraph_buffer.clear()
            consecutive_empty = 0

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if stripped.startswith("## "):
            flush_paragraph()
            blocks.append({"type": "heading2", "text": stripped[3:].strip()})
            consecutive_empty = 0
        elif stripped.startswith("# "):
            flush_paragraph()
            blocks.append({"type": "heading1", "text": stripped[2:].strip()})
            consecutive_empty = 0
        elif stripped.startswith("- "):
            flush_paragraph()
            blocks.append({"type": "bullet", "text": stripped[2:].strip()})
            consecutive_empty = 0
        elif stripped == "":
            flush_paragraph()
            if consecutive_empty == 0:
                blocks.append({"type": "empty"})
            consecutive_empty += 1
        else:
            paragraph_buffer.append(line)
            consecutive_empty = 0

    flush_paragraph()
    return blocks


def build_doc_requests_from_blocks(blocks: list[dict]) -> list[dict]:
    requests: list[dict] = []
    index = 1

    for block in blocks:
        block_type = block.get("type")
        text = block.get("text", "")

        if block_type == "empty":
            insert_text = "\n"
        elif block_type == "bullet":
            insert_text = f"{text}\n"
        else:
            insert_text = f"{text}\n"

        requests.append(
            {"insertText": {"location": {"index": index}, "text": insert_text}}
        )
        start = index
        end = index + len(insert_text)

        if block_type == "heading1":
            requests.append(
                {
                    "updateParagraphStyle": {
                        "range": {"startIndex": start, "endIndex": end},
                        "paragraphStyle": {"namedStyleType": "HEADING_1"},
                        "fields": "namedStyleType",
                    }
                }
            )
        elif block_type == "heading2":
            requests.append(
                {
                    "updateParagraphStyle": {
                        "range": {"startIndex": start, "endIndex": end},
                        "paragraphStyle": {"namedStyleType": "HEADING_2"},
                        "fields": "namedStyleType",
                    }
                }
            )
        elif block_type == "bullet":
            requests.append(
                {
                    "createParagraphBullets": {
                        "range": {"startIndex": start, "endIndex": end},
                        "bulletPreset": "BULLET_DISC_CIRCLE_SQUARE",
                    }
                }
            )

        index = end

    return requests


@app.get("/")
@login_required
def index():
    teacher = require_current_teacher()
    bootstrap = {
        "teachers": load_teachers(),
        "students": load_students(),
        "driveTargets": load_drive_targets(),
        "currentTeacher": teacher,
        "fixedDriveParentId": FIXED_DRIVE_PARENT_ID,
        "fixedDriveFolderLink": f"https://drive.google.com/drive/folders/{FIXED_DRIVE_PARENT_ID}",
    }
    return render_template(
        "index.html",
        bootstrap=json.dumps(bootstrap, ensure_ascii=False),
        asset_version=ASSET_VERSION,
        oauth_status=oauth_status(),
        oauth_message=pop_oauth_flash(),
    )


@app.get("/oauth/start")
@login_required
def oauth_start():
    try:
        flow = Flow.from_client_secrets_file(
            str(get_oauth_client_secret_path()),
            scopes=OAUTH_SCOPES,
            redirect_uri=url_for("oauth_callback", _external=True),
        )
    except Exception as exc:  # noqa: BLE001
        session["oauth_message"] = f"OAuth設定エラー: {exc}"
        return redirect(url_for("index"))

    authorization_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )
    session["oauth_state"] = state
    return redirect(authorization_url)


@app.get("/oauth/callback")
def oauth_callback():
    saved_state = session.get("oauth_state")
    incoming_state = request.args.get("state")
    if not saved_state or saved_state != incoming_state:
        session["oauth_message"] = "OAuth認証の状態が一致しません。もう一度お試しください。"
        return redirect(url_for("index"))

    try:
        flow = Flow.from_client_secrets_file(
            str(get_oauth_client_secret_path()),
            scopes=OAUTH_SCOPES,
            redirect_uri=url_for("oauth_callback", _external=True),
            state=saved_state,
        )
        flow.fetch_token(authorization_response=request.url)
    except Exception as exc:  # noqa: BLE001
        session["oauth_message"] = f"Google認証に失敗しました: {exc}"
        session.pop("oauth_state", None)
        return redirect(url_for("index"))

    creds = flow.credentials
    save_user_credentials(creds)
    session["oauth_message"] = "Googleアカウントとの接続が完了しました。"
    session.pop("oauth_state", None)
    return redirect(url_for("index"))


@app.get("/login")
def login_form():
    if session.get("teacher_id"):
        return redirect(url_for("index"))
    next_url = request.args.get("next") or url_for("index")
    return render_template(
        "login.html",
        error=None,
        next_url=next_url,
        asset_version=ASSET_VERSION,
        employee_code="",
    )


@app.post("/login")
def login_submit():
    employee_code = request.form.get("employee_code", "").strip()
    password = request.form.get("password", "")
    next_url = request.form.get("next") or url_for("index")
    teacher = find_teacher_by_employee_code(employee_code)
    if not teacher or not check_password_hash(teacher["password_hash"], password):
        return render_template(
            "login.html",
            error="社員番号またはパスワードが正しくありません。",
            next_url=next_url,
            asset_version=ASSET_VERSION,
            employee_code=employee_code,
        ), 401
    session["teacher_id"] = teacher["id"]
    return redirect(next_url)


@app.route("/logout", methods=["GET", "POST"])
def logout():
    session.pop("teacher_id", None)
    session.pop("oauth_state", None)
    session.pop("oauth_message", None)
    session.modified = True
    return redirect(url_for("login_form"))


@app.post("/api/drafts")
@login_required
def api_save_draft():
    data = request.get_json(force=True, silent=True) or {}
    student_key = data.get("studentKey")
    payload = data.get("payload")
    if not student_key or not isinstance(payload, dict):
        return jsonify({"error": "studentKey と payload が必要です。"}), 400
    store_draft(student_key, payload)
    return jsonify({"status": "saved", "updatedAt": timestamp()})


@app.get("/api/drafts")
@login_required
def api_get_draft():
    student_key = request.args.get("studentKey")
    if not student_key:
        return jsonify({"error": "studentKey を指定してください。"}), 400
    draft = load_draft(student_key)
    return jsonify({"draft": draft})


@app.get("/api/context")
@login_required
def api_context():
    teacher = require_current_teacher()
    teacher_id = teacher["id"]
    mode = request.args.get("mode", "existing")
    student_id = request.args.get("studentId", "").strip()
    student_name = request.args.get("studentName", "").strip()
    copy_previous = request.args.get("copyPrevious", "false").lower() == "true"
    if mode == "existing" and not student_id:
        return jsonify({"error": "studentId is required"}), 400
    if mode == "new" and not student_name:
        return jsonify({"error": "studentName is required"}), 400
    if mode == "existing":
        identifier = student_id
    else:
        existing_ids = {s["id"] for s in load_students()}
        identifier = new_student_id(student_name, existing_ids)
    student_key = build_student_key(teacher_id, identifier)
    draft = load_draft(student_key)
    previous = load_last_submission(student_key) if copy_previous else None
    response = {
        "studentKey": student_key,
        "studentIdentifier": identifier,
        "draft": draft,
        "previous": previous,
        "templateFields": {
            "lesson_date": datetime.now().strftime("%Y-%m-%d"),
            "lesson_goal": "",
            "lesson_summary": "",
            "student_reaction": "",
            "next_actions": "",
            "memo": "",
        },
    }
    return jsonify(response)


@app.post("/submit")
@login_required
def submit():
    form = request.form
    teacher = require_current_teacher()
    teacher_id = teacher["id"]
    mode = form.get("student_mode", "existing")
    copy_previous = form.get("copy_previous") == "on"

    drive_parent_id = FIXED_DRIVE_PARENT_ID
    submitted_student_key = form.get("student_key") or ""
    submitted_identifier = form.get("student_identifier") or ""
    resolved_student = None
    student_identifier = ""
    student_name = ""
    student_grade = ""

    if mode == "existing":
        student_id = form.get("student_id", "")
        resolved_student = find_student(student_id)
        if not resolved_student:
            return render_template(
                "index.html",
                error="生徒を選択してください。",
                bootstrap=json.dumps(base_context(), ensure_ascii=False),
                asset_version=ASSET_VERSION,
                oauth_status=oauth_status(),
                oauth_message=None,
            ), 400
        student_identifier = resolved_student["id"]
        student_name = resolved_student["name"]
        student_grade = resolved_student.get("grade", "")
        if submitted_identifier and submitted_identifier != student_identifier:
            student_identifier = submitted_identifier
    else:
        student_name = form.get("new_student_name", "").strip()
        if not student_name:
            return render_template(
                "index.html",
                error="新規生徒の名前を入力してください。",
                bootstrap=json.dumps(base_context(), ensure_ascii=False),
                asset_version=ASSET_VERSION,
                oauth_status=oauth_status(),
                oauth_message=None,
            ), 400
        student_grade = form.get("new_student_grade", "")
        memo = form.get("new_student_memo", "")
        existing_ids = {s["id"] for s in load_students()}
        if submitted_identifier:
            student_identifier = submitted_identifier
        else:
            student_identifier = new_student_id(student_name, existing_ids)
        resolved_student = {
            "id": student_identifier,
            "name": student_name,
            "grade": student_grade,
            "memo": memo,
            "driveFolderId": "",
        }

    if submitted_student_key:
        student_key = submitted_student_key
    else:
        student_key = build_student_key(teacher_id, student_identifier)

    payload = build_form_payload(form, teacher, student_name)
    payload["student_grade"] = student_grade

    document_blocks = render_document_blocks(payload)

    try:
        user_credentials = require_user_credentials()
        client = GoogleWorkspaceClient(user_credentials)
        folder_id = client.ensure_student_folder(
            student_name, resolved_student.get("driveFolderId"), drive_parent_id
        )
        doc_title = f"{student_name}_{payload['lesson_date']}"
        document_meta = client.create_document(doc_title, document_blocks, folder_id)
    except Exception as exc:  # noqa: BLE001
        app.logger.exception("Failed to push document")
        return render_template(
            "index.html",
            error=f"Googleドキュメントの作成に失敗しました: {exc}",
            bootstrap=json.dumps(base_context(), ensure_ascii=False),
            asset_version=ASSET_VERSION,
            oauth_status=oauth_status(),
            oauth_message=None,
        ), 500

    resolved_student["driveFolderId"] = folder_id
    upsert_student_record(resolved_student)

    append_history(student_key, payload)
    delete_draft(student_key)

    guardian_notifications: list[dict] = []
    try:
        guardian_notifications = notify_guardians(
            client=client,
            student=resolved_student,
            teacher=teacher,
            document_meta=document_meta,
            payload=payload,
        )
    except Exception as exc:  # noqa: BLE001
        app.logger.exception("Failed to notify guardians: %s", exc)

    folder_link = (
        f"https://drive.google.com/drive/folders/{folder_id}" if folder_id else ""
    )
    result = {
        "document": document_meta,
        "student": {
            "id": resolved_student["id"],
            "name": student_name,
            "grade": student_grade,
            "driveFolderId": folder_id,
        },
        "teacher": teacher,
        "savedAt": timestamp(),
        "copyPreviousUsed": copy_previous,
        "guardianNotifications": guardian_notifications,
        "folderLink": folder_link,
    }
    return render_template("complete.html", result=result, asset_version=ASSET_VERSION)


def notify_guardians(
    client: GoogleWorkspaceClient,
    student: dict,
    teacher: dict,
    document_meta: dict,
    payload: dict,
) -> list[dict]:
    student_id = student.get("id", "")
    if not student_id:
        return []
    guardians = find_guardians_for_student(student_id)
    notifications: list[dict] = []
    if not guardians:
        app.logger.info("No guardians linked to student %s", student_id)
        return notifications
    # 代表保護者（先頭1名）のみに通知
    guardians = guardians[:1]
    for guardian in guardians:
        guardian_email = (guardian.get("email") or "").strip()
        if not guardian_email:
            notifications.append(
                {"guardian": guardian, "status": "skipped", "reason": "missing_email"}
            )
            continue
        try:
            client.share_document_with_guardian(
                document_meta["id"], guardian_email, allow_comment=True
            )
        except Exception as exc:  # noqa: BLE001
            app.logger.warning(
                "Permission grant failed for %s: %s", guardian_email, exc
            )
            notifications.append(
                {"guardian": guardian, "status": "failed", "reason": f"permission: {exc}"}
            )
            continue
        message = build_guardian_email(
            guardian=guardian,
            student=student,
            teacher=teacher,
            document_meta=document_meta,
            payload=payload,
        )
        try:
            client.send_email(message)
            notifications.append({"guardian": guardian, "status": "sent"})
        except HttpError as exc:
            app.logger.exception(
                "Failed to send email to %s: %s", guardian_email, exc
            )
            notifications.append(
                {"guardian": guardian, "status": "failed", "reason": str(exc)}
            )
    return notifications


def build_guardian_email(
    guardian: dict,
    student: dict,
    teacher: dict,
    document_meta: dict,
    payload: dict,
) -> EmailMessage:
    student_name = student.get("name", "")
    guardian_name = guardian.get("name", "")
    teacher_name = teacher.get("name", "")
    lesson_date = payload.get("lesson_date", "")
    summary = payload.get("lesson_summary", "").strip() or "（記入なし）"
    next_actions = payload.get("next_actions", "").strip() or "（記入なし）"
    doc_url = document_meta.get("webViewLink", "")

    message = EmailMessage()
    message["To"] = guardian.get("email")
    from_email = teacher.get("email") or "no-reply@example.com"
    message["From"] = f"{teacher_name} <{from_email}>"
    message["Subject"] = f"{student_name}さんの授業振り返り（{lesson_date}）"

    body = "\n".join(
        [
            f"{guardian_name} 様",
            "",
            "いつもお世話になっております。",
            f"{teacher_name}です。",
            "",
            f"{student_name}さんの授業振り返りシートを作成しました。",
            f"以下のリンクよりご確認ください: {doc_url}",
            "",
            f"◆ 授業日: {lesson_date}",
            f"◆ 概要: {summary}",
            f"◆ 次回に向けて: {next_actions}",
            "",
            "ご不明な点がございましたらお気軽にご連絡ください。",
        ]
    )
    message.set_content(body)
    return message


@app.get("/healthz")
def healthcheck():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    app.run(debug=True)
