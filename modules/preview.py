"""文件预览 — PPT 转换 + 文本读取 + 分类状态"""
import subprocess
from pathlib import Path

from flask import jsonify, send_file
from flask import current_app as app_ctx

from .db import get_db, rds

TEXT_EXTS = {'.txt', '.md', '.markdown', '.py', '.js', '.ts', '.json', '.xml', '.yaml', '.yml',
             '.css', '.html', '.htm', '.sh', '.bash', '.cfg', '.ini', '.toml', '.env',
             '.sql', '.log', '.csv', '.tsv', '.rb', '.go', '.rs', '.java', '.c', '.cpp', '.h'}

PPT_CACHE_DIR = Path(__file__).parent.parent / "ppt_cache"
PPT_CACHE_DIR.mkdir(exist_ok=True)


def register_preview_routes(bp):
    """在 files_bp 上注册预览相关路由"""

    @bp.route("/api/read/<path:filepath>")
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
            except Exception:
                return jsonify({"error": "cannot decode"}), 400
        if len(content) > 500 * 1024:
            content = content[:500 * 1024] + "\n\n... (文件过大，仅显示前 500KB)"
        is_md = ext in ('.md', '.markdown')
        return jsonify({"content": content, "type": "markdown" if is_md else "text", "name": fname})

    @bp.route("/api/preview/ppt/<path:filepath>")
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

    @bp.route("/api/preview/ppt-img/<cache_key>/<img_name>")
    def serve_ppt_img(cache_key, img_name):
        return send_file(PPT_CACHE_DIR / cache_key / img_name, mimetype='image/png')

    @bp.route("/api/classify-status/<name>")
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

    @bp.route("/api/classify-batch", methods=["POST"])
    def classify_batch_status():
        from flask import request
        data = request.get_json() or {}
        names = data.get("names", [])
        result = {n: rds.get(f"fm:classify:{n}") or "unknown" for n in names}
        return jsonify({"statuses": result})
