#!/usr/bin/env python3
"""Associative recall: Personalized PageRank (spreading activation) over the vault.

Usage:
  python3 recall_ppr.py hook                  # UserPromptSubmit hook (reads JSON on stdin)
  python3 recall_ppr.py stopcheck             # Stop hook (Claude Code; reads JSON on stdin)
  python3 recall_ppr.py precheck "gist of the answer"
  python3 recall_ppr.py learn "text" <summary> [<summary> ...]
  python3 recall_ppr.py build                 # raw-text index (the hook also builds it)
  uv run --no-project --python 3.12 --with numpy --with scipy python 99_Scripts/recall_ppr.py train
                                              # optional learned model (by hand)
  python3 recall_ppr.py query "text" [--k 8] [--damping 0.5] [--eps 0.05]
                        [--no-degree-damp] [--debug]
  python3 recall_ppr.py stats

Standard library only, so the hook runs on any machine without setup. The graph is
not stored; it is rebuilt from the markdown files on every call. Seeds come from
recall ledger hooks, entity aliases, and the full text of every thread (cached in
FT_CACHE, keyed by file mtimes). The raw-text index (20_Wiki/.recall_raw/) compares
character pieces of user turns and answers. The optional learned model lives in the
same folder; only train needs numpy and scipy, and the hook reads the result with the
standard library. All of these are per-machine and derived.
"""
import argparse
import array
import contextlib
import csv
import json
import datetime
import hashlib
import mmap
import random
import glob
import math
import operator
import os
import re
import sys
import time
import unicodedata
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from session_to_thread import is_injected   # the one definition of harness text

VAULT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SUMMARIES = os.path.join(VAULT, '20_Wiki', 'summaries')
RECALL = os.path.join(VAULT, '20_Wiki', 'recall.md')
RECALL_COLD = os.path.join(VAULT, '20_Wiki', 'recall_cold.md')
ALIASES = os.path.join(VAULT, '20_Wiki', 'entity_aliases.tsv')
HEBBIAN = os.path.join(VAULT, '20_Wiki', 'recall_hebbian.tsv')
FT_CACHE = os.path.join(VAULT, '20_Wiki', '.recall_fulltext_cache.pkl')
OPEN = os.path.join(VAULT, '90_Vaulted', 'open')   # unlocked view of the sensitive store
MARKER = '/tmp/.vault_open_' + hashlib.md5(VAULT.encode()).hexdigest()[:8]


def vault_open():
    """When unlocked, the ledger, summaries and threads under open/ join recall."""
    return os.path.isdir(OPEN) and os.path.exists(MARKER)

# Base edge weights
W_PASSAGE_ENTITY = 1.0
W_PASSAGE_PASSAGE = 2.0   # "## Links" between summaries: curated, so strong
W_ENTITY_ENTITY = 0.3     # co-occurrence in one summary: noisy, so weak
DF_MAX = 50               # tokens found in more passages than this never seed
# Function words and chat filler. They appear in most prompts and would seed noise.
STOPWORDS = {
    # English
    'a', 'an', 'the', 'and', 'or', 'but', 'if', 'so', 'of', 'to', 'in', 'on', 'at', 'by',
    'for', 'from', 'with', 'into', 'onto', 'about', 'as', 'is', 'am', 'are', 'was', 'were',
    'be', 'been', 'being', 'do', 'does', 'did', 'doing', 'done', 'have', 'has', 'had',
    'will', 'would', 'shall', 'should', 'can', 'could', 'may', 'might', 'must', 'not', 'no',
    'yes', 'it', 'its', 'this', 'that', 'these', 'those', 'there', 'here', 'then', 'than',
    'what', 'when', 'where', 'which', 'who', 'whom', 'why', 'how', 'i', 'me', 'my', 'mine',
    'we', 'us', 'our', 'ours', 'you', 'your', 'yours', 'he', 'him', 'his', 'she', 'her',
    'they', 'them', 'their', 'up', 'out', 'over', 'just', 'also', 'very', 'really', 'too',
    'some', 'any', 'all', 'more', 'most', 'much', 'many', 'again', 'now', 'today',
    'yesterday', 'first', 'let', 'lets', 'please', 'okay', 'ok', 'thanks', 'hi', 'hey',
    'want', 'need', 'know', 'think', 'thought', 'like', 'thing', 'things', 'something',
    'anything', 'talk', 'story', 'experience', 'conversation', 'll', 've', 're', 'don',
    # Korean
    '어떻게', '어디까지', '다시', '그리고', '그래서', '하지만', '위해', '대해',
    '때문', '어제', '오늘', '지금', '이번', '저번', '먼저', '이제', '결국',
    '무엇', '왜냐', '해서', '하면', '하는', '하기', '해도', '이거', '그거',
    '이야기', '얘기', '경험', '자리', '전에', '생각', '대화'}
SEED_W_ENTITY = 1.0       # seed weight for a direct entity hit (id or alias)
EPS_UNIFORM = 0.05        # uniform share of the reset vector (exploration floor)
W_FULLTEXT = 0.5          # full-text seed weight; weaker than ledger hooks (1/df)
FT_TOKEN_MAX = 24         # longer full-text tokens are dropped (URL pieces, hashes)

# Hebbian layer: the only persistent state. Edges used together get stronger;
# the weight decays with time and the decay is computed at read time.
HEBB_LR = 0.1             # increment per reinforcement
HEBB_HALF_LIFE = 90.0     # days; effective = sum(delta * 0.5 ** (age_days / half_life))
HEBB_TOP_ENTITIES = 8     # top active entities that may be reinforced


def parse_summaries():
    """summary -> {'entities': [...], 'links': [other summary names...], 'thread': path}"""
    out = {}
    paths = glob.glob(os.path.join(SUMMARIES, '*.md'))
    if vault_open():
        paths += glob.glob(os.path.join(OPEN, '20_Wiki', 'summaries', '*.md'))
    for path in paths:
        name = os.path.splitext(os.path.basename(path))[0]
        with open(path, encoding='utf-8') as f:
            text = f.read()
        m = re.match(r'^---\n(.*?)\n---', text, re.S)
        fm = m.group(1) if m else ''
        em = re.search(r'^mentioned_entities:[ \t]*\[([^]\n]*)\]', fm, re.M)
        if em:
            ents = [e.strip().strip("\"'") for e in next(csv.reader([em.group(1)], skipinitialspace=True))]
        else:   # block list, with or without indentation under the key
            em = re.search(r'^mentioned_entities:[ \t]*\n((?:[ \t]*-[ \t]+[^\n]*(?:\n|$))*)', fm, re.M)
            ents = [e.strip().strip("\"'") for e in re.findall(r'-[ \t]+(.+)', em.group(1))] if em else []
        cm = re.search(r'## Links\n(.*?)(\n## |\Z)', text, re.S)
        links = [t.strip() for t in re.findall(r'\[\[([^\]|#]+)', cm.group(1))] if cm else []
        tr = re.search(r'^thread_ref:[ \t]*(.+)$', fm, re.M)
        thread = tr.group(1).strip() if tr else ''
        if thread.startswith('"'):
            thread = json.loads(thread)
        elif thread.startswith("'") and thread.endswith("'"):
            thread = thread[1:-1].replace("''", "'")
        out[name] = {'entities': ents, 'links': links,
                     'thread': thread}
    for info in out.values():  # keep summary-to-summary links only (not threads or hubs)
        info['links'] = [t for t in info['links'] if t in out]
    return out


def parse_recall():
    """summary -> {'title': str, 'hooks': [segments...], 'line': ledger line, 'cold': bool}"""
    out = {}
    ledgers = [(RECALL, False), (RECALL_COLD, True)]
    if vault_open():
        ledgers.append((os.path.join(OPEN, '20_Wiki', 'recall_vaulted.md'), False))
    for path, cold in ledgers:
        if not os.path.exists(path):
            continue
        for line in open(path, encoding='utf-8'):
            m = re.match(r'-\s+[\d-]+\s+\[\[([^\]|]+)\|([^\]]+)\]\]\s*(?:—\s*(.*))?', line)
            if not m:
                continue
            name, title, hooks = m.group(1).strip(), m.group(2).strip(), m.group(3) or ''
            out[name] = {'title': title,
                         'hooks': [h.strip() for h in hooks.split('·') if h.strip()],
                         'line': line.rstrip(), 'cold': cold}
    return out


