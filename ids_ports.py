import os
import re
import sqlite3
from typing import Literal

import config

# students_deployer.py から使うアプリ種別
AppType = Literal["static", "flask", "streamlit", "spring"]

# === 運用ポリシー ===
# STRICT_YYMM=1 → MM=01..12, NN=01..99 を強制（従来互換）
# STRICT_YYMM=0 → 学習用途（YYMMNNは識別子として扱う）
STRICT_YYMM = os.environ.get("STRICT_YYMM", "0").strip() == "1"


def _parse_student_id(student_id: str) -> tuple[int, int, int]:
    """
    受講者ID (YYMMNN) を分解して (year, mm, nn) を返す。
    学習用途では MM/NN の範囲チェックは行わない。
    """
    if not re.fullmatch(r"\d{6}", student_id):
        raise ValueError("受講者IDは 6桁の数字で入力してください。")

    year = int(student_id[:2])      # YY
    mm = int(student_id[2:4])       # MM（識別子）
    nn = int(student_id[4:6])       # NN（識別子）

    if STRICT_YYMM:
        if not 1 <= mm <= 12:
            raise ValueError("受講者IDの MM 部分が 01〜12 の範囲ではありません。")
        if not 1 <= nn <= 99:
            raise ValueError("受講者IDの NN 部分は 01〜99 の範囲で入力してください。")

    return year, mm, nn


def student_id_to_term(student_id: str) -> str:
    """
    受講者ID (YYMMNN) から term を生成する。
    - term はあくまで識別子（YYYY-MM）
    """
    year, mm, _ = _parse_student_id(student_id)
    full_year = 2000 + year
    return f"{full_year:04d}-{mm:02d}"


# ==== ここからポート連番管理 ====

PORT_BASE: int = getattr(config, "PORT_BASE", 10000)
PORT_MAX: int = getattr(config, "PORT_MAX", 64999)


def _db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(config.REQUESTS_DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def _db_max_used_port(conn: sqlite3.Connection) -> int:
    try:
        r = conn.execute("SELECT MAX(port) AS m FROM requests WHERE port IS NOT NULL").fetchone()
        m = r["m"] if r else None
        return int(m) if m is not None else (PORT_BASE - 1)
    except sqlite3.Error:
        return PORT_BASE - 1


def _ensure_port_counter(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS port_counter (
            id INTEGER PRIMARY KEY,
            next_port INTEGER NOT NULL
        )
        """
    )

    row = conn.execute("SELECT next_port FROM port_counter WHERE id = 1").fetchone()
    if row is not None:
        return

    start = max(PORT_BASE, _db_max_used_port(conn) + 1)
    conn.execute("INSERT INTO port_counter (id, next_port) VALUES (1, ?)", (start,))


def _alloc_port_from_db() -> int:
    with _db_connect() as conn:
        conn.execute("BEGIN IMMEDIATE")

        _ensure_port_counter(conn)

        used = set()
        for r in conn.execute("SELECT port FROM requests WHERE port IS NOT NULL").fetchall():
            try:
                used.add(int(r["port"]))
            except Exception:
                pass

        next_row = conn.execute("SELECT next_port FROM port_counter WHERE id = 1").fetchone()
        port = int(next_row["next_port"]) if next_row else PORT_BASE

        while port in used:
            port += 1

        if port > PORT_MAX:
            raise ValueError(f"自動割り当てできるポートが枯渇しています: {port}")

        conn.execute("UPDATE port_counter SET next_port = ? WHERE id = 1", (port + 1,))
        conn.commit()
        return port


def auto_port(apptype: AppType, student_id: str) -> int:
    if apptype == "static":
        raise ValueError("static アプリにポートは不要です。auto_port は呼び出さないでください。")

    if apptype not in {"flask", "streamlit", "spring"}:
        raise ValueError(f"不正なアプリ種別です: {apptype}")

    # student_id 妥当性チェック（STRICT/LOOSE は _parse_student_id に従う）
    _ = student_id_to_term(student_id)

    return _alloc_port_from_db()
