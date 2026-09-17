# teacher_app.py
# 修正点:
# - auto_allocate_port を使っていた箇所を auto_port に修正
# - 初回デプロイ成功時に DB に port を保存（mark_done(port=port)）
# - teacher_requests.html へ render_template で表示（埋め込みHTMLは使わない）
# - 状態表示（日本語）: received/ done/ public/ stopped
# - 一覧に runtime / port / version / last_deploy_at を付与

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from functools import wraps
from typing import Any

from flask import Flask, request, render_template, redirect, url_for, session, abort, flash, send_file

import re
import subprocess

import requests

import config
import requests_db
import ids_ports
import nginx_utils
import flask_runtime
import spring_runtime
import common_paths

import subprocess
from pathlib import Path
import glob


def _norm_prefix(p: str, default: str) -> str:
    p = (p or default).strip()
    if not p:
        p = default
    if not p.startswith("/"):
        p = "/" + p
    return p.rstrip("/")

URL_PREFIX = _norm_prefix(getattr(config, "TEACHER_URL_PREFIX", "/teacher"), "/teacher")


def _systemd_purge(service_name: str) -> None:
    """
    unit本体 + drop-in（override.conf 等）を完全削除。
    drop-in が残ると ExecStart/port が古いまま残り、502 の原因になる。
    """
    unit_path = Path(config.SYSTEMD_DIR) / f"{service_name}.service"
    dropin_dir = Path(config.SYSTEMD_DIR) / f"{service_name}.service.d"

    subprocess.run(["sudo", "-n", "systemctl", "stop", f"{service_name}.service"], check=False)
    subprocess.run(["sudo", "-n", "systemctl", "disable", f"{service_name}.service"], check=False)
    subprocess.run(["sudo", "-n", "systemctl", "reset-failed", f"{service_name}.service"], check=False)

    subprocess.run(["sudo", "-n", "rm", "-f", str(unit_path)], check=False)
    subprocess.run(["sudo", "-n", "rm", "-rf", str(dropin_dir)], check=False)

    subprocess.run(["sudo", "-n", "systemctl", "daemon-reload"], check=False)


def _nginx_reload_strict() -> None:
    subprocess.run(["sudo", "-n", "nginx", "-t"], check=True)
    subprocess.run(["sudo", "-n", "systemctl", "reload", "nginx"], check=True)


def _nginx_purge_student_conf(term: str, student_id: str, app_name: str) -> None:
    """
    /etc/nginx/students.d/<term>_<sid>_<app>.conf を削除（揺れがあっても消す）。
    """
    pattern = f"/etc/nginx/students.d/{term}_{student_id}_{app_name}*.conf"
    for p in glob.glob(pattern):
        subprocess.run(["sudo", "-n", "rm", "-f", p], check=False)

# --- static nginx location helper (teacher side only) ---
# students_redeploy は static を /srv/students/{term}/{student}/{app}/public に配置するため、
# 講師側の初回デプロイも同じパスで枠を作る（BASE_DIR_STATIC には依存しない）。
def _ensure_static_nginx_location(term: str, student_id: str, app_name: str, flash_func=None) -> bool:
    location_prefix = f"/s/{term}/{student_id}/{app_name}/"
    public_dir = common_paths.public_dir(term, student_id, app_name)
    public_dir.mkdir(parents=True, exist_ok=True)

    conf_dir = config.NGINX_SNIPPET_DIR
    conf_dir.mkdir(parents=True, exist_ok=True)
    conf_path = conf_dir / f"{term}_{student_id}_{app_name}.conf"

    conf = f"""# {term} / {student_id} / {app_name} (static)
location {location_prefix} {{
    alias {public_dir.as_posix()}/;
    index index.html;
    autoindex off;
}}
"""

    # atomic write
    tmp = conf_path.with_suffix(conf_path.suffix + ".tmp")
    tmp.write_text(conf, encoding="utf-8")
    tmp.replace(conf_path)

    t = subprocess.run(["sudo", "-n", "nginx", "-t"], capture_output=True, text=True)
    if t.returncode != 0:
        # 壊れた conf を残すと危険なので削除して戻す
        try:
            conf_path.unlink(missing_ok=True)
        except Exception:
            pass
        if flash_func:
            flash_func(f"nginx -t に失敗: {(t.stderr or '').strip()}", "error")
        return False

    r = subprocess.run(["sudo", "-n", "systemctl", "reload", "nginx"], capture_output=True, text=True)
    if r.returncode != 0:
        if flash_func:
            flash_func(f"nginx reload に失敗: {(r.stderr or '').strip()}", "error")
        return False

    if flash_func:
        flash_func(f"static 公開枠を作成しました: {location_prefix}", "ok")
    return True

