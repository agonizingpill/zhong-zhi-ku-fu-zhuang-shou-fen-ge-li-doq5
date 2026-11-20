"""Drying-moisture queue: readings, pause merging, rebooking and slot races.

Rules implemented here:

* Readings are grouped by crop (lot -> accession) and fitted into a drying
  rate.  Fewer than two usable slope samples leaves the bag 待排.
* A late 停烘 record may overlap earlier ones.  Overlaps are merged, so the
  same pause day is only deducted once from drying time.
* A bag that already has a measured reading keeps its original appointment and
  that first reading.  A bag still waiting is pulled out of its old slot and
  re-queued at the first free slot after its (re)forecast.  Continued drying
  appends readings without moving the appointment.
* Booking a new appointment and rebooking an old one may race for the same
  slot.  A single queue row lock serialises the two: the first to take it keeps
  the slot, the other bag rolls to the next free date.
"""
from datetime import date, timedelta
from fastapi import HTTPException
from sqlalchemy import select
from .models import (Harvest,Lot,Accession,Regeneration,DryingReading,DryingPause,
                     MoistureSlot,MoistureAppointment,MoistureQueueLock)
from .common import lock,pack,audit
from . import drying_math as m

LOCK_ID=1


def _day(value):
    return value.isoformat() if isinstance(value,date) else str(value)


def _parse(value):
    return date.fromisoformat(str(value))


def lock_queue(s):
    """Serialise slot allocation for the whole station.

    The row is the stable first lock in every queue mutation.  READ COMMITTED
    plus SELECT ... FOR UPDATE means a waiter re-reads committed appointments
    after the winner commits, so it cannot overwrite the claimed slot.
    """
    if not s.get(MoistureQueueLock,LOCK_ID):
        s.add(MoistureQueueLock(id=LOCK_ID));s.flush()
    return s.scalar(
        select(MoistureQueueLock).where(MoistureQueueLock.id==LOCK_ID)
        .with_for_update().execution_options(populate_existing=True)
    )


def _appt(s,harvest_id):
    return s.scalar(select(MoistureAppointment).where(MoistureAppointment.harvest_id==harvest_id)
                    .execution_options(populate_existing=True))


def _readings(s,harvest_id):
    return s.scalars(select(DryingReading).where(DryingReading.harvest_id==harvest_id)
                     .order_by(DryingReading.measured_on,DryingReading.id)
                     .execution_options(populate_existing=True)).all()


def _pauses(s,harvest_id):
    rows=s.scalars(select(DryingPause).where(DryingPause.harvest_id==harvest_id)).all()
    return m.merge_spans([(_parse(x.start_on),_parse(x.end_on)) for x in rows])


def _measured(appt):
    return appt is not None and appt.reading_id is not None


def _qualified(s,harvest):
    """Latest actual reading already meets the ≤12% store threshold."""
    readings=_readings(s,harvest.id)
    return bool(readings) and float(readings[-1].moisture)<=m.THRESHOLD


def _crop(s,harvest):
    regen=s.get(Regeneration,harvest.regeneration_id);lot=s.get(Lot,regen.lot_id)
    return s.get(Accession,lot.accession_id)


def crop_rate(s,harvest):
    """Fit drying speed from this crop's readings across bags.

    Each bag contributes one slope per consecutive reading pair:
    (moisture drop)/(effective drying days), overlap pauses counted once.
    Bags with fewer than two readings contribute nothing; fewer than two usable
    slopes for the crop means the rate is unavailable (待排).
    """
    accession=_crop(s,harvest)
    # Same crop, not necessarily the same accession: drying speed is a crop trait.
    rows=s.execute(
        select(DryingReading,Harvest)
        .join(Harvest,DryingReading.harvest_id==Harvest.id)
        .join(Regeneration,Harvest.regeneration_id==Regeneration.id)
        .join(Lot,Regeneration.lot_id==Lot.id)
        .join(Accession,Lot.accession_id==Accession.id)
        .where(Accession.crop==accession.crop)
        .order_by(DryingReading.measured_on,DryingReading.id)
    ).all()
    grouped={}
    for reading,h in rows:grouped.setdefault(h.id,[]).append(reading)
    samples=[]
    for hid,points in grouped.items():
        points.sort(key=lambda r:(r.measured_on,r.id))
        spans=_pauses(s,hid)
        for before,after in zip(points,points[1:]):
            days=m.effective_drying_days(_parse(before.measured_on),_parse(after.measured_on),spans)
            drop=float(before.moisture)-float(after.moisture)
            if days>0 and drop>0:samples.append(drop/days)
    return accession,m.fit_rate(samples)


