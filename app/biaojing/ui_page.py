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
 .cand details{min-width:220px}.cand textarea{width:230px;min-height:48px}
 .ruleStatus{border:1px solid #edf0f3;border-radius:5px;padding:8px;margin:5px 0}
 .finding{overflow-wrap:anywhere}
</style>
</head>
<body>
<header><div><h1>标镜 · 本地工作台</h1>
<p>批量导入 → 解析覆盖率 → 证据回溯 → 字段确认 → 规则筛查。所有筛查结果为人工核查线索，不是违法结论。</p></div>
<button id="aboutButton" type="button">关于</button></header>
<main>

<section>
<h2>一、拖入资料（支持目录 / ZIP / 多文件）</h2>
<label>资料来源 <select id="sourceType"><option value="unknown">待核</option>
<option value="bid_document">投标文件</option><option value="tenderer_document">采购人文件</option>
<option value="agency_document">代理机构文件</option><option value="historical_record">历史资料</option>
<option value="other">其他</option></select></label>
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
<p id="candPage" class="dim"></p><button id="moreCandidates" class="ghost" hidden>加载更多候选</button>
<p id="confCount" class="dim"></p>
<details><summary>查看确认历史（最近100条）</summary><div id="confHistory"></div></details>
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
      :result.status==="rejected"?"版本 "+result.latest_version+" 曾未通过兼容检查；当前程序已回退，等待修复版发布。"
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
    const unit=job.kind==="docx_upload"?"张图片":job.kind==="zip_upload"?"个归档成员":"页";
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
      const r=await fetch("/api/upload?name="+encodeURIComponent(relPath)
        +"&source_type="+encodeURIComponent($("#sourceType").value),{method:"POST",body:f});
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
let candidateOffset=0,candidateState=null;
async function loadState(append=false){
  const offset=append?candidateOffset:0;
  const st=await api("/api/state?candidate_offset="+offset+"&candidate_limit=200");
  const pageCount=st.candidates.length;
  if(append&&candidateState){st.candidates=[...candidateState.candidates,...st.candidates]}
  candidateState=st;
  candidateOffset=offset+pageCount;
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
    if(s.sha256){
      const bind=el("button","绑定到当前主体","ghost");
      bind.addEventListener("click",()=>bindFile(s));
      action.appendChild(bind);
    }
    tr3.appendChild(action);
    srcTb.appendChild(tr3);
  }
  renderCands(st);renderConfirms(st);renderHistory(st);
  const last=st.last_screen_run||{status:"not_run"};
  if(last.status==="completed"){
    const imported=last.import||{};
    $("#screenMeta").textContent="最近运行 "+(last.run_at||"时间未知")
      +"；规则 "+(last.rules_version||"未知")
      +"；事实快照 SHA-256 "+(last.facts_sha256||"未记录")
      +(imported.event_count_after!=null?"；累计事件 "+imported.event_count_after:"");
  }else if(last.status==="failed"){
    $("#screenMeta").textContent="最近一次筛查失败："+(last.error||"详情未记录");
  }else{$("#screenMeta").textContent="尚未运行筛查"}
  if(st.last_screen_run.status==="not_run")
    renderFindings({run_status:"not_run",findings:[]});
  else renderFindings({run_status:st.last_screen_run.status,
    error:st.last_screen_run.error,rule_statuses:st.last_screen_run.rule_statuses,
    findings:st.findings||[]});
}
function option(sel,value,label){const o=el("option",label);o.value=value;sel.appendChild(o)}
function renderCands(st){
  const tb=$("#cands");tb.textContent="";
  $("#candPage").textContent="已显示 "+st.candidates.length+" / "+st.candidate_total+" 条候选";
  $("#moreCandidates").hidden=!st.candidate_has_more;
  if(!st.candidates.length){const tr=el("tr");const td=el("td",st.candidate_total?"本页没有候选":"暂无候选（先拖入资料）","dim");td.colSpan=5;tr.appendChild(td);tb.appendChild(tr);return}
  const CONTACT_FIELDS=new Set(["contact_phone","contact_email","bank_account"]);
  const PERSON_FIELDS=new Set(["person_manager","person_tech","authorize_rep","legal_rep","id_number"]);
  const ROLES=["unknown","bidder","tenderer","agency","platform","public_service"];
  for(const c of st.candidates){
    const tr=el("tr","","cand");tr.appendChild(el("td",c.field));
    let display=typeof c.value==="object"?JSON.stringify(c.value):String(c.value);
    if(c.field==="total_price"&&c.value&&typeof c.value==="object")
      display=c.value.raw+"；金额状态："+c.value.status+(c.value.amount_yuan?"；标准金额 "+c.value.amount_yuan+" 元":"");
    if(c.field==="price_lines"&&Array.isArray(c.value))
      display="按主体汇总的 "+c.value.length+" 条清单报价行；请展开逐项核对";
    tr.appendChild(el("td",display));tr.appendChild(el("td",c.locator_display,"dim"));
    tr.appendChild(el("td",c.note||"","dim"));
    const td=el("td");
    const mk=(label,cls,fn)=>{const b=el("button",label,cls);b.addEventListener("click",fn);return b};
    let fix;
    if(c.field==="price_lines"){
      const details=el("details"),summary=el("summary","查看 / 编辑全部报价行");
      fix=document.createElement("textarea");fix.value=JSON.stringify(c.value,null,2);fix.dataset.cid=c.id;
      details.appendChild(summary);details.appendChild(fix);td.appendChild(details);
    }else{
      fix=document.createElement("input");fix.placeholder="更正值";fix.style.width="120px";fix.dataset.cid=c.id;
      if(c.field==="total_price"&&c.value)fix.value=c.value.raw||"";
      else if(typeof c.value==="string")fix.value=c.value;
      td.appendChild(fix);
    }
    td.appendChild(mk("查看证据","ghost",()=>showEv(c.evidence_id)));
    td.appendChild(mk("确认","",()=>doConfirm(c,"confirm",null)));
    td.appendChild(mk("更正","ghost",()=>doConfirm(c,"correct",fix.value)));
    td.appendChild(mk("标 unknown","ghost",()=>doConfirm(c,"unknown",null)));
    if(CONTACT_FIELDS.has(c.field)){
      const sel=document.createElement("select");sel.dataset.cid=c.id;sel.className="roleSel";
      sel.title="只有明确归属投标人的联系方式才参与 R002";
      for(const r of ROLES)option(sel,r,"来源角色："+r);td.appendChild(sel);
    }
    if(PERSON_FIELDS.has(c.field)){
      const person=document.createElement("input");person.dataset.cid=c.id;person.className="personSel";
      person.placeholder="人工确认身份 ID；同一人跨主体填写相同值";person.style.width="190px";
      td.appendChild(person);
    }
    if(c.field==="total_price"){
      const unit=document.createElement("select");unit.dataset.cid=c.id;unit.className="unitSel";unit.title="金额单位";
      for(const p of [["unknown","单位：待核"],["yuan","元"],["ten_thousand_yuan","万元"]])option(unit,p[0],p[1]);
      if(c.value&&["yuan","ten_thousand_yuan"].includes(c.value.unit_hint))unit.value=c.value.unit_hint;
      const currency=document.createElement("select");currency.dataset.cid=c.id;currency.className="currencySel";currency.title="币种";
      for(const p of [["unknown","币种：待核"],["CNY","人民币 CNY"],["USD","美元 USD"]])option(currency,p[0],p[1]);
      currency.value=c.currency_hint||c.value.currency_hint||"unknown";
      const tax=document.createElement("select");tax.dataset.cid=c.id;tax.className="taxSel";tax.title="税口径";
      for(const p of [["unknown","税口径：待核"],["true","含税"],["false","不含税"]])option(tax,p[0],p[1]);
      td.appendChild(unit);td.appendChild(currency);td.appendChild(tax);
    }
    tr.appendChild(td);tb.appendChild(tr);
  }
}
$("#moreCandidates").addEventListener("click",()=>loadState(true));
async function doConfirm(cand,action,fixValue){
  const eid=$("#eid").value.trim(),lid=$("#lid").value.trim(),bid=$("#bid").value.trim();
  if(!eid||!lid||!bid){toast("请先填写事件 / 标段 / 主体 ID");return}
  const original=JSON.stringify(cand.value);
  if(cand.field==="total_price"){
    const unitSel=document.querySelector('.unitSel[data-cid="'+cand.id+'"]');
    const currencySel=document.querySelector('.currencySel[data-cid="'+cand.id+'"]');
    const taxSel=document.querySelector('.taxSel[data-cid="'+cand.id+'"]');
    const r=await fetch("/api/confirm_amount",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({event_id:eid,lot_id:lid,bidder_id:bid,
        raw_value:action==="unknown"?null:action==="correct"?fixValue:cand.value.raw,
        unit:action==="unknown"?"unknown":unitSel.value,
        currency:action==="unknown"?"unknown":currencySel.value,
        tax_included:action==="unknown"?"unknown":taxSel.value==="unknown"?"unknown":taxSel.value==="true",
        evidence_id:cand.evidence_id,action,original_candidate:original})});
    const j=await r.json();if(!r.ok||!j.ok){toast(j.error||"金额确认失败");return}
    toast(action==="unknown"?"总报价及其单位、币种、税口径已标为 unknown":"金额、单位、币种和税口径已原子保存");
    await loadState();return;
  }
  let value=cand.value;
  if(action==="correct"){
    if(!fixValue){toast("请输入更正值");return}
    if(cand.field==="price_lines"){
      try{value=JSON.parse(fixValue)}catch(_){toast("报价行 JSON 格式有误");return}
    }else value=fixValue;
  }
  const roleSel=document.querySelector('.roleSel[data-cid="'+cand.id+'"]');
  const personSel=document.querySelector('.personSel[data-cid="'+cand.id+'"]');
  const r=await fetch("/api/confirm",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({event_id:eid,lot_id:lid,bidder_id:bid,field:cand.field,
      value,evidence_id:cand.evidence_id,action,
      source_role:roleSel?roleSel.value:"unknown",
      person_id:personSel&&personSel.value.trim()?personSel.value.trim():null,
      original_candidate:original})});
  const j=await r.json();if(!r.ok||!j.ok){toast(j.error||"确认失败");return}
  toast(action==="unknown"?"已标 unknown":"已确认；联系方式和人员身份仍按来源/人工身份分别核查");
  await loadState();
}
async function bindFile(source){
  const eid=$("#eid").value.trim(),lid=$("#lid").value.trim(),bid=$("#bid").value.trim();
  if(!eid||!lid||!bid){toast("请先填写事件 / 标段 / 主体 ID");return}
  const context=prompt("文件用途：bid_document / tenderer_document / legal_performance / joint_venture_reference / other","bid_document");
  if(context===null)return;
  const owner=prompt("文件声明主体 ID（可留空，须依据文件核对）","");if(owner===null)return;
  const uscc=prompt("文件声明统一社会信用代码（可留空）","");if(uscc===null)return;
  const r=await api("/api/bind_file",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({event_id:eid,lot_id:lid,bidder_id:bid,sha256:source.sha256,
      context,source_type:source.source_type||"unknown",declared_owner_id:owner||null,declared_uscc:uscc||null})});
  if(!r.ok){toast(r.error||"文件绑定失败");return}
  toast("文件已绑定到当前主体；请检查用途和声明信息");await loadState();
}
function renderConfirms(st){
  const p=$("#confCount");
  p.textContent=st.confirmations.length
    ? "当前已确认 "+st.confirmations.length+" 项事实；历史操作 "+st.confirmation_history_total+" 条（追加保存）"
    : "已确认 0 条字段";
}
function renderHistory(st){
  const box=$("#confHistory");box.textContent="";
  const items=(st.confirmation_history||[]).map(item=>({...item,history_kind:"field"}));
  for(const item of (st.file_binding_history||[]))items.push({
    event_id:item.event_id,lot_id:item.lot_id,bidder_id:item.bidder_id,
    field:"文件绑定 · "+item.sha256,action:"登记 / 修改",
    old_value:item.old_value||"（无）",new_value:item.new_value,
    evidence_id:item.evidence_id,changed_at:item.changed_at,history_kind:"file"});
  items.sort((a,b)=>String(b.changed_at||"").localeCompare(String(a.changed_at||"")));
  if(!items.length){box.appendChild(el("p","暂无确认或文件绑定历史","dim"));return}
  box.appendChild(el("p","确认操作 "+st.confirmation_history_total
    +" 条；文件绑定操作 "+st.file_binding_history_total+" 条（最近各 100 条）","dim"));
  const table=el("table"),head=el("tr");
  for(const title of ["时间","事件 / 标段 / 主体","字段","操作","原值 → 新值","证据"]){const th=el("th",title);head.appendChild(th)}
  const thead=el("thead");thead.appendChild(head);table.appendChild(thead);
  const body=el("tbody");
  for(const item of items){
    const row=el("tr"),oldv=item.old_value||"（无）",newv=item.new_value||"（无）";
    const source=el("td");
    for(const [label,value] of [["旧",item.old_evidence_id],["新",item.evidence_id]]){
      if(!value)continue;
      const link=el("span",label+":"+value,"evlink");link.addEventListener("click",()=>showEv(value));source.appendChild(link);source.appendChild(document.createTextNode(" "));
    }
    for(const value of [item.changed_at,item.event_id+" / "+item.lot_id+" / "+item.bidder_id,item.field,item.action,
      String(oldv)+" → "+String(newv)])row.appendChild(el("td",value));
    row.appendChild(source);body.appendChild(row);
  }
  table.appendChild(body);box.appendChild(table);
}

