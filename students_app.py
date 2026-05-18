# students_app.py
# Students UI (redeploy): request / upload / restart for static, flask, spring
from __future__ import annotations

import json
import urllib.request
import urllib.error
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from functools import wraps
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

from flask import Flask, redirect, render_template, request, send_file, session, url_for

# --- local imports (same dir) ---
BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

import config  # type: ignore
import common_paths  # type: ignore
import ids_ports  # type: ignore
import requests_db  # type: ignore
import nginx_utils  # type: ignore
import flask_runtime  # type: ignore
import spring_runtime  # type: ignore

from students_ops import _get_service_state, _service_and_appdir, _install_requirements_if_present, _has_any_file
from zip_safety import _scan_zip, _zip_is_single_folder, _safe_extract_zip, _basic_file_safety_check
from deploy_report import Issue, _issue_from_text, _dedupe_issues, issues_to_items, _report_path, _delete_report_if_exists, _write_html_report
from line_notify import _notify_first_deploy_result


# ============================================================
# Settings
# ============================================================
REQUESTS_DB_PATH = Path(getattr(config, "REQUESTS_DB_PATH", BASE_DIR / "requests.db"))
REQUIRE_PIN = bool(getattr(config, "REQUIRE_PIN", False))
STUDENT_PIN = str(getattr(config, "STUDENT_PIN", "")).strip()
PUBLIC_BASE_URL = str(getattr(config, "PUBLIC_BASE_URL", "")).rstrip("/")
LAN_BASE_URL = str(getattr(config, "LAN_BASE_URL", "")).rstrip("/")
URL_PREFIX = str(getattr(config, "STUDENTS_URL_PREFIX", "/redeploy")).rstrip("/")

# Deploy-check defaults
DEPLOY_CHECK_DEFAULT = str(getattr(config, "DEPLOY_CHECK_DEFAULT", "off")).lower()  # "on" / "off"

# ============================================================
# Flask app
# ============================================================
app = Flask(__name__)
app.secret_key = str(getattr(config, "FLASK_SECRET", "students-redeploy-secret"))
app.url_map.strict_slashes = False  # avoid /login vs /login/ issues


# ============================================================
# Helpers
# ============================================================
def _decode_best_effort(b: bytes) -> str:
    try:
        return b.decode("utf-8", errors="replace")
    except Exception:
        return ""


def _line_info(text: str, pos: int) -> tuple[int, str]:
    line_no = text.count("\n", 0, pos) + 1
    line_start = text.rfind("\n", 0, pos) + 1
    line_end = text.find("\n", pos)
    if line_end == -1:
        line_end = len(text)
    return line_no, text[line_start:line_end]


def _status_jp(status: str) -> str:
    return {
        "received": "申請中",
        "done": "完了",
        "public": "公開中",
        "stopped": "停止",
        "error": "エラー",
    }.get(status, status)


def _safe_filename(name: str) -> str:
    name = os.path.basename(name or "").strip()
    name = name.replace("\\", "_").replace("/", "_")
    return name or "upload.bin"


def _term_of(student_id: str) -> str:
    """term は維持するが YYMM の MM を月として厳密チェックしない運用を許容（例: 269900 -> 2026-99）。"""
    try:
        return ids_ports.student_id_to_term(student_id)
    except Exception:
        s = (student_id or "").strip()
        if re.fullmatch(r"\d{6}", s):
            yy = s[:2]
            mm = s[2:4]
            return f"20{yy}-{mm}"
        return "unknown"


def _is_private_host(host: str) -> bool:
    host = (host or "").split(":")[0]
    return host.startswith(("192.168.", "10.", "172."))


def _public_url(term: str, student_id: str, app_name: str, request_kind: str, *, force_public: bool = False) -> str:
    base_prefix = {"static": "/s", "flask": "/f", "streamlit": "/f", "spring": "/b"}.get(request_kind, "")

    # 外部をデフォルト
    base = PUBLIC_BASE_URL

    # LANはIPで開く運用 → host がプライベートIPならLANへ
    if (not force_public) and _is_private_host(request.host):
        base = LAN_BASE_URL or "http://192.168.3.200"

    base = (base or "").rstrip("/")
    if not base_prefix or not base:
        return ""

    return f"{base}{base_prefix}/{term}/{student_id}/{app_name}/"


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if "student_id" not in session:
            # POST-only endpoints must not be used as "next" (GET would become 405)
            nxt = request.path if request.method == "GET" else f"{URL_PREFIX}/"
            return redirect(url_for("students_login_get", next=nxt))
        return fn(*args, **kwargs)
    return wrapper


def _line_settings() -> dict:
    """DBのグローバルLINE通知設定を取得（取れなければデフォルト）"""
    try:
        return requests_db.get_line_notify_settings(REQUESTS_DB_PATH)
    except Exception:
        # デフォルト：公開/再公開 ON、新規申請/停止 OFF
        return {"on_new": 0, "on_publish": 1, "on_stop": 0, "on_republish": 1}


def _line_enabled(event: str) -> bool:
    s = _line_settings()
    key = {
        "new": "on_new",
        "publish": "on_publish",
        "republish": "on_republish",
        "stop": "on_stop",
    }.get(event, "")
    return bool(s.get(key, 0))


def _notify_line_event(event: str, *, student_id: str, term: str, app_name: str, request_kind: str, public_url: str, ok: bool = True, error: str = "") -> None:
    """
    LINE通知（ON/OFF判定込み）
    - line_notify.py に汎用通知関数が無い前提でも動くよう、既存 _notify_first_deploy_result を流用する。
    """
    if not _line_enabled(event):
        return

    try:
        _notify_first_deploy_result(
            student_id, term, app_name, request_kind, ok,
            app_desc="",
            python_version="",
            java_release="",
            public_url=public_url,
            error=error if not ok else "",
        )
    except TypeError:
        # _notify_first_deploy_result の引数が現環境と微妙に違う場合の保険（既存シグネチャに合わせる）
        _notify_first_deploy_result(
            student_id, term, app_name, request_kind, ok,
            app_desc="",
            public_url=public_url,
            error=error if not ok else "",
        )
    except Exception:
        pass


