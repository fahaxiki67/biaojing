# -*- coding: utf-8 -*-
"""标镜工作台内嵌页面（单文件 HTML/CSS/JS，全部本地，无外部资源）。

安全口径：文档抽取的全部文本（候选值、原文、文件名、finding 原因等）
都是不可信输入——渲染一律走 createElement/textContent，不使用
innerHTML 插值，也不使用内联事件处理器（事件经 addEventListener +
dataset 绑定）。tests/test_p3.py 有静态守卫测试防止回退。
"""

INDEX_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>标镜 · 本地工作台</title>
<style>
 body{font-family:"PingFang SC","Microsoft YaHei",sans-serif;margin:0;background:#f5f6f8;color:#222}
 header{background:#1f3a5f;color:#fff;padding:14px 24px;display:flex;align-items:center;justify-content:space-between;gap:16px}
 header h1{margin:0;font-size:20px} header p{margin:4px 0 0;font-size:12px;opacity:.8}
 main{max-width:1080px;margin:18px auto;padding:0 16px}
 section{background:#fff;border:1px solid #e2e5ea;border-radius:8px;padding:16px;margin-bottom:16px}
 h2{font-size:15px;margin:0 0 10px;color:#1f3a5f}
 #drop{border:2px dashed #9db4d0;border-radius:8px;padding:26px;text-align:center;color:#557;cursor:pointer}
 #drop.over{background:#eef4fb;border-color:#1f3a5f}
 table{width:100%;border-collapse:collapse;font-size:13px}
 td,th{border-bottom:1px solid #edf0f3;padding:6px 8px;text-align:left;vertical-align:top}
 th{color:#556;background:#fafbfc}
 .ok{color:#1a7f37}.warn{color:#b25e09}.bad{color:#c0392b}.dim{color:#888}
 input{padding:5px 8px;border:1px solid #ccd2da;border-radius:4px;font-size:13px}
 button{padding:5px 12px;border:0;border-radius:4px;background:#1f3a5f;color:#fff;cursor:pointer;font-size:13px}
 button.ghost{background:#eef1f5;color:#334}
 #aboutButton{background:#ffffff20;border:1px solid #ffffff70}
 dialog{width:min(420px,calc(100vw - 48px));border:1px solid #ccd2da;border-radius:8px;padding:18px;color:#222}
 dialog::backdrop{background:#17253688}
 dialog h2{margin:0 0 12px}
 dialog p{font-size:13px;line-height:1.55}
 #toast{position:fixed;right:16px;bottom:16px;background:#333;color:#fff;padding:8px 14px;border-radius:6px;display:none;font-size:13px;max-width:70vw}
 .detail{font-size:12.5px;background:#fafbfc;border-radius:6px;padding:8px;margin-top:8px}
 .kv{margin:2px 0}
 .evlink{color:#1f3a5f;text-decoration:underline;cursor:pointer}
 .reason{white-space:pre-line;overflow-wrap:anywhere}
 #uploadLog{white-space:pre-wrap;overflow-wrap:anywhere}
 #taskPanel{display:flex;align-items:center;gap:10px;margin-top:8px}
</style>
</head>
<body>
<header><div><h1>标镜 · 本地工作台</h1>
<p>批量导入 → 解析覆盖率 → 证据回溯 → 字段确认 → 规则筛查。所有筛查结果为人工核查线索，不是违法结论。</p></div>
<button id="aboutButton" type="button">关于</button></header>
<main>

<section>
<h2>一、拖入资料（支持目录 / ZIP / 多文件）</h2>
<div id="drop">将整个资料目录或文件拖到这里，或点击选择文件
<input id="file" type="file" multiple hidden>
<input id="dir" type="file" webkitdirectory multiple hidden>
</div>
<p><button id="pickDir">选择整个目录</button> <span class="dim">（也可直接拖入目录/ZIP）</span></p>
<div id="uploadLog" class="dim" style="margin-top:8px"></div>
<div id="taskPanel" hidden>
<progress id="taskMeter" max="1" value="0" aria-label="长任务逐页进度"></progress>
<span id="taskText" role="status" aria-live="polite" class="dim"></span>
<button id="cancelTask" class="ghost" hidden>取消当前任务</button>
</div>
</section>

<section>
<h2>二、处理状态与覆盖率</h2>
<table id="coverage"><thead><tr>
<th>成功</th><th>部分</th><th>失败</th><th>待 OCR</th><th>待转换</th><th>重复</th><th>拒收</th><th>归档容器</th><th>unknown</th><th>输入合计</th>
</tr></thead><tbody></tbody></table>
<table style="margin-top:10px"><thead><tr>
<th>来源引用</th><th>类型</th><th>状态</th><th>原因</th><th>解析器 / 最近版本</th><th>操作</th></tr></thead>
<tbody id="sources"></tbody></table>
</section>

<section>
<h2>三、候选字段确认（确认后才会参与规则；缺证据只能标 unknown）</h2>
<p class="dim">确认前请先填写本次确认归属的事件 / 标段 / 主体 ID（可用候选值，如 EV-2026-001-01 / SYN-LOT-001 / SYN-BIDDER-01）。</p>
<div style="margin-bottom:10px">
事件ID <input id="eid" style="width:170px">
标段ID <input id="lid" style="width:150px">
主体ID <input id="bid" style="width:150px">
</div>
<table><thead><tr>
<th>字段</th><th>候选值</th><th>来源定位</th><th>说明</th><th>操作</th></tr></thead>
<tbody id="cands"></tbody></table>
<p id="confCount" class="dim"></p>
</section>

<section>
<h2>四、运行筛查并查看异常</h2>
<button id="runScreen">基于已确认事实运行筛查</button>
<span id="screenMeta" class="dim" style="margin-left:10px"></span>
<div id="findings"></div>
</section>

</main>
<dialog id="aboutDialog" aria-labelledby="aboutTitle">
<h2 id="aboutTitle">关于标镜</h2>
<p id="aboutProduct"></p><p id="aboutAuthor"></p><p id="aboutVersion"></p>
<p id="updateStatus" class="dim" role="status" aria-live="polite"></p>
<button id="checkUpdate" type="button">立即检查更新</button>
<button id="closeAbout" type="button" class="ghost">关闭</button>
</dialog>
<div id="toast"></div>
<script>
"use strict";
const $=s=>document.querySelector(s);
function el(tag,text,cls){const n=document.createElement(tag);if(text!=null)n.textContent=text;if(cls)n.className=cls;return n}
function toast(m){const t=$("#toast");t.textContent=m;t.style.display="block";setTimeout(()=>t.style.display="none",3000)}
async function api(path,opt){const r=await fetch(path,opt);const j=await r.json().catch(()=>({}));if(!r.ok)throw new Error(j.error||String(r.status));return j}
function statusClass(s){return s==="success"?"ok":(s==="failed"||s==="pending_convert")?"bad":(s==="partial"||s==="pending_ocr")?"warn":"dim"}
const aboutDialog=$("#aboutDialog");
async function openAbout(){
  aboutDialog.showModal();
  try{
    const info=await api("/api/about");
    $("#aboutProduct").textContent="程序："+info.product_name;
    $("#aboutAuthor").textContent="作者："+info.author;
    $("#aboutVersion").textContent="版本："+info.version;
    $("#updateStatus").textContent=info.updates_enabled
      ?"已启用 GitHub 自动更新：启动时检查；有新版本时校验下载，并在启动时安装。"
      :"自动更新将在配置公开 GitHub 仓库后启用。";
    $("#checkUpdate").disabled=!info.updates_enabled;
  }catch(err){$("#updateStatus").textContent="版本信息读取失败："+err.message}
}
$("#aboutButton").addEventListener("click",openAbout);
$("#closeAbout").addEventListener("click",()=>aboutDialog.close());
$("#checkUpdate").addEventListener("click",async()=>{
  const button=$("#checkUpdate"),status=$("#updateStatus");
  button.disabled=true;status.textContent="正在检查 GitHub 更新…";
  try{
    const result=await api("/api/update/check",{method:"POST",
      headers:{"Content-Type":"application/json"},body:"{}"});
    status.textContent=result.status==="unconfigured"
      ?"自动更新尚未配置 GitHub 仓库。"
      :result.status==="current"?"当前已是最新版本（"+result.current_version+"）。"
      :"已下载并校验版本 "+result.latest_version+"；下次启动时自动安装。";
  }catch(err){status.textContent="更新检查失败："+err.message+"。当前版本仍可使用。"}
  finally{button.disabled=false}
});
let activeJobId=null;
const cancelTask=$("#cancelTask");
cancelTask.addEventListener("click",async()=>{
  if(!activeJobId)return;
  cancelTask.disabled=true;
  try{await api("/api/jobs/"+activeJobId+"/cancel",{method:"POST",
    headers:{"Content-Type":"application/json"},body:"{}"});
    $("#taskText").textContent="已请求取消；正在完成当前页或图片，未处理部分会保留为待处理";
  }catch(err){toast("取消请求失败："+err.message);cancelTask.disabled=false}
});
async function waitForJob(jobId,label){
  activeJobId=jobId;cancelTask.hidden=false;cancelTask.disabled=false;
  $("#taskPanel").hidden=false;
  const meter=$("#taskMeter"),text=$("#taskText");
  while(true){
    const job=await api("/api/jobs/"+jobId);
    meter.max=Math.max(1,job.total_pages||1);
    meter.value=Math.max(0,(job.page||0)-(job.stage==="processing"?1:0));
    const unit=job.kind==="docx_upload"?"张图片":"页";
    const page=job.total_pages?"第 "+(job.page||0)+" / "+job.total_pages+" "+unit:"准备处理中";
    const stage=job.cancel_requested?"取消将在当前处理项完成后生效":
      job.stage==="processing"?"正在识别":job.stage==="page_done"?"页面已处理":job.stage;
    text.textContent=label+"："+page+"，"+stage;
    if(["completed","cancelled","failed"].includes(job.status)){
      activeJobId=null;cancelTask.hidden=true;cancelTask.disabled=false;
      return job;
    }
    await new Promise(resolve=>setTimeout(resolve,500));
  }
}

// ---- 拖入与上传 ----
const drop=$("#drop");
// #file/#dir 是 #drop 的子元素：input.click() 的合成 click 会冒泡回
// #drop 重入本 handler（两次 "user activation" 警告的来源），必须按
// target 守卫，只响应点击落在本区域本身
function dropClickHandler(e){if(e.target!==drop)return;$("#file").click()}
drop.addEventListener("click",dropClickHandler);
$("#pickDir").addEventListener("click",e=>{e.stopPropagation();$("#dir").click()});
$("#file").addEventListener("change",e=>{
  uploadFiles([...e.target.files].map(f=>({f,rel:f.name})));
  e.target.value="";  // 清空以便重复选择同一文件仍触发 change
});
$("#dir").addEventListener("change",e=>{
  uploadFiles([...e.target.files].map(f=>({f,rel:f.webkitRelativePath||f.name})));
  e.target.value="";
});
drop.addEventListener("dragover",e=>{e.preventDefault();drop.classList.add("over")});
drop.addEventListener("dragleave",()=>drop.classList.remove("over"));
drop.addEventListener("drop",async e=>{
  e.preventDefault();drop.classList.remove("over");
  const files=[];
  const errors=[];
  const entries=[...e.dataTransfer.items].map(i=>i.webkitGetAsEntry&&i.webkitGetAsEntry()).filter(Boolean);
  if(entries.length){for(const ent of entries)await walk(ent,"",files,errors);uploadFiles(files,errors);}
  else uploadFiles([...e.dataTransfer.files].map(f=>({f,rel:f.name})));
});
// Chromium readEntries 每次最多返回 100 条：必须循环调用到空批（MDN）
function readAllEntries(reader){
  return new Promise((resolve,reject)=>{
    const all=[];
    (function next(){
      reader.readEntries(batch=>{
        if(!batch.length){resolve(all);return}
        all.push(...batch);next();
      },err=>reject(err));
    })();
  });
}
async function walk(entry,path,out,errors){
  try{
    if(entry.isFile){
      const f=await new Promise((ok,fail)=>entry.file(ok,fail));
      out.push({f,rel:path+entry.name});
    }else if(entry.isDirectory){
      const reader=entry.createReader();
      const batch=await readAllEntries(reader);  // 循环读到空批，防大目录截断
      for(const e of batch) await walk(e,path+entry.name+"/",out,errors);
    }
  }catch(err){
    errors.push({name:path+entry.name,reason:"读取失败："+(err.message||err)});
  }
}
// 拒收记录上报 helper：统一 response.ok 判定；返回是否已计入覆盖率
async function reportReject(name,reason){
  try{
    const r=await fetch("/api/reject_record",{method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({name,reason})});
    if(r.ok) return true;
    toast("拒收记录上报未成功（HTTP "+r.status+"）："+name);
  }catch(_){toast("拒收记录上报未成功："+name)}
  return false;
}
async function uploadFiles(list,errors){
  const log=$("#uploadLog");log.textContent="";
  // 枚举失败：逐条上报为拒收记录；计入总失败数，是否落覆盖率如实提示
  let enumFail=0;
  for(const err of (errors||[])){
    log.textContent+="[枚举失败] "+err.name+"："+err.reason+"\\n";
    const counted=await reportReject(err.name,err.reason);
    if(counted) log.textContent+="[覆盖率] 已记为拒收\\n";
    else log.textContent+="[提示] 该失败项未计入服务器覆盖率\\n";
    enumFail++;
  }
  const total=list.length;
  if(!total){
    if(enumFail){
      log.textContent+="发现文件数为 0；枚举失败 "+enumFail
        +" 项"+(enumFail?"（其中成功记入覆盖率的见上）":"")+"，未发生文件导入\\n";
    } else log.textContent+="未发现文件：所选目录为空或拖入内容不含可上传文件，未发生导入\\n";
    loadState();return;
  }
  let done=0,fail=enumFail;  // 枚举失败计入总失败数
  log.textContent+="共发现 "+total+" 个文件（另有枚举失败 "+enumFail+" 项），开始上传\\n";
  let attempted=0;  // 本次实际发起处理（上传或超限拒收）的文件数
  for(const item of list){
    // 畸形项可见失败并跳过，不中止其余文件
    const f=(item&&item.f!==undefined)?item.f:item;
    if(!f||typeof f.size!=="number"){
      attempted++;
      const label=(item&&item.rel)||"（无文件名）";
      log.textContent+="[失败] "+label+"：不是有效文件对象，已跳过\\n";
      fail++;continue;
    }
    const relPath=item.rel||f.name;
    if(f.size>134217728){
      // 超限文件不上传字节，只发小型拒收记录；仅在确实计入时才声称
      attempted++;
      const counted=await reportReject(relPath,
        "文件 "+f.size+" 字节超过 128MB 上限");
      if(counted) log.textContent+="[拒收] "+relPath+"：超过 128MB 上限（已记录到覆盖率）\\n";
      else log.textContent+="[拒收] "+relPath+"：超过 128MB 上限（上报未成功，未计入覆盖率）\\n";
      fail++;continue;
    }
    attempted++;
    log.textContent+="上传中（"+attempted+"/"+total+"）："+relPath+" …\\n";
    try{
      const r=await fetch("/api/upload?name="+encodeURIComponent(relPath),{method:"POST",body:f});
      let j=await r.json();
      if(!r.ok) throw new Error(j.error||("HTTP "+r.status));
      if(j.job_id){
        const task=await waitForJob(j.job_id,relPath);
        if(task.status==="failed")throw new Error(task.error||"后台处理失败");
        if(task.status==="cancelled")
          log.textContent+="[已取消] "+relPath+"：剩余页已保留待处理，可稍后重试\\n";
        j=task.result||{};
      }
      // HTTP 200 ≠ 业务成功：failed/rejected 按服务端结果计入失败
      const items=j.results||[j.result||{}];
      for(const x of items)
        log.textContent+="["+ (x.status||"?") +"] "+(x.ref||relPath)
          + (x.candidates!=null?("，候选 "+x.candidates+" 条"):"")
          + (x.error?("，"+x.error):"") + "\\n";
      if(items.some(x=>x.status==="failed"||x.status==="rejected")) fail++;
      else done++;
    }catch(err){log.textContent+="[失败] "+relPath+"："+err.message+"\\n";fail++}
  }
  log.textContent+="上传结束：已处理 "+done+"，失败/拒收 "+fail
    +"（含枚举失败 "+enumFail+"），发现文件 "+total+"\\n";
  loadState();
}

// ---- 状态与覆盖率 ----
async function loadState(){
  const st=await api("/api/state");
  const cov=st.coverage, by=cov.by_status, tb=$("#coverage tbody");
  tb.textContent="";
  const tr=el("tr");tb.appendChild(tr);
  for(const s of cov.status_order){const td=el("td",String(by[s]||0),statusClass(s)==="dim"?"":statusClass(s));tr.appendChild(td)}
  tr.appendChild(el("td",String(cov.total_input_occurrences)));
  const srcTb=$("#sources");srcTb.textContent="";
  if(!st.source_rows.length){const tr2=el("tr");const td=el("td","尚未导入资料","dim");td.colSpan=6;tr2.appendChild(td);srcTb.appendChild(tr2)}
  for(const s of st.source_rows){
    const tr3=el("tr");
    let statusText=s.status;
    let pending=0;
    try{const counts=JSON.parse(s.counts_json||"{}");
      const isDocx=s.doc_type==="docx";
      const ocr=isDocx?(counts.images_ocr||0):(counts.pages_ocr||0);
      pending=isDocx?(counts.images_pending_ocr||0):(counts.pages_pending_ocr||0);
      const unit=isDocx?"张图片":"页";
      if(ocr||pending) statusText+="（本机 OCR 成功 "+ocr+" "+unit+"，待 OCR "+pending+" "+unit+"）";
    }catch(_){}
    tr3.appendChild(el("td",s.ref,"dim"));
    tr3.appendChild(el("td",s.doc_type||"-"));
    tr3.appendChild(el("td",statusText,statusClass(s.status)));
    tr3.appendChild(el("td",s.reason||"","dim reason"));
    tr3.appendChild(el("td",(s.parser||"-")+"\\n"+(s.extract_version||"版本未知"),"dim reason"));
    const action=el("td");
    if(s.doc_type==="pdf"&&s.sha256&&pending>0&&["partial","pending_ocr"].includes(s.status)){
      const retry=el("button","重试待 OCR "+pending+" 页","ghost");
      retry.addEventListener("click",async()=>{
        retry.disabled=true;
        try{
          const started=await api("/api/retry_ocr",{method:"POST",
            headers:{"Content-Type":"application/json"},
            body:JSON.stringify({sha256:s.sha256})});
          const task=await waitForJob(started.job_id,"OCR 重试");
          if(task.status==="failed")throw new Error(task.error||"后台处理失败");
          const j=task.result||{};
          toast((task.status==="cancelled"?"已取消：":"重试完成：")
            +"本次 "+(j.retried_pages||0)+" 页，仍待 OCR "+(j.pages_pending_ocr||0)+" 页");
          await loadState();
        }catch(err){toast("OCR 重试失败："+err.message);retry.disabled=false}
      });
      action.appendChild(retry);
    }else action.appendChild(el("span","—","dim"));
    tr3.appendChild(action);
    srcTb.appendChild(tr3);
  }
  renderCands(st);renderConfirms(st);
  if(st.findings) renderFindings({findings: st.findings});
}
function renderCands(st){
  const tb=$("#cands");tb.textContent="";
  if(!st.candidates.length){const tr=el("tr");const td=el("td","暂无候选（先拖入资料）","dim");td.colSpan=5;tr.appendChild(td);tb.appendChild(tr);return}
  const CONTACT_FIELDS=new Set(["contact_phone","contact_email","bank_account"]);
  const ROLES=["unknown","bidder","tenderer","agency","platform","public_service"];
  for(const c of st.candidates){
    const tr=el("tr","","cand");
    tr.appendChild(el("td",c.field));
    const v=typeof c.value==="object"?JSON.stringify(c.value):String(c.value);
    tr.appendChild(el("td",v.slice(0,60)));
    tr.appendChild(el("td",c.locator_display,"dim"));
    tr.appendChild(el("td",c.note||"","dim"));
    const td=el("td");
    const mk=(label,cls,fn)=>{const b=el("button",label,cls);b.addEventListener("click",fn);return b};
    const fix=document.createElement("input");fix.placeholder="改为";fix.style.width="110px";fix.dataset.cid=c.id;
    td.appendChild(mk("查看证据","ghost",()=>showEv(c.evidence_id)));
    td.appendChild(mk("确认","",()=>doConfirm(c,"confirm",null,null)));
    td.appendChild(fix);
    td.appendChild(mk("更正","ghost",()=>doConfirm(c,"correct",fix.value,null)));
    td.appendChild(mk("标unknown","ghost",()=>doConfirm(c,"unknown",null,null)));
    if(CONTACT_FIELDS.has(c.field)){
      const sel=document.createElement("select");
      sel.dataset.cid=c.id;sel.className="roleSel";sel.title="来源角色（确认值≠确认角色）";
      for(const r of ROLES){const o=document.createElement("option");o.value=r;o.textContent="角色:"+r;sel.appendChild(o)}
      td.appendChild(sel);
    }
    if(c.field==="total_price"){
      const sel=document.createElement("select");
      sel.dataset.cid=c.id;sel.className="taxSel";sel.title="税口径（unknown 不参与集中度统计）";
      for(const r of [["unknown","税口径:unknown"],["true","税口径:含税"],["false","税口径:不含税"]]){
        const o=document.createElement("option");o.value=r[0];o.textContent=r[1];sel.appendChild(o)}
      td.appendChild(sel);
    }
    tr.appendChild(td);
    tb.appendChild(tr);
  }
}
async function doConfirm(cand,action,fixValue,unused){
  const eid=$("#eid").value.trim(),lid=$("#lid").value.trim(),bid=$("#bid").value.trim();
  if(!eid||!lid||!bid){toast("请先填写事件/标段/主体 ID");return}
  let value=cand.value;
  if(action==="correct"){
    if(!fixValue){toast("请输入更正值");return}
    const n=Number(String(fixValue).replace(/,/g,""));
    value=cand.field==="total_price"&&Number.isFinite(n)?n:fixValue;
  }
  const sel=document.querySelector('.roleSel[data-cid="'+cand.id+'"]');
  const sourceRole=sel?sel.value:"unknown";
  const r=await fetch("/api/confirm",{method:"POST",
    headers:{"Content-Type":"application/json"},
    body:JSON.stringify({event_id:eid,lot_id:lid,bidder_id:bid,field:cand.field,
      value,evidence_id:cand.evidence_id,action,
      source_role:sourceRole,
      original_candidate:String(cand.value)})});
  const j=await r.json();
  if(!j.ok){toast(j.error||"确认失败");return}
  // 总报价确认联动：币种候选（由人民币/表头单位推断）与税口径一并提交
  if(action==="confirm"&&cand.companion){
    await fetch("/api/confirm",{method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({event_id:eid,lot_id:lid,bidder_id:bid,
        field:cand.companion.field,value:cand.companion.value,
        evidence_id:cand.evidence_id,action:"confirm",
        original_candidate:String(cand.value)})});
  }
  if(action==="confirm"&&cand.field==="total_price"){
    const taxSel=document.querySelector('.taxSel[data-cid="'+cand.id+'"]');
    if(taxSel&&taxSel.value!=="unknown"){
      await fetch("/api/confirm",{method:"POST",
        headers:{"Content-Type":"application/json"},
        body:JSON.stringify({event_id:eid,lot_id:lid,bidder_id:bid,
          field:"tax_included",value:taxSel.value==="true",
          evidence_id:cand.evidence_id,action:"confirm",
          original_candidate:String(cand.value)})});
    }
  }
  toast(action==="unknown"?"已标 unknown":("已确认（来源角色 "+sourceRole+"）"));
  loadState();
}
function renderConfirms(st){
  const p=$("#confCount");
  p.textContent=st.confirmations.length
    ? "已确认 "+st.confirmations.length+" 条字段"
    : "已确认 0 条字段";
}

// ---- 筛查 ----
$("#runScreen").addEventListener("click",async()=>{
  const r=await fetch("/api/screen",{method:"POST",body:"{}"});
  const j=await r.json();
  if(!r.ok||!j.import){toast(j.error||"筛查失败，请检查已确认字段");return}
  $("#screenMeta").textContent="事件累计 "+j.import.event_count_after+" 个，本次插入 "
    +j.import.events_inserted+"，跳过 "+j.import.events_skipped_idempotent
    +"，冲突 "+j.import.total_conflicts;
  renderFindings(j);
});
function renderFindings(j){
  const box=$("#findings");box.textContent="";
  const fs=j.findings||[];
  if(!fs.length){box.appendChild(el("p","暂无筛查结果（先确认字段再运行）","dim"));return}
  for(const f of fs){
    const card=el("div","","finding");
    const head=el("div","["+f.rule_id+" · "+f.signal+"] "+f.trigger_reason,"head");
    const detail=el("div","","detail");detail.style.display="none";
    head.addEventListener("click",()=>{detail.style.display=detail.style.display==="none"?"block":"none"});
    card.appendChild(head);
    const addLine=(label,text)=>{
      const d=el("div","","kv");d.appendChild(el("b",label+"："));
      d.appendChild(document.createTextNode(text));detail.appendChild(d)};
    addLine("规则",f.rule_id+"（版本 "+f.rule_version+"）");
    addLine("范围",JSON.stringify(f.scope));
    addLine("实际输入",JSON.stringify(f.inputs));
    addLine("参数/公式/阈值",JSON.stringify(f.params));
    addLine("触发原因",f.trigger_reason);
    addLine("限制",f.limitations.join("；"));
    addLine("替代解释",f.alternative_explanations.join("；"));
    addLine("复核状态",f.review_status);
    const evd=el("div","","kv");evd.appendChild(el("b","证据："));
    if(!f.evidence_ids.length) evd.appendChild(document.createTextNode("（无）"));
    f.evidence_ids.forEach((eid,idx)=>{
      if(idx) evd.appendChild(document.createTextNode("、"));
      const link=el("span",eid,"evlink");
      link.addEventListener("click",()=>showEv(eid));
      evd.appendChild(link);
    });
    detail.appendChild(evd);
    card.appendChild(detail);
    box.appendChild(card);
  }
}
async function showEv(id){
  // 完整证据详情：不截断原文；含定位、SHA-256、全部来源引用与原件下载
  const r=await fetch("/api/evidence/"+id);
  const j=await r.json();
  const box=$("#findings");
  const panel=el("div","","finding");
  panel.appendChild(el("div","","head")).textContent="证据详情 "+id;
  const d=el("div","","detail");
  const addKV=(k,v)=>{const row=el("div","","kv");row.appendChild(el("b",k+"："));
    row.appendChild(document.createTextNode(v==null||v===""?"（无）":String(v)));d.appendChild(row)};
  addKV("证据 ID",j.evidence_id);
  addKV("定位",j.locator_display);
  if(j.page_no)addKV("物理页码",j.page_no);
  if(j.page_status){
    addKV("页面状态",j.page_status);
    if(j.page_error)addKV("待处理原因",j.page_error);
    if(j.page_note)addKV("页面提示",j.page_note);
  }
  addKV("解析版本",j.extract_version);
  addKV("文件 SHA-256",j.sha256);
  addKV("来源类型",j.source_type);
  if(j.ocr_used) addKV("文字来源","本机 OCR 机器识别文本，请对照原件复核");
  addKV("来源引用",(j.refs||[]).join("；"));
  d.appendChild(el("b","原文（完整，不截断）："));
  const pre=el("pre");pre.style.whiteSpace="pre-wrap";pre.style.maxHeight="300px";
  pre.style.overflow="auto";pre.textContent=j.quote||"（无文本）";
  d.appendChild(pre);
  const dl=el("button","下载原始文件（逐字节校验）");
  dl.addEventListener("click",()=>window.open("/api/download/"+j.sha256));
  d.appendChild(dl);
  const close=el("button","关闭","ghost");
  close.style.marginLeft="8px";
  close.addEventListener("click",()=>panel.remove());
  d.appendChild(close);
  panel.appendChild(d);
  box.insertBefore(panel,box.firstChild);
}
loadState();
</script>
</body></html>
"""
