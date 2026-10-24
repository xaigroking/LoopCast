"""Bounded fixture subprocess: fixed operations, no user commands."""
import json
import sys
import time


def main():
    data = json.loads(sys.stdin.buffer.read(4096))
    stage = data['stage']
    value = data['input']
    if stage == 'execute':
        time.sleep(value['delay_ms'] / 1000)
        if value['scenario'] == 'slow':
            time.sleep(1)
        if value['scenario'] == 'fail' or (value['scenario'] == 'fail_once' and data['number'] == 1):
            sys.stderr.write('Controlled fixture failure\n')
            return 2
    if stage == 'report':
        print('LoopCast fixture report\n')
        print('This is a control-plane fixture, not a generated video or publication.')
        print('Title: ' + value['title'])
        print('Attempt: ' + str(data['number']))
        print('Input: ' + json.dumps(value, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
