# deploy_report.py
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

BASE_DIR = Path(__file__).resolve().parent
REPORT_DIR = BASE_DIR / "deploy_check_reports"
REPORT_DIR.mkdir(parents=True, exist_ok=True)


def _report_path(student_id: str, request_id: int) -> Path:
    safe_sid = re.sub(r"[^0-9]", "_", student_id or "unknown")
    d = REPORT_DIR / safe_sid
    d.mkdir(parents=True, exist_ok=True)
    return d / f"deploy_check_{request_id}.html"


def _delete_report_if_exists(student_id: str, request_id: int) -> None:
    try:
        p = _report_path(student_id, request_id)
        if p.exists():
            p.unlink()
    except Exception:
        pass


# ============================================================
# Structured Issue
# ============================================================

@dataclass(frozen=True)
class Issue:
    file: str
    line: Optional[int]
    problem: str
    fix: str
    snippet: Optional[str] = None
    severity: str = "NG"
    rule_id: Optional[str] = None


def _issue_from_text(msg: str, *, severity: str = "NG", fix: str = "") -> Issue:
    raw = (msg or "").strip()
    m = re.match(r'^\[(NG|WARN|INFO)\]\s*(.*)$', raw)
    if m:
        severity = m.group(1)
        raw = (m.group(2) or "").strip()

    if not fix:
        fix = {
            "NG":   "エラーを修正してから再アップロードしてください。",
            "WARN": "内容を確認してください。問題なければそのまま提出できます。",
            "INFO": "参考情報です。対応は任意です。",
        }.get(severity, "指摘内容を確認してください。")

    return Issue(
        file="",
        line=None,
        problem=raw,
        fix=fix,
        snippet=None,
        severity=severity,
        rule_id=None,
    )


def _dedupe_issues(issues: List[Issue]) -> List[Issue]:
    seen: set = set()
    out: List[Issue] = []
    for it in issues:
        key = (it.file, it.line, it.problem, it.fix, it.snippet, it.severity, it.rule_id)
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


def issues_to_items(issues: List[Issue]) -> List[str]:
    """後方互換用。"""
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
        out.append("")
    return out


# ============================================================
# HTML Report
# ============================================================

def _severity_meta(severity: str) -> dict:
    return {
        "NG":   {"label": "NG（修正必須）",  "color": "#c0392b", "bg": "#fff5f5", "border": "#e74c3c", "icon": "&#x2715;"},
        "WARN": {"label": "WARN（要確認）",  "color": "#b7770d", "bg": "#fffbf0", "border": "#f0a500", "icon": "&#x26A0;"},
        "INFO": {"label": "INFO（参考情報）", "color": "#1a5276", "bg": "#f0f8ff", "border": "#2e86c1", "icon": "&#x2139;"},
    }.get(severity, {"label": severity, "color": "#333", "bg": "#f9f9f9", "border": "#ccc", "icon": "&bull;"})


def _esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _render_section(section_issues: List[Issue], severity: str) -> str:
    if not section_issues:
        return ""
    meta = _severity_meta(severity)
    cards_html = ""
    for idx, issue in enumerate(section_issues, 1):
        file_hint = (
            f'<div class="card-file">{_esc(issue.file)}</div>'
            if issue.file and issue.file not in ("", "(general)") else ""
        )
        snippet_html = (
            f'<div class="card-snippet"><code>{_esc(issue.snippet)}</code></div>'
            if issue.snippet else ""
        )
        cards_html += f"""
    <div class="card" style="border-left-color:{meta['border']};background:{meta['bg']};">
      <div class="card-header" style="color:{meta['color']};">
        <span class="card-icon">{meta['icon']}</span>
        <span class="card-num">#{idx}</span>
      </div>
      {file_hint}
      <div class="card-problem">{_esc(issue.problem)}</div>
      <div class="card-fix">
        <span class="fix-label">対処方法</span>
        {_esc(issue.fix)}
      </div>
      {snippet_html}
    </div>"""

    return f"""
  <section class="section">
    <h2 class="section-title" style="color:{meta['color']};border-bottom-color:{meta['border']};">
      {meta['icon']}&nbsp;{meta['label']} &mdash; {len(section_issues)}件
    </h2>
    {cards_html}
  </section>"""


