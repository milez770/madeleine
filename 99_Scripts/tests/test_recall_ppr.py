import argparse
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
import time
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('recall', Path(__file__).parents[1] / 'recall_ppr.py')
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)


class FakeGraph:
    """A graph with fixed PPR scores for the prompt (text starting with Q) and for anything else."""

    def __init__(self, prompt_scores, answer_scores, fired=()):
        names = sorted(set(prompt_scores) | set(answer_scores))
        self.nodes = ['P:' + n for n in names]
        self.idx = {n: i for i, n in enumerate(self.nodes)}
        self.recall = {n: {'line': '- line ' + n, 'title': n, 'hooks': [], 'cold': False} for n in names}
        self.p, self.a, self.fired = prompt_scores, answer_scores, list(fired)

    def seed_dict(self):
        return {}

    def match_seeds(self, text, tm):
        return ({'q': 1.0} if text.startswith('Q') else {'a': 1.0}), self.fired, {}

    def ppr(self, seeds):
        src = self.p if 'q' in seeds else self.a
        return [src.get(n[2:], 0.0) for n in self.nodes]


def thread(*users):
    body = '\n\n---\n\n'.join(f'**User**: {u}\n\n---\n\n**Claude**: ok' for u in users)
    return '---\ndate: 2026-10-27\n---\n\n# Title\n\n' + body


def transcript(path, items):
    Path(path).write_text('\n'.join(json.dumps(x, ensure_ascii=False) for x in items), encoding='utf-8')


def user(text, **kw):
    return {'type': 'user', 'message': {'content': text}, **kw}


def said(text):
    return {'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': text}]}}


