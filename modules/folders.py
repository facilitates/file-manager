"""文件夹 CRUD — 按用户隔离"""
import pymysql
from flask import Blueprint, request, jsonify, session

from .db import get_db, invalidate_cache, get_folders_from_db
from .classify import safe_filename

folders_bp = Blueprint('folders', __name__)


@folders_bp.route("/api/folders")
def list_folders():
    uid = session.get('user_id')
    if not uid:
        return jsonify({"folders": []})
    return jsonify({"folders": get_folders_from_db(uid)})


@folders_bp.route("/api/folders", methods=["POST"])
def create_folder():
    uid = session.get('user_id')
    if not uid:
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json() or {}
    name = safe_filename(data.get("name", "").strip())
    if not name:
        return jsonify({"error": "name required"}), 400
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("INSERT INTO folders (name, owner_id) VALUES (%s, %s)", (name, uid))
        conn.commit()
        fid = cur.lastrowid
    except pymysql.err.IntegrityError:
        return jsonify({"error": "folder exists"}), 409
    finally:
        cur.close()
        conn.close()
    # 文件系统同步（文件夹名不加用户前缀，因为不同用户的同名文件夹物理隔离在各自的用户目录下也可）
    # 保持简单：文件夹名直接作为目录名，文件层面通过 DB 隔离
    from flask import current_app
    upload_dir = current_app.config['UPLOAD_DIR']
    (upload_dir / name).mkdir(exist_ok=True)
    invalidate_cache()
    return jsonify({"ok": True, "id": fid, "name": name})


@folders_bp.route("/api/folders/<name>", methods=["DELETE"])
def delete_folder(name):
    uid = session.get('user_id')
    if not uid:
        return jsonify({"error": "unauthorized"}), 401
    name = safe_filename(name)
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id FROM folders WHERE name = %s AND owner_id = %s", (name, uid))
    row = cur.fetchone()
    if not row:
        cur.close()
        conn.close()
        return jsonify({"error": "not found"}), 404
    fid = row[0]
    cur.execute("SELECT COUNT(*) FROM files WHERE folder_id = %s", (fid,))
    if cur.fetchone()[0] > 0:
        cur.close()
        conn.close()
        return jsonify({"error": "folder not empty"}), 400
    cur.execute("DELETE FROM folders WHERE id = %s", (fid,))
    conn.commit()
    cur.close()
    conn.close()
    from flask import current_app
    upload_dir = current_app.config['UPLOAD_DIR']
    dirpath = upload_dir / name
    if dirpath.exists() and dirpath.is_dir():
        try:
            dirpath.rmdir()
        except OSError:
            pass
    invalidate_cache()
    return jsonify({"ok": True})


# ---- 公开分享文件夹 ----


@folders_bp.route("/api/public-folders")
def list_public_folders():
    """列出所有用户的「🌐 公开分享」文件夹（排除自己的）"""
    uid = session.get("user_id")
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """SELECT f.id, f.name, u.id, u.username,
                  (SELECT COUNT(*) FROM files WHERE folder_id = f.id) AS cnt
           FROM folders f
           JOIN users u ON u.id = f.owner_id
           WHERE f.name = 'public' AND f.owner_id != %s""",
        (uid or 0,),
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return jsonify({
        "folders": [
            {"id": r[0], "name": r[1], "owner_id": r[2],
             "owner_name": r[3], "count": r[4]}
            for r in rows
        ]
    })
