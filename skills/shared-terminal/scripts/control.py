#!/usr/bin/env python3
"""Control an owned shared-terminal session without putting input in argv."""
import argparse
import json
import os
from pathlib import Path
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

MAX_INPUT = 65536
KEYS = {'enter': '\r', 'up': '\x1b[A', 'down': '\x1b[B', 'left': '\x1b[D',
        'right': '\x1b[C', 'tab': '\t', 'escape': '\x1b', 'ctrl-c': '\x03'}


def read_state(path):
    with os.fdopen(os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))) as file:
        metadata = os.fstat(file.fileno())
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                or metadata.st_mode & 0o077):
            raise ValueError('State must be an owned private regular file (0600).')
        data = json.loads(file.read(16385))
    target = urllib.parse.urlsplit(data.get('url', ''))
    if (data.get('schema_version') != 1 or target.scheme != 'http'
            or target.hostname != '127.0.0.1' or not target.port
            or target.username or target.password or target.path or target.query or target.fragment
            or not isinstance(data.get('token'), str) or not data['token'].isascii()
            or len(data['token']) != 43):
        raise ValueError('Invalid shared-terminal state.')
    return data


def request(state, path, data=None):
    headers = {'Authorization': 'Bearer ' + state['token'], 'Origin': state['url']}
    body = None
    if data is not None:
        headers['Content-Type'] = 'application/json'
        body = json.dumps(data).encode()
    # Local session traffic must not follow proxy settings or redirects.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *_):
            return None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(urllib.request.Request(state['url'] + path, data=body, headers=headers), timeout=8) as response:
            return json.loads(response.read(1024 * 1024))
    except urllib.error.HTTPError as error:
        error.close()
        if error.code == 409:
            raise ValueError('Expected text is not visible; no input was sent.') from None
        raise ValueError('Local terminal request was rejected (HTTP ' + str(error.code) + ').') from None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-file', type=Path, required=True)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('status', help='Show session health without terminal contents.')
    match = commands.add_parser('match', help='Check for public prompt text without printing the screen.')
    match.add_argument('--text', required=True)
    screen = commands.add_parser('screen', help='Explicitly inspect terminal text outside sensitive input steps.')
    screen.add_argument('--raw', action='store_true', required=True)
    send = commands.add_parser('send', help='Send stdin bytes, or one named key, to the same PTY.')
    send.add_argument('--expect', help='Reject input unless this text is still on the current screen.')
    send.add_argument('--key', choices=KEYS)
    commands.add_parser('stop', help='Close this owned PTY and its server; leave installed services alone.')
    args = parser.parse_args(argv)
    state = read_state(args.state_file)
    if args.command == 'status':
        print(json.dumps(request(state, '/health')))
    elif args.command == 'match':
        screen = request(state, '/screen')
        print(json.dumps({'matches': args.text in screen['text'], 'alive': screen['alive']}))
    elif args.command == 'screen':
        print(request(state, '/screen')['text'])
    elif args.command == 'send':
        if args.key:
            data = KEYS[args.key]
        else:
            raw = sys.stdin.buffer.read(MAX_INPUT + 1)
            if not 0 < len(raw) <= MAX_INPUT:
                raise ValueError('Supply up to 65536 input bytes through stdin, or use --key.')
            data = raw.decode('utf-8')
        request(state, '/input', {'data': data, 'expected': args.expect})
        print('Input sent.')
    elif args.command == 'stop':
        request(state, '/stop', {})
        deadline = time.monotonic() + 5
        while args.state_file.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        if args.state_file.exists():
            raise ValueError('Shutdown was requested, but state cleanup is not confirmed.')
        print('Shared terminal stopped; private access files removed.')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, urllib.error.URLError) as error:
        # Network exceptions can include URLs; avoid echoing untrusted state.
        message = str(error) if isinstance(error, ValueError) and not isinstance(error, json.JSONDecodeError) else type(error).__name__
        print('Shared terminal: ' + message, file=sys.stderr)
        sys.exit(1)