def tool(**inp):
    return {'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'name': 'Read', 'input': inp}]}}


class RecallTest(unittest.TestCase):
    def setUp(self):
        """Every test writes under its own folder: vault root, per-machine cache, hook files."""
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home)
        for name, value in (('VAULT', self.home),
                            ('SHOWN_DIR', os.path.join(self.home, 'cache', 'shown')),
                            ('USE_TURN_DIR', os.path.join(self.home, 'cache', 'use_turn')),
                            ('PRECHECK_SCORES', os.path.join(self.home, 'cache', 'scores.txt')),
                            ('MARKER', os.path.join(self.home, 'marker')),
                            ('OPEN', os.path.join(self.home, 'open'))):
            p = patch.object(r, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = patch.dict(os.environ, {'VAULT_LOCK_TRANSCRIPTS': self.home})
        p.start()
        self.addCleanup(p.stop)

    def test_entity_list_forms_and_quoted_paths(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(r, 'SUMMARIES', tmp), patch.object(r, 'vault_open', return_value=False):
            for entities in ('[one, "two"]', '\n  - one\n  - "two"', '\n- one\n- "two"'):
                Path(tmp, 'card.md').write_text('---\nthread_ref: "00_Inbox/a b.md"\nmentioned_entities: '
                                                + entities + '\ndisagreements_or_corrections:\n- not an entity\n---\n')
                info = r.parse_summaries()['card']
                self.assertEqual(info['entities'], ['one', 'two'], entities)
                self.assertEqual(info['thread'], '00_Inbox/a b.md')

    def test_hook_failure_is_not_an_empty_success(self):
        with patch.object(r.sys, 'stdin', io.StringIO('{"prompt":"a long enough question"}')), \
             patch.object(r, 'Graph', side_effect=RuntimeError('graph failed')), \
             contextlib.redirect_stderr(io.StringIO()) as err:
            with self.assertRaises(SystemExit) as ex:
                r.cmd_hook(None)
        self.assertEqual(ex.exception.code, 1)
        self.assertIn('graph failed', err.getvalue())

    def test_stopcheck_failure_is_visible(self):
        with patch.object(r.sys, 'stdin', io.StringIO('{"transcript_path":"/nonexistent/record"}')), \
             contextlib.redirect_stderr(io.StringIO()) as err:
            with self.assertRaises(SystemExit) as ex:
                r.cmd_stopcheck(None)
        self.assertEqual(ex.exception.code, 1)
        self.assertIn('recall failed', err.getvalue())

    # ---- seeds ----

    def _vault(self, tmp, cards):
        """Build a tiny vault: cards = {name: (ledger hooks, thread text)}."""
        root = Path(tmp)
        (root / '20_Wiki' / 'summaries').mkdir(parents=True)
        (root / '00_Inbox' / '_processed').mkdir(parents=True)
        ledger = []
        for name, (hooks, text) in cards.items():
            thread_path = f'00_Inbox/_processed/{name}__thread.md'
            (root / thread_path).write_text(text, encoding='utf-8')
            (root / '20_Wiki' / 'summaries' / f'{name}__summary.md').write_text(
                f'---\nthread_ref: {thread_path}\nmentioned_entities: []\n---\n\n## Links\n', encoding='utf-8')
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

    def test_hook_prints_ledger_line_and_builds_index_in_background(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as st:
            for p in self._vault(tmp, {'a': ('lighthouse · ferry', 'x')}):
                st.enter_context(p)
            popen = st.enter_context(patch('subprocess.Popen'))
            st.enter_context(patch.object(r.sys, 'stdin', io.StringIO('{"prompt":"the ferry was late"}')))
            out = st.enter_context(contextlib.redirect_stdout(io.StringIO()))
            r.cmd_hook(None)
        self.assertIn('Recalled memories', out.getvalue())
        self.assertIn('- 2026-01-01 [[a__summary|a]] — lighthouse · ferry', out.getvalue())
        self.assertEqual([c.args[0][1:] for c in popen.call_args_list], [[os.path.abspath(r.__file__), 'build']])

    # ---- prompt side ----

    def test_prompt_cards_are_top_five(self):
        g = FakeGraph({f'c{i}': 0.1 - i * 0.01 for i in range(8)}, {})
        self.assertEqual(r.prompt_cards(g, {'q': 1.0}, {}), ['c0', 'c1', 'c2', 'c3', 'c4'])

    def test_queue_lines_keep_two_overlapping_threads(self):
        with patch.object(r, 'vault_open', return_value=False):
            q = Path(r.VAULT, '00_Inbox', '_queue')
            q.mkdir(parents=True)
            files = {
                'a': thread('fix the recall hook', 'next'),        # two tokens
                'b': thread('the hook is broken'),                 # one token, newest
                'c': thread('recall feels off'),                   # one token, older
                'd': thread('check the stock'),                    # no overlap
                'e': thread('This session is being continued recall hook'),   # only in injected text
                'f': thread('a fishhook and recalling'),           # Latin tokens inside other words
            }
            for i, (k, text) in enumerate(files.items()):
                p = q / f'2026-10-27_{k}__thread.md'
                p.write_text(text, encoding='utf-8')
                os.utime(p, (1000 + i, 1000 + (10 if k == 'b' else i)))
            lines = r.queue_lines(['recall', 'hook~'])
            self.assertEqual(r.queue_lines([]), [])
        self.assertEqual(len(lines), 2)
        self.assertIn('2026-10-27 a', lines[0])
        self.assertIn('"fix the recall hook" · "next"', lines[0])
        self.assertIn('2026-10-27 b', lines[1])

    def test_queue_lines_match_korean_inside_words(self):
        with patch.object(r, 'vault_open', return_value=False):
            q = Path(r.VAULT, '00_Inbox', '_queue')
            q.mkdir(parents=True)
            (q / '2026-10-27_k__thread.md').write_text(thread('연상 훅을 고치자'), encoding='utf-8')
            self.assertEqual(len(r.queue_lines(['훅'])), 1)

    def hook_env(self, g, prompt):
        st = contextlib.ExitStack()
        st.enter_context(patch.object(r, 'vault_open', return_value=False))
        st.enter_context(patch.object(r, 'Graph', lambda: g))
        st.enter_context(patch.object(r.sys, 'stdin', io.StringIO(json.dumps({'prompt': prompt}))))
        return st

    def test_hook_puts_queue_before_five_cards(self):
        g = FakeGraph({f'c{i}': 0.1 - i * 0.01 for i in range(8)}, {}, fired=['recall'])
        q = Path(r.VAULT, '00_Inbox', '_queue')
        q.mkdir(parents=True)
        for k in 'xyz':
            (q / f'2026-10-27_{k}__thread.md').write_text(thread('recall talk'), encoding='utf-8')
        with self.hook_env(g, 'Q recall hook question'), patch('subprocess.Popen'), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            r.cmd_hook(None)
        lines = [ln for ln in out.getvalue().splitlines() if ln.startswith('- ')]
        self.assertEqual(len(lines), 7)
        self.assertTrue(all('(not ingested)' in ln for ln in lines[:2]))
        self.assertEqual(lines[2:], ['- line c0', '- line c1', '- line c2', '- line c3', '- line c4'])

    def test_hook_without_index_builds_it_once_in_background(self):
        """No index folder at all: five keyword lines, and one background build."""
        g = FakeGraph({f'c{i}': 0.1 - i * 0.01 for i in range(8)}, {})
        Path(r.VAULT, '20_Wiki', 'summaries').mkdir(parents=True)
        with patch.object(r, 'SUMMARIES', os.path.join(r.VAULT, '20_Wiki', 'summaries')), \
             patch('subprocess.Popen') as popen:
            self.assertFalse(os.path.exists(r.raw_dir()))
            for _ in range(2):
                with self.hook_env(g, 'Q recall hook question'), \
                     contextlib.redirect_stdout(io.StringIO()) as out:
                    r.cmd_hook(None)
                self.assertEqual([ln for ln in out.getvalue().splitlines() if ln.startswith('- ')],
                                 [f'- line c{i}' for i in range(5)])
            self.assertTrue(os.path.exists(os.path.join(r.raw_dir(), 'building')))
        self.assertEqual([c.args[0][1:] for c in popen.call_args_list], [[os.path.abspath(r.__file__), 'build']])

    def test_hook_silent_on_injected(self):
        for q in self.INJ:
            with patch.object(r.sys, 'stdin', io.StringIO(json.dumps({'prompt': q, 'session_id': 't'}))), \
                 patch.object(r, 'Graph', side_effect=AssertionError('must not reach the graph')), \
                 contextlib.redirect_stdout(io.StringIO()) as out:
                r.cmd_hook(None)
            self.assertEqual(out.getvalue(), '', q)

    # ---- raw-text index ----

    RAW_TEXTS = {'c0': 'apple apple apple story', 'c1': 'zinc vow', 'c2': 'zinc vow',
                 'c3': 'apple apple story', 'c4': 'apple pie story', 'c5': 'zinc vow'}

    @contextlib.contextmanager
    def raw_vault(self, with_model=False):
        """Cards c0 to c5 with threads and summaries, and the index built from them.
        The model (with_model) has every piece vector [1,0,0,0]; the pair vector of c5 is
        [1,0,0,0] and all others [0,1,0,0], so any query points to c5."""
        with patch.object(r, 'SUMMARIES', os.path.join(r.VAULT, '20_Wiki', 'summaries')), \
             patch.object(r, 'vault_open', return_value=False):
            Path(r.SUMMARIES).mkdir(parents=True)
            Path(r.VAULT, '00_Inbox', '_processed').mkdir(parents=True)
            for name, text in self.RAW_TEXTS.items():
                self.add_card(name, text)
            with contextlib.redirect_stdout(io.StringIO()) as out:
                r.cmd_build(None)
            self.assertIn('build: 6 pairs', out.getvalue())
            d = r.raw_dir()
            if with_model:
                with open(os.path.join(d, 'units.json'), encoding='utf-8') as f:
                    units = json.load(f)['units']
                with open(os.path.join(d, 'model.json'), 'w') as f:
                    json.dump({'trained': 0, 'dim': 4, 'units': units}, f)
                Path(d, 'm_idf.f32').write_bytes(Path(d, 'idf.f32').read_bytes())
                Path(d, 'W.f32').write_bytes(r.array.array('f', [1.0, 0, 0, 0] * r.RAW_NB).tobytes())
                Path(d, 'E.f32').write_bytes(r.array.array(
                    'f', [x for u in units for x in ([1.0, 0, 0, 0] if u[0] == 'c5' else [0, 1.0, 0, 0])]).tobytes())
            yield d

    @staticmethod
    def add_card(name, text):
        Path(r.VAULT, '00_Inbox', '_processed', name + '.md').write_text(thread(text), encoding='utf-8')
        Path(r.SUMMARIES, name + '.md').write_text(f'---\nthread_ref: 00_Inbox/_processed/{name}.md\n---\n')

    def run_hook_with_raw(self, with_model):
        """Keyword ranks c0 first; raw ranks the cards with the most 'apple'; the model points to c5."""
        g = FakeGraph({f'c{i}': 0.1 - i * 0.01 for i in range(6)}, {})
        with self.raw_vault(with_model):
            with self.hook_env(g, 'Q apple story'), contextlib.redirect_stdout(io.StringIO()) as out:
                r.cmd_hook(None)
            lines = [ln for ln in out.getvalue().splitlines() if ln.startswith('- ')]
            path, num = lines[-1].rsplit(' → ', 1)[1].split(':')
            target = Path(r.VAULT, path).read_text(encoding='utf-8').splitlines()[int(num) - 1]
        self.assertIn('[keyword], [model] and [raw]', out.getvalue())
        return lines, target

    def test_hook_splits_two_two_two_without_duplicates(self):
        lines, target = self.run_hook_with_raw(True)
        self.assertEqual([ln.split(']')[0] for ln in lines], ['- [keyword'] * 2 + ['- [model'] + ['- [raw'] * 2)
        self.assertEqual(lines[:2], ['- [keyword] line c0', '- [keyword] line c1'])
        self.assertEqual(lines[2], '- [model] c5 [[c5|c5]] — you said: "zinc vow" → 00_Inbox/_processed/c5.md:7')
        self.assertIn('[[c3|c3]]', lines[3])   # keyword already took c0
        self.assertIn('[[c4|c4]]', lines[4])
        self.assertEqual(target, '**User**: apple pie story')

    def test_build_syncs_model_pairs_with_index(self):
        with self.raw_vault(True) as d:
            os.remove(os.path.join(r.SUMMARIES, 'c1.md'))   # a card that was sealed away
            self.add_card('c6', 'new talk')
            with contextlib.redirect_stdout(io.StringIO()) as out:
                r.cmd_build(None)
            with open(os.path.join(d, 'model.json'), encoding='utf-8') as f:
                mj = json.load(f)
            E = r.array.array('f')
            E.frombytes(Path(d, 'E.f32').read_bytes())
        self.assertIn('model pairs +1 -1', out.getvalue())
        self.assertEqual([u[0] for u in mj['units']], ['c0', 'c2', 'c3', 'c4', 'c5', 'c6'])
        self.assertEqual(list(E[-4:]), [1.0, 0, 0, 0])   # a new pair is the sum of learned piece vectors
        self.assertEqual(list(E[:4]), [0, 1.0, 0, 0])    # existing pairs keep their vectors
        self.assertEqual(len(E), 4 * len(mj['units']))

    def test_hook_without_model_gives_its_slots_to_keywords(self):
        lines, target = self.run_hook_with_raw(False)
        raw_line = lines[-1]
        self.assertEqual(lines[:4], [f'- [keyword] line c{i}' for i in range(4)])
        self.assertEqual(len(lines), 5)   # raw's top c0 and c3 are already shown; zinc cards share nothing
        self.assertEqual(raw_line, '- [raw] c4 [[c4|c4]] — you said: "apple pie story" → 00_Inbox/_processed/c4.md:7')
        self.assertEqual(target, '**User**: apple pie story')

    def test_rebuild_drops_removed_cards(self):
        with self.raw_vault() as d:
            os.remove(os.path.join(r.SUMMARIES, 'c1.md'))
            self.add_card('c6', 'new talk')
            with contextlib.redirect_stdout(io.StringIO()):
                r.cmd_build(None)
            with open(os.path.join(d, 'units.json'), encoding='utf-8') as f:
                units = json.load(f)['units']
        self.assertEqual([u[0] for u in units], ['c0', 'c2', 'c3', 'c4', 'c5', 'c6'])

    def test_thread_pairs_keep_first_user_line_and_skip_injected(self):
        p = Path(r.VAULT, 't.md')
        p.write_text(thread('first turn', 'This session is being continued summary', 'second turn',
                            '[Image: original 1200x800, displayed at 1200x800.]'), encoding='utf-8')
        pairs = r.thread_pairs(str(p))
        lines = p.read_text(encoding='utf-8').splitlines()
        self.assertEqual([x['u'] for x in pairs], ['first turn', 'second turn'])
        self.assertTrue(all(lines[x['line'] - 1].startswith('**User**: ') for x in pairs))
        self.assertEqual(pairs[0]['a'], 'ok\nok')   # the answer after an injected block joins the pair before
        self.assertEqual(pairs[1]['a'], 'ok\nok')

    INJ = ['Stop hook feedback: 2 cards', 'A session-scoped Stop hook is now active', '# Autonomous loop\ndo it',
           '/compact', '/foo:bar-baz  ', '<task-notification>x</task-notification>']

    def test_thread_pairs_skip_new_injected(self):
        p = Path(r.VAULT, 't.md')
        p.write_text(thread('first turn', *self.INJ, 'second turn', '/ talk about paths'), encoding='utf-8')
        self.assertEqual([x['u'] for x in r.thread_pairs(str(p))], ['first turn', 'second turn', '/ talk about paths'])

    def test_recent_pairs_newest_first_without_current_prompt(self):
        tpath = os.path.join(r.VAULT, 't.jsonl')
        transcript(tpath, [user('one'), said('answer 1'), user('[Image]', isMeta=True),
                           {'type': 'user', 'message': {'content': [{'type': 'tool_result'}]}},
                           user('two'), said('answer 2'), user('current prompt')])
        self.assertEqual(r.recent_pairs(tpath, 2, 'current prompt'), ['two\nanswer 2', 'one\nanswer 1'])
        self.assertEqual(r.recent_pairs('', 2), [])

    # ---- use ledger ----

    def ledger(self, cols=slice(2, 5)):
        return [ln.split('\t')[cols] for ln in Path(r.use_ledger()).read_text(encoding='utf-8').splitlines()]

    def test_use_ledger_marks_used_and_missed_from_opened_files(self):
        x = '2026-10-01_x__summary'
        with self.raw_vault():
            Path(r.VAULT, '00_Inbox', '_processed', '2026-10-01_x__thread.md').write_text(thread('apple'), encoding='utf-8')
            Path(r.SUMMARIES, x + '.md').write_text('---\nthread_ref: 00_Inbox/_processed/2026-10-01_x__thread.md\n---\n')
            opened = r.opened_cards([os.path.join(r.SUMMARIES, x + '.md'),                     # Read path
                                     "sed -n 7p '00_Inbox/_processed/2026-10-01_x__thread.md'",  # thread -> card
                                     'grep -l apple 20_Wiki/summaries/*.md',                     # a list search opens nothing
                                     'cat 2026-10-02_none__summary.md'])                       # no such card
            self.assertEqual(opened, {x})
            r.record_use('abcdef123', 'question\twith\nlines', [('keyword', 'c0'), ('raw', x)], opened | {'c3'})
            with patch.object(r, 'vault_open', return_value=True):   # nothing is written while unlocked
                r.record_use('s', 'q', [('keyword', 'c0')], set())
            rows = self.ledger(slice(None))
        self.assertEqual([x_[2:5] for x_ in rows], [['turn', 'keyword1·raw1', ''], ['used', 'raw', x], ['missed', '', 'c3']])
        self.assertEqual(rows[1][5], 'question with lines')
        self.assertEqual(rows[1][1], 'abcdef12')

    def test_record_use_writes_turn_once_and_only_new_rows(self):
        with self.raw_vault():
            shown = [('keyword', 'c0')]
            r.record_use('sid1', 'question', shown, {'c0'}, turn='3')
            r.record_use('sid1', 'question', shown, {'c0', 'c3'}, turn='3')   # Stop ran again in the same turn
            r.record_use('sid1', 'question', shown, {'c0', 'c3'}, turn='3')
            self.assertEqual(self.ledger(slice(1, 5)), [['sid1', 'turn', 'keyword1', ''], ['sid1', 'used', 'keyword', 'c0'],
                                                        ['sid1', 'missed', '', 'c3']])
            r.record_use('sid1', 'question', shown, {'c0'}, turn='9')        # the same prompt in a later turn
            r.record_use('sid2', 'question', shown, {'c0'}, turn='9')        # another session
            self.assertEqual(self.ledger(slice(1, 5))[3:], [['sid1', 'turn', 'keyword1', ''], ['sid1', 'used', 'keyword', 'c0'],
                                                            ['sid2', 'turn', 'keyword1', ''], ['sid2', 'used', 'keyword', 'c0']])

    def test_record_use_skips_injected(self):
        with self.raw_vault():
            for q in self.INJ:
                r.record_use('s', q, [('keyword', 'c0')], {'c0'})
            self.assertFalse(os.path.exists(r.use_ledger()))

    # ---- precheck and stopcheck ----

    def test_threshold_uses_fallback_then_recent_quantile(self):
        path = r.PRECHECK_SCORES
        self.assertEqual(r.precheck_threshold(), 0.041)
        os.makedirs(os.path.dirname(path))
        Path(path).write_text(''.join(f'{i / 1000:.6f}\n' for i in range(49)))
        self.assertEqual(r.precheck_threshold(), 0.041)
        Path(path).write_text(''.join(f'{i / 1000:.6f}\n' for i in range(400)))
        self.assertAlmostEqual(r.precheck_threshold(), 0.370)   # last 300 = 0.100 to 0.399

    def run_precheck(self, answer_scores, shown_age=0):
        g = FakeGraph({}, answer_scores)
        os.makedirs(r.SHOWN_DIR)
        sp = Path(r.SHOWN_DIR, 'old')
        sp.write_text('raw\tn2', encoding='utf-8')
        os.utime(sp, (time.time() - shown_age, time.time() - shown_age))
        with patch.object(r, 'Graph', lambda: g), contextlib.redirect_stdout(io.StringIO()) as out:
            r.cmd_precheck(argparse.Namespace(text='gist of the answer'))
        return out.getvalue(), Path(r.PRECHECK_SCORES).read_text() if os.path.exists(r.PRECHECK_SCORES) else None

    def test_precheck_gives_cards_over_threshold_skips_shown_and_records_best(self):
        out, rec = self.run_precheck({'n1': 0.09, 'n2': 0.08, 'n3': 0.07, 'n4': 0.01})
        self.assertIn('If anything conflicts with the answer, put it in the answer', out)
        self.assertIn('- line n1\n- line n3', out)
        self.assertNotIn('n2', out)
        self.assertNotIn('n4', out)
        self.assertEqual(rec, '0.090000\n')

    def test_precheck_ignores_stale_shown_file(self):
        out, _ = self.run_precheck({'n1': 0.09, 'n2': 0.08}, shown_age=700)
        self.assertIn('- line n2', out)

    def test_precheck_says_none_when_nothing_reaches(self):
        out, rec = self.run_precheck({'n1': 0.01})
        self.assertEqual(out, 'No memory reached\n')
        self.assertEqual(rec, '0.010000\n')

    LONG = 'answer ' * 50
    PRE = 'python3 99_Scripts/recall_ppr.py precheck "gist"'

    def run_stopcheck(self, items, active=False, sid='sid1'):
        tpath = os.path.join(r.VAULT, 't.jsonl')
        transcript(tpath, items)
        with patch.object(r, 'Graph', side_effect=AssertionError('stopcheck does not retrieve')), \
             patch.object(r.time, 'sleep', lambda s: None), \
             patch.object(r.sys, 'stdin', io.StringIO(json.dumps({
                 'transcript_path': tpath, 'session_id': sid, 'stop_hook_active': active}))), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            r.cmd_stopcheck(None)
        return out.getvalue()

    def test_stopcheck_blocks_long_answer_without_precheck(self):
        out = self.run_stopcheck([user('Q question'), user('[Image: original 10x10]', isMeta=True), said(self.LONG)])
        self.assertIn('precheck', json.loads(out)['reason'])

    def test_stopcheck_passes_with_precheck_short_or_active(self):
        for items, active in (([user('Q question'), tool(command=self.PRE), said(self.LONG)], False),
                              ([user('Q question'), said('answer ' * 40)], False),
                              ([user('Q question'), said(self.LONG)], True)):
            self.assertEqual(self.run_stopcheck(items, active), '', items)

    def cards_on_disk(self, *names):
        os.makedirs(r.SUMMARIES, exist_ok=True)
        for c in names:
            Path(r.SUMMARIES, c + '.md').write_text('---\n---\n')

    def test_stopcheck_records_opened_card_and_skips_shown(self):
        with patch.object(r, 'SUMMARIES', os.path.join(r.VAULT, 's')), patch.object(r, 'vault_open', return_value=False):
            Path(r.VAULT, '20_Wiki').mkdir()
            self.cards_on_disk('n2__summary')
            os.makedirs(r.SHOWN_DIR)
            Path(r.SHOWN_DIR, 'sid1').write_text('keyword\tn1\nraw\tx', encoding='utf-8')
            self.run_stopcheck([user('Q question'), tool(file_path=os.path.join(r.SUMMARIES, 'n2__summary.md')),
                                said('answer ' * 10)])
            self.assertEqual(self.ledger(), [['turn', 'keyword1·raw1', ''], ['missed', '', 'n2__summary']])

    def test_stopcheck_twice_in_one_turn_adds_only_new_rows(self):
        """A subagent report (isMeta) continues the answer to the same prompt and Stop runs again."""
        with patch.object(r, 'SUMMARIES', os.path.join(r.VAULT, 's')), patch.object(r, 'vault_open', return_value=False):
            Path(r.VAULT, '20_Wiki').mkdir()
            self.cards_on_disk('n2__summary', 'n3__summary')
            os.makedirs(r.SHOWN_DIR)
            Path(r.SHOWN_DIR, 'sid1').write_text('raw\tn1', encoding='utf-8')
            items = [user('Q question'), tool(file_path=os.path.join(r.SUMMARIES, 'n2__summary.md')), said('answer')]
            self.run_stopcheck(items)
            items += [user('subagent report', isMeta=True), tool(file_path=os.path.join(r.SUMMARIES, 'n3__summary.md')),
                      said('answer')]
            self.run_stopcheck(items)
            self.assertEqual(self.ledger(), [['turn', 'raw1', ''], ['missed', '', 'n2__summary'], ['missed', '', 'n3__summary']])

    def test_stopcheck_after_block_records_cards_opened_after_it(self):
        """Cards opened while running precheck after the block are recorded too. No second block."""
        with patch.object(r, 'SUMMARIES', os.path.join(r.VAULT, 's')), patch.object(r, 'vault_open', return_value=False):
            Path(r.VAULT, '20_Wiki').mkdir()
            self.cards_on_disk('n2__summary', 'n3__summary')
            os.makedirs(r.SHOWN_DIR)
            Path(r.SHOWN_DIR, 'sid1').write_text('raw\tn1', encoding='utf-8')
            items = [user('Q question'), tool(file_path=os.path.join(r.SUMMARIES, 'n2__summary.md')), said(self.LONG)]
            first = self.run_stopcheck(items)
            items += [user('Stop hook feedback:\ncheck', isMeta=True),
                      tool(file_path=os.path.join(r.SUMMARIES, 'n3__summary.md')), tool(command=self.PRE), said(self.LONG)]
            second = self.run_stopcheck(items, active=True)
            self.assertEqual(self.ledger(), [['turn', 'raw1', ''], ['missed', '', 'n2__summary'], ['missed', '', 'n3__summary']])
        self.assertIn('precheck', json.loads(first)['reason'])
        self.assertEqual(second, '')

    def test_hebbian_ledger_is_append_only(self):
        path = os.path.join(r.VAULT, 'hebb.tsv')
        with patch.object(r, 'HEBBIAN', path):
            r.append_hebbian({('P:a', 'E:x')})
            r.append_hebbian({('P:a', 'E:x')})
            rows = Path(path).read_text(encoding='utf-8').splitlines()
            self.assertEqual(len(rows), 3)
            self.assertTrue(rows[0].startswith('#'))
            self.assertAlmostEqual(r.load_hebbian()[('E:x', 'P:a')], 0.2)
        self.assertFalse(hasattr(r, 'cmd_compact'))


if __name__ == '__main__':
    unittest.main()
