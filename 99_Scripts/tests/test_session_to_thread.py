import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('s2t', Path(__file__).parents[1] / 'session_to_thread.py')
s2t = importlib.util.module_from_spec(spec)
spec.loader.exec_module(s2t)


def ev(role, content, **kw):
    return json.dumps({'type': role, 'message': {'role': role, 'content': content}, **kw}, ensure_ascii=False)


class SessionToThreadTest(unittest.TestCase):
    def test_injected_messages_are_dropped(self):
        lines = [
            ev('user', 'a real question'),
            ev('assistant', [{'type': 'text', 'text': 'answer'}]),
            ev('user', 'meta injection', isMeta=True),
            ev('user', '<task-notification>done</task-notification>'),
            ev('user', 'Stop hook feedback: cards'),
            ev('user', 'A session-scoped Stop hook is now active'),
            ev('user', '# Autonomous loop\ndo it'),
            ev('user', '/compact'),
            ev('user', '<task-notification>a</task-notification> words after it'),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp, 'sid.jsonl')
            p.write_text('\n'.join(lines), encoding='utf-8')
            turns = s2t.parse_transcript(p)['turns']
        self.assertEqual(turns, [('user', 'a real question'), ('assistant', 'answer'), ('user', 'words after it')])

    def test_is_injected(self):
        self.assertTrue(s2t.is_injected('<bash-input>x'))
        self.assertFalse(s2t.is_injected('<div>', tag=False))
        self.assertFalse(s2t.is_injected('/Users/x look at this path'))
        self.assertFalse(s2t.is_injected('/ at the start'))

    def test_codex_terminal_sessions_from_cli_and_vscode(self):
        self.assertTrue(s2t.is_codex_interactive({'source': 'cli'}))
        self.assertTrue(s2t.is_codex_interactive({'source': 'vscode'}))
        self.assertFalse(s2t.is_codex_interactive({'source': 'exec'}))
        self.assertFalse(s2t.is_codex_interactive({'source': {'subagent': 'x'}}))


if __name__ == '__main__':
    unittest.main()
