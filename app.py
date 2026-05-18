import os
import uuid
import base64
import mimetypes
import subprocess
import requests
from datetime import datetime
from pathlib import Path

from flask import Flask, request, jsonify, send_from_directory, render_template_string

app = Flask(__name__)

UPLOAD_DIR = Path(__file__).parent / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)
MAX_SIZE = 200 * 1024 * 1024

# 豆包 API 配置
DOUBAO_KEY = "ark-6e3d9b8f-4b25-4b4a-8b40-6c0e7f386bb5-31b66"
DOUBAO_BASE = "https://ark.cn-beijing.volces.com/api/v3"
DOUBAO_MODEL = "ep-20260518223807-r9sqp"

CLASSIFY_PROMPT = """用1-2个中文字给这张图一个分类名，从以下选：
猫, 狗, 动物, 风景, 美食, 人物, 截图, 表情包, 动漫, 
文档, 代码, 设计, 自拍, 萌宠, 植物, 建筑, 车

如果都不匹配，给一个2字内的分类名。
只回复1-2个字，不要标点不要解释。"""

def safe_filename(name):
    return name.replace("..", "").replace("/", "_").replace("\\", "_")

def get_folders():
    folders = []
    for d in sorted(UPLOAD_DIR.iterdir()):
        if d.is_dir():
            count = sum(1 for f in d.iterdir() if f.is_file())
            folders.append({"name": d.name, "count": count})
    return folders

def get_files(folder=""):
    base = UPLOAD_DIR / folder if folder else UPLOAD_DIR
    if not base.exists():
        return []
    files = []
    for f in sorted(base.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
        if f.is_file():
            stat = f.stat()
            mime, _ = mimetypes.guess_type(f.name)
            is_image = bool(mime and mime.startswith("image/"))
            is_video = bool(mime and mime.startswith("video/"))
            rel = f"{folder}/{f.name}" if folder else f.name
            files.append({
                "name": f.name, "folder": folder,
                "size": stat.st_size,
                "mtime": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                "type": mime or "unknown",
                "is_image": is_image, "is_video": is_video,
                "url": f"/uploads/{rel}"
            })
    return files

def ai_classify_image(image_path):
    """用豆包视觉 API 对图片分类"""
    try:
        with open(image_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        
        resp = requests.post(
            f"{DOUBAO_BASE}/chat/completions",
            headers={
                "Authorization": f"Bearer {DOUBAO_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": DOUBAO_MODEL,
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                        {"type": "text", "text": CLASSIFY_PROMPT}
                    ]
                }],
                "max_tokens": 10,
                "temperature": 0.1
            },
            timeout=30
        )
        result = resp.json()
        category = result["choices"][0]["message"]["content"].strip()
        # 清理：去掉标点、换行
        category = category.replace("\n", "").replace("。", "").replace("，", "").replace("、", "").replace(",", "").replace(" ", "")
        return safe_filename(category) or "其他"
    except Exception as e:
        print(f"[AI分类错误] {e}")
        return None

def ai_classify_video(video_path):
    """提取视频首帧，用 AI 分类"""
    try:
        frame_path = video_path + ".frame.jpg"
        subprocess.run([
            "ffmpeg", "-y", "-i", str(video_path),
            "-vframes", "1", "-q:v", "3",
            frame_path
        ], capture_output=True, timeout=15)
        if os.path.exists(frame_path) and os.path.getsize(frame_path) > 100:
            cat = ai_classify_image(frame_path)
            os.remove(frame_path)
            return cat
    except Exception as e:
        print(f"[视频分类错误] {e}")
    return "视频"

def ai_classify(filepath):
    """自动分类文件，返回文件夹名"""
    mime, _ = mimetypes.guess_type(str(filepath))
    if mime and mime.startswith("image/"):
        return ai_classify_image(filepath)
    elif mime and mime.startswith("video/"):
        return ai_classify_video(filepath)
    return None

# ====== 路由 ======

@app.route("/")
def index():
    return render_template_string(HTML, folders=get_folders())

@app.route("/uploads/<path:filepath>")
def serve_upload(filepath):
    path = Path(filepath)
    folder = str(path.parent) if path.parent != Path(".") else ""
    return send_from_directory(UPLOAD_DIR / folder, path.name)

@app.route("/api/folders")
def list_folders():
    return jsonify({"folders": get_folders()})

