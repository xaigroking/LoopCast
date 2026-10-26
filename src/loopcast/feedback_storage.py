"""Transactional manual evidence, quarantined imports and immutable decision lineage."""
from datetime import datetime, timezone, timedelta
import hashlib
import json
from .domain import Problem, canonical, digest, now, uid
from .feedback_models import MetricsImport
from .feedback_rules import timestamp, choose, STRATEGY
from .studio_models import Draft


class FeedbackMethods:
    def _feedback_work(self, db, identity):
        row=db.execute('SELECT * FROM feedback_works WHERE id=?',(identity,)).fetchone()
        if row is None:raise Problem('NOT_FOUND','Work binding does not exist.',404)
        return dict(row)

    def feedback_bind(self, value, key):
        request=dict(value)
        with self.transaction(True) as db:
            old=self._studio_prior(db,'feedback.bind',key,request)
            if old:return self._feedback_work(db,old['id'])
            account=self._account(db,value['account_id']);asset=self._asset(db,value['asset_id'])
            if asset['kind']!='video':raise Problem('INVALID_MEDIA','Bind a registered video.',400)
            published=timestamp(value['published_at'])
            if datetime.fromisoformat(published)>datetime.now(timezone.utc)+timedelta(minutes=5):raise Problem('OBSERVATION_TIME_INVALID','Publication time cannot be in the future.',400)
            existing=db.execute('SELECT * FROM feedback_works WHERE platform=? AND stable_subject=? AND work_id=? AND evidence_mode=?',
                (account['platform'],account['stable_subject'],value['work_id'],account['evidence_mode'])).fetchone()
            if existing:
                if existing['asset_id']!=asset['id'] or existing['release_target_id']!=value.get('release_target_id') or existing['published_at']!=published:
                    raise Problem('WORK_MAPPING_CONFLICT','A stable work already has a different immutable binding.',409)
                result=dict(existing)
            else:
                kind='MANUAL_ASSOCIATION';target_id=value.get('release_target_id')
                if target_id:
                    target=db.execute('SELECT t.*,b.asset_id FROM release_targets t JOIN release_batches b ON b.id=t.batch_id WHERE t.id=?',(target_id,)).fetchone()
                    if target is None or target['account_id']!=account['id'] or target['asset_id']!=asset['id'] or target['evidence_mode']!=account['evidence_mode']:
                        raise Problem('WORK_MAPPING_CONFLICT','The target, account and exact media must match.',409)
                    receipts=db.execute("SELECT work_id FROM release_receipts WHERE target_id=? AND outcome='PUBLISHED'",(target_id,)).fetchall()
                    if receipts:
                        if not any(r['work_id']==value['work_id'] for r in receipts):raise Problem('WORK_IDENTITY_MISMATCH','Work ID differs from the confirmed receipt.',409)
                        kind='PLATFORM_RECEIPT'
                    elif target['state'] not in {'SUBMITTED','UNKNOWN'}:
                        raise Problem('TARGET_NOT_SUBMITTED','This target has no send evidence to associate.',409)
                identity=uid()
                db.execute('INSERT INTO feedback_works VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                    (identity,account['id'],asset['id'],account['platform'],account['stable_subject'],value['work_id'],target_id,published,value['source'],account['evidence_mode'],kind,now()))
                result=self._feedback_work(db,identity)
            self._studio_remember(db,'feedback.bind',key,request,{'id':result['id']});return result

    def _feedback_import(self, db, identity):
        row=db.execute('SELECT id,filename,sha256,source,evidence_mode,version,report,created_at FROM feedback_imports WHERE id=?',(identity,)).fetchone()
        if row is None:raise Problem('NOT_FOUND','Import does not exist.',404)
        result=dict(row);result['rows']=json.loads(result.pop('report'))
        result['resolution_history']=[dict(r) for r in db.execute('SELECT version,created_at FROM feedback_resolutions WHERE import_id=? ORDER BY version',(identity,))]
        return result

    def _feedback_snapshot(self, db, identity):
        row=db.execute('SELECT s.*,w.platform,w.stable_subject,w.work_id,w.asset_id,w.evidence_mode FROM feedback_snapshots s JOIN feedback_works w ON w.id=s.work_binding_id WHERE s.id=?',(identity,)).fetchone()
        if row is None:raise Problem('NOT_FOUND','Snapshot does not exist.',404)
        result=dict(row);result.pop('observation_key');result['metrics']=json.loads(result['metrics']);return result

    def feedback_workspace(self):
        with self.transaction() as db:
            return {'works':[dict(r) for r in db.execute('SELECT * FROM feedback_works ORDER BY rowid DESC LIMIT 200')],
                    'imports':[self._feedback_import(db,r[0]) for r in db.execute('SELECT id FROM feedback_imports ORDER BY rowid DESC LIMIT 100')],
                    'snapshots':[self._feedback_snapshot(db,r[0]) for r in db.execute('SELECT id FROM feedback_snapshots ORDER BY observed_at DESC LIMIT 500')]}

    def feedback_raw(self, identity):
        with self.transaction() as db:
            row=db.execute('SELECT raw,filename,sha256 FROM feedback_imports WHERE id=?',(identity,)).fetchone()
            if row is None:raise Problem('NOT_FOUND','Import does not exist.',404)
            data=bytes(row['raw'])
            if hashlib.sha256(data).hexdigest()!=row['sha256']:raise Problem('ARTIFACT_UNAVAILABLE','Original import integrity failed.',409)
            return {'data':data,'filename':row['filename'],'sha256':row['sha256']}

    def _feedback_ingest_row(self, db, import_id, row, mode):
        work=db.execute('SELECT * FROM feedback_works WHERE platform=? AND stable_subject=? AND work_id=? AND evidence_mode=?',
            (row['platform'],row['stable_subject'],row['work_id'],mode)).fetchone()
        if work is None:raise Problem('WORK_UNBOUND','Explicitly bind the stable work before importing this row.',400)
        if work['asset_id']!=row['asset_id']:raise Problem('MEDIA_MAPPING_MISMATCH','Use the exact bound media identity.',400)
        observed=timestamp(row['observed_at']);start=timestamp(row['window_start']) if row['window_start'] else None;end=timestamp(row['window_end']) if row['window_end'] else None
        if datetime.fromisoformat(observed)>datetime.now(timezone.utc)+timedelta(minutes=5) or observed<work['published_at']:
            raise Problem('OBSERVATION_TIME_INVALID','Observation must follow publication and cannot be in the future.',400)
        if row['aggregation']=='delta':
            if not start or not end or not work['published_at']<=start<end<=observed:raise Problem('WINDOW_INVALID','Delta observations need a valid non-overlapping interval.',400)
        elif start or end:raise Problem('WINDOW_INVALID','Cumulative observations have no delta interval.',400)
        metrics=row['metrics']
        if all(value is None for value in metrics.values()):raise Problem('METRICS_MISSING','Provide at least one observed metric.',400)
        if any(value is not None and type(value) is not int for name,value in metrics.items() if name!='watch_seconds'):
            raise Problem('COUNTER_TYPE_INVALID','Count metrics must be integers.',400)
        observation={'work':work['id'],'observed_at':observed if row['aggregation']=='cumulative' else None,'aggregation':row['aggregation'],'definition':row['definition'],'start':start,'end':end}
        identity_key=digest(observation);checksum=digest(observation|{'metrics':metrics})
        existing=db.execute('SELECT id,sha256 FROM feedback_snapshots WHERE observation_key=?',(identity_key,)).fetchone()
        if existing:
            if existing['sha256']!=checksum:raise Problem('OBSERVATION_CONFLICT','An immutable observation already has different values.',409)
            return {'state':'DUPLICATE','code':'EXISTING_SNAPSHOT','snapshot_id':existing['id']}
        for prior in db.execute('SELECT * FROM feedback_snapshots WHERE work_binding_id=? AND definition=? AND aggregation=?',(work['id'],row['definition'],row['aggregation'])):
            if row['aggregation']=='delta':
                if start<prior['window_end'] and end>prior['window_start']:raise Problem('DELTA_OVERLAP','Overlapping deltas are quarantined.',409)
            else:
                old=json.loads(prior['metrics'])
                for name,value in metrics.items():
                    if value is None or old[name] is None:continue
                    if prior['observed_at']<observed and old[name]>value or prior['observed_at']>observed and old[name]<value:
                        raise Problem('COUNTER_DECREASE','Cumulative observations must be consistent over time.',409)
        identity=uid()
        db.execute('INSERT INTO feedback_snapshots VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',(identity,import_id,work['id'],identity_key,observed,row['observed_at'],row['aggregation'],row['definition'],start,end,canonical(metrics),checksum,now()))
        return {'state':'ACCEPTED','code':'OK','snapshot_id':identity}

    def _feedback_rows(self, db, import_id, value, previous=None):
        results=[]
        for index,row in enumerate(value['rows']):
            if previous and previous[index]['state']!='QUARANTINED':results.append(previous[index]);continue
            try:result=self._feedback_ingest_row(db,import_id,row,value['evidence_mode'])
            except Problem as problem:result={'state':'QUARANTINED','code':problem.code,'snapshot_id':None}
            results.append({'row':index+1,**result})
        return results

    def feedback_import(self, raw, filename, key):
        value=MetricsImport.model_validate_json(raw).model_dump()
        checksum=hashlib.sha256(raw).hexdigest();request={'sha256':checksum}
        with self.transaction(True) as db:
            prior=self._studio_prior(db,'feedback.import',key,request)
            existing=db.execute('SELECT id FROM feedback_imports WHERE sha256=?',(checksum,)).fetchone()
            if prior:return self._feedback_import(db,prior['id'])
            if existing:
                identity=existing[0]
            else:
                identity=uid()
                db.execute('INSERT INTO feedback_imports VALUES(?,?,?,?,?,?,?,?,?)',(identity,filename,checksum,value['source'],value['evidence_mode'],raw,1,'[]',now()))
                results=self._feedback_rows(db,identity,value)
                db.execute('UPDATE feedback_imports SET report=? WHERE id=?',(canonical(results),identity))
                db.execute('INSERT INTO feedback_resolutions VALUES(?,?,?,?,?)',(uid(),identity,1,canonical(results),now()))
            self._studio_remember(db,'feedback.import',key,request,{'id':identity});return self._feedback_import(db,identity)

    def feedback_reprocess(self, identity, version, key):
        with self.transaction(True) as db:
            current=self._feedback_import(db,identity)
            if self._studio_prior(db,'feedback.reprocess:'+identity,key,{'version':version}):return current
            if current['version']!=version:raise Problem('VERSION_CONFLICT','Refresh the import before reprocessing.',409)
            row=db.execute('SELECT raw,sha256 FROM feedback_imports WHERE id=?',(identity,)).fetchone();raw=bytes(row['raw'])
            if hashlib.sha256(raw).hexdigest()!=row['sha256']:raise Problem('ARTIFACT_UNAVAILABLE','Original import integrity failed.',409)
            value=MetricsImport.model_validate_json(raw).model_dump();results=self._feedback_rows(db,identity,value,current['rows'])
            db.execute('UPDATE feedback_imports SET version=version+1,report=? WHERE id=?',(canonical(results),identity))
            db.execute('INSERT INTO feedback_resolutions VALUES(?,?,?,?,?)',(uid(),identity,version+1,canonical(results),now()))
            self._studio_remember(db,'feedback.reprocess:'+identity,key,{'version':version},{'id':identity});return self._feedback_import(db,identity)

    def _feedback_decision(self, db, identity):
        row=db.execute('SELECT * FROM feedback_decisions WHERE id=?',(identity,)).fetchone()
        if row is None:raise Problem('NOT_FOUND','Decision does not exist.',404)
        result=dict(row)
        for name in ('input','snapshot_digests','excluded'):result[name]=json.loads(result[name])
        result['revisions']=[dict(r) for r in db.execute('SELECT l.revision_id,r.project_id,p.version AS project_version,l.created_at FROM feedback_revision_links l JOIN studio_revisions r ON r.id=l.revision_id JOIN studio_projects p ON p.id=r.project_id WHERE l.decision_id=? ORDER BY r.rowid',(identity,))]
        return result

    def feedback_decisions(self):
        with self.transaction() as db:return [self._feedback_decision(db,r[0]) for r in db.execute('SELECT id FROM feedback_decisions ORDER BY rowid DESC LIMIT 100')]

    def feedback_propose(self, value, key):
        if not value['topic'].strip():raise Problem('INVALID_INPUT','Provide a topic.',400)
        with self.transaction(True) as db:
            prior=self._studio_prior(db,'feedback.propose',key,value)
            if prior:return self._feedback_decision(db,prior['id'])
            snapshots=[self._feedback_snapshot(db,identity) for identity in value['snapshot_ids']]
            works={s['work_binding_id']:self._feedback_work(db,s['work_binding_id']) for s in snapshots}
            result=choose(value,snapshots,works);identity=uid()
            db.execute('INSERT INTO feedback_decisions VALUES(?,?,?,?,?,?,?,?,?,?,?)',(identity,1,'PROPOSED',STRATEGY,canonical(value),canonical({s['id']:s['sha256'] for s in snapshots}),result['selected_snapshot_id'],result['reason'],canonical(result['excluded']),result['evidence_mode'],now()))
            self._studio_remember(db,'feedback.propose',key,value,{'id':identity});return self._feedback_decision(db,identity)

    def feedback_confirm(self, identity, value, key):
        with self.transaction(True) as db:
            current=self._feedback_decision(db,identity)
            if self._studio_prior(db,'feedback.confirm:'+identity,key,value):return current
            if current['version']!=value['expected_version']:raise Problem('VERSION_CONFLICT','Refresh the decision.',409)
            if not value['topic'].strip():raise Problem('INVALID_INPUT','Provide a topic.',400)
            project_id=value.get('project_id')
            if project_id:
                project=self._project(db,project_id)
                if project['version']!=value.get('expected_project_version'):raise Problem('VERSION_CONFLICT','Refresh the project.',409)
                draft=Draft.model_validate(project['draft']|{'title':value['topic'],'brief':value['brief']}).model_dump()
                db.execute('UPDATE studio_projects SET draft=?,version=version+1,updated_at=? WHERE id=?',(canonical(draft),now(),project_id))
            else:
                if value.get('expected_project_version') is not None:raise Problem('INVALID_INPUT','A project version requires a project.',400)
                project_id=uid();draft=Draft(title=value['topic'],brief=value['brief']).model_dump()
                db.execute('INSERT INTO studio_projects VALUES(?,?,?,?,?)',(project_id,canonical(draft),1,now(),now()))
            revision_id=uid();number=db.execute('SELECT COALESCE(MAX(number),0)+1 FROM studio_revisions WHERE project_id=?',(project_id,)).fetchone()[0]
            db.execute('INSERT INTO studio_revisions VALUES(?,?,?,?,?,?)',(revision_id,project_id,number,canonical(draft),digest(draft),now()))
            db.execute('INSERT INTO feedback_revision_links VALUES(?,?,?)',(revision_id,identity,now()))
            db.execute("UPDATE feedback_decisions SET state='CONFIRMED',version=version+1 WHERE id=?",(identity,))
            self._studio_remember(db,'feedback.confirm:'+identity,key,value,{'id':identity});return self._feedback_decision(db,identity)
