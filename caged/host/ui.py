"""Terminal output for people: one line per step, details only where they help.

Results for scripts stay JSON: --json prints them instead of these lines, and
reports are also saved under state/. Colors only on a terminal without NO_COLOR.
Probes inside the containers keep answering in JSON; only this layer reads them.
"""
import json
import os
import shutil
import sys
import textwrap

JSON = False  # set from --json: stdout then carries only the JSON result
LABEL = 12
MARKS = {'ok': ('✓', '32'), 'fail': ('✗', '31'), 'warn': ('!', '33'), 'info': ('·', '2')}


def paint(code, text, stream):
    if os.environ.get('NO_COLOR') or not hasattr(stream, 'isatty') or not stream.isatty():
        return text
    return f'\033[{code}m{text}\033[0m'


def width():
    return max(60, min(120, shutil.get_terminal_size((100, 24)).columns))


def wrap(text, indent):
    lines = textwrap.wrap(str(text), width() - indent, break_on_hyphens=False, break_long_words=False) or ['']
    return lines[0], [' ' * indent + line for line in lines[1:]]


def step(kind, label, text='', details=(), out=False, width=LABEL):
    """One step: mark, fixed-width label, text; details are (name, value) lines under it.

    Problems go to stderr unless out is set (a report such as ./cage status)."""
    stream = sys.stderr if JSON or (kind in ('fail', 'warn') and not out) else sys.stdout
    mark, code = MARKS[kind]
    indent = 5 + width
    first, rest = wrap(text, indent)
    print(f'  {paint(code, mark, stream)} {label:<{width}} {first}'.rstrip(), file=stream, flush=True)
    for line in rest:
        print(line, file=stream, flush=True)
    for name, value in details:
        first, rest = wrap(f'{name}: {value}', indent + 2)
        print(' ' * indent + first, file=stream, flush=True)
        for line in rest:
            print('  ' + line, file=stream, flush=True)


def ok(label, text='', details=()):
    step('ok', label, text, details)


def warn(label, text='', details=()):
    step('warn', label, text, details)


def fail(label, text='', details=()):
    step('fail', label, text, details)


def info(label, text='', details=()):
    step('info', label, text, details)


def line(text='', stream=None):
    """Plain text for people; with --json it moves to stderr."""
    print(text, file=stream or (sys.stderr if JSON else sys.stdout), flush=True)


def heading(text):
    line(paint('1', text, sys.stderr if JSON else sys.stdout))


def result(value):
    """The machine-readable result, printed only with --json."""
    if JSON:
        print(json.dumps(value, ensure_ascii=False, indent=2), flush=True)


def short(text, root):
    """Paths inside the project, shown relative to it."""
    return str(text).replace(str(root).rstrip('/') + '/', '')


def probe_error(text):
    """The message of a probe's {"error": ...} answer, or the text unchanged."""
    lines = (text or '').strip().splitlines()
    try:
        value = json.loads(lines[-1]) if lines else None
    except ValueError:
        return text
    if isinstance(value, dict) and isinstance(value.get('error'), str):
        return value['error']
    return text


def error(message):
    """The first line is wrapped prose; further lines (such as a log tail) stay as they are."""
    stream = sys.stderr
    head, _, tail = probe_error(str(message)).partition('\n')
    first, rest = wrap(head, 7)
    print(paint('31', 'Error:', stream) + ' ' + first, file=stream, flush=True)
    for text in rest:
        print(text, file=stream, flush=True)
    if tail:
        print(tail, file=stream, flush=True)
