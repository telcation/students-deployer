# line_notify.py (extracted from students_redeploy.py; no behavior change)
from __future__ import annotations

import json
import urllib.request
import urllib.error

import config  # type: ignore

# LINE notify (optional)
LINE_NOTIFY_ENABLED = bool(getattr(config, "LINE_NOTIFY_ENABLED", False))
LINE_CHANNEL_ACCESS_TOKEN = str(getattr(config, "LINE_CHANNEL_ACCESS_TOKEN", "")).strip()
# 送信先（ユーザーID/グループID/ルームID）をカンマ区切りで複数指定可
LINE_NOTIFY_TO = str(getattr(config, "LINE_NOTIFY_TO", "")).strip()
LINE_NOTIFY_PREFIX = str(getattr(config, "LINE_NOTIFY_PREFIX", "【申請通知】")).strip()

def _line_push(text: str) -> None:
    """LINE Messaging API push. 失敗しても例外は飲み込む（申請処理を止めない）。"""
    if not LINE_NOTIFY_ENABLED:
        return
    if not LINE_CHANNEL_ACCESS_TOKEN or not LINE_NOTIFY_TO:
        return

    tos = [t.strip() for t in LINE_NOTIFY_TO.split(",") if t.strip()]
    if not tos:
        return

    url = "https://api.line.me/v2/bot/message/push"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
    }

    for to in tos:
        body = {
            "to": to,
            "messages": [{"type": "text", "text": text}],
        }
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                # 200以外でも例外にならない場合があるので、念のためログ
                if getattr(resp, "status", 200) >= 300:
                    print(f"[LINE] push failed status={resp.status}", flush=True)
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            print(f"[LINE] HTTPError {e.code}: {e.reason} body={body}", flush=True)

        except Exception as e:
            print(f"[LINE] push exception: {e}", flush=True)



def _notify_new_request(student_id: str, term: str, app_name: str, kind: str, app_desc: str | None,
                        python_version: str | None, java_release: str | None) -> None:
    desc = (app_desc or "").strip()
    lines = [
        f"{LINE_NOTIFY_PREFIX} 新規申請",
        f"受講者ID: {student_id}（{term}）",
        f"種別: {kind}",
        f"アプリ名: {app_name}",
    ]
    if python_version:
        lines.append(f"Python: {python_version}")
    if java_release:
        lines.append(f"Java: {java_release}")
    if desc:
        lines.append(f"概要: {desc}")
    _line_push("\n".join(lines))


def _notify_first_deploy_result(student_id: str, term: str, app_name: str, kind: str,
                                ok: bool, *,
                                app_desc: str = "",
                                python_version: str = "",
                                java_release: str = "",
                                public_url: str | None = None,
                                error: str | None = None) -> None:
    lines = [
        f"{LINE_NOTIFY_PREFIX} ",
        f"受講者ID: {student_id}（{term}）",
        f"種別: {kind}",
        f"アプリ名: {app_name}",
    ]
    if app_desc:
        one = app_desc.replace("\n", " ").strip()
        lines.append(f"概要: {one[:120]}")
    if kind == "flask" and python_version:
        lines.append(f"Python: {python_version}")
    if kind == "spring" and java_release:
        lines.append(f"Java: {java_release}")
    if public_url:
        lines.append(f"URL: {public_url}")
    if error:
        one = (error.replace("\n", " ").strip())[:200]
        lines.append(f"理由: {one}")

    _line_push("\n".join(lines))

