from fastapi import HTTPException
from sqlalchemy import select
from .models import Accession,Lot,Regeneration,Pollination,Harvest,Flowering,DryingReading,MoistureAppointment
from .common import lock,pack,add,audit

def save(s,d,u,id=None):
    r=lock(s,Regeneration,d.regeneration_id)
    pollen=s.scalar(select(Pollination).where(Pollination.regeneration_id==r.id))
    if not pollen or pollen.state!='pollinated' or r.state!='issued':raise HTTPException(409,'必须完成套袋授粉后才能登记收获')
    x=lock(s,Harvest,id) if id else None
    if x and (x.state!='drying' or x.regeneration_id!=r.id or x.code!=d.code):raise HTTPException(409,'已回库或跨单收获不可修改')
    data=d.model_dump()
    started=data.get('drying_started_on')
    if started is not None:data['drying_started_on']=started.isoformat()
    if x:
        # 停烘起算日一经登记即冻结；后续停烘走独立记录，含水走实测簿。
        if x.drying_started_on:
            if started is not None and started.isoformat()!=x.drying_started_on:
                raise HTTPException(409,'停烘起算日不可覆盖，补记停烘请登记停烘记录')
            data['drying_started_on']=x.drying_started_on
        if s.scalar(select(DryingReading.id).where(DryingReading.harvest_id==x.id)) and \
           float(data.get('moisture',x.moisture))!=float(x.moisture):
            raise HTTPException(409,'已有含水实测簿，请登记实测读数而非改表单含水')
        for k,v in data.items():setattr(x,k,v)
    else:
        x=Harvest(**data);s.add(x)
    s.flush();audit(s,u,'记录收获干燥 '+d.code);return pack(x)

def store(s,id,u):
    pre=s.get(Harvest,id)
    if not pre:raise HTTPException(404,'收获单不存在')
    from .drying_service import lock_queue
    # 复测队列锁先取，回库释放复测位时不会与并发新约/改约互相压位。
    lock_queue(s)
    regen=s.get(Regeneration,pre.regeneration_id);parent=s.get(Lot,regen.lot_id);lock(s,Accession,parent.accession_id)
    # 与save保持相同顺序；种质锁只用于新增种批的保底聚合。
    r=lock(s,Regeneration,pre.regeneration_id);h=lock(s,Harvest,id)
    if h.state!='drying' or r.state!='issued':raise HTTPException(409,'回库不可重复')
    # 回库凭合格实测读数：登记时的含水只是初值，回库看最近一次实测。
    reading=s.scalar(select(DryingReading).where(DryingReading.harvest_id==h.id)
                     .order_by(DryingReading.measured_on.desc(),DryingReading.id.desc()))
    if not reading:raise HTTPException(409,'须先登记含水实测读数（≤12%）再回库')
    if reading.moisture>12:raise HTTPException(409,'最后一次含水实测须≤12%，请干燥合格后再回库')
    appt=s.scalar(select(MoistureAppointment).where(MoistureAppointment.harvest_id==h.id))
    if appt and appt.slot_id is not None:
        # 回库后释放复测位，读数与预约历史照原样保留。
        appt.slot_id=None;appt.state='measured'
    pollen=s.scalar(select(Pollination).where(Pollination.regeneration_id==r.id));f=s.scalar(select(Flowering).where(Flowering.regeneration_id==r.id))
    child=Lot(accession_id=parent.accession_id,code=h.code+'-G'+str(parent.generation+1),generation=parent.generation+1,available=h.seeds,stored_months=0,temperature=-18,initial_rate=parent.initial_rate,parent_lot_id=parent.id);s.add(child);s.flush()
    h.lineage={'parent_lot':pack(parent),'father_lot':pack(s.get(Lot,pollen.father_lot_id)),'pollination':pack(pollen),'flowering':pack(f),'vitality':r.vitality_snapshot,'moisture_reading':pack(reading) if reading else None,'initial_rate_note':'Inherited planning prior; offspring requires independent germination test before sharing'}
    h.state='stored';h.offspring_lot_id=child.id;r.state='harvested';f.state='released';audit(s,u,'主管回库继承G'+str(child.generation)+' '+h.code);return pack(h)
