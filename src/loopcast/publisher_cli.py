"""Controlled local publication simulator. No network or real platform integration."""
import json
import os
from pathlib import Path
import sys
import time


def main():
    directory, attempt, scenario = sys.argv[1:]
    if scenario == 'slow': time.sleep(30)
    if scenario in ('success', 'lost_response'):
        path = Path(directory) / (attempt + '.json')
        with path.open('x') as stream:
            json.dump({'attempt_id': attempt, 'work_id': 'sim-' + attempt}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        if scenario == 'lost_response': return 2
        print(json.dumps({'work_id': 'sim-' + attempt}))
    else: print(json.dumps({'submitted': True}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
