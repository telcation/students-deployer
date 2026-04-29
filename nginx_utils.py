# nginx_utils.py (completed)
from __future__ import annotations

from pathlib import Path
from typing import Callable
import config
import subprocess
import textwrap

NGINX_SNIPPETS_DIR = config.NGINX_SNIPPET_DIR

def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True)

def reload_nginx_with_check(
    flash_func: Callable[[str, str], None] | None = None,
) -> bool:
    """nginx -t で検証してから reload。失敗時は stderr を返す。"""
    r = _run(["sudo", "-n", "nginx", "-t"])
    if r.returncode != 0:
        if flash_func:
            flash_func("nginx 設定テストに失敗しました", "error")
            if r.stderr:
                flash_func(r.stderr.strip(), "error")
        return False
    r2 = _run(["sudo", "-n", "systemctl", "reload", "nginx"])
    if r2.returncode != 0:
        if flash_func:
            flash_func("nginx reload に失敗しました", "error")
            if r2.stderr:
                flash_func(r2.stderr.strip(), "error")
        return False
    return True

def snippet_filename(term: str, student_id: str, app_name: str) -> str:
    # 既存運用に合わせて term_student_app.conf
    return f"{term}_{student_id}_{app_name}.conf"

def snippet_path(term: str, student_id: str, app_name: str) -> Path:
    return NGINX_SNIPPETS_DIR / snippet_filename(term, student_id, app_name)

def render_location_block(
    apptype: str,
    term: str,
    student_id: str,
    app_name: str,
    port: int | None,
    is_streamlit: bool = False,
) -> str:
    apptype = (apptype or "").lower()

    if apptype == "static":
        return textwrap.dedent(f"""\
        # {term} / {student_id} / {app_name} (static)
        location /s/{term}/{student_id}/{app_name}/ {{
            alias /srv/students/{term}/{student_id}/{app_name}/public/;
            index index.html;
            autoindex off;
        }}
        """)

    if apptype == "flask":
        if port is None:
            raise ValueError("flask requires port")

        location_prefix = f"/f/{term}/{student_id}/{app_name}/"
        prefix_no_slash = location_prefix.rstrip("/")

        if is_streamlit:
            return textwrap.dedent(f"""\
            # {term} / {student_id} / {app_name} (streamlit:{port})
            location {location_prefix} {{
                proxy_set_header Host $host;
                proxy_set_header X-Real-IP $remote_addr;
                proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
                proxy_set_header X-Forwarded-Proto $scheme;

                proxy_http_version 1.1;
                proxy_set_header Upgrade $http_upgrade;
                proxy_set_header Connection "upgrade";
                proxy_read_timeout 86400;

                proxy_pass http://127.0.0.1:{port};
            }}
            """)
        else:
            return textwrap.dedent(f"""\
            # {term} / {student_id} / {app_name} (flask:{port})
            location {location_prefix} {{
                proxy_set_header Host $host;
                proxy_set_header X-Real-IP $remote_addr;
                proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
                proxy_set_header X-Forwarded-Proto $scheme;
    
                proxy_set_header X-Forwarded-Prefix {prefix_no_slash};
    
                proxy_http_version 1.1;
                proxy_set_header Upgrade $http_upgrade;
                proxy_set_header Connection "upgrade";
                proxy_read_timeout 86400;
    
                proxy_pass http://127.0.0.1:{port}/;
            }}
            """)

    if apptype == "spring":
        if port is None:
            raise ValueError("spring requires port")

        # Spring は context-path を /b/... にしている想定（従来運用維持）
        location_prefix = f"/b/{term}/{student_id}/{app_name}/"
        return textwrap.dedent(f"""\
        # {term} / {student_id} / {app_name} (spring:{port})
        location {location_prefix} {{
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto $scheme;
            proxy_pass http://127.0.0.1:{port};
        }}
        """)

    raise ValueError(f"unknown apptype: {apptype}")


def write_student_location(
    apptype: str,
    term: str,
    student_id: str,
    app_name: str,
    port: int | None,
    flash_func: Callable[[str, str], None] | None = None,
    is_streamlit: bool = False,
) -> Path:
    """/etc/nginx/students.d に snippet を作成/更新。"""
    NGINX_SNIPPETS_DIR.mkdir(parents=True, exist_ok=True)
    p = snippet_path(term, student_id, app_name)
    content = render_location_block(
        apptype,
        term,
        student_id,
        app_name,
        port,
        is_streamlit=is_streamlit,
    )
    # root で書く必要があるので tee
    r = _run(["sudo", "-n", "tee", str(p)])
    # Can't send content via tee without stdin; do second run with input
    if r.returncode != 0:
        # We'll do via subprocess.run with input
        pass
    r = subprocess.run(["sudo", "-n", "tee", str(p)], input=content, text=True, capture_output=True)
    if r.returncode != 0:
        if flash_func:
            flash_func(f"nginx snippet 作成に失敗: {p}", "error")
            if r.stderr:
                flash_func(r.stderr.strip(), "error")
        raise RuntimeError(r.stderr.strip() if r.stderr else "failed to write snippet")
    # 権限調整（念のため）
    subprocess.run(["sudo", "-n", "chmod", "644", str(p)], capture_output=True, text=True)
    if flash_func:
        flash_func(f"nginx: {p.name} を作成/更新しました", "ok")
    return p

def remove_student_location(term: str, student_id: str, app_name: str, flash_func=None) -> None:
    p = snippet_path(term, student_id, app_name)
    subprocess.run(["sudo", "-n", "rm", "-f", str(p)], capture_output=True, text=True)
    if flash_func:
        flash_func(f"nginx: {p.name} を削除しました", "ok")
