"""文件 CRUD + 回收站 + 收藏 + 批量 + 分享过期 + 存储统计"""
import os
import re
import uuid
import subprocess
import mimetypes
from pathlib import Path
from datetime import datetime, timedelta

import hashlib
import pymysql
import zipfile
import io
from flask import Blueprint, request, jsonify, session, send_file
from flask import current_app as app_ctx

from .db import get_db, invalidate_cache, rds
from .classify import safe_filename, start_classify_task

files_bp = Blueprint('files', __name__)

TEXT_EXTS = {'.txt', '.md', '.markdown', '.py', '.js', '.ts', '.json', '.xml', '.yaml', '.yml',
             '.css', '.html', '.htm', '.sh', '.bash', '.cfg', '.ini', '.toml', '.env',
             '.sql', '.log', '.csv', '.tsv', '.rb', '.go', '.rs', '.java', '.c', '.cpp', '.h'}


def normalize_filename(name: str) -> str:
    stem, suffix = Path(name).stem, Path(name).suffix
    trans_map = {ord(c): r for c, r in {
        '（': '(', '）': ')', '：': '-', '；': '-', '！': '', '？': '', '。': '', '，': '',
        '、': '-', '【': '[', '】': ']', '「': '', '」': '', '—': '-', '～': '-',
        '《': '', '》': '', '＊': '', '＆': '&', '　': '_',
    }.items()}
    stem = stem.translate(trans_map)
    stem = re.sub(r'\s+', '_', stem)
    stem = re.sub(r'[-_]{2,}', '_', stem)
    stem = stem.strip('-_ .')
    if len(stem) > 100:
        stem = stem[:100]
    if not stem:
        stem = "file"
    return safe_filename(stem + suffix.lower())


def get_files_for_user(folder_name: str, user_id: int, favorites_only=False, public_owner=None) -> list:
    conn = get_db()
    cur = conn.cursor()
    base_sql = """
        SELECT fl.id, fl.uuid_name, fl.original_name, fl.size, fl.mime_type,
               fl.is_image, fl.is_video, fl.ai_category, fl.classify_status,
               fl.created_at, fl.owner_id, fl.visibility, {} AS folder_name,
               u.username AS owner_name, fl.share_expires_at,
               COALESCE(fv.id, 0) AS is_fav
        FROM files fl
        LEFT JOIN users u ON u.id = fl.owner_id
        LEFT JOIN favorites fv ON fv.file_id = fl.id AND fv.user_id = %s
    """

    # 支持查看他人的公开文件夹（public_owner_id != user_id）
    public_owner_id = public_owner or 0
    if folder_name:
        owner_filter = "AND f.owner_id = %s"
        owner_val = user_id if public_owner_id == user_id else public_owner_id
        if public_owner_id and public_owner_id != user_id:
            # 查看别人公开文件夹：不过滤 owner，在下面只显示公开文件
            owner_filter = "AND f.owner_id = %s"
            owner_val = public_owner_id

        sql = base_sql.format("f.name") + f"""\n            JOIN folders f ON f.id = fl.folder_id
            WHERE fl.deleted = 0 AND f.name = %s {owner_filter}
            {{}}
            ORDER BY fl.created_at DESC
        """
        cur.execute(sql.format("AND fv.user_id IS NOT NULL" if favorites_only else ""),
                    (user_id, folder_name, owner_val))
    else:
        sql = base_sql.format("NULL") + """
            WHERE fl.deleted = 0 AND fl.folder_id IS NULL
            {}
            ORDER BY fl.created_at DESC
        """
        cur.execute(sql.format("AND fv.user_id IS NOT NULL" if favorites_only else ""),
                    (user_id,))

    files = []
    for r in cur.fetchall():
        fid, uname, oname, size, mime, is_img, is_vid, ai_cat, cls_st, \
            ctime, owner_id, vis, folder, owner_name, expires_at, is_fav = r
        if owner_id != user_id and vis == "private":
            continue
        if owner_id != user_id and vis == "shared":
            cur2 = conn.cursor()
            cur2.execute("SELECT 1 FROM file_shares WHERE file_id = %s AND user_id = %s", (fid, user_id))
            if not cur2.fetchone():
                cur2.close()
                continue
            cur2.close()
        if owner_id != user_id and expires_at and datetime.now() > expires_at:
            continue
        folder_str = folder or ""
        rel = f"{folder_str}/{uname}" if folder_str else uname
        is_ppt = bool(mime and ('presentation' in mime or 'powerpoint' in mime or uname.lower().endswith(('.ppt', '.pptx'))))
        files.append({
            "id": fid, "name": uname, "original": oname,
            "size": size, "type": mime or "unknown",
            "is_image": bool(is_img), "is_video": bool(is_vid),
            "is_ppt": is_ppt,
            "ai_category": ai_cat, "classify_status": cls_st,
            "folder": folder_str, "rel": rel,
            "mtime": ctime.isoformat() if ctime else "",
            "url": f"/uploads/{rel}",
            "is_owner": owner_id == user_id,
            "is_fav": bool(is_fav),
            "visibility": vis,
            "owner_name": owner_name or "?"
        })
    cur.close()
    conn.close()
    return files


