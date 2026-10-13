"use strict";
const $ = id => document.getElementById(id);
const names = ["پین‌ها و اتصال‌ها","مدار و نت‌لیست","حدود مطلق","شرایط کار پیشنهادی","نیازمندی‌های تغذیه","انتخاب قطعات خارجی","ابعاد و فوت‌پرینت","قواعد PCB","مشخصات الکتریکی","رابط‌های ارتباطی","مشخصات حرارتی","مشخصات زمانی","حفاظت‌ها","شناسایی قطعه","خطاها و یادداشت‌ها","نمودارهای عملکرد","لحیم‌کاری و مونتاژ"];
let jobId = null, cursor = 0, pollTimer, startedAt, finishedAt, busy = false, partialSignature = "";
function text(id, value) { $(id).textContent = value; }
function make(tag, cls, content) { const el = document.createElement(tag); if (cls) el.className = cls; if (content !== undefined) el.textContent = content; return el; }
for (let i = 1; i <= 17; i++) {
  const row = make("div", "section-row"); const label = make("span", "section-name");
  label.append(make("span", "section-num", String(i).padStart(2,"0")),document.createTextNode(names[i-1]));
  const dots = make("span", "stage-dots");
  for (const role of ["extract", "blind", "review"]) { const dot = make("span", "stage-dot"); dot.id = `${role}-${i}`; dot.title = `${role}: waiting`; dots.append(dot); }
  row.append(label, dots); $("sections").append(row);
}
function fileChanged(file) { text("file-name", file ? file.name : "انتخاب یا رهاکردن PDF"); text("form-error", ""); }
$("pdf-file").addEventListener("change", () => fileChanged($("pdf-file").files[0]));
for (const event of ["dragenter","dragover"]) $("drop-zone").addEventListener(event,e=>{e.preventDefault(); $("drop-zone").classList.add("dragging");});
for (const event of ["dragleave","drop"]) $("drop-zone").addEventListener(event,e=>{e.preventDefault(); $("drop-zone").classList.remove("dragging");});
$("drop-zone").addEventListener("drop",e=>{if (!busy && e.dataTransfer.files.length) {$("pdf-file").files=e.dataTransfer.files; fileChanged(e.dataTransfer.files[0]);}});
function appendLog(e) {
  const terminal = $("log").parentElement; const atBottom = terminal.scrollHeight-terminal.scrollTop-terminal.clientHeight < 45;
  const row = make("div", "log-line" + (e.message.includes("completed") ? " success" : e.message.includes("failed") ? " failure" : ""));
  row.append(make("span", "log-time", new Date(e.time).toLocaleTimeString("en-GB",{hour12:false})),make("span", "", e.message));
  $("log").append(row); if (atBottom) terminal.scrollTop=terminal.scrollHeight;
}
function clock() {
  if (!startedAt) return;
  const seconds = Math.max(0,Math.floor(((finishedAt ? new Date(finishedAt) : new Date())-new Date(startedAt))/1000));
  text("elapsed",`${String(Math.floor(seconds/60)).padStart(2,"0")}:${String(seconds%60).padStart(2,"0")}`);
}
setInterval(clock,1000);
async function readError(response) { try {const data=await response.json(); return typeof data.detail==="string" ? data.detail : "درخواست پذیرفته نشد.";} catch {return "ارتباط با سرور برقرار نشد.";} }
$("upload-form").addEventListener("submit", async e=>{
  e.preventDefault(); const file=$("pdf-file").files[0]; if(!file || busy)return;
  if(file.size>20*1024*1024){text("form-error","حداکثر حجم فایل ۲۰ مگابایت است.");return;}
  busy=true; $("submit").disabled=true; text("submit","در حال آپلود…");text("form-error","");
  try{
    const body=new FormData();body.append("file",file);const fresh=new URL(document.URL).searchParams.get("fresh")==="1";if(fresh)body.append("fresh_run","true");const r=await fetch(`/api/jobs${fresh ? "?fresh=true" : ""}`,{method:"POST",body});
    if(!r.ok)throw new Error(await readError(r));
    const job=await r.json();jobId=job.id;cursor=0;startedAt=null;finishedAt=null;
    history.replaceState(null,"",`?job=${jobId}`);$("log").replaceChildren();$("target-card").hidden=true;$("result-panel").hidden=true;$("live-output").hidden=true;$("resume").hidden=true;partialSignature="";text("elapsed","00:00");text("completed","0 / 51");$("progress").value=0;
    for(const dot of document.querySelectorAll(".stage-dot")){dot.className="stage-dot";}
    text("command-file",file.name);text("job-id",`JOB ${jobId}`);text("submit","اجرا روی سرور ادامه دارد");$("cursor").hidden=false;
    clearTimeout(pollTimer);poll();
  }catch(error){text("form-error",error.message);busy=false;$("submit").disabled=false;text("submit","شروع تولید نمونه ↗");}
});
$("resume").addEventListener("click",async()=>{
  if(busy || !jobId)return;$("resume").disabled=true;text("form-error","");
  try{const r=await fetch(`/api/jobs/${jobId}/resume`,{method:"POST"});if(!r.ok)throw new Error(await readError(r));
    busy=true;finishedAt=null;$("resume").hidden=true;$("submit").disabled=true;$("cursor").hidden=false;text("submit","اجرا روی سرور ادامه دارد");clearTimeout(pollTimer);poll();
  }catch(error){text("form-error",error.message);}finally{$("resume").disabled=false;}
});
async function poll(){
  try{
    const r=await fetch(`/api/jobs/${jobId}?after=${cursor}`,{cache:"no-store"});if(!r.ok)throw new Error(await readError(r));const job=await r.json();
    text("connection","CONNECTED");text("model",job.model);text("command-file",job.filename);
    for(const e of job.events){appendLog(e);cursor=Math.max(cursor,e.sequence);}
    startedAt=job.attempt_started_at || job.started_at || job.created_at;finishedAt=job.finished_at;clock();
    let count=0;for(const [key,status] of Object.entries(job.stages)){const dot=$(key.replace(":","-"));if(dot){dot.className=`stage-dot ${status}`;dot.title=`${key}: ${status}`;}if(status==="completed")count++;}
    if(job.partial_sections?.length && JSON.stringify(job.partial_sections)!==partialSignature){
      const firstPreview=!partialSignature;partialSignature=JSON.stringify(job.partial_sections);
      const opened=new Set([...$("partial-sections").querySelectorAll("details[open]")].map(el=>el.dataset.section));
      $("live-output").hidden=false;$("partial-sections").replaceChildren();
      for(const section of job.partial_sections){const item=make("details");item.dataset.section=String(section.section_id);item.open=opened.has(item.dataset.section) || (firstPreview && section===job.partial_sections[0]);item.append(make("summary","",`${section.section_id}. ${names[section.section_id-1]} · ${section.claims} ادعا · ${section.review_method === "skipped_no_source" ? "بازبینی انجام نشد؛ شاهد ارائه نشده" : section.review_method === "declined_source_inspection" ? "بازبین شاهدی را تأیید نکرد؛ نیازمند بررسی" : section.reviewed ? "بازبینی انجام شد" : "در انتظار بازبینی"}`));
        for(const fact of section.preview)item.append(make("p","result-detail",`${fact.parameter}: ${fact.value}`));$("partial-sections").append(item);}
    }
    text("completed",`${count} / 51`);$("progress").value=count;
    const labels={queued:"در صف پردازش",running:"در حال استخراج",completed:"خروجی آماده شد",failed:"اجرا متوقف شد"};text("run-status",labels[job.status]);
    if(job.target){$("target-card").hidden=false;text("target-notice",`${job.target_count.toLocaleString("fa-IR")} گزینهٔ مدل/پکیج در PDF شناسایی شد. گزینهٔ اول را برای تولید این نمونه انتخاب کردیم.`);text("target-name",job.target.ordering_code || job.target.part_number);text("target-package",`${job.target.package_name}${job.target.package_code ? ` (${job.target.package_code})` : ""}`);text("target-grade",job.target.temperature_grade || "");}
    if(job.status==="completed" || job.status==="failed"){
      busy=false;$("cursor").hidden=true;$("submit").disabled=false;$("resume").hidden=!job.can_resume;text("submit","شروع نمونهٔ جدید ↗");text("connection",job.status==="completed" ? "COMPLETE" : "STOPPED");
      if(job.status==="completed"){text("status-note","هر ۱۷ بخش استخراج و بازبینی شد. خروجی و موارد نیازمند بررسی در پایین صفحه قرار دارد.");await showResult(job);}else{text("form-error",job.error);text("status-note",job.error);}
      return;
    }
    text("status-note",job.status==="queued" ? "نمونه در صف است و پس از پایان اجرای قبلی شروع می‌شود." : "اجرا فعال است. فاصله بین رویدادها زمان پاسخ‌دادن مدل است؛ می‌توانی این صفحه را ببندی و با همین لینک برگردی.");
  }catch(error){text("connection","RECONNECTING");text("status-note","ارتباط صفحه قطع شده؛ اتصال را دوباره امتحان می‌کنیم. اجرای سرور مستقل از این صفحه ادامه دارد.");}
  pollTimer=setTimeout(poll,2000);
}
async function showResult(job){
  const r=await fetch(`/api/jobs/${jobId}/result`,{cache:"no-store"});if(!r.ok)throw new Error(await readError(r));const result=await r.json();
  $("download").href=`/api/jobs/${jobId}/result`;$("result-panel").hidden=false;
  text("result-summary",`${job.summary.sections.toLocaleString("fa-IR")} بخش، ${job.summary.requirements.toLocaleString("fa-IR")} نیازمندی و ${job.summary.claims.toLocaleString("fa-IR")} ادعای استخراج‌شده برای ${result.target.ordering_code || result.target.part_number}.`);
  text("review-note",result.status==="needs_review" ? `${result.issues.length.toLocaleString("fa-IR")} مورد برای بررسی ثبت شده است. وضعیت خروجی needs_review است؛ این عدد، نمرهٔ دقت نیست.` : "بازبینی خودکار انجام شد. وضعیت reviewed به معنی تأیید قطعی یا تأیید انسانی نیست.");
  $("result-sections").replaceChildren();$("result-issues").replaceChildren();$("result-issues").className="issue-list";
  for(const section of result.sections){
    const details=make("details");details.append(make("summary","",`${section.section_id}. ${names[section.section_id-1]} · ${section.status} · ${section.claims.length} ادعا`));
    const content=make("div","result-detail");const list=make("ul");
    for(const finding of section.findings)list.append(make("li","",`${finding.requirement_id}: ${finding.status} — ${finding.reason || ""}`));
    content.append(list);for(const claim of section.claims)content.append(make("p","",`${claim.parameter}: ${claim.raw_value || claim.value || claim.quantity?.expression || "—"}`));
    details.append(content);$("result-sections").append(details);
  }
  for(const issue of result.issues){const item=make("div","issue");item.append(make("strong","",`SECTION ${issue.section_id || "—"} · ${issue.severity || "warning"} · ${issue.category || issue.code || "review"}`),make("span","",issue.explanation || issue.message || JSON.stringify(issue)));$("result-issues").append(item);}
  if(!result.issues.length)$("result-issues").append(make("p","hint","موردی توسط بازبینی خودکار ثبت نشده است."));
}
fetch("/api/config").then(r=>r.json()).then(config=>text("model",config.model)).catch(()=>text("model","سرور در دسترس نیست"));
const saved=new URLSearchParams(location.search).get("job");if(saved && /^[a-f0-9]{32}$/.test(saved)){jobId=saved;busy=true;$("submit").disabled=true;text("submit","در حال بازیابی اجرا…");$("log").replaceChildren();text("job-id",`JOB ${jobId}`);$("cursor").hidden=false;poll();}