// ---- 筛查 ----
$("#runScreen").addEventListener("click",async()=>{
  $("#runScreen").disabled=true;
  try{
    const r=await fetch("/api/screen",{method:"POST",body:"{}"});
    const j=await r.json();
    if(!r.ok||!j.import){renderFindings({run_status:"failed",error:j.error||"筛查失败",findings:[]});toast(j.error||"筛查失败");await loadState();return}
    $("#screenMeta").textContent="事件累计 "+j.import.event_count_after+" 个，本次插入 "
      +j.import.events_inserted+"，跳过 "+j.import.events_skipped_idempotent
      +"，冲突 "+j.import.total_conflicts;
    renderFindings(j);await loadState();
  }catch(err){renderFindings({run_status:"failed",error:err.message,findings:[]});toast("筛查失败："+err.message)}
  finally{$("#runScreen").disabled=false}
});
function renderFindings(j){
  const box=$("#findings");box.textContent="";
  if(j.run_status==="not_run"){
    box.appendChild(el("p","尚未运行筛查。点击上方按钮后，系统会逐条显示可检查范围和未具备条件的规则。","dim"));return;
  }
  if(j.run_status==="failed"){
    box.appendChild(el("p","本次筛查执行失败："+(j.error||"详情未返回")+"。失败状态已单独记录，之前的筛查结果不代表本次运行。","bad"));return;
  }
  const statuses=j.rule_statuses||{};
  const statusLabel={findings:"发现待人工复核线索",checked_no_finding:"已检查，未发现线索",insufficient_data:"资料不足，无法完整检查"};
  const statusBox=el("div");statusBox.appendChild(el("b","规则检查覆盖情况"));
  for(const [id,s] of Object.entries(statuses)){
    const text=id+" "+s.name+"："+(statusLabel[s.status]||s.status)+"（输入 "+s.input_count+" 项；线索 "+s.finding_count+" 项）"+(s.reason?"；"+s.reason:"");
    statusBox.appendChild(el("div",text,"ruleStatus "+(s.status==="findings"?"warn":s.status==="insufficient_data"?"dim":"ok")));
  }
  box.appendChild(statusBox);
  const fs=j.findings||[];
  const reportable=fs.filter(f=>f.signal!=="不计算");
  if(!reportable.length){box.appendChild(el("p","筛查已完成；没有可报告的异常线索。资料不足或未具备条件的规则已在上方逐条列明。","dim"));return}
  for(const f of reportable){
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