# ==================== 路由 ====================

@files_bp.route("/api/files")
def list_files():
    folder = request.args.get("folder", "").strip()
    fav = request.args.get("fav", "0") == "1"
    public_owner = request.args.get("public_owner")
    if public_owner:
        try:
            public_owner = int(public_owner)
        except (ValueError, TypeError):
            public_owner = None
    files = get_files_for_user(folder, session['user_id'], favorites_only=fav, public_owner=public_owner)
    return jsonify({"files": files, "folder": folder})


@files_bp.route("/api/upload", methods=["POST"])
def upload():
    folder_name = safe_filename(request.form.get("folder", "").strip())
    use_ai = request.form.get("ai", "0") == "1"
    upload_dir = app_ctx.config['UPLOAD_DIR']
    dest = upload_dir / folder_name if folder_name else upload_dir
    dest.mkdir(exist_ok=True)

    if "file" not in request.files:
        return jsonify({"error": "no file"}), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "empty filename"}), 400

    original_name = f.filename
    ext = Path(original_name).suffix or ""
    uuid_name = f"{uuid.uuid4().hex[:8]}{ext.lower()}"
    save_path = dest / uuid_name
    f.save(save_path)

    mime, _ = mimetypes.guess_type(uuid_name)
    is_image = bool(mime and mime.startswith("image/"))
    is_video = bool(mime and mime.startswith("video/"))
    is_media = is_image or is_video

    conn = get_db()
    cur = conn.cursor()
    folder_id = None
    if folder_name:
        cur.execute("SELECT id FROM folders WHERE name = %s AND owner_id = %s", (folder_name, session['user_id']))
        row = cur.fetchone()
        if row:
            folder_id = row[0]

    classify_status = "classifying" if (use_ai and is_media) else None
    cur.execute("""INSERT INTO files
        (uuid_name, original_name, folder_id, size, mime_type, is_image, is_video,
         classify_status, owner_id, visibility, deleted)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'private',0)""",
        (uuid_name, original_name, folder_id, save_path.stat().st_size, mime,
         is_image, is_video, classify_status, session['user_id']))
    conn.commit()
    cur.close()
    conn.close()

    invalidate_cache()
    classifying = False
    if use_ai and is_media:
        start_classify_task(uuid_name, save_path, upload_dir)
        classifying = True

    rel = f"{folder_name}/{uuid_name}" if folder_name else uuid_name
    return jsonify({
        "name": uuid_name, "original": original_name,
        "folder": folder_name, "url": f"/uploads/{rel}",
        "is_image": is_image, "classifying": classifying,
        "visibility": "private"
    })


# ---- 重命名 ----
@files_bp.route("/api/files/<path:filepath>/rename", methods=["PUT"])
def rename_file(filepath):
    path = Path(filepath)
    folder = str(path.parent) if path.parent != Path(".") else ""
    fname = path.name
    data = request.get_json() or {}
    new_name = data.get("name", "").strip()
    if not new_name:
        return jsonify({"error": "name required"}), 400
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, owner_id FROM files WHERE uuid_name = %s AND deleted = 0", (fname,))
    row = cur.fetchone()
    if not row:
        cur.close(); conn.close()
        return jsonify({"error": "not found"}), 404
    if row[1] != session['user_id']:
        cur.close(); conn.close()
        return jsonify({"error": "仅拥有者可重命名"}), 403
    new_name = normalize_filename(new_name)
    cur.execute("UPDATE files SET original_name = %s WHERE id = %s", (new_name, row[0]))
    conn.commit()
    cur.close(); conn.close()
    return jsonify({"ok": True, "name": new_name})