app = Flask(__name__)
app.secret_key = config.SECRET_KEY

REQUESTS_DB_PATH = config.REQUESTS_DB_PATH

requests_db.init_db(REQUESTS_DB_PATH)


def _status_jp(status: str) -> str:
    return {
        "received": "申請中",
        "done": "完了",
        "public": "公開中",
        "stopped": "停止",
    }.get(status, status)


def _term_of(student_id: str) -> str:
    """term は維持するが、YYMM(=月1..12)に拘らない運用を許容する。
    ids_ports 側が例外を投げても 500 にしない。"""
    try:
        return ids_ports.student_id_to_term(student_id)
    except Exception:
        # fallback: 先頭4桁をそのまま term にする（例: 269900 -> 2026-99）
        if re.fullmatch(r"\d{6}", student_id):
            yy = student_id[:2]
            mm = student_id[2:4]
            return f"20{yy}-{mm}"
        return "unknown"



def teacher_login_required(f):
    @wraps(f)
    def wrapper(*a, **kw):
        if session.get("teacher_id"):
            return f(*a, **kw)
        nxt = request.path
        return redirect(url_for("teacher_login_get", next=nxt))
    return wrapper



@app.get(f"{URL_PREFIX}/login")
def teacher_login_get():
    nxt = request.args.get("next", f"{URL_PREFIX}/")
    return render_template("teacher_login.html", next=nxt)


@app.post(f"{URL_PREFIX}/login")
def teacher_login_post():
    teacher_id = (request.form.get("teacher_id") or "").strip()
    password = (request.form.get("password") or "")
    nxt = request.form.get("next") or f"{URL_PREFIX}/"

    # まだ講師が1人も登録されていない場合は、ログインできないので明示
    if requests_db.teachers_count(REQUESTS_DB_PATH) == 0:
        msg = "講師アカウントが未登録です。requests.db の teachers テーブルに teacher_id/password を登録してください。"
        return render_template("teacher_login.html", next=nxt, msg=msg)

    ok = requests_db.verify_teacher(REQUESTS_DB_PATH, teacher_id=teacher_id, password=password)
    if ok:
        session["teacher_id"] = teacher_id
        return redirect(nxt)

    return render_template("teacher_login.html", next=nxt, msg="講師番号またはパスワードが違います")


@app.get(f"{URL_PREFIX}/logout")
def teacher_logout():
    session.clear()
    return redirect(f"{URL_PREFIX}/login")


def _get_service_state(service_name: str) -> str:
    r = subprocess.run(["sudo", "-n", "systemctl", "is-active", service_name],
                       capture_output=True, text=True)
    return (r.stdout or "").strip()


def _runtime_state(kind: str, term: str, student_id: str, app_name: str) -> str:
    kind = (kind or "").lower()
    if kind not in ("flask", "streamlit", "spring"):
        return "-"
    service_name = common_paths.service_name(kind, term, student_id, app_name)
    out = _get_service_state(service_name)
    if out == "active":
        return "active"
    if out in ("inactive", "failed", "deactivating", "activating"):
        return "stopped"
    return out or "unknown"


def _service_and_appdir(term: str, student_id: str, app_name: str, kind: str) -> tuple[str, str]:
    if kind == "flask":
        service_name = common_paths.service_name("flask", term, student_id, app_name)
        app_dir = f"{config.BASE_DIR_FLASK}/{term}/{student_id}/{app_name}"
        return service_name, app_dir
    if kind == "streamlit":
        service_name = common_paths.service_name("streamlit", term, student_id, app_name)
        app_dir = f"{config.BASE_DIR_FLASK}/{term}/{student_id}/{app_name}"
        return service_name, app_dir
    if kind == "spring":
        service_name = common_paths.service_name("spring", term, student_id, app_name)
        app_dir = f"{config.BASE_DIR_SPRING}/{term}/{student_id}/{app_name}"
        return service_name, app_dir
    raise ValueError("unsupported kind")


