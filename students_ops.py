# students_ops.py (extracted from students_app.py; no behavior change)
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional, Tuple

import common_paths  # type: ignore
import ids_ports  # type: ignore
import requests_db  # type: ignore

def _get_service_state(service_name: str) -> str:
    r = subprocess.run(["systemctl", "is-active", service_name], capture_output=True, text=True)
    return (r.stdout or "").strip() if r.returncode == 0 else "inactive"


def _service_and_appdir(term: str, student_id: str, app_name: str, request_kind: str):
    """
    request_kind: 'spring' / 'flask' / 'static'
    """
    app_type = request_kind  # unit名の接頭辞にそのまま使う
    svc = common_paths.service_name(app_type, term, student_id, app_name)
    app_dir = str(common_paths.app_root(term, student_id, app_name))
    return svc, app_dir


def _install_requirements_if_present(app_dir: Path) -> Tuple[bool, str]:
    """
    Flask zip展開後に requirements.txt があれば venv にインストールする。
    return: (ok, message). ok=False の場合 message はエラー詳細。
    """
    req = app_dir / "requirements.txt"
    if not req.exists():
        return True, ""  # requirements 無しはOK扱い（任意）

    pip = app_dir / "venv" / "bin" / "pip"
    if not pip.exists():
        return False, f"venv が見つかりません: {pip}\n（初回デプロイで venv 作成が完了している必要があります）"

    # インストール（タイムアウトは長めに）
    try:
        r = subprocess.run(
            [str(pip), "install", "-r", str(req)],
            capture_output=True,
            text=True,
            timeout=600,
        )
    except subprocess.TimeoutExpired:
        return False, "pip install がタイムアウトしました（600秒）。requirements の内容を見直してください。"
    except Exception as e:
        return False, f"pip install 実行で例外: {e}"

    if r.returncode != 0:
        out = (r.stdout or "").strip()
        err = (r.stderr or "").strip()
        return False, "requirements インストールに失敗しました。\n\n--- stdout ---\n" + out + "\n\n--- stderr ---\n" + err

    return True, ""


def _has_any_file(root: Path) -> bool:
    if not root.exists():
        return False
    return any(p.is_file() for p in root.rglob("*"))


