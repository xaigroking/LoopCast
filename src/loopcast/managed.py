"""Bounded private stdin protocol. Invalid input closes admission, never logs bytes."""
import asyncio
import json
import os
import re

class ManagedInput:
    def __init__(self):
        self.reader = asyncio.StreamReader(limit=512)
        self.transport = None
        self.invalid = False

    async def open(self):
        loop = asyncio.get_running_loop()
        pipe = os.fdopen(os.dup(0), 'rb', buffering=0)
        self.transport, _ = await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(self.reader), pipe)
        return self

    async def line(self, limit):
        try:
            raw = await self.reader.readuntil(b'\n')
        except asyncio.IncompleteReadError as error:
            if error.partial: raise ValueError('INVALID_MANAGED_INPUT') from None
            return None
        except asyncio.LimitOverrunError: raise ValueError('INVALID_MANAGED_INPUT') from None
        if len(raw) > limit or any(value < 32 or value > 126 for value in raw[:-1]): raise ValueError('INVALID_MANAGED_INPUT')
        return raw[:-1]

    async def session(self):
        raw = await asyncio.wait_for(self.line(256), timeout=10)
        if raw is None: raise ValueError('INVALID_MANAGED_INPUT')
        def unique(items):
            result = {}
            for key, value in items:
                if key in result: raise ValueError('INVALID_MANAGED_INPUT')
                result[key] = value
            return result
        try:
            value = json.loads(raw, object_pairs_hook=unique)
            if not isinstance(value, dict) or set(value) != {'format', 'token'} or value['format'] != 'loopcast.desktop-session.v1' or not isinstance(value['token'], str) or not re.fullmatch('[a-f0-9]{64}', value['token']): raise ValueError()
            return value['token']
        except (ValueError, KeyError, TypeError): raise ValueError('INVALID_MANAGED_INPUT') from None

    async def stop(self):
        try:
            value = await self.line(64)
            if value not in (None, b'STOP'): self.invalid = True
        except ValueError: self.invalid = True

    def close(self):
        if self.transport: self.transport.close()