def _load_request_by_student_app(student_id: str, app_name: str):
    rows = requests_db.search(REQUESTS_DB_PATH, student_id=student_id, app_name=app_name, status="all")
    if not rows:
        return None
    return rows[0]


@app.get(f"{URL_PREFIX}/search")
@teacher_login_required
def teacher_search():
    status = (request.args.get("status") or "all").strip() or "all"
    student_id = (request.args.get("student_id") or "").strip()
    app_name   = (request.args.get("app_name") or "").strip()

    rows = requests_db.search(
        REQUESTS_DB_PATH,
        status=status,
        student_id=student_id,
        app_name=app_name,
    )
    # rows = rows[:300]

    view_rows = []
    for r in rows:
        term = _term_of(r.student_id)
        kind = (getattr(r, "request_kind", None) or "static").lower()

        runtime = _runtime_state(kind, term, r.student_id, r.app_name)

        version = "-"
        if kind in ("flask", "streamlit"):
            pv = getattr(r, "python_version", None) or "auto"
            version = f"Python {pv}"
        elif kind == "spring":
            jr = getattr(r, "java_release", None) or "auto"
            version = f"Java {jr}"

        last_deploy_at = getattr(r, "last_upload_at", None) or ""

        view_rows.append(
            dict(
                id=r.id,
                term=term,
                student_id=r.student_id,
                app_name=r.app_name,
                kind=kind,
                status=r.status,
                status_jp=_status_jp(r.status),
                runtime=runtime,
                port=getattr(r, "port", None),
                version=version,
                last_deploy_at=last_deploy_at,
                created_at=r.created_at,
            )
        )

    return render_template(
        "teacher_requests.html",
        rows=view_rows,
        status=status,
        student_id=student_id,
        app_name=app_name,
        config=config,
    )


