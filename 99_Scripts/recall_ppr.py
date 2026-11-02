#!/usr/bin/env python3
"""Associative recall: Personalized PageRank (spreading activation) over the vault.

Usage:
  python3 recall_ppr.py hook                  # UserPromptSubmit hook (reads JSON on stdin)
  python3 recall_ppr.py query "text" [--k 8] [--damping 0.5] [--eps 0.05]
                        [--no-degree-damp] [--debug]
  python3 recall_ppr.py learn "text" <summary> [<summary> ...]
  python3 recall_ppr.py stats
  python3 recall_ppr.py compact

Standard library only, so the hook runs on any machine without setup. The graph is
not stored; it is rebuilt from the markdown files on every call. Seeds come from
recall ledger hooks, entity aliases, and the full text of every thread (cached in
FT_CACHE, keyed by file mtimes; the cache is a per-machine artifact).
"""
import argparse
import csv
import json
import datetime
import hashlib
import random
import glob
import math
import os
import re
import sys
import time
import unicodedata

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
HEBB_PRUNE = 0.01         # compact drops effective weights below this
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
        else:
            em = re.search(r'mentioned_entities:[ \t]*\n((?:[ \t]+-[ \t]+[^\n]*(?:\n|$))*)', fm)
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
            f.write('# nodeA\tnodeB\tdelta\tdate - append-only Hebbian ledger. '
                    'compact merges and prunes. Decay is applied when read.\n')
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


def cmd_hook(args):
    """UserPromptSubmit hook. Reads the prompt from stdin JSON and prints candidates.

    Optional parts (lock marker, sealed-session notice, queue listing) swallow
    their own errors. A failure of the graph itself exits 1 with the error on stderr.
    """
    try:
        import json
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
        prompt = json.load(sys.stdin).get('prompt', '')
        if len(prompt.strip()) < 4:
            return
        lines = []
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
        # Queued threads that are not ingested yet are candidates too. They have no
        # summary, so the graph cannot rank them; list them newest first by mtime
        # (file names do not keep time order within a day). Each line carries a few
        # user utterances, for the 10 newest only, to bound the injection size.
        # Sealed queue files (.age) are unreadable and skipped. When unlocked, the
        # queue under open/ is included.
        try:
            qdirs = [os.path.join(VAULT, '00_Inbox', '_queue')]
            if vault_open():
                qdirs.append(os.path.join(OPEN, '00_Inbox', '_queue'))
            qpaths = []
            for qd in qdirs:
                if os.path.isdir(qd):
                    qpaths += [os.path.join(qd, f) for f in os.listdir(qd)
                               if f.endswith('__thread.md')]
            qpaths.sort(key=os.path.getmtime, reverse=True)
            for qi, qp in enumerate(qpaths):
                f = os.path.basename(qp)
                qrel = os.path.relpath(qp, VAULT)
                stem = f[:-len('__thread.md')]
                tail = ''
                try:
                    utter = [] if qi >= 10 else [
                        u.strip() for u in re.findall(
                            r'\*\*User\*\*: (.+)',
                            open(qp, encoding='utf-8').read())
                        if not u.lstrip().startswith('<')]  # skip injected system blocks
                    if utter:
                        # 6 utterances: first, last, and 4 random ones in order, so each
                        # turn shows a different slice
                        if len(utter) <= 6:
                            picks = utter
                        else:
                            mid = sorted(random.sample(range(1, len(utter) - 1), 4))
                            picks = [utter[0]] + [utter[i] for i in mid] + [utter[-1]]
                        tail = ' — ' + ' · '.join(
                            '"' + (u[:60] + '…' if len(u) > 60 else u) + '"' for u in picks)
                except Exception:
                    pass
                lines.append(f'- (not ingested) {stem.replace("_", " ")}{tail} → {qrel}')
        except Exception:
            pass
        g = Graph()
        seeds, _, cues = g.match_seeds(prompt, g.seed_dict())
        if seeds:
            scores = g.ppr(seeds)
            ranked = sorted(
                ((scores[i] * (1 + (cues.get(i, 0) - 1 if cues.get(i, 0) else 0)), n[2:])
                 for i, n in enumerate(g.nodes) if n.startswith('P:')),
                reverse=True)[:10]
            for score, name in ranked:
                rec = g.recall.get(name)
                if not rec or score < 1e-4:
                    continue
                # The ledger line as written by INGEST: its hooks are the gist.
                lines.append(rec['line'])
        if lines:
            print('Recalled memories (injected automatically: recent conversations not yet '
                  'ingested, then past conversations this prompt touches). Each line is a memory '
                  'that came to mind. Keep them in the background; open the path or [[link]] '
                  'when you need detail. If you Read a summary, run recall_ppr.py learn:')
            print('\n'.join(lines))
    except Exception as e:
        print(f'recall failed: {type(e).__name__}: {e}', file=sys.stderr)
        raise SystemExit(1)


def cmd_compact(args):
    """Merge the append-only ledger to one row per pair (sum of effective weights,
    re-dated today), drop rows below HEBB_PRUNE, sort. Run it by hand now and then."""
    hebb = load_hebbian()
    kept = {k: v for k, v in hebb.items() if v >= HEBB_PRUNE}
    today = datetime.date.today().isoformat()
    with open(HEBBIAN, 'w', encoding='utf-8') as f:
        f.write('# nodeA\tnodeB\tdelta\tdate - append-only Hebbian ledger. '
                'compact merges and prunes. Decay is applied when read.\n')
        for (a, b), v in sorted(kept.items()):
            f.write(f'{a}\t{b}\t{v:.4f}\t{today}\n')
    print(f'compact: {len(hebb)} pairs -> {len(kept)} pairs (pruned {len(hebb) - len(kept)})')


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
    pc = sub.add_parser('compact')
    pc.set_defaults(func=cmd_compact)
    args = ap.parse_args()
    args.func(args)


if __name__ == '__main__':
    main()
