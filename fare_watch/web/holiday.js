// 首頁「連假便宜機票」：連假來自人事行政總處辦公日曆表，價格來自與其他分頁相同的單程查價。
// 依賴 app.js 的 $、esc、money、stamp、api、post、table、airlineName、setMode。
// CSP 不允許 inline style，所以每個目的地的封面顏色都是 style.css 裡的 .dest-XXX class。
const HOLIDAY_ORIGIN='TPE';
const places={OKA:['沖繩','日本 · 海島度假'],KIX:['大阪','日本 · 關西美食與古都'],NRT:['東京','日本 · 成田機場'],FUK:['福岡','日本 · 九州玄關'],ICN:['首爾','韓國 · 仁川機場'],PUS:['釜山','韓國 · 港都海景'],HKG:['香港','香港 · 週末快閃'],BKK:['曼谷','泰國 · 夜市與按摩']};
const placeName=code=>(places[code]||[code])[0];
const weekdays='日一二三四五六';
const shortDate=v=>{const d=new Date(v+'T12:00:00');return (d.getMonth()+1)+'/'+d.getDate()+'('+weekdays[d.getDay()]+')';};
const leaveText=n=>n?'請 '+n+' 天假':'不用請假';
const optionKey=o=>o.depart+'|'+o.return;
let holidayBreaks=[],holidayPick=null,holidayOption='',holidayReport=null,holidayRunning=false,holidayToday='';

function holidayMessage(s=''){$('holiday-message').textContent=s;$('holiday-message').hidden=!s;}
function daysUntil(v){return Math.round((Date.parse(v+'T12:00:00')-Date.parse(holidayToday+'T12:00:00'))/86400000);}

function renderBreaks(){
$('break-list').innerHTML=holidayBreaks.map((b,i)=>{const d=new Date(b.start+'T12:00:00');return '<button type="button" class="break'+(b===holidayPick?' active':'')+'" data-break="'+i+'" aria-pressed="'+(b===holidayPick)+'"><span class="break-date" aria-hidden="true"><b>'+(d.getMonth()+1)+'月</b>'+d.getDate()+'</span><span class="break-text"><strong>'+esc(b.name)+'</strong><span>'+shortDate(b.start)+' – '+shortDate(b.end)+'</span><small>連休 '+b.days+' 天 · 還有 '+daysUntil(b.start)+' 天</small></span></button>';}).join('');
$('break-list').querySelectorAll('[data-break]').forEach(el=>el.onclick=()=>pickBreak(holidayBreaks[Number(el.dataset.break)],false));
}

// 請假走法同時是篩選器：選了某個走法，卡片就改列每個地點在那幾天的價格
function renderOptions(b){
const choice=(key,title,sub,tag)=>'<button type="button" class="option'+(holidayOption===key?' active':'')+'" data-option="'+esc(key)+'" aria-pressed="'+(holidayOption===key)+'"><strong>'+title+'</strong><span>'+sub+'</span><span class="tag">'+tag+'</span></button>';
$('break-options').innerHTML='<h2 class="section-title">怎麼請假</h2><p class="section-note">連假前後一天都是上班日；多請一天假，機票常常便宜不少。</p><div class="option-list" role="group" aria-label="請假走法">'+choice('','每個地點最便宜的走法','不限請假天數','全部')+b.options.map(o=>choice(optionKey(o),shortDate(o.depart)+' 去 – '+shortDate(o.return)+' 回','共 '+o.total_days+' 天',leaveText(o.leave_days))).join('')+'</div>';
$('break-options').querySelectorAll('[data-option]').forEach(el=>el.onclick=()=>{holidayOption=el.dataset.option;renderOptions(b);renderDeals();});
}

function legRow(label,f){return '<div class="deal-leg"><span class="deal-leg-label">'+label+'</span><span class="deal-leg-airline">'+esc(airlineName(f.airline))+'</span><span class="time">'+esc(f.depart_time)+' → '+esc(f.arrive_time)+'</span></div>';}