def load_hebbian(now=None):
    """(nodeA, nodeB) -> effective weight. Decay is computed here, never stored."""
    out = {}
    if not os.path.exists(HEBBIAN):
        return out
    now = now or datetime.date.today()
    for line in open(HEBBIAN, encoding='utf-8'):
        if line.startswith('#') or not line.strip():
            continue
        parts = line.rstrip('\n').split('\t')
        if len(parts) != 4:
            continue
        a, b, delta, iso = parts
        try:
            days = (now - datetime.date.fromisoformat(iso)).days
            eff = float(delta) * 0.5 ** (max(days, 0) / HEBB_HALF_LIFE)
        except ValueError:
            continue
        key = tuple(sorted((a, b)))
        out[key] = out.get(key, 0.0) + eff
    return out


def append_hebbian(pairs, now=None):
    """Append reinforced edges. Existing rows are never rewritten, so git merges stay clean."""
    if not pairs:
        return
    now = now or datetime.date.today()
    new = os.path.exists(HEBBIAN)
    with open(HEBBIAN, 'a', encoding='utf-8') as f:
        if not new:
            f.write('# nodeA\tnodeB\tdelta\tdate - Hebbian ledger. Rows are only appended. '
                    'Decay is applied when read.\n')
        for a, b in sorted(pairs):
            f.write(f'{a}\t{b}\t{HEBB_LR}\t{now.isoformat()}\n')


_FT_SPLIT = re.compile(r'[\s/()\[\],.·—=→"\'`?!:;<>#*|-]+')
_FT_NUM = re.compile(r'[\d.%~+-]+')
_SUFFIXES = ('ies', 'ing', 'ied', 'ed', 'es', 's', 'y', 'e')
_INFLECT = r"(?:s|es|ed|d|ing|er|ers|'s)?"


def _latin(tok):
    """Tokens that start with an ASCII letter or digit use word-boundary matching.
    Other scripts (Korean, Japanese, ...) attach particles to words, so they keep
    substring and prefix matching."""
    return tok[:1].isascii() and tok[:1].isalnum()


def _head(w):
    """Leading ASCII part of a word. Korean particles can follow a Latin word ("slate를")."""
    m = re.match(r'[a-z0-9]+', w)
    return m.group(0) if m else w


def _stem(w):
    """Light English stemmer: memories/memory -> memor, studied/study -> stud."""
    for suf in _SUFFIXES:
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            return w[:-len(suf)]
    return w


def _ft_tokens(path):
    toks = set()
    for t in _FT_SPLIT.split(open(path, encoding='utf-8').read()):
        t = t.strip('-_%').lower()
        if not 2 <= len(t) <= FT_TOKEN_MAX or t in STOPWORDS:
            continue
        if _FT_NUM.fullmatch(t):
            continue
        toks.add(t)
    return toks


def load_fulltext(summaries):
    """Full text of every thread as a seed pool: (names, {token: 'idx,idx,...'}, sorted keys).

    A word that is missing from the ledger hooks can still find a conversation
    through its raw text. Threads never change, so the pickle cache is reused while
    the mtime signature matches. Tokens with df above DF_MAX are not indexed.
    Threads under open/ (the unlocked sensitive store) are never cached, so no
    vocabulary from them stays on disk after locking; they are tokenized on the fly.
    """
    import pickle
    refs, open_refs = [], []
    for name, info in sorted(summaries.items()):
        t = info.get('thread') or ''
        if not t:
            continue
        p = os.path.join(VAULT, t)
        po = os.path.join(OPEN, t)
        if os.path.exists(p):
            refs.append((p, name))
        elif vault_open() and os.path.exists(po):
            open_refs.append((po, name))
    sig = {p: os.path.getmtime(p) for p, _ in refs}
    names = post = keys = None
    try:
        with open(FT_CACHE, 'rb') as f:
            c = pickle.load(f)
        if c.get('sig') == sig:
            names, post, keys = c['names'], c['post'], c['keys']
    except Exception:
        pass
    if names is None:
        names, per = [], []
        for p, name in refs:
            try:
                toks = _ft_tokens(p)
            except Exception:
                continue
            names.append(name)
            per.append(toks)
        df = {}
        for toks in per:
            for t in toks:
                df[t] = df.get(t, 0) + 1
        post = {}
        for i, toks in enumerate(per):
            for t in toks:
                if df[t] <= DF_MAX:
                    post.setdefault(t, []).append(i)
        post = {t: ','.join(map(str, v)) for t, v in post.items()}
        keys = sorted(post)
        try:
            with open(FT_CACHE, 'wb') as f:
                pickle.dump({'sig': sig, 'names': names, 'post': post, 'keys': keys}, f)
        except Exception:
            pass
    if open_refs:
        names, post = list(names), dict(post)   # do not mutate the cached objects
        fresh = set()
        for p, name in open_refs:
            try:
                toks = _ft_tokens(p)
            except Exception:
                continue
            idx = str(len(names))
            names.append(name)
            for t in toks:
                post[t] = (post[t] + ',' + idx) if t in post else idx
                fresh.add(t)
        keys = sorted(set(keys) | fresh)
    return names, post, keys


def parse_aliases():
    """entity_id -> [aliases...]"""
    out = {}
    if not os.path.exists(ALIASES):
        return out
    for line in open(ALIASES, encoding='utf-8'):
        if line.startswith('#') or not line.strip():
            continue
        parts = [p.strip() for p in line.rstrip('\n').split('\t') if p.strip()]
        if parts:
            out[parts[0]] = parts[1:]
    return out


