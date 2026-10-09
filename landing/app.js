'use strict';
const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
const steps = [
  {type:'code',tag:'CODE',title:'سند را به اجزای قابل بررسی تبدیل کن.',text:'PDF فقط متن نیست. جدول، شکل و موقعیت هر بخش را جدا نگه می‌داریم؛ هر شاهد شناسه دارد و به صفحه و مختصات اصلی برمی‌گردد.',fn:'ingestion.ingest() → pages / regions / artifacts',note:'pdfplumber + PDFium؛ تشخیص تصویری جدول و مدار همچنان نیازمند خواندن مدل است.',command:'uv run datasheet ingest "max3232.pdf"',lines:[['info','[document] MAX3232 · 30 pages'],['dim','[parse] text + tables + page images'],['dim','[store] MongoDB metadata / GridFS artifacts'],['success','parse_id=2c0ac822b70de04b3bbb5cee']]},
  {type:'ai',tag:'AI + HUMAN',title:'اول مشخص کن: کدام مدل، کدام پکیج؟',text:'یک دیتاشیت می‌تواند چند کد سفارش و گرید داشته باشد. شواهد سفارش با heuristic جمع می‌شوند، مدل ترکیب‌های مستند را پیدا می‌کند و کاربر هدف خروجی را انتخاب می‌کند.',fn:'discovery_packet() → Engine.discover() → choose_target()',note:'گرید C با I یا پکیج D با DW یکی نیست. هر هدف، run_id مستقل دارد.',command:'uv run datasheet extract 2c0ac822b70de04b3bbb5cee --model gpt-6.1-sol --reasoning medium --prepare-only',lines:[['dim','[targets] documented ordering combinations'],['info','1: MAX3232CD   / SOIC-16 (D) / C: 0..70°C'],['dim','2: MAX3232CDB  / SSOP-16 (DB)'],['dim','3: MAX3232CPW  / TSSOP-16 (PW)'],['question','Select target number: 1'],['success','target=MAX3232CD · run_id=ce40e4a85acc4eb5']],picker:true},
  {type:'heuristic',tag:'HEURISTIC + CODE',title:'نقشهٔ شواهد، به‌جای ارسال کل سند.',text:'برای هر یک از ۵۱ نیازمندی، قطعات مرتبط را پیدا می‌کنیم. عنوان‌ها context می‌دهند؛ ارجاع دور، جدول و پاورقی با روابط صریح به هم وصل می‌شوند.',fn:'ensure_index() → Retriever.select() → map_retrieval()',note:'BM25 زمینه‌دار و روابط دو hop؛ embedding و reranker اختیاری‌اند و در این اجرا فعال نبودند.',command:'uv run datasheet index 2c0ac822b70de04b3bbb5cee',lines:[['info','[index] text / tables / visual anchors'],['dim','[context] heading + page + source region'],['dim','[links] references / captions / conditions'],['success','[coverage] 17 sections · 51 requirements'],['dim','[mapping] deterministic code; no map LLM call']]},
  {type:'heuristic',tag:'HEURISTIC + CODE',title:'هر درخواست، یک بستهٔ شواهد مرتبط.',text:'هر منطقه یک بار وارد بسته می‌شود. شناسهٔ کوتاه eN به شاهد اصلی قابل بازگشت است. تصویر صفحات منتخب همراه متن ارسال می‌شود تا شکل و اتصال‌ها از دست نروند.',fn:'retrieval_packet() → build_packet() → EvidencePacket',note:'Cite alias فقط به شواهد همان بسته نگاشت می‌شود. تصاویر هنوز صفحهٔ کامل‌اند؛ crop دقیق مسیر بهینه‌سازی بعدی است.',command:'uv run datasheet packet ce40e4a85acc4eb5 2 --output .\work\packets\netlist',lines:[['dim','[select] section 2 / existing application circuit'],['info','[packet] 24,216 source characters · 11 images'],['dim','[deduplicate] canonical regions, once each'],['dim','[e19] ↔ source region / page / bbox'],['success','saved: evidence.txt + manifest.json']]},
  {type:'ai',tag:'AI + CODE',title:'مدل استخراج می‌کند؛ کد قرارداد را کنترل می‌کند.',text:'نقش تخصصی هر بخش فقط از شواهد عرضه‌شده پاسخ می‌دهد. ادعا، عدد، واحد، شرایط و citation ساختار دارند؛ validator هدف و قرارداد خروجی را بررسی می‌کند.',fn:'read_retrieved() → section_contract() → validate_section()',note:'نبود شاهد باعث expanded نمی‌شود. ابهام ثبت می‌شود؛ schema صحیح به معنی تفسیر صحیح نیست.',command:'uv run datasheet resume ce40e4a85acc4eb5 --sections 1 --no-review',lines:[['info','extract:1: started'],['success','extract:1: completed'],['dim','[result] 16 pads · 8 claims'],['warning','[conflict] FORCEOFF/FORCEON missing from pin table'],['dim','[policy] expanded disabled · preserve uncertainty']]},
  {type:'ai',tag:'AI · INDEPENDENT READ',title:'یک خواندن مستقل. سپس داوری با سند.',text:'Blind reader جواب اول را نمی‌بیند. reviewer دو پاسخ را با منبع مقایسه می‌کند تا شرایط جاافتاده، هدف نادرست و خطاهای خواندن را پیدا کند. توافق دو پاسخ، جای شاهد را نمی‌گیرد.',fn:'review_retrieved() → blind read → source-grounded review',note:'Review فعلاً گزارش ایراد می‌دهد؛ اصلاح خودکار پاسخ اول پیاده‌سازی نشده است.',command:'uv run datasheet resume ce40e4a85acc4eb5 --sections 1',lines:[['info','blind:1: started'],['success','blind:1: completed'],['info','review:1: started'],['success','review:1: completed'],['warning','[issue] missing capacitor-pin connections'],['warning','[issue] short-circuit conditions omitted']]},
  {type:'code',tag:'CODE · EVALUATION',title:'خروجی نهایی را ذخیره کن. بعد، اندازه بگیر.',text:'هر ۱۷ بخش همراه شواهد و موارد حل‌نشده export می‌شوند. ارزیابی با مرجع مستقل انجام می‌شود؛ نتیجهٔ نیازمندی، عدد، پایه و مدار باید با معیار مناسب سنجیده شود.',fn:'Engine.export() → evaluation.evaluate() → comparison report',note:'مرجع فعلی candidate است. امتیاز exact-match را accuracy کلی نمی‌نامیم.',command:'uv run datasheet export ce40e4a85acc4eb5 outputs/MAX3232CD.json',lines:[['success','[export] 17 sections · 313 claims'],['warning','status=needs_review'],['dim','[compare] independent MAX3232CD candidate'],['info','[aligned] pins 16/16 · topology pairs 25/25'],['info','[coverage] present requirements 27/36'],['warning','[reference] two source-verified corrections']]}
];
const architecture = [
 {title:'Document ingestion',type:'code',fn:'ingestion.ingest()',detail:'آماده‌سازی PDF با pdfplumber و PDFium. متن، جدول، تصویر صفحه و bbox هر منطقه ذخیره می‌شود تا هر ادعا به شاهد قابل مشاهده متصل باشد.'},
 {title:'Target discovery',type:'ai',fn:'Engine.discover()',detail:'شواهد سفارش با heuristic انتخاب می‌شود؛ AI مدل، پکیج، گرید و کد سفارش را از سند تشخیص می‌دهد. کاربر ترکیب هدف را انتخاب می‌کند؛ محدودیت هدف باید در تمام مراحل حفظ شود.'},
 {title:'Human selection',type:'human',fn:'choose_target()',detail:'کاربر ترکیب دقیق مدل و پکیج را انتخاب می‌کند. run_id مستقل، تنظیمات مدل و شناسهٔ هدف را برای resume حفظ می‌کند.'},
 {title:'Contextual index',type:'heuristic',fn:'ensure_index() / Retriever',detail:'ایندکس ساختاری یک بار ساخته می‌شود. BM25 روی متن دارای context، عنوان و اطلاعات صفحه اجرا می‌شود؛ لینک‌های جدول، پاورقی و ارجاع‌ها تا دو hop دنبال می‌شوند.'},
 {title:'Evidence packet',type:'heuristic',fn:'build_packet()',detail:'قطعات انتخاب‌شده بدون تکرار همراه تصاویر صفحه و alias کوتاه eN به مدل می‌رسند. alias ناشناخته پذیرفته نمی‌شود. گسترش خودکار درخواست اکنون حذف شده است.'},
 {title:'Specialist extraction',type:'ai',fn:'read_retrieved()',detail:'نقش تخصصی هر بخش یک بار شواهد را می‌خواند. output schema بخش، ادعاها، شرایط و شواهد را ساختاریافته می‌کند. مدل اجرای واقعی gpt-6.1-sol با reasoning=medium است.'},
 {title:'Structural validation',type:'code',fn:'validate_section()',detail:'کد کامل‌بودن نیازمندی‌های بخش، وجود citation، شناسهٔ هدف، قالب عدد و اتصال مدار را بررسی می‌کند. این کنترل‌ها تفسیر معنایی سند را اثبات نمی‌کنند.'},
 {title:'Blind read + review',type:'ai',fn:'review_retrieved()',detail:'بازخوانی مستقل با جست‌وجوی مکمل، بدون پاسخ اولیه. reviewer سپس هر دو پاسخ و شواهد را می‌گیرد؛ ایرادها را ثبت می‌کند و پاسخ را خودکار اصلاح نمی‌کند.'},
 {title:'Export + evaluation',type:'code',fn:'Engine.export() / evaluate()',detail:'کنترل ارتباط بین بخش‌ها و خروجی JSON هر ۱۷ بخش. evaluate با مرجع، status نیازمندی‌ها، ادعاها، پایه و مدار را مقایسه می‌کند؛ reference و معیارها هنوز نیازمند بازبینی هستند.'}
];