@app.post(f"{URL_PREFIX}/action")
@teacher_login_required
def teacher_action():
    action = (request.form.get("action") or "").strip()
    student_id = (request.form.get("student_id") or "").strip()
    app_name = (request.form.get("app_name") or "").strip()
    apptype = (request.form.get("apptype") or "").strip().lower()
    return_to = request.form.get("return_to") or f"{URL_PREFIX}/search"

    row = _load_request_by_student_app(student_id, app_name)
    if row is None:
        flash("対象の申請が見つかりません", "error")
        return redirect(return_to)

    term = _term_of(student_id)

    if action == "deploy":
        # 初回デプロイは received のときだけ
        if row.status != "received":
            flash("初回デプロイは申請中（received）のみ可能です", "error")
            return redirect(return_to)

        # 種別は DB の request_kind を正とする（フォーム値の混入を防ぐ）
        apptype = (getattr(row, "request_kind", None) or apptype or "static").strip().lower()

        try:
            port = None

            if apptype in ("flask", "streamlit", "spring"):
                # ポート確保（欠番運用）
                port = ids_ports.auto_port(apptype, student_id)

            if apptype == "flask":
                flask_runtime.setup_flask_runtime(
                    term=term,
                    student_id=student_id,
                    app_name=app_name,
                    port=port,
                    python_version=(getattr(row, "python_version", None) or config.DEFAULT_PYTHON_VERSION),
                )

                nginx_utils.write_student_location("flask", term, student_id, app_name, port=port, flash_func=flash)
                if not nginx_utils.reload_nginx_with_check(flash):
                    raise RuntimeError("nginx reload failed")

            elif apptype == "streamlit":
                flask_runtime.setup_flask_runtime(
                    term=term,
                    student_id=student_id,
                    app_name=app_name,
                    port=port,
                    python_version=(getattr(row, "python_version", None) or config.DEFAULT_PYTHON_VERSION),
                )

                nginx_utils.write_student_location("flask", term, student_id, app_name, port=port, is_streamlit=True, flash_func=flash)
                if not nginx_utils.reload_nginx_with_check(flash):
                    raise RuntimeError("nginx reload failed")

            elif apptype == "spring":
                spring_runtime.first_deploy_spring(
                    term=term,
                    student_id=student_id,
                    app_name=app_name,
                    port=port,
                    jar_name="app.jar",
                )

                nginx_utils.write_student_location("spring", term, student_id, app_name, port=port, flash_func=flash)
                if not nginx_utils.reload_nginx_with_check(flash):
                    raise RuntimeError("nginx reload failed")

            elif apptype == "static":
                # static は初回枠作成のみ（ポート不要 / systemd不要）
                if not _ensure_static_nginx_location(term, student_id, app_name, flash_func=flash):
                    raise RuntimeError("static nginx location create failed")

            else:
                flash(f"未知の種別: {apptype}", "error")
                return redirect(return_to)

            # done: 公開枠が確保できた（ファイル未アップロードでもOK）
            requests_db.mark_done(REQUESTS_DB_PATH, row.id, port=port)
            flash("初回デプロイが完了しました", "ok")

        except Exception as e:
            flash(f"初回デプロイに失敗: {e}", "error")

        return redirect(return_to)

    if action == "stop":
        # 停止は public のみ
        apptype = (getattr(row, "request_kind", None) or apptype or "static").strip().lower()
        if row.status != "public":
            flash("停止は公開中（public）のみ可能です", "error")
            return redirect(return_to)
    
        try:
            if apptype in ("flask", "streamlit", "spring"):
                service_name = common_paths.service_name(apptype, term, student_id, app_name)
                subprocess.run(["sudo", "-n", "systemctl", "stop", service_name], check=False)
            requests_db.mark_stopped(REQUESTS_DB_PATH, row.id)
            flash("停止しました", "ok")
        except Exception as e:
            flash(f"停止に失敗: {e}", "error")
    
        return redirect(return_to)
    
    if action == "purge":
        # 物理削除（講師のみ）
        # ===== purge（統一版・完成ブロック） =====
        apptype = (getattr(row, "request_kind", None) or apptype or "static").strip().lower()
        purge_key = (request.form.get("purge_key") or "").strip()
        if purge_key != getattr(config, "PURGE_KEY", ""):
            flash("Purge Key が違います", "error")
            return redirect(return_to)

        try:
            # 0) app_dir を種別で確定（staticでも落ちない）
            # app_dir を common_paths に頼らず、config から確定する
            if apptype == "spring":
                service_name = common_paths.service_name("spring", term, student_id, app_name)
                app_dir = f"{config.BASE_DIR_SPRING}/{term}/{student_id}/{app_name}"
                _systemd_purge(service_name)

            elif apptype == "streamlit":
                service_name = common_paths.service_name("streamlit", term, student_id, app_name)
                app_dir = f"{config.BASE_DIR_FLASK}/{term}/{student_id}/{app_name}"
                _systemd_purge(service_name)

            elif apptype == "flask":
                service_name = common_paths.service_name("flask", term, student_id, app_name)
                app_dir = f"{config.BASE_DIR_FLASK}/{term}/{student_id}/{app_name}"
                _systemd_purge(service_name)

            elif apptype == "static":
                service_name = ""
                app_dir = f"{config.BASE_DIR_STATIC}/{term}/{student_id}/{app_name}"

            else:
                raise ValueError(f"unsupported apptype: {apptype}")

            # 1) nginx conf は全種別で消す
            _nginx_purge_student_conf(term, student_id, app_name)

            # 2) 実体ディレクトリ削除（全種別）
            subprocess.run(["sudo", "-n", "rm", "-rf", app_dir], check=False)

            # 3) nginx reload（テストしてから）
            _nginx_reload_strict()

            # 4) DB（requests）から削除/更新はあなたの既存仕様に合わせる
            # 例：完全削除
            requests_db.delete_request(REQUESTS_DB_PATH, row.id)

            flash("削除しました（purge 完了）", "ok")

        except Exception as e:
            flash(f"削除に失敗: {e}", "error")

        return redirect(return_to)
        # ===== purge（統一版・完成ブロック）ここまで =====
    
    flash("不明な操作です", "error")
    return redirect(return_to)
    
    
@app.get("/")
def index():
    return redirect(f"{URL_PREFIX}/")


@app.get(f"{URL_PREFIX}/")
@teacher_login_required
def teacher_menu():
    return render_template("teacher_menu.html")


@app.get(f"{URL_PREFIX}/accounts_manage")
@teacher_login_required
def teacher_accounts_manage():
    return render_template("teacher_accounts_manage.html")


