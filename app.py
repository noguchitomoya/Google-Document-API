from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timezone
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

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
TEMPLATE_DIR = DATA_DIR / "templates"
DRAFT_DIR = BASE_DIR / "drafts"
HISTORY_DIR = BASE_DIR / "history"

for directory in (DRAFT_DIR, HISTORY_DIR):
    directory.mkdir(parents=True, exist_ok=True)

TEACHERS_FILE = DATA_DIR / "teachers.json"
STUDENTS_FILE = DATA_DIR / "students.json"
DRIVE_TARGETS_FILE = DATA_DIR / "drive_targets.json"

DEFAULT_TEMPLATE_NAME = os.getenv("DEFAULT_TEMPLATE_NAME", "reflection_template.md")
TEMPLATE_PATH = TEMPLATE_DIR / DEFAULT_TEMPLATE_NAME
ASSET_VERSION = os.getenv("ASSET_VERSION", "20251127")

OAUTH_SCOPES = [
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/documents",
]
OAUTH_TOKEN_FILE = BASE_DIR / "oauth_token.json"

app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("FLASK_SECRET_KEY", "dev-secret")

students_lock = threading.Lock()
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


class GoogleWorkspaceClient:
    """Wrapper around Drive / Docs APIs."""

    def __init__(self, credentials: UserCredentials):
        self.credentials = credentials
        self.drive = build(
            "drive", "v3", credentials=self.credentials, cache_discovery=False
        )
        self.docs = build("docs", "v1", credentials=self.credentials, cache_discovery=False)

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


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def save_json(path: Path, payload: Any):
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_teachers() -> list[dict]:
    return load_json(TEACHERS_FILE, [])


def load_students() -> list[dict]:
    return load_json(STUDENTS_FILE, [])


def save_students(students: list[dict]):
    save_json(STUDENTS_FILE, students)


def load_drive_targets() -> list[dict]:
    return load_json(DRIVE_TARGETS_FILE, [])


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
    return next((t for t in load_teachers() if t["id"] == teacher_id), None)


def find_student(student_id: str) -> dict | None:
    return next((s for s in load_students() if s["id"] == student_id), None)


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
        "drive_targets": load_drive_targets(),
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
def index():
    bootstrap = {
        "teachers": load_teachers(),
        "students": load_students(),
        "driveTargets": load_drive_targets(),
    }
    return render_template(
        "index.html",
        bootstrap=json.dumps(bootstrap, ensure_ascii=False),
        asset_version=ASSET_VERSION,
        oauth_status=oauth_status(),
        oauth_message=pop_oauth_flash(),
    )


@app.get("/oauth/start")
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


@app.post("/api/drafts")
def api_save_draft():
    data = request.get_json(force=True, silent=True) or {}
    student_key = data.get("studentKey")
    payload = data.get("payload")
    if not student_key or not isinstance(payload, dict):
        return jsonify({"error": "studentKey と payload が必要です。"}), 400
    store_draft(student_key, payload)
    return jsonify({"status": "saved", "updatedAt": timestamp()})


@app.get("/api/drafts")
def api_get_draft():
    student_key = request.args.get("studentKey")
    if not student_key:
        return jsonify({"error": "studentKey を指定してください。"}), 400
    draft = load_draft(student_key)
    return jsonify({"draft": draft})


@app.get("/api/context")
def api_context():
    teacher_id = request.args.get("teacherId", "").strip()
    mode = request.args.get("mode", "existing")
    student_id = request.args.get("studentId", "").strip()
    student_name = request.args.get("studentName", "").strip()
    copy_previous = request.args.get("copyPrevious", "false").lower() == "true"
    if not teacher_id:
        return jsonify({"error": "teacherId is required"}), 400
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
def submit():
    form = request.form
    teacher_id = form.get("teacher_id", "")
    mode = form.get("student_mode", "existing")
    copy_previous = form.get("copy_previous") == "on"
    teacher = find_teacher(teacher_id)
    if not teacher:
        return render_template(
            "index.html",
            error="講師を選択してください。",
            bootstrap=json.dumps(base_context(), ensure_ascii=False),
            asset_version=ASSET_VERSION,
            oauth_status=oauth_status(),
            oauth_message=None,
        ), 400

    drive_parent_id = form.get("drive_parent_id") or os.getenv(
        "DEFAULT_DRIVE_PARENT_ID", ""
    )
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

    if not resolved_student.get("driveFolderId"):
        resolved_student["driveFolderId"] = folder_id
        with students_lock:
            students = load_students()
            existing = next((s for s in students if s["id"] == resolved_student["id"]), None)
            if existing:
                existing["driveFolderId"] = folder_id
            else:
                students.append(resolved_student)
            save_students(students)

    append_history(student_key, payload)
    delete_draft(student_key)

    result = {
        "document": document_meta,
        "student": {"name": student_name, "grade": student_grade},
        "teacher": teacher,
        "savedAt": timestamp(),
        "copyPreviousUsed": copy_previous,
    }
    return render_template("complete.html", result=result, asset_version=ASSET_VERSION)


@app.get("/healthz")
def healthcheck():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    app.run(debug=True)
