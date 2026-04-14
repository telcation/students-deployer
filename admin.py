from __future__ import annotations

import os
from flask import Flask, request, redirect, url_for, render_template_string, abort

import config
import requests_db  # 同じフォルダに requests_db.py がある想定

# ★DBは必ず config 経由（環境変数 REQUESTS_DB_PATH で切替可能）
DB_PATH = str(getattr(config, "REQUESTS_DB_PATH"))

app = Flask(__name__)

# --- Nginx のサブパス (/admin) 対応 ---
class PrefixMiddleware(object):
    def __init__(self, app, prefix=''):
        self.app = app
        self.prefix = prefix
    def __call__(self, environ, start_response):
        if environ['PATH_INFO'].startswith(self.prefix):
            environ['PATH_INFO'] = environ['PATH_INFO'][len(self.prefix):]
            environ['SCRIPT_NAME'] = self.prefix
            return self.app(environ, start_response)
        else:
            return self.app(environ, start_response)

ADMIN_PREFIX = '/admin'
app.wsgi_app = PrefixMiddleware(app.wsgi_app, prefix=ADMIN_PREFIX)
# ---------------------------------------
app.config["SECRET_KEY"] = os.environ.get("ADMIN_SECRET_KEY", "dev")  # 任意（dev固定でも可）


LIST_HTML = """
<!doctype html>
<meta charset="utf-8">
<title>Requests CRUD</title>
<style>
  body{font-family:system-ui, sans-serif; margin:16px;}
  table{border-collapse:collapse; width:100%;}
  th,td{border:1px solid #ccc; padding:6px; vertical-align:top; font-size:14px;}
  th{background:#f5f5f5;}
  .row{display:flex; gap:12px; flex-wrap:wrap; margin:12px 0;}
  .btn{display:inline-block; padding:6px 10px; border:1px solid #333; text-decoration:none; border-radius:6px;}
  .danger{border-color:#b00; color:#b00;}
  input[type=text], input[type=number], textarea{width:360px; max-width:100%;}
  textarea{height:90px;}
  .muted{color:#666;}
</style>

<h1>Requests CRUD</h1>

<div class="row">
  <a class="btn" href="{{ url_for('new_row') }}">新規登録</a>
  <a class="btn" href="{{ url_for('teachers_index') }}">Teachers CRUD</a>
  <a class="btn" href="{{ url_for('line_settings_get') }}">LINE通知設定</a>
  <span class="muted">DB: {{ db_path }}</span>
</div>

<table>
  <thead>
    <tr>
      <th>ID</th>
      <th>student_id</th>
      <th>app_name</th>
      <th>request_kind</th>
      <th>status</th>
      <th>created_at</th>
      <th>port</th>
      <th>操作</th>
    </tr>
  </thead>
  <tbody>
    {% for r in rows %}
    <tr>
      <td>{{ r.id }}</td>
      <td>{{ r.student_id }}</td>
      <td>{{ r.app_name }}</td>
      <td>{{ r.request_kind }}</td>
      <td>{{ r.status }}</td>
      <td>{{ r.created_at }}</td>
      <td>{{ r.port if r.port is not none else "" }}</td>
      <td>
        <a class="btn" href="{{ url_for('edit_row', request_id=r.id) }}">編集</a>
        <form style="display:inline" method="post" action="{{ url_for('delete_row', request_id=r.id) }}"
              onsubmit="return confirm('ID={{ r.id }} を削除しますか？');">
          <button class="btn danger" type="submit">削除</button>
        </form>
      </td>
    </tr>
    {% endfor %}
  </tbody>
</table>
"""

