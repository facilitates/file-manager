#!/usr/bin/env python3
"""
自动同步脚本：监控 Hermes 缓存目录，新文件自动转存到文件管理器
每 30 秒扫描一次，已同步的文件记录在 .sync_db.json 中避免重复
"""
import os
import json
import shutil
import time
import hashlib
from pathlib import Path

HERMES_HOME = Path.home() / ".hermes"
WATCH_DIRS = [
    HERMES_HOME / "image_cache",
    HERMES_HOME / "document_cache",
    HERMES_HOME / "audio_cache",
]
DEST_BASE = Path("/home/ubuntu/file-manager/uploads")
DB_FILE = Path("/home/ubuntu/file-manager/.sync_db.json")
SYNC_FOLDER = "微信"  # 所有自动同步的文件放这个文件夹

def load_db():
    if DB_FILE.exists():
        try:
            return json.loads(DB_FILE.read_text())
        except:
            pass
    return {}

def save_db(db):
    DB_FILE.write_text(json.dumps(db))

def file_hash(path):
    """快速哈希：用路径+mtime+大小"""
    stat = path.stat()
    return hashlib.md5(f"{path}{stat.st_mtime}{stat.st_size}".encode()).hexdigest()

def sync():
    db = load_db()
    dest = DEST_BASE / SYNC_FOLDER
    dest.mkdir(exist_ok=True)

    for watch_dir in WATCH_DIRS:
        if not watch_dir.exists():
            continue
        for f in watch_dir.iterdir():
            if not f.is_file():
                continue
            h = file_hash(f)
            if h in db:
                continue  # 已同步过

            # 复制到目标
            ext = f.suffix or ""
            # 尝试用原始名，否则用 hash 前8位
            try:
                shutil.copy2(f, dest / f.name)
                print(f"[OK] {f.name} → {SYNC_FOLDER}/")
            except:
                continue

            db[h] = f.name
            save_db(db)

if __name__ == "__main__":
    print("🔄 自动同步已启动（每30秒扫描）")
    print(f"   源: {[str(d) for d in WATCH_DIRS]}")
    print(f"   目标: {DEST_BASE / SYNC_FOLDER}")
    while True:
        try:
            sync()
        except Exception as e:
            print(f"[ERR] {e}")
        time.sleep(30)
