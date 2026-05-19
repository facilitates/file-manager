"""重复文件检测 — 1 个端点"""
import hashlib
from flask import request, jsonify, session
from flask import current_app as app_ctx

from .db import get_db


def register_duplicate_routes(bp):
    """在 files_bp 上注册重复文件检测路由"""

    @bp.route("/api/duplicates")
    def find_duplicates():
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            SELECT id, uuid_name, original_name, folder_id, size, mime_type, is_image, is_video
            FROM files WHERE owner_id = %s AND deleted = 0
        """, (session['user_id'],))
        rows = cur.fetchall()
        cur.close()
        conn.close()

        upload_dir = app_ctx.config['UPLOAD_DIR']
        hash_groups = {}

        for row in rows:
            fid, uname, oname, fold_id, sz, mime, is_img, is_vid = row
            folder_name = ""
            if fold_id:
                cur2 = get_db().cursor()
                cur2.execute("SELECT name FROM folders WHERE id = %s", (fold_id,))
                fr = cur2.fetchone()
                folder_name = fr[0] if fr else ""
                cur2.close()
                full_path = upload_dir / folder_name / uname
            else:
                full_path = upload_dir / uname

            if not full_path.exists():
                continue

            try:
                md5 = hashlib.md5(full_path.read_bytes()).hexdigest()
            except Exception:
                continue

            rel = f"{folder_name}/{uname}" if folder_name else uname
            file_info = {
                "id": fid, "name": uname, "original": oname,
                "size": sz, "mime": mime or "",
                "is_image": bool(is_img), "is_video": bool(is_vid),
                "folder": folder_name or "", "rel": rel,
                "url": f"/uploads/{rel}"
            }
            if md5 not in hash_groups:
                hash_groups[md5] = []
            hash_groups[md5].append(file_info)

        duplicates = []
        saved = 0
        for md5, files in hash_groups.items():
            if len(files) > 1:
                duplicates.append({"hash": md5[:12], "files": files})
                saved += files[0]["size"] * (len(files) - 1)

        return jsonify({
            "groups": duplicates,
            "total_groups": len(duplicates),
            "total_duplicates": sum(len(g["files"]) - 1 for g in duplicates),
            "wasted_bytes": saved
        })