FORM_HTML = """
<!doctype html>
<meta charset="utf-8">
<title>{{ title }}</title>
<style>
  body{font-family:system-ui, sans-serif; margin:16px;}
  label{display:block; margin-top:10px; font-weight:600;}
  input[type=text], input[type=number], textarea{width:520px; max-width:100%; padding:6px;}
  textarea{height:120px;}
  .row{display:flex; gap:10px; margin-top:14px; align-items:center;}
  .btn{padding:8px 12px; border:1px solid #333; border-radius:8px; background:#fff; cursor:pointer; text-decoration:none; color:#000;}
  .muted{color:#666;}
</style>

<h1>{{ title }}</h1>
<p class="muted">※公開しない前提のため、認証・入力検証は最低限です。</p>

<form method="post">
  <label>student_id</label>
  <input type="text" name="student_id" value="{{ v('student_id') }}" required>

  <label>app_name</label>
  <input type="text" name="app_name" value="{{ v('app_name') }}" required>

  <label>request_kind</label>
  <input type="text" name="request_kind" value="{{ v('request_kind') }}" required>

  <label>status</label>
  <input type="text" name="status" value="{{ v('status') }}" required>

  <label>created_at（例: 2026-01-16 12:34:56）</label>
  <input type="text" name="created_at" value="{{ v('created_at') }}" required>

  <label>app_desc</label>
  <textarea name="app_desc">{{ v('app_desc') }}</textarea>

  <label>python_version</label>
  <input type="text" name="python_version" value="{{ v('python_version') }}">

  <label>java_release</label>
  <input type="text" name="java_release" value="{{ v('java_release') }}">

  <label>java_build</label>
  <input type="text" name="java_build" value="{{ v('java_build') }}">

  <label>port（空欄可）</label>
  <input type="number" name="port" value="{{ v('port') }}">

  <label>last_upload</label>
  <input type="text" name="last_upload" value="{{ v('last_upload') }}">

  <label>last_upload_at（例: 2026-01-16 12:34:56）</label>
  <input type="text" name="last_upload_at" value="{{ v('last_upload_at') }}">

  <div class="row">
    <button class="btn" type="submit">保存</button>
    <a class="btn" href="{{ url_for('index') }}">戻る</a>
    {% if request_id is not none %}
      <span class="muted">ID={{ request_id }}</span>
    {% endif %}
  </div>
</form>
"""

TEACHERS_LIST_HTML = """
<!doctype html>
<meta charset="utf-8">
<title>Teachers CRUD</title>
<style>
  body{font-family:system-ui, sans-serif; margin:16px;}
  table{border-collapse:collapse; width:100%;}
  th,td{border:1px solid #ccc; padding:6px; vertical-align:top; font-size:14px;}
  th{background:#f5f5f5;}
  .row{display:flex; gap:12px; flex-wrap:wrap; margin:12px 0;}
  .btn{display:inline-block; padding:6px 10px; border:1px solid #333; text-decoration:none; border-radius:6px;}
  .danger{border-color:#b00; color:#b00;}
  input[type=text], input[type=password]{width:360px; max-width:100%;}
  .muted{color:#666;}
</style>

<h1>Teachers CRUD</h1>

<div class="row">
  <a class="btn" href="{{ url_for('teachers_new') }}">講師追加</a>
  <a class="btn" href="{{ url_for('index') }}">Requests CRUD</a>
  <span class="muted">DB: {{ db_path }}</span>
</div>

<table>
  <thead>
    <tr>
      <th>teacher_id</th>
      <th>created_at</th>
      <th>操作</th>
    </tr>
  </thead>
  <tbody>
    {% for t in teachers %}
    <tr>
      <td>{{ t.teacher_id }}</td>
      <td>{{ t.created_at }}</td>
      <td>
        <a class="btn" href="{{ url_for('teachers_edit', teacher_id=t.teacher_id) }}">パスワード再設定</a>
        <form style="display:inline" method="post" action="{{ url_for('teachers_delete', teacher_id=t.teacher_id) }}"
              onsubmit="return confirm('teacher_id={{ t.teacher_id }} を削除しますか？');">
          <button class="btn danger" type="submit">削除</button>
        </form>
      </td>
    </tr>
    {% endfor %}
  </tbody>
</table>
"""

TEACHERS_FORM_HTML = """
<!doctype html>
<meta charset="utf-8">
<title>{{ title }}</title>
<style>
  body{font-family:system-ui, sans-serif; margin:16px;}
  label{display:block; margin-top:10px; font-weight:600;}
  input[type=text], input[type=password]{width:520px; max-width:100%; padding:6px;}
  .row{display:flex; gap:10px; margin-top:14px; align-items:center;}
  .btn{padding:8px 12px; border:1px solid #333; border-radius:8px; background:#fff; cursor:pointer; text-decoration:none; color:#000;}
  .muted{color:#666;}
</style>

<h1>{{ title }}</h1>
<p class="muted">※パスワードは表示できないため、編集は「再設定」のみです。</p>

<form method="post">
  <label>teacher_id</label>
  {% if readonly %}
    <input type="text" name="teacher_id" value="{{ teacher_id }}" readonly>
  {% else %}
    <input type="text" name="teacher_id" value="{{ teacher_id }}" required>
  {% endif %}

  <label>password（平文入力 → DBにはハッシュで保存）</label>
  <input type="password" name="password" value="" required>

  <div class="row">
    <button class="btn" type="submit">保存</button>
    <a class="btn" href="{{ url_for('teachers_index') }}">戻る</a>
  </div>
</form>
"""