@app.get(f"{URL_PREFIX}/download")
@teacher_login_required
def teacher_download():
    """デプロイ済みapp_dirを再zip化してダウンロード"""
    import io, zipfile, tempfile, os

    student_id = (request.args.get("student_id") or "").strip()
    app_name   = (request.args.get("app_name")   or "").strip()

    row = _load_request_by_student_app(student_id, app_name)
    if row is None:
        abort(404)

    kind = (getattr(row, "request_kind", None) or "static").strip().lower()
    if kind not in ("flask", "streamlit", "spring", "static"):
        abort(400)

    # app_dir を確定
    if kind == "spring":
        app_dir = Path(f"{config.BASE_DIR_SPRING}/{_term_of(student_id)}/{student_id}/{app_name}")
    elif kind == "static":
        app_dir = Path(f"{config.BASE_DIR_STATIC}/{_term_of(student_id)}/{student_id}/{app_name}")
    else:  # flask / streamlit
        app_dir = Path(f"{config.BASE_DIR_FLASK}/{_term_of(student_id)}/{student_id}/{app_name}")

    if not app_dir.exists():
        abort(404)

    # メモリ上でzip化
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for file_path in sorted(app_dir.rglob("*")):
            if file_path.is_file():
                zf.write(file_path, file_path.relative_to(app_dir))
    buf.seek(0)

    download_name = f"{student_id}_{app_name}.zip"
    return send_file(
        buf,
        mimetype="application/zip",
        as_attachment=True,
        download_name=download_name,
    )


# ---------------------------------------------------------------------------
# アカウント一覧 xlsx アップロード／ダウンロード
# ---------------------------------------------------------------------------

ACCOUNTS_DIR  = config.UPLOADS_DIR / "accounts"
ACCOUNTS_FILE = ACCOUNTS_DIR / "アカウント一覧.xlsx"


@app.post(f"{URL_PREFIX}/upload_accounts")
@teacher_login_required
def teacher_upload_accounts():
    """講師がアカウント一覧 xlsx をアップロードする。"""
    return_to = request.form.get("return_to") or f"{URL_PREFIX}/"
    f = request.files.get("accounts_file")

    if not f or not f.filename:
        flash("ファイルが選択されていません", "error")
        return redirect(return_to)

    if not f.filename.lower().endswith(".xlsx"):
        flash(".xlsx ファイルのみアップロードできます", "error")
        return redirect(return_to)

    ACCOUNTS_DIR.mkdir(parents=True, exist_ok=True)
    f.save(str(ACCOUNTS_FILE))
    flash("アカウント一覧を更新しました", "ok")
    return redirect(return_to)