@app.route("/api/folders", methods=["POST"])
def create_folder():
    data = request.get_json() or {}
    name = safe_filename(data.get("name", "").strip())
    if not name:
        return jsonify({"error": "name required"}), 400
    (UPLOAD_DIR / name).mkdir(exist_ok=True)
    return jsonify({"ok": True, "name": name})

@app.route("/api/folders/<name>", methods=["DELETE"])
def delete_folder(name):
    path = UPLOAD_DIR / safe_filename(name)
    if not path.exists() or not path.is_dir():
        return jsonify({"error": "not found"}), 404
    if any(path.iterdir()):
        return jsonify({"error": "folder not empty"}), 400
    path.rmdir()
    return jsonify({"ok": True})

@app.route("/api/files")
def list_files():
    folder = request.args.get("folder", "").strip()
    return jsonify({"files": get_files(folder), "folder": folder})

@app.route("/api/upload", methods=["POST"])
def upload():
    folder = safe_filename(request.form.get("folder", "").strip())
    use_ai = request.form.get("ai", "0") == "1"
    
    dest = UPLOAD_DIR / folder if folder else UPLOAD_DIR
    dest.mkdir(exist_ok=True)

    if "file" not in request.files:
        return jsonify({"error": "no file"}), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "empty filename"}), 400

    ext = Path(f.filename).suffix or ""
    safe_name = f"{uuid.uuid4().hex[:8]}{ext}"
    save_path = dest / safe_name
    f.save(save_path)

    mime, _ = mimetypes.guess_type(safe_name)
    ai_folder = None

    if use_ai:
        cat = ai_classify(save_path)
        if cat and cat != folder:
            ai_folder = cat
            new_dest = UPLOAD_DIR / cat
            new_dest.mkdir(exist_ok=True)
            new_path = new_dest / safe_name
            save_path.rename(new_path)
            save_path = new_path

    rel = f"{ai_folder}/{safe_name}" if ai_folder else (f"{folder}/{safe_name}" if folder else safe_name)
    return jsonify({
        "name": safe_name,
        "original": f.filename,
        "folder": ai_folder or folder,
        "url": f"/uploads/{rel}",
        "is_image": bool(mime and mime.startswith("image/")),
        "ai_classified": ai_folder is not None,
        "ai_category": ai_folder
    })

@app.route("/api/classify", methods=["POST"])
def classify_existing():
    data = request.get_json() or {}
    filepath = data.get("path", "")
    if not filepath:
        return jsonify({"error": "path required"}), 400
    full = UPLOAD_DIR / filepath
    if not full.exists():
        return jsonify({"error": "not found"}), 404
    cat = ai_classify(full)
    return jsonify({"category": cat})

@app.route("/api/download/<path:filepath>")
def download_file(filepath):
    path = Path(filepath)
    folder = str(path.parent) if path.parent != Path(".") else ""
    filename = path.name
    full = UPLOAD_DIR / folder / filename
    if not full.exists():
        return jsonify({"error": "not found"}), 404
    return send_from_directory(full.parent, filename, as_attachment=True, download_name=filename)

@app.route("/api/files/<path:filepath>", methods=["DELETE"])
def delete_file(filepath):
    path = Path(filepath)
    folder = str(path.parent) if path.parent != Path(".") else ""
    full = UPLOAD_DIR / folder / path.name
    if not full.exists():
        return jsonify({"error": "not found"}), 404
    full.unlink()
    return jsonify({"ok": True})

