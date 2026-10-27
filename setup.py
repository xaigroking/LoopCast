"""Embed source identity in both distribution formats, never inspect Git at runtime."""
import json
from pathlib import Path
import re
import subprocess
from setuptools import setup
from setuptools.command.build_py import build_py
from setuptools.command.sdist import sdist

ROOT = Path(__file__).parent
def source():
    if (ROOT / '.git').exists():
        value = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
        if subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT): value = 'unavailable'
    else:
        try: value = json.loads((ROOT/'src/loopcast/resources/source.json').read_text())['revision']
        except (OSError, ValueError, KeyError): value = 'unavailable'
    return {'revision': value if re.fullmatch('[a-f0-9]{40}', value) else 'unavailable'}

class Build(build_py):
    def run(self):
        super().run()
        target = Path(self.build_lib)/'loopcast/resources/source.json'
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(source()) + '\n')

class Source(sdist):
    def make_release_tree(self, base_dir, files):
        super().make_release_tree(base_dir, files)
        target = Path(base_dir)/'src/loopcast/resources/source.json'
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(source()) + '\n')

setup(cmdclass={'build_py': Build, 'sdist': Source})
