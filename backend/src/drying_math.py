"""Pure date/rate helpers for the drying-moisture queue.

No database or fastapi import lives here so the arithmetic can be unit tested
without the service stack.  A pause (停烘) interval is inclusive on both ends;
overlapping late records are merged before any drying time is deducted.
"""
from datetime import date, timedelta
from statistics import median

THRESHOLD=12.0
MIN_RATE_SAMPLES=2
MAX_DRYING_DAYS=730


def merge_spans(spans):
    """Merge inclusive (start,end) date spans so an overlap is only counted once."""
    ordered=sorted((min(a,b),max(a,b)) for a,b in spans)
    merged=[]
    for start,end in ordered:
        if merged and start<=merged[-1][1]+timedelta(days=1):
            if end>merged[-1][1]:merged[-1]=(merged[-1][0],end)
        else:merged.append((start,end))
    return merged


def covered_days(spans,start,end):
    """Days in the half-open drying segment (start,end] covered by inclusive pauses."""
    total=0
    for pause_start,pause_end in merge_spans(spans):
        # An inclusive pause [a,b] is the half-open interval (a-1, b].
        left=max(pause_start-timedelta(days=1),start);right=min(pause_end,end)
        if right>left:total+=(right-left).days
    return total


def effective_drying_days(start,end,spans):
    """Calendar days between two readings minus pause days, each pause counted once."""
    if end<=start:return 0
    return max(0,(end-start).days-covered_days(spans,start,end))


def fit_rate(samples,minimum=MIN_RATE_SAMPLES):
    """samples: moisture points lost per effective drying day.

    A robust median keeps one odd weighing from bending the whole queue.
    Returns {'rate_pct_per_day', 'samples'} or None when there are too few.
    """
    usable=[float(x) for x in samples if x and x>0]
    if len(usable)<minimum:return None
    return {'rate_pct_per_day':round(float(median(usable)),4),'samples':len(usable)}


def is_paused(day,spans):
    return any(start<=day<=end for start,end in spans)


def project_ready(start,moisture,rate,spans,threshold=THRESHOLD):
    """First date on which the bag is forecast to reach the threshold.

    Only drying days count; paused days are skipped.  Returns None when the
    forecast would run past the sanity horizon (treated as 待排).
    """
    if moisture<=threshold:return start
    if rate is None or rate<=0:return None
    needed=int(-(-(moisture-threshold)//rate))  # ceil without float noise
    if needed<0 or needed>MAX_DRYING_DAYS:return None
    day=start;counted=0
    while counted<needed:
        day+=timedelta(days=1)
        if not is_paused(day,spans):counted+=1
    return day


def next_free(day,taken):
    """First date at or after day that is not in the taken set."""
    while day in taken:day+=timedelta(days=1)
    return day