# ====== 前端 ======
HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>📁 文件管理</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:#0d0d0d;color:#d0d0d0;min-height:100vh;display:flex}
.sidebar{width:220px;background:#141414;border-right:1px solid #222;display:flex;flex-direction:column;flex-shrink:0;height:100vh;position:sticky;top:0}
.sidebar-header{padding:16px;border-bottom:1px solid #222;font-weight:700;font-size:16px}
.sidebar-folders{flex:1;overflow-y:auto;padding:8px}
.sidebar-folders .folder{padding:8px 12px;border-radius:6px;cursor:pointer;display:flex;align-items:center;gap:8px;font-size:13px;transition:all .15s;margin-bottom:2px}
.sidebar-folders .folder:hover{background:#1e1e1e}
.sidebar-folders .folder.active{background:#1a3a5c;color:#6ab0f3}
.sidebar-folders .folder .count{font-size:11px;color:#666;margin-left:auto}
.sidebar-folders .folder.active .count{color:#4a8fcc}
.sidebar-add-folder{margin:8px;display:flex;gap:6px}
.sidebar-add-folder input{flex:1;background:#1a1a1a;border:1px solid #333;border-radius:6px;padding:6px 10px;color:#ccc;font-size:12px;outline:none}
.sidebar-add-folder input:focus{border-color:#4a90d9}
.sidebar-add-folder button{background:#2a2a2a;border:none;border-radius:6px;color:#ccc;padding:6px 10px;cursor:pointer;font-size:12px}
.sidebar-add-folder button:hover{background:#3a3a3a}
.main{flex:1;display:flex;flex-direction:column;min-width:0}
.header{background:#141414;padding:12px 20px;border-bottom:1px solid #222;display:flex;align-items:center;gap:10px;flex-shrink:0}
.header .breadcrumb{font-size:13px;color:#888;display:flex;gap:4px;align-items:center}
.header .breadcrumb a{color:#6ab0f3;text-decoration:none;cursor:pointer}
.upload-zone{margin:16px 20px;border:2px dashed #2a2a2a;border-radius:10px;padding:28px;text-align:center;cursor:pointer;transition:all .15s;flex-shrink:0}
.upload-zone:hover,.upload-zone.dragover{border-color:#4a90d9;background:rgba(74,144,217,.04)}
.upload-zone p{color:#777;font-size:13px}
.upload-zone .icon{font-size:32px;margin-bottom:8px}
.upload-zone .ai-badge{display:inline-block;background:#1a3a5c;color:#6ab0f3;padding:2px 10px;border-radius:10px;font-size:11px;margin-top:8px;cursor:pointer;user-select:none;transition:all .15s}
.upload-zone .ai-badge.off{background:#2a1515;color:#d55}
#fileInput{display:none}
.toolbar{padding:0 20px 12px;display:flex;align-items:center;gap:8px;flex-shrink:0}
.toolbar .count{color:#666;font-size:12px;margin-left:auto}
.grid{flex:1;overflow-y:auto;display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:10px;padding:0 20px 20px;align-content:start}
.card{background:#1a1a1a;border-radius:8px;overflow:hidden;border:1px solid #252525;transition:all .12s}
.card:hover{border-color:#3a3a3a;transform:translateY(-1px)}
.card .preview{height:130px;display:flex;align-items:center;justify-content:center;background:#111;overflow:hidden;position:relative}
.card .preview img{max-width:100%;max-height:100%;object-fit:cover}
.card .preview video{max-width:100%;max-height:100%}
.card .preview .file-icon{font-size:42px;opacity:.4}
.card .preview .play-icon{position:absolute;font-size:28px;opacity:.7;pointer-events:none}
.card .info{padding:8px 10px}
.card .name{font-size:11px;color:#bbb;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-bottom:3px}
.card .meta{font-size:10px;color:#555;display:flex;justify-content:space-between}
.card .ai-tag{font-size:9px;background:#1a3520;color:#6a6;padding:1px 6px;border-radius:4px}
.card .actions{display:flex;padding:0 10px 8px;gap:3px}
.card .actions button{flex:1;padding:5px 0;border:none;border-radius:5px;cursor:pointer;font-size:10px;transition:all .12s}
.btn-copy{background:#1e1e1e;color:#aaa}
.btn-copy:hover{background:#2e2e2e}
.btn-down{background:#162216;color:#7a7}
.btn-down:hover{background:#223322}
.btn-del{background:#221515;color:#d55}
.btn-del:hover{background:#332020}
.empty{text-align:center;padding:60px;color:#444;grid-column:1/-1}
.empty .icon{font-size:50px;margin-bottom:10px}
.toast{position:fixed;bottom:20px;left:50%;transform:translateX(-50%);background:#2a2a2a;color:#ddd;padding:8px 18px;border-radius:8px;font-size:12px;z-index:99;animation:toastIn .2s}
@keyframes toastIn{from{opacity:0;transform:translateX(-50%) translateY(8px)}to{opacity:1;transform:translateX(-50%) translateY(0)}}
.modal-overlay{position:fixed;inset:0;background:rgba(0,0,0,.6);display:flex;align-items:center;justify-content:center;z-index:100}
.modal{background:#1e1e1e;border-radius:12px;padding:20px;max-width:90vw;max-height:90vh}
.modal img,.modal video{max-width:80vw;max-height:80vh;border-radius:6px}
@media(max-width:600px){.sidebar{display:none}.grid{grid-template-columns:repeat(auto-fill,minmax(140px,1fr))}}
</style>
</head>
<body>

<div class="sidebar">
  <div class="sidebar-header">📁 文件夹</div>
  <div class="sidebar-folders" id="folderList">
    <div class="folder active" onclick="selectFolder('')" data-folder="">
      📂 全部文件
    </div>
    {% for f in folders %}
    <div class="folder" onclick="selectFolder('{{ f.name }}')" data-folder="{{ f.name }}">
      📁 {{ f.name }} <span class="count">{{ f.count }}</span>
    </div>
    {% endfor %}
  </div>
  <div class="sidebar-add-folder">
    <input type="text" id="newFolderInput" placeholder="新建文件夹..." onkeydown="if(event.key==='Enter')addFolder()">
    <button onclick="addFolder()">+</button>
  </div>
</div>

<div class="main">
  <div class="header">
    <span style="font-weight:600;font-size:15px">📁 文件管理</span>
    <span style="color:#444">|</span>
    <div class="breadcrumb" id="breadcrumb"><a onclick="selectFolder('')">根目录</a></div>
  </div>

  <div class="upload-zone" id="dropZone">
    <div class="icon" id="uploadIcon">📤</div>
    <p id="uploadText">拖拽文件到这里 或 点击上传</p>
    <p style="font-size:11px;margin-top:4px;color:#444">图片 · 视频 · 文档，最大 200MB</p>
    <div class="ai-badge" id="aiToggle" onclick="toggleAI(event)">🤖 AI 分类：开</div>
  </div>
  <input type="file" id="fileInput" multiple>

  <div class="toolbar">
    <span class="count" id="fileCount"></span>
  </div>

  <div class="grid" id="fileGrid"></div>
</div>

<div class="modal-overlay" id="previewModal" style="display:none" onclick="closePreview()">
  <div class="modal" id="previewContent" onclick="event.stopPropagation()"></div>
</div>

<script>
let currentFolder = '';
let aiEnabled = true;
const BASE = window.location.origin;

const dz = document.getElementById('dropZone');
const inp = document.getElementById('fileInput');
const grid = document.getElementById('fileGrid');
const cnt = document.getElementById('fileCount');
const bc = document.getElementById('breadcrumb');

dz.onclick = () => inp.click();
dz.ondragover = e => { e.preventDefault(); dz.classList.add('dragover'); };
dz.ondragleave = () => dz.classList.remove('dragover');
dz.ondrop = e => { e.preventDefault(); dz.classList.remove('dragover'); uploadFiles(e.dataTransfer.files); };
inp.onchange = () => { uploadFiles(inp.files); inp.value = ''; };

function toggleAI(e) {
  e.stopPropagation();
  aiEnabled = !aiEnabled;
  const badge = document.getElementById('aiToggle');
  if (aiEnabled) {
    badge.className = 'ai-badge';
    badge.textContent = '🤖 AI 分类：开';
  } else {
    badge.className = 'ai-badge off';
    badge.textContent = '📋 AI 分类：关';
  }
}

async function uploadFiles(files) {
  const icon = document.getElementById('uploadIcon');
  const text = document.getElementById('uploadText');
  
  let results = [];
  for (const f of files) {
    icon.textContent = aiEnabled ? '🔍' : '⏳';
    text.textContent = aiEnabled ? `AI 识别中: ${f.name}` : `上传中: ${f.name}`;
    
    const fd = new FormData();
    fd.append('file', f);
    if (!aiEnabled && currentFolder) fd.append('folder', currentFolder);
    if (aiEnabled) fd.append('ai', '1');
    
    try {
      const res = await fetch('/api/upload', { method: 'POST', body: fd });
      const data = await res.json();
      results.push(data);
    } catch(e) { toast('❌ ' + f.name); }
  }
  
  icon.textContent = '📤';
  text.textContent = '拖拽文件到这里 或 点击上传';
  
  if (aiEnabled && results.some(r => r.ai_classified)) {
    const cats = [...new Set(results.filter(r=>r.ai_category).map(r=>r.ai_category))];
    toast('🤖 AI 已分类 → ' + cats.join(', '));
  } else {
    toast('✅ 完成');
  }
  
  loadFiles();
  loadFolders();
}

function selectFolder(name) {
  currentFolder = name;
  document.querySelectorAll('.folder').forEach(el => el.classList.remove('active'));
  const el = document.querySelector(`[data-folder="${name}"]`);
  if (el) el.classList.add('active');
  bc.innerHTML = name
    ? `<a onclick="selectFolder('')">根目录</a> <span style="color:#555">›</span> <span>${name}</span>`
    : `<a onclick="selectFolder('')">根目录</a>`;
  loadFiles();
}

async function addFolder() {
  const input = document.getElementById('newFolderInput');
  const name = input.value.trim();
  if (!name) return;
  await fetch('/api/folders', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({name}) });
  input.value = '';
  loadFolders();
}

async function loadFolders() {
  const res = await fetch('/api/folders');
  const data = await res.json();
  const list = document.getElementById('folderList');
  list.innerHTML = `<div class="folder${currentFolder===''?' active':''}" onclick="selectFolder('')" data-folder="">📂 全部文件</div>`;
  for (const f of data.folders) {
    list.innerHTML += `<div class="folder${currentFolder===f.name?' active':''}" onclick="selectFolder('${f.name}')" data-folder="${f.name}">📁 ${f.name} <span class="count">${f.count}</span></div>`;
  }
}

async function loadFiles() {
  const q = currentFolder ? '?folder=' + encodeURIComponent(currentFolder) : '';
  const res = await fetch('/api/files' + q);
  const data = await res.json();
  cnt.textContent = `共 ${data.files.length} 个文件`;

  if (!data.files.length) {
    grid.innerHTML = '<div class="empty"><div class="icon">📭</div><p>这里还没有文件～</p></div>';
    return;
  }

  grid.innerHTML = data.files.map(f => {
    let preview, badge = '';
    if (f.is_image) preview = `<img src="${f.url}" loading="lazy" onclick="preview('${f.url}','image')">`;
    else if (f.is_video) preview = `<div class="file-icon" onclick="preview('${f.url}','video')">🎬</div><div class="play-icon">▶</div>`;
    else preview = `<div class="file-icon">📄</div>`;
    if (f.folder) badge = `<span class="ai-tag">📁 ${f.folder}</span>`;

    const rel = f.folder ? f.folder + '/' + f.name : f.name;
    return `
    <div class="card">
      <div class="preview">${preview}</div>
      <div class="info">
        <div class="name" title="${f.name}">${f.name}</div>
        <div class="meta">${badge}<span>${fmtSize(f.size)}</span><span>${new Date(f.mtime).toLocaleDateString('zh-CN')}</span></div>
      </div>
      <div class="actions">
        <button class="btn-copy" onclick="copyLink('${BASE+f.url}')">🔗</button>
        <button class="btn-down" onclick="downloadFile('${rel}')">⬇</button>
        <button class="btn-del" onclick="delFile('${rel}')">🗑</button>
      </div>
    </div>`;
  }).join('');
}

function preview(url, type) {
  const modal = document.getElementById('previewModal');
  const content = document.getElementById('previewContent');
  content.innerHTML = type === 'image'
    ? `<img src="${url}">`
    : `<video src="${url}" controls autoplay style="max-width:80vw;max-height:80vh"></video>`;
  modal.style.display = 'flex';
}
function closePreview() {
  document.getElementById('previewModal').style.display = 'none';
  const v = document.querySelector('#previewContent video');
  if (v) v.pause();
}
async function delFile(rel) {
  if (!confirm('确定删除 ' + rel + '？')) return;
  await fetch('/api/files/' + rel, { method:'DELETE' });
  toast('已删除');
  loadFiles(); loadFolders();
}
function copyLink(url) {
  navigator.clipboard.writeText(url).then(() => toast('✅ 已复制'));
}
function downloadFile(rel) {
  const a = document.createElement('a');
  a.href = '/api/download/' + rel;
  a.download = rel.split('/').pop();
  document.body.appendChild(a); a.click(); document.body.removeChild(a);
  toast('⬇ 下载中');
}
function fmtSize(b) {
  if (b<1024) return b+'B';
  if (b<1048576) return (b/1024).toFixed(1)+'KB';
  if (b<1073741824) return (b/1048576).toFixed(1)+'MB';
  return (b/1073741824).toFixed(1)+'GB';
}
function toast(msg) {
  const t = document.createElement('div'); t.className='toast'; t.textContent=msg;
  document.body.appendChild(t); setTimeout(()=>t.remove(),2000);
}

loadFiles();
loadFolders();
</script>
</body>
</html>"""

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
