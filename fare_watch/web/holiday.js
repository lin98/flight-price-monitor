// 首頁「連假便宜機票」：連假來自人事行政總處辦公日曆表，價格來自與其他分頁相同的單程查價。
// 依賴 app.js 的 $、esc、money、stamp、api、post、table、airlineName、setMode。
const HOLIDAY_ORIGIN='TPE';
const placeNames={OKA:'沖繩',KIX:'大阪',NRT:'東京成田',FUK:'福岡',ICN:'首爾',PUS:'釜山',HKG:'香港',BKK:'曼谷'};
const weekdays='日一二三四五六';
const shortDate=v=>{const d=new Date(v+'T12:00:00');return (d.getMonth()+1)+'/'+d.getDate()+'('+weekdays[d.getDay()]+')';};
const leaveText=n=>n?'請 '+n+' 天假':'不用請假';
let holidayBreaks=[],holidayPick=null;

function holidayMessage(s=''){$('holiday-message').textContent=s;$('holiday-message').hidden=!s;}

function renderBreaks(){
$('break-list').innerHTML=holidayBreaks.map((b,i)=>'<button type="button" class="break'+(b===holidayPick?' active':'')+'" data-break="'+i+'" aria-pressed="'+(b===holidayPick)+'"><strong>'+esc(b.name)+'</strong><span>'+shortDate(b.start)+' – '+shortDate(b.end)+'</span><small>連休 '+b.days+' 天</small></button>').join('');
$('break-list').querySelectorAll('[data-break]').forEach(el=>el.onclick=()=>pickBreak(holidayBreaks[Number(el.dataset.break)],false));
}

function renderOptions(b){
$('break-options').innerHTML='<h3>'+esc(b.name)+'怎麼排</h3><p class="meta">連假前後一天都是上班日；多請一天假，機票常常便宜不少。</p><ul class="option-list">'+b.options.map(o=>'<li><strong>'+shortDate(o.depart)+' 去 – '+shortDate(o.return)+' 回</strong><span>共 '+o.total_days+' 天</span><span class="tag">'+leaveText(o.leave_days)+'</span></li>').join('')+'</ul>';
}

function flightCell(f){return esc(airlineName(f.airline))+'<br><span class="time">'+esc(f.depart_time)+' → '+esc(f.arrive_time)+'</span>';}

function renderDeals(report,running){
const box=$('deal-results');
if(!report){box.innerHTML=running?'':'<section class="panel"><p class="meta">這個連假還沒查過票價。</p></section>';return;}
const deals=report.deals||[],best=new Map();
// 排行只留每個地點最便宜的走法；其餘走法收在下方明細，避免同一個地點洗版
for(const d of deals)if(!best.has(d.destination))best.set(d.destination,d);
const row=d=>['<strong>'+esc(placeNames[d.destination]||d.destination)+'</strong> <small>'+esc(d.destination)+'</small>',shortDate(d.depart)+' – '+shortDate(d.return)+'<br><small>共 '+d.total_days+' 天 · '+leaveText(d.leave_days)+'</small>','<strong class="price">'+money(d.price)+'</strong>',flightCell(d.outbound),flightCell(d.inbound),'<button type="button" class="link-button" data-deal="'+deals.indexOf(d)+'">看全部航班</button>'];
const head=['地點','日期','每人兩張單程合計','去程','回程',''];
let html='<section class="panel"><div class="result-head"><h3>便宜地點排行</h3><span class="tag">'+stamp(report.generated_at)+'</span></div>';
html+=best.size?table(head,[...best.values()].map(row)):'<p class="meta">'+(running?'查價中，第一個地點查完就會出現在這裡…':'這次沒有取得可排名的報價，不代表沒有航班。')+'</p>';
html+='<p class="status-note">台北桃園出發 · 1 位成人 · 經濟艙 · 直飛 · 兩張單程相加，不是來回套票；行李與票規需另外確認。已完成 '+report.completed_queries+' / '+report.requested_queries+' 次查詢。</p>';
if(report.aborted_reason)html+='<p class="status-note">本輪提前停止：'+esc(report.aborted_reason)+'</p>';
html+='</section>';
if(deals.length>best.size)html+='<details class="panel"><summary>所有地點 × 請假走法（'+deals.length+' 組）</summary>'+table(head,deals.map(row))+'</details>';
box.innerHTML=html;
box.querySelectorAll('[data-deal]').forEach(el=>el.onclick=()=>{const d=deals[Number(el.dataset.deal)];$('origin').value=report.origin;$('destination').value=d.destination;$('depart').value=d.depart;$('return-date').value=d.return;setMode('dates');$('search-form').requestSubmit();});
}

function holidayBusy(on,line){$('holiday-progress').hidden=!on;$('refresh-deals').disabled=on;if(line)$('holiday-progress-line').textContent=line;}

async function pollDeals(id,b){
try{
const j=await api('/api/jobs/'+encodeURIComponent(id));
if(b!==holidayPick)return;
const last=(j.logs||[]).at(-1);
if(j.report)renderDeals(j.report,j.state==='running');
if(j.state==='running'){holidayBusy(true,last||'正在連線查詢，請稍候…');setTimeout(()=>pollDeals(id,b),2000);return;}
holidayBusy(false);if(j.error)holidayMessage(j.error);
}catch(e){holidayBusy(false);holidayMessage(e.message);}
}

async function startDeals(b,refresh){
holidayMessage();
try{const j=await post('/api/deals',{origin:HOLIDAY_ORIGIN,start:b.start,end:b.end,refresh});holidayBusy(true,'正在連線查詢，請稍候…');if(!$('deal-results').querySelector('table'))renderDeals(null,true);pollDeals(j.id,b);}
catch(e){holidayMessage(e.message);}
}

async function pickBreak(b,autoStart){
holidayPick=b;holidayMessage();renderBreaks();renderOptions(b);holidayBusy(false);$('refresh-deals').hidden=false;
try{
const q=new URLSearchParams({origin:HOLIDAY_ORIGIN,start:b.start,end:b.end}),r=await api('/api/deals?'+q);
if(b!==holidayPick)return;
renderDeals(r.report,!!r.job);
$('refresh-deals').textContent=r.report?'重新查價':'查這個連假的機票';
if(r.job){holidayBusy(true);pollDeals(r.job,b);}
// 只有一進首頁、最近的連假、而且從沒查過時才自動開查；其餘都等使用者按，避免每次開頁面都去打來源
else if(!r.report&&autoStart)startDeals(b,false);
}catch(e){holidayMessage(e.message);}
}

$('refresh-deals').onclick=()=>{if(holidayPick)startDeals(holidayPick,true);};

(async()=>{
try{
const h=await api('/api/holidays');holidayBreaks=h.breaks;
const src=h.sources.map(s=>s.year+' 年（'+stamp(s.fetched_at)+' 下載'+(s.stale?'，本次更新失敗、沿用舊檔':'')+'）').join('、');
$('holiday-source').innerHTML='連假依據：<a href="'+esc(h.source_page)+'" target="_blank" rel="noopener">'+esc(h.source_name)+'</a>'+(src?' · '+esc(src):'')+'<br>'+esc(h.caveat);
if(!h.breaks.length){holidayMessage(h.errors.join('；')||'辦公日曆表上目前沒有即將到來的連假。');return;}
pickBreak(h.breaks[0],true);
}catch(e){holidayMessage(e.message);}
})();