# ---- 删除（移入回收站） ----
@files_bp.route("/api/files/<path:filepath>", methods=["DELETE"])
def delete_file(filepath):
    path = Path(filepath)
    folder = str(path.parent) if path.parent != Path(".") else ""
    fname = path.name
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, owner_id, uuid_name, original_name, size, mime_type, folder_id FROM files WHERE uuid_name = %s AND deleted = 0", (fname,))
    row = cur.fetchone()
    if not row:
        cur.close(); conn.close()
        return jsonify({"error": "not found"}), 404
    fid, owner_id, uname, oname, sz, mime, fold_id = row
    if owner_id != session['user_id']:
        cur.close(); conn.close()
        return jsonify({"error": "仅拥有者可删除"}), 403
    cur.execute("UPDATE files SET deleted = 1 WHERE id = %s", (fid,))
    cur.execute("INSERT INTO trash (uuid_name, original_name, original_folder, original_path, owner_id, size, mime_type) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (uname, oname, folder, folder + '/' + uname if folder else uname, owner_id, sz, mime))
    conn.commit()
    cur.close(); conn.close()
    rds.delete(f"fm:classify:{fname}")
    invalidate_cache()
    return jsonify({"ok": True, "trash": True})


# ---- 回收站 ----
@files_bp.route("/api/trash")
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


@files_bp.route("/api/trash/<int:tid>/restore", methods=["POST"])
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


@files_bp.route("/api/trash/<int:tid>", methods=["DELETE"])
def permanent_delete(tid):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT uuid_name, original_path, owner_id FROM trash WHERE id = %s", (tid,))
    row = cur.fetchone()
    if not row or row[2] != session['user_id']:
        cur.close(); conn.close()
        return jsonify({"error": "not found"}), 404
    uuid_name, orig_path, _ = row
    # 删文件
    upload_dir = app_ctx.config['UPLOAD_DIR']
    full = upload_dir / orig_path
    if full.exists():
        full.unlink()
    # 删 files 表记录
    cur.execute("DELETE FROM files WHERE uuid_name = %s", (uuid_name,))
    cur.execute("DELETE FROM trash WHERE id = %s", (tid,))
    conn.commit()
    cur.close(); conn.close()
    invalidate_cache()
    return jsonify({"ok": True})


@files_bp.route("/api/trash/empty", methods=["POST"])
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


@files_bp.route("/api/trash/batch-restore", methods=["POST"])
def batch_restore_trash():
    """批量恢复回收站文件"""
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


@files_bp.route("/api/trash/batch-delete", methods=["POST"])
def batch_permanent_delete():
    """批量永久删除回收站文件"""
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


# ---- 收藏 ----
@files_bp.route("/api/files/<name>/fav", methods=["POST"])
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


# ---- 批量操作 ----
@files_bp.route("/api/batch", methods=["POST"])
def batch_operation():
    data = request.get_json() or {}
    action = data.get("action", "")
    files_list = data.get("files", [])
    target_folder = data.get("folder", "")
    if not files_list:
        return jsonify({"error": "no files"}), 400
    conn = get_db()
    cur = conn.cursor()
    upload_dir = app_ctx.config['UPLOAD_DIR']

    for rel in files_list:
        path = Path(rel)
        folder = str(path.parent) if path.parent != Path(".") else ""
        fname = path.name
        cur.execute("SELECT id, owner_id FROM files WHERE uuid_name = %s AND deleted = 0", (fname,))
        row = cur.fetchone()
        if not row or row[1] != session['user_id']:
            continue
        fid = row[0]
        if action == "delete":
            cur.execute("UPDATE files SET deleted = 1 WHERE id = %s", (fid,))
            cur.execute("INSERT INTO trash (uuid_name, original_name, original_folder, original_path, owner_id, size, mime_type) SELECT uuid_name, original_name, %s, %s, owner_id, size, mime_type FROM files WHERE id = %s",
                        (folder, rel, fid))
        elif action == "move" and target_folder:
            cur.execute("SELECT id FROM folders WHERE name = %s AND owner_id = %s", (target_folder, session['user_id']))
            fr = cur.fetchone()
            if fr:
                new_fid = fr[0]
                src = upload_dir / folder / fname
                dst_dir = upload_dir / target_folder
                dst_dir.mkdir(exist_ok=True)
                dst = dst_dir / fname
                if src.exists():
                    src.rename(dst)
                cur.execute("UPDATE files SET folder_id = %s WHERE id = %s", (new_fid, fid))

    conn.commit()
    cur.close(); conn.close()
    invalidate_cache()
    return jsonify({"ok": True})


# ---- 存储概览 ----
@files_bp.route("/api/stats")
def storage_stats():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*), COALESCE(SUM(size),0) FROM files WHERE owner_id = %s AND deleted = 0", (session['user_id'],))
    total_files, total_size = cur.fetchone()
    # 按类型
    cur.execute("""
        SELECT CASE
            WHEN is_image THEN 'image' WHEN is_video THEN 'video'
            WHEN mime_type LIKE '%%pdf%%' THEN 'document'
            WHEN mime_type LIKE '%%presentation%%' OR mime_type LIKE '%%powerpoint%%' OR uuid_name LIKE '%%.pptx' THEN 'presentation'
            WHEN mime_type LIKE '%%text%%' OR uuid_name RLIKE '\\.(txt|md|py|js|json|log|csv)$' THEN 'text'
            ELSE 'other'
        END AS category, COUNT(*), COALESCE(SUM(size),0)
        FROM files WHERE owner_id = %s AND deleted = 0
        GROUP BY category
    """, (session['user_id'],))
    by_type = [{"type": r[0], "count": r[1], "size": r[2]} for r in cur.fetchall()]
    # 回收站
    cur.execute("SELECT COUNT(*), COALESCE(SUM(size),0) FROM trash WHERE owner_id = %s", (session['user_id'],))
    trash_count, trash_size = cur.fetchone()
    cur.close(); conn.close()
    return jsonify({
        "total_files": total_files, "total_size": total_size,
        "by_type": by_type,
        "trash_count": trash_count or 0, "trash_size": trash_size or 0
    })


