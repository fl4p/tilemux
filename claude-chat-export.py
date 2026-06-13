#!/usr/bin/env python3
import json, os, re, glob, datetime, collections

PROJECTS_ROOT = os.environ.get("CLAUDE_PROJECTS_ROOT") or os.path.expanduser("~/.claude/projects")
OUT_ROOT = os.environ.get("CHAT_HISTORY_DIR") or os.path.expanduser("~/claude-chat-history")
# Sandboxed claude (claude-box) writes transcripts to <project>/.claude/projects
# instead of PROJECTS_ROOT. This registry holds one symlink per sandboxed
# project, pointing at its .claude dir, so those sessions are discoverable.
CONTAINERS_ROOT = os.environ.get("CLAUDE_CONTAINERS_ROOT") or ""

def strip_noise(s):
    s = re.sub(r"<system-reminder>.*?</system-reminder>", "", s, flags=re.S)
    s = re.sub(r"<local-command-stdout>.*?</local-command-stdout>", "", s, flags=re.S)
    s = re.sub(r"<command-(name|message|args)>.*?</command-\1>", "", s, flags=re.S)
    return s.strip()

def user_text(content):
    if isinstance(content, str):
        return strip_noise(content)
    out = []
    if isinstance(content, list):
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                out.append(strip_noise(b.get("text", "")))
    return "\n".join(t for t in out if t).strip()

def asst_text(content):
    out = []
    if isinstance(content, list):
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                t = b.get("text", "").strip()
                if t:
                    out.append(t)
    return "\n\n".join(out).strip()

def detect_cwd(jsonl_files):
    for fp in jsonl_files:
        try:
            with open(fp) as f:
                for line in f:
                    try:
                        o = json.loads(line)
                    except Exception:
                        continue
                    cwd = o.get("cwd")
                    if cwd:
                        return cwd
        except Exception:
            continue
    return None

HTML_TMPL = """<!doctype html>
<html lang=en>
<meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
<style>
  :root{color-scheme:light dark}
  body{font:15px/1.55 -apple-system,system-ui,sans-serif;max-width:900px;margin:0 auto;padding:1.5rem}
  h1{font-size:1.3rem;margin:0 0 .2rem}
  .sub{opacity:.6;margin:0 0 1rem}
  #q{width:100%;box-sizing:border-box;padding:.6rem .8rem;font-size:1rem;border:1px solid #8888;border-radius:8px}
  #count{opacity:.6;font-size:.85rem;margin:.7rem 0 .3rem}
  .item{padding:.55rem 0;border-bottom:1px solid #8882}
  .item a{font-weight:600;text-decoration:none}
  .item a:hover{text-decoration:underline}
  .proj{display:inline-block;font-size:.72rem;background:#8883;border-radius:5px;padding:.05rem .45rem;margin-left:.35rem}
  .meta{opacity:.55;font-size:.8rem;margin-left:.35rem}
  .snip{font-size:.85rem;opacity:.85;margin-top:.3rem;white-space:pre-wrap;word-break:break-word}
  mark{background:#fd0;color:#000;border-radius:2px;padding:0 1px}
</style>
<h1>__TITLE__</h1>
<p class=sub>__SUB__</p>
<input id=q type=search placeholder="Full-text search chat content…" autofocus>
<div id=count></div>
<div id=out></div>
<script>
const DATA=__DATA__;
// pre-lowercase once at load so each keystroke is a plain includes(), not a re-lowercasing of the whole corpus
for(const e of DATA){e.xl=e.x.toLowerCase();e.tl=e.t.toLowerCase();}
const q=document.getElementById('q'),out=document.getElementById('out'),count=document.getElementById('count');
const esc=s=>s.replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
function snippet(text,lc,qlc){
  const i=lc.indexOf(qlc);
  if(i<0)return '';
  const a=Math.max(0,i-60),b=Math.min(text.length,i+qlc.length+110);
  return (a>0?'…':'')+esc(text.slice(a,i))+'<mark>'+esc(text.slice(i,i+qlc.length))+'</mark>'+esc(text.slice(i+qlc.length,b))+(b<text.length?'…':'');
}
function render(){
  const query=q.value.trim(),qlc=query.toLowerCase();
  let rows=DATA;
  if(qlc)rows=DATA.filter(e=>e.xl.includes(qlc)||e.tl.includes(qlc));
  count.textContent=rows.length+(qlc?' of '+DATA.length+' sessions match "'+query+'"':' sessions');
  out.innerHTML=rows.map(e=>{
    const proj=e.p?'<span class=proj>'+esc(e.p)+'</span>':'';
    const snip=qlc?snippet(e.x,e.xl,qlc):'';
    return '<div class=item><a href="'+encodeURI(e.f)+'">'+esc(e.d)+' — '+esc(e.t)+'</a>'+proj+'<span class=meta>'+e.n+' turns</span>'+(snip?'<div class=snip>'+snip+'</div>':'')+'</div>';
  }).join('');
}
let timer;
q.addEventListener('input',()=>{clearTimeout(timer);timer=setTimeout(render,150);});
render();
</script>
"""

