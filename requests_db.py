# requests_db.py（互換版：講師UI/受講者UI 共通DB）
"""Deploy requests SQLite helper.

方針
- 既存DBを壊さない（列追加のみ）
- INSERTは列名を明示して「列数不一致」を防ぐ
- 講師UI(students_deployer.py) と 受講者UI(students_redeploy.py) の両方から呼ばれる関数を揃える
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional


@dataclass
class RequestRow:
    id: int
    student_id: str
    app_name: str
    request_kind: str
    status: str
    created_at: str
    
    app_desc: str | None = None
    python_version: str | None = None
    java_release: str | None = None
    java_build: str | None = None
    port: int | None = None

    last_upload: str | None = None
    last_upload_at: str | None = None


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {r[1] for r in rows}  # name


def _add_column_if_missing(conn: sqlite3.Connection, table: str, col: str, ddl_type: str) -> None:
    cols = _table_columns(conn, table)
    if col in cols:
        return
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl_type}")


def init_db(db_path: str | Path) -> None:
    db_path = str(db_path)
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    with _connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                student_id TEXT NOT NULL,
                app_name TEXT NOT NULL,
                request_kind TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )

        # 互換用の追加列（過去のDBとの差分吸収）
        _add_column_if_missing(conn, "requests", "python_version", "TEXT")
        _add_column_if_missing(conn, "requests", "java_release", "TEXT")
        _add_column_if_missing(conn, "requests", "java_build", "TEXT")
        _add_column_if_missing(conn, "requests", "port", "INTEGER")
        _add_column_if_missing(conn, "requests", "last_upload", "TEXT")
        _add_column_if_missing(conn, "requests", "last_upload_at", "TEXT")
        _add_column_if_missing(conn, "requests", "app_desc", "TEXT")

        # 重複申請防止のためのインデックス（存在してもOK）
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_requests_student_app
            ON requests(student_id, app_name)
            """
        )


        # 講師アカウント用テーブル
        _ensure_teachers_table(conn)
        _ensure_line_notify_settings_table(conn) 


def _row_to_obj(r: sqlite3.Row) -> RequestRow:
    return RequestRow(
        id=int(r["id"]),
        student_id=str(r["student_id"]),
        app_name=str(r["app_name"]),
        request_kind=str(r["request_kind"]),
        status=str(r["status"]),
        created_at=str(r["created_at"]),
        app_desc=r["app_desc"],
        python_version=r["python_version"],
        java_release=r["java_release"],
        java_build=r["java_build"],
        port=r["port"],
        last_upload=r["last_upload"],
        last_upload_at=r["last_upload_at"],
    )


def add_request(
    db_path: str | Path,
    *,
    student_id: str,
    app_name: str,
    request_kind: str,
    status: str = "received",
    python_version: str | None = None,
    java_release: str | None = None,
    java_build: str | None = None,
    app_desc: str | None = None,
) -> int:
    """受講者が「新規申請」する（メタ情報のみ）。"""
    existing = get_by_student_app(db_path, student_id, app_name)
    if existing is not None:
        raise ValueError("同じアプリ名は申請できません（既に存在します）。")

    db_path = str(db_path)
    init_db(db_path)

    created_at = _now()

    with _connect(db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO requests (
                student_id, app_name, request_kind, status, created_at,
                app_desc,
                python_version, java_release, java_build
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (student_id, app_name, request_kind, status, created_at, app_desc, python_version, java_release, java_build),
        )
        return int(cur.lastrowid)


def get_by_id(db_path: str | Path, request_id: int) -> Optional[RequestRow]:
    db_path = str(db_path)
    init_db(db_path)

    with _connect(db_path) as conn:
        r = conn.execute("SELECT * FROM requests WHERE id = ?", (request_id,)).fetchone()
        return _row_to_obj(r) if r else None


def get_by_student_app(db_path: str | Path, student_id: str, app_name: str) -> Optional[RequestRow]:
    db_path = str(db_path)
    init_db(db_path)

    with _connect(db_path) as conn:
        r = conn.execute(
            "SELECT * FROM requests WHERE student_id = ? AND app_name = ? ORDER BY id DESC LIMIT 1",
            (student_id, app_name),
        ).fetchone()
        return _row_to_obj(r) if r else None


def list_by_student(db_path: str | Path, student_id: str) -> list[RequestRow]:
    db_path = str(db_path)
    init_db(db_path)

    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM requests WHERE student_id = ? ORDER BY id DESC",
            (student_id,),
        ).fetchall()
        return [_row_to_obj(r) for r in rows]


def search(
    db_path: str | Path,
    *,
    status: str = "received",
    student_id: str = "",
    app_name: str = "",
) -> list[RequestRow]:
    """講師UIの一覧検索。"""
    db_path = str(db_path)
    init_db(db_path)

    where: list[str] = []
    params: list[object] = []

    if status and status != "all":
        where.append("status = ?")
        params.append(status)

    if student_id:
        where.append("student_id LIKE ?")
        params.append(f"%{student_id}%")

    if app_name:
        where.append("app_name LIKE ?")
        params.append(f"%{app_name}%")

    sql = "SELECT * FROM requests"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC"

    with _connect(db_path) as conn:
        rows = conn.execute(sql, params).fetchall()
        return [_row_to_obj(r) for r in rows]


