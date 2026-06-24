#!/usr/bin/env python3
"""compact_viewer — a read-only local web viewer for Claude Code session compact summaries.

Answers one question per session: "if I resume this, what's in context, and is it worth reusing?"

Run (installed):
    compact-viewer
Run (from source):
    python3 compact_viewer.py
Then open http://localhost:8765 (override the port with COMPACT_VIEWER_PORT).

No dependencies (Python stdlib only). Reads ~/.claude/projects/*/*.jsonl read-only;
never writes to or mutates your session transcripts.
"""
from __future__ import annotations

import json
import os
import re
import html
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

PROJECTS_DIR = os.path.expanduser("~/.claude/projects")
PORT = int(os.environ.get("COMPACT_VIEWER_PORT", "8765"))

# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

_CACHE: dict[str, tuple[float, dict]] = {}  # path -> (mtime, parsed)


def _text_of(message) -> str:
    """Pull plain text out of a message.content (str or list of blocks)."""
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    out = []
    if isinstance(content, list):
        for blk in content:
            if isinstance(blk, dict):
                if isinstance(blk.get("text"), str):
                    out.append(blk["text"])
            elif isinstance(blk, str):
                out.append(blk)
    return "\n".join(out)


def parse_file(path: str) -> dict:
    """Parse one session .jsonl into a compact summary record (cached by mtime)."""
    mtime = os.path.getmtime(path)
    cached = _CACHE.get(path)
    if cached and cached[0] == mtime:
        return cached[1]

    session_id = os.path.splitext(os.path.basename(path))[0]
    project = os.path.basename(os.path.dirname(path))

    ai_title = None
    slug = None
    branch = None
    cwd = None
    last_prompt = None
    first_user_text = None
    msg_count = 0
    last_ts = None
    version = None

    compact_metas: list[dict] = []   # from system entries (compactMetadata)
    compact_summaries: list[dict] = []  # from user entries (isCompactSummary)

    try:
        with open(path, "r", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except Exception:
                    continue

                t = o.get("type")
                ts = o.get("timestamp")
                if ts:
                    last_ts = ts

                if t == "ai-title" and o.get("aiTitle"):
                    ai_title = o["aiTitle"]
                if o.get("slug"):
                    slug = o["slug"]
                if o.get("gitBranch"):
                    branch = o["gitBranch"]
                if o.get("cwd"):
                    cwd = o["cwd"]
                if o.get("version"):
                    version = o["version"]
                if t == "last-prompt" and o.get("lastPrompt"):
                    last_prompt = o["lastPrompt"]

                if t in ("user", "assistant"):
                    msg_count += 1

                if t == "user" and o.get("isCompactSummary"):
                    compact_summaries.append({
                        "text": _text_of(o.get("message", {})),
                        "timestamp": ts,
                    })
                elif t == "user" and first_user_text is None and not o.get("isMeta"):
                    txt = _text_of(o.get("message", {}))
                    if txt and not txt.startswith("<"):
                        first_user_text = txt[:300]

                if o.get("compactMetadata"):
                    cm = o["compactMetadata"]
                    compact_metas.append({
                        "trigger": cm.get("trigger"),
                        "preTokens": cm.get("preTokens"),
                        "postTokens": cm.get("postTokens"),
                        "durationMs": cm.get("durationMs"),
                        "preservedUuids": (cm.get("preservedMessages") or {}).get("allUuids")
                                          or (cm.get("preservedMessages") or {}).get("uuids") or [],
                        "timestamp": ts,
                    })
    except FileNotFoundError:
        return {}

    # Pair metadata with summary text in order. Either list may be longer; zip defensively.
    compactions = []
    n = max(len(compact_metas), len(compact_summaries))
    for i in range(n):
        meta = compact_metas[i] if i < len(compact_metas) else {}
        summ = compact_summaries[i] if i < len(compact_summaries) else {}
        compactions.append({
            "index": i,
            "trigger": meta.get("trigger"),
            "preTokens": meta.get("preTokens"),
            "postTokens": meta.get("postTokens"),
            "durationMs": meta.get("durationMs"),
            "preservedCount": len(meta.get("preservedUuids") or []),
            "timestamp": meta.get("timestamp") or summ.get("timestamp"),
            "text": summ.get("text", ""),
        })

    title = ai_title or slug or (first_user_text[:80] if first_user_text else None) or session_id[:8]

    last_post = None
    for c in reversed(compactions):
        if c.get("postTokens") is not None:
            last_post = c["postTokens"]
            break

    rec = {
        "id": session_id,
        "project": project,
        "path": path,
        "title": title,
        "slug": slug,
        "branch": branch,
        "cwd": cwd,
        "version": version,
        "mtime": mtime,
        "last_ts": last_ts,
        "sizeBytes": os.path.getsize(path),
        "msgCount": msg_count,
        "numCompactions": len(compactions),
        "lastPostTokens": last_post,
        "lastPrompt": (last_prompt or first_user_text or "")[:280],
        "firstPrompt": (first_user_text or "")[:280],
        "compactions": compactions,
    }
    _CACHE[path] = (mtime, rec)
    return rec


def scan_sessions() -> list[dict]:
    out = []
    if not os.path.isdir(PROJECTS_DIR):
        return out
    for proj in os.listdir(PROJECTS_DIR):
        pdir = os.path.join(PROJECTS_DIR, proj)
        if not os.path.isdir(pdir):
            continue
        for fn in os.listdir(pdir):
            if not fn.endswith(".jsonl"):
                continue
            path = os.path.join(pdir, fn)
            try:
                rec = parse_file(path)
            except Exception:
                continue
            if not rec:
                continue
            # listing payload (no full summary text — keeps response small)
            out.append({k: rec[k] for k in (
                "id", "project", "title", "branch", "cwd", "mtime", "last_ts",
                "sizeBytes", "msgCount", "numCompactions", "lastPostTokens",
                "lastPrompt",
            )})
    out.sort(key=lambda r: r["mtime"], reverse=True)
    return out


def humanize_project(proj: str) -> str:
    # "-home-sg-code-Discovery" -> "Discovery"
    parts = proj.strip("-").split("-")
    return parts[-1] if parts else proj


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

PAGE = r"""<!doctype html>
<html><head><meta charset="utf-8"><title>Compact Viewer</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root{
  --bg:#0f1115; --panel:#171a21; --panel2:#1d2129; --border:#2a2f3a;
  --fg:#e6e9ef; --mut:#8b93a7; --acc:#7aa2f7; --good:#9ece6a; --warn:#e0af68; --bad:#f7768e;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
a{color:var(--acc);text-decoration:none}
.wrap{display:flex;height:100vh;overflow:hidden}
.list{width:46%;min-width:380px;border-right:1px solid var(--border);overflow:auto;background:var(--panel)}
.detail{flex:1;overflow:auto;padding:24px 28px}
header.top{position:sticky;top:0;z-index:5;background:var(--panel);padding:14px 16px;border-bottom:1px solid var(--border)}
header.top h1{font-size:15px;margin:0 0 8px}
.controls{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
input,select{background:var(--panel2);border:1px solid var(--border);color:var(--fg);border-radius:8px;padding:7px 10px;font-size:13px}
input.search{flex:1;min-width:160px}
.muted{color:var(--mut)}
.row{padding:12px 16px;border-bottom:1px solid var(--border);cursor:pointer}
.row:hover{background:var(--panel2)}
.row.active{background:var(--panel2);box-shadow:inset 3px 0 0 var(--acc)}
.row .t{font-weight:600;margin-bottom:3px}
.row .meta{display:flex;gap:10px;flex-wrap:wrap;font-size:12px;color:var(--mut)}
.badge{display:inline-block;padding:1px 7px;border-radius:999px;font-size:11px;border:1px solid var(--border);background:var(--bg)}
.badge.c0{color:var(--mut)}
.badge.c{color:var(--warn);border-color:#4a3f24}
.pill{font-size:11px;padding:1px 7px;border-radius:6px;background:var(--bg);border:1px solid var(--border);color:var(--mut)}
.prompt{font-size:12px;color:var(--mut);margin-top:5px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.detail h2{font-size:18px;margin:0 0 4px}
.kv{display:flex;gap:8px;flex-wrap:wrap;margin:10px 0 16px}
.cmd{background:#0a0c10;border:1px solid var(--border);border-radius:8px;padding:8px 12px;font-family:ui-monospace,Menlo,monospace;font-size:12.5px;display:flex;justify-content:space-between;align-items:center;gap:10px;margin-bottom:18px}
.cmd button{background:var(--panel2);border:1px solid var(--border);color:var(--fg);border-radius:6px;padding:4px 10px;cursor:pointer;font-size:12px}
.cmd button:hover{border-color:var(--acc)}
.comp{border:1px solid var(--border);border-radius:10px;margin-bottom:14px;overflow:hidden;background:var(--panel)}
.comp .h{padding:10px 14px;background:var(--panel2);display:flex;gap:10px;align-items:center;flex-wrap:wrap;cursor:pointer}
.comp .h .lbl{font-weight:600}
.comp .body{padding:0 16px;max-height:0;overflow:hidden;transition:max-height .2s ease}
.comp.open .body{max-height:none;padding:14px 16px}
.comp pre{white-space:pre-wrap;word-wrap:break-word;font:12.5px/1.6 ui-monospace,Menlo,monospace;margin:0;color:#d7dcea}
.arrow{transition:transform .15s}
.comp.open .arrow{transform:rotate(90deg)}
.delta{font-family:ui-monospace,monospace;font-size:12px}
.empty{color:var(--mut);padding:40px;text-align:center}
.tok-good{color:var(--good)} .tok-warn{color:var(--warn)} .tok-bad{color:var(--bad)}
.section-label{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--mut);margin:18px 0 8px;display:flex;align-items:center;gap:10px}
.section-label .sp{flex:1}
.linkbtn{background:none;border:0;color:var(--acc);cursor:pointer;font-size:11px;text-transform:none;letter-spacing:0}
.linkbtn:hover{text-decoration:underline}
hr{border:0;border-top:1px solid var(--border);margin:18px 0}
/* rendered summary */
.sum h4.sum-h{font-size:13.5px;color:var(--acc);margin:16px 0 6px;font-weight:600;scroll-margin-top:10px}
.sum h4.sum-h:first-child{margin-top:2px}
.sum p{margin:6px 0;color:#d2d7e3}
.sum ul{margin:6px 0 6px 4px;padding-left:18px}
.sum li{margin:3px 0;color:#d2d7e3}
.sum code{background:#0a0c10;border:1px solid var(--border);border-radius:4px;padding:0 5px;font:12px ui-monospace,Menlo,monospace;color:#a9d6ff}
.toc{display:flex;gap:6px;flex-wrap:wrap;margin:2px 0 14px}
.toc a{font-size:11.5px;background:var(--bg);border:1px solid var(--border);border-radius:999px;padding:3px 10px;color:var(--mut)}
.toc a:hover{color:var(--fg);border-color:var(--acc)}
.kbd{font:11px ui-monospace,monospace;background:var(--bg);border:1px solid var(--border);border-bottom-width:2px;border-radius:4px;padding:0 5px;color:var(--mut)}
.raw-pre{white-space:pre-wrap;word-wrap:break-word;font:12px/1.6 ui-monospace,Menlo,monospace;margin:0;color:#aeb6c7}
</style></head>
<body>
<div class="wrap">
  <div class="list">
    <header class="top">
      <h1>Compact Viewer <span class="muted" id="count"></span>
        <span class="muted" style="float:right;font-weight:400"><span class="kbd">j</span><span class="kbd">k</span> nav · <span class="kbd">/</span> search</span></h1>
      <div class="controls">
        <input class="search" id="q" placeholder="filter title / branch / prompt…">
        <select id="proj"><option value="">all projects</option></select>
        <select id="sort">
          <option value="mtime">recent</option>
          <option value="compactions">most compacted</option>
          <option value="size">largest</option>
        </select>
        <label class="muted" style="font-size:12px"><input type="checkbox" id="onlyc"> compacted only</label>
      </div>
    </header>
    <div id="rows"></div>
  </div>
  <div class="detail" id="detail"><div class="empty">Select a session on the left.</div></div>
</div>
<script>
let SESSIONS=[], VISIBLE=[], CUR=null, CUR_COMPS=[];
const $=s=>document.querySelector(s);
function fmtBytes(b){if(b>1e6)return (b/1e6).toFixed(1)+'MB';if(b>1e3)return (b/1e3).toFixed(0)+'KB';return b+'B';}
function fmtTok(n){if(n==null)return '—';return (n/1000).toFixed(1)+'k';}
function ago(ts){if(!ts)return '';const d=(Date.now()-ts*1000)/1000;if(d<3600)return Math.round(d/60)+'m ago';if(d<86400)return Math.round(d/3600)+'h ago';return Math.round(d/86400)+'d ago';}
function tokClass(n){if(n==null)return '';if(n<15000)return 'tok-good';if(n<40000)return 'tok-warn';return 'tok-bad';}
function esc(s){return (s||'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}

async function load(){
  SESSIONS=await (await fetch('/api/sessions')).json();
  const projs=[...new Set(SESSIONS.map(s=>s.project))].sort();
  const sel=$('#proj');
  projs.forEach(p=>{const o=document.createElement('option');o.value=p;o.textContent=p.replace(/^-/,'').split('-').pop();sel.appendChild(o);});
  render();
}
function render(){
  const q=$('#q').value.toLowerCase(), proj=$('#proj').value, sort=$('#sort').value, onlyc=$('#onlyc').checked;
  let rows=SESSIONS.filter(s=>{
    if(proj&&s.project!==proj)return false;
    if(onlyc&&!s.numCompactions)return false;
    if(q){const h=(s.title+' '+(s.branch||'')+' '+(s.lastPrompt||'')).toLowerCase();if(!h.includes(q))return false;}
    return true;
  });
  if(sort==='compactions')rows.sort((a,b)=>b.numCompactions-a.numCompactions||b.mtime-a.mtime);
  else if(sort==='size')rows.sort((a,b)=>b.sizeBytes-a.sizeBytes);
  else rows.sort((a,b)=>b.mtime-a.mtime);
  VISIBLE=rows;
  $('#count').textContent='· '+rows.length;
  $('#rows').innerHTML=rows.map(s=>{
    const cb=s.numCompactions?`<span class="badge c">${s.numCompactions}× compact</span>`:`<span class="badge c0">no compact</span>`;
    return `<div class="row ${CUR===s.id?'active':''}" onclick="open_('${s.id}')">
      <div class="t">${esc(s.title)}</div>
      <div class="meta">
        ${cb}
        ${s.branch?`<span class="pill">⎇ ${esc(s.branch)}</span>`:''}
        <span class="pill ${tokClass(s.lastPostTokens)}">ctx ${fmtTok(s.lastPostTokens)}</span>
        <span>${s.msgCount} msgs</span>
        <span>${fmtBytes(s.sizeBytes)}</span>
        <span>${ago(s.mtime)}</span>
      </div>
      ${s.lastPrompt?`<div class="prompt">${esc(s.lastPrompt)}</div>`:''}
    </div>`;
  }).join('')||'<div class="empty">No sessions match.</div>';
}
function mdInline(s){
  s=esc(s);
  s=s.replace(/`([^`]+)`/g,(m,p)=>'<code>'+p+'</code>');
  s=s.replace(/\*\*([^*]+)\*\*/g,(m,p)=>'<b>'+p+'</b>');
  return s;
}
// Turn a compact summary's numbered-outline text into readable HTML + a heading list.
function renderSummary(text,cid){
  const lines=(text||'').split('\n');
  let out='',inList=false,hi=0;const headings=[];
  const closeList=()=>{if(inList){out+='</ul>';inList=false;}};
  for(const raw of lines){
    const t=raw.trim();
    if(!t){closeList();continue;}
    let m;
    if((m=t.match(/^(\d+)\.\s+(.*)/))&&/[A-Za-z]/.test(m[2])){
      closeList();
      const id=cid+'-h'+(hi++);
      const title=m[2].replace(/:\s*$/,'');
      headings.push({id,title});
      out+=`<h4 id="${id}" class="sum-h">${mdInline(title)}</h4>`;
    }else if(m=t.match(/^[-*]\s+(.*)/)){
      if(!inList){out+='<ul>';inList=true;}
      out+=`<li>${mdInline(m[1])}</li>`;
    }else{
      closeList();
      out+=`<p>${mdInline(t)}</p>`;
    }
  }
  closeList();
  return {html:out,headings};
}
function toggleAll(open){document.querySelectorAll('#detail .comp').forEach(e=>e.classList.toggle('open',open));}
function flash(btn,msg){const o=btn.textContent;btn.textContent=msg;setTimeout(()=>btn.textContent=o,1200);}
function copyMd(i,btn){
  const c=CUR_COMPS[i];if(!c){return;}
  navigator.clipboard.writeText(c.text||'').then(()=>flash(btn,'copied ✓'),()=>flash(btn,'failed'));
}
function toggleRaw(btn){
  const body=btn.closest('.comp').querySelector('.body');
  const pre=body.querySelector('.raw-pre'),sum=body.querySelector('.sum');
  const showRaw=pre.style.display==='none'||!pre.style.display;
  pre.style.display=showRaw?'block':'none';sum.style.display=showRaw?'none':'block';
  btn.textContent=showRaw?'rendered':'raw';
}
async function open_(id){
  CUR=id;render();
  const d=await (await fetch('/api/session?id='+id)).json();
  CUR_COMPS=d.compactions||[];
  const resume=`claude --resume ${d.id}`;
  const lastIdx=(d.compactions||[]).length-1;
  let toc='';
  let comps=(d.compactions||[]).map((c,i)=>{
    const last=i===d.compactions.length-1;
    const cid='c'+i;
    const delta=(c.preTokens!=null&&c.postTokens!=null)?`<span class="delta">${fmtTok(c.preTokens)} → <b class="${tokClass(c.postTokens)}">${fmtTok(c.postTokens)}</b></span>`:'';
    const trig=c.trigger?`<span class="pill">${c.trigger}</span>`:'';
    const dur=c.durationMs?`<span class="muted">${(c.durationMs/1000).toFixed(0)}s</span>`:'';
    const pres=c.preservedCount?`<span class="muted">${c.preservedCount} msgs kept</span>`:'';
    const r=renderSummary(c.text,cid);
    if(last&&r.headings.length){
      toc=`<div class="toc">`+r.headings.map(h=>`<a href="javascript:void 0" onclick="document.getElementById('${h.id}').scrollIntoView({behavior:'smooth',block:'start'})">${esc(h.title)}</a>`).join('')+`</div>`;
    }
    const bodyHtml=r.html
      ? `<div class="sum">${r.html}</div><pre class="raw-pre" style="display:none">${esc(c.text)}</pre>`
      : `<pre class="raw-pre" style="display:block">${esc(c.text||'(no summary text captured)')}</pre><div class="sum" style="display:none"></div>`;
    return `<div class="comp ${last?'open':''}">
      <div class="h" onclick="if(event.target.tagName!=='BUTTON')this.parentNode.classList.toggle('open')">
        <span class="arrow">▶</span>
        <span class="lbl">Compaction #${c.index+1}${last?' · latest':''}</span>
        ${trig} ${delta} ${dur} ${pres}
        <span style="flex:1"></span>
        <button class="linkbtn" onclick="copyMd(${i},this)" title="Copy this summary as markdown">copy md</button>
        ${r.html?`<button class="linkbtn" onclick="toggleRaw(this)">raw</button>`:''}
      </div>
      <div class="body">${bodyHtml}</div>
    </div>`;
  }).join('');
  if(!d.compactions||!d.compactions.length){
    comps=`<div class="empty">This session has never been compacted — nothing summarized. Its full transcript is still intact.</div>`;
  }
  $('#detail').innerHTML=`
    <h2>${esc(d.title)}</h2>
    <div class="kv">
      ${d.branch?`<span class="pill">⎇ ${esc(d.branch)}</span>`:''}
      <span class="pill">${esc(d.project.replace(/^-/,'').split('-').pop())}</span>
      <span class="pill ${tokClass(d.lastPostTokens)}">current ctx ${fmtTok(d.lastPostTokens)}</span>
      <span class="pill">${d.msgCount} msgs</span>
      <span class="pill">${d.numCompactions}× compacted</span>
      <span class="pill">${fmtBytes(d.sizeBytes)}</span>
    </div>
    <div class="cmd"><span>${resume}</span>
      <span style="display:flex;gap:8px">
        ${lastIdx>=0?`<button onclick="copyMd(${lastIdx},this)" title="Copy the latest compact summary as markdown — paste into another agent">copy latest summary</button>`:''}
        <button onclick="navigator.clipboard.writeText('${resume}');flash(this,'copied ✓')">copy</button>
      </span></div>
    ${d.cwd?`<div class="muted" style="font-size:12px;margin-bottom:6px">cwd: ${esc(d.cwd)}</div>`:''}
    ${toc}
    <div class="section-label">Compaction history (latest = what you'd resume with)<span class="sp"></span>${(d.compactions&&d.compactions.length>1)?`<button class="linkbtn" onclick="toggleAll(true)">expand all</button><button class="linkbtn" onclick="toggleAll(false)">collapse all</button>`:''}</div>
    ${comps}`;
  $('#detail').scrollTop=0;
}
function move(delta){
  if(!VISIBLE.length)return;
  let i=VISIBLE.findIndex(s=>s.id===CUR);
  i=i<0?(delta>0?0:VISIBLE.length-1):Math.max(0,Math.min(VISIBLE.length-1,i+delta));
  const s=VISIBLE[i];
  open_(s.id);
  const el=document.querySelector('.row.active');
  if(el)el.scrollIntoView({block:'nearest'});
}
document.addEventListener('keydown',e=>{
  if(/^(INPUT|SELECT|TEXTAREA)$/.test(document.activeElement.tagName)){
    if(e.key==='Escape')document.activeElement.blur();
    return;
  }
  if(e.key==='j'){e.preventDefault();move(1);}
  else if(e.key==='k'){e.preventDefault();move(-1);}
  else if(e.key==='/'){e.preventDefault();$('#q').focus();}
});
$('#q').oninput=render;$('#proj').onchange=render;$('#sort').onchange=render;$('#onlyc').onchange=render;
load();
</script>
</body></html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/" or u.path == "/index.html":
            self._send(200, PAGE, "text/html; charset=utf-8")
        elif u.path == "/api/sessions":
            self._send(200, json.dumps(scan_sessions()))
        elif u.path == "/api/session":
            qs = parse_qs(u.query)
            sid = (qs.get("id") or [""])[0]
            # find file
            rec = None
            for proj in os.listdir(PROJECTS_DIR):
                path = os.path.join(PROJECTS_DIR, proj, sid + ".jsonl")
                if os.path.isfile(path):
                    rec = parse_file(path)
                    break
            if rec:
                self._send(200, json.dumps(rec))
            else:
                self._send(404, json.dumps({"error": "not found"}))
        else:
            self._send(404, json.dumps({"error": "not found"}))


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://localhost:{PORT}"
    print(f"compact_viewer serving {PROJECTS_DIR}")
    print(f"  -> {url}   (Ctrl-C to stop)")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