@app.get(f"{URL_PREFIX}/accounts/")
@app.get(f"{URL_PREFIX}/accounts")
def accounts_view():
    """認証不要でアカウント一覧を HTML テーブルとして閲覧させる（DL不可、コピペ可）。"""
    import openpyxl, html as _html

    if not ACCOUNTS_FILE.exists():
        return (
            "<html><body style='font-family:sans-serif;padding:40px'>"
            "<h2>アカウント一覧はまだアップロードされていません。</h2></body></html>",
            404,
        )

    wb = openpyxl.load_workbook(ACCOUNTS_FILE, data_only=True)
    ws = wb.active

    # --- ヘッダー行を探す（「PC番号」が入っている行）---
    header_row = None
    pc_col_idx = None   # 「PC番号」列のインデックス
    data_rows = []
    for row in ws.iter_rows(values_only=True):
        if header_row is None:
            for i, c in enumerate(row):
                if str(c or "").strip() == "PC番号":
                    header_row = row
                    pc_col_idx = i
                    break
        else:
            # PC番号列がブランクの行は無視する
            if pc_col_idx is not None:
                pc_val = row[pc_col_idx] if pc_col_idx < len(row) else None
                if pc_val is None or str(pc_val).strip() == "":
                    continue
            if any(c is not None for c in row):
                data_rows.append(row)

    if header_row is None:
        return (
            "<html><body style='font-family:sans-serif;padding:40px'>"
            "<h2>ヘッダー行が見つかりません。</h2></body></html>",
            500,
        )

    def cell(v):
        return _html.escape(str(v)) if v is not None else ""

    thead = "".join(f"<th>{cell(c)}</th>" for c in header_row if c is not None)
    col_indices = [i for i, c in enumerate(header_row) if c is not None]

    menu_url = url_for("teacher_menu")

    tbody_rows = []
    for row in data_rows:
        tds = "".join(f"<td>{cell(row[i] if i < len(row) else None)}</td>" for i in col_indices)
        tbody_rows.append(f"<tr>{tds}</tr>")
    tbody = "\n".join(tbody_rows)

    page = f"""<!doctype html>
<html lang="ja">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>アカウント一覧</title>
  <style>
    body {{
      font-family: 'Noto Sans JP', 'Hiragino Kaku Gothic ProN', sans-serif;
      background: #f0f4f8; margin: 0; padding: 24px;
    }}
    h1 {{ font-size: 18px; margin: 0 0 6px; color: #0f172a; }}
    .note {{
      font-size: 12px; color: #64748b; margin-bottom: 18px;
    }}
    .wrap {{
      background: #fff; border-radius: 14px;
      border: 1px solid #dde3ec;
      box-shadow: 0 2px 12px rgba(15,23,42,.07);
      overflow-x: auto; padding: 4px;
    }}
    table {{
      border-collapse: collapse; width: 100%;
      font-size: 13px;
    }}
    th {{
      background: #1d4ed8; color: #fff;
      padding: 10px 12px; text-align: left;
      white-space: nowrap; position: sticky; top: 0;
    }}
    td {{
      padding: 9px 12px; border-bottom: 1px solid #e2e8f0;
      white-space: nowrap;
    }}
    tr:last-child td {{ border-bottom: none; }}
    tr:nth-child(even) td {{ background: #f8fafc; }}
    tr:hover td {{ background: #eff6ff; }}
    .nav {{
      display: flex; align-items: center; gap: 12px; margin-bottom: 14px;
    }}
    .btn-back {{
      padding: 7px 14px; background: #fff; border: 1px solid #cbd5e1;
      border-radius: 10px; color: #334155; text-decoration: none;
      font-size: 13px; font-weight: 700;
    }}
    .btn-back:hover {{ background: #f1f5f9; }}
  </style>
</head>
<body>
  <div class="nav">
    <a class="btn-back" href="{menu_url}">← 講師メニュー</a>
    <h1 style="margin:0">📊 アカウント一覧</h1>
  </div>
  <p class="note">自分のアカウント情報を確認してください。ダウンロードはできません。</p>
  <div class="wrap">
    <table>
      <thead><tr>{thead}</tr></thead>
      <tbody>{tbody}</tbody>
    </table>
  </div>
</body>
</html>"""
    from flask import Response
    return Response(page, mimetype="text/html",
                    headers={"Content-Disposition": "inline",
                             "X-Content-Type-Options": "nosniff"})




# ---------------------------------------------------------------------------
# 環境リセット機能
# ---------------------------------------------------------------------------
# config.py に以下を追加して使用:
#
#   RESET_KEY              = os.environ.get("RESET_KEY", "")
#
#   GITHUB_PRACTICE_USERS  = [f"user{i:02d}-practice" for i in range(1, 16)]  # user01-practice〜user15-practice
#   GITHUB_PRACTICE_PATS   = {username: os.environ.get(f"GITHUB_PAT_{...}", "") ...}  # 各アカウント自身のPAT（delete_repoスコープ）
#
#   MAIL_SSH_HOST       = os.environ.get("MAIL_SSH_HOST", "telcation.com")
#   MAIL_SSH_USER       = os.environ.get("MAIL_SSH_USER", "ubuntu")
#   MAIL_MAILDIR_BASE   = os.environ.get("MAIL_MAILDIR_BASE", "/home")
#   MAIL_USERS_FILE     = os.environ.get("MAIL_USERS_FILE", "")  # 空なら /etc/passwd から取得
# ---------------------------------------------------------------------------


def _ssh_run(host: str, user: str, cmd: str) -> tuple[int, str, str]:
    """SSH でリモートコマンドを実行し (returncode, stdout, stderr) を返す。"""
    result = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
         f"{user}@{host}", cmd],
        capture_output=True, text=True
    )
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def _get_reset_key() -> str:
    return (getattr(config, "RESET_KEY", "") or "").strip()


# ---- 練習用 GitHub アカウント初期化 ----

_GITHUB_API = "https://api.github.com"