def mark_done(db_path: str | Path, request_id: int, port: int | None = None) -> None:
    db_path = str(db_path)
    init_db(db_path)

    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE requests SET status = 'done', port = COALESCE(?, port) WHERE id = ?",
            (port, request_id),
        )





def mark_error(db_path: str | Path, request_id: int, port: int | None = None) -> None:
    db_path = str(db_path)
    init_db(db_path)

    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE requests SET status = 'error', port = COALESCE(?, port) WHERE id = ?",
            (port, request_id),
        )


def mark_public(db_path: str | Path, request_id: int, port: int | None = None) -> None:
    """Mark request as public (公開中).

    This is used after a successful (re)deploy that started the service.
    """
    db_path = str(db_path)
    init_db(db_path)

    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE requests SET status = 'public', port = COALESCE(?, port) WHERE id = ?",
            (port, request_id),
        )

def mark_stopped(db_path: str | Path, request_id: int) -> None:
    db_path = str(db_path)
    init_db(db_path)

    with _connect(db_path) as conn:
        conn.execute("UPDATE requests SET status = 'stopped' WHERE id = ?", (request_id,))


def update_last_upload(db_path: str | Path, request_id: int, filename: str) -> None:
    db_path = str(db_path)
    init_db(db_path)

    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE requests SET last_upload = ?, last_upload_at = ? WHERE id = ?",
            (filename, _now(), request_id),
        )


def delete_request(db_path: str | Path, request_id: int) -> None:
    db_path = str(db_path)
    init_db(db_path)

    with _connect(db_path) as conn:
        conn.execute("DELETE FROM requests WHERE id = ?", (request_id,))


def update_app_desc(db_path: str | Path, request_id: int, *, student_id: str, app_desc: str) -> bool:
    """本人のアプリ概要(app_desc)だけ更新できる。成功なら True."""
    db_path = str(db_path)
    init_db(db_path)

    # 長さ暴走を軽く抑止（任意）
    app_desc = (app_desc or "").strip()
    if len(app_desc) > 2000:
        app_desc = app_desc[:2000]

    with _connect(db_path) as conn:
        cur = conn.execute(
            "UPDATE requests SET app_desc = ? WHERE id = ? AND student_id = ?",
            (app_desc, request_id, student_id),
        )
        return cur.rowcount == 1


# --- ここから追記（requests_db.py の末尾など） ---

def list_all(db_path: str | Path) -> list[RequestRow]:
    db_path = str(db_path)
    init_db(db_path)
    with _connect(db_path) as conn:
        rows = conn.execute("SELECT * FROM requests ORDER BY id DESC").fetchall()
        return [_row_to_obj(r) for r in rows]


def update_request_all_fields(
    db_path: str | Path,
    request_id: int,
    *,
    student_id: str,
    app_name: str,
    request_kind: str,
    status: str,
    created_at: str,
    app_desc: str | None = None,
    python_version: str | None = None,
    java_release: str | None = None,
    java_build: str | None = None,
    port: int | None = None,
    last_upload: str | None = None,
    last_upload_at: str | None = None,
) -> bool:
    """講師/管理用途: requests の全項目を更新する（ログイン無し運用を想定してもOK）"""
    db_path = str(db_path)
    init_db(db_path)

    with _connect(db_path) as conn:
        cur = conn.execute(
            """
            UPDATE requests SET
                student_id = ?,
                app_name = ?,
                request_kind = ?,
                status = ?,
                created_at = ?,
                app_desc = ?,
                python_version = ?,
                java_release = ?,
                java_build = ?,
                port = ?,
                last_upload = ?,
                last_upload_at = ?
            WHERE id = ?
            """,
            (
                student_id, app_name, request_kind, status, created_at,
                app_desc, python_version, java_release, java_build,
                port, last_upload, last_upload_at,
                request_id,
            ),
        )
        return cur.rowcount == 1


# --- teachers (講師アカウント) ---

def _ensure_teachers_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS teachers (
            teacher_id TEXT PRIMARY KEY,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )


def _hash_password(password: str) -> str:
    # werkzeug を優先（Flask 同梱）
    try:
        from werkzeug.security import generate_password_hash
        return generate_password_hash(password)
    except Exception:
        import hashlib
        import os as _os
        salt = _os.urandom(16)
        h = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200_000)
        return "pbkdf2$" + salt.hex() + "$" + h.hex()


def _check_password(password: str, password_hash: str) -> bool:
    try:
        from werkzeug.security import check_password_hash
        return check_password_hash(password_hash, password)
    except Exception:
        if not password_hash.startswith("pbkdf2$"):
            return False
        import hashlib
        _, salt_hex, h_hex = password_hash.split("$", 2)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(h_hex)
        got = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200_000)
        return got == expected