const story = document.querySelector('#story-steps');
const dots = document.querySelector('#step-dots');
steps.forEach((step,index)=>{
 const article=document.createElement('article');article.className='story-step';article.id=`step-${index+1}`;
 const kicker=document.createElement('div');kicker.className='step-kicker';
 const badge=document.createElement('span');badge.className=`node-label ${step.type}`;badge.textContent=step.tag;
 const number=document.createElement('span');number.className='step-number';number.dir='ltr';number.textContent=String(index+1).padStart(2,'0')+' / 07';
 kicker.append(badge,number);
 const title=document.createElement('h3');title.textContent=step.title;
 const text=document.createElement('p');text.textContent=step.text;
 const fn=document.createElement('div');fn.className='function-label';fn.textContent=step.fn;
 const note=document.createElement('p');note.className='step-footnote';note.textContent=step.note;
 article.append(kicker,title,text,fn,note);
 if(step.picker){const options=document.createElement('div');options.className='target-options';[['SOIC (D)',true],['SSOP (DB)',false],['TSSOP (PW)',false]].forEach(([name,enabled])=>{const b=document.createElement('button');b.type='button';b.textContent=name;b.disabled=!enabled;b.className=enabled?'active':'';b.title=enabled?'گزارش این صفحه مربوط به MAX3232CD است':'برای این گزارش فقط هدف MAX3232CD انتخاب شده است';b.addEventListener('click',()=>showStep(index,true));options.append(b)});article.append(options)}
 story.append(article);
 const dot=document.createElement('button');dot.type='button';dot.setAttribute('aria-label',`مرحلهٔ ${index+1}: ${step.title}`);dot.addEventListener('click',()=>article.scrollIntoView({behavior:reducedMotion?'auto':'smooth',block:window.innerWidth<=640?'start':'center'}));dots.append(dot);
});

