# -*- coding: utf-8 -*-
"""Students deploy system configuration (完成版).

Imported by:
- teacher_app.py (teacher UI / deployer)
- students_app.py (student UI / request & upload)

Design:
- 既存運用を壊さないため、値は「環境変数 > デフォルト」の順で決定。
- Path は pathlib.Path を使用（.mkdir() などが使える）。
"""

from __future__ import annotations

from pathlib import Path
import os
import secrets

# -----------------------
# Security / Sessions
# -----------------------
# Keep stable after deployment; changing it logs everyone out.
SECRET_KEY = os.environ.get("STUDENTS_SECRET_KEY") or secrets.token_hex(32)

# -----------------------
# RESERVED (vNext): Teacher auth (optional)
# -----------------------
# If empty, teacher UI basic-auth is disabled (IP制限のみ運用も可)
TEACHER_BASIC_USER = os.environ.get("TEACHER_BASIC_USER", "")
TEACHER_BASIC_PASS = os.environ.get("TEACHER_BASIC_PASS", "")

# 例: "192.168.,10.,172.16." のような prefix 指定（空なら全許可）
# students_deployer.py 側で str/list どちらでも扱えるようにしているので、
# ここは「文字列 or 空」でOK。
TEACHER_ALLOW_IP_PREFIXES = os.environ.get("TEACHER_ALLOW_IP_PREFIXES", "")

#
# Teacher login PIN
#
TEACHER_LOGIN_PIN = os.environ.get("TEACHER_LOGIN_PIN", "").strip()

# -----------------------
# Dangerous operations
# -----------------------
# If empty, emergency physical delete is disabled.
PURGE_KEY = os.environ.get("PURGE_KEY", "")

# -----------------------
# Student login PIN (optional)
# -----------------------
# students_redeploy.py 側が REQUIRE_PIN を参照する実装でも落ちないように追加。
# 0/false/off/no -> False 扱い
def _env_bool(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    v = str(v).strip().lower()
    return v not in ("0", "false", "off", "no", "")

# PIN必須にするなら: REQUIRE_PIN=1 を環境変数で指定
REQUIRE_PIN = _env_bool("REQUIRE_PIN", False)

# PIN本体（REQUIRE_PIN=1 のときに照合したい場合）
# 例: STUDENTS_LOGIN_PIN=1234
STUDENTS_LOGIN_PIN = os.environ.get("STUDENTS_LOGIN_PIN", "")

# -----------------------
# URL / routing
# -----------------------
# Student UI prefix (students_redeploy.py)
STUDENTS_URL_PREFIX = (os.environ.get("STUDENTS_URL_PREFIX", "/students").rstrip("/")) or "/students"
TEACHER_URL_PREFIX = (os.environ.get("TEACHER_URL_PREFIX", "/teacher").rstrip("/")) or "/teacher"

# External public base URL shown to students
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "https://telcation.f5.si")

# RESERVED (vNext): LAN base URL shown to students (if you want)
LAN_BASE_URL = os.environ.get("LAN_BASE_URL", "http://192.168.3.200")

# -----------------------
# Storage / directories
# -----------------------
BASE_DIR_FLASK = os.environ.get("BASE_DIR_FLASK", "/srv/students")
BASE_DIR_SPRING = os.environ.get("BASE_DIR_SPRING", "/srv/students")
BASE_DIR_STATIC = os.environ.get("BASE_DIR_STATIC", "/srv/students")

# ---- backward-compatible aliases ----
# Some modules may still refer to these older names.
STUDENTS_BASE_DIR = os.environ.get("STUDENTS_BASE_DIR", BASE_DIR_FLASK)  # default: /srv/students
STUDENTS_ROOT = STUDENTS_BASE_DIR

# RESERVED (vNext): 互換用の別名（現行コードでは未使用）
STUDENTS_NGINX_DIR = os.environ.get("STUDENTS_NGINX_DIR", "/etc/nginx/students.d")

NGINX_SNIPPET_DIR = Path(os.environ.get("NGINX_SNIPPET_DIR", "/etc/nginx/students.d"))  # canonical
SYSTEMD_DIR = Path(os.environ.get("SYSTEMD_DIR", "/etc/systemd/system"))

BASE_DIR = Path(__file__).resolve().parent
# requests DB: prod default -> requests.db, dev overrides via env
REQUESTS_DB_PATH = Path(os.environ.get("REQUESTS_DB_PATH", str(BASE_DIR / "requests.db")))
UPLOADS_DIR = Path(os.environ.get("UPLOADS_DIR", "/home/appuser/students_deployer/uploads"))

# Template directory (used by flask_runtime.py)
PROJECT_DIR = Path(__file__).resolve().parent
FLASK_TEMPLATE_DIR = Path(os.environ.get("FLASK_TEMPLATE_DIR", str(PROJECT_DIR / "template_flask")))

# -----------------------
# Flask prefix support (SCRIPT_NAME)
# -----------------------
# wsgi_prefix.py を import できるディレクトリ（基盤側の共通ファイル置き場）
WSGI_PREFIX_DIR = Path(os.environ.get("WSGI_PREFIX_DIR", "/home/appuser/students_deployer"))
# # Runtime versions の起動ターゲット（wsgi_prefix.py 内の app）
WSGI_PREFIX_ENTRYPOINT = os.environ.get("WSGI_PREFIX_ENTRYPOINT", "wsgi_prefix:app")

# -----------------------
# Deploy check (student upload)
# -----------------------
# on / off
DEPLOY_CHECK_DEFAULT = os.environ.get("DEPLOY_CHECK_DEFAULT", "on")
# -----------------------
# Upload safety limits
# -----------------------
MAX_UPLOAD_BYTES = 200 * 1024 * 1024
MAX_ZIP_FILES = 3000
MAX_ZIP_TOTAL_BYTES = 300 * 1024 * 1024
MAX_SINGLE_FILE_BYTES = 100 * 1024 * 1024

# -----------------------
# Runtime versions
# -----------------------
DEFAULT_PYTHON_VERSION = os.environ.get("DEFAULT_PYTHON_VERSION", "3.12")
DEFAULT_JAVA_RELEASE = os.environ.get("DEFAULT_JAVA_RELEASE", "25")
DEFAULT_SPRING_BUILD_TOOL = os.environ.get("DEFAULT_SPRING_BUILD_TOOL", "gradle")
GUNICORN_TIMEOUT = int(os.environ.get("GUNICORN_TIMEOUT", "300"))

SUPPORTED_PYTHON_VERSIONS = ["3.12", "3.13", "3.14"]
SUPPORTED_JAVA_RELEASES = ["17", "21", "25"]
SUPPORTED_SPRING_BUILD_TOOLS = ["gradle", "maven"]

# Interpreters / JAVA_HOME mapping (needed by flask_runtime.py / spring_runtime.py)
PYTHON_BINS = {
    "3.12": os.environ.get("PYTHON_BIN_3_12", "/usr/bin/python3.12"),
    "3.13": os.environ.get("PYTHON_BIN_3_13", "/usr/bin/python3.13"),
    "3.14": os.environ.get("PYTHON_BIN_3_14", "/usr/bin/python3.14"),
    "python3": "python3",
}

JAVA_HOMES = {
    # Set these if you manage multiple JDKs; otherwise /usr/bin/java will be used.
    "17": os.environ.get("JAVA_HOME_17", ""),
    "21": os.environ.get("JAVA_HOME_21", ""),
    "25": os.environ.get("JAVA_HOME_25", ""),
}

# -----------------------
# Port allocation policy (ids_ports.py)
# -----------------------
PORT_BASE = int(os.environ.get("PORT_BASE", "10000"))
PORT_MIN = int(os.environ.get("PORT_MIN", "1024"))
PORT_MAX = int(os.environ.get("PORT_MAX", "64999"))
PORT_LIST_FILE = os.environ.get("PORT_LIST_FILE", "/srv/students/ports.txt")

#
# LINE通知
#
# デフォルトOFF + envで指定
LINE_NOTIFY_ENABLED = _env_bool("LINE_NOTIFY_ENABLED", False)
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_NOTIFY_TO = os.environ.get("LINE_NOTIFY_TO", "")  # カンマで複数可
LINE_NOTIFY_PREFIX = os.environ.get("LINE_NOTIFY_PREFIX", "【受講者デプロイ】")

# -----------------------
# 環境リセット共通
# -----------------------
# リセット操作の実行に必要なキー（PURGE_KEY とは別に管理）
RESET_KEY = os.environ.get("RESET_KEY", "")

# -----------------------
# NextCloud リセット
# -----------------------
# SSH でアクセスする NextCloud サーバーの IP またはホスト名
NEXTCLOUD_SSH_HOST = os.environ.get("NEXTCLOUD_SSH_HOST", "192.168.3.201")

# SSH ログインユーザー（sudo 権限が必要）
NEXTCLOUD_SSH_USER = os.environ.get("NEXTCLOUD_SSH_USER", "ksk")

# PHP バイナリのパス（occ の実行に使用）
NEXTCLOUD_OCC_PHP  = os.environ.get("NEXTCLOUD_OCC_PHP",  "/usr/bin/php")

# occ コマンドのパス
NEXTCLOUD_OCC_PATH = os.environ.get("NEXTCLOUD_OCC_PATH", "/var/www/nextcloud/occ")

# NextCloud の実行ユーザー（occ を sudo -u で実行するユーザー）
NEXTCLOUD_WEBROOT  = os.environ.get("NEXTCLOUD_WEBROOT",  "www-data")

# NextCloud データディレクトリ（`occ config:system:get datadirectory` で確認）
NEXTCLOUD_DATA_DIR = os.environ.get("NEXTCLOUD_DATA_DIR", "/mnt/share/nextcloud")

# -----------------------
# メールボックスリセット
# -----------------------
# SSH でアクセスするメールサーバーのホスト名
MAIL_SSH_HOST      = os.environ.get("MAIL_SSH_HOST",      "telcation.com")

# SSH ログインユーザー（sudo 権限が必要）
MAIL_SSH_USER      = os.environ.get("MAIL_SSH_USER",      "user")

# Maildir の親ディレクトリ（各ユーザーの Maildir は {base}/{user}/Maildir を想定）
MAIL_MAILDIR_BASE  = os.environ.get("MAIL_MAILDIR_BASE",  "/home")

# クリア対象ユーザー一覧ファイル（1行1ユーザー名）
# 空にすると Maildir が存在するホームユーザーを自動検出する
MAIL_USERS_FILE    = os.environ.get("MAIL_USERS_FILE",    "")

# 自動検出時に除外するユーザー（カンマ区切り）
# "user" のようなシステム管理用ユーザーを誤削除しないために設定する
MAIL_EXCLUDE_USERS = os.environ.get("MAIL_EXCLUDE_USERS", "user,ubuntu,root")

# -----------------------
# Helpers
# -----------------------
def ensure_dirs() -> None:
    """Create required directories if missing."""
    NGINX_SNIPPET_DIR.mkdir(parents=True, exist_ok=True)
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    REQUESTS_DB_PATH.parent.mkdir(parents=True, exist_ok=True)

def ensure_data_dirs() -> None:
    """Backward-compatible alias (students_redeploy.py uses this name)."""
    ensure_dirs()