# ============================================================
# Deploy-check core
# ============================================================
# ------------------------------------------------------------
# Structured Issue (minimal-diff extension)
# ------------------------------------------------------------
def _check_flask_strict(zip_path: Path) -> List[str]:
    scan, issues = _scan_zip(zip_path)

    if not scan.names:
        issues.append("zip内にファイルがありません。")
        return sorted(set(issues))

    if _zip_is_single_folder(scan):
        issues.append("zipが1段深い構造です。zip直下に app.py / requirements.txt が来るように作り直してください。")

    # Windowsバックスラッシュパス検出（Linux展開時にサブディレクトリが作られず TemplateNotFound の原因になる）
    backslash_members = [n for n in scan.names if "\\" in n]
    if backslash_members:
        shown = backslash_members[:5]
        issues.append(
            "ZIPにWindowsのパス区切り（\\）が含まれています。"
            "Linuxサーバーで展開すると templates/auth/ などのサブディレクトリが作られず、"
            "TemplateNotFound エラーになります。"
            "エクスプローラーのZIP作成ではなく、7-Zip 等で '/' 区切りのZIPを作成してください。例: "
            + ", ".join(shown)
        )

    # required files at root
    root_files = {Path(n).name.lower() for n in scan.names if "/" not in n and "\\" not in n}
    if "requirements.txt" not in root_files:
        issues.append("requirements.txt が zip直下にありません。")
    if ("app.py" not in root_files) and ("wsgi.py" not in root_files):
        issues.append("起動点ファイル（app.py または wsgi.py）が zip直下にありません。")

    # DB file included / secrets
    for n in scan.names:
        ext = Path(n).suffix.lower()
        if ext in {".db", ".sqlite", ".sqlite3"}:
            issues.append(f"DBファイル同梱は禁止です: {n}")
        if Path(n).name.lower() == ".env":
            issues.append(".env の同梱は禁止です（秘密情報混入リスク）。")

    # --- Deep checks (read zip members) ---
    try:
        zf = zipfile.ZipFile(zip_path, "r")
    except Exception:
        issues.append("zipファイルが壊れています（開けません）。")
        return sorted(set(issues))

    def _read_member(member: str, limit: int = 512 * 1024) -> bytes:
        try:
            b = zf.read(member)
            if len(b) > limit:
                return b[:limit]
            return b
        except Exception:
            return b""

    with zf:
        # 1) requirements.txt UTF-16 は必ずNG
        if "requirements.txt" in root_files:
            b = _read_member("requirements.txt", limit=512 * 1024)
            if b.startswith(b"\xff\xfe") or b.startswith(b"\xfe\xff"):
                issues.append("requirements.txt が UTF-16 です（必ずNG）。UTF-8で保存し直してください。")
            else:
                # BOMが無くてもUTF-16っぽい（NULが多い）場合を一応弾く
                if b and (b.count(b"\x00") >= max(20, len(b) // 10)):
                    issues.append("requirements.txt が UTF-16 相当の可能性があります（NULが多い）。UTF-8で保存し直してください。")

        # 2) templates/*.html の href/src/action="/..." を検出
        abs_attr_pat = re.compile(r'\b(?:href|src|action)\s*=\s*["\'](/[^"\']*)["\']', re.IGNORECASE)
        abs_hits: List[str] = []

        template_members = [
            n for n in scan.names
            if n.replace("\\", "/").startswith("templates/") and n.lower().endswith((".html", ".htm"))
        ]

        for m in template_members:
            txt = _decode_best_effort(_read_member(m))
            if not txt:
                continue
            for mm in abs_attr_pat.finditer(txt):
                p = mm.group(1)
                # //cdn... や /#... は除外（遷移とは別物が多い）
                if p.startswith("//") or p.startswith("/#"):
                    continue
                abs_hits.append(f"{m}: {mm.group(0)[:140]}")

        if abs_hits:
            shown = abs_hits[:8]
            issues.append(
                'templates のHTMLに "/..." の絶対パス参照（href/src/action）が含まれています。'
                "context-path配下で404になりやすいので提出物として不適切です。例: "
                + " / ".join(shown)
            )
            if len(abs_hits) > 8:
                issues.append(f"（他にも {len(abs_hits) - 8} 件あります）")


        # 2b) JS ファイルの絶対パス検出 (static/**/*.js, templates/**/*.js)
        js_abs_pat = re.compile(
            r'(?:fetch|axios\s*(?:\.\s*(?:get|post|put|delete|patch|request))?)\s*\(\s*[\'"](/[^\'"]{2,}?)[\'"]'
            r'|location\s*(?:\.\s*href)?\s*=\s*[\'"](/[^\'"]{2,}?)[\'"]'
            r'|[\'"](/(?:[A-Za-z0-9_\-]+/)[^\'"]{1,}?)[\'"]',
            re.IGNORECASE,
        )
        js_members = [
            n for n in scan.names
            if n.lower().endswith(".js")
            and (n.replace("\\", "/").startswith("static/") or n.replace("\\", "/").startswith("templates/"))
        ]
        js_abs_hits: List[str] = []
        for m in js_members:
            txt = _decode_best_effort(_read_member(m))
            if not txt:
                continue
            for mm in js_abs_pat.finditer(txt):
                path_val = mm.group(1) or mm.group(2) or mm.group(3) or ""
                if path_val.startswith("//") or path_val.startswith("/#"):
                    continue
                js_abs_hits.append(f"{m}: {mm.group(0)[:140]}")

        if js_abs_hits:
            shown = js_abs_hits[:8]
            issues.append(
                "JS ファイルに \"/...\"始まりの絶対パス参照が含まれています。"
                "context-path配下では必ず404になります。例: "
                + " / ".join(shown)
            )
            if len(js_abs_hits) > 8:
                issues.append(f"（他にも {len(js_abs_hits) - 8} 件あります）")

        # 3) url_for 整合性チェック（テンプレート内で参照している endpoint が Python 側に存在するか）
        # 3-1) Python側 endpoint 抽出（app.route / bp.route + Blueprint名）
        blueprint_var_to_name: dict[str, str] = {}
        endpoints: set[str] = set()

        bp_def_pat = re.compile(r'(\w+)\s*=\s*Blueprint\(\s*[\'"]([^\'"]+)[\'"]', re.MULTILINE)
        # decorator本体（1つ以上のデコレータ + def）
        dec_def_pat = re.compile(
            r'(@[A-Za-z_][A-Za-z0-9_\.]*\.route\(\s*.*?\)\s*)+'
            r'\s*def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(',
            re.DOTALL
        )
        endpoint_kw_pat = re.compile(r'endpoint\s*=\s*[\'"]([^\'"]+)[\'"]')

        py_members = [n for n in scan.names if n.lower().endswith(".py")]
        py_texts: dict[str, str] = {}

        for pm in py_members:
            b = _read_member(pm, limit=1024 * 1024)
            txt = _decode_best_effort(b)
            if not txt:
                continue
            py_texts[pm] = txt
            for m in bp_def_pat.finditer(txt):
                var, name = m.group(1), m.group(2)
                blueprint_var_to_name[var] = name

        for pm, txt in py_texts.items():
            for m in dec_def_pat.finditer(txt):
                decorator_block = m.group(0)
                func_name = m.group(2)

                # どのオブジェクトの route か（app / bp変数）
                # 例: @app.route(...) or @bp.route(...)
                first_line = decorator_block.splitlines()[0].strip()
                target = None
                mt = re.search(r'^@([A-Za-z_][A-Za-z0-9_]*)\.route\(', first_line)
                if mt:
                    target = mt.group(1)

                # endpoint="..." があれば優先
                ep_override = None
                mep = endpoint_kw_pat.search(decorator_block)
                if mep:
                    ep_override = mep.group(1)

                if target and target in blueprint_var_to_name:
                    bpname = blueprint_var_to_name[target]
                    if ep_override:
                        # Flaskは blueprint endpoints を "bpname.endpoint" で扱うのが基本
                        endpoints.add(f"{bpname}.{ep_override}" if "." not in ep_override else ep_override)
                    else:
                        endpoints.add(f"{bpname}.{func_name}")
                else:
                    # app側（または解析不能だが単純にfunc名endpointとして扱う）
                    endpoints.add(ep_override or func_name)

        # 3-2) テンプレート側 url_for("...") 抽出
        url_for_pat = re.compile(r'url_for\(\s*[\'"]([^\'"]+)[\'"]')
        unknown_refs: List[str] = []
        for m in template_members:
            txt = _decode_best_effort(_read_member(m))
            if not txt:
                continue
            for mm in url_for_pat.finditer(txt):
                ep = mm.group(1).strip()
                if not ep:
                    continue
                # Flask標準の static は許可
                if ep == "static":
                    continue
                if ep not in endpoints:
                    unknown_refs.append(f"{m}: url_for('{ep}') が見つかりません（Python側 endpoint 不一致の可能性）")

        if unknown_refs:
            shown = unknown_refs[:10]
            issues.append(
                "[WARN] templates 内の url_for 参照先が、Python側に存在しない可能性があります（画面遷移しない原因になりやすい）。例: "
                + " / ".join(shown)
            )
            if len(unknown_refs) > 10:
                issues.append(f"[WARN]（他にも {len(unknown_refs) - 10} 件あります）")

    return sorted(set(i for i in issues if i))


def _check_static_strict(zip_path: Path) -> List[str]:
    scan, issues = _scan_zip(zip_path)
    if not scan.names:
        issues.append("zip内にファイルがありません。")
        return issues

    if _zip_is_single_folder(scan):
        issues.append("zipが1段深い構造です。zip直下に index.html が来るように作り直してください。")

    root_files = {Path(n).name.lower() for n in scan.names if "/" not in n}
    if "index.html" not in root_files:
        issues.append("index.html が zip直下にありません。")

    for n in scan.names:
        ext = Path(n).suffix.lower()
        if ext in {".db", ".sqlite", ".sqlite3"}:
            issues.append(f"DBファイル同梱は禁止です: {n}")

    return sorted(set(issues))


def _check_spring_jar_strict(jar_path: Path) -> List[str]:
    issues: List[str] = []
    if not jar_path.exists():
        return ["jarファイルが存在しません。"]

    size = jar_path.stat().st_size
    if size < 1 * 1024 * 1024:
        issues.append("jarサイズが小さすぎます（1MB未満）。提出物が不完全な可能性があります。")
    if size > 300 * 1024 * 1024:
        issues.append("jarサイズが大きすぎます（300MB超）。")

    try:
        zf = zipfile.ZipFile(jar_path, "r")
    except Exception:
        return ["jarが壊れています（zipとして開けません）。"]

    with zf:
        names = set(zf.namelist())

        # --- WARN: root (/) mapping / welcome page ---
        # Spring Boot's default welcome page locations
        welcome_candidates = [
            "BOOT-INF/classes/static/index.html",
            "BOOT-INF/classes/public/index.html",
            "BOOT-INF/classes/resources/index.html",
            "BOOT-INF/classes/META-INF/resources/index.html",
        ]
        has_welcome = any(p in names for p in welcome_candidates)

        # templates/index.html は "それだけ" では / に出ないので、ここでは welcome 扱いしない
        # （/ を出すには controller が必要になるため）
        if not has_welcome:
            issues.append(
                "[WARN] ルート(/)の既定ページ（static/index.html 等）が見つかりません。"
                "Controller に @GetMapping(\"/\") が無い場合、公開URL直下は 404（Whitelabel）になりがちです。"
                "対策: @GetMapping(\"/\") で /list 等へ redirect する、または static/index.html を用意してください。"
            )

        # --- required structure (Spring Boot fat jar) ---
        required = [
            "META-INF/MANIFEST.MF",
            "BOOT-INF/",
            "BOOT-INF/classes/",
            "BOOT-INF/lib/",
        ]
        for r in required:
            if r not in names:
                # directories might not appear explicitly; check prefix existence
                if r.endswith("/"):
                    if not any(n.startswith(r) for n in names):
                        issues.append(f"{r} が見つかりません（Spring Boot jarとして不完全）。")
                else:
                    issues.append(f"{r} が見つかりません（起動可能jarとして不完全）。")

        # --- secret-like files ---
        for n in names:
            ext = Path(n).suffix.lower()
            if ext in {".pem", ".key"}:
                issues.append(f"秘密鍵/証明書系ファイルの同梱は禁止です: {n}")

        # --- MANIFEST checks (deploy suitability) ---
        try:
            mf = zf.read("META-INF/MANIFEST.MF").decode("utf-8", errors="replace")
        except Exception:
            mf = ""
    if mf:
        # Build-Jdk-Spec: 17 / 21 / 25 ... など
        #
        # 要件: DEFAULT_JAVA_RELEASE を上限として「それ以下はすべてOK」
        # 例) DEFAULT=25 のとき 17/21/25/11 などはOK、26以上はNG
        m = re.search(r"^Build-Jdk-Spec:\s*(\d+)\s*$", mf, re.MULTILINE)
        if m:
            spec_s = m.group(1)

            default_s = str(getattr(config, "DEFAULT_JAVA_RELEASE", "25")).strip() or "25"
            try:
                default_i = int(default_s)
            except Exception:
                default_i = 25
                default_s = "25"

            try:
                spec_i = int(spec_s)
            except Exception:
                spec_i = None

            if spec_i is None:
                issues.append(
                    "[WARN] Build-Jdk-Spec を数値として判定できません。"
                    "Java 25 以下でビルドされていることを確認してください。"
                )
            elif spec_i > default_i:
                issues.append(
                    f"このjarは Java {spec_i} でビルドされています。"
                    f"サーバーの対応上限は Java {default_i} です。"
                )
            else:
                # OK（止めない）: 情報として残す
                issues.append(
                    f"[INFO] このjarは Java {spec_i} でビルドされています（許可範囲内）。"
                )

        # --- Absolute-path checks in templates/static (context-path事故の主要原因) ---
        # 対象: templates(.html) / static(.html,.css,.js)
        def _read_text(member: str, limit: int = 512 * 1024) -> str:
            try:
                b = zf.read(member)
                if len(b) > limit:
                    b = b[:limit]
                return b.decode("utf-8", errors="replace")
            except Exception:
                return ""

        abs_hits: List[str] = []

        # HTML: href="/...", src="/..."
        html_attr_pat = re.compile(r'\b(?:href|src)\s*=\s*["\'](/[^"\']*)["\']', re.IGNORECASE)
        # CSS: url(/...)
        css_url_pat = re.compile(r'url\(\s*/[^)]+\)', re.IGNORECASE)
        # JS: fetch("/"), axios.get("/"), location.href="/"
        js_pat = re.compile(
            r'\b(fetch|axios\.(get|post)|location\.(href|assign)|window\.location\.(href|assign))\s*\(\s*["\']/',
            re.IGNORECASE
        )

        def _collect_from(member: str, kind: str) -> None:
            txt = _read_text(member)
            if not txt:
                return
            if kind == "html":
                for m in html_attr_pat.finditer(txt):
                    p = m.group(1)
                    if p.startswith("//") or p.startswith("/#"):
                        continue
                    abs_hits.append(f"{member}: {m.group(0)[:120]}")
            elif kind == "css":
                if css_url_pat.search(txt):
                    abs_hits.append(f"{member}: CSS内に url(/...) があります（context-pathで404になりやすい）")
            elif kind == "js":
                if js_pat.search(txt):
                    abs_hits.append(f'{member}: JS内に "/..."" 参照があります（context-pathで404になりやすい）')

        for n in names:
            if n.startswith("BOOT-INF/classes/templates/") and n.lower().endswith((".html", ".htm")):
                _collect_from(n, "html")
            elif n.startswith("BOOT-INF/classes/static/"):
                ln = n.lower()
                if ln.endswith((".html", ".htm")):
                    _collect_from(n, "html")
                elif ln.endswith(".css"):
                    _collect_from(n, "css")
                elif ln.endswith(".js"):
                    _collect_from(n, "js")

        if abs_hits:
            shown = abs_hits[:8]
            issues.append(
                'HTML/CSS/JS に "/xxx" の絶対パス参照が含まれています。'
                "context-path配下で 404 になりやすいため提出物として不適切です。例: "
                + " / ".join(shown)
            )
            if len(abs_hits) > 8:
                issues.append(f"（他にも {len(abs_hits) - 8} 件あります）")

    return sorted(set(issues))


def _check_static_strict_issues(zip_path: Path) -> List[Issue]:
    return [_issue_from_text(s, severity="NG") for s in _check_static_strict(zip_path) if s]


def _check_spring_jar_strict_issues(jar_path: Path) -> List[Issue]:
    return [_issue_from_text(s, severity="NG") for s in _check_spring_jar_strict(jar_path) if s]


def _check_flask_strict_issues(zip_path: Path) -> List[Issue]:
    # まず既存の厳しめ判定（文章）を Issue に包む（差分最小・既存の検出網羅を維持）
    base = [_issue_from_text(s) for s in _check_flask_strict(zip_path) if s]

    # 追加で「どのファイルの何行目」を出せるものだけ、構造化 Issue を上乗せ
    extra: List[Issue] = []

    try:
        zf = zipfile.ZipFile(zip_path, "r")
    except Exception:
        return _dedupe_issues(base)

    def _read_member(member: str, limit: int = 512 * 1024) -> bytes:
        try:
            b = zf.read(member)
            return b[:limit] if len(b) > limit else b
        except Exception:
            return b""

    with zf:
        names = [n for n in zf.namelist() if n and not n.endswith("/") and not n.startswith("__MACOSX/")]

        # requirements.txt UTF-16 は必ずNG（行番号: 1）
        if "requirements.txt" in names:
            b = _read_member("requirements.txt", limit=512 * 1024)
            if b.startswith(b"\xff\xfe") or b.startswith(b"\xfe\xff") or (b and (b.count(b"\x00") >= max(20, len(b) // 10))):
                extra.append(Issue(
                    file="requirements.txt",
                    line=1,
                    problem="requirements.txt が UTF-16 です（必ずNG）。",
                    fix="requirements.txt を UTF-8 で保存し直してください（BOMなし推奨）。",
                    snippet="(encoding: UTF-16)",
                    severity="NG",
                    rule_id="REQ_UTF16",
                ))

        template_members = [
            n for n in names
            if n.replace("\\", "/").startswith("templates/") and n.lower().endswith((".html", ".htm"))
        ]

        # templates の絶対パス href/src/action="/..."
        abs_attr_pat = re.compile(r'\b(?:href|src|action)\s*=\s*["\'](/[^"\']*)["\']', re.IGNORECASE)
        abs_cap = 0
        for m in template_members:
            txt = _decode_best_effort(_read_member(m))
            if not txt:
                continue
            for mm in abs_attr_pat.finditer(txt):
                p = (mm.group(1) or "")
                if p.startswith("//") or p.startswith("/#"):
                    continue
                line_no, line_txt = _line_info(txt, mm.start())
                extra.append(Issue(
                    file=m,
                    line=line_no,
                    problem='templates のHTMLで "/..." の絶対パス参照（href/src/action）が使われています。サブパス配下で404になりやすいです。',
                    fix='相対パスにする（例: href="menu"）か、Flaskなら url_for は SCRIPT_NAME 等の設定込みで運用してください。',
                    snippet=line_txt.strip()[:200],
                    severity="NG",
                    rule_id="HTML_ABS_PATH",
                ))
                abs_cap += 1
                if abs_cap >= 20:
                    break
            if abs_cap >= 20:
                break

    return _dedupe_issues(base + extra)


def _deploy_check(kind: str, upload_path: Path, filename: str, enabled: bool) -> List[Issue]:
    """
    Return structured issues list.
    If enabled=False, only minimal safety checks apply.
    If enabled=True, "strict" checks apply per kind.
    """
    issues: List[Issue] = []
    for s in _basic_file_safety_check(upload_path, filename):
        if s:
            issues.append(_issue_from_text(s, severity="NG"))

    lower = filename.lower()

    if lower.endswith(".zip"):
        _, zip_issues = _scan_zip(upload_path)
        for s in zip_issues:
            if s:
                issues.append(_issue_from_text(s, severity="NG"))

    if not enabled:
        return _dedupe_issues(issues)

    # enabled (strict)
    if kind == "flask":
        if not lower.endswith(".zip"):
            issues.append(_issue_from_text("Flask提出（チェックON）は zip 必須です。単体ファイルはチェックOFFで提出してください。", severity="NG"))
            return _dedupe_issues(issues)
        issues.extend(_check_flask_strict_issues(upload_path))

    elif kind == "streamlit":
        if not lower.endswith(".zip"):
            issues.append(_issue_from_text("Streamlit提出（チェックON）は zip 必須です。", severity="NG"))
            return _dedupe_issues(issues)
        issues.extend(_check_flask_strict_issues(upload_path))  # app.py/requirements.txt 等は共通

    elif kind == "static":
        if lower.endswith(".zip"):
            issues.extend(_check_static_strict_issues(upload_path))

    elif kind == "spring":
        if not lower.endswith(".jar"):
            issues.append(_issue_from_text("Spring Boot提出（チェックON）は jar 必須です。", severity="NG"))
            return _dedupe_issues(issues)
        issues.extend(_check_spring_jar_strict_issues(upload_path))

    else:
        issues.append(_issue_from_text(f"不明な種別です: {kind}", severity="NG"))

    return _dedupe_issues([i for i in issues if i.problem])


# ============================================================
# Views
# ============================================================
def _render_with_msg(msg: str, student_id: str):
    reqs = requests_db.list_by_student(REQUESTS_DB_PATH, student_id)
    term = _term_of(student_id)

    for r in reqs:
        r.status_jp = _status_jp(getattr(r, "status", ""))
        r.public_url = _public_url(term, student_id, r.app_name, r.request_kind) if r.status in ("done", "public", "stopped") else ""
        r.runtime_state = "pending"
        r.service_state = ""
        r.report_exists = _report_path(student_id, int(r.id)).exists()

        if r.request_kind in ("flask", "streamlit", "spring") and r.status in ("done", "public", "stopped"):
            service_name, _ = _service_and_appdir(term, student_id, r.app_name, r.request_kind)
            r.service_state = _get_service_state(service_name)
            if r.status == "stopped":
                r.runtime_state = "stopped"
            else:
                r.runtime_state = "running" if r.service_state == "active" else "stopped"

        if r.request_kind == "static" and r.status in ("done", "public", "stopped"):
            r.runtime_state = "static"

    return render_template(
        "students_dashboard.html",
        msg=msg,
        student_id=student_id,
        reqs=reqs,
        config=config,
        url_prefix=URL_PREFIX,
        require_pin=REQUIRE_PIN,
        deploy_check_default=DEPLOY_CHECK_DEFAULT,
    )



# ============================================================
# Deploy helpers (status sync)
# ============================================================
def _stop_service_quiet(service_name: str) -> None:
    try:
        subprocess.run(["sudo", "systemctl", "stop", service_name], capture_output=True, text=True)
    except Exception:
        pass


def _ensure_service_active(service_name: str) -> None:
    """restart が成功しても即死するケースがあるため、active を必ず確認する"""
    r = subprocess.run(["sudo", "systemctl", "is-active", "--quiet", service_name])
    if r.returncode != 0:
        raise RuntimeError(f"起動に失敗しました: {service_name} が active ではありません")


def _mark_stopped_on_fail(term: str, student_id: str, row, request_id: int, reason: str) -> None:
    # Flask / Spring は systemd を止めてから DB を停止状態にする（失敗しても続行）
    try:
        if getattr(row, "request_kind", "") in ("flask", "spring"):
            service_name, _ = _service_and_appdir(term, student_id, row.app_name, row.request_kind)
            _stop_service_quiet(service_name)
    except Exception:
        pass
    try:
        requests_db.mark_stopped(REQUESTS_DB_PATH, request_id)
    except Exception:
        pass


@app.get(f"{URL_PREFIX}/login")
def students_login_get():
    nxt = request.args.get("next", f"{URL_PREFIX}/")
    return render_template("students_login.html", next=nxt, require_pin=REQUIRE_PIN)


@app.post(f"{URL_PREFIX}/login")
def students_login_post():
    student_id = (request.form.get("student_id") or "").strip()
    nxt = (request.form.get("next") or f"{URL_PREFIX}/").strip()

    if not re.fullmatch(r"\d{6}", student_id):
        return render_template("students_login.html", next=nxt, require_pin=REQUIRE_PIN, msg="受講者番号は6桁数字で入力してください。")

    # 受講者登録テーブルで存在確認
    if not requests_db.student_exists(REQUESTS_DB_PATH, student_id):
        return render_template("students_login.html", next=nxt, require_pin=REQUIRE_PIN, msg="この受講者番号は登録されていません。")

    if REQUIRE_PIN:
        pin = (request.form.get("pin") or "").strip()
        if pin != STUDENT_PIN:
            return render_template("students_login.html", next=nxt, require_pin=REQUIRE_PIN, msg="PINが違います。")

    # avoid redirect to POST-only endpoints
    post_only = {f"{URL_PREFIX}/request", f"{URL_PREFIX}/upload", f"{URL_PREFIX}/restart"}
    if nxt in post_only:
        nxt = f"{URL_PREFIX}/"

    session["student_id"] = student_id
    return redirect(nxt)


@app.get(f"{URL_PREFIX}/logout")
def students_logout():
    session.pop("student_id", None)
    return redirect(url_for("students_login_get"))


@app.get(f"{URL_PREFIX}/report/<int:request_id>")
@login_required
def students_report(request_id: int):
    student_id = session["student_id"]
    report = _report_path(student_id, request_id)
    if not report.exists():
        return _render_with_msg("チェックレポートはありません（チェックOFF、または問題なし）。", student_id)
    return send_file(report, mimetype="text/html", as_attachment=False)


@app.get(f"{URL_PREFIX}/")
@login_required
def students_dashboard():
    return _render_with_msg("", session["student_id"])


@app.post(f"{URL_PREFIX}/request")
@login_required
def students_request():
    student_id = session["student_id"]
    term = _term_of(student_id)

    app_name = (request.form.get("app_name") or "").strip()
    request_kind = (request.form.get("request_kind") or "").strip()  # static/flask/spring
    app_desc = (request.form.get("app_desc") or "").strip()
    if len(app_desc) > 200:
        return _render_with_msg("アプリ概要は200文字以内で入力してください。", student_id)
    if app_desc == "":
        app_desc = None

    if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", app_name):
        return _render_with_msg("アプリ名は英数字/ハイフン/アンダースコアで入力してください（最大40文字）。", student_id)
    if request_kind not in ("static", "flask", "streamlit", "spring"):
        return _render_with_msg("種別が不正です。", student_id)

    # 追加：種別ごとのバージョン（autoは廃止。未選択なら既定値へ寄せる）
    python_version = None
    java_release = None

    if request_kind in ("flask", "streamlit"):
        v = (request.form.get("python_version") or "").strip()
        if v not in getattr(config, "SUPPORTED_PYTHON_VERSIONS", []):
            v = "3.12"
        python_version = v

    if request_kind == "spring":
        v = (request.form.get("java_release") or "").strip()

        # DEFAULT_JAVA_RELEASE を上限として「それ以下はすべてOK」
        # 未選択/不正は既定値へ寄せる
        default_java_s = str(getattr(config, "DEFAULT_JAVA_RELEASE", "25")).strip() or "25"
        try:
            default_java_i = int(default_java_s)
        except Exception:
            default_java_i = 25
            default_java_s = "25"

        if not v:
            v = default_java_s
        else:
            try:
                vi = int(v)
                if vi > default_java_i:
                    v = default_java_s
            except Exception:
                v = default_java_s

        java_release = v

    # 修正：requests_db は add_request() が正（insert_request は存在しない）
    try:
        request_id = requests_db.add_request(
            REQUESTS_DB_PATH,
            student_id=student_id,
            app_name=app_name,
            request_kind=request_kind,
            app_desc=app_desc,
            python_version=python_version,
            java_release=java_release,
        )
    except ValueError as e:
        return _render_with_msg(str(e), student_id)

    # ★追加：新規申請は講師確認なしで「初回枠作成」→ done
    try:
        port = None

        if request_kind in ("flask", "streamlit", "spring"):
            # ポート確保（欠番運用）
            port = ids_ports.auto_port(request_kind, student_id)

        if request_kind == "flask":
            flask_runtime.setup_flask_runtime(
                term=term,
                student_id=student_id,
                app_name=app_name,
                port=int(port),
                python_version=(python_version or getattr(config, "DEFAULT_PYTHON_VERSION", "3.12")),
            )
            nginx_utils.write_student_location("flask", term, student_id, app_name, port=int(port))
            if not nginx_utils.reload_nginx_with_check():
                raise RuntimeError("nginx reload failed")

        elif request_kind == "streamlit":
            flask_runtime.setup_flask_runtime(
                term=term,
                student_id=student_id,
                app_name=app_name,
                port=int(port),
                python_version=(python_version or getattr(config, "DEFAULT_PYTHON_VERSION", "3.12")),
            )
            nginx_utils.write_student_location("flask", term, student_id, app_name, port=int(port), is_streamlit=True)
            if not nginx_utils.reload_nginx_with_check():
                raise RuntimeError("nginx reload failed")

        elif request_kind == "spring":
            spring_runtime.first_deploy_spring(
                term=term,
                student_id=student_id,
                app_name=app_name,
                port=int(port),
                jar_name="app.jar",
            )
            nginx_utils.write_student_location("spring", term, student_id, app_name, port=int(port))
            if not nginx_utils.reload_nginx_with_check():
                raise RuntimeError("nginx reload failed")

        else:
            # static
            common_paths.public_dir(term, student_id, app_name).mkdir(parents=True, exist_ok=True)
            nginx_utils.write_student_location("static", term, student_id, app_name, port=None)
            if not nginx_utils.reload_nginx_with_check():
                raise RuntimeError("nginx reload failed")

        # done: 公開枠が確保できた（ファイル未アップロードでもOK）
        requests_db.mark_done(REQUESTS_DB_PATH, request_id, port=port)
        public_url = _public_url(term, student_id, app_name, request_kind, force_public=True)
        _notify_line_event(
            "new",
            student_id=student_id, term=term, app_name=app_name, request_kind=request_kind,
            public_url=public_url,
            ok=True
        )

    except Exception as e:
        # 失敗時は error に落として講師が介入できるように残す
        try:
            requests_db.mark_error(REQUESTS_DB_PATH, request_id, port=port)
            public_url = _public_url(term, student_id, app_name, request_kind, force_public=True)
            _notify_line_event(
                "new",
                student_id=student_id, term=term, app_name=app_name, request_kind=request_kind,
                public_url=public_url,
                ok=False,
                error=str(e),
            )

        except Exception:
            pass
        return _render_with_msg(f"初回公開枠の作成に失敗しました。講師に連絡してください。({e})", student_id)

    return redirect(url_for("students_dashboard"))


def _redeploy_now(row, term: str, student_id: str):
    request_id = int(row.id)
    prev_status = getattr(row, "status", "")

    try:
        if row.request_kind == "spring":
            service_name, app_dir = _service_and_appdir(term, student_id, row.app_name, "spring")
            if not (Path(app_dir) / "app.jar").exists():
                return _render_with_msg("まだ jar がアップロードされていません。先にアップロードしてください。", student_id)

            r = subprocess.run(["sudo", "systemctl", "restart", service_name], capture_output=True, text=True)
            if r.returncode != 0:
                raise RuntimeError((r.stderr or "").strip() or "systemctl restart failed")
            _ensure_service_active(service_name)

            requests_db.mark_public(REQUESTS_DB_PATH, request_id, port=row.port)
            event = "republish" if prev_status == "stopped" else "publish"
            public_url = _public_url(term, student_id, row.app_name, row.request_kind, force_public=True)
            _notify_line_event(event, student_id=student_id, term=term, app_name=row.app_name, request_kind=row.request_kind, public_url=public_url, ok=True)
            return redirect(url_for("students_dashboard"))

        if row.request_kind == "flask":
            service_name, app_dir = _service_and_appdir(term, student_id, row.app_name, "flask")
            app_dir_p = Path(app_dir)
            if not _has_any_file(app_dir_p):
                return _render_with_msg("まだ .zip / ファイルがアップロードされていません。先にアップロードしてください。", student_id)

            nginx_utils.write_student_location(
                "flask", term, student_id, row.app_name,
                port=int(row.port), is_streamlit=False,
            )
            if not nginx_utils.reload_nginx_with_check():
                raise RuntimeError("nginx reload failed")
            r = subprocess.run(["sudo", "systemctl", "restart", service_name], capture_output=True, text=True)
            if r.returncode != 0:
                raise RuntimeError((r.stderr or "").strip() or "systemctl restart failed")
            _ensure_service_active(service_name)

            requests_db.mark_public(REQUESTS_DB_PATH, request_id, port=row.port)
            event = "republish" if prev_status == "stopped" else "publish"
            public_url = _public_url(term, student_id, row.app_name, row.request_kind, force_public=True)
            _notify_line_event(event, student_id=student_id, term=term, app_name=row.app_name, request_kind=row.request_kind, public_url=public_url, ok=True)
            return redirect(url_for("students_dashboard"))

        if row.request_kind == "streamlit":
            service_name, app_dir = _service_and_appdir(term, student_id, row.app_name, "streamlit")
            app_dir_p = Path(app_dir)
            if not _has_any_file(app_dir_p):
                return _render_with_msg("まだ .zip / ファイルがアップロードされていません。先にアップロードしてください。", student_id)

            nginx_utils.write_student_location(
                "flask", term, student_id, row.app_name,
                port=int(row.port), is_streamlit=True,
            )
            if not nginx_utils.reload_nginx_with_check():
                raise RuntimeError("nginx reload failed")
            flask_runtime.setup_streamlit_runtime(
                term=term,
                student_id=student_id,
                app_name=row.app_name,
                port=int(row.port),
            )

            requests_db.mark_public(REQUESTS_DB_PATH, request_id, port=row.port)
            event = "republish" if prev_status == "stopped" else "publish"
            public_url = _public_url(term, student_id, row.app_name, row.request_kind, force_public=True)
            _notify_line_event(event, student_id=student_id, term=term, app_name=row.app_name, request_kind=row.request_kind, public_url=public_url, ok=True)
            return redirect(url_for("students_dashboard"))

        # static
        public_dir = common_paths.public_dir(term, student_id, row.app_name)
        if not _has_any_file(public_dir):
            return _render_with_msg("まだ .zip / ファイルがアップロードされていません。先にアップロードしてください。", student_id)

        requests_db.mark_public(REQUESTS_DB_PATH, request_id, port=None)
        event = "republish" if prev_status == "stopped" else "publish"
        public_url = _public_url(term, student_id, row.app_name, row.request_kind, force_public=True)
        _notify_line_event(event, student_id=student_id, term=term, app_name=row.app_name, request_kind=row.request_kind, public_url=public_url, ok=True)
        return redirect(url_for("students_dashboard"))

    except Exception as e:
        _mark_stopped_on_fail(term, student_id, row, request_id, str(e))
        return _render_with_msg(f"再デプロイに失敗:{e}", student_id)


@app.post(f"{URL_PREFIX}/upload")
@login_required
def students_upload():
    """アップロード＋再デプロイ（チェックONなら厳しめ判定で block）"""
    student_id = session["student_id"]
    term = _term_of(student_id)

    request_id = int(request.form.get("request_id") or "0")
    row = requests_db.get_by_id(REQUESTS_DB_PATH, request_id)
    if not row or row.student_id != student_id:
        return _render_with_msg("対象の申請が見つかりません。", student_id)

    # ★追加：再公開判定用
    prev_status = row.status
    # ★ 新しいアップロード（または再デプロイ）開始時点で、旧レポートは必ず消す
    _delete_report_if_exists(student_id, request_id)

    if row.status == "received":
        return _render_with_msg("申請中はアップロード/再デプロイできません。", student_id)

    if row.status not in ("done", "public", "stopped"):
        return _render_with_msg("この状態ではアップロード/再デプロイできません。", student_id)

    f = request.files.get("file")
    if not f or not getattr(f, "filename", ""):
        _delete_report_if_exists(student_id, request_id)
        # no file => behave like restart-only
        return _redeploy_now(row, term, student_id)

    filename = _safe_filename(f.filename)
    lower = filename.lower()

    # deploy-check toggle (upload form only)
    raw = request.form.get("deploy_check")
    deploy_check = (raw or DEPLOY_CHECK_DEFAULT).lower()

    # deploy_check がフォームから来ていない事故対策：
    # 「来てないならON扱い」にして、必ず検出する
    if raw is None and row.request_kind in ("spring", "flask", "streamlit", "static"):
        deploy_check = "on"

    check_enabled = deploy_check in ("on", "1", "true", "yes")
    print(f"[UPLOAD HIT] request_id={request.form.get('request_id')!r} deploy_check={request.form.get('deploy_check')!r}", flush=True)


    # save to temp
    tmp_dir = Path(tempfile.mkdtemp(prefix="upload_", dir=str(BASE_DIR)))
    try:
        save_path = tmp_dir / filename
        f.save(str(save_path))

        # run checks
        issues = _deploy_check(row.request_kind, save_path, filename, enabled=check_enabled)

        # レポートは「指摘があれば」作る（WARN/INFO含む）
        if issues:
            report = _report_path(student_id, request_id)
            _write_html_report(
                report,
                title=f"アプリ名: {row.app_name} / 種別: {row.request_kind}",
                issues=issues,
            )

        # 不合格（デプロイ停止）は NG が1件でもある場合だけ
        has_ng = any(i.severity == "NG" for i in issues)

        if has_ng:
            # ★ NG のときは「停止」にする（停止表示と実稼働を一致させる）
            if row.request_kind in ("flask", "streamlit", "spring"):
                try:
                    service_name, _ = _service_and_appdir(term, student_id, row.app_name, row.request_kind)
                    subprocess.run(["sudo", "systemctl", "stop", service_name], capture_output=True, text=True)
                except Exception:
                    pass  # 停止失敗で処理全体は止めない（必要なら厳密化）

            try:
                requests_db.mark_stopped(REQUESTS_DB_PATH, request_id)
            except Exception as e:
                return _render_with_msg(f"停止状態の記録に失敗しました: {e}", student_id)

            # keep last_upload for visibility
            try:
                requests_db.update_last_upload(REQUESTS_DB_PATH, request_id, filename)
            except Exception as e:
                return _render_with_msg(
                    f"アップロードは受け付けましたが、DBのlast_upload更新に失敗しました: {e}",
                    student_id
                )

            return _render_with_msg(
                "チェックで致命的な問題（NG）が見つかったため、デプロイせず停止状態にしました。『チェックレポート』を確認してください。",
                student_id
            )

        # OK => deploy
        if row.request_kind == "spring":
            service_name, app_dir = _service_and_appdir(term, student_id, row.app_name, "spring")
            app_dir_p = Path(app_dir)
            app_dir_p.mkdir(parents=True, exist_ok=True)
            (app_dir_p / "app.jar").write_bytes(save_path.read_bytes())

            r = subprocess.run(["sudo", "systemctl", "restart", service_name], capture_output=True, text=True)
            if r.returncode != 0:
                raise RuntimeError((r.stderr or "").strip() or "systemctl restart failed")
            _ensure_service_active(service_name)

        elif row.request_kind == "flask":
            service_name, app_dir = _service_and_appdir(term, student_id, row.app_name, "flask")
            app_dir_p = Path(app_dir)
            app_dir_p.mkdir(parents=True, exist_ok=True)

            if lower.endswith(".zip"):
                _safe_extract_zip(save_path, app_dir_p)
                ok, msg = _install_requirements_if_present(app_dir_p)
                if not ok:
                    raise RuntimeError(msg)
                nginx_utils.write_student_location(
                    "flask", term, student_id, row.app_name,
                    port=int(row.port), is_streamlit=False,
                )
                if not nginx_utils.reload_nginx_with_check():
                    raise RuntimeError("nginx reload failed")
                r = subprocess.run(["sudo", "systemctl", "restart", service_name], capture_output=True, text=True)
                if r.returncode != 0:
                    raise RuntimeError((r.stderr or "").strip() or "systemctl restart failed")
                _ensure_service_active(service_name)
            else:
                # (check OFF) single file: html -> templates, else -> static
                target_dir = (app_dir_p / "templates") if lower.endswith((".html", ".htm")) else (app_dir_p / "static")
                target_dir.mkdir(parents=True, exist_ok=True)
                (target_dir / filename).write_bytes(save_path.read_bytes())

        elif row.request_kind == "streamlit":
            service_name, app_dir = _service_and_appdir(term, student_id, row.app_name, "streamlit")
            app_dir_p = Path(app_dir)
            app_dir_p.mkdir(parents=True, exist_ok=True)

            if lower.endswith(".zip"):
                _safe_extract_zip(save_path, app_dir_p)
                ok, msg = _install_requirements_if_present(app_dir_p)
                if not ok:
                    raise RuntimeError(msg)
                nginx_utils.write_student_location(
                    "flask", term, student_id, row.app_name,
                    port=int(row.port), is_streamlit=True,
                )
                if not nginx_utils.reload_nginx_with_check():
                    raise RuntimeError("nginx reload failed")
                flask_runtime.setup_streamlit_runtime(
                    term=term,
                    student_id=student_id,
                    app_name=row.app_name,
                    port=int(row.port),
                )
            else:
                # 単ファイルはStreamlitでは非対応
                raise RuntimeError("Streamlit アプリは .zip 形式でアップロードしてください。")

        else:
            public_dir = common_paths.public_dir(term, student_id, row.app_name)
            public_dir.mkdir(parents=True, exist_ok=True)

            if lower.endswith(".zip"):
                _safe_extract_zip(save_path, public_dir)
            else:
                (public_dir / filename).write_bytes(save_path.read_bytes())

        requests_db.mark_public(REQUESTS_DB_PATH, request_id, port=row.port)
        requests_db.update_last_upload(REQUESTS_DB_PATH, request_id, filename)
        event = "republish" if prev_status == "stopped" else "publish"
        public_url = _public_url(term, student_id, row.app_name, row.request_kind, force_public=True)
        _notify_line_event(event, student_id=student_id, term=term, app_name=row.app_name, request_kind=row.request_kind, public_url=public_url, ok=True)
        return redirect(url_for("students_dashboard"))

    except Exception as e:
        _mark_stopped_on_fail(term, student_id, row, request_id, str(e))
        return _render_with_msg(f"再デプロイに失敗:\n{e}", student_id)
    finally:
        try:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            pass


@app.post(f"{URL_PREFIX}/restart")
@login_required
def students_restart():
    """再デプロイ（ファイル無し専用ボタン用）"""
    student_id = session["student_id"]
    term = _term_of(student_id)

    request_id = int(request.form.get("request_id") or "0")
    row = requests_db.get_by_id(REQUESTS_DB_PATH, request_id)
    if not row or row.student_id != student_id:
        return _render_with_msg("対象の申請が見つかりません。", student_id)

    if row.status == "received":
        return _render_with_msg("申請中は再デプロイできません。", student_id)

    if row.status not in ("done", "public", "stopped"):
        return _render_with_msg("この状態では再デプロイできません。", student_id)

    return _redeploy_now(row, term, student_id)


@app.post(f"{URL_PREFIX}/stop")
@login_required
def students_stop():
    """デプロイ中止（削除ではなく停止）"""
    student_id = session["student_id"]
    term = _term_of(student_id)

    request_id = int(request.form.get("request_id") or "0")
    row = requests_db.get_by_id(REQUESTS_DB_PATH, request_id)
    if not row or row.student_id != student_id:
        return _render_with_msg("対象の申請が見つかりません。", student_id)
    prev_status = row.status

    if row.status == "received":
        return _render_with_msg("申請中はデプロイ中止できません。", student_id)

    if row.status not in ("done", "public", "stopped"):
        return _render_with_msg("この状態ではデプロイ中止できません。", student_id)

    # Flask / Spring は systemd を停止、static は「停止」状態だけ付ける
    if row.request_kind in ("flask", "streamlit", "spring") and row.status != "stopped":
        service_name, _ = _service_and_appdir(term, student_id, row.app_name, row.request_kind)
        r = subprocess.run(["sudo", "systemctl", "stop", service_name], capture_output=True, text=True)
        if r.returncode != 0:
            return _render_with_msg(f"デプロイ中止に失敗:\n{(r.stderr or '').strip()}", student_id)

    # DB を停止状態に更新
    try:
        requests_db.mark_stopped(REQUESTS_DB_PATH, request_id)
    except Exception as e:
        return _render_with_msg(f"停止状態の記録に失敗しました: {e}", student_id)

    if prev_status != "stopped":
        public_url = _public_url(term, student_id, row.app_name, row.request_kind, force_public=True)
        _notify_line_event("stop", student_id=student_id, term=term, app_name=row.app_name, request_kind=row.request_kind, public_url=public_url, ok=True)

    return redirect(url_for("students_dashboard"))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    app.run(host="127.0.0.1", port=port, debug=False)
