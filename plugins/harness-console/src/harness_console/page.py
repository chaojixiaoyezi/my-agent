# LLM: 纯常量页面：内联 CSS + 少量内联 JS，不引用任何外部资源（CSP 只放行 self 与内联）。
#   页面数据全部来自同源 GET /api/state（cookie 鉴权），用 textContent 渲染，线程标题等宿主数据不会被当成 HTML 执行。
#   宿主令牌从不出现在页面里；页面加载后用 history.replaceState 去掉地址栏里的会话令牌。
# 模块用途: 渲染工作台单页（顶栏、线程列表、运行状态与上下文用量、插件列表）。

from __future__ import annotations

_STYLE = """
:root{--bg:#f6f7f9;--panel:#fff;--line:#dde1e6;--text:#1d2329;--muted:#6b7580;--accent:#2f6fde;
--ok:#1f9d55;--warn:#d98a00;--bad:#d64545;--seg1:#4c8bf5;--seg2:#9b6bdf;--seg3:#2bb3a3}
@media (prefers-color-scheme:dark){:root{--bg:#15181c;--panel:#1d2126;--line:#30363d;--text:#e4e8ec;
--muted:#8b949e;--accent:#6ea0ff}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);
font:14px/1.5 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif}
header{display:flex;gap:16px;align-items:center;padding:10px 16px;border-bottom:1px solid var(--line);background:var(--panel)}
header h1{font-size:16px;margin:0 auto 0 0}.badge{padding:2px 8px;border-radius:10px;font-size:12px;border:1px solid var(--line)}
.badge.ok{color:var(--ok)}.badge.revoked,.badge.unavailable,.badge.error{color:var(--bad)}
#banner{background:var(--bad);color:#fff;padding:8px 16px}
.grid{display:grid;grid-template-columns:260px 1fr 260px;gap:12px;padding:12px;min-height:calc(100vh - 50px)}
@media (max-width:820px){.grid{grid-template-columns:1fr}}
section{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:12px;overflow:auto}
h2{font-size:13px;color:var(--muted);margin:0 0 8px;font-weight:600}
ul{list-style:none;margin:0;padding:0}li{padding:6px 8px;border-radius:6px;cursor:default}
#threads li{cursor:pointer}#threads li:hover{background:var(--bg)}#threads li.sel{background:var(--accent);color:#fff}
.meta{font-size:12px;color:var(--muted)}li.sel .meta{color:#e8eefc}
.state{font-size:22px;font-weight:600;margin:4px 0}.state.working{color:var(--accent)}.state.waiting{color:var(--warn)}
.state.idle{color:var(--muted)}.bar{position:relative;height:18px;background:var(--bg);border:1px solid var(--line);
border-radius:4px;overflow:hidden;display:flex;margin:8px 0}.bar span{height:100%}
.trigger{position:absolute;top:0;bottom:0;width:2px;background:var(--bad)}
.legend i{display:inline-block;width:10px;height:10px;border-radius:2px;margin:0 4px 0 10px;vertical-align:-1px}
.on{color:var(--ok)}.off{color:var(--muted)}
"""

