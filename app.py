"""文件管理器 — 入口"""
import os
import secrets
from pathlib import Path

from flask import Flask, render_template, session, send_from_directory

from modules.db import get_folders_from_db
from modules.auth import auth_bp, get_current_user
from modules.files import files_bp
from modules.folders import folders_bp
from modules.tools import tools_bp
from modules.logview import logview_bp

# 注册子模块路由到 files_bp（降低单文件耦合）
from modules.trash import register_trash_routes
from modules.sharing import register_sharing_routes
from modules.duplicates import register_duplicate_routes
from modules.preview import register_preview_routes
register_trash_routes(files_bp)
register_sharing_routes(files_bp)
register_duplicate_routes(files_bp)
register_preview_routes(files_bp)

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY') or secrets.token_hex(32)

# 配置
UPLOAD_DIR = Path(__file__).parent / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)
app.config['UPLOAD_DIR'] = UPLOAD_DIR
app.config['MAX_CONTENT_LENGTH'] = 200 * 1024 * 1024

# 注册蓝图
app.register_blueprint(auth_bp)
app.register_blueprint(files_bp)
app.register_blueprint(folders_bp)
app.register_blueprint(tools_bp)
app.register_blueprint(logview_bp)


# ---- 页面路由 ----

@app.route("/")
def index():
    if 'user_id' not in session:
        return render_template("login.html")
    user = get_current_user()
    return render_template("main.html", folders=get_folders_from_db(), user=user)


# ---- 静态文件服务 ----

@app.route("/uploads/<path:filepath>")
def serve_upload(filepath):
    path = Path(filepath)
    folder = str(path.parent) if path.parent != Path(".") else ""
    return send_from_directory(UPLOAD_DIR / folder, path.name)


@app.route("/api/download/<path:filepath>")
def download_file(filepath):
    path = Path(filepath)
    folder = str(path.parent) if path.parent != Path(".") else ""
    full = UPLOAD_DIR / folder / path.name
    if not full.exists():
        from flask import jsonify
        return jsonify({"error": "not found"}), 404
    return send_from_directory(full.parent, path.name, as_attachment=True, download_name=path.name)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
