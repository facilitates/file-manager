"""收藏 + 可见性 + 共享 + 过期 — 4 个端点"""
from datetime import datetime, timedelta

import pymysql
from flask import request, jsonify, session

from .db import get_db


def register_sharing_routes(bp):
    """在 files_bp 上注册分享相关路由"""

    @bp.route("/api/files/<name>/fav", methods=["POST"])
    def toggle_fav(name):
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT id FROM files WHERE uuid_name = %s AND deleted = 0", (name,))
        row = cur.fetchone()
        if not row:
            cur.close(); conn.close()
            return jsonify({"error": "not found"}), 404
        fid = row[0]
        cur.execute("SELECT id FROM favorites WHERE user_id = %s AND file_id = %s", (session['user_id'], fid))
        if cur.fetchone():
            cur.execute("DELETE FROM favorites WHERE user_id = %s AND file_id = %s", (session['user_id'], fid))
            fav = False
        else:
            cur.execute("INSERT INTO favorites (user_id, file_id) VALUES (%s, %s)", (session['user_id'], fid))
            fav = True
        conn.commit()
        cur.close(); conn.close()
        return jsonify({"ok": True, "fav": fav})

    @bp.route("/api/files/<name>/expire", methods=["PUT"])
    def set_share_expire(name):
        data = request.get_json() or {}
        days = int(data.get("days", 0))
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT id, owner_id FROM files WHERE uuid_name = %s AND deleted = 0", (name,))
        row = cur.fetchone()
        if not row:
            cur.close(); conn.close()
            return jsonify({"error": "not found"}), 404
        if row[1] != session['user_id']:
            cur.close(); conn.close()
            return jsonify({"error": "仅拥有者可设置"}), 403
        expires = (datetime.now() + timedelta(days=days)).strftime('%Y-%m-%d %H:%M:%S') if days > 0 else None
        cur.execute("UPDATE files SET share_expires_at = %s WHERE id = %s", (expires, row[0]))
        conn.commit()
        cur.close(); conn.close()
        return jsonify({"ok": True, "expires_in_days": days})

    @bp.route("/api/files/<name>/visibility", methods=["PUT"])
    def toggle_visibility(name):
        data = request.get_json() or {}
        new_vis = data.get("visibility", "").strip()
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT id, owner_id FROM files WHERE uuid_name = %s AND deleted = 0", (name,))
        row = cur.fetchone()
        if not row:
            cur.close(); conn.close()
            return jsonify({"error": "not found"}), 404
        if row[1] != session['user_id']:
            cur.close(); conn.close()
            return jsonify({"error": "仅文件拥有者可修改权限"}), 403
        if new_vis not in ("private", "public", "shared"):
            return jsonify({"error": "无效的可见性值"}), 400
        cur.execute("UPDATE files SET visibility = %s WHERE id = %s", (new_vis, row[0]))
        conn.commit()
        cur.close(); conn.close()
        return jsonify({"ok": True, "visibility": new_vis})

    @bp.route("/api/files/<name>/share", methods=["POST"])
    def share_file(name):
        data = request.get_json() or {}
        user_ids = data.get("user_ids", [])
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT id, owner_id FROM files WHERE uuid_name = %s AND deleted = 0", (name,))
        row = cur.fetchone()
        if not row:
            cur.close(); conn.close()
            return jsonify({"error": "not found"}), 404
        fid, owner_id = row
        if owner_id != session['user_id']:
            cur.close(); conn.close()
            return jsonify({"error": "仅文件拥有者可设置共享"}), 403
        cur.execute("DELETE FROM file_shares WHERE file_id = %s", (fid,))
        for uid in user_ids:
            if uid != owner_id:
                try:
                    cur.execute("INSERT INTO file_shares (file_id, user_id) VALUES (%s, %s)", (fid, uid))
                except pymysql.err.IntegrityError:
                    pass
        conn.commit()
        cur.close(); conn.close()
        return jsonify({"ok": True, "shared_with": user_ids})

    @bp.route("/api/files/<name>/shares")
    def get_shares(name):
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT id, owner_id FROM files WHERE uuid_name = %s AND deleted = 0", (name,))
        row = cur.fetchone()
        if not row:
            cur.close(); conn.close()
            return jsonify({"error": "not found"}), 404
        if row[1] != session['user_id']:
            cur.close(); conn.close()
            return jsonify({"error": "仅文件拥有者可查看共享列表"}), 403
        cur.execute("SELECT u.id, u.username FROM file_shares fs JOIN users u ON u.id = fs.user_id WHERE fs.file_id = %s", (row[0],))
        shares = [{"id": r[0], "username": r[1]} for r in cur.fetchall()]
        cur.close(); conn.close()
        return jsonify({"shares": shares})
