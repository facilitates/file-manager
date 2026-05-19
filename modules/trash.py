"""回收站管理 — 6 个端点"""
from datetime import datetime
from flask import request, jsonify, session
from flask import current_app as app_ctx

from .db import get_db, invalidate_cache


def register_trash_routes(bp):
    """在 files_bp 上注册回收站路由"""

    @bp.route("/api/trash")
    def list_trash():
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT id, uuid_name, original_name, original_folder, size, mime_type, deleted_at FROM trash WHERE owner_id = %s ORDER BY deleted_at DESC", (session['user_id'],))
        now = datetime.now()
        items = []
        for r in cur.fetchall():
            deleted_at = r[6]
            remaining = 30 - (now - deleted_at).days if deleted_at else 0
            items.append({
                "id": r[0], "uuid_name": r[1], "original_name": r[2],
                "original_folder": r[3], "size": r[4], "mime_type": r[5],
                "deleted_at": deleted_at.isoformat() if deleted_at else "",
                "remaining_days": max(remaining, 0)
            })
        cur.close(); conn.close()
        return jsonify({"items": items})

    @bp.route("/api/trash/<int:tid>/restore", methods=["POST"])
    def restore_trash(tid):
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM trash WHERE id = %s AND owner_id = %s", (tid, session['user_id']))
        row = cur.fetchone()
        if not row:
            cur.close(); conn.close()
            return jsonify({"error": "not found"}), 404
        cur.execute("UPDATE files SET deleted = 0 WHERE uuid_name = %s AND deleted = 1", (row[1],))
        cur.execute("DELETE FROM trash WHERE id = %s", (tid,))
        conn.commit()
        cur.close(); conn.close()
        invalidate_cache()
        return jsonify({"ok": True})

    @bp.route("/api/trash/<int:tid>", methods=["DELETE"])
    def permanent_delete(tid):
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT uuid_name, original_path, owner_id FROM trash WHERE id = %s", (tid,))
        row = cur.fetchone()
        if not row or row[2] != session['user_id']:
            cur.close(); conn.close()
            return jsonify({"error": "not found"}), 404
        uuid_name, orig_path, _ = row
        upload_dir = app_ctx.config['UPLOAD_DIR']
        full = upload_dir / orig_path
        if full.exists():
            full.unlink()
        cur.execute("DELETE FROM files WHERE uuid_name = %s", (uuid_name,))
        cur.execute("DELETE FROM trash WHERE id = %s", (tid,))
        conn.commit()
        cur.close(); conn.close()
        invalidate_cache()
        return jsonify({"ok": True})

    @bp.route("/api/trash/empty", methods=["POST"])
    def empty_trash():
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT uuid_name, original_path FROM trash WHERE owner_id = %s", (session['user_id'],))
        rows = cur.fetchall()
        upload_dir = app_ctx.config['UPLOAD_DIR']
        for uuid_name, orig_path in rows:
            full = upload_dir / orig_path
            if full.exists():
                full.unlink()
        cur.execute("DELETE FROM files WHERE uuid_name IN (SELECT uuid_name FROM trash WHERE owner_id = %s)", (session['user_id'],))
        cur.execute("DELETE FROM trash WHERE owner_id = %s", (session['user_id'],))
        conn.commit()
        cur.close(); conn.close()
        invalidate_cache()
        return jsonify({"ok": True})

    @bp.route("/api/trash/batch-restore", methods=["POST"])
    def batch_restore_trash():
        data = request.get_json() or {}
        ids = data.get("ids", [])
        if not ids:
            return jsonify({"error": "no ids"}), 400
        conn = get_db()
        cur = conn.cursor()
        for tid in ids:
            cur.execute("SELECT uuid_name FROM trash WHERE id = %s AND owner_id = %s", (tid, session['user_id']))
            row = cur.fetchone()
            if row:
                cur.execute("UPDATE files SET deleted = 0 WHERE uuid_name = %s AND deleted = 1", (row[0],))
                cur.execute("DELETE FROM trash WHERE id = %s", (tid,))
        conn.commit()
        cur.close(); conn.close()
        invalidate_cache()
        return jsonify({"ok": True, "restored": len(ids)})

    @bp.route("/api/trash/batch-delete", methods=["POST"])
    def batch_permanent_delete():
        data = request.get_json() or {}
        ids = data.get("ids", [])
        if not ids:
            return jsonify({"error": "no ids"}), 400
        conn = get_db()
        cur = conn.cursor()
        upload_dir = app_ctx.config['UPLOAD_DIR']
        for tid in ids:
            cur.execute("SELECT uuid_name, original_path FROM trash WHERE id = %s AND owner_id = %s", (tid, session['user_id']))
            row = cur.fetchone()
            if row:
                full = upload_dir / row[1]
                if full.exists():
                    full.unlink()
                cur.execute("DELETE FROM files WHERE uuid_name = %s", (row[0],))
                cur.execute("DELETE FROM trash WHERE id = %s", (tid,))
        conn.commit()
        cur.close(); conn.close()
        invalidate_cache()
        return jsonify({"ok": True, "deleted": len(ids)})
