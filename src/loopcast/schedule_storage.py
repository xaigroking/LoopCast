"""Finite schedule identity and immutable revisions in the existing transaction owner."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
from .domain import Problem, canonical, digest, uid
from .schedule_time import expand_slots


class ScheduleMethods:
    def _schedule_event(self, db, identity, kind, reason, occurrence=None):
        db.execute('INSERT INTO schedule_events VALUES(?,?,?,?,?,?)',
                   (uid(), identity, occurrence, kind, reason, self.clock.iso()))

    def _schedule_row(self, db, identity):
        row = db.execute('SELECT * FROM schedules WHERE id=?', (identity,)).fetchone()
        if row is None:raise Problem('NOT_FOUND', 'The schedule does not exist.', 404)
        return row

    def _schedule_version(self, db, identity, day=None):
        if day is None:
            return db.execute('SELECT * FROM schedule_versions WHERE schedule_id=? ORDER BY number DESC LIMIT 1', (identity,)).fetchone()
        return db.execute('SELECT * FROM schedule_versions WHERE schedule_id=? AND (effective_date IS NULL OR effective_date<=?) ORDER BY number DESC LIMIT 1', (identity, day)).fetchone()

    def _schedule_data(self, db, value, capabilities, mode, identity=None, raw=None):
        spec, raw, slots = expand_slots(value, raw)
        frozen, blockers = [], []
        for slot in slots:
            group = slot['group']
            batch = db.execute('SELECT * FROM release_batches WHERE id=?', (group['batch_id'],)).fetchone()
            if batch is None:raise Problem('NOT_FOUND', 'A frozen release does not exist.', 404)
            asset = self._asset(db, batch['asset_id'])
            original = json.loads(batch['frozen'])
            for target_id in group['target_ids']:
                target = db.execute('SELECT * FROM release_targets WHERE id=? AND batch_id=?', (target_id, batch['id'])).fetchone()
                if target is None:raise Problem('INVALID_INPUT', 'The target is not part of this frozen release.', 400)
                owner = db.execute('SELECT * FROM schedule_target_owners WHERE target_id=? AND active=1', (target_id,)).fetchone()
                old_occurrence = db.execute('SELECT t.state FROM schedule_occurrence_targets t JOIN schedule_occurrences o ON o.id=t.occurrence_id WHERE o.schedule_id=? AND t.target_id=? ORDER BY o.rowid DESC LIMIT 1', (identity, target_id)).fetchone() if identity else None
                settled = old_occurrence and old_occurrence['state'] in {'SKIPPED_MISSED','SKIPPED_TIME_GAP','CANCELLED','PUBLISHED'}
                if owner and owner['schedule_id'] != identity and not settled:raise Problem('TARGET_ALREADY_SCHEDULED', 'This target already belongs to another schedule.', 409)
                account = self._account(db, target['account_id'])
                cap = capabilities[account['platform']]
                fields = json.loads(target['frozen'])
                historical = identity and db.execute('SELECT 1 FROM schedule_occurrence_targets t JOIN schedule_occurrences o ON o.id=t.occurrence_id WHERE o.schedule_id=? AND t.target_id=?', (identity, target_id)).fetchone()
                reasons = []
                if not historical and target['state'] not in {'READY', 'NOT_SENT'}:reasons.append('PUBLICATION_NOT_RETRYABLE')
                if self._restore_guard(db):reasons.append('RESTORE_GUARD_ACTIVE')
                if asset['kind'] != 'video' or asset['sha256'] != fields['asset_sha256']:reasons.append('ASSET_CHANGED')
                if target['evidence_mode'] != mode or account['evidence_mode'] != mode:reasons.append('EVIDENCE_MISMATCH')
                if account['stable_subject'] != target['stable_subject']:reasons.append('ACCOUNT_IDENTITY_CHANGED')
                if not cap['configured'] or not cap['video']:reasons.append('CAPABILITY_UNAVAILABLE')
                if account['state'] != 'AVAILABLE':reasons.append('ACCOUNT_UNAVAILABLE')
                if not account['checked_at'] or self.clock.now() - datetime.fromisoformat(account['checked_at']).timestamp() > 300:reasons.append('ACCOUNT_STALE')
                if len(original['title']) > cap['title_limit']:reasons.append('TITLE_TOO_LONG')
                if fields['fields'].get('declaration') and not cap['ai_disclosure']:reasons.append('AI_DISCLOSURE_UNAVAILABLE')
                if cap['requires_category'] and not fields['fields'].get('category'):reasons.append('CATEGORY_REQUIRED')
                if fields['fields']['visibility'] not in cap['visibility_options']:reasons.append('VISIBILITY_UNAVAILABLE')
                blocked = self._release_blocker(db, account, target['family_id'], exclude=target_id)
                if blocked:reasons.append('PERMANENT_DUPLICATE' if blocked == 'PUBLISHED' else 'PUBLICATION_UNRESOLVED')
                frozen.append({'target_id': target_id, 'batch_id': batch['id'], 'target_version': target['version'],
                               'account_id': account['id'], 'account_version': account['version'], 'platform': target['platform'],
                               'stable_subject': target['stable_subject'], 'evidence_mode': target['evidence_mode'],
                               'asset_id': asset['id'], 'asset_sha256': asset['sha256'], 'family_id': target['family_id'],
                               'input_digest': batch['input_digest']})
                if reasons and not (historical and target['state'] in {'PUBLISHED', 'CANCELLED'}):
                    blockers.append({'target_id': target_id, 'reasons': reasons})
        return {'spec': spec, 'slots': slots, 'frozen': frozen, 'blockers': blockers,
                'time_rule_version': 'local-daily-v1', 'fold_policy': 'EARLIER', 'gap_policy': 'SKIP',
                'tzif_sha256': hashlib.sha256(raw).hexdigest(), 'tzdb_version': None}, raw

    def schedule_preview(self, value, capabilities, mode, identity=None, expected_version=None, cursor=None):
        if cursor is not None:
            try:key, offset = cursor.split('.');offset = int(offset)
            except (ValueError, AttributeError):raise Problem('INVALID_CURSOR', 'The preview cursor is invalid.', 400) from None
            cached = self._schedule_previews.get(key)
            if cached is None or cached['expires'] < self.clock.now() or cached['value'] != value or cached['identity'] != identity or cached['version'] != expected_version or not 0 <= offset < len(cached['data']['slots']):
                raise Problem('PREVIEW_EXPIRED', 'Preview the complete schedule again.', 409)
        else:
            with self.transaction() as db:
                if identity:
                    row = self._schedule_row(db, identity)
                    if row['version'] != expected_version:raise Problem('VERSION_CONFLICT', 'Refresh the schedule.', 409)
                    version = self._schedule_version(db, identity)
                    raw = bytes(version['tzif'])
                else:raw = None
                data, raw = self._schedule_data(db, value, capabilities, mode, identity, raw)
            key = digest({'nonce': uid(), 'data': data});offset = 0
            cached = {'data': data, 'raw': raw, 'expires': self.clock.now() + 60, 'value': json.loads(canonical(value)),
                      'identity': identity, 'version': expected_version}
            if len(self._schedule_previews) >= 64:self._schedule_previews.pop(next(iter(self._schedule_previews)))
            self._schedule_previews[key] = cached
        result = {k: v for k, v in cached['data'].items() if k not in {'spec','slots','frozen'}}
        result.update({'preview_digest': key, 'confirmation_digest': key, 'expires_at': datetime.fromtimestamp(cached['expires'], timezone.utc).isoformat(),
                       'total': len(cached['data']['slots']), 'items': cached['data']['slots'][offset:offset + 31],
                       'next_cursor': key + '.' + str(offset + 31) if offset + 31 < len(cached['data']['slots']) else None})
        return result

    def _schedule_confirm(self, db, value, checksum, capabilities, mode, identity=None, version=None):
        cached = self._schedule_previews.get(checksum)
        if not cached or cached['expires'] < self.clock.now() or cached['value'] != value or cached['identity'] != identity or cached['version'] != version:
            raise Problem('PREVIEW_EXPIRED', 'Preview and explicitly confirm the schedule again.', 409)
        current, raw = self._schedule_data(db, value, capabilities, mode, identity, cached['raw'])
        if current != cached['data']:raise Problem('VERSION_CONFLICT', 'Frozen inputs or account capabilities changed. Preview again.', 409)
        return current, raw

    def _schedule_limits(self, db, data):
        spec, settings = data['spec'], self.schedule_settings
        count = sum(len(g['target_ids']) for g in spec['items'])
        if len(spec['items']) > settings.max_slots_per_plan or spec['quota']['attempts_per_utc_day'] > settings.plan_attempts_per_utc_day or count > min(spec['quota']['pending_targets'], settings.plan_pending_targets):
            raise Problem('SCHEDULE_LIMIT', 'The finite schedule exceeds trusted limits.', 429)
        if spec['missed']['policy'] == 'BOUNDED_CATCHUP' and (spec['missed']['max_age_seconds'] > settings.max_catchup_age_seconds or spec['missed']['max_slots'] > settings.max_catchup_slots):
            raise Problem('SCHEDULE_LIMIT', 'The requested catch-up exceeds trusted limits.', 429)
        if db.execute("SELECT COUNT(*) FROM schedules s WHERE control_state!='CANCELLED' AND EXISTS(SELECT 1 FROM schedule_target_owners o WHERE o.schedule_id=s.id AND o.active=1)").fetchone()[0] >= settings.max_active_plans:
            raise Problem('SCHEDULE_LIMIT', 'The plan limit has been reached.', 429)
        owned = db.execute('SELECT COUNT(*) FROM schedule_target_owners WHERE active=1').fetchone()[0]
        if owned + count > settings.max_pending_targets:raise Problem('SCHEDULE_LIMIT', 'The pending target limit has been reached.', 429)
        subjects = {}
        for target in data['frozen']:
            key = (target['platform'], target['stable_subject'], target['evidence_mode'])
            subjects[key] = subjects.get(key, 0) + 1
        for key, addition in subjects.items():
            pending = db.execute('SELECT COUNT(*) FROM schedule_target_owners o JOIN release_targets t ON t.id=o.target_id WHERE o.active=1 AND t.platform=? AND t.stable_subject=? AND t.evidence_mode=?', key).fetchone()[0]
            if pending + addition > settings.account_pending_targets:raise Problem('SCHEDULE_LIMIT', 'The account pending target limit has been reached.', 429)

    def schedule_create(self, value, preview_digest, key, capabilities, mode):
        request = {'spec': value, 'preview_digest': preview_digest}
        with self.transaction(True) as db:
            prior = self._studio_prior(db, 'schedule.create', key, request)
            if prior:return self._schedule(db, prior['id'])
            data, raw = self._schedule_confirm(db, value, preview_digest, capabilities, mode)
            self._schedule_limits(db, data)
            if any(s['scheduled_at_utc'] and datetime.fromisoformat(s['scheduled_at_utc']).timestamp() <= self.clock.now() for s in data['slots']):
                raise Problem('INVALID_TIME_RULE', 'New slots must be in the future.', 422)
            if any('PUBLICATION_NOT_RETRYABLE' in b['reasons'] for b in data['blockers']):raise Problem('PUBLICATION_NOT_RETRYABLE', 'Only unsent frozen targets may be scheduled.', 409)
            identity, timestamp = uid(), self.clock.iso()
            db.execute("INSERT INTO schedules VALUES(?,1,1,'PAUSED','AWAITING_CONFIRMATION',NULL,NULL,?)", (identity, timestamp))
            db.execute('INSERT INTO schedule_versions VALUES(?,1,?,?,?,?,NULL,?)', (identity, canonical(data['spec']), raw, data['tzif_sha256'], canonical(data['frozen']), timestamp))
            for slot in data['slots']:
                db.execute('INSERT INTO schedule_slots VALUES(?,?,?,?,?)', (identity, slot['slot_key'], slot['position'], slot['nominal_local'][:10], canonical(slot['group'])))
                for target in slot['group']['target_ids']:
                    db.execute('INSERT INTO schedule_target_owners VALUES(?,?,?,1,1) ON CONFLICT(target_id) DO UPDATE SET schedule_id=excluded.schedule_id,slot_key=excluded.slot_key,active=1,assignment_version=assignment_version+1', (target, identity, slot['slot_key']))
            self._schedule_event(db, identity, 'CREATED', 'AWAITING_CONFIRMATION')
            self._studio_remember(db, 'schedule.create', key, request, {'id': identity})
            return self._schedule(db, identity)

    def _schedule(self, db, identity):
        row = dict(self._schedule_row(db, identity));version = self._schedule_version(db, identity)
        counts = {r['state']: r['n'] for r in db.execute('SELECT t.state,COUNT(*) n FROM schedule_occurrence_targets t JOIN schedule_occurrences o ON o.id=t.occurrence_id WHERE o.schedule_id=? GROUP BY t.state', (identity,))}
        total = db.execute('SELECT COUNT(*) FROM schedule_slots WHERE schedule_id=?', (identity,)).fetchone()[0]
        done = db.execute('SELECT COUNT(*) FROM schedule_occurrences WHERE schedule_id=?', (identity,)).fetchone()[0]
        inflight = [dict(r) for r in db.execute("SELECT t.id target_id,a.id attempt_id,a.state FROM release_attempts a JOIN release_targets t ON t.id=a.target_id JOIN schedule_job_bindings b ON b.target_id=t.id JOIN studio_jobs j ON j.id=b.job_id AND j.token=a.token WHERE b.schedule_id=? AND a.state='SENDING'", (identity,))]
        state = 'WAITING' if row['control_state'] == 'ACTIVE' else row['control_state']
        if any(counts.get(s) for s in ('UNKNOWN','SUBMITTED')):state = 'UNRESOLVED'
        elif inflight:state = 'EXECUTING'
        elif counts.get('PUBLISHED'):
            state = 'PUBLISHED' if done == total and sum(counts.values()) == counts['PUBLISHED'] else 'PARTIAL'
        elif done == total and not any(counts.get(s) for s in ('READY','PREPARED','SENDING','WAITING_ACCOUNT','WAITING_CAPABILITY','WAITING_QUOTA','BLOCKED_RESTORE')):state = 'FINISHED_WITHOUT_PUBLICATION'
        return {'id': row['id'], 'control_version': row['version'], 'spec_version': row['spec_version'],
                'control_state': row['control_state'], 'display_state': state, 'pause_reason': row['pause_reason'],
                'confirmed_digest': row['confirmed_digest'], 'confirmed_at': row['confirmed_at'], 'created_at': row['created_at'],
                'spec': json.loads(version['spec']), 'tzif_sha256': version['tzif_sha256'], 'counts': counts,
                'slot_count': total, 'classified_count': done, 'in_flight': inflight}

    def schedule_get(self, identity):
        with self.transaction() as db:return self._schedule(db, identity)

    def schedule_list(self, before=None, limit=30):
        with self.transaction() as db:
            rows = db.execute('SELECT rowid,id FROM schedules WHERE rowid<COALESCE(?,9223372036854775807) ORDER BY rowid DESC LIMIT ?', (before, limit + 1)).fetchall()
            return {'items': [self._schedule(db, r['id']) for r in rows[:limit]], 'next_cursor': str(rows[limit-1]['rowid']) if len(rows) > limit else None}

    def schedule_update(self, identity, value, expected_version, checksum, effective, key, capabilities, mode):
        request = {'spec': value, 'expected_version': expected_version, 'preview_digest': checksum, 'effective_from_local_date': effective}
        with self.admission_lock, self.transaction(True) as db:
            if self._studio_prior(db, 'schedule.update:' + identity, key, request):return self._schedule(db, identity)
            row = self._schedule_row(db, identity)
            if row['version'] != expected_version:raise Problem('VERSION_CONFLICT', 'Refresh the schedule.', 409)
            if row['control_state'] != 'PAUSED':raise Problem('ACTION_UNAVAILABLE', 'Pause the schedule before editing.', 409)
            data, raw = self._schedule_confirm(db, value, checksum, capabilities, mode, identity, expected_version)
            previous = self._schedule_version(db, identity);old = json.loads(previous['spec'])
            if any(old.get(k) != data['spec'].get(k) for k in ('kind','timezone','local_date','start_local_date','items')):
                raise Problem('IMMUTABLE_SCHEDULE_IDENTITY', 'Create a new plan to change dates, time zone or targets.', 409)
            from io import BytesIO
            from zoneinfo import ZoneInfo
            today = datetime.fromtimestamp(self.clock.now(), ZoneInfo.from_file(BytesIO(raw))).date()
            if value['kind'] == 'DAILY_LOCAL':
                try:day = datetime.fromisoformat(effective).date()
                except (TypeError, ValueError):raise Problem('INVALID_TIME_RULE', 'Choose a future effective date.', 422) from None
                if day <= today:raise Problem('INVALID_TIME_RULE', 'Changes take effect on a later local date.', 422)
            elif effective is not None:raise Problem('INVALID_TIME_RULE', 'One-time changes do not take an effective date.', 422)
            old_slots = {s['slot_key']: s for s in expand_slots(old, bytes(previous['tzif']))[2]}
            affected = [s for s in data['slots'] if not effective or s['nominal_local'][:10] >= effective]
            if not affected:raise Problem('INVALID_TIME_RULE', 'The change must affect a future slot.', 422)
            for slot in affected:
                if effective and slot['nominal_local'][:10] < effective:continue
                if db.execute('SELECT 1 FROM schedule_occurrences WHERE schedule_id=? AND slot_key=?', (identity, slot['slot_key'])).fetchone():
                    raise Problem('OCCURRENCE_FROZEN', 'An existing occurrence cannot be edited.', 409)
                old_slot = old_slots[slot['slot_key']]
                if any(s['scheduled_at_utc'] and datetime.fromisoformat(s['scheduled_at_utc']).timestamp() <= self.clock.now() for s in (old_slot, slot)):
                    raise Problem('OCCURRENCE_FROZEN', 'A due slot cannot be edited.', 409)
            if value['quota']['attempts_per_utc_day'] > self.schedule_settings.plan_attempts_per_utc_day or value['quota']['pending_targets'] > self.schedule_settings.plan_pending_targets:
                raise Problem('SCHEDULE_LIMIT', 'The requested quota exceeds trusted limits.', 429)
            if value['missed']['policy'] == 'BOUNDED_CATCHUP' and (value['missed']['max_age_seconds'] > self.schedule_settings.max_catchup_age_seconds or value['missed']['max_slots'] > self.schedule_settings.max_catchup_slots):
                raise Problem('SCHEDULE_LIMIT', 'The catch-up exceeds trusted limits.', 429)
            pending_count = db.execute('SELECT COUNT(*) FROM schedule_target_owners WHERE schedule_id=? AND active=1', (identity,)).fetchone()[0]
            if pending_count > value['quota']['pending_targets']:
                raise Problem('SCHEDULE_LIMIT', 'The pending limit cannot be below existing target authorizations.', 429)
            number = row['spec_version'] + 1
            db.execute('INSERT INTO schedule_versions VALUES(?,?,?,?,?,?,?,?)', (identity, number, canonical(data['spec']), raw, data['tzif_sha256'], canonical(data['frozen']), effective, self.clock.iso()))
            db.execute('UPDATE schedules SET spec_version=?,version=version+1,confirmed_digest=NULL,confirmed_at=NULL WHERE id=?', (number, identity))
            self._schedule_event(db, identity, 'UPDATED', 'RECONFIRM_REQUIRED')
            self._studio_remember(db, 'schedule.update:' + identity, key, request, {'id': identity})
            return self._schedule(db, identity)

    def schedule_control(self, identity, action, expected_version, key, capabilities, mode, checksum=None):
        request = {'action': action, 'expected_version': expected_version, 'confirmation_digest': checksum}
        with self.admission_lock, self.transaction(True) as db:
            if self._studio_prior(db, 'schedule.control:' + identity, key, request):return self._schedule(db, identity)
            row = self._schedule_row(db, identity)
            if type(expected_version) is not int or row['version'] != expected_version:raise Problem('VERSION_CONFLICT', 'Refresh the schedule.', 409)
            states = {'pause':'PAUSED','resume':'ACTIVE','cancel':'CANCELLED'}
            if action not in states:raise Problem('ACTION_UNAVAILABLE', 'Unknown control action.', 400)
            if row['control_state'] == 'CANCELLED' and action != 'cancel':raise Problem('ACTION_UNAVAILABLE', 'A cancelled schedule stays cancelled.', 409)
            if action == 'resume':
                self._require_upload(db)
                if not self.schedule_settings.enabled or mode == 'live' and not self.schedule_settings.allow_live:
                    raise Problem('LOCAL_SCHEDULING_DISABLED', 'Automatic local publication is disabled by trusted configuration.', 409)
                version = self._schedule_version(db, identity)
                data, _ = self._schedule_confirm(db, json.loads(version['spec']), checksum, capabilities, mode, identity, expected_version)
                if data['blockers']:raise Problem('PREFLIGHT_BLOCKED', 'Resolve the frozen target checks before enabling.', 409)
            rearmed = 0
            if action == 'resume':
                rearmed = db.execute("UPDATE schedule_occurrence_targets SET state='READY',reason='USER_RECONFIRMED' WHERE state='WAITING_CONFIRMATION' AND occurrence_id IN (SELECT id FROM schedule_occurrences WHERE schedule_id=?)", (identity,)).rowcount
            elif action in {'pause','cancel'}:
                self._schedule_revoke(db, identity, 'USER_' + action.upper())
            if row['control_state'] != states[action] or rearmed:
                db.execute('UPDATE schedules SET control_state=?,version=version+1,pause_reason=?,confirmed_digest=COALESCE(?,confirmed_digest),confirmed_at=CASE WHEN ? IS NULL THEN confirmed_at ELSE ? END WHERE id=?',
                           (states[action], None if action == 'resume' else 'USER_' + action.upper(), checksum, checksum, self.clock.iso(), identity))
                self._schedule_event(db, identity, action.upper(), 'USER_CONFIRMED' if action == 'resume' else 'USER_' + action.upper())
            if action == 'cancel':
                db.execute("UPDATE schedule_target_owners SET active=0,assignment_version=assignment_version+1 WHERE schedule_id=? AND target_id IN (SELECT id FROM release_targets WHERE state IN ('READY','NOT_SENT','CANCELLED'))", (identity,))
            self._studio_remember(db, 'schedule.control:' + identity, key, request, {'id': identity})
            return self._schedule(db, identity)

    def schedule_occurrences(self, identity, before=None, limit=30):
        with self.transaction() as db:
            self._schedule_row(db, identity)
            rows = db.execute('SELECT rowid,* FROM schedule_occurrences WHERE schedule_id=? AND rowid<COALESCE(?,9223372036854775807) ORDER BY rowid DESC LIMIT ?', (identity, before, limit + 1)).fetchall()
            items = []
            for r in rows[:limit]:
                item = {k:r[k] for k in r.keys() if k != 'rowid'}
                item['targets'] = [dict(t) for t in db.execute('SELECT * FROM schedule_occurrence_targets WHERE occurrence_id=? ORDER BY rowid', (r['id'],))]
                items.append(item)
            return {'items': items, 'next_cursor': str(rows[limit-1]['rowid']) if len(rows) > limit else None}