# ---- 文件压缩 ----
@files_bp.route("/api/compress/<path:filepath>", methods=["POST"])
def compress_file(filepath):
    """压缩单张图片，返回新文件"""
    path = Path(filepath)
    folder = str(path.parent) if path.parent != Path(".") else ""
    fname = path.name
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, owner_id, original_name, is_image FROM files WHERE uuid_name = %s AND deleted = 0", (fname,))
    row = cur.fetchone()
    if not row or row[1] != session['user_id']:
        cur.close(); conn.close()
        return jsonify({"error": "not found"}), 404
    fid, _, oname, is_img = row
    upload_dir = app_ctx.config['UPLOAD_DIR']
    full = upload_dir / Path(filepath)
    if not full.exists():
        cur.close(); conn.close()
        return jsonify({"error": "file missing"}), 404

    old_size = full.stat().st_size
    ext = full.suffix.lower()

    if is_img and ext in ('.jpg', '.jpeg', '.png', '.webp', '.bmp', '.gif'):
        try:
            from PIL import Image
            img = Image.open(full)
            new_name = f"{Path(oname).stem}_compressed.jpg"
            uuid_name = f"{uuid.uuid4().hex[:8]}.jpg"
            new_path = full.parent / uuid_name
            # 转 RGB + 压缩
            if img.mode in ('RGBA', 'P'):
                img = img.convert('RGB')
            img.save(new_path, 'JPEG', quality=60, optimize=True)
            new_size = new_path.stat().st_size
        except Exception as e:
            cur.close(); conn.close()
            return jsonify({"error": f"压缩失败: {str(e)}"}), 500
    else:
        cur.close(); conn.close()
        return jsonify({"error": "仅支持图片压缩"}), 400

    # 写入数据库
    cur.execute("""INSERT INTO files (uuid_name, original_name, folder_id, size, mime_type,
                   is_image, is_video, classify_status, owner_id, visibility, deleted)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'private',0)""",
                (uuid_name, new_name, None, new_size, 'image/jpeg', 1, 0, None, session['user_id']))
    conn.commit()
    cur.close(); conn.close()

    return jsonify({
        "ok": True,
        "name": uuid_name, "original": new_name,
        "old_size": old_size, "new_size": new_size,
        "saved": old_size - new_size,
        "ratio": round((1 - new_size / old_size) * 100, 1) if old_size else 0
    })


# ---- 打包下载 ----
@files_bp.route("/api/zip", methods=["POST"])
def zip_download():
    """将选中的多个文件打包为 zip 并下载"""
    data = request.get_json() or {}
    files_list = data.get("files", [])
    if not files_list:
        return jsonify({"error": "no files"}), 400

    upload_dir = app_ctx.config['UPLOAD_DIR']
    buf = io.BytesIO()
    conn = get_db()
    cur = conn.cursor()

    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for rel in files_list:
            path = Path(rel)
            folder = str(path.parent) if path.parent != Path(".") else ""
            fname = path.name
            cur.execute("SELECT id, owner_id, original_name FROM files WHERE uuid_name = %s AND deleted = 0", (fname,))
            row = cur.fetchone()
            if not row or row[1] != session['user_id']:
                continue
            full = upload_dir / Path(rel)
            if full.exists():
                zf.write(full, row[2])  # 用原始文件名

    cur.close(); conn.close()
    buf.seek(0)
    return send_file(buf, mimetype='application/zip', as_attachment=True,
                     download_name=f"files_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip")