class Graph:
    def __init__(self, degree_damp=True):
        self.summaries = parse_summaries()
        self.recall = parse_recall()
        try:
            self.ft_names, self.ft_post, self.ft_keys = load_fulltext(self.summaries)
        except Exception:
            self.ft_names, self.ft_post, self.ft_keys = [], {}, []
        self.nodes = []          # 'P:<summary>' | 'E:<entity>'
        self.idx = {}
        self.adj = []            # idx → [(idx, w), ...]
        self._build(degree_damp)

    def _add(self, node):
        if node not in self.idx:
            self.idx[node] = len(self.nodes)
            self.nodes.append(node)
            self.adj.append([])
        return self.idx[node]

    def _edge(self, a, b, w):
        self.adj[a].append((b, w))
        self.adj[b].append((a, w))

    def _build(self, degree_damp):
        ent_deg = {}
        for info in self.summaries.values():
            for e in set(info['entities']):
                ent_deg[e] = ent_deg.get(e, 0) + 1
        damp = (lambda e: 1.0 / math.log2(1 + ent_deg[e])) if degree_damp else (lambda e: 1.0)
        for name, info in self.summaries.items():
            p = self._add('P:' + name)
            ents = sorted(set(info['entities']))
            for e in ents:
                self._edge(p, self._add('E:' + e), W_PASSAGE_ENTITY * damp(e))
            for i, a in enumerate(ents):
                for b in ents[i + 1:]:
                    self._edge(self.idx['E:' + a], self.idx['E:' + b],
                               W_ENTITY_ENTITY * damp(a) * damp(b))
        seen = set()
        for name, info in self.summaries.items():
            for t in info['links']:
                key = tuple(sorted((name, t)))
                if key in seen:
                    continue
                seen.add(key)
                self._edge(self.idx['P:' + name], self.idx['P:' + t], W_PASSAGE_PASSAGE)
        # Hebbian layer: structural weight x (1 + effective Hebbian weight)
        self.hebb = load_hebbian()
        if self.hebb:
            for i, nbrs in enumerate(self.adj):
                self.adj[i] = [
                    (j, w * (1 + self.hebb.get(tuple(sorted((self.nodes[i], self.nodes[j]))), 0.0)))
                    for j, w in nbrs]
        self.out_w = [sum(w for _, w in nbrs) for nbrs in self.adj]
        self.ent_deg = ent_deg

    # ---- seed dictionary ----
    def seed_dict(self):
        """token -> [(node idx, weight)] from ledger hooks and entity ids/aliases."""
        token_map = {}
        df = {}
        per_passage = {}
        for name, rec in self.recall.items():
            if 'P:' + name not in self.idx:
                continue
            toks = set()
            for seg in [rec['title']] + rec['hooks']:
                for t in re.split(r'[\s/()\[\],.·—=→"\'`?!:;]+', seg):
                    t = t.strip('-_%').lower()
                    if len(t) < 2 or t.isdigit() or re.fullmatch(r'[\d.%~+-]+', t):
                        continue
                    if t in STOPWORDS:
                        continue
                    toks.add(t)
            per_passage[name] = toks
            for t in toks:
                df[t] = df.get(t, 0) + 1
        for name, toks in per_passage.items():
            for t in toks:
                if df[t] > DF_MAX:
                    continue
                token_map.setdefault(t, []).append((self.idx['P:' + name], 1.0 / df[t]))
        aliases = parse_aliases()
        for e in self.ent_deg:
            node = self.idx['E:' + e]
            for t in [e] + aliases.get(e, []):
                if len(t) >= 2:
                    token_map.setdefault(t, []).append((node, SEED_W_ENTITY))
        return token_map

    def match_seeds(self, query, token_map):
        """Longest match first, with masking: a span taken by a long token cannot
        be taken again by a shorter one.

        Ledger and alias tokens: Latin tokens match on word boundaries and may carry
        a short English inflection ("art" matches "arts", not "start"). Other tokens
        match as substrings, because particles attach to the word in Korean and
        similar languages.

        The full-text pool is a separate pass over the words of the query. Latin
        words match index tokens with the same light stem. Other words take the
        longest prefix that exists in the index and union its postings. A union
        larger than DF_MAX is too generic and does not fire. This pass does not
        share the mask with the ledger pass, so raw-text fragments cannot steal a
        ledger hook.

        Returns (seeds, fired, cue_count). cue_count counts, per passage, how many
        distinct query tokens reached it (directly or via an entity); the caller
        boosts passages reached by several cues.
        """
        q = unicodedata.normalize('NFC', query)
        seeds = {}
        fired = []
        cue_tokens = {}   # passage idx -> {tokens that reached it}
        for tok in sorted(token_map, key=len, reverse=True):
            pos, hit = 0, False
            t = tok.lower()
            ql = q.lower()
            if t not in ql:
                continue
            pat = (re.compile(r'(?<![a-z0-9])' + re.escape(t) + _INFLECT + r'(?![a-z0-9])')
                   if _latin(t) else None)
            while True:
                if pat:
                    m = pat.search(ql, pos)
                    if not m:
                        break
                    i, end = m.start(), m.end()
                else:
                    i = ql.find(t, pos)
                    if i < 0:
                        break
                    end = i + len(t)
                q = q[:i] + '\x00' * (end - i) + q[end:]
                ql = q.lower()
                pos, hit = end, True
            if hit:
                fired.append(tok)
                for node, w in token_map[tok]:
                    seeds[node] = seeds.get(node, 0.0) + w
                    name = self.nodes[node]
                    if name.startswith('P:'):
                        cue_tokens.setdefault(node, set()).add(tok)
                    else:  # entity hit: the cue reaches every passage with that entity
                        for j, _ in self.adj[node]:
                            if self.nodes[j].startswith('P:'):
                                cue_tokens.setdefault(j, set()).add(tok)
        if self.ft_keys:
            import bisect
            numlike = re.compile(r'[\d.%~+-]+')
            words = []
            for w0 in re.split(r'[\s/()\[\],.·—=→"\'`?!:;<>#*|-]+',
                               unicodedata.normalize('NFC', query).lower()):
                w0 = w0.strip('-_%')
                if len(w0) >= 2 and w0 not in words and w0 not in STOPWORDS \
                        and not numlike.fullmatch(w0):
                    words.append(w0)
            for w0 in words:
                if _latin(w0):
                    w0 = _head(w0)
                    stem = _stem(w0)
                    union = set()
                    k = bisect.bisect_left(self.ft_keys, stem)
                    while k < len(self.ft_keys) and self.ft_keys[k].startswith(stem):
                        key = _head(self.ft_keys[k])
                        if key == w0 or _stem(key) == stem:
                            union.update(self.ft_post[self.ft_keys[k]].split(','))
                        if len(union) > DF_MAX:
                            break
                        k += 1
                    prefixes = [(stem, union)] if union and len(union) <= DF_MAX else []
                else:
                    prefixes = []
                    for L in range(min(len(w0), FT_TOKEN_MAX), 1, -1):
                        sub = w0[:L]
                        if sub in STOPWORDS:
                            continue
                        union = set()
                        k = bisect.bisect_left(self.ft_keys, sub)
                        while k < len(self.ft_keys) and self.ft_keys[k].startswith(sub):
                            union.update(self.ft_post[self.ft_keys[k]].split(','))
                            if len(union) > DF_MAX:
                                break
                            k += 1
                        if not union:
                            continue           # try a shorter prefix
                        if len(union) <= DF_MAX:
                            prefixes = [(sub, union)]
                        break                  # shorter prefixes only grow the union
                for sub, union in prefixes:
                    wgt = W_FULLTEXT / len(union)
                    for fi in union:
                        node = self.idx.get('P:' + self.ft_names[int(fi)])
                        if node is None:
                            continue
                        seeds[node] = seeds.get(node, 0.0) + wgt
                        cue_tokens.setdefault(node, set()).add(sub)
                    fired.append(sub + '~')   # ~ marks a full-text hit (for --debug)
        cue_count = {n: len(s) for n, s in cue_tokens.items()}
        return seeds, fired, cue_count

    # ---- PPR ----
    def ppr(self, seeds, damping=0.5, eps=EPS_UNIFORM, iters=100, tol=1e-10):
        n = len(self.nodes)
        if not n:
            return []
        total = sum(seeds.values())
        uni = 1.0 / n
        if total <= 0:
            reset = [uni] * n
        else:
            reset = [eps * uni + 0.0] * n
            for node, w in seeds.items():
                reset[node] += (1 - eps) * w / total
            s = sum(reset)
            reset = [r / s for r in reset]
        x = reset[:]
        for _ in range(iters):
            nxt = [0.0] * n
            dangling = 0.0
            for i, xi in enumerate(x):
                if not self.adj[i]:
                    dangling += xi
                    continue
                share = xi / self.out_w[i]
                for j, w in self.adj[i]:
                    nxt[j] += share * w
            x2 = [(1 - damping) * reset[i] + damping * (nxt[i] + dangling * reset[i])
                  for i in range(n)]
            if sum(abs(a - b) for a, b in zip(x2, x)) < tol:
                x = x2
                break
            x = x2
        return x


def cmd_stats(args):
    t0 = time.time()
    g = Graph(degree_damp=not args.no_degree_damp)
    build_ms = (time.time() - t0) * 1000
    n_pass = sum(1 for n in g.nodes if n.startswith('P:'))
    n_ent = len(g.nodes) - n_pass
    n_edges = sum(len(a) for a in g.adj) // 2
    seen, comps, iso_pass = set(), [], 0
    for i in range(len(g.nodes)):
        if i in seen:
            continue
        stack, comp = [i], set()
        while stack:
            v = stack.pop()
            if v in comp:
                continue
            comp.add(v)
            stack.extend(j for j, _ in g.adj[v] if j not in comp)
        seen |= comp
        comps.append(comp)
        if len(comp) == 1 and g.nodes[i].startswith('P:'):
            iso_pass += 1
    comps.sort(key=len, reverse=True)
    giant_pass = sum(1 for i in comps[0] if g.nodes[i].startswith('P:')) if comps else 0
    top = sorted(g.ent_deg.items(), key=lambda kv: -kv[1])[:10]
    tm = g.seed_dict()
    t1 = time.time()
    seeds, _, _ = g.match_seeds('refund policy for the book club', tm)
    g.ppr(seeds)
    ppr_ms = (time.time() - t1) * 1000
    print(f'nodes {len(g.nodes)} (passages {n_pass} + entities {n_ent}) · edges {n_edges}')
    print(f'components {len(comps)} · passages in largest component {giant_pass}/{n_pass}'
          f' ({100 * giant_pass / max(n_pass, 1):.0f}%) · isolated passages {iso_pass}')
    print(f'ledger lines {len(g.recall)} (cold {sum(1 for r in g.recall.values() if r["cold"])})'
          f' · seed tokens {len(tm)} · alias table {"yes" if os.path.exists(ALIASES) else "no"}')
    print('top entities by degree:', ', '.join(f'{e}({d})' for e, d in top))
    if g.hebb:
        thick = sorted(g.hebb.items(), key=lambda kv: -kv[1])[:5]
        print(f'Hebbian pairs {len(g.hebb)} · strongest:',
              ', '.join(f'{a[2:]}—{b[2:]}({v:.2f})' for (a, b), v in thick))
    else:
        print('no Hebbian weights yet')
    print(f'build {build_ms:.0f}ms · seeds+PPR {ppr_ms:.0f}ms')


