import os
import sqlite3
from flask import Flask, render_template, request, session, redirect, url_for, g
import re
import config
import requests_db
import ids_ports

app = Flask(__name__)
app.secret_key = config.SECRET_KEY
DATABASE = str(config.REQUESTS_DB_PATH)
LAN_BASE_URL = str(getattr(config, "LAN_BASE_URL", "")).rstrip("/")


# Student ID validation helpers
def student_id_exists(student_id: str) -> bool:
    return requests_db.student_exists(DATABASE, student_id)


def validate_student_id(student_id: str):
    if not re.fullmatch(r"\d{6}", student_id or ""):
        return False, "受講者番号は6桁の数字で入力してください。"

    if not student_id.startswith("9"):
        if not student_id_exists(student_id):
            return False, "この受講者番号は登録されていません。"

    return True, None


# --- URL生成ロジック ---
def _is_private_host(host: str) -> bool:
    host = (host or "").split(":")[0]
    return host.startswith(("192.168.", "10.", "172."))


def generate_public_url(row):
    """
    Telcationのルーティング規則に従ってURLを動的に生成する
    規則: /{kind_prefix}/{term}/{student_id}/{app_name}/
    """
    try:
        term = ids_ports.student_id_to_term(row.student_id)
    except Exception:
        return ""

    # デフォルトは外部
    base = config.PUBLIC_BASE_URL

    # LANはIPで開く運用 → host 判定で切替
    if _is_private_host(request.host):
        base = LAN_BASE_URL or "http://192.168.3.200"

    base = (base or "").rstrip("/")

    kind = row.request_kind
    if kind == "spring":
        prefix = "b"
    elif kind == "flask":
        prefix = "f"
    else:
        prefix = "s"

    if not base:
        return ""

    return f"{base}/{prefix}/{term}/{row.student_id}/{row.app_name}/"


# --- Nginxのサブパス (/community) 対応 ---
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


# Nginxのlocation設定に合わせて /community を指定
COMMUNITY_PREFIX = (os.environ.get("COMMUNITY_URL_PREFIX", "/community").rstrip("/")) or "/community"
app.wsgi_app = PrefixMiddleware(app.wsgi_app, prefix=COMMUNITY_PREFIX)


# ---------------------------------------

def get_db():
    db = getattr(g, '_database', None)
    if db is None:
        db = g._database = sqlite3.connect(DATABASE)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON;")
    return db


@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()


def _table_columns(db, table: str) -> set[str]:
    rows = db.execute(f"PRAGMA table_info({table})").fetchall()
    return {r[1] for r in rows}


def _add_column_if_missing(db, table: str, col: str, ddl_type: str) -> None:
    cols = _table_columns(db, table)
    if col in cols:
        return
    db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl_type}")


def init_community_db():
    with app.app_context():
        db = get_db()
        db.execute("""
            CREATE TABLE IF NOT EXISTS comments (
                comment_id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id INTEGER NOT NULL,
                author_id TEXT NOT NULL,
                parent_id INTEGER DEFAULT NULL,
                content TEXT NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (request_id) REFERENCES requests(id),
                FOREIGN KEY (parent_id) REFERENCES comments(comment_id)
            );
        """)
        # 追加列（あってもOK）
        _add_column_if_missing(db, "comments", "updated_at", "DATETIME")
        _add_column_if_missing(db, "comments", "edited", "INTEGER DEFAULT 0")
        db.commit()


def get_comment_tree(request_id):
    db = get_db()
    cursor = db.execute("SELECT * FROM comments WHERE request_id = ? ORDER BY created_at ASC", (request_id,))
    comments = [dict(row) for row in cursor.fetchall()]
    comment_map = {c['comment_id']: {**c, 'replies': []} for c in comments}
    tree = []
    for c in comment_map.values():
        if c['parent_id'] is None:
            tree.append(c)
        elif c['parent_id'] in comment_map:
            comment_map[c['parent_id']]['replies'].append(c)
    return tree


@app.route("/", methods=["GET", "POST"])
def login():
    error = None

    if request.method == "POST":
        student_id = (request.form.get("student_id") or "").strip()

        ok, error = validate_student_id(student_id)
        if ok:
            session["student_id"] = student_id
            return redirect(url_for("index"))

    return render_template("login.html", error=error)


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