LINE_SETTINGS_HTML = """
<!doctype html>
<meta charset="utf-8">
<title>LINE通知設定</title>
<style>
  body{font-family:system-ui, sans-serif; margin:16px;}
  .row{display:flex; gap:12px; flex-wrap:wrap; margin:12px 0; align-items:center;}
  .card{border:1px solid #ddd; padding:12px; border-radius:10px; max-width:680px;}
  label{display:block; margin:10px 0; font-size:14px;}
  .btn{display:inline-block; padding:6px 10px; border:1px solid #333; text-decoration:none; border-radius:6px; background:#fff; cursor:pointer;}
  .muted{color:#666;}
</style>

<h1>LINE通知設定（グローバル）</h1>

<div class="row">
  <a class="btn" href="{{ url_for('index') }}">Requests CRUDへ</a>
  <span class="muted">DB: {{ db_path }}</span>
</div>

<div class="card">
  <p class="muted">通知を受け取るタイミングを選択します（デフォルト: 公開/再公開 ON、 新規申請/停止 OFF）。</p>

  <form method="post" action="{{ url_for('line_settings_post') }}">
    <label><input type="checkbox" name="on_new" value="1" {{ "checked" if s.on_new else "" }}> 新規申請</label>
    <label><input type="checkbox" name="on_publish" value="1" {{ "checked" if s.on_publish else "" }}> 公開</label>
    <label><input type="checkbox" name="on_stop" value="1" {{ "checked" if s.on_stop else "" }}> 停止</label>
    <label><input type="checkbox" name="on_republish" value="1" {{ "checked" if s.on_republish else "" }}> 再公開</label>

    <div class="row">
      <button class="btn" type="submit">保存</button>
    </div>
  </form>
</div>
"""

def _str_or_none(s: str) -> str | None:
    s = (s or "").strip()
    return s if s else None

def _int_or_none(s: str) -> int | None:
    s = (s or "").strip()
    if not s:
        return None
    return int(s)

@app.get("/")
def index():
    requests_db.init_db(DB_PATH)
    rows = requests_db.list_all(DB_PATH)
    return render_template_string(LIST_HTML, rows=rows, db_path=DB_PATH)

@app.get("/new")
def new_row():
    # created_at は helper を使って今時刻で埋めるのが楽
    requests_db.init_db(DB_PATH)
    default = {
        "student_id": "",
        "app_name": "",
        "request_kind": "flask",
        "status": "received",
        "created_at": requests_db._now(),  # 既存関数を利用
        "app_desc": "",
        "python_version": "",
        "java_release": "",
        "java_build": "",
        "port": "",
        "last_upload": "",
        "last_upload_at": "",
    }
    return render_template_string(
        FORM_HTML,
        title="新規登録",
        request_id=None,
        v=lambda k: default.get(k, ""),
    )

@app.post("/new")
def new_row_post():
    requests_db.init_db(DB_PATH)

    # add_request では created_at を内部生成するので、フォームのcreated_atは反映されません。
    # 「created_atも任意編集したい」場合は、登録後に update_request_all_fields で上書きします。
    rid = requests_db.add_request(
        DB_PATH,
        student_id=request.form["student_id"].strip(),
        app_name=request.form["app_name"].strip(),
        request_kind=request.form["request_kind"].strip(),
        status=request.form["status"].strip(),
        python_version=_str_or_none(request.form.get("python_version", "")),
        java_release=_str_or_none(request.form.get("java_release", "")),
        java_build=_str_or_none(request.form.get("java_build", "")),
        app_desc=_str_or_none(request.form.get("app_desc", "")),
    )

    # created_at / port / last_upload / last_upload_at も含め、全項目をフォーム値で確定
    ok = requests_db.update_request_all_fields(
        DB_PATH,
        rid,
        student_id=request.form["student_id"].strip(),
        app_name=request.form["app_name"].strip(),
        request_kind=request.form["request_kind"].strip(),
        status=request.form["status"].strip(),
        created_at=request.form["created_at"].strip(),
        app_desc=_str_or_none(request.form.get("app_desc", "")),
        python_version=_str_or_none(request.form.get("python_version", "")),
        java_release=_str_or_none(request.form.get("java_release", "")),
        java_build=_str_or_none(request.form.get("java_build", "")),
        port=_int_or_none(request.form.get("port", "")),
        last_upload=_str_or_none(request.form.get("last_upload", "")),
        last_upload_at=_str_or_none(request.form.get("last_upload_at", "")),
    )
    if not ok:
        abort(500, "登録後の更新に失敗しました。")

    return redirect(url_for("index"))

# --- Teachers CRUD ---

@app.get("/teachers")
def teachers_index():
    requests_db.init_db(DB_PATH)
    teachers = requests_db.list_teachers(DB_PATH)
    # dict -> attribute風アクセスに合わせる
    class T:  # 簡易
        def __init__(self, d): self.__dict__.update(d)
    teachers = [T(t) for t in teachers]
    return render_template_string(TEACHERS_LIST_HTML, teachers=teachers, db_path=DB_PATH)