def _forecast(s,harvest,rate):
    readings=_readings(s,harvest.id)
    if readings:
        last=readings[-1];start=_parse(last.measured_on);moisture=float(last.moisture)
    elif harvest.drying_started_on:
        start=_parse(harvest.drying_started_on);moisture=float(harvest.moisture)
    else:
        return None,'尚无含水读数与停烘起算日，待排'
    if moisture<=m.THRESHOLD:return None,'已达回库含水，待主管回库，不再排复测位'
    if rate is None:return None,'同作物有效读数不足，待排'
    ready=m.project_ready(start,moisture,rate['rate_pct_per_day'],_pauses(s,harvest.id))
    if ready is None:return None,'按当前速率近期不达标，待排'
    return ready,'按同作物历次读数估计'


def _taken(s,exclude_appt_id=None):
    rows=s.scalars(select(MoistureAppointment).where(MoistureAppointment.slot_id.isnot(None))
                   .execution_options(populate_existing=True)).all()
    return {a.slot_id:a for a in rows if a.id!=exclude_appt_id}


def _slot(s,day):
    slot=s.scalar(select(MoistureSlot).where(MoistureSlot.on_date==_day(day))
                  .execution_options(populate_existing=True))
    if not slot:slot=MoistureSlot(on_date=_day(day));s.add(slot);s.flush()
    return slot


def _claim(s,day,exclude_appt_id=None):
    """First free slot at or after day, never in the past; existing appointments are not evicted."""
    taken=_taken(s,exclude_appt_id);probe=max(day,date.today())
    while True:
        slot=_slot(s,probe)
        if slot.id not in taken:return slot
        probe+=timedelta(days=1)


def _place(s,harvest,appt,forecast):
    slot=_claim(s,forecast)
    appt.slot_id=slot.id;appt.state='booked';appt.predicted_ready_on=_day(forecast)
    return appt,slot


def register_reading(s,d,u):
    """Actual moisture measurement.

    The reading taken at a bag's slot is kept on the appointment and the slot
    stays frozen.  Continuing to dry may warrant further readings; those are
    appended to the book and never move the appointment or overwrite its first
    reading.  A qualified (≤12%) bag stops taking readings until the supervisor
    stores it.
    """
    lock_queue(s)
    harvest=lock(s,Harvest,d.harvest_id)
    if harvest.state!='drying':raise HTTPException(409,'只有干燥中的收获单可登记含水实测')
    if _qualified(s,harvest):raise HTTPException(409,'该袋最新实测已达标，等待主管回库，不再重复登记')
    if d.measured_on>date.today():raise HTTPException(409,'含水实测日期不能晚于今天')
    if harvest.drying_started_on and d.measured_on<_parse(harvest.drying_started_on):
        raise HTTPException(409,'含水实测不能早于停烘起算日')
    reading=DryingReading(harvest_id=harvest.id,measured_on=_day(d.measured_on),
                          moisture=d.moisture,note=d.note)
    s.add(reading);s.flush()
    harvest.moisture=d.moisture
    appt=_appt(s,harvest.id)
    if appt and not appt.reading_id:
        # 第一位实测落在原预约位上：预约与读数从此冻结。
        appt.reading_id=reading.id;appt.state='measured'
        audit(s,u,'登记含水实测，保留原预约 '+harvest.code)
    elif appt:
        audit(s,u,'续测含水，原预约与首位读数保留 '+harvest.code)
    else:
        audit(s,u,'登记含水实测 '+harvest.code)
    return pack(reading)


def pause(s,d,u):
    """Late 停烘 entry; overlaps are merged when time is deducted."""
    harvest=s.get(Harvest,d.harvest_id)
    if not harvest:raise HTTPException(404,'收获单不存在')
    lock_queue(s)
    harvest=lock(s,Harvest,d.harvest_id)
    if harvest.state!='drying':raise HTTPException(409,'只有干燥中的收获单可补记停烘')
    if d.end_on<d.start_on:raise HTTPException(409,'停烘结束不得早于开始')
    x=DryingPause(harvest_id=harvest.id,start_on=_day(d.start_on),end_on=_day(d.end_on),note=d.note)
    s.add(x);s.flush();audit(s,u,'补记停烘 '+harvest.code+' '+x.start_on+'至'+x.end_on)
    return pack(x)


def new(s,d,u):
    """New appointment.  If the requested slot was just taken, roll forward."""
    lock_queue(s)
    harvest=lock(s,Harvest,d.harvest_id)
    if harvest.state!='drying':raise HTTPException(409,'只有干燥中的收获单可预约复测位')
    if _appt(s,harvest.id):raise HTTPException(409,'该袋已有含水复测预约，请改约')
    _,rate=crop_rate(s,harvest)
    forecast,reason=_forecast(s,harvest,rate)
    # 样本不足或近期不达标只能待排，不手工占位。
    if forecast is None:raise HTTPException(409,reason)
    desired=d.slot_on or forecast
    if desired<forecast:raise HTTPException(409,'预约日期早于预计达标日 '+_day(forecast))
    slot=_claim(s,desired)
    appt=MoistureAppointment(harvest_id=harvest.id,code='MS-'+harvest.code,slot_id=slot.id,
                             state='booked',predicted_ready_on=_day(forecast))
    s.add(appt);s.flush();audit(s,u,'预约含水复测位 '+harvest.code)
    return pack(appt)