let activeStep=-1;let timers=[];
const command=document.querySelector('#terminal-command');const output=document.querySelector('#terminal-output');
function later(fn,delay){timers.push(window.setTimeout(fn,delay))}
function showStep(index,force=false){
 if(index===activeStep&&!force)return;
 activeStep=index;timers.forEach(clearTimeout);timers=[];
 document.querySelectorAll('.story-step').forEach((e,i)=>e.classList.toggle('active',i===index));
 [...dots.children].forEach((e,i)=>{e.classList.toggle('active',i===index);e.setAttribute('aria-current',i===index?'step':'false')});
 document.querySelector('#terminal-progress').textContent=String(index+1).padStart(2,'0')+' / 07';
 const role=document.querySelector('#terminal-role');role.className=`node-label ${steps[index].type}`;role.textContent=steps[index].tag;
 command.textContent='';output.replaceChildren();
 const step=steps[index];const speed=reducedMotion?0:Math.min(10,1000/step.command.length);
 if(reducedMotion){command.textContent=step.command}else{[...step.command].forEach((_,i)=>later(()=>command.textContent=step.command.slice(0,i+1),i*speed))}
 step.lines.forEach(([type,text],i)=>later(()=>{const line=document.createElement('span');line.className=`terminal-line ${type}`;line.textContent=text;output.append(line)},reducedMotion?0:step.command.length*speed+150+i*210));
}
document.querySelector('#replay').addEventListener('click',()=>showStep(Math.max(activeStep,0),true));