def teachers_count(db_path: str | Path) -> int:
    db_path = str(db_path)
    init_db(db_path)
    with _connect(db_path) as conn:
        _ensure_teachers_table(conn)
        r = conn.execute("SELECT COUNT(*) AS c FROM teachers").fetchone()
        return int(r["c"]) if r else 0


def upsert_teacher(db_path: str | Path, *, teacher_id: str, password: str) -> None:
    teacher_id = (teacher_id or "").strip()
    if not teacher_id:
        raise ValueError("teacher_id is required")
    if not password:
        raise ValueError("password is required")
    db_path = str(db_path)
    init_db(db_path)
    with _connect(db_path) as conn:
        _ensure_teachers_table(conn)
        conn.execute(
            """
            INSERT INTO teachers(teacher_id, password_hash, created_at)
            VALUES(?, ?, ?)
            ON CONFLICT(teacher_id) DO UPDATE SET
                password_hash=excluded.password_hash
            """
            ,
            (teacher_id, _hash_password(password), _now()),
        )


def delete_teacher(db_path: str | Path, *, teacher_id: str) -> None:
    teacher_id = (teacher_id or "").strip()
    db_path = str(db_path)
    init_db(db_path)
    with _connect(db_path) as conn:
        _ensure_teachers_table(conn)
        conn.execute("DELETE FROM teachers WHERE teacher_id = ?", (teacher_id,))


def list_teachers(db_path: str | Path) -> list[dict[str, str]]:
    db_path = str(db_path)
    init_db(db_path)
    with _connect(db_path) as conn:
        _ensure_teachers_table(conn)
        rows = conn.execute("SELECT teacher_id, created_at FROM teachers ORDER BY teacher_id").fetchall()
        return [{"teacher_id": str(r["teacher_id"]), "created_at": str(r["created_at"])} for r in rows]


def verify_teacher(db_path: str | Path, *, teacher_id: str, password: str) -> bool:
    teacher_id = (teacher_id or "").strip()
    db_path = str(db_path)
    init_db(db_path)
    with _connect(db_path) as conn:
        _ensure_teachers_table(conn)
        r = conn.execute("SELECT password_hash FROM teachers WHERE teacher_id = ?", (teacher_id,)).fetchone()
        if not r:
            return False
        return _check_password(password, str(r["password_hash"]))


# ============================================================
# LINE notify global settings (admin managed)
# ============================================================

def _ensure_line_notify_settings_table(conn: sqlite3.Connection) -> None:
    """
    グローバルな LINE 通知設定テーブル。
    1行(id=1)のみを使う。
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS line_notify_settings (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            on_new       INTEGER NOT NULL DEFAULT 0,
            on_publish   INTEGER NOT NULL DEFAULT 1,
            on_stop      INTEGER NOT NULL DEFAULT 0,
            on_republish INTEGER NOT NULL DEFAULT 1,
            updated_at   TEXT NOT NULL
        )
        """
    )

    # 初期行を必ず1件用意（既存なら無視）
    conn.execute(
        """
        INSERT OR IGNORE INTO line_notify_settings
          (id, on_new, on_publish, on_stop, on_republish, updated_at)
        VALUES
          (1, 0, 1, 0, 1, ?)
        """,
        (_now(),),
    )


def get_line_notify_settings(db_path: str | Path) -> dict[str, int]:
    """
    LINE通知設定を取得。
    戻り値は {on_new, on_publish, on_stop, on_republish}
    """
    db_path = str(db_path)
    init_db(db_path)

    with _connect(db_path) as conn:
        _ensure_line_notify_settings_table(conn)
        r = conn.execute(
            """
            SELECT on_new, on_publish, on_stop, on_republish
            FROM line_notify_settings
            WHERE id = 1
            """
        ).fetchone()

        if not r:
            # 念のための保険（通常ここには来ない）
            return {
                "on_new": 0,
                "on_publish": 1,
                "on_stop": 0,
                "on_republish": 1,
            }

        return {
            "on_new": int(r["on_new"]),
            "on_publish": int(r["on_publish"]),
            "on_stop": int(r["on_stop"]),
            "on_republish": int(r["on_republish"]),
        }


def update_line_notify_settings(
    db_path: str | Path,
    *,
    on_new: int,
    on_publish: int,
    on_stop: int,
    on_republish: int,
) -> None:
    """
    LINE通知設定を更新（admin 画面用）
    """
    db_path = str(db_path)
    init_db(db_path)

    with _connect(db_path) as conn:
        _ensure_line_notify_settings_table(conn)
        conn.execute(
            """
            UPDATE line_notify_settings
               SET on_new       = ?,
                   on_publish   = ?,
                   on_stop      = ?,
                   on_republish = ?,
                   updated_at   = ?
             WHERE id = 1
            """,
            (on_new, on_publish, on_stop, on_republish, _now()),
        )
