import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('recall', Path(__file__).parents[1] / 'recall_ppr.py')
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)


class RecallTest(unittest.TestCase):
    def test_both_entity_array_forms_and_quoted_paths(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(r, 'SUMMARIES', tmp), patch.object(r, 'vault_open', return_value=False):
            for entities in ('[one, "two"]', '\n  - one\n  - "two"'):
                Path(tmp, 'card.md').write_text('---\nthread_ref: "00_Inbox/a b.md"\nmentioned_entities: ' + entities + '\n---\n')
                info = r.parse_summaries()['card']
                self.assertEqual(info['entities'], ['one', 'two'])
                self.assertEqual(info['thread'], '00_Inbox/a b.md')

    def test_hook_failure_is_not_an_empty_success(self):
        with patch.object(r.sys, 'stdin', io.StringIO('{"prompt":"a long enough question"}')), \
             patch.object(r, 'Graph', side_effect=RuntimeError('graph failed')), \
             contextlib.redirect_stderr(io.StringIO()) as err:
            with self.assertRaises(SystemExit) as ex:
                r.cmd_hook(None)
        self.assertEqual(ex.exception.code, 1)
        self.assertIn('graph failed', err.getvalue())

    def _vault(self, tmp, cards):
        """Build a tiny vault: cards = {name: (ledger hooks, thread text)}."""
        root = Path(tmp)
        (root / '20_Wiki' / 'summaries').mkdir(parents=True)
        (root / '00_Inbox' / '_processed').mkdir(parents=True)
        ledger = []
        for name, (hooks, text) in cards.items():
            thread = f'00_Inbox/_processed/{name}__thread.md'
            (root / thread).write_text(text, encoding='utf-8')
            (root / '20_Wiki' / 'summaries' / f'{name}__summary.md').write_text(
                f'---\nthread_ref: {thread}\nmentioned_entities: []\n---\n\n## Links\n', encoding='utf-8')
            ledger.append(f'- 2026-01-01 [[{name}__summary|{name}]] — {hooks}')
        (root / '20_Wiki' / 'recall.md').write_text('\n'.join(ledger) + '\n', encoding='utf-8')
        return [patch.object(r, 'VAULT', tmp),
                patch.object(r, 'SUMMARIES', str(root / '20_Wiki' / 'summaries')),
                patch.object(r, 'RECALL', str(root / '20_Wiki' / 'recall.md')),
                patch.object(r, 'RECALL_COLD', str(root / '20_Wiki' / 'none.md')),
                patch.object(r, 'ALIASES', str(root / '20_Wiki' / 'none.tsv')),
                patch.object(r, 'HEBBIAN', str(root / '20_Wiki' / 'none.tsv')),
                patch.object(r, 'FT_CACHE', str(root / 'cache.pkl')),
                patch.object(r, 'vault_open', return_value=False)]

    def _fired(self, cards, query):
        with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as st:
            for p in self._vault(tmp, cards):
                st.enter_context(p)
            g = r.Graph()
            seeds, fired, _ = g.match_seeds(query, g.seed_dict())
            return {g.nodes[i][2:] for i in seeds}, fired

    def test_latin_hook_matches_whole_words_with_inflection(self):
        cards = {'a': ('art · gallery', 'x'), 'b': ('engine', 'y')}
        hit, _ = self._fired(cards, 'we start the engine')
        self.assertEqual(hit, {'b__summary'})
        hit, _ = self._fired(cards, 'old arts and crafts')
        self.assertEqual(hit, {'a__summary'})

    def test_korean_hook_matches_with_particles(self):
        hit, _ = self._fired({'a': ('잠자리', 'x')}, '잠자리를 봤어')
        self.assertEqual(hit, {'a__summary'})

    def test_fulltext_uses_light_stem_for_latin_words(self):
        cards = {'a': ('unrelated', 'we talked about one memory of the lake'),
                 'b': ('other', 'a star was bright')}
        hit, fired = self._fired(cards, 'any memories?')
        self.assertEqual(hit, {'a__summary'})
        hit, _ = self._fired(cards, 'start')
        self.assertEqual(hit, set())

    def test_hook_prints_ledger_line(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as st:
            for p in self._vault(tmp, {'a': ('lighthouse · ferry', 'x')}):
                st.enter_context(p)
            st.enter_context(patch.object(r.sys, 'stdin', io.StringIO('{"prompt":"the ferry was late"}')))
            out = st.enter_context(contextlib.redirect_stdout(io.StringIO()))
            r.cmd_hook(None)
        self.assertIn('Recalled memories', out.getvalue())
        self.assertIn('[[a__summary|a]]', out.getvalue())

if __name__ == '__main__':
    unittest.main()