function dealCard(code,d,rank,running){
const [name,tagline]=places[code]||[code,''];
const cover='<div class="deal-cover dest-'+esc(code)+'">'+(rank===0&&d?'<span class="deal-rank">最便宜</span>':'')+'<span class="deal-code" aria-hidden="true">'+esc(code)+'</span><h3>'+esc(name)+'</h3><p>'+esc(tagline)+'</p></div>';
if(!d)return '<article class="deal'+(running?' loading':' empty-deal')+'">'+cover+'<div class="deal-body">'+(running?'<div class="skeleton wide"></div><div class="skeleton"></div><div class="skeleton"></div><p class="deal-wait">查價中…</p>':'<p class="deal-wait">這個走法沒有取得報價，不代表沒有航班。</p>')+'</div></article>';
return '<article class="deal">'+cover+'<div class="deal-body"><p class="deal-price"><small>每人 · 兩張單程合計</small><strong>'+money(d.price)+'</strong><span>起</span></p><p class="deal-dates">'+shortDate(d.depart)+' – '+shortDate(d.return)+' · 共 '+d.total_days+' 天 <span class="tag'+(d.leave_days?'':' tag-ok')+'">'+leaveText(d.leave_days)+'</span></p>'+legRow('去',d.outbound)+legRow('回',d.inbound)+'<button type="button" class="primary deal-cta" data-deal="'+esc(code+'|'+optionKey(d))+'">看全部航班 <span aria-hidden="true">→</span></button></div></article>';
}

function renderDeals(){
const box=$('deal-results'),report=holidayReport,running=holidayRunning;
if(!report){box.innerHTML=running?'':'<section class="panel"><p class="meta empty-note">這個連假還沒查過票價，按下方按鈕開始比較 8 個地點。</p></section>';return;}
const deals=report.deals||[],pool=holidayOption?deals.filter(d=>optionKey(d)===holidayOption):deals,best=new Map();
// deals 已依價格排序；每個地點第一次出現的就是它在這個篩選下最便宜的走法
for(const d of pool)if(!best.has(d.destination))best.set(d.destination,d);
const order=[...best.keys()].concat((report.destinations||[]).filter(c=>!best.has(c)));
let html='<div class="result-head"><h2 class="section-title">'+(holidayOption?'這樣請假，可以去哪裡':'便宜地點排行')+'</h2><span class="tag">報價時間 '+stamp(report.generated_at)+'</span></div>';
html+='<div class="deal-grid">'+order.map((code,i)=>dealCard(code,best.get(code),i,running)).join('')+'</div>';
html+='<p class="status-note">台北桃園出發 · 1 位成人 · 經濟艙 · 直飛 · 兩張單程相加，不是來回套票；行李與票規需另外確認。已完成 '+report.completed_queries+' / '+report.requested_queries+' 次查詢。</p>';
if(report.aborted_reason)html+='<p class="status-note">本輪提前停止：'+esc(report.aborted_reason)+'</p>';
if(deals.length)html+='<details class="panel"><summary>所有地點 × 請假走法（'+deals.length+' 組）</summary>'+table(['地點','日期','每人兩張單程合計','去程','回程'],deals.map(d=>['<strong>'+esc(placeName(d.destination))+'</strong> <small>'+esc(d.destination)+'</small>',shortDate(d.depart)+' – '+shortDate(d.return)+'<small>共 '+d.total_days+' 天 · '+leaveText(d.leave_days)+'</small>','<strong class="price">'+money(d.price)+'</strong>',esc(airlineName(d.outbound.airline))+'<br><span class="time">'+esc(d.outbound.depart_time)+' → '+esc(d.outbound.arrive_time)+'</span>',esc(airlineName(d.inbound.airline))+'<br><span class="time">'+esc(d.inbound.depart_time)+' → '+esc(d.inbound.arrive_time)+'</span>']))+'</details>';
box.innerHTML=html;
box.querySelectorAll('[data-deal]').forEach(el=>el.onclick=()=>{const [code,depart,ret]=el.dataset.deal.split('|');$('origin').value=report.origin;$('destination').value=code;$('depart').value=depart;$('return-date').value=ret;setMode('dates');$('search-form').requestSubmit();$('search-form').scrollIntoView({block:'start'});});
}