def _write_html_report(html_path: Path, title: str, issues: List[Issue]) -> None:
    html_path.parent.mkdir(parents=True, exist_ok=True)

    ng_issues   = [i for i in issues if i.severity == "NG"]
    warn_issues = [i for i in issues if i.severity == "WARN"]
    info_issues = [i for i in issues if i.severity == "INFO"]
    total       = len(issues)

    badges = ""
    if ng_issues:
        badges += f'<span class="badge badge-ng">NG {len(ng_issues)}件</span>'
    if warn_issues:
        badges += f'<span class="badge badge-warn">WARN {len(warn_issues)}件</span>'
    if info_issues:
        badges += f'<span class="badge badge-info">INFO {len(info_issues)}件</span>'
    if not badges:
        badges = '<span style="color:#999;">指摘なし</span>'

    if ng_issues:
        overall_cls  = "overall-ng"
        overall_text = f"デプロイは停止されました。NG {len(ng_issues)}件を修正して再アップロードしてください。"
    elif warn_issues:
        overall_cls  = "overall-warn"
        overall_text = f"デプロイしました。WARN {len(warn_issues)}件の内容を確認することを推奨します。"
    else:
        overall_cls  = "overall-ok"
        overall_text = "問題は見つかりませんでした。デプロイしました。"

    sections_html = (
        _render_section(ng_issues, "NG")
        + _render_section(warn_issues, "WARN")
        + _render_section(info_issues, "INFO")
    )

    body_content = (
        '<div class="no-issues">&#x2713; 問題は見つかりませんでした</div>'
        if not issues else sections_html
    )

    css = """
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: "Hiragino Sans", "Meiryo", "Noto Sans JP", sans-serif;
      font-size: 14px; line-height: 1.7; color: #1a1a2e;
      background: #f4f6fb; padding: 24px 16px 48px;
    }
    .page { max-width: 820px; margin: 0 auto; }

    .report-header {
      background: #1a1a2e; color: #fff;
      border-radius: 12px 12px 0 0; padding: 24px 28px 20px;
    }
    .report-header h1 { font-size: 18px; font-weight: 700; margin-bottom: 6px; }
    .report-header .subtitle { font-size: 12px; color: #a0aec0; }

    .summary-bar {
      background: #fff; border: 1px solid #e2e8f0; border-top: none;
      padding: 14px 28px; display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
    }
    .badge {
      display: inline-block; padding: 4px 12px; border-radius: 999px;
      font-size: 13px; font-weight: 700;
    }
    .badge-ng   { background:#fff0f0; color:#c0392b; border:1.5px solid #e74c3c; }
    .badge-warn { background:#fffbf0; color:#b7770d; border:1.5px solid #f0a500; }
    .badge-info { background:#f0f8ff; color:#1a5276; border:1.5px solid #2e86c1; }

    .overall {
      border-radius: 0 0 12px 12px; padding: 13px 28px;
      font-size: 14px; font-weight: 700; margin-bottom: 28px;
    }
    .overall-ng   { background: #c0392b; color: #fff; }
    .overall-warn { background: #e67e22; color: #fff; }
    .overall-ok   { background: #27ae60; color: #fff; }

    .section { margin-bottom: 24px; }
    .section-title {
      font-size: 15px; font-weight: 700;
      padding-bottom: 8px; border-bottom: 2px solid; margin-bottom: 14px;
    }

    .card {
      background: #fff; border-left: 5px solid;
      border-radius: 0 10px 10px 0; padding: 16px 20px;
      margin-bottom: 12px; box-shadow: 0 2px 6px rgba(0,0,0,0.05);
    }
    .card-header {
      display: flex; align-items: center; gap: 8px;
      font-weight: 700; font-size: 13px; margin-bottom: 10px;
    }
    .card-icon { font-size: 16px; }
    .card-num  { opacity: 0.55; }
    .card-file {
      font-size: 11px; font-family: monospace; color: #666;
      margin-bottom: 6px; word-break: break-all;
    }
    .card-problem {
      font-size: 14px; font-weight: 600; color: #1a1a2e;
      margin-bottom: 12px; word-break: break-word;
    }
    .card-fix {
      font-size: 13px; color: #2d3748; background: #f7fafc;
      border-radius: 6px; padding: 10px 14px; word-break: break-word;
    }
    .fix-label {
      display: block; font-size: 11px; font-weight: 700;
      color: #718096; text-transform: uppercase;
      letter-spacing: 0.05em; margin-bottom: 4px;
    }
    .card-snippet { margin-top: 10px; font-size: 12px; }
    .card-snippet code {
      display: block; background: #2d3748; color: #e2e8f0;
      padding: 8px 12px; border-radius: 6px;
      word-break: break-all; white-space: pre-wrap;
    }
    .no-issues {
      text-align: center; padding: 48px 24px;
      color: #27ae60; font-size: 16px; font-weight: 700;
      background: #fff; border-radius: 12px;
    }
"""

    html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{_esc(title)}</title>
  <style>{css}</style>
</head>
<body>
  <div class="page">
    <div class="report-header">
      <h1>デプロイ前チェックレポート</h1>
      <div class="subtitle">{_esc(title)}</div>
    </div>
    <div class="summary-bar">
      {badges}
      <span style="margin-left:auto;font-size:12px;color:#718096;">合計 {total}件</span>
    </div>
    <div class="overall {overall_cls}">{_esc(overall_text)}</div>
    {body_content}
  </div>
</body>
</html>"""

    html_path.write_text(html, encoding="utf-8")


def _write_pdf_report(html_path: Path, title: str, items: List[str]) -> None:
    """旧インターフェース互換ラッパー（PDF→HTML）。"""
    issues: List[Issue] = []
    i = 0
    while i < len(items):
        line = (items[i] or "").strip()
        if not line:
            i += 1
            continue
        m = re.match(r'^\[(NG|WARN|INFO)\]\s*(.*)', line)
        if m:
            severity, problem = m.group(1), m.group(2).strip()
            fix = ""
            if i + 1 < len(items):
                next_line = (items[i + 1] or "").strip()
                if next_line.startswith("改善策:"):
                    fix = next_line[len("改善策:"):].strip()
                    i += 1
            issues.append(Issue(file="", line=None, problem=problem, fix=fix, severity=severity))
        i += 1
    _write_html_report(html_path, title, issues)