_SCRIPT = """
const $=id=>document.getElementById(id);
const STATES={working:"工作中",waiting:"等待审批",idle:"空闲"};
let selected="",stopped=false,pollMs=2000;
history.replaceState(null,"","/");
function el(tag,cls,text){const e=document.createElement(tag);if(cls)e.className=cls;if(text!==undefined)e.textContent=text;return e;}
function ago(t){if(!t)return "—";const d=Math.max(0,Date.now()/1000-t);
 return d<60?Math.floor(d)+" 秒前":d<3600?Math.floor(d/60)+" 分钟前":d<86400?Math.floor(d/3600)+" 小时前":Math.floor(d/86400)+" 天前";}
function dur(t){const d=Math.max(0,Math.floor(Date.now()/1000-t)),h=Math.floor(d/3600),m=Math.floor(d%3600/60);
 return (h?h+" 小时 ":"")+(h||m?m+" 分 ":"")+d%60+" 秒";}
function num(n){return (n||0).toLocaleString();}
function stop(msg){stopped=true;$("banner").textContent=msg;$("banner").hidden=false;}
function renderThreads(s){const ul=$("threads");ul.replaceChildren();
 if(!s.threads.length)ul.append(el("li","meta",s.owner_loaded?"还没有线程":"owner 未加载（还没有会话在运行）"));
 for(const t of s.threads){const li=el("li",t.thread_id===s.selected?"sel":"");
  li.append(el("div","",t.title||"（无标题）"),
   el("div","meta",(t.status||"—")+" · "+ago(t.updated_at)+" · 压缩 "+(t.compact_generation||0)+" 代"));
  li.onclick=()=>{selected=t.thread_id;refresh();};ul.append(li);}}
function renderRun(s){const box=$("run");box.replaceChildren();const r=s.run;
 if(!r){box.append(el("div","meta",s.selected?"没有运行信息":"未选中线程"));return;}
 box.append(el("div","state "+r.state,STATES[r.state]||r.state),el("div","",r.activity||"（无当前活动）"));
 const extra=[];if(r.state!=="idle"&&r.started_at)extra.push("已进行 "+dur(r.started_at));
 if(r.active_task_count)extra.push("后台任务 "+r.active_task_count);if(r.subagent_count)extra.push("子代理 "+r.subagent_count);
 if(extra.length)box.append(el("div","meta",extra.join(" · ")));}
function renderContext(s){const box=$("context");box.replaceChildren();const c=s.context;
 if(!c||!c.known){box.append(el("div","meta","还没有上下文快照（发出第一次模型请求后出现）"));return;}
 const win=c.context_window_tokens||1,pct=v=>Math.min(100,100*(v||0)/win)+"%";
 const bar=el("div","bar");
 for(const [k,v] of [["--seg1",c.messages_tokens],["--seg2",c.runtime_guidance_tokens],["--seg3",c.tool_schema_tokens]]){
  const seg=el("span");seg.style.width=pct(v);seg.style.background="var("+k+")";bar.append(seg);}
 if(c.compact_trigger_tokens){const m=el("div","trigger");m.style.left=pct(c.compact_trigger_tokens);bar.append(m);}
 box.append(el("div","","当前 "+num(c.current_tokens)+" / 窗口 "+num(c.context_window_tokens)+"（"+
  (100*(c.current_tokens||0)/win).toFixed(1)+"%）"+(c.estimated?" · 估算":"")),bar);
 const lg=el("div","meta legend");
 for(const [k,name,v] of [["--seg1","消息",c.messages_tokens],["--seg2","引导",c.runtime_guidance_tokens],["--seg3","工具目录",c.tool_schema_tokens]]){
  const i=el("i");i.style.background="var("+k+")";lg.append(i,name+" "+num(v));}
 box.append(lg,el("div","meta","触发线 "+num(c.compact_trigger_tokens)+"（红线） · 压缩 "+(c.compact_count||0)+" 次"));}
function renderPlugins(s){const ul=$("plugins");ul.replaceChildren();
 for(const p of s.plugins){const li=el("li");
  li.append(el("div","",p.plugin_id+" "+(p.version||"")),el("span",p.enabled?"on":"off",p.enabled?"● 启用":"○ 停用"),
   el("div","meta",p.summary||""));ul.append(li);}}
function render(s){const ok=s.host.status==="ok";
 $("host").textContent="宿主 API："+(ok?"已连接":s.host.message);$("host").className="badge "+s.host.status;
 $("gw").textContent=s.gateway?"Gateway 进程 "+s.gateway.pid+(s.gateway.port?" · 端口 "+s.gateway.port:""):"Gateway：—";
 $("banner").hidden=ok;if(!ok)$("banner").textContent=s.host.message;
 if(s.host.status==="revoked")stop(s.host.message);
 pollMs=1000*(s.poll_seconds||2);if(s.selected)selected=s.selected;
 renderThreads(s);renderRun(s);renderContext(s);renderPlugins(s);}
async function refresh(){try{const r=await fetch("/api/state?thread="+encodeURIComponent(selected),{cache:"no-store"});
 if(r.status===403){stop("工作台会话已失效，请重新打开工作台");return;}render(await r.json());}
 catch(e){$("banner").textContent="工作台服务已停止或暂时无法连接";$("banner").hidden=false;}}
async function tick(){await refresh();if(!stopped)setTimeout(tick,pollMs);}
tick();
"""

_BODY = """<header><h1>my-agent 工作台</h1><span id="gw" class="meta">Gateway：—</span>
<span id="host" class="badge">宿主 API：连接中</span></header><div id="banner" hidden></div>
<div class="grid"><section><h2>最近线程</h2><ul id="threads"></ul></section>
<section><h2>运行状态</h2><div id="run"></div><h2 style="margin-top:20px">上下文用量</h2><div id="context"></div></section>
<section><h2>已安装插件</h2><ul id="plugins"></ul></section></div>"""


# LLM: 纯函数，返回固定页面；不接收任何宿主数据，因此无需转义。
# 函数用途: 生成工作台单页 HTML。
def console_page() -> str:
    return (f'<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>my-agent 工作台</title>'
            f'<meta name="viewport" content="width=device-width,initial-scale=1"><style>{_STYLE}</style></head>'
            f'<body>{_BODY}<script>{_SCRIPT}</script></body></html>')