def cmd_query(args):
    g = Graph(degree_damp=not args.no_degree_damp)
    tm = g.seed_dict()
    seeds, fired, cues = g.match_seeds(args.text, tm)
    if not seeds:
        print('(no seeds: no token matched)')
        return
    scores = g.ppr(seeds, damping=args.damping, eps=args.eps)
    if args.debug:
        print('fired tokens:', ', '.join(sorted(fired, key=len, reverse=True)[:20]))
        seed_pass = sorted(((w, g.nodes[i]) for i, w in seeds.items()), reverse=True)[:8]
        print('top seeds:', ', '.join(f'{n[2:]}({w:.2f})' for w, n in seed_pass))
        ent_top = sorted(((scores[i], n) for i, n in enumerate(g.nodes) if n.startswith('E:')),
                         reverse=True)[:8]
        print('active entities:', ', '.join(f'{n[2:]}({s:.4f})' for s, n in ent_top))
        print()
    # Multi-cue boost: a passage reached by n distinct tokens scores x(1 + boost*(n-1))
    ranked = sorted(((scores[i] * (1 + args.cue_boost * (cues.get(i, 0) - 1 if cues.get(i, 0) else 0)),
                      n[2:]) for i, n in enumerate(g.nodes) if n.startswith('P:')),
                    reverse=True)[:args.k]
    for score, name in ranked:
        rec = g.recall.get(name)
        mark = '❄' if rec and rec['cold'] else ' '
        title = rec['title'] if rec else name
        direct = '●' if g.idx['P:' + name] in seeds else ' '
        print(f'{score:.5f} {mark}{direct} {title}')
        print(f'          [[{name}]]')


def cmd_learn(args):
    """Reinforce only the cards the session actually Read. Being surfaced is not a
    signal; being chosen is.

    Reinforced: curated links among the chosen cards, edges from a chosen card to
    its entities that carried activation this turn (seeded or among the top active
    entities), and edges among those entities. Candidates that were not chosen get
    nothing.
    """
    g = Graph()
    chosen = []
    for name in args.summaries:
        name = re.sub(r'\.md$', '', os.path.basename(name))
        if 'P:' + name in g.idx:
            chosen.append(g.idx['P:' + name])
        else:
            print(f'(skipped: {name} is not a summary)')
    if not chosen:
        return
    tm = g.seed_dict()
    seeds, _, _ = g.match_seeds(args.text, tm)
    scores = g.ppr(seeds) if seeds else [0.0] * len(g.nodes)
    top_ents = set(sorted((i for i, n in enumerate(g.nodes) if n.startswith('E:')),
                          key=lambda i: -scores[i])[:HEBB_TOP_ENTITIES])
    bridge = set()   # entities of chosen cards that carried activation this turn
    for p in chosen:
        for j, _ in g.adj[p]:
            if g.nodes[j].startswith('E:') and (j in seeds or j in top_ents):
                bridge.add(j)
    core = set(chosen) | bridge
    pairs = set()
    for i in core:
        for j, _ in g.adj[i]:
            if j in core:
                pairs.add(tuple(sorted((g.nodes[i], g.nodes[j]))))
    append_hebbian(pairs)
    print(f'learn: {len(chosen)} card(s), {len(pairs)} edge(s) reinforced')


# ---- Raw-text recall: per-machine index and learned model (20_Wiki/.recall_raw/) ----
# A memory unit is one user turn plus the answer that follows it. Ledger hooks and
# entities are not used here.
RAW_NB = 1 << 18             # hash buckets for character pieces
RAW_DIM = 128                # model vector size used by train (readers take dim from model.json)
MODEL_FILES = ('W.f32', 'm_idf.f32', 'E.f32', 'model.json')   # replaced in this order, model.json last
RAW_U_MAX, RAW_A_MAX = 1500, 2500   # characters kept per user turn and per answer
FLOW_W = (1.0, 0.5, 0.25)    # current prompt, previous exchange, the one before
# Prompt-side summary candidates on a machine with the index, per method, without overlap.
# Without model files, [keyword] takes the model slots.
HOOK_SPLIT = (('keyword', 2), ('model', 2), ('raw', 2))
RAW_REBUILD_LOCK_S = 900     # a background rebuild is not started again within this time
RECALL_CACHE = os.path.join(os.path.expanduser('~/.cache/madeleine'),
                            hashlib.md5(VAULT.encode()).hexdigest()[:8])   # per machine and vault
SHOWN_DIR = os.path.join(RECALL_CACHE, 'shown')         # per session: cards shown on the prompt side
USE_TURN_DIR = os.path.join(RECALL_CACHE, 'use_turn')   # per session: rows written for the last turn
_OPEN_SUMMARY = re.compile(r'([^\s/"\'*?]+__summary)(?:\.md)?')
_OPEN_THREAD = re.compile(r'([^\s/"\'*?]+__thread)\.md')
_RAW_TOK = re.compile(r'\w+')
_PAIR_HEAD = re.compile(r'^\*\*(User|Claude|Codex)\*\*: ', re.M)


def raw_dir():
    return os.path.join(VAULT, '20_Wiki', '.recall_raw')


def thread_pairs(path):
    """thread -> [{'line', 'u', 'a'}]: each user turn with the answer that follows it.
    Injected harness text is skipped."""
    with open(path, encoding='utf-8') as f:
        text = f.read()
    out, cur, line = [], None, 1
    for k, blk in enumerate(text.split('\n\n---\n\n')):
        start, line = line, line + blk.count('\n') + 4
        m = _PAIR_HEAD.search(blk)
        if not m or (m.start() and k):   # only the first chunk has a header before the speaker
            continue
        start += blk.count('\n', 0, m.start())
        body = blk[m.end():].strip()
        if m.group(1) == 'User':
            if not body or is_injected(body):
                continue
            cur = {'line': start, 'u': body[:RAW_U_MAX], 'a': ''}
            out.append(cur)
        elif cur is not None and len(cur['a']) < RAW_A_MAX:
            cur['a'] = (cur['a'] + '\n' + body if cur['a'] else body)[:RAW_A_MAX]
    return out


def raw_counts(text):
    """Character pieces (2 and 3 characters inside a word), hashed to buckets -> count."""
    c = {}
    for w in _RAW_TOK.findall(text.lower()):
        if len(w) > 30:
            continue
        s = '<' + w + '>'
        for n in (2, 3):
            for i in range(len(s) - n + 1):
                b = zlib.crc32(s[i:i + n].encode()) & (RAW_NB - 1)
                c[b] = c.get(b, 0) + 1
    return c


def raw_units():
    """All pairs of threads that have a summary -> ([card, line, thread path, start of the
    user turn], [pair text], [user turn]). Only cards in 20_Wiki/summaries are used, so
    nothing from the unlocked sensitive store is written to the per-machine files."""
    units, texts, users = [], [], []
    for name, info in sorted(parse_summaries().items()):
        t = info.get('thread') or ''
        p = os.path.join(VAULT, t)
        if not t or not os.path.exists(os.path.join(SUMMARIES, name + '.md')) or not os.path.exists(p):
            continue
        try:
            pairs = thread_pairs(p)
        except Exception:
            continue
        for pr in pairs:
            snip = pr['u'].split('\n', 1)[0].strip()
            units.append([name, pr['line'], t, snip[:100] + ('…' if len(snip) > 100 else '')])
            texts.append(pr['u'] + '\n' + pr['a'])
            users.append(pr['u'])
    return units, texts, users


def _tofile(path, arr):
    with open(path + '.tmp', 'wb') as f:
        arr.tofile(f)
    os.replace(path + '.tmp', path)


def cmd_build(args):
    """Raw-text index: for each pair, the weights of its character pieces (log count x
    rarity, scaled to length 1), stored as an inverted index."""
    t0 = time.time()
    d = raw_dir()
    os.makedirs(d, exist_ok=True)
    units, texts, _ = raw_units()
    keys, vals = [], []
    df = array.array('i', bytes(4 * RAW_NB))
    for text in texts:
        c = raw_counts(text)
        ks = array.array('i', c.keys())
        keys.append(ks)
        vals.append(array.array('f', (1.0 + math.log(v) for v in c.values())))
        for b in ks:
            df[b] += 1
    n = len(texts)
    idf = array.array('f', (math.log((n + 1) / (x + 1)) + 1 for x in df))
    cnt = array.array('q', bytes(8 * (RAW_NB + 1)))
    for ks, vs in zip(keys, vals):
        for i, b in enumerate(ks):
            vs[i] *= idf[b]
        nrm = math.sqrt(sum(x * x for x in vs)) or 1.0
        for i, b in enumerate(ks):
            vs[i] /= nrm
            cnt[b + 1] += 1
    for b in range(RAW_NB):
        cnt[b + 1] += cnt[b]
    pos = array.array('q', cnt)
    post_u = array.array('i', bytes(4 * cnt[RAW_NB]))
    post_w = array.array('f', bytes(4 * cnt[RAW_NB]))
    for u, (ks, vs) in enumerate(zip(keys, vals)):
        for b, w in zip(ks, vs):
            p = pos[b]
            post_u[p], post_w[p] = u, w
            pos[b] = p + 1
    _tofile(os.path.join(d, 'idf.f32'), idf)
    _tofile(os.path.join(d, 'post_ptr.i64'), cnt)
    _tofile(os.path.join(d, 'post_unit.i32'), post_u)
    _tofile(os.path.join(d, 'post_w.f32'), post_w)
    with open(os.path.join(d, 'units.json.tmp'), 'w', encoding='utf-8') as f:
        json.dump({'built': time.time(), 'units': units}, f, ensure_ascii=False)
    os.replace(os.path.join(d, 'units.json.tmp'), os.path.join(d, 'units.json'))
    with model_lock():
        added, removed = sync_model(units, texts)
    try:
        os.remove(os.path.join(d, 'building'))
    except OSError:
        pass
    print(f'build: {n} pairs · {cnt[RAW_NB]:,} pieces · model pairs +{added} -{removed} · '
          f'{time.time() - t0:.0f}s')