// hero 右側的登機證：只講最近的那個連假，不跟著下方選到的連假變
function renderTicket(report){
const b=holidayBreaks[0];if(!b)return;
const cheapest=report&&report.deals&&report.deals[0];
$('hero-ticket').innerHTML='<p class="ticket-label">下一個連假</p><h2>'+esc(b.name)+'</h2><p class="ticket-dates">'+shortDate(b.start)+' – '+shortDate(b.end)+'</p><div class="ticket-row"><div><small>連休</small><strong>'+b.days+' 天</strong></div><div><small>倒數</small><strong>'+daysUntil(b.start)+' 天</strong></div><div><small>'+(cheapest?'最低 · '+esc(placeName(cheapest.destination)):'最低票價')+'</small><strong class="ticket-price">'+(cheapest?money(cheapest.price):holidayRunning?'查價中':'尚未查價')+'</strong></div></div>';
$('hero-ticket').hidden=false;
}

function setDeals(report,running,b){holidayReport=report;holidayRunning=running;renderDeals();if(b===holidayBreaks[0])renderTicket(report);}
function holidayBusy(on,line){$('holiday-progress').hidden=!on;$('refresh-deals').disabled=on;if(line)$('holiday-progress-line').textContent=line;}

async function pollDeals(id,b){
try{
const j=await api('/api/jobs/'+encodeURIComponent(id));
if(b!==holidayPick)return;
const last=(j.logs||[]).at(-1),running=j.state==='running';
if(j.report)setDeals(j.report,running,b);
if(running){holidayBusy(true,last||'正在連線查詢，請稍候…');setTimeout(()=>pollDeals(id,b),2000);return;}
setDeals(holidayReport,false,b);holidayBusy(false);$('refresh-deals').textContent='重新查價';if(j.error)holidayMessage(j.error);
}catch(e){setDeals(holidayReport,false,b);holidayBusy(false);holidayMessage(e.message);}
}

async function startDeals(b,refresh){
holidayMessage();
try{const j=await post('/api/deals',{origin:HOLIDAY_ORIGIN,start:b.start,end:b.end,refresh});holidayBusy(true,'正在連線查詢，請稍候…');setDeals(holidayReport,true,b);pollDeals(j.id,b);}
catch(e){holidayMessage(e.message);}
}

async function pickBreak(b,autoStart){
holidayPick=b;holidayOption='';holidayMessage();renderBreaks();renderOptions(b);holidayBusy(false);$('refresh-deals').hidden=false;
try{
const q=new URLSearchParams({origin:HOLIDAY_ORIGIN,start:b.start,end:b.end}),r=await api('/api/deals?'+q);
if(b!==holidayPick)return;
setDeals(r.report,!!r.job,b);
$('refresh-deals').textContent=r.report?'重新查價':'查這個連假的機票';
if(r.job){holidayBusy(true);pollDeals(r.job,b);}
// 只有一進首頁、最近的連假、而且從沒查過時才自動開查；其餘都等使用者按，避免每次開頁面都去打來源
else if(!r.report&&autoStart)startDeals(b,false);
}catch(e){holidayMessage(e.message);}
}

$('refresh-deals').onclick=()=>{if(holidayPick)startDeals(holidayPick,true);};

(async()=>{
try{
const h=await api('/api/holidays');holidayBreaks=h.breaks;holidayToday=h.today;
const src=h.sources.map(s=>s.year+' 年（'+stamp(s.fetched_at)+' 下載'+(s.stale?'，本次更新失敗、沿用舊檔':'')+'）').join('、');
$('holiday-source').innerHTML='連假依據：<a href="'+esc(h.source_page)+'" target="_blank" rel="noopener">'+esc(h.source_name)+'</a>'+(src?' · '+esc(src):'')+'<br>'+esc(h.caveat);
if(!h.breaks.length){holidayMessage(h.errors.join('；')||'辦公日曆表上目前沒有即將到來的連假。');return;}
renderTicket(null);pickBreak(h.breaks[0],true);
}catch(e){holidayMessage(e.message);}
})();
