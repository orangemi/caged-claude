"""Terminal output: aligned steps, unwrapped probe errors, JSON only on request."""
import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from caged.host import ui


def capture(function, *args, **kwargs):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err), patch.dict('os.environ', {'NO_COLOR': '1'}):
        function(*args, **kwargs)
    return out.getvalue(), err.getvalue()


class Steps(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(ui, 'JSON', False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_steps_align_and_problems_go_to_stderr(self):
        out, err = capture(ui.ok, 'Gateway', 'exit 203.0.113.9 (JP)')
        self.assertEqual((out, err), ('  ✓ Gateway      exit 203.0.113.9 (JP)\n', ''))
        out, err = capture(ui.fail, 'DNS', 'no reply', [('Fix', 'add another DoH endpoint')])
        self.assertEqual(out, '')
        self.assertEqual(err.splitlines(), ['  ✗ DNS          no reply', '                 Fix: add another DoH endpoint'])
        out, _ = capture(ui.step, 'warn', 'Outbound', 'closed', out=True)
        self.assertEqual(out, '  ! Outbound     closed\n')

    def test_long_text_wraps_under_its_column(self):
        with patch('shutil.get_terminal_size', return_value=__import__('os').terminal_size((60, 20))):
            out, _ = capture(ui.ok, 'Browser', ' '.join(['word'] * 30))
        lines = out.splitlines()
        self.assertGreater(len(lines), 1)
        self.assertTrue(all(len(line) <= 60 for line in lines))
        self.assertTrue(all(line.startswith(' ' * 17) for line in lines[1:]))

    def test_errors_show_the_probe_message_not_its_json(self):
        _, err = capture(ui.error, json.dumps({'error': 'Could not verify DNS through the selected proxy.'}))
        self.assertEqual(err, 'Error: Could not verify DNS through the selected proxy.\n')
        _, err = capture(ui.error, 'The network image build failed. Last lines of build.log:\n#5 ERROR\n#6 done')
        self.assertEqual(err.splitlines()[1:], ['#5 ERROR', '#6 done'])
        self.assertEqual(ui.probe_error('plain text'), 'plain text')

    def test_json_mode_keeps_stdout_for_the_result(self):
        with patch.object(ui, 'JSON', True):
            out, err = capture(lambda: (ui.ok('Node', 'pinned'), ui.result({'passed': 11})))
        self.assertEqual(json.loads(out), {'passed': 11})
        self.assertIn('✓ Node', err)
        out, _ = capture(ui.result, {'passed': 11})
        self.assertEqual(out, '')
