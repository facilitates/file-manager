"""工具箱 — PDF/图片/OCR/ZIP/格式转换 (对标WPS付费功能)"""
import os, io, zipfile, tempfile, subprocess
from pathlib import Path

from PIL import Image
from PyPDF2 import PdfReader, PdfWriter, PdfMerger
from flask import Blueprint, request, jsonify, session, send_file

from .db import get_db

tools_bp = Blueprint('tools', __name__)


def _user_upload_dir():
    from flask import current_app
    return current_app.config['UPLOAD_DIR']


def _resolve_path(rel):
    """将 relative path 转为绝对路径"""
    p = Path(rel)
    folder = str(p.parent) if p.parent != Path(".") else ""
    fname = p.name
    full = _user_upload_dir() / folder / fname
    return full, folder, fname


# ==================== PDF 工具 ====================

@tools_bp.route("/api/tools/pdf/merge", methods=["POST"])
def pdf_merge():
    """合并多个 PDF"""
    data = request.get_json() or {}
    files = data.get("files", [])
    if len(files) < 2:
        return jsonify({"error": "至少需要2个PDF"}), 400

    merger = PdfMerger()
    for rel in files:
        full, _, _ = _resolve_path(rel)
        if not full.exists() or full.suffix.lower() != '.pdf':
            continue
        merger.append(str(full))

    if not merger.pages:
        return jsonify({"error": "没有有效的PDF"}), 400

    out_name = f"merged_{len(files)}pdfs.pdf"
    out_path = _user_upload_dir() / out_name
    merger.write(str(out_path))
    merger.close()

    _record_file(out_name, out_name, out_path.stat().st_size, "application/pdf")
    return jsonify({"ok": True, "name": out_name, "url": f"/uploads/{out_name}", "pages": len(merger.pages)})


@tools_bp.route("/api/tools/pdf/split", methods=["POST"])
def pdf_split():
    """拆分 PDF — 返回每页单独文件，或按页码范围"""
    data = request.get_json() or {}
    rel = data.get("file", "")
    pages_str = data.get("pages", "")  # "1-3,5,7-9" or empty = all pages
    full, _, _ = _resolve_path(rel)
    if not full.exists():
        return jsonify({"error": "not found"}), 404

    reader = PdfReader(str(full))
    total = len(reader.pages)

    # 解析页码范围
    to_extract = set()
    if pages_str:
        for part in pages_str.split(","):
            part = part.strip()
            if "-" in part:
                a, b = part.split("-", 1)
                to_extract.update(range(int(a) - 1, min(int(b), total)))
            else:
                to_extract.add(int(part) - 1)
    else:
        to_extract = set(range(total))

    to_extract = sorted(p for p in to_extract if 0 <= p < total)
    if not to_extract:
        return jsonify({"error": "无效页码"}), 400

    results = []
    stem = Path(rel).stem
    for i in to_extract:
        writer = PdfWriter()
        writer.add_page(reader.pages[i])
        out_name = f"{stem}_p{i+1}.pdf"
        out_path = _user_upload_dir() / out_name
        writer.write(str(out_path))
        writer.close()
        _record_file(out_name, out_name, out_path.stat().st_size, "application/pdf")
        results.append({"name": out_name, "url": f"/uploads/{out_name}", "page": i + 1})

    return jsonify({"ok": True, "total_pages": total, "extracted": len(results), "files": results})


@tools_bp.route("/api/tools/pdf/compress", methods=["POST"])
def pdf_compress():
    """压缩 PDF（用 ghostscript）"""
    data = request.get_json() or {}
    rel = data.get("file", "")
    full, _, _ = _resolve_path(rel)
    if not full.exists():
        return jsonify({"error": "not found"}), 404

    original_size = full.stat().st_size
    out_name = f"compressed_{Path(rel).name}"
    out_path = _user_upload_dir() / out_name

    subprocess.run([
        "gs", "-sDEVICE=pdfwrite", "-dCompatibilityLevel=1.4",
        "-dPDFSETTINGS=/ebook", "-dNOPAUSE", "-dQUIET", "-dBATCH",
        f"-sOutputFile={out_path}", str(full)
    ], capture_output=True, timeout=60)

    if not out_path.exists():
        return jsonify({"error": "压缩失败"}), 500

    new_size = out_path.stat().st_size
    _record_file(out_name, out_name, new_size, "application/pdf")
    return jsonify({
        "ok": True, "name": out_name, "url": f"/uploads/{out_name}",
        "original_size": original_size, "compressed_size": new_size,
        "ratio": round((1 - new_size / original_size) * 100, 1)
    })