def _github_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _github_list_own_repos(token: str) -> list[str]:
    """トークンの持ち主自身の全リポジトリ full_name を返す（private含む・ページネーション対応）。"""
    repos: list[str] = []
    page = 1
    while True:
        resp = requests.get(
            f"{_GITHUB_API}/user/repos",
            headers=_github_headers(token),
            params={"per_page": 100, "page": page, "affiliation": "owner"},
            timeout=15,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"リポジトリ一覧取得失敗（{resp.status_code}）: {resp.text[:200]}")
        batch = resp.json()
        if not batch:
            break
        repos.extend(r["full_name"] for r in batch)
        if len(batch) < 100:
            break
        page += 1
        if page > 20:  # 安全弁（想定外の無限ループ防止）
            break
    return repos


def _github_delete_repo(full_name: str, token: str) -> None:
    resp = requests.delete(
        f"{_GITHUB_API}/repos/{full_name}",
        headers=_github_headers(token),
        timeout=15,
    )
    if resp.status_code not in (204, 404):
        raise RuntimeError(f"削除失敗（{resp.status_code}）: {resp.text[:200]}")


@app.get(f"{URL_PREFIX}/reset_github_practice")
@teacher_login_required
def reset_github_practice_get():
    users = getattr(config, "GITHUB_PRACTICE_USERS", []) or []
    pats = getattr(config, "GITHUB_PRACTICE_PATS", {}) or {}

    accounts = []
    for username in users:
        token = (pats.get(username) or "").strip()
        if not token:
            accounts.append({
                "username": username,
                "repos": [],
                "error": "PAT未設定",
            })
            continue
        try:
            repo_full_names = _github_list_own_repos(token)
            repo_names = [f.split("/", 1)[-1] for f in repo_full_names]
            accounts.append({"username": username, "repos": repo_names, "error": None})
        except Exception as e:
            accounts.append({"username": username, "repos": [], "error": f"取得失敗: {e}"})

    return render_template("teacher_reset_github_practice.html", accounts=accounts)


@app.post(f"{URL_PREFIX}/reset_github_practice")
@teacher_login_required
def reset_github_practice_post():
    reset_key = (request.form.get("reset_key") or "").strip()
    if not _get_reset_key() or reset_key != _get_reset_key():
        flash("Reset Key が違います", "error")
        return redirect(f"{URL_PREFIX}/reset_github_practice")

    pats = getattr(config, "GITHUB_PRACTICE_PATS", {}) or {}
    all_users = set(getattr(config, "GITHUB_PRACTICE_USERS", []) or [])

    selected = request.form.getlist("accounts")
    # 既知のアカウント一覧との積集合のみを対象にする（フォーム改ざん対策）
    users = [u for u in selected if u in all_users]

    if not users:
        flash("対象アカウントを1つ以上選択してください", "error")
        return redirect(f"{URL_PREFIX}/reset_github_practice")

    logs = []
    errors = []

    for username in users:
        token = (pats.get(username) or "").strip()
        if not token:
            errors.append(f"{username}: PAT が未設定です（GITHUB_PAT_{username.upper().replace('-', '_')}）")
            continue

        try:
            repo_full_names = _github_list_own_repos(token)
        except Exception as e:
            errors.append(f"{username}: リポジトリ一覧取得に失敗 - {e}")
            continue

        if not repo_full_names:
            logs.append(f"{username}（対象リポジトリなし）")
            continue

        deleted = []
        for full_name in repo_full_names:
            try:
                _github_delete_repo(full_name, token)
                deleted.append(full_name.split("/", 1)[-1])
            except Exception as e:
                errors.append(f"{full_name}: 削除失敗 - {e}")

        if deleted:
            logs.append(f"{username}（{len(deleted)}件: {', '.join(deleted)}）")

    if errors:
        flash("一部エラー:\n" + "\n".join(errors), "error")
    if logs:
        flash("GitHub 練習用アカウント初期化完了:\n" + "\n".join(logs), "ok")

    return redirect(f"{URL_PREFIX}/reset_github_practice")


# ---- メールボックス リセット ----

