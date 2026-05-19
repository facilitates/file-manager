"""Hermes 实时日志看板 — 嵌入 8081"""
import json, os, glob, time
from flask import Blueprint, jsonify, request, render_template_string

logview_bp = Blueprint("logview", __name__)
SESSION_DIR = os.path.expanduser("~/.hermes/sessions/")

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN" data-theme="dark">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>📡 Hermes 实时看板</title>
<style>
:root{--bg:#0b0e11;--s1:#161a1f;--s2:#1e2228;--b1:#2a2f36;--tx:#e2e6ea;--t2:#8b9199;--t3:#5c6269;--a1:#f0a040;--a2:rgba(240,160,64,.12);--g1:#43b581;--r1:#f04747}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Noto Sans SC",sans-serif;background:var(--bg);color:var(--tx);padding:20px;min-height:100vh}
h1{font-size:18px;margin-bottom:8px;display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.status{font-size:12px;padding:4px 12px;border-radius:12px;font-weight:600}
.status.idle{background:var(--s2);color:var(--t2)}
.status.working{background:var(--a2);color:var(--a1);animation:pulse 1.5s infinite}
.status.done{background:rgba(67,181,129,.15);color:var(--g1)}
@keyframes pulse{0%,100%{opacity:.6}50%{opacity:1}}
.sub{font-size:12px;color:var(--t3);margin-bottom:16px;display:flex;gap:16px;flex-wrap:wrap}
.stream{background:var(--s1);border:1px solid var(--b1);border-radius:10px;overflow:hidden;max-height:calc(100vh - 140px);overflow-y:auto}
.entry{padding:10px 14px;border-bottom:1px solid var(--b1);font-size:13px;line-height:1.5}
.entry:last-child{border-bottom:none}
.entry .time{font-size:10px;color:var(--t3);margin-bottom:2px;font-family:monospace}
.entry.user{border-left:3px solid var(--a1);background:var(--a2)}
.entry.assistant{border-left:3px solid #58a6ff;background:rgba(88,166,255,.05)}
.entry.tool{border-left:3px solid var(--g1);background:rgba(67,181,129,.05)}
.entry .label{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.05em;margin-right:6px;padding:1px 6px;border-radius:4px}
.entry.user .label{background:var(--a2);color:var(--a1)}
.entry.assistant .label{background:rgba(88,166,255,.15);color:#58a6ff}
.entry.tool .label{background:rgba(67,181,129,.15);color:var(--g1)}
.entry .content{color:var(--tx);white-space:pre-wrap;word-break:break-word;margin-top:4px}
.entry .tool-name{color:var(--t2);font-family:monospace;font-size:12px;margin-bottom:2px}
.entry .ok{color:var(--g1);font-weight:600;font-size:12px}
.entry .err{color:var(--r1);font-weight:600;font-size:12px}
.tool-result{font-size:11px;color:var(--t2);font-family:monospace;max-height:150px;overflow-y:auto;background:var(--bg);padding:8px;border-radius:6px;margin-top:4px}
.detail-toggle{color:var(--a1);cursor:pointer;font-size:11px;margin-top:4px;display:inline-block;user-select:none}
.detail-toggle:hover{text-decoration:underline}
.msg-preview{color:var(--t2);font-size:12px}
trunc{color:var(--t3)}
.nav{position:fixed;top:12px;right:12px;background:var(--s1);border:1px solid var(--b1);border-radius:8px;padding:8px 14px;font-size:12px;color:var(--a1);text-decoration:none;z-index:10}
.nav:hover{background:var(--a2)}
@media(max-width:600px){
  body{padding:12px}
  .stream{max-height:calc(100vh - 120px)}
}
</style></head>
<body>
<a class="nav" href="/">← 返回档案室</a>
<h1>📡 Hermes 实时看板 <span class="status idle">待命中</span></h1>
<div class="sub">
  <span>会话: <span id="sessName">-</span></span>
  <span>刷新: <span id="refreshCount">3</span>s</span>
  <span><span id="entryCount">0</span> 条</span>
</div>
<div class="stream" id="stream"><div style="text-align:center;padding:40px;color:var(--t3)">⏳ 加载中...</div></div>
<script>
var POLL=3000,lastTs='',sessionName='';
function h(s){return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')}
function ts(d){return new Date(d).toLocaleTimeString('zh-CN',{hour12:false})}
function fmtOutput(content){
  try{var o=JSON.parse(content);
    if(o.output){var s=o.output;return h(s.length>1500?s.substring(0,1500)+'\\n...':s)}
    return h(JSON.stringify(o).substring(0,1500));
  }catch(e){return h(content.substring(0,1500))}
}
async function poll(){
  try{
    var r=await fetch('/hermes-log/api?after='+encodeURIComponent(lastTs)+'&session='+encodeURIComponent(sessionName));
    var d=await r.json();
    sessionName=d.session||'';document.getElementById('sessName').textContent=sessionName||'-';
    if(d.entries&&d.entries.length){
      var s=document.getElementById('stream');
      if(lastTs==='')s.innerHTML='';
      d.entries.forEach(function(e){
        var div=document.createElement('div');div.className='entry '+e.role;
        var html='<div class="time">'+ts(e.ts)+'</div>';
        if(e.role==='tool'){
          html+='<div class="tool-name"><span class="label">TOOL</span> '+h(e.tool||'')+'</div>';
          html+='<div class="'+(e.ok?'ok':'err')+'">'+(e.ok?'✅ 完成':'❌ 失败')+'</div>';
          if(e.content){var cid='tc_'+Math.random().toString(36).slice(2);
            html+='<span class="detail-toggle" onclick="var el=document.getElementById(\\''+cid+'\\');el.style.display=el.style.display===\\'none\\'?\\'block\\':\\'none\\'">📋 详情</span>';
            html+='<div class="tool-result" id="'+cid+'" style="display:none">'+fmtOutput(e.content)+'</div>';}
        }else if(e.role==='assistant'){
          html+='<span class="label">AI</span>';
          if(e.reasoning){var rid='rr_'+Math.random().toString(36).slice(2);
            html+='<div class="msg-preview">'+h(e.reasoning.substring(0,120))+'…</div>';
            html+='<span class="detail-toggle" onclick="var el=document.getElementById(\\''+rid+'\\');el.style.display=el.style.display===\\'none\\'?\\'block\\':\\'none\\'">🧠 展开推理</span>';
            html+='<div class="tool-result" id="'+rid+'" style="display:none">'+h(e.reasoning)+'</div>';}
          html+='<div class="content">'+h(e.content.length>300?e.content.substring(0,300)+'… <trunc>('+e.content.length+'字)</trunc>':e.content)+'</div>';
        }else if(e.role==='user'){
          html+='<span class="label">YOU</span><div class="content">'+h(e.content||'')+'</div>';
        }else{
          html+='<span class="label">'+e.role.toUpperCase()+'</span><div class="content">'+h((e.content||'').substring(0,500))+'</div>';
        }
        div.innerHTML=html;s.appendChild(div);
      });
      var last=d.entries[d.entries.length-1];lastTs=last.ts;
      s.scrollTop=s.scrollHeight;
      document.getElementById('entryCount').textContent=s.children.length;
      var st=document.querySelector('.status');
      st.className='status ';
      if(last.role==='user'){st.className+='working';st.textContent='思考中…'}
      else if(last.role==='assistant'&&last.reasoning){st.className+='working';st.textContent='🧠 推理中'}
      else if(last.role==='tool'){st.className+='working';st.textContent='🔧 '+h(last.tool||'')}
      else if(last.role==='assistant'){st.className+='done';st.textContent='✅ 已回复'}
      else{st.className+='idle';st.textContent='待命中'}
    }
  }catch(e){}
  var cd=3;
  var iv=setInterval(function(){cd--;document.getElementById('refreshCount').textContent=cd;if(cd<=0)clearInterval(iv)},1000);
  setTimeout(poll,POLL);
}
poll();
</script>
</body></html>"""


def _find_latest():
    files = glob.glob(os.path.join(SESSION_DIR, "*.jsonl"))
    if not files:
        return None, None
    latest = max(files, key=os.path.getmtime)
    return os.path.basename(latest).replace(".jsonl", ""), latest


def _extract_tool(entry):
    calls = entry.get("tool_calls", [])
    if calls:
        for c in calls:
            if isinstance(c, dict):
                return c.get("function", {}).get("name", "")
    return entry.get("tool", entry.get("tool_name", ""))


@logview_bp.route("/hermes-log")
def log_page():
    return render_template_string(HTML_TEMPLATE)


@logview_bp.route("/hermes-log/api")
def log_api():
    after = request.args.get("after", "")
    sess_name = request.args.get("session", "")

    if sess_name:
        path = os.path.join(SESSION_DIR, sess_name + ".jsonl")
    else:
        sess_name, path = _find_latest()

    if not path or not os.path.exists(path):
        return jsonify({"entries": [], "session": ""})

    entries = []
    with open(path, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            ts = entry.get("timestamp", "")
            if after and ts <= after:
                continue

            role = entry.get("role", "unknown")
            content = entry.get("content", "") or ""

            result = {"ts": ts, "role": role, "content": content[:2000]}

            if role == "assistant":
                reasoning = entry.get("reasoning_content", entry.get("reasoning", ""))
                result["reasoning"] = reasoning[:2000] if reasoning else ""

            if role == "tool":
                result["tool"] = _extract_tool(entry)
                # Determine success
                txt = content.lower()
                result["ok"] = not (
                    "error" in txt
                    or "fail" in txt
                    or "exception" in txt
                    or '"exit_code": 1' in content
                )

            entries.append(result)

    return jsonify({
        "entries": entries,
        "session": sess_name,
    })
