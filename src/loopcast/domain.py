"""Pure run rules and value types; no transport, persistence or execution I/O."""
from datetime import datetime, timezone
import hashlib
import json
import uuid

STAGES = ('prepare', 'execute', 'report')
TERMINAL = {'SUCCEEDED', 'FAILED', 'CANCELLED'}


def uid():
    return uuid.uuid4().hex


def now():
    return datetime.now(timezone.utc).isoformat()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class Problem(Exception):
    def __init__(self, code, message, status=409, run_id=None):
        super().__init__(message)
        self.code, self.status, self.run_id = code, status, run_id


def actions(state):
    return {
        'cancel': {'enabled': state in {'QUEUED', 'RUNNING'},
                   'reason_code': 'AVAILABLE' if state in {'QUEUED', 'RUNNING'} else 'CANCEL_NOT_APPLICABLE'},
        'retry': {'enabled': state == 'FAILED',
                  'reason_code': 'AVAILABLE' if state == 'FAILED' else 'RETRY_REQUIRES_FAILURE'},
    }


def validate_input(value):
    if not isinstance(value, dict) or set(value) - {'mode', 'title', 'scenario', 'delay_ms'}:
        raise Problem('INVALID_INPUT', 'Use mode, title, scenario and delay_ms fields only.', 400)
    if value.get('mode') == 'mpt':
        if set(value) - {'mode', 'title'} or not isinstance(value.get('title'), str) or not 1 <= len(value['title'].strip()) <= 120:
            raise Problem('INVALID_INPUT', 'Provide a generation title of 1–120 characters.', 400)
        return {'mode': 'mpt', 'title': value['title'].strip()}
    if value.get('mode', 'fixture') != 'fixture':
        raise Problem('CAPABILITY_UNAVAILABLE', 'Only fixture mode is available.', 422)
    title = value.get('title', 'Untitled fixture')
    scenario = value.get('scenario', 'success')
    delay = value.get('delay_ms', 150)
    if not isinstance(title, str) or not title.strip() or len(title) > 120:
        raise Problem('INVALID_INPUT', 'Title must contain 1–120 characters.', 400)
    if scenario not in ('success', 'fail_once', 'fail', 'slow'):
        raise Problem('INVALID_INPUT', 'Unknown fixture scenario.', 400)
    if type(delay) is not int or not 0 <= delay <= 3000:
        raise Problem('INVALID_INPUT', 'delay_ms must be an integer between 0 and 3000.', 400)
    return {'mode': 'fixture', 'title': title.strip(), 'scenario': scenario, 'delay_ms': delay}