def cmd_train(args):
    """Learned model: pairs that follow each other in one conversation and pairs of
    hand-linked cards (## Links) are pulled together, pairs of other conversations pushed apart.

    Each character piece gets a RAW_DIM vector, and a pair vector is the sum of its piece
    vectors. Training starts from an SVD of the raw-text matrix and refines it with
    contrastive learning. Needs numpy and scipy; the hook reads only the resulting files.
    """
    import numpy as np
    import scipy.sparse as sp
    from scipy.sparse.linalg import svds
    t0 = time.time()
    random.seed(0)
    np.random.seed(0)
    units, texts, users = raw_units()

    def csr(docs):
        rows, cols, vs = [], [], []
        for i, t in enumerate(docs):
            for b, v in raw_counts(t).items():
                rows.append(i); cols.append(b); vs.append(1.0 + math.log(v))
        return sp.csr_matrix((np.array(vs, np.float32), (rows, cols)), shape=(len(docs), RAW_NB))

    by_card = {}
    for k, (card, *_rest) in enumerate(units):
        by_card.setdefault(card, []).append(k)
    if not any(len(ks) >= 2 for ks in by_card.values()):
        print('train: no conversation has two or more user turns yet; nothing to learn from')
        raise SystemExit(1)
    Xraw = csr(texts)
    dfv = np.asarray((Xraw > 0).sum(0)).ravel()
    idf = (np.log((Xraw.shape[0] + 1) / (dfv + 1)) + 1).astype(np.float32)

    def norm(X):
        X = X.multiply(idf).tocsr()
        nr = np.sqrt(np.asarray(X.multiply(X).sum(1)).ravel()) + 1e-9
        return (sp.diags((1 / nr).astype(np.float32)) @ X).astype(np.float32).tocsr()

    X, XU = norm(Xraw), norm(csr(users))
    anchors, positives = [], []
    for ks in by_card.values():
        if len(ks) < 2:
            continue
        for j, k in enumerate(ks):
            row = XU[k] * FLOW_W[0]
            for back in (1, 2):
                if j - back >= 0:
                    row = row + X[ks[j - back]] * FLOW_W[back]
            anchors.append(row)
            positives.append([o for o in ks if o != k])
    A = sp.vstack(anchors).tocsr().astype(np.float32)
    links = [(a, b) for a, info in parse_summaries().items() if a in by_card
             for b in info['links'] if b in by_card and b != a]
    dim = min(RAW_DIM, min(X.shape) - 1)   # a small vault has fewer pairs than RAW_DIM
    _, _, Vt = svds(X, k=dim)
    W = Vt.T.astype(np.float32).copy()
    m1, m2 = np.zeros_like(W), np.zeros_like(W)
    B, tau, lr, steps = 256, 0.05, 2e-3, 200
    for step in range(1, steps + 1):
        ia = [random.randrange(len(positives)) for _ in range(B // 2 if links else B)]
        Xa = [A[j] for j in ia]
        Xp = [random.choice(positives[j]) for j in ia]
        for _ in range(B - len(Xa)):
            a, b = random.choice(links)
            Xa.append(X[random.choice(by_card[a])])
            Xp.append(random.choice(by_card[b]))
        Xa = sp.vstack(Xa).tocsr()
        Xp = X[Xp]
        Ea, Ep = np.asarray(Xa @ W), np.asarray(Xp @ W)
        na = np.linalg.norm(Ea, axis=1, keepdims=True) + 1e-9
        nb = np.linalg.norm(Ep, axis=1, keepdims=True) + 1e-9
        ea, ep = Ea / na, Ep / nb
        L = ea @ ep.T / tau
        L -= L.max(1, keepdims=True)
        P = np.exp(L)
        P /= P.sum(1, keepdims=True)
        dL = (P - np.eye(len(P), dtype=np.float32)) / len(P)
        dea, dep = dL @ ep / tau, dL.T @ ea / tau
        dA = (dea - ea * (dea * ea).sum(1, keepdims=True)) / na
        dP = (dep - ep * (dep * ep).sum(1, keepdims=True)) / nb
        g = (np.asarray(Xa.T @ dA) + np.asarray(Xp.T @ dP)).astype(np.float32)
        m1 = 0.9 * m1 + 0.1 * g
        m2 = 0.999 * m2 + 0.001 * g * g
        W -= lr * (m1 / (1 - 0.9 ** step)) / (np.sqrt(m2 / (1 - 0.999 ** step)) + 1e-8)
    E = np.asarray(X @ W)
    E /= np.linalg.norm(E, axis=1, keepdims=True) + 1e-9
    d = raw_dir()
    os.makedirs(d, exist_ok=True)
    for name, arr in (('W.f32', W), ('m_idf.f32', idf), ('E.f32', E.astype(np.float32))):
        arr.astype(np.float32).tofile(os.path.join(d, name + '.train'))   # apart from sync_model's .tmp
    with open(os.path.join(d, 'model.json.train'), 'w', encoding='utf-8') as f:
        json.dump({'trained': time.time(), 'dim': dim, 'units': units}, f, ensure_ascii=False)
    with model_lock():
        for name in MODEL_FILES:
            os.replace(os.path.join(d, name + '.train'), os.path.join(d, name))
    print(f'train: {len(units)} pairs · {len(anchors)} flow pairs · {len(links)} hand links · '
          f'{dim} dims · {time.time() - t0:.0f}s')


def _map(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        return mmap.mmap(fd, 0, prot=mmap.PROT_READ) if os.fstat(fd).st_size else b''
    finally:
        os.close(fd)


def model_vec(q, W, dim):
    """Piece weights -> weighted sum of learned piece vectors, length 1."""
    v = [0.0] * dim
    for b, x in q.items():
        row = array.array('f')
        row.frombytes(W[b * dim * 4:(b + 1) * dim * 4])
        for k in range(dim):
            v[k] += x * row[k]
    nrm = math.sqrt(sum(t * t for t in v)) or 1.0
    return [t / nrm for t in v]


def sync_model(units, texts):
    """Aligns the model's pair vectors with the index: pairs that are gone (sealed ones
    included) are dropped, new pairs are computed from the learned piece vectors. Only
    train learns new connections. -> (added, removed)"""
    d = raw_dir()
    if not os.path.exists(os.path.join(d, 'model.json')):
        return 0, 0
    with open(os.path.join(d, 'model.json'), encoding='utf-8') as f:
        mj = json.load(f)
    old_units = mj['units']
    E_old, m_idf = array.array('f'), array.array('f')
    with open(os.path.join(d, 'E.f32'), 'rb') as f:
        E_old.frombytes(f.read())
    with open(os.path.join(d, 'm_idf.f32'), 'rb') as f:
        m_idf.frombytes(f.read())
    dim = mj.get('dim') or len(E_old) // max(1, len(old_units))
    W = _map(os.path.join(d, 'W.f32'))
    old = {(u[0], u[1]): k for k, u in enumerate(old_units)}
    E_new, added = array.array('f'), 0
    for unit, text in zip(units, texts):
        k = old.get((unit[0], unit[1]))
        if k is not None:
            E_new.extend(E_old[k * dim:(k + 1) * dim])
        else:
            q = {b: (1.0 + math.log(c)) * m_idf[b] for b, c in raw_counts(text).items()}
            E_new.extend(model_vec(q, W, dim))
            added += 1
    _tofile(os.path.join(d, 'E.f32'), E_new)
    with open(os.path.join(d, 'model.json.tmp'), 'w', encoding='utf-8') as f:
        json.dump({'trained': mj['trained'], 'dim': dim, 'units': units}, f, ensure_ascii=False)
    os.replace(os.path.join(d, 'model.json.tmp'), os.path.join(d, 'model.json'))
    return added, len(old_units) - (len(units) - added)


@contextlib.contextmanager
def model_lock():
    """Changes to the model files (train writing them, sync_model) run one at a time on this machine."""
    import fcntl
    d = raw_dir()
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, 'model.lock'), 'w') as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


class RawIndex:
    """Reads the raw-text index and the model files. load() returns None when there is no index."""

    @classmethod
    def load(cls):
        d = raw_dir()
        if not os.path.exists(os.path.join(d, 'units.json')):
            return None
        self = cls()
        with open(os.path.join(d, 'units.json'), encoding='utf-8') as f:
            meta = json.load(f)
        self.built, self.units = meta['built'], meta['units']
        self.idf, self.ptr = array.array('f'), array.array('q')
        with open(os.path.join(d, 'idf.f32'), 'rb') as f:
            self.idf.frombytes(f.read())
        with open(os.path.join(d, 'post_ptr.i64'), 'rb') as f:
            self.ptr.frombytes(f.read())
        self.post_u = _map(os.path.join(d, 'post_unit.i32'))
        self.post_w = _map(os.path.join(d, 'post_w.f32'))
        self.model = None
        if os.path.exists(os.path.join(d, 'model.json')):
            with open(os.path.join(d, 'model.json'), encoding='utf-8') as f:
                mj = json.load(f)
            m_idf, E = array.array('f'), array.array('f')
            with open(os.path.join(d, 'm_idf.f32'), 'rb') as f:
                m_idf.frombytes(f.read())
            with open(os.path.join(d, 'E.f32'), 'rb') as f:
                E.frombytes(f.read())
            dim = mj.get('dim') or len(E) // max(1, len(mj['units']))
            if len(E) == dim * len(mj['units']):   # skipped while sync_model is replacing the two files
                self.model = {'units': mj['units'], 'idf': m_idf, 'E': E, 'dim': dim,
                              'W': _map(os.path.join(d, 'W.f32'))}
        return self

    @staticmethod
    def flow(texts, idf):
        """[current prompt, previous exchange, the one before] -> piece weights mixed by FLOW_W."""
        q = {}
        for text, w in zip(texts, FLOW_W):
            if not text:
                continue
            v = {b: (1.0 + math.log(k)) * idf[b] for b, k in raw_counts(text).items()}
            nrm = math.sqrt(sum(x * x for x in v.values())) or 1.0
            for b, x in v.items():
                q[b] = q.get(b, 0.0) + x / nrm * w
        return q

    @staticmethod
    def _best(units, scores):
        """The closest pair of each card, cards in score order -> [(card, unit)]."""
        best = {}
        for u, s in enumerate(scores):
            card = units[u][0]
            if card not in best or s > best[card][0]:
                best[card] = (s, u)
        return [(c, units[u]) for c, (s, u) in sorted(best.items(), key=lambda kv: -kv[1][0])
                if s > 0]

    def rank_raw(self, texts):
        q = self.flow(texts, self.idf)
        scores = [0.0] * len(self.units)
        for b, x in q.items():
            a, z = self.ptr[b], self.ptr[b + 1]
            if a == z:
                continue
            us, ws = array.array('i'), array.array('f')
            us.frombytes(self.post_u[a * 4:z * 4])
            ws.frombytes(self.post_w[a * 4:z * 4])
            for u, w in zip(us, ws):
                scores[u] += x * w
        return self._best(self.units, scores)

    def rank_model(self, texts):
        """Ranking by the learned model. None when there are no model files."""
        if not self.model:
            return None
        m, dim = self.model, self.model['dim']
        v = model_vec(self.flow(texts, m['idf']), m['W'], dim)
        E = m['E']
        scores = [sum(map(operator.mul, v, E[u * dim:(u + 1) * dim])) for u in range(len(m['units']))]
        return self._best(m['units'], scores)


def recent_pairs(tpath, n=2, prompt=''):
    """The last n exchanges from the end of the session transcript, newest first, as
    'user turn\\nanswer'. The current prompt is left out."""
    if not tpath or not os.path.exists(tpath):
        return []
    with open(tpath, 'rb') as f:
        f.seek(max(0, os.path.getsize(tpath) - 2_000_000))
        raw = f.read().decode('utf-8', 'ignore')
    pairs = []
    for line in raw.splitlines():
        try:
            e = json.loads(line)
        except Exception:
            continue
        t, c = e.get('type'), (e.get('message') or {}).get('content')
        if t == 'user' and isinstance(c, str) and not e.get('isMeta') \
                and not is_injected(c):
            pairs.append([c, ''])
        elif t == 'assistant' and isinstance(c, list) and pairs:
            txt = ' '.join(b.get('text', '') for b in c if b.get('type') == 'text')
            if txt.strip() and len(pairs[-1][1]) < RAW_A_MAX:
                a = pairs[-1][1]
                pairs[-1][1] = (a + '\n' + txt if a else txt)[:RAW_A_MAX]
    if pairs and pairs[-1][0].strip() == prompt.strip():
        pairs.pop()
    return [u[:RAW_U_MAX] + '\n' + a for u, a in reversed(pairs[-n:])]


def use_ledger():
    return os.path.join(VAULT, '20_Wiki', 'recall_use.tsv')


def opened_cards(tool_texts):
    """Summary and thread files named in the turn's tool calls (Read paths, Bash
    commands) -> card names."""
    text = '\n'.join(tool_texts)
    cards = set(_OPEN_SUMMARY.findall(text))
    threads = set(_OPEN_THREAD.findall(text))
    if threads:
        by_thread = {os.path.splitext(os.path.basename(info.get('thread') or ''))[0]: name
                     for name, info in parse_summaries().items()}
        cards |= {by_thread[t] for t in threads if t in by_thread}
    return {c for c in cards if os.path.exists(os.path.join(SUMMARIES, c + '.md'))}


def record_use(session_id, prompt, shown, opened, turn=''):
    """Writes the turn's use to the use ledger: 'used' with the prompt for each shown card
    that was opened, 'missed' for each opened card that was not shown, and one 'turn' row
    with the number of cards shown per method.
    Called again in the same turn (same session, prompt, and turn), it writes only rows
    that are new. turn is the prompt's position in the transcript, so the same prompt
    asked again later counts as a new turn. The rows written for the last turn are kept
    per session in USE_TURN_DIR.
    Nothing is written while the vault is unlocked; the prompt may be sensitive."""
    if vault_open() or not (shown or opened) or is_injected(prompt):
        return
    shown_cards = {c for _, c in shown}
    q = re.sub(r'\s+', ' ', prompt).strip()[:300]
    counts = {}
    for tag, _ in shown:
        counts[tag] = counts.get(tag, 0) + 1
    rows = [('turn', '·'.join(f'{t or "PPR"}{n}' for t, n in counts.items()), '', '')]
    rows += [('used', tag, card, q) for tag, card in shown if card in opened]
    rows += [('missed', '', card, q) for card in sorted(opened - shown_cards)]
    key = f'{turn}\t{q}'
    done_path = os.path.join(USE_TURN_DIR, re.sub(r'[^\w-]', '', session_id) or '_')
    try:
        with open(done_path, encoding='utf-8') as f:
            lines = f.read().split('\n')
    except OSError:
        lines = []
    done = set(lines[1:]) if lines and lines[0] == key else set()
    rows = [row for row in rows if '\t'.join(row[:3]) not in done and not (done and row[0] == 'turn')]
    if not rows:
        return
    today = datetime.date.today().isoformat()
    with open(use_ledger(), 'a', encoding='utf-8') as f:
        for label, tag, card, text in rows:
            f.write(f'{today}\t{session_id[:8]}\t{label}\t{tag}\t{card}\t{text}\n')
    os.makedirs(USE_TURN_DIR, exist_ok=True)
    with open(done_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join([key, *sorted(done | {'\t'.join(row[:3]) for row in rows})]))


def raw_rebuild_if_stale(idx):
    """Starts one background build when there is no index (idx None) or a summary is newer than it."""
    lock = os.path.join(raw_dir(), 'building')
    if idx is not None and os.path.getmtime(SUMMARIES) <= idx.built:
        return
    if os.path.exists(lock) and time.time() - os.path.getmtime(lock) < RAW_REBUILD_LOCK_S:
        return
    os.makedirs(raw_dir(), exist_ok=True)
    open(lock, 'w').close()
    import subprocess
    subprocess.Popen([sys.executable, os.path.abspath(__file__), 'build'],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def unit_line(tag, g, unit):
    card, line, tref, snip = unit
    rec = g.recall.get(card)
    title = rec['title'] if rec else card
    return f'- [{tag}] {card[:10]} [[{card}|{title}]] — you said: "{snip}" → {tref}:{line}'


def hook_cards(g, seeds, cues, prompt, tpath):
    """Prompt-side summary candidates [(card, line)].

    With the raw-text index, each method in HOOK_SPLIT picks its share without overlap;
    the model slots go to [keyword] while there is no model. Without the index, or when
    reading it fails, the PPR top HOOK_TOP as whole ledger lines. Without the index, a
    background build starts.
    """
    ppr = prompt_cards(g, seeds, cues)
    plain = [(n, g.recall[n]['line']) for n in ppr]
    try:
        idx = RawIndex.load()
        raw_rebuild_if_stale(idx)
        if idx is None:
            return plain
        texts = [prompt] + recent_pairs(tpath, len(FLOW_W) - 1, prompt)
        ranked = {'keyword': [(n, '- [keyword] ' + g.recall[n]['line'][2:]) for n in ppr],
                  'raw': [(c, unit_line('raw', g, u)) for c, u in idx.rank_raw(texts)]}
        m = idx.rank_model(texts)
    except Exception as e:
        print(f'raw-text recall failed, keyword only: {type(e).__name__}: {e}', file=sys.stderr)
        return plain
    split = HOOK_SPLIT
    if m is None:
        spare = sum(k for t, k in HOOK_SPLIT if t == 'model')
        split = tuple((t, k + (spare if t == 'keyword' else 0)) for t, k in HOOK_SPLIT if t != 'model')
    else:
        ranked['model'] = [(c, unit_line('model', g, u)) for c, u in m]
    out, seen = [], set()
    for tag, k in split:
        got = 0
        for card, line in ranked[tag]:
            if got == k:
                break
            if card in seen:
                continue
            seen.add(card)
            out.append((card, line))
            got += 1
    return out


HOOK_TOP = 5          # prompt-side summary candidates (machine without the index)
HOOK_QUEUE_MAX = 2    # queued threads that overlap the prompt


def prompt_cards(g, seeds, cues):
    """Names of prompt-side summary candidates: the top HOOK_TOP by multi-cue boosted
    score that have a ledger line. The hook shows these, and precheck leaves them out."""
    if not seeds:
        return []
    scores = g.ppr(seeds)
    ranked = sorted(
        ((scores[i] * (1 + (cues.get(i, 0) - 1 if cues.get(i, 0) else 0)), n[2:])
         for i, n in enumerate(g.nodes) if n.startswith('P:')),
        reverse=True)[:HOOK_TOP]
    return [name for score, name in ranked if name in g.recall and score >= 1e-4]


def _occurs(tok, text):
    """Does a fired token occur in lowercased text? Latin tokens match at word starts:
    a ledger or alias token as a whole word with a short inflection, a full-text stem
    (marked '~') as a prefix. Other tokens match as substrings."""
    stem = tok.endswith('~')
    t = tok.rstrip('~')
    if not _latin(t):
        return t in text
    tail = '' if stem else _INFLECT + r'(?![a-z0-9])'
    return re.search(r'(?<![a-z0-9])' + re.escape(t) + tail, text) is not None


def queue_lines(fired):
    """Queued threads whose user turns contain a token the prompt fired (fired from
    match_seeds).

    They are not ingested yet, so the graph cannot rank them. Most shared tokens first,
    newest capture (mtime) first on ties, at most HOOK_QUEUE_MAX. Each line carries a
    slice of user turns. Sealed queue files (.age) are unreadable and skipped. When
    unlocked, the queue under open/ is included.
    """
    toks = {unicodedata.normalize('NFC', t).lower() for t in fired}
    if not toks:
        return []
    qdirs = [os.path.join(VAULT, '00_Inbox', '_queue')]
    if vault_open():
        qdirs.append(os.path.join(OPEN, '00_Inbox', '_queue'))
    qpaths = []
    for qd in qdirs:
        if os.path.isdir(qd):
            qpaths += [os.path.join(qd, f) for f in os.listdir(qd)
                       if f.endswith('__thread.md')]
    qpaths.sort(key=os.path.getmtime, reverse=True)
    hits = []
    for qp in qpaths:
        try:
            with open(qp, encoding='utf-8') as f:
                raw = f.read()
        except Exception:
            continue
        blocks = [u.strip() for u in re.findall(
            r'^\*\*User\*\*: (.*?)(?=\n\n---\n\n|\Z)', raw, re.S | re.M)]
        blocks = [u for u in blocks if u and not is_injected(u)]
        text = unicodedata.normalize('NFC', '\n'.join(blocks)).lower()
        n = sum(1 for t in toks if _occurs(t, text))
        if n:
            hits.append((n, qp, blocks))
    hits.sort(key=lambda h: -h[0])   # stable sort keeps newest first on ties
    lines = []
    for _, qp, blocks in hits[:HOOK_QUEUE_MAX]:
        utter = [u.split('\n', 1)[0].strip() for u in blocks]
        # 6 turns: first, last, and 4 random ones in order, so each prompt shows a different slice
        if len(utter) <= 6:
            picks = utter
        else:
            mid = sorted(random.sample(range(1, len(utter) - 1), 4))
            picks = [utter[0]] + [utter[i] for i in mid] + [utter[-1]]
        tail = ' — ' + ' · '.join(
            '"' + (u[:60] + '…' if len(u) > 60 else u) + '"' for u in picks) if picks else ''
        stem = os.path.basename(qp)[:-len('__thread.md')]
        lines.append(f'- (not ingested) {stem.replace("_", " ")}{tail} → {os.path.relpath(qp, VAULT)}')
    return lines


def cmd_hook(args):
    """UserPromptSubmit hook. Reads the prompt from stdin JSON and prints candidates.

    Optional parts (lock marker, sealed-session notice, queue lines, raw-text recall)
    swallow their own errors. A failure of the graph itself exits 1 with the error on stderr.
    """
    try:
        # Activity marker: touched every turn. Past the idle limit, start an auto-lock
        # (a second safety net next to the watcher process).
        try:
            idle_h = float(os.environ.get('VAULT_LOCK_IDLE_H', '3'))
            stale = (os.path.exists(MARKER)
                     and time.time() - os.path.getmtime(MARKER) > idle_h * 3600)
            orphan = os.path.isdir(OPEN) and not os.path.exists(MARKER)  # left over after reboot
            if stale or orphan:
                import subprocess
                subprocess.Popen(
                    [sys.executable,
                     os.path.join(VAULT, '99_Scripts', 'vault_lock.py'), 'lock'],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    start_new_session=True)
            elif os.path.exists(MARKER):
                os.utime(MARKER)   # activity
        except Exception:
            pass
        d = json.load(sys.stdin)
        prompt = d.get('prompt', '')
        if len(prompt.strip()) < 4 or is_injected(prompt):
            return
        lines = []
        sid = d.get('session_id', '')
        # Sealed sessions while locked: one-line notice. It disappears after unlock
        # restores them.
        try:
            if not vault_open():
                tdir = os.environ.get('VAULT_LOCK_TRANSCRIPTS') or os.path.expanduser(
                    '~/.claude/projects/' + VAULT.replace('/', '-').replace('_', '-'))
                sealed = sum(1 for f in os.listdir(tdir)
                             if f.endswith('.jsonl.age')) if os.path.isdir(tdir) else 0
                if sealed:
                    lines.append(f'- (locked) {sealed} sealed session(s). To resume, run '
                                 '`python3 99_Scripts/vault_lock.py unlock` in a separate terminal')
        except Exception:
            pass
        g = Graph()
        seeds, fired, cues = g.match_seeds(prompt, g.seed_dict())
        try:
            lines += queue_lines(fired)
        except Exception:
            pass
        cards = hook_cards(g, seeds, cues, prompt, d.get('transcript_path', ''))
        lines += [line for _, line in cards]
        if sid and cards:   # precheck and stopcheck read the cards shown in this session
            try:
                os.makedirs(SHOWN_DIR, exist_ok=True)
                with open(os.path.join(SHOWN_DIR, re.sub(r'[^\w-]', '', sid)), 'w', encoding='utf-8') as f:
                    f.write('\n'.join((re.match(r'- \[(\S+?)\]', line) or [None, ''])[1] + '\t' + n
                                      for n, line in cards))
            except Exception:
                pass
        if lines:
            tagged = any(line.startswith('- [') for _, line in cards)
            print('Recalled memories (injected automatically: conversations not yet ingested '
                  'that overlap this prompt, then past conversations it touches. Each line is a '
                  'memory that came to mind. '
                  + ('[keyword], [model] and [raw] name the method that brought the line up. ' if tagged else '')
                  + 'Keep them in the background; open the path or [[link]] when you need detail. '
                  'If you Read a summary, run recall_ppr.py learn):')
            print('\n'.join(lines))
    except Exception as e:
        print(f'recall failed: {type(e).__name__}: {e}', file=sys.stderr)
        raise SystemExit(1)


ANSWER_CHECK_MIN_LEN = 300       # the Stop hook checks answers at least this long
PRECHECK_CMD = 'recall_ppr.py precheck'
PRECHECK_SHOWN_MAX_AGE_S = 600   # a shown file not touched for this long is not from the current session
PRECHECK_MAX = 2                 # cards precheck prints at most
PRECHECK_SCORES = os.path.join(RECALL_CACHE, 'precheck_scores.txt')   # best score of each precheck run
PRECHECK_WINDOW = 300            # recent scores used for the quantile
PRECHECK_MIN_SAMPLES = 50        # with fewer scores, PRECHECK_DEFAULT
PRECHECK_DEFAULT = 0.041         # 90th percentile measured on the author's vault
PRECHECK_Q = 0.9


def precheck_threshold():
    """The PRECHECK_Q quantile of the last PRECHECK_WINDOW scores on this machine."""
    vals = []
    if os.path.exists(PRECHECK_SCORES):
        with open(PRECHECK_SCORES, encoding='utf-8') as f:
            for x in f.read().split():
                try:
                    vals.append(float(x))
                except ValueError:
                    continue
    vals = vals[-PRECHECK_WINDOW:]
    if len(vals) < PRECHECK_MIN_SAMPLES:
        return PRECHECK_DEFAULT
    vals.sort()
    return vals[min(len(vals) - 1, int(PRECHECK_Q * len(vals)))]


def reach_cards(g, text, exclude, thr):
    """Seeds the graph with text -> (best score of a card not in exclude, ledger lines of
    up to PRECHECK_MAX such cards scoring at least thr). (None, []) when nothing seeds."""
    seeds, _, _ = g.match_seeds(text, g.seed_dict())
    if not seeds:
        return None, []
    sc = g.ppr(seeds)
    cands = sorted(((sc[g.idx['P:' + name]], name) for name in g.recall
                    if 'P:' + name in g.idx and name not in exclude), reverse=True)
    best = cands[0][0] if cands else 0.0
    return best, [g.recall[name]['line'] for s, name in cands[:PRECHECK_MAX] if s >= thr]


def cmd_precheck(args):
    """Run with the gist of an answer before writing it. Prints the cards the gist
    reaches that the prompt side did not show (the newest shown file, if recent), when
    they score at least this machine's quantile. Each run adds its best score to the record."""
    shown = set()
    try:
        files = [os.path.join(SHOWN_DIR, f) for f in os.listdir(SHOWN_DIR)]
        if files:
            latest = max(files, key=os.path.getmtime)
            if time.time() - os.path.getmtime(latest) <= PRECHECK_SHOWN_MAX_AGE_S:
                with open(latest, encoding='utf-8') as f:
                    shown = {ln.split('\t', 1)[-1] for ln in f.read().splitlines() if ln.strip()}
    except OSError:
        pass
    thr = precheck_threshold()
    best, novel = reach_cards(Graph(), args.text, shown, thr)
    if best is not None:
        try:
            os.makedirs(os.path.dirname(PRECHECK_SCORES), exist_ok=True)
            with open(PRECHECK_SCORES, 'a', encoding='utf-8') as f:
                f.write('%.6f\n' % best)
        except OSError:
            pass
    if novel:
        print('Memories the answer touches. If anything conflicts with the answer, put it in the answer.')
        print('\n'.join(novel))
    else:
        print('No memory reached')


def cmd_stopcheck(args):
    """Stop hook (Claude Code).

    Writes the turn's use to the use ledger (cards shown by the hook vs. summary and
    thread files opened in the turn's tool calls). Then, if the last answer is at least
    ANSWER_CHECK_MIN_LEN characters and no tool call in the turn ran precheck, blocks
    once with a one-line request to run it. With stop_hook_active it only writes the
    ledger (loop guard). A failure exits 1 with the error on stderr.
    """
    try:
        d = json.load(sys.stdin)
        tpath = d.get('transcript_path', '')

        def read_turns():
            turns = []   # (type, text)
            with open(tpath, encoding='utf-8') as f:
                for line in f:
                    try:
                        e = json.loads(line)
                    except Exception:
                        continue
                    t = e.get('type')
                    c = (e.get('message') or {}).get('content')
                    if t == 'user' and isinstance(c, str) and not e.get('isMeta'):
                        turns.append(('user', c))   # isMeta: injected, e.g. image notices, subagent reports
                    elif t == 'assistant' and isinstance(c, list):
                        txt = ' '.join(b.get('text', '') for b in c if b.get('type') == 'text')
                        if txt.strip():
                            turns.append(('assistant', txt))
                        for b in c:   # files opened in the turn, for the use ledger
                            inp = (b.get('input') or {}) if b.get('type') == 'tool_use' else {}
                            tool = ' '.join(str(inp.get(k, '')) for k in ('file_path', 'command') if inp.get(k))
                            if tool:
                                turns.append(('tool', tool))
            return turns

        prompt, answer, prev = '', '', None
        for _ in range(6):   # the answer may be only partly flushed; wait until two reads agree
            turns = read_turns()
            last_u = max((i for i, (t, _) in enumerate(turns) if t == 'user'), default=-1)
            prompt = turns[last_u][1] if last_u >= 0 else ''
            answer = ' '.join(txt for t, txt in turns[last_u + 1:] if t == 'assistant')
            if answer and answer == prev:
                break
            prev = answer
            time.sleep(0.4)
        sid = d.get('session_id', '')
        shown_path = os.path.join(SHOWN_DIR, re.sub(r'[^\w-]', '', sid))
        shown = []
        if sid and os.path.exists(shown_path):
            with open(shown_path, encoding='utf-8') as f:
                shown = [tuple(ln.split('\t', 1)) if '\t' in ln else ('', ln)
                         for ln in f.read().splitlines() if ln.strip()]
        try:
            opened = opened_cards([txt for t, txt in turns[last_u + 1:] if t == 'tool'])
            record_use(sid, prompt, shown, opened, turn=str(last_u))
        except Exception as e:
            print(f'use ledger failed: {type(e).__name__}: {e}', file=sys.stderr)
        if d.get('stop_hook_active'):
            return
        last_text = next((txt for t, txt in reversed(turns[last_u + 1:]) if t == 'assistant'), '')
        ran = any(PRECHECK_CMD in txt for t, txt in turns[last_u + 1:] if t == 'tool')
        if len(last_text) >= ANSWER_CHECK_MIN_LEN and not ran:
            print(json.dumps({'decision': 'block', 'reason':
                'You did not run the check before answering. Run '
                '`python3 99_Scripts/recall_ppr.py precheck "<gist>"` with the gist of your answer, '
                'and add only what conflicts.'}, ensure_ascii=False))
    except Exception as e:
        print(f'recall failed: {type(e).__name__}: {e}', file=sys.stderr)
        raise SystemExit(1)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest='cmd', required=True)
    ps = sub.add_parser('stats')
    ps.add_argument('--no-degree-damp', action='store_true')
    ps.set_defaults(func=cmd_stats)
    pq = sub.add_parser('query')
    pq.add_argument('text')
    pq.add_argument('--k', type=int, default=8)
    pq.add_argument('--damping', type=float, default=0.5)
    pq.add_argument('--eps', type=float, default=EPS_UNIFORM)
    pq.add_argument('--no-degree-damp', action='store_true')
    pq.add_argument('--cue-boost', type=float, default=1.0)
    pq.add_argument('--debug', action='store_true')
    pq.set_defaults(func=cmd_query)
    pl = sub.add_parser('learn')
    pl.add_argument('text', help='the prompt or context of that turn')
    pl.add_argument('summaries', nargs='+', help='summary file names you actually Read')
    pl.set_defaults(func=cmd_learn)
    ph = sub.add_parser('hook')
    ph.set_defaults(func=cmd_hook)
    sub.add_parser('stopcheck').set_defaults(func=cmd_stopcheck)
    pp = sub.add_parser('precheck')
    pp.add_argument('text', help='the gist of the answer you are about to write')
    pp.set_defaults(func=cmd_precheck)
    sub.add_parser('build').set_defaults(func=cmd_build)
    sub.add_parser('train').set_defaults(func=cmd_train)
    args = ap.parse_args()
    args.func(args)


if __name__ == '__main__':
    main()
