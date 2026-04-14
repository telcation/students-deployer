# deploy_report.py (extracted from students_redeploy.py; no behavior change)
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen import canvas

# Use module-local BASE_DIR to avoid relying on caller's globals
BASE_DIR = Path(__file__).resolve().parent
REPORT_DIR = BASE_DIR / "deploy_check_reports"
REPORT_DIR.mkdir(parents=True, exist_ok=True)

def _report_path(student_id: str, request_id: int) -> Path:
    safe_sid = re.sub(r"[^0-9]", "_", student_id or "unknown")
    d = REPORT_DIR / safe_sid
    d.mkdir(parents=True, exist_ok=True)
    return d / f"deploy_check_{request_id}.pdf"


def _delete_report_if_exists(student_id: str, request_id: int) -> None:
    """古いチェックレポートを消す（再デプロイ開始時に呼ぶ）"""
    try:
        pdf = _report_path(student_id, request_id)
        if pdf.exists():
            pdf.unlink()
    except Exception:
        # レポート削除失敗で本処理を止めない
        pass


def _write_pdf_report(pdf_path: Path, title: str, items: List[str]) -> None:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfgen import canvas

    pdf_path.parent.mkdir(parents=True, exist_ok=True)

    # Japanese font (CID)
    try:
        pdfmetrics.registerFont(UnicodeCIDFont("HeiseiKakuGo-W5"))
        font_name = "HeiseiKakuGo-W5"
    except Exception:
        font_name = "Helvetica"

    c = canvas.Canvas(str(pdf_path), pagesize=A4)
    w, h = A4

    left = 36
    right = 36
    top = 48
    bottom = 60

    y = h - top
    c.setFont(font_name, 16)
    c.drawString(left, y, title)
    y -= 28

    font_size = 11
    leading = 16
    c.setFont(font_name, font_size)

    bullet = "• "
    bullet_w = c.stringWidth(bullet, font_name, font_size)

    text_x = left + bullet_w
    max_width = w - right - text_x  # ★ bullet分を差し引いた本文幅

    def _wrap_by_width(text: str) -> List[str]:
        text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
        out: List[str] = []
        for para in text.split("\n"):
            para = para.strip()
            if not para:
                continue

            cur = ""
            for ch in para:
                trial = cur + ch
                if c.stringWidth(trial, font_name, font_size) <= max_width:
                    cur = trial
                else:
                    if cur:
                        out.append(cur)
                        cur = ch
                    else:
                        out.append(ch)
                        cur = ""
            if cur:
                out.append(cur)
        return out or [""]

    for s in items:
        wrapped = _wrap_by_width(s)

        for i, line in enumerate(wrapped):
            if y < bottom:
                c.showPage()
                c.setFont(font_name, font_size)
                y = h - top

            if i == 0:
                # 1行目だけ「•」
                c.drawString(left, y, bullet)
                c.drawString(text_x, y, line)
            else:
                # 継続行はインデントのみ（「•」なし）
                c.drawString(text_x, y, line)

            y -= leading

    c.save()


def _wrap_text(s: str, max_chars: int = 80) -> List[str]:
    s = (s or "").replace("\r\n", "\n").replace("\r", "\n")
    out: List[str] = []
    for para in s.split("\n"):
        para = para.strip()
        if not para:
            continue
        while len(para) > max_chars:
            out.append(para[:max_chars])
            para = para[max_chars:]
        out.append(para)
    return out or [""]


# ============================================================
# Deploy-check core
# ============================================================
# ------------------------------------------------------------
# Structured Issue (minimal-diff extension)
# ------------------------------------------------------------
@dataclass(frozen=True)
class Issue:
    file: str
    line: Optional[int]
    problem: str
    fix: str
    snippet: Optional[str] = None
    severity: str = "NG"          # NG / WARN / INFO
    rule_id: Optional[str] = None # optional rule identifier


def _issue_from_text(msg: str, *, severity: str = "NG") -> Issue:
    # Allow message to override severity via prefix tags like "[NG]" "[WARN]" "[INFO]"
    raw = (msg or "").strip()
    m = re.match(r'^\[(NG|WARN|INFO)\]\s*(.*)$', raw)
    if m:
        severity = m.group(1)
        raw = (m.group(2) or "").strip()
    return Issue(
        file="(general)",
        line=None,
        problem=raw,
        fix="指摘内容に従って修正してください。",
        snippet=None,
        severity=severity,
        rule_id=None,
    )



def _dedupe_issues(issues: List[Issue]) -> List[Issue]:
    seen = set()
    out: List[Issue] = []
    for it in issues:
        key = (it.file, it.line, it.problem, it.fix, it.snippet, it.severity, it.rule_id)
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


def issues_to_items(issues: List[Issue]) -> List[str]:
    # PDF側は List[str] を前提にしているため、ここで最終テキストへ落とす（フロー維持）
    out: List[str] = []
    for it in issues:
        loc = it.file
        if it.line is not None:
            loc += f":{it.line}"
        head = f"[{it.severity}] {loc}"
        out.append(head)
        out.append(f"問題点: {it.problem}")
        out.append(f"改善策: {it.fix}")
        if it.snippet:
            out.append(f"該当: {it.snippet}")
        out.append("")  # 区切り空行
    return out

