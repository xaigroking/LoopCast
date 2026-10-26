"""Pure observation validation and a small versioned manual decision baseline."""
from datetime import datetime, timezone
from .domain import Problem

STRATEGY = 'manual-observation-v1'


def timestamp(value):
    try:
        result=datetime.fromisoformat(value.replace('Z','+00:00'))
        if result.tzinfo is None or result.utcoffset() is None:raise ValueError()
        return result.astimezone(timezone.utc).isoformat(timespec='microseconds')
    except (ValueError,TypeError,AttributeError,OverflowError):
        raise Problem('OBSERVATION_TIME_INVALID','Use an ISO timestamp with an explicit timezone offset.',400) from None


def choose(value, snapshots, works):
    if len(set(value['snapshot_ids'])) != len(value['snapshot_ids']):raise Problem('INVALID_INPUT','Choose each snapshot once.',400)
    modes={s['evidence_mode'] for s in snapshots}
    platforms={s['platform'] for s in snapshots}
    if len(modes)>1 or len(platforms)>1:raise Problem('EVIDENCE_MISMATCH','Use one evidence mode and one platform per decision.',400)
    latest={};excluded={};eligible=[]
    for row in sorted(snapshots,key=lambda s:(s['observed_at'],s['id']),reverse=True):
        identity=row['work_binding_id']
        if identity in latest:excluded[row['id']]='SUPERSEDED';continue
        latest[identity]=row
        metrics=row['metrics'];work=works[identity]
        if row['aggregation']!='cumulative':excluded[row['id']]='CUMULATIVE_REQUIRED';continue
        if metrics['views'] is None:excluded[row['id']]='METRIC_MISSING';continue
        age=(datetime.fromisoformat(row['observed_at'])-datetime.fromisoformat(work['published_at'])).total_seconds()/3600
        if age<value['minimum_age_hours']:excluded[row['id']]='IMMATURE';continue
        if metrics['views']<value['minimum_views']:excluded[row['id']]='LOW_SAMPLE';continue
        eligible.append(row)
    winner=max(eligible,key=lambda s:(s['metrics']['views'],s['observed_at'],s['id'])) if eligible else None
    return {'selected_snapshot_id':winner['id'] if winner else None,'reason':'TOP_OBSERVED_VIEWS' if winner else 'INSUFFICIENT_DATA',
            'excluded':excluded,'evidence_mode':next(iter(modes)) if modes else None}
