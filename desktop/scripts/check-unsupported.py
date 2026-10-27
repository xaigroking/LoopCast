"""Exercise an actually unsupported OS without claiming a native platform port."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from loopcast.desktop_diagnostics import IDS

if sys.platform != 'win32': raise SystemExit('This check requires an actual Windows runner.')
with tempfile.TemporaryDirectory() as name:
    root=Path(name);env={**os.environ,'DIAGNOSTIC_SECRET':'unsupported-environment-canary'}
    result=subprocess.run([sys.executable,'-I','-m','loopcast','desktop-diagnostics','--manifest',str(root/'absent-manifest'),'--data-dir',str(root/'absent-data')],capture_output=True,text=True,timeout=10,env=env)
    assert result.returncode==0 and result.stderr==''
    report=json.loads(result.stdout)
    assert report['platform']['os']=='win32'
    assert [item['id'] for item in report['checks']]==list(IDS)
    assert all(item['code']=='PLATFORM_UNSUPPORTED' and item['status']=='unavailable' for item in report['checks'])
    assert list(root.iterdir())==[]
    assert str(root) not in result.stdout and 'unsupported-environment-canary' not in result.stdout
    print('Actual unsupported OS diagnostic PASS',report['platform']['os'],report['platform']['arch'],report['platform']['revision'])