def rebook(s,appointment_id,d,u):
    """Move a waiting appointment.  Measured appointments are frozen."""
    lock_queue(s)
    pre=s.get(MoistureAppointment,appointment_id)
    if not pre:raise HTTPException(404,'复测预约不存在')
    appt=lock(s,MoistureAppointment,pre.id)
    harvest=lock(s,Harvest,appt.harvest_id)
    if _measured(appt):raise HTTPException(409,'已有实测读数，保留原预约与读数')
    if harvest.state!='drying':raise HTTPException(409,'只有干燥中的收获单可改约')
    forecast,reason=_forecast(s,harvest,crop_rate(s,harvest)[1])
    if forecast is None:raise HTTPException(409,reason)
    desired=d.slot_on or forecast
    if desired<forecast:raise HTTPException(409,'改约日期早于预计达标日 '+_day(forecast))
    slot=_claim(s,desired,exclude_appt_id=appt.id)
    appt.slot_id=slot.id;appt.state='booked';appt.predicted_ready_on=_day(forecast)
    audit(s,u,'改约含水复测位 '+harvest.code)
    return pack(appt)


def schedule(s,u,harvest_id=None):
    """Forecast and slot every drying bag (or one bag).

    Waiting bags give up their old slot before re-queuing, so a bag that is now
    pushed later does not block a bag that is ready earlier.  Re-queue order is
    by forecast date, then bag id, so the earliest-ready bag claims the first
    gap.  Measured bags are untouched and keep their original appointment.
    """
    lock_queue(s)
    bags=s.scalars(select(Harvest).where(Harvest.state=='drying').order_by(Harvest.id)
                   .execution_options(populate_existing=True)).all()
    if harvest_id is not None:bags=[b for b in bags if b.id==harvest_id]
    results=[];waiting=[]
    for bag in bags:
        appt=_appt(s,bag.id)
        if appt and _measured(appt):
            results.append({'harvest':bag.code,'appointment':pack(appt),
                            'slot':_slot_date(s,appt.slot_id),'reason':'已测保留原预约'})
            continue
        if appt:
            appt.slot_id=None;appt.state='queued'
            s.flush()
        _,rate=crop_rate(s,bag)
        forecast,reason=_forecast(s,bag,rate)
        waiting.append((forecast,bag.id,bag,appt,reason))
    waiting.sort(key=lambda row:(row[0] is None,row[0] or date.max,row[1]))
    for forecast,_,bag,appt,reason in waiting:
        if forecast is None:
            if appt:appt.predicted_ready_on=None;appt.state='queued'
            results.append({'harvest':bag.code,'appointment':pack(appt) if appt else None,
                            'slot':None,'reason':reason})
            continue
        if not appt:
            appt=MoistureAppointment(harvest_id=bag.id,code='MS-'+bag.code,state='booked');s.add(appt);s.flush()
        _place(s,bag,appt,forecast)
        results.append({'harvest':bag.code,'appointment':pack(appt),
                        'slot':_slot_date(s,appt.slot_id),'reason':reason})
    return results


def _slot_date(s,slot_id):
    slot=s.get(MoistureSlot,slot_id)
    return slot.on_date if slot else None


def snapshot(s):
    """Read-only view: per drying bag forecast, booked slot and crop rate."""
    bags=s.scalars(select(Harvest).where(Harvest.state=='drying').order_by(Harvest.id)).all()
    rows=[];rates={}
    for bag in bags:
        accession,rate=crop_rate(s,bag)
        rates[accession.code]=rate
        appt=_appt(s,bag.id)
        if appt and _measured(appt):
            rows.append({'harvest':bag.code,'harvest_id':bag.id,'appointment':pack(appt),
                         'slot':_slot_date(s,appt.slot_id),'measured':True,
                         'reason':'已测保留原预约'})
            continue
        forecast,reason=_forecast(s,bag,rate)
        rows.append({'harvest':bag.code,'harvest_id':bag.id,
                     'appointment':pack(appt) if appt else None,
                     'slot':_slot_date(s,appt.slot_id) if appt else None,
                     'predicted_ready_on':_day(forecast) if forecast else None,
                     'measured':False,'reason':reason})
    return {'bags':rows,'crop_rates':rates,'threshold_pct':m.THRESHOLD,
            'min_rate_samples':m.MIN_RATE_SAMPLES}
