"""Pure draft, evidence and permanent-release decisions."""
from .domain import Problem


def validate_draft(draft):
    if not draft['title'].strip():
        raise Problem('INVALID_INPUT', 'A title is required.', 400)
    scenes = draft['scenes']
    if len({s['id'] for s in scenes}) != len(scenes) or sum(s['duration'] for s in scenes) > 3600:
        raise Problem('INVALID_INPUT', 'Use unique scenes and a duration of at most one hour.', 400)
    if scenes and any(not scene['narration'].strip() for scene in scenes):
        raise Problem('INVALID_INPUT', 'Each scene needs narration.', 400)
    if len(set(draft['settings']['material_ids'])) != len(draft['settings']['material_ids']):
        raise Problem('INVALID_INPUT', 'Choose each material once.', 400)
    return draft


def batch_state(states):
    if all(s == 'PUBLISHED' for s in states):
        return 'PUBLISHED'
    if any(s in {'UNKNOWN', 'SUBMITTED'} for s in states):
        return 'UNRESOLVED'
    if any(s == 'PUBLISHED' for s in states):
        return 'PARTIAL'
    if any(s in {'PREPARED', 'SENDING'} for s in states):
        return 'ACTIVE'
    if all(s in {'NOT_SENT', 'CANCELLED'} for s in states):
        return 'NOT_SENT'
    return 'READY'


def classify_result(result):
    """A command exit, credential file or submission alone is never publication."""
    state = result.get('outcome')
    work = result.get('work_id')
    if state == 'PUBLISHED' and result.get('confirmed') is True and isinstance(work, str) and 1 <= len(work) <= 128:
        return 'PUBLISHED', work, 'CONFIRMED_WORK'
    if state == 'NOT_SENT' and result.get('boundary_crossed') is False:
        return 'NOT_SENT', None, 'DEFINITE_NOT_SENT'
    if state == 'SUBMITTED':
        return 'SUBMITTED', None, 'SUBMISSION_ONLY'
    return 'UNKNOWN', None, 'UNCONFIRMED_RESULT'