# ==================== 图片工具 ====================

@tools_bp.route("/api/tools/image/compress", methods=["POST"])
def image_compress():
    """压缩图片"""
    data = request.get_json() or {}
    rel = data.get("file", "")
    quality = int(data.get("quality", 70))
    max_width = int(data.get("max_width", 0)) or None

    full, folder, fname = _resolve_path(rel)
    if not full.exists():
        return jsonify({"error": "not found"}), 404

    original_size = full.stat().st_size
    img = Image.open(str(full))
    img = img.convert("RGB")  # JPEG doesn't support alpha

    if max_width and img.width > max_width:
        ratio = max_width / img.width
        new_h = int(img.height * ratio)
        img = img.resize((max_width, new_h), Image.LANCZOS)

    stem = Path(rel).stem
    out_name = f"{stem}_compressed.jpg"
    out_path = _user_upload_dir() / (folder + "/" + out_name if folder else out_name)
    out_path.parent.mkdir(exist_ok=True)
    img.save(str(out_path), "JPEG", quality=quality, optimize=True)

    new_size = out_path.stat().st_size
    _record_file(out_name, out_name, new_size, "image/jpeg", folder)
    return jsonify({
        "ok": True, "name": out_name, "url": f"/uploads/{out_name}",
        "original_size": original_size, "compressed_size": new_size,
        "ratio": round((1 - new_size / original_size) * 100, 1)
    })


@tools_bp.route("/api/tools/image/resize", methods=["POST"])
def image_resize():
    """调整图片尺寸"""
    data = request.get_json() or {}
    rel = data.get("file", "")
    width = int(data.get("width", 0))
    height = int(data.get("height", 0))

    if not width and not height:
        return jsonify({"error": "need width or height"}), 400

    full, folder, fname = _resolve_path(rel)
    if not full.exists():
        return jsonify({"error": "not found"}), 404

    img = Image.open(str(full))
    ow, oh = img.size
    if width and height:
        new_size = (width, height)
    elif width:
        new_size = (width, int(oh * width / ow))
    else:
        new_size = (int(ow * height / oh), height)

    img = img.resize(new_size, Image.LANCZOS)
    out_name = f"resized_{Path(rel).name}"
    out_path = _user_upload_dir() / (folder + "/" + out_name if folder else out_name)
    out_path.parent.mkdir(exist_ok=True)
    img.save(str(out_path))

    _record_file(out_name, out_name, out_path.stat().st_size, None, folder)
    return jsonify({"ok": True, "name": out_name, "url": f"/uploads/{out_name}", "size": list(new_size)})


@tools_bp.route("/api/tools/image/convert", methods=["POST"])
def image_convert():
    """图片格式转换"""
    data = request.get_json() or {}
    rel = data.get("file", "")
    fmt = data.get("format", "jpeg").lower()

    full, folder, fname = _resolve_path(rel)
    if not full.exists():
        return jsonify({"error": "not found"}), 404

    img = Image.open(str(full))
    if fmt == "jpg":
        fmt = "jpeg"
        img = img.convert("RGB")

    out_name = f"{Path(rel).stem}.{fmt}"
    out_path = _user_upload_dir() / (folder + "/" + out_name if folder else out_name)
    out_path.parent.mkdir(exist_ok=True)
    img.save(str(out_path), fmt.upper())

    _record_file(out_name, out_name, out_path.stat().st_size, f"image/{fmt}", folder)
    return jsonify({"ok": True, "name": out_name, "url": f"/uploads/{out_name}"})


# ==================== ZIP 压缩/解压 ====================