@app.get("/teachers/new")
def teachers_new():
    requests_db.init_db(DB_PATH)
    return render_template_string(
        TEACHERS_FORM_HTML,
        title="講師追加",
        teacher_id="",
        readonly=False,
    )

@app.post("/teachers/new")
def teachers_new_post():
    requests_db.init_db(DB_PATH)
    teacher_id = (request.form.get("teacher_id") or "").strip()
    password = request.form.get("password") or ""
    if not teacher_id or not password:
        abort(400, "teacher_id と password は必須です。")
    requests_db.upsert_teacher(DB_PATH, teacher_id=teacher_id, password=password)
    return redirect(url_for("teachers_index"))

@app.get("/teachers/edit/<teacher_id>")
def teachers_edit(teacher_id: str):
    requests_db.init_db(DB_PATH)
    # 存在確認（なければ404）
    exists = any(t["teacher_id"] == teacher_id for t in requests_db.list_teachers(DB_PATH))
    if not exists:
        abort(404)
    return render_template_string(
        TEACHERS_FORM_HTML,
        title="パスワード再設定",
        teacher_id=teacher_id,
        readonly=True,
    )

@app.post("/teachers/edit/<teacher_id>")
def teachers_edit_post(teacher_id: str):
    requests_db.init_db(DB_PATH)
    password = request.form.get("password") or ""
    if not password:
        abort(400, "password は必須です。")
    requests_db.upsert_teacher(DB_PATH, teacher_id=teacher_id, password=password)
    return redirect(url_for("teachers_index"))

@app.post("/teachers/delete/<teacher_id>")
def teachers_delete(teacher_id: str):
    requests_db.init_db(DB_PATH)
    requests_db.delete_teacher(DB_PATH, teacher_id=teacher_id)
    return redirect(url_for("teachers_index"))

@app.get("/line-settings")
def line_settings_get():
    requests_db.init_db(DB_PATH)
    s = requests_db.get_line_notify_settings(DB_PATH)  # dict
    s = type("S", (), s)  # テンプレで s.on_publish みたいに扱うため
    return render_template_string(LINE_SETTINGS_HTML, s=s, db_path=DB_PATH)

@app.post("/line-settings")
def line_settings_post():
    requests_db.init_db(DB_PATH)

    def on(name: str) -> int:
        return 1 if request.form.get(name) == "1" else 0

    requests_db.update_line_notify_settings(
        DB_PATH,
        on_new=on("on_new"),
        on_publish=on("on_publish"),
        on_stop=on("on_stop"),
        on_republish=on("on_republish"),
    )
    return redirect(url_for("line_settings_get"))


# --- Requests CRUD ---

@app.get("/edit/<int:request_id>")
def edit_row(request_id: int):
    requests_db.init_db(DB_PATH)
    row = requests_db.get_by_id(DB_PATH, request_id)
    if row is None:
        abort(404)

    def v(key: str):
        val = getattr(row, key)
        return "" if val is None else str(val)

    return render_template_string(
        FORM_HTML,
        title="編集",
        request_id=request_id,
        v=v,
    )

@app.post("/edit/<int:request_id>")
def edit_row_post(request_id: int):
    requests_db.init_db(DB_PATH)
    row = requests_db.get_by_id(DB_PATH, request_id)
    if row is None:
        abort(404)

    ok = requests_db.update_request_all_fields(
        DB_PATH,
        request_id,
        student_id=request.form["student_id"].strip(),
        app_name=request.form["app_name"].strip(),
        request_kind=request.form["request_kind"].strip(),
        status=request.form["status"].strip(),
        created_at=request.form["created_at"].strip(),
        app_desc=_str_or_none(request.form.get("app_desc", "")),
        python_version=_str_or_none(request.form.get("python_version", "")),
        java_release=_str_or_none(request.form.get("java_release", "")),
        java_build=_str_or_none(request.form.get("java_build", "")),
        port=_int_or_none(request.form.get("port", "")),
        last_upload=_str_or_none(request.form.get("last_upload", "")),
        last_upload_at=_str_or_none(request.form.get("last_upload_at", "")),
    )
    if not ok:
        abort(500, "更新に失敗しました。")

    return redirect(url_for("index"))

@app.post("/delete/<int:request_id>")
def delete_row(request_id: int):
    requests_db.init_db(DB_PATH)
    requests_db.delete_request(DB_PATH, request_id)
    return redirect(url_for("index"))

if __name__ == "__main__":
    # 公開しない前提なら 127.0.0.1 でOK（同一PC内だけ）
    app.run(host="127.0.0.1", port=5003, debug=True)

