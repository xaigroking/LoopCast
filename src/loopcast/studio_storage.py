"""Transactional creation records and media lineage; Store owns all writes."""
import json
from .domain import Problem, canonical, digest, now, uid
from .studio_models import Draft
from .studio_rules import validate_draft


class StudioMethods:
    def _studio_prior(self, db, scope, key, value):
        if not isinstance(key, str) or not key.isascii() or not 1 <= len(key) <= 128:
            raise Problem('IDEMPOTENCY_REQUIRED', 'Supply an Idempotency-Key.', 400)
        old = db.execute('SELECT * FROM studio_requests WHERE scope=? AND key=?', (scope, key)).fetchone()
        if old and old['input_digest'] != digest(value):
            raise Problem('IDEMPOTENCY_CONFLICT', 'The request key has different input.', 409)
        return json.loads(old['result']) if old else None

    def _studio_remember(self, db, scope, key, value, result):
        db.execute('INSERT INTO studio_requests VALUES(?,?,?,?)', (scope, key, digest(value), canonical(result)))

    def _project(self, db, project_id):
        row = db.execute('SELECT * FROM studio_projects WHERE id=?', (project_id,)).fetchone()
        if row is None:
            raise Problem('NOT_FOUND', 'Project does not exist.', 404)
        revisions = []
        for r in db.execute('SELECT * FROM studio_revisions WHERE project_id=? ORDER BY number DESC', (project_id,)):
            generations = []
            for g in db.execute('SELECT g.*,r.state FROM studio_generations g JOIN runs r ON r.id=g.run_id WHERE revision_id=? ORDER BY g.rowid DESC', (r['id'],)):
                generations.append({key: g[key] for key in ('id', 'revision_id', 'run_id', 'stage', 'input_digest', 'state', 'created_at')} | {
                    'asset_ids': [a[0] for a in db.execute('SELECT asset_id FROM generation_assets WHERE generation_id=?', (g['id'],))]})
            revisions.append({'id': r['id'], 'number': r['number'], 'draft': json.loads(r['draft']),
                              'input_digest': r['input_digest'], 'created_at': r['created_at'], 'generations': generations})
        return {'id': row['id'], 'version': row['version'], 'draft': json.loads(row['draft']),
                'created_at': row['created_at'], 'updated_at': row['updated_at'], 'revisions': revisions}

    def studio_projects(self):
        with self.transaction() as db:
            return {'items': [self._project(db, r[0]) for r in db.execute('SELECT id FROM studio_projects ORDER BY rowid DESC LIMIT 100')]}

    def studio_project(self, project_id):
        with self.transaction() as db:
            return self._project(db, project_id)

    def studio_create(self, title, key):
        draft = validate_draft(Draft(title=title).model_dump())
        with self.transaction(True) as db:
            prior = self._studio_prior(db, 'project.create', key, {'title': title})
            if prior:
                return self._project(db, prior['id'])
            identity, timestamp = uid(), now()
            db.execute('INSERT INTO studio_projects VALUES(?,?,?,?,?)', (identity, canonical(draft), 1, timestamp, timestamp))
            self._studio_remember(db, 'project.create', key, {'title': title}, {'id': identity})
            return self._project(db, identity)

    def studio_save(self, project_id, value, expected_version, key):
        draft = validate_draft(Draft.model_validate(value).model_dump())
        request = {'draft': draft, 'expected_version': expected_version}
        scope = 'project.save:' + project_id
        with self.transaction(True) as db:
            prior = self._studio_prior(db, scope, key, request)
            if prior:
                return self._project(db, project_id)
            current = self._project(db, project_id)
            if type(expected_version) is not int or current['version'] != expected_version:
                raise Problem('VERSION_CONFLICT', 'Refresh the project before saving.', 409)
            db.execute('UPDATE studio_projects SET draft=?,version=version+1,updated_at=? WHERE id=?', (canonical(draft), now(), project_id))
            self._studio_remember(db, scope, key, request, {'id': project_id})
            return self._project(db, project_id)

    def studio_revision(self, project_id, expected_version, key):
        scope, request = 'project.freeze:' + project_id, {'expected_version': expected_version}
        with self.transaction(True) as db:
            if self._studio_prior(db, scope, key, request):
                return self._project(db, project_id)
            project = self._project(db, project_id)
            if project['version'] != expected_version:
                raise Problem('VERSION_CONFLICT', 'Refresh the project before freezing.', 409)
            number = db.execute('SELECT COALESCE(MAX(number),0)+1 FROM studio_revisions WHERE project_id=?', (project_id,)).fetchone()[0]
            identity = uid()
            db.execute('INSERT INTO studio_revisions VALUES(?,?,?,?,?,?)', (identity, project_id, number, canonical(project['draft']), digest(project['draft']), now()))
            db.execute('UPDATE studio_projects SET version=version+1,updated_at=? WHERE id=?', (now(), project_id))
            self._studio_remember(db, scope, key, request, {'id': identity})
            return self._project(db, project_id)

    def studio_generate(self, project_id, revision_id, stage, expected_version, key, trace=None):
        scope = 'project.generate:' + revision_id
        request = {'stage': stage, 'expected_version': expected_version}
        with self.transaction(True) as db:
            prior = self._studio_prior(db, scope, key, request)
            if prior:
                return self._project(db, project_id)
            project = self._project(db, project_id)
            if project['version'] != expected_version:
                raise Problem('VERSION_CONFLICT', 'Refresh the project before generating.', 409)
            revision = db.execute('SELECT * FROM studio_revisions WHERE id=? AND project_id=?', (revision_id, project_id)).fetchone()
            if revision is None:
                raise Problem('NOT_FOUND', 'Revision does not exist.', 404)
            if stage not in {'script', 'audio', 'materials', 'subtitle', 'video'}:
                raise Problem('INVALID_INPUT', 'Unknown generation stage.', 400)
            draft = json.loads(revision['draft'])
            if stage != 'script' and not draft['script'].strip() and not draft['scenes']:
                raise Problem('SCRIPT_REQUIRED', 'Save a script before generating this stage.', 400)
            settings = draft['settings']
            for identity in settings['material_ids'] + [v for v in (settings['audio_id'], settings['music_id']) if v]:
                self._asset(db, identity)
            run, _ = self.create_within(db, {'mode': 'mpt', 'title': draft['title']}, 'studio:' + uid(), trace)
            identity = uid()
            checksum = digest({'draft': draft, 'stage': stage, 'component': self.component})
            db.execute('INSERT INTO studio_generations VALUES(?,?,?,?,?,?)', (identity, revision_id, run['run_id'], stage, checksum, now()))
            db.execute('UPDATE studio_projects SET version=version+1,updated_at=? WHERE id=?', (now(), project_id))
            self._studio_remember(db, scope, key, request, {'id': identity})
            return self._project(db, project_id)

    def studio_generation_input(self, run_id):
        with self.transaction() as db:
            row = db.execute('SELECT g.*,r.draft FROM studio_generations g JOIN studio_revisions r ON r.id=g.revision_id WHERE g.run_id=?', (run_id,)).fetchone()
            return ({'generation_id': row['id'], 'draft': json.loads(row['draft']), 'stage': row['stage'], 'input_digest': row['input_digest']} if row else None)

    def _family_root(self, db, identity):
        seen = set()
        while identity:
            if identity in seen:
                raise RuntimeError('Media lineage cycle')
            seen.add(identity)
            row = db.execute('SELECT parent_id FROM media_families WHERE id=?', (identity,)).fetchone()
            if row is None:
                raise Problem('NOT_FOUND', 'Media lineage does not exist.', 404)
            if row[0] is None:
                return identity
            identity = row[0]

    def _family_members(self, db, identity):
        root = self._family_root(db, identity)
        return [r[0] for r in db.execute('SELECT id FROM media_families') if self._family_root(db, r[0]) == root]

    def _asset(self, db, identity):
        row = db.execute('SELECT * FROM studio_assets WHERE id=?', (identity,)).fetchone()
        if row is None:
            raise Problem('NOT_FOUND', 'Asset does not exist.', 404)
        return {key: row[key] for key in ('id', 'name', 'kind', 'media_type', 'size', 'sha256', 'family_id', 'parent_id', 'origin', 'created_at')} | {'metadata': json.loads(row['metadata'])}

    def studio_assets(self):
        with self.transaction() as db:
            return {'items': [self._asset(db, r[0]) for r in db.execute('SELECT id FROM studio_assets ORDER BY rowid DESC LIMIT 200')]}

    def studio_asset(self, identity):
        with self.transaction() as db:
            return self._asset(db, identity)

    def studio_asset_file(self, identity):
        with self.transaction() as db:
            row = db.execute('SELECT storage_ref,sha256,size FROM studio_assets WHERE id=?', (identity,)).fetchone()
            if row is None:
                raise Problem('NOT_FOUND', 'Asset does not exist.', 404)
            return dict(row)

    def studio_register_asset(self, value, key, parent_id=None, generation_id=None):
        request = {'sha256': value['sha256'], 'kind': value['kind'], 'parent_id': parent_id, 'generation_id': generation_id}
        with self.transaction(True) as db:
            prior = self._studio_prior(db, 'asset.import', key, request)
            if prior:
                return self._asset(db, prior['id']), False
            # Copies and reimports are aliases of existing media, not new works.
            same = db.execute('SELECT id,family_id FROM studio_assets WHERE sha256=?', (value['sha256'],)).fetchone()
            families = []
            if same:
                families.append(self._family_root(db, same['family_id']))
            if parent_id:
                parent = self._asset(db, parent_id)
                families.append(self._family_root(db, parent['family_id']))
            fingerprint = value.get('fingerprint')
            if fingerprint and value['kind'] == 'video':
                for old in db.execute("SELECT family_id,fingerprint,metadata FROM studio_assets WHERE kind='video' AND fingerprint IS NOT NULL"):
                    other = json.loads(old['fingerprint'])
                    if self._same_video(fingerprint, other):
                        families.append(self._family_root(db, old['family_id']))
            if families:
                family = min(families)
                for other in set(families) - {family}:
                    db.execute('UPDATE media_families SET parent_id=? WHERE id=?', (family, other))
            else:
                family = uid()
                db.execute('INSERT INTO media_families VALUES(?,NULL)', (family,))
            identity = value['id']
            db.execute('INSERT INTO studio_assets VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)', (
                identity, value['name'], value['kind'], value['media_type'], value['size'], value['sha256'], value['storage_ref'], family,
                parent_id, value['origin'], canonical(value['metadata']), canonical(fingerprint) if fingerprint else None, now()))
            if generation_id:
                db.execute('INSERT INTO generation_assets VALUES(?,?)', (generation_id, identity))
            self._studio_remember(db, 'asset.import', key, request, {'id': identity})
            return self._asset(db, identity), True

    @staticmethod
    def _same_video(first, second):
        if abs(first['duration'] - second['duration']) > max(.15, first['duration'] * .01):
            return False
        a, b = first['frames'], second['frames']
        return len(a) == len(b) and all((int(x, 16) ^ int(y, 16)).bit_count() <= 4 for x, y in zip(a, b))
