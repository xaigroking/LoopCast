"""Fixed local-only diagnostic bridge, bounded by the existing process runner."""
import json
from pathlib import Path
import sys
from . import generator_diagnostics as generator_probe
from . import generation
from . import operations_snapshot as operations_probe
from .inspection_models import InspectionOutcome
from .runner import RunnerError


def inspect(value):
    counts={k:0 for k in ('generate','paid_model','authorize','upload','query','check','version_process','cli_help_process','wheel_verify_process')}
    failure=[]
    def allow(original):
        def execute(argv,*args,**kwargs):
            if argv[-1:] == ['-version']:counts['version_process']+=1
            elif argv[-1:] == ['--help']:counts['cli_help_process']+=1
            elif '-c' in argv and generation.VERIFY in argv:counts['wheel_verify_process']+=1
            else:raise RunnerError('READ_ONLY_OPERATION_REQUIRED')
            # Keep nested diagnostic processes inside the bounded outer group.
            kwargs['new_session']=False
            try:return original(argv,*args,**kwargs)
            except RunnerError as error:
                failure.append(error.code);raise
        return execute
    old=(generator_probe.run,generation.run,operations_probe.run)
    generator_probe.run=allow(old[0]);generation.run=allow(old[1]);operations_probe.run=allow(old[2])
    try:
        kind=value['kind'];checks=[];result='UNVERIFIED';reason='NOT_CHECKED';scope='LOCAL_RUNTIME';evidence='unverified'
        if kind=='GENERATOR':
            report=generator_probe.inspect_generator(value['generator'],value['media'])
            mapping={'NOT_CONFIGURED':('NOT_CONFIGURED','GENERATOR_NOT_CONFIGURED'),'INVALID':('MISCONFIGURED','GENERATOR_INVALID'),'NOT_INSTALLED':('MISCONFIGURED','COMPONENT_UNAVAILABLE'),'CALLABLE':('CALLABLE','OK')}
            result,reason=mapping[report['state']];scope='COMPONENT_INSTALL'
            for item in report['checks']:
                checks.append({'check_type':item['id'].upper(),'source':'generator.diagnostics','result':'CALLABLE' if item['state']=='PASS' else 'UNVERIFIED' if item['state']=='WARN' else 'MISCONFIGURED','reason_code':item['code']})
            # CLI/install verification never upgrades to a live generation claim.
            evidence='source_inspected' if result=='CALLABLE' else 'unverified'
        elif kind=='MEDIA':
            if value['media'] is None:result='NOT_CONFIGURED';reason='MEDIA_NOT_CONFIGURED'
            else:
                for name in ('ffmpeg','ffprobe'):
                    item=operations_probe.media_version(value['media'],name)
                    good=item['status']=='verified'
                    checks.append({'check_type':name.upper(),'source':'media.version','result':'CALLABLE' if good else 'MISCONFIGURED','reason_code':'OK' if good else 'MEDIA_UNAVAILABLE','value':item['value'] if good else None})
                result='CALLABLE' if all(c['result']=='CALLABLE' for c in checks) else 'MISCONFIGURED';reason='OK' if result=='CALLABLE' else 'MEDIA_UNAVAILABLE';evidence='source_inspected' if result=='CALLABLE' else 'unverified'
        elif kind=='CONTROL_STORAGE':
            report=operations_probe.snapshot(value['directory'],service_ready=value['ready'])
            for category,key,code in [('database','schema','SCHEMA'),('runtime','service_ready','SERVICE_READY'),('recovery','guard','RESTORE_GUARD')]:
                item=report[category][key]
                checks.append({'check_type':code,'source':'operations.snapshot','result':'CALLABLE' if item['status']=='verified' else 'UNVERIFIED','reason_code':'OK' if item['status']=='verified' else 'NOT_OBSERVED','value':item['value']})
            result='CALLABLE' if value['ready'] else 'UNKNOWN';reason='OK' if value['ready'] else 'SERVICE_NOT_READY';evidence='source_inspected'
        else:raise ValueError('Unsupported local check')
        if failure:result='UNKNOWN';reason='CHECK_TIMEOUT' if 'TIMEOUT' in failure else 'CHECK_UNAVAILABLE';evidence='unverified'
        return InspectionOutcome.model_validate({'result':result,'reason_code':reason,'evidence_status':evidence,'evidence_scope':scope,'checks':checks,'actual_operations':counts}).model_dump()
    finally:generator_probe.run,generation.run,operations_probe.run=old


def main():
    path=Path(sys.argv[1])
    try:
        if path.is_symlink() or path.stat().st_size>524288:raise ValueError()
        value=json.loads(path.read_text())
        if set(value)!={'kind','directory','generator','media','ready'} or type(value['ready'])is not bool:raise ValueError()
        report=inspect(value)
    except Exception:
        report={'result':'UNKNOWN','reason_code':'CHECK_UNAVAILABLE','evidence_status':'unverified','evidence_scope':'LOCAL_RUNTIME','checks':[],'declarations':[],'actual_operations':{}}
    sys.stdout.write(json.dumps(report))


if __name__=='__main__':main()