# ---- 重复文件检测 ----
@files_bp.route("/api/duplicates")
def find_duplicates():
    """扫描当前用户的所有文件，按 MD5 哈希分组，返回重复文件组"""
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
    hash_groups = {}  # md5 -> [file_info, ...]

    for row in rows:
        fid, uname, oname, fold_id, sz, mime, is_img, is_vid = row
        folder_name = ""
        # 构建完整路径
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

    # 只返回有重复的组（>1个文件）
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


# ---- 分享过期 ----
@files_bp.route("/api/files/<name>/expire", methods=["PUT"])
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


# ---- 可见性 ----
@files_bp.route("/api/files/<name>/visibility", methods=["PUT"])
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


# ---- 共享 ----
@files_bp.route("/api/files/<name>/share", methods=["POST"])
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


@files_bp.route("/api/files/<name>/shares")
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


# ---- 分类状态 ----
@files_bp.route("/api/classify-status/<name>")
def classify_status(name):
    stat = rds.get(f"fm:classify:{name}")
    if stat:
        return jsonify({"name": name, "status": stat})
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT classify_status, ai_category FROM files WHERE uuid_name = %s", (name,))
    row = cur.fetchone()
    cur.close(); conn.close()
    if row:
        if row[0] == "done":
            return jsonify({"name": name, "status": row[1] or "done"})
        return jsonify({"name": name, "status": row[0] or "unknown"})
    return jsonify({"name": name, "status": "unknown"})


@files_bp.route("/api/classify-batch", methods=["POST"])
def classify_batch_status():
    data = request.get_json() or {}
    names = data.get("names", [])
    result = {n: rds.get(f"fm:classify:{n}") or "unknown" for n in names}
    return jsonify({"statuses": result})


# ---- 文本读取 ----
@files_bp.route("/api/read/<path:filepath>")
def read_file_content(filepath):
    path = Path(filepath)
    folder = str(path.parent) if path.parent != Path(".") else ""
    fname = path.name
    full = app_ctx.config['UPLOAD_DIR'] / folder / fname
    if not full.exists():
        return jsonify({"error": "not found"}), 404
    ext = full.suffix.lower()
    if ext not in TEXT_EXTS:
        return jsonify({"error": "unsupported type"}), 400
    try:
        content = full.read_text(encoding='utf-8')
    except UnicodeDecodeError:
        try:
            content = full.read_text(encoding='gbk')
        except:
            return jsonify({"error": "cannot decode"}), 400
    if len(content) > 500 * 1024:
        content = content[:500 * 1024] + "\n\n... (文件过大，仅显示前 500KB)"
    is_md = ext in ('.md', '.markdown')
    return jsonify({"content": content, "type": "markdown" if is_md else "text", "name": fname})


# ---- PPT 预览 ----
PPT_CACHE_DIR = Path(__file__).parent.parent / "ppt_cache"
PPT_CACHE_DIR.mkdir(exist_ok=True)


@files_bp.route("/api/preview/ppt/<path:filepath>")
def preview_ppt(filepath):
    path = Path(filepath)
    folder = str(path.parent) if path.parent != Path(".") else ""
    fname = path.name
    full = app_ctx.config['UPLOAD_DIR'] / folder / fname
    if not full.exists():
        return jsonify({"error": "not found"}), 404
    cache_key = fname.rsplit('.', 1)[0]
    cache_dir = PPT_CACHE_DIR / cache_key
    if not cache_dir.exists() or not any(cache_dir.iterdir()):
        cache_dir.mkdir(parents=True, exist_ok=True)
        try:
            subprocess.run(["libreoffice", "--headless", "--convert-to", "png", "--outdir", str(cache_dir), str(full)],
                           capture_output=True, timeout=60, check=True)
        except subprocess.CalledProcessError:
            return jsonify({"error": "PPT 转换失败"}), 500
    slides = sorted(cache_dir.glob("*.png"))
    urls = [f"/api/preview/ppt-img/{cache_key}/{s.name}" for s in slides]
    return jsonify({"slides": urls, "count": len(slides)})


@files_bp.route("/api/preview/ppt-img/<cache_key>/<img_name>")
def serve_ppt_img(cache_key, img_name):
    return send_file(PPT_CACHE_DIR / cache_key / img_name, mimetype='image/png')