def _detect_mail_users() -> tuple[str, str, str, list[str]]:
    """(host, ssh_user, maildir_base, mail_users) を返す。host が空なら未設定。"""
    host         = getattr(config, "MAIL_SSH_HOST",      "")
    user         = getattr(config, "MAIL_SSH_USER",      "ubuntu")
    maildir_base = getattr(config, "MAIL_MAILDIR_BASE",  "/home")
    users_file   = getattr(config, "MAIL_USERS_FILE",    "")
    # 除外ユーザー（カンマ区切り）。"user" のようなシステムユーザーを誤削除しないために使う
    exclude_raw  = getattr(config, "MAIL_EXCLUDE_USERS", "user,ubuntu,root")
    exclude_set  = {u.strip() for u in exclude_raw.split(",") if u.strip()}

    if not host:
        return host, user, maildir_base, []

    if users_file:
        # ファイルから取得（1行1ユーザー）
        rc, out, err = _ssh_run(host, user, f"cat {users_file}")
        if rc != 0:
            raise RuntimeError(f"ユーザーファイル読み込み失敗: {err}")
        mail_users = [u.strip() for u in out.splitlines()
                      if u.strip() and u.strip() not in exclude_set]
    else:
        # Maildir が存在するホームディレクトリのユーザーを自動検出
        rc, out, err = _ssh_run(host, user,
            f"sudo find {maildir_base} -maxdepth 2 -name 'Maildir' -type d 2>/dev/null")
        if rc != 0:
            raise RuntimeError(f"Maildir 検索失敗: {err}")
        mail_users = []
        base_depth = len(maildir_base.rstrip("/").split("/"))
        for path in out.splitlines():
            parts = path.strip().split("/")
            if len(parts) > base_depth:
                detected = parts[base_depth]
                if detected and detected not in exclude_set:
                    mail_users.append(detected)

    # user01〜user15 の許可リストに含まれるものだけに絞り込む
    allowed = set(getattr(config, "MAIL_PRACTICE_USERS", []) or [])
    mail_users = [u for u in mail_users if u in allowed]

    return host, user, maildir_base, sorted(mail_users)


@app.get(f"{URL_PREFIX}/reset_mail")
@teacher_login_required
def reset_mail_get():
    try:
        host, _, _, mail_users = _detect_mail_users()
    except Exception as e:
        flash(f"メールユーザー一覧の取得に失敗: {e}", "error")
        host, mail_users = "", []

    if not host:
        flash("MAIL_SSH_HOST が設定されていません", "error")

    return render_template("teacher_reset_mail.html", mail_users=mail_users)


@app.post(f"{URL_PREFIX}/reset_mail")
@teacher_login_required
def reset_mail_post():
    reset_key = (request.form.get("reset_key") or "").strip()
    if not _get_reset_key() or reset_key != _get_reset_key():
        flash("Reset Key が違います", "error")
        return redirect(f"{URL_PREFIX}/reset_mail")

    selected = request.form.getlist("accounts")
    if not selected:
        flash("対象ユーザーを1つ以上選択してください", "error")
        return redirect(f"{URL_PREFIX}/reset_mail")

    try:
        host, ssh_user, maildir_base, detected_users = _detect_mail_users()
    except Exception as e:
        flash(f"メールユーザー一覧の取得に失敗: {e}", "error")
        return redirect(f"{URL_PREFIX}/reset_mail")

    if not host:
        flash("MAIL_SSH_HOST が設定されていません", "error")
        return redirect(f"{URL_PREFIX}/reset_mail")

    # 検出済みユーザーとの積集合のみを対象にする（フォーム改ざん対策）
    detected_set = set(detected_users)
    mail_users = [u for u in selected if u in detected_set]

    if not mail_users:
        flash("選択されたユーザーが対象一覧に見つかりませんでした", "error")
        return redirect(f"{URL_PREFIX}/reset_mail")

    logs = []
    errors = []

    for mail_user in mail_users:
        user_maildir = f"{maildir_base}/{mail_user}/Maildir"
        rc, out, err = _ssh_run(host, ssh_user,
            f"sudo find {user_maildir} -mindepth 1 -type f -delete 2>&1")
        if rc != 0:
            errors.append(f"{mail_user}: 削除失敗 - {err or out}")
            continue
        logs.append(mail_user)

    if errors:
        flash("一部エラー:\n" + "\n".join(errors), "error")
    if logs:
        flash(f"メールボックスクリア完了: {len(logs)} ユーザー（{', '.join(logs)}）", "ok")

    return redirect(f"{URL_PREFIX}/reset_mail")


if __name__ == "__main__":
    host = getattr(config, "TEACHER_HOST", "127.0.0.1")
    port = int(getattr(config, "PORT", 5000))
    app.run(host=host, port=port, debug=False)
