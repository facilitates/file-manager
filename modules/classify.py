"""AI 分类模块"""
import os
import base64
import mimetypes
import subprocess
import threading
import requests

from .db import DOUBAO_KEY, DOUBAO_BASE, DOUBAO_MODEL, CLASSIFY_PROMPT, get_db, invalidate_cache, rds


def safe_filename(name: str) -> str:
    return name.replace("..", "").replace("/", "_").replace("\\", "_")


def ai_classify_image(image_path: str) -> str | None:
    try:
        with open(image_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        resp = requests.post(
            f"{DOUBAO_BASE}/chat/completions",
            headers={"Authorization": f"Bearer {DOUBAO_KEY}", "Content-Type": "application/json"},
            json={
                "model": DOUBAO_MODEL,
                "messages": [{"role": "user", "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                    {"type": "text", "text": CLASSIFY_PROMPT}
                ]}],
                "max_tokens": 10, "temperature": 0.1
            },
            timeout=30
        )
        category = resp.json()["choices"][0]["message"]["content"].strip()
        category = category.replace("\n", "").replace("。", "").replace("，", "").replace("、", "").replace(",", "").replace(" ", "")
        return safe_filename(category) or "其他"
    except Exception as e:
        print(f"[AI分类错误] {e}")
        return None


def ai_classify_video(video_path: str) -> str:
    try:
        frame_path = str(video_path) + ".frame.jpg"
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(video_path), "-vframes", "1", "-q:v", "3", frame_path],
            capture_output=True, timeout=15
        )
        if os.path.exists(frame_path) and os.path.getsize(frame_path) > 100:
            cat = ai_classify_image(frame_path)
            os.remove(frame_path)
            return cat
    except Exception as e:
        print(f"[视频分类错误] {e}")
    return "视频"


def ai_classify(filepath: str) -> str | None:
    mime, _ = mimetypes.guess_type(str(filepath))
    if mime and mime.startswith("image/"):
        return ai_classify_image(filepath)
    elif mime and mime.startswith("video/"):
        return ai_classify_video(filepath)
    return None


def classify_background(uuid_name: str, save_path, upload_dir):
    """后台线程：AI 分类并更新 DB，移动文件"""
    try:
        cat = ai_classify(str(save_path))
        conn = get_db()
        cur = conn.cursor()
        if cat and cat.strip():
            cat = safe_filename(cat)
            cur.execute("INSERT IGNORE INTO folders (name) VALUES (%s)", (cat,))
            conn.commit()
            cur.execute("SELECT id FROM folders WHERE name = %s", (cat,))
            fid = cur.fetchone()[0]
            new_dest = upload_dir / cat
            new_dest.mkdir(exist_ok=True)
            save_path.rename(new_dest / uuid_name)
            cur.execute(
                "UPDATE files SET folder_id=%s, ai_category=%s, classify_status='done' WHERE uuid_name=%s",
                (fid, cat, uuid_name)
            )
            print(f"[AI分类] {uuid_name} → {cat}")
        else:
            cur.execute("UPDATE files SET classify_status='failed' WHERE uuid_name=%s", (uuid_name,))
        conn.commit()
        rds.setex(f"fm:classify:{uuid_name}", 1800, cat if cat else "failed")
        invalidate_cache()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"[AI分类错误] {uuid_name}: {e}")


def start_classify_task(uuid_name: str, save_path, upload_dir):
    """启动后台分类线程"""
    rds.setex(f"fm:classify:{uuid_name}", 1800, "classifying")
    threading.Thread(
        target=classify_background,
        args=(uuid_name, save_path, upload_dir),
        daemon=True
    ).start()
