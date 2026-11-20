import{esc,number}from'../utils/format.js';
export const title=(tag,heading,desc)=>`<div class="pagehead"><div class="eyebrow">QINGHE FIELD BOOK / ${tag}</div><h1>${heading}</h1><p>${desc}</p></div>`;
export const specimen=(lot,d)=>{let a=d.accessions.find(a=>a.id===lot.accession_id);return `<div class="specimen"><span class="seedmark">◒</span><div><b>${esc(a.code)} · ${esc(a.name)}</b><small>${esc(lot.code)} / G${lot.generation} / ${number(lot.available)} 粒</small></div></div>`};
export const reserve=(a,d)=>{let total=d.lots.filter(l=>l.accession_id===a.id).reduce((n,l)=>n+l.available,0);return `<div class="reserve"><span>活种保底 / ${esc(a.code)}</span><b>${number(total)} <small>/ 最低 ${number(a.minimum)} 粒</small></b><div class="reserve-track"><i style="width:${Math.min(100,a.minimum/Math.max(total,1)*100)}%"></i></div><small>保底量按同种质全部种批合计，抽样 / 复壮 / 共享共同扣用。</small></div>`};
export const lotOptions=d=>d.lots.map(l=>`<option value="${l.id}">${esc(l.code)} / G${l.generation} / ${l.available}粒</option>`).join('');
export const regenOptions=(d,states=['issued'])=>d.regenerations.filter(r=>states.includes(r.state)).map(r=>`<option value="${r.id}">${esc(r.code)} / ${r.issued}粒</option>`).join('');
export const empty=text=>`<div class="empty">${esc(text)}</div>`;
export const flowerBand=(f,d)=>{let p=d.plots.find(p=>p.id===f.plot_id),r=d.regenerations.find(r=>r.id===f.regeneration_id);return `<div class="flowerband"><strong>${esc(p.code)}</strong><span>${esc(r.code)}</span><i></i><b>${f.start} → ${f.end}</b><small>${esc(f.state)}</small></div>`};
export const moistureRow=(h,d,queue,write)=>{
  let appt=d.moisture_appointments.find(a=>a.harvest_id===h.id),
      slot=appt&&appt.slot_id?d.moisture_slots.find(s=>s.id===appt.slot_id):null,
      latest=d.drying_readings.filter(r=>r.harvest_id===h.id).sort((a,b)=>a.measured_on<b.measured_on?-1:1).at(-1),
      row=queue.bags.find(b=>b.harvest_id===h.id),
      measured=appt&&appt.reading_id,
      status=appt?(measured?'已实测':'已预约'):(row&&row.predicted_ready_on?'待预约':'待排');
  return `<div class="moisture ${measured?'done':''}"><div><b>${esc(h.code)}</b><small>${h.seeds}粒 · 停烘起算 ${esc(h.drying_started_on||'未登记')}</small></div><div><small>最新实测</small><b>${latest?latest.moisture+'%':'—'}</b><small>${latest?esc(latest.measured_on):'尚无读数'}</small></div><div><small>预计达标</small><b>${row&&row.predicted_ready_on?esc(row.predicted_ready_on):'待排'}</b><small>${esc(row?row.reason:'')}</small></div><div><small>复测位</small><b>${slot?esc(slot.on_date):'—'}</b><small>${status}${appt?' · '+esc(appt.code):''}</small></div>${write?`<button data-moisture="${h.id}">实测 / 预约 / 改约</button>`:''}</div>`;
};
export const moistureLegend=q=>`<p class="note">同作物历次含水读数拟合干燥速率（当前${Object.entries(q.crop_rates).map(([k,v])=>esc(k)+'：'+(v?v.rate_pct_per_day+'%/日 / '+v.samples+'样本':'有效样本不足 → 待排')).join('；')}）。停烘区间有重叠只扣一次；样本不足或近期不达标的袋待排，不占复测位。</p>`;