def write_html(path, title, sub, data):
    blob = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    html = (HTML_TMPL.replace("__TITLE__", title)
                     .replace("__SUB__", sub)
                     .replace("__DATA__", blob))
    with open(path, "w") as f:
        f.write(html)

def process_project(src_dir, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    files = sorted(glob.glob(os.path.join(src_dir, "*.jsonl")))
    made = 0
    skipped = 0
    index = []
    entries = []
    for fp in files:
        sid = os.path.splitext(os.path.basename(fp))[0]
        turns = []
        title = None
        start_ts = None
        with open(fp) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                t = o.get("type")
                if t == "ai-title" and not title:
                    title = o.get("title") or o.get("aiTitle") or o.get("message")
                if t not in ("user", "assistant"):
                    continue
                if o.get("isSidechain"):
                    continue
                ts = o.get("timestamp")
                if ts and not start_ts:
                    start_ts = ts
                msg = o.get("message", {})
                if not isinstance(msg, dict):
                    continue
                content = msg.get("content")
                if t == "user":
                    txt = user_text(content)
                    if txt:
                        turns.append(("user", ts, txt))
                else:
                    txt = asst_text(content)
                    if txt:
                        turns.append(("assistant", ts, txt))
        if not turns:
            skipped += 1
            continue
        date = "unknown"
        if start_ts:
            try:
                date = datetime.datetime.fromisoformat(start_ts.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d_%H%M")
            except Exception:
                pass
        fname = f"{date}__{sid[:8]}.md"
        path = os.path.join(out_dir, fname)
        title = title or "(untitled session)"
        lines = []
        lines.append(f"# {title}")
        lines.append("")
        lines.append(f"*Session `{sid}` — started {start_ts or '?'} — {len(turns)} chat turns*")
        lines.append("")
        prev_role = None
        for role, ts, txt in turns:
            if role == "assistant" and prev_role == "assistant":
                lines.append("----")
            else:
                who = "🧑 User" if role == "user" else "🤖 Claude"
                lines.append(f"## {who}")
            lines.append("")
            lines.append(txt)
            lines.append("")
            prev_role = role
        with open(path, "w") as out:
            out.write("\n".join(lines))
        made += 1
        index.append((date, fname, title, len(turns)))
        entries.append({"f": fname, "t": title, "d": date, "n": len(turns),
                        "x": "\n\n".join(txt for _, _, txt in turns)})

    index.sort()
    indexed = {fname for _, fname, _, _ in index}
    existing = {n for n in os.listdir(out_dir) if n.endswith(".md") and n != "INDEX.md"}
    orphans = sorted(existing - indexed)
    proj = os.path.basename(out_dir)
    with open(os.path.join(out_dir, "INDEX.md"), "w") as f:
        f.write(f"# {proj} chat history\n\n")
        f.write(f"Full-text search: open [index.html](index.html) in a browser.\n\n")
        f.write(f"{made} sessions with chat content ({skipped} empty/tool-only skipped).\n\n")
        for date, fname, title, n in index:
            f.write(f"- [{date}]({fname}) — {title} ({n} turns)\n")
        if orphans:
            f.write(f"\n## Not from this run ({len(orphans)})\n\n")
            f.write("Files present in the destination but not produced by this export — stale sessions, renamed/deleted JSONLs, or manual additions.\n\n")
            for fname in orphans:
                f.write(f"- [{fname}]({fname})\n")

    entries.sort(key=lambda e: e["d"], reverse=True)
    write_html(os.path.join(out_dir, "index.html"),
               f"{proj} — chat history",
               f"{made} sessions · full-text search of chat content",
               entries)
    return made, skipped, len(orphans), entries


os.makedirs(OUT_ROOT, exist_ok=True)
slugs = [d for d in sorted(os.listdir(PROJECTS_ROOT)) if os.path.isdir(os.path.join(PROJECTS_ROOT, d))]

projects = []
name_counts = collections.Counter()
for slug in slugs:
    src = os.path.join(PROJECTS_ROOT, slug)
    jsonls = sorted(glob.glob(os.path.join(src, "*.jsonl")))
    if not jsonls:
        continue
    cwd = detect_cwd(jsonls)
    base = (os.path.basename(cwd) if cwd else slug.lstrip("-")) or slug.lstrip("-")
    projects.append((slug, src, base, cwd))
    name_counts[base] += 1

# Container sessions. Each registry symlink resolves to <project>/.claude; the
# transcripts live in its projects/ subdir. The in-transcript cwd is the
# useless "/workspace", so recover the real project path from the symlink
# target instead. A "__box" folder suffix keeps these distinct from (and
# collision-free with) any host sessions for the same project.
if os.path.isdir(CONTAINERS_ROOT):
    for name in sorted(os.listdir(CONTAINERS_ROOT)):
        claude_dir = os.path.realpath(os.path.join(CONTAINERS_ROOT, name))
        proj_root = os.path.join(claude_dir, "projects")
        if not os.path.isdir(proj_root):
            continue
        real_project = os.path.dirname(claude_dir)  # strip trailing /.claude
        for inner in sorted(os.listdir(proj_root)):
            src = os.path.join(proj_root, inner)
            if not os.path.isdir(src) or not glob.glob(os.path.join(src, "*.jsonl")):
                continue
            base = (os.path.basename(real_project) or name) + "__box"
            projects.append((name + inner, src, base, real_project))
            name_counts[base] += 1

summary = []
all_entries = []
for slug, src, base, cwd in projects:
    folder = base if name_counts[base] == 1 else f"{base}__{slug.lstrip('-')}"
    out_dir = os.path.join(OUT_ROOT, folder)
    made, skipped, orphans, entries = process_project(src, out_dir)
    summary.append((folder, cwd or slug, made, skipped, orphans))
    for e in entries:
        all_entries.append({**e, "f": f"{folder}/{e['f']}", "p": folder})
    print(f"  {folder:40s}  {made:3d} files  ({skipped} skipped, {orphans} orphans)")

with open(os.path.join(OUT_ROOT, "INDEX.md"), "w") as f:
    f.write("# Claude chat history — all projects\n\n")
    f.write("Full-text search across all projects: open [index.html](index.html) in a browser.\n\n")
    for folder, cwd, made, skipped, orphans in sorted(summary, key=lambda r: (-r[2], r[0])):
        f.write(f"- [{folder}/]({folder}/INDEX.md) — `{cwd}` — {made} sessions")
        if orphans:
            f.write(f" ({orphans} orphans)")
        f.write("\n")

all_entries.sort(key=lambda e: e["d"], reverse=True)
write_html(os.path.join(OUT_ROOT, "index.html"),
           "Claude chat history — all projects",
           f"{len(all_entries)} sessions across {len(summary)} projects · full-text search of chat content",
           all_entries)

print(f"\nwrote {len(summary)} project folders + search index to {OUT_ROOT}")