@tools_bp.route("/api/tools/zip/compress", methods=["POST"])
def zip_compress():
    """多文件打包 ZIP"""
    data = request.get_json() or {}
    files = data.get("files", [])
    if not files:
        return jsonify({"error": "no files"}), 400

    out_name = "archive.zip"
    out_path = _user_upload_dir() / out_name
    with zipfile.ZipFile(str(out_path), 'w', zipfile.ZIP_DEFLATED) as zf:
        for rel in files:
            full, _, _ = _resolve_path(rel)
            if full.exists():
                zf.write(str(full), Path(rel).name)

    _record_file(out_name, out_name, out_path.stat().st_size, "application/zip")
    return jsonify({"ok": True, "name": out_name, "url": f"/uploads/{out_name}", "count": len(files)})


@tools_bp.route("/api/tools/zip/extract", methods=["POST"])
def zip_extract():
    """解压 ZIP"""
    data = request.get_json() or {}
    rel = data.get("file", "")
    full, folder, _ = _resolve_path(rel)
    if not full.exists() or full.suffix.lower() != '.zip':
        return jsonify({"error": "not a zip file"}), 400

    extracted = []
    dest_dir = _user_upload_dir() / folder if folder else _user_upload_dir()
    with zipfile.ZipFile(str(full), 'r') as zf:
        for name in zf.namelist():
            # 安全：防止路径穿越
            safe_name = Path(name).name
            if not safe_name:
                continue
            zf.extract(name, str(dest_dir))
            out_path = dest_dir / name
            if out_path.is_file():
                _record_file(safe_name, safe_name, out_path.stat().st_size, None, folder)
                extracted.append(safe_name)

    return jsonify({"ok": True, "extracted": len(extracted), "files": extracted})


# ==================== OCR 识别 ====================

@tools_bp.route("/api/tools/ocr", methods=["POST"])
def ocr_image():
    """图片 OCR 文字识别（支持中英文）"""
    data = request.get_json() or {}
    rel = data.get("file", "")
    full, _, _ = _resolve_path(rel)
    if not full.exists():
        return jsonify({"error": "not found"}), 404

    lang = data.get("lang", "chi_sim+eng")
    try:
        result = subprocess.run(
            ["tesseract", str(full), "stdout", "-l", lang, "--psm", "3"],
            capture_output=True, text=True, timeout=30
        )
        text = result.stdout.strip()
        if not text:
            text = result.stderr.strip() or "(无文字识别结果)"
    except subprocess.TimeoutExpired:
        return jsonify({"error": "OCR超时"}), 500

    return jsonify({"ok": True, "text": text, "lines": text.count('\n') + 1 if text else 0})


# ==================== 格式转换 ====================

@tools_bp.route("/api/tools/convert/images-to-pdf", methods=["POST"])
def images_to_pdf():
    """多图转 PDF"""
    data = request.get_json() or {}
    files = data.get("files", [])
    if not files:
        return jsonify({"error": "no files"}), 400

    images = []
    for rel in files:
        full, _, _ = _resolve_path(rel)
        if not full.exists():
            continue
        try:
            img = Image.open(str(full)).convert("RGB")
            images.append(img)
        except Exception:
            continue

    if not images:
        return jsonify({"error": "no valid images"}), 400

    out_name = "images_to_pdf.pdf"
    out_path = _user_upload_dir() / out_name
    images[0].save(str(out_path), save_all=True, append_images=images[1:])

    _record_file(out_name, out_name, out_path.stat().st_size, "application/pdf")
    return jsonify({"ok": True, "name": out_name, "url": f"/uploads/{out_name}", "pages": len(images)})


# ==================== 辅助 ====================

def _record_file(uuid_name, original_name, size, mime_type, folder=None):
    """在数据库注册新生成的文件"""
    conn = get_db()
    cur = conn.cursor()
    folder_id = None
    if folder:
        cur.execute("SELECT id FROM folders WHERE name = %s", (folder,))
        row = cur.fetchone()
        if row:
            folder_id = row[0]
    cur.execute("""
        INSERT INTO files (uuid_name, original_name, folder_id, size, mime_type, owner_id, visibility, deleted)
        VALUES (%s,%s,%s,%s,%s,%s,'private',0)
    """, (uuid_name, original_name, folder_id, size, mime_type, session['user_id']))
    conn.commit()
    cur.close()
    conn.close()
