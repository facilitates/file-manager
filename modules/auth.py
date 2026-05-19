"""认证模块：注册、登录、退出"""
import bcrypt
import pymysql
from functools import wraps
from flask import Blueprint, request, jsonify, session

from .db import get_db

auth_bp = Blueprint('auth', __name__)

# 每人一个公开分享文件夹
SHARED_FOLDER_NAME = "public"


def _ensure_shared_folder(uid: int):
    """确保用户拥有「🌐 公开分享」文件夹"""
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "INSERT IGNORE INTO folders (name, owner_id) VALUES (%s, %s)",
        (SHARED_FOLDER_NAME, uid),
    )
    conn.commit()
    cur.close()
    conn.close()


def hash_password(pw: str) -> str:
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()


def check_password(pw: str, hashed: str) -> bool:
    return bcrypt.checkpw(pw.encode(), hashed.encode())


def login_required(f):
    """装饰器：要求登录"""
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return jsonify({"error": "login required"}), 401
        return f(*args, **kwargs)
    return decorated


def get_current_user():
    if 'user_id' not in session:
        return None
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, username FROM users WHERE id = %s", (session['user_id'],))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return {"id": row[0], "username": row[1]} if row else None


def get_all_users():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, username FROM users ORDER BY username")
    users = [{"id": r[0], "username": r[1]} for r in cur.fetchall()]
    cur.close()
    conn.close()
    return users


# ---- 路由 ----

@auth_bp.route("/api/register", methods=["POST"])
def register():
    data = request.get_json() or {}
    username = data.get("username", "").strip()
    password = data.get("password", "").strip()
    if not username or not password:
        return jsonify({"error": "用户名和密码不能为空"}), 400
    if len(username) < 2 or len(username) > 32:
        return jsonify({"error": "用户名2-32字符"}), 400
    if len(password) < 4:
        return jsonify({"error": "密码至少4位"}), 400
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("INSERT INTO users (username, password_hash) VALUES (%s, %s)",
                    (username, hash_password(password)))
        conn.commit()
        uid = cur.lastrowid
        session['user_id'] = uid
        session['username'] = username
        _ensure_shared_folder(uid)
    except pymysql.err.IntegrityError:
        return jsonify({"error": "用户名已存在"}), 409
    finally:
        cur.close()
        conn.close()
    return jsonify({"ok": True, "user": {"id": uid, "username": username}})


@auth_bp.route("/api/login", methods=["POST"])
def login():
    data = request.get_json() or {}
    username = data.get("username", "").strip()
    password = data.get("password", "").strip()
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, username, password_hash FROM users WHERE username = %s", (username,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    if not row or not check_password(password, row[2]):
        return jsonify({"error": "用户名或密码错误"}), 401
    session['user_id'] = row[0]
    session['username'] = row[1]
    _ensure_shared_folder(row[0])
    return jsonify({"ok": True, "user": {"id": row[0], "username": row[1]}})


@auth_bp.route("/api/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"ok": True})


@auth_bp.route("/api/me")
def me():
    user = get_current_user()
    return jsonify({"user": user})


@auth_bp.route("/api/users")
@login_required
def users():
    return jsonify({"users": get_all_users()})
