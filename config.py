# -*- coding: utf-8 -*-
"""Students deploy system configuration.

秘密情報（SECRET_KEY, LINE_*）のみ環境変数で受け取る。
それ以外はこのファイルに直値で定義する。
"""

from __future__ import annotations

import os
from pathlib import Path

# -----------------------
# Base directory
# -----------------------
BASE_DIR = Path(__file__).resolve().parent

# -----------------------
# Security / Sessions
# 秘密情報のみ環境変数で受け取る（systemd の Environment= で設定）
# -----------------------
SECRET_KEY = os.environ.get("SECRET_KEY", "")
if not SECRET_KEY:
    raise RuntimeError(
        "環境変数 SECRET_KEY が設定されていません。"
        "systemd のユニットファイルに Environment=SECRET_KEY=... を追加してください。"
    )

# students_app.py が FLASK_SECRET という名前で参照しているため別名を用意
FLASK_SECRET = SECRET_KEY

# 講師の物理削除操作（purge）に必要なキー
PURGE_KEY = os.environ.get("PURGE_KEY", "")

# -----------------------
# Database
# -----------------------
REQUESTS_DB_PATH = BASE_DIR / "requests.db"

# -----------------------
# URL / routing
# -----------------------
STUDENTS_URL_PREFIX = "/students"
TEACHER_URL_PREFIX  = "/teacher"
PUBLIC_BASE_URL     = "https://telcation.f5.si"
LAN_BASE_URL        = "http://192.168.3.16"

# -----------------------
# Student login PIN (無効)
# -----------------------
REQUIRE_PIN        = False
STUDENT_PIN        = ""
STUDENTS_LOGIN_PIN = ""

# -----------------------
# Storage / directories
# -----------------------
BASE_DIR_FLASK  = "/srv/students"
BASE_DIR_SPRING = "/srv/students"
BASE_DIR_STATIC = "/srv/students"

NGINX_SNIPPET_DIR = Path("/etc/nginx/students.d")
SYSTEMD_DIR       = Path("/etc/systemd/system")

# -----------------------
# Flask / gunicorn prefix support
# -----------------------
WSGI_PREFIX_DIR        = "/home/appuser/students_deployer"
WSGI_PREFIX_ENTRYPOINT = "wsgi_prefix:app"
FLASK_TEMPLATE_DIR     = BASE_DIR / "template_flask"

# -----------------------
# Deploy check
# -----------------------
DEPLOY_CHECK_DEFAULT = "on"

# -----------------------
# Runtime versions
# -----------------------
DEFAULT_PYTHON_VERSION    = "3.12"
DEFAULT_JAVA_RELEASE      = "25"
DEFAULT_SPRING_BUILD_TOOL = "gradle"

SUPPORTED_PYTHON_VERSIONS    = ["3.12", "3.13", "3.14"]
SUPPORTED_JAVA_RELEASES      = ["17", "21", "25"]
SUPPORTED_SPRING_BUILD_TOOLS = ["gradle", "maven"]

PYTHON_BINS = {
    "3.12": "/usr/bin/python3.12",
    "3.13": "/usr/bin/python3.13",
    "3.14": "/usr/bin/python3.14",
    "python3": "python3",
}

JAVA_HOMES = {
    "17": "",
    "21": "",
    "25": "",
}

# -----------------------
# Port allocation
# -----------------------
PORT_BASE = 10000
PORT_MIN  = 1024
PORT_MAX  = 64999

# -----------------------
# LINE通知
# 秘密情報のみ環境変数で受け取る（systemd の Environment= で設定）
# -----------------------
LINE_NOTIFY_ENABLED       = os.environ.get("LINE_NOTIFY_ENABLED", "0").lower() not in ("0", "false", "off", "no", "")
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_NOTIFY_TO            = os.environ.get("LINE_NOTIFY_TO", "")
LINE_NOTIFY_PREFIX        = "【受講者デプロイ】"

# -----------------------
# Helpers
# -----------------------
def ensure_dirs() -> None:
    """起動時に必要なディレクトリを作成する。"""
    NGINX_SNIPPET_DIR.mkdir(parents=True, exist_ok=True)
    REQUESTS_DB_PATH.parent.mkdir(parents=True, exist_ok=True)

# 後方互換エイリアス
ensure_data_dirs  = ensure_dirs
STUDENTS_BASE_DIR = BASE_DIR_FLASK
STUDENTS_ROOT     = BASE_DIR_FLASK
