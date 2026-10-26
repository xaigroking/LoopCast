"""Reproducible finite wall-clock rules backed by measured IANA TZif bytes."""
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
import hashlib
from importlib import resources
from io import BytesIO
from pathlib import Path
import re
from zoneinfo import TZPATH, ZoneInfo, ZoneInfoNotFoundError, available_timezones
from .domain import Problem
from .schedule_models import SchedulePreviewRequest

RULE_VERSION = 'local-daily-v1'


@lru_cache(maxsize=1)
def installed_zones():
    return frozenset(available_timezones()) - {'localtime', 'posixrules'}


@lru_cache(maxsize=128)
def zone_bytes(key):
    if not isinstance(key, str) or len(key) > 128 or not re.fullmatch(r'[A-Za-z0-9_+-]+(?:/[A-Za-z0-9_+-]+)*', key):
        raise Problem('INVALID_TIMEZONE', 'Choose an installed IANA time zone.', 422)
    if key not in installed_zones():raise Problem('INVALID_TIMEZONE', 'Choose an installed IANA time zone.', 422)
    raw = None
    for root in TZPATH:
        path = Path(root).joinpath(*key.split('/'))
        if path.is_file():
            if path.stat().st_size > 1024 * 1024:break
            raw = path.read_bytes();break
    if raw is None:
        try:raw = resources.files('tzdata.zoneinfo').joinpath(*key.split('/')).read_bytes()
        except (ImportError, FileNotFoundError, ModuleNotFoundError, OSError):pass
    try:
        if raw is None or len(raw) > 1024 * 1024:raise ValueError()
        ZoneInfo.from_file(BytesIO(raw), key=key)
    except (ValueError, OSError, ZoneInfoNotFoundError):
        raise Problem('INVALID_TIMEZONE', 'Choose an installed IANA time zone.', 422) from None
    return raw


def spec_value(value):
    return SchedulePreviewRequest.model_validate({'spec': value}).spec.model_dump()


def expand_slots(value, raw=None):
    spec = spec_value(value)
    raw = zone_bytes(spec['timezone']) if raw is None else raw
    zone = ZoneInfo.from_file(BytesIO(raw), key=spec['timezone'])
    start = date.fromisoformat(spec.get('local_date', spec.get('start_local_date')))
    slots = []
    for index, group in enumerate(spec['items']):
        try:day = start + timedelta(days=index)
        except OverflowError:raise Problem('INVALID_TIME_RULE', 'The finite rule exceeds the calendar range.', 422) from None
        local = datetime.fromisoformat(day.isoformat() + 'T' + spec['local_time'])
        candidates = {}
        for fold in (0, 1):
            aware = local.replace(tzinfo=zone, fold=fold)
            try:
                utc = aware.astimezone(timezone.utc)
                if utc.astimezone(zone).replace(tzinfo=None) == local:
                    candidates.setdefault(utc, fold)
            except OverflowError:
                raise Problem('INVALID_TIME_RULE', 'This local rule exceeds the supported UTC calendar.', 422) from None
        selected = min(candidates) if candidates else None
        if selected is None and spec['kind'] == 'ONCE':
            raise Problem('NONEXISTENT_LOCAL_TIME', 'This local time does not exist in the selected zone.', 422)
        slots.append({'slot_key': 'ONCE' if spec['kind'] == 'ONCE' else day.isoformat(), 'position': index,
                      'nominal_local': local.isoformat(timespec='minutes'), 'timezone': spec['timezone'],
                      'tzif_sha256': hashlib.sha256(raw).hexdigest(), 'selected_fold': candidates[selected] if selected else None,
                      'scheduled_at_utc': selected.isoformat() if selected else None,
                      'reason': 'READY' if selected else 'SKIPPED_TIME_GAP', 'group': group})
    return spec, raw, slots