let framePending=false;
function updateFromScroll(){
 framePending=false;
 const hero=document.querySelector('.hero');const fraction=Math.min(1,window.scrollY/Math.max(hero.offsetHeight*.9,1));
 const heroIndex=Math.min(4,Math.floor(fraction*5));document.querySelectorAll('.hero-word').forEach((e,i)=>e.classList.toggle('active',i===heroIndex));
 const anchor=window.innerWidth<=640?Math.min(window.innerHeight-60,410+(window.innerHeight-410)*.35):window.innerHeight*.48;
 const articles=[...document.querySelectorAll('.story-step')];let closest=0,dist=Infinity;
 articles.forEach((e,i)=>{const r=e.getBoundingClientRect();const d=Math.abs((r.top+r.bottom)/2-anchor);if(d<dist){dist=d;closest=i}});
 const bounds=document.querySelector('.journey-layout').getBoundingClientRect();
 if(bounds.top<anchor&&bounds.bottom>anchor)showStep(closest);
}
window.addEventListener('scroll',()=>{if(!framePending){framePending=true;requestAnimationFrame(updateFromScroll)}},{passive:true});
window.addEventListener('resize',updateFromScroll);showStep(0);

const archFlow=document.querySelector('#arch-flow');
architecture.forEach((node,index)=>{
 const button=document.createElement('button');button.type='button';button.className='arch-node';button.setAttribute('aria-pressed','false');
 const top=document.createElement('div');const badge=document.createElement('span');badge.className=`node-label ${node.type}`;badge.textContent=node.type.toUpperCase();const number=document.createElement('span');number.className='arch-number';number.textContent=String(index+1).padStart(2,'0')+' →';top.append(badge,number);
 const title=document.createElement('h3');title.textContent=node.title;const fn=document.createElement('span');fn.className='arch-function';fn.textContent=node.fn;button.append(top,title,fn);
 button.addEventListener('click',()=>{[...archFlow.children].forEach(e=>{e.classList.remove('selected');e.setAttribute('aria-pressed','false')});button.classList.add('selected');button.setAttribute('aria-pressed','true');const d=document.querySelector('#arch-detail');d.replaceChildren();const label=document.createElement('span');label.className='tiny-label';label.dir='ltr';label.textContent=`${node.type.toUpperCase()} / ${node.fn}`;const p=document.createElement('p');p.textContent=node.detail;d.append(label,p)});archFlow.append(button);
});

const dialog=document.querySelector('#json-dialog');
document.querySelector('#show-sample').addEventListener('click',async()=>{
 document.querySelector('#json-sample').textContent='Loading the recorded summary…';dialog.showModal();
 try{const r=await fetch('reports/output-summary.json');if(!r.ok)throw Error('HTTP '+r.status);document.querySelector('#json-sample').textContent=JSON.stringify(await r.json(),null,2)}catch{document.querySelector('#json-sample').textContent='Summary unavailable. Please use the report download.'}
});
document.querySelector('#close-dialog').addEventListener('click',()=>dialog.close());
dialog.addEventListener('click',e=>{const r=dialog.getBoundingClientRect();if(e.target===dialog&&(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom))dialog.close()});