@app.route('/index')
def index():
    if 'student_id' not in session: return redirect(url_for('login'))
    my_apps = requests_db.list_by_student(DATABASE, session['student_id'])

    # 自分のアプリ一覧にもURLを付与
    for app_row in my_apps:
        app_row.public_url = generate_public_url(app_row)

    return render_template('index.html', my_apps=my_apps)


@app.route('/search')
def search():
    q = request.args.get('q', '')
    # requests_db.py の search を利用
    results = requests_db.search(DATABASE, status='public', student_id=q)

    # 取得した各行に対して URL を付与
    processed_results = []
    for row in results:
        # RequestRowオブジェクトに動的URLをセット
        row.public_url = generate_public_url(row)
        processed_results.append(row)

    return render_template('search.html', results=processed_results, query=q)


@app.route('/app/<int:request_id>', methods=['GET', 'POST'])
def app_detail(request_id):
    if 'student_id' not in session: return redirect(url_for('login'))
    db = get_db()
    if request.method == 'POST':
        db.execute("INSERT INTO comments (request_id, author_id, parent_id, content) VALUES (?, ?, ?, ?)",
                   (request_id, session['student_id'], request.form.get('parent_id'), request.form.get('content')))
        db.commit()
        return redirect(url_for('app_detail', request_id=request_id))
    # 特定のアプリ情報を取得
    app_info = requests_db.get_by_id(DATABASE, request_id)
    comments = get_comment_tree(request_id)
    return render_template('detail.html', app=app_info, comments=comments)


@app.route('/app/<int:request_id>/overview', methods=['POST'])
def update_overview(request_id):
    if 'student_id' not in session:
        return redirect(url_for('login'))

    desc = request.form.get('app_desc', '')
    ok = requests_db.update_app_desc(
        DATABASE,
        request_id,
        student_id=session['student_id'],
        app_desc=desc,
    )
    # 失敗時は他人のアプリ等。とりあえず詳細へ戻す（必要なら 403 にしてもOK）
    return redirect(url_for('app_detail', request_id=request_id))


@app.route('/comment/<int:comment_id>/edit', methods=['GET', 'POST'])
def edit_comment(comment_id):
    if 'student_id' not in session:
        return redirect(url_for('login'))

    db = get_db()
    c = db.execute("SELECT * FROM comments WHERE comment_id = ?", (comment_id,)).fetchone()
    if c is None:
        return redirect(url_for('index'))

    # 本人チェック
    if str(c["author_id"]) != str(session["student_id"]):
        return redirect(url_for('app_detail', request_id=int(c["request_id"])))

    if request.method == 'POST':
        new_content = (request.form.get('content') or '').strip()
        if not new_content:
            return redirect(url_for('app_detail', request_id=int(c["request_id"])))

        db.execute(
            "UPDATE comments SET content = ?, updated_at = CURRENT_TIMESTAMP, edited = 1 WHERE comment_id = ?",
            (new_content, comment_id),
        )
        db.commit()
        return redirect(url_for('app_detail', request_id=int(c["request_id"])))

    # GET は編集画面（テンプレを用意）
    return render_template('comment_edit.html', comment=dict(c))


@app.route('/comment/<int:comment_id>/delete', methods=['POST'])
def delete_comment(comment_id):
    if 'student_id' not in session:
        return redirect(url_for('login'))

    db = get_db()
    c = db.execute("SELECT * FROM comments WHERE comment_id = ?", (comment_id,)).fetchone()
    if c is None:
        return redirect(url_for('index'))

    # 本人チェック
    if str(c["author_id"]) != str(session["student_id"]):
        return redirect(url_for('app_detail', request_id=int(c["request_id"])))

    req_id = int(c["request_id"])

    # 返信がある場合の扱い：消すとツリーが崩れるので「本文だけ削除扱い」にする（安全）
    has_replies = db.execute(
        "SELECT 1 FROM comments WHERE parent_id = ? LIMIT 1",
        (comment_id,)
    ).fetchone() is not None

    if has_replies:
        db.execute(
            "UPDATE comments "
            "SET content = '[削除されました]', updated_at = CURRENT_TIMESTAMP, edited = 1 "
            "WHERE comment_id = ?",
            (comment_id,)
        )
    else:
        # 返信が無いコメントは物理削除でOK
        db.execute("DELETE FROM comments WHERE comment_id = ?", (comment_id,))

    db.commit()
    return redirect(url_for('app_detail', request_id=req_id))


if __name__ == '__main__':
    init_community_db()
    port = int(os.environ.get("PORT", 5002))
    app.run(host='0.0.0.0', port=port)