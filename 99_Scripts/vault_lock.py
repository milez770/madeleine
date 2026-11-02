#!/usr/bin/env python3
"""Open and close the sensitive container — age asymmetric keys.

The public key (99_Config/vault_key.pub) is plain text — sealing works any time
without a password. The private key (99_Config/vault_key.age) is locked with a
passphrase; only unlock uses it. The container (90_Vaulted/store.tar.age) holds
files under their original relative paths.
Open state = 90_Vaulted/open/ exists + a /tmp marker. The per-turn hook touches
the marker (activity); after IDLE_H idle hours the watcher locks automatically.

Usage (in a separate terminal, not the chat — a password typed in chat is
saved by session capture):
  vault_lock.py init                 create the key pair (once; asks for a passphrase)
  vault_lock.py unlock [-i keyfile]  open the container + start the watcher
  vault_lock.py lock                 reseal + delete open/ + capture, then seal transcripts
  vault_lock.py add <file...>        move files in while open (a summary brings its thread and ledger line)
  vault_lock.py seal <file...>       seal with the public key (works while locked — for queue files)
  vault_lock.py status / watch
"""
import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time

VAULT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PUB = os.path.join(VAULT, '99_Config', 'vault_key.pub')
KEY = os.path.join(VAULT, '99_Config', 'vault_key.age')
STORE = os.path.join(VAULT, '90_Vaulted', 'store.tar.age')
OPEN = os.path.join(VAULT, '90_Vaulted', 'open')
# Fixed to /tmp — TMPDIR can differ per process, so hook, terminal and watcher would disagree.
# /tmp is cleared on reboot = reboot means locked (a leftover open/ is locked by the hook).
MARKER = '/tmp/.vault_open_' + hashlib.md5(VAULT.encode()).hexdigest()[:8]
RECALL = os.path.join(VAULT, '20_Wiki', 'recall.md')
RECALL_COLD = os.path.join(VAULT, '20_Wiki', 'recall_cold.md')
RECALL_VAULTED = os.path.join('20_Wiki', 'recall_vaulted.md')   # relative path inside open/
VAULTED_IDS = os.path.join(VAULT, '90_Vaulted', 'vaulted_session_ids.txt')
SEAL_LIVE_GUARD = 600   # transcripts modified within this many seconds are not sealed (live session)
FT_CACHE = os.path.join(VAULT, '20_Wiki', '.recall_fulltext_cache.pkl')
RAW_INDEX = os.path.join(VAULT, '20_Wiki', '.recall_raw')
TRANSCRIPTS = os.environ.get('VAULT_LOCK_TRANSCRIPTS') or os.path.expanduser(
    '~/.claude/projects/' + VAULT.replace('/', '-').replace('_', '-'))
IDLE_H = float(os.environ.get('VAULT_LOCK_IDLE_H', '3'))


def die(msg):
    print(msg, file=sys.stderr)
    sys.exit(1)


def recipient():
    if not os.path.exists(PUB):
        die('No public key — run init first')
    return open(PUB).read().strip()


def is_open():
    return os.path.isdir(OPEN) and os.path.exists(MARKER)


def cmd_init(args):
    if os.path.exists(PUB) or os.path.exists(KEY):
        die('Keys already exist — to replace them, delete vault_key.pub/.age yourself and run init again')
    raw = subprocess.run(['age-keygen'], capture_output=True, text=True, check=True).stdout
    pub = re.search(r'public key: (age1\S+)', raw).group(1)
    secret = re.search(r'(AGE-SECRET-KEY-1\S+)', raw).group(1)
    print('┌─ Paper backup (shown only this once)')
    print('│ The line below is the master key. Write it on paper and keep it physically (wallet, safe).')
    print('│ If you forget the passphrase, unlock -i <file containing this line> still opens the container.')
    print('│ Do not put it in email or cloud notes — that account would then hold the master key.')
    print('│')
    print(f'│   {secret}')
    print('│')
    input('└─ Press Enter when written down → then set the passphrase (for daily unlock): ')
    enc = subprocess.run(['age', '-p', '-o', KEY], input=raw, text=True)
    if enc.returncode != 0:
        die('Failed to encrypt the private key')
    with open(PUB, 'w') as f:
        f.write(pub + '\n')
    os.makedirs(os.path.dirname(STORE), exist_ok=True)
    print(f'Keys created. Public key: {PUB}\nCommit both to git (the private key is passphrase-locked).')


def _identity_file(args):
    """Write the decrypted private key to a 0600 temp file. The caller deletes it after use."""
    if getattr(args, 'identity', None):
        return args.identity, False
    if not os.path.exists(KEY):
        die('No private key — run init first')
    fd, tmp = tempfile.mkstemp(prefix='.vk_')
    os.close(fd)
    os.chmod(tmp, 0o600)
    r = subprocess.run(['age', '-d', '-o', tmp, KEY])
    if r.returncode != 0:
        os.remove(tmp)
        die('Decryption failed (wrong passphrase?)')
    return tmp, True


def cmd_unlock(args):
    if is_open():
        print('Already open')
        return
    ident, temp = _identity_file(args)
    try:
        os.makedirs(OPEN, exist_ok=True)
        if os.path.exists(STORE):
            tar = subprocess.run(['age', '-d', '-i', ident, STORE], capture_output=True)
            if tar.returncode != 0:
                die('Failed to decrypt the container')
            with tempfile.TemporaryFile() as tf:
                tf.write(tar.stdout)
                tf.seek(0)
                with tarfile.open(fileobj=tf) as t:
                    try:
                        t.extractall(OPEN, filter='data')
                    except TypeError:   # filter argument needs 3.12+
                        t.extractall(OPEN)
        # Open sealed queue files now — the only moment the private key is available.
        # Plain text goes only under open/ (absorbed into the container on the next
        # lock), and the original .age file is deleted.
        qdir = os.path.join(VAULT, '00_Inbox', '_queue')
        if os.path.isdir(qdir):
            for f in os.listdir(qdir):
                if not f.endswith('.age'):
                    continue
                dst = os.path.join(OPEN, '00_Inbox', '_queue', f[:-len('.age')])
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                r = subprocess.run(['age', '-d', '-i', ident, '-o', dst,
                                    os.path.join(qdir, f)])
                if r.returncode == 0:
                    os.remove(os.path.join(qdir, f))
        # Restore sealed transcripts so --resume works again
        if os.path.isdir(TRANSCRIPTS):
            for f in os.listdir(TRANSCRIPTS):
                if not f.endswith('.jsonl.age'):
                    continue
                src = os.path.join(TRANSCRIPTS, f)
                dst = src[:-len('.age')]
                if os.path.exists(dst):   # a live plain file wins — keep the sealed copy
                    continue
                if subprocess.run(['age', '-d', '-i', ident, '-o', dst,
                                   src]).returncode == 0:
                    os.remove(src)
    finally:
        if temp:
            os.remove(ident)
    with open(MARKER, 'w') as f:
        f.write(str(time.time()))   # content = unlock time (sealing range), mtime = last activity
    subprocess.Popen([sys.executable, os.path.abspath(__file__), 'watch'],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
    print(f'Open → {OPEN}  (locks automatically after {IDLE_H}h idle)')


def _seal_transcripts(since):
    """Only if capture succeeded: seal (.age) this vault's transcripts that overlap
    the open period, and delete the plain files. They are restored on the next
    unlock so --resume still works — sealing instead of deleting keeps sessions
    that are continued days later."""
    cap = subprocess.run([sys.executable,
                          os.path.join(VAULT, '99_Scripts', 'session_to_thread.py'),
                          '--auto'], capture_output=True)
    if cap.returncode != 0:
        print('Session capture failed — skipped sealing transcripts (to avoid losing conversations)')
        return
    rec = recipient()
    vaulted = set(open(VAULTED_IDS, encoding='utf-8').read().split()) \
        if os.path.exists(VAULTED_IDS) else set()
    n = 0
    if os.path.isdir(TRANSCRIPTS):
        now = time.time()
        for f in os.listdir(TRANSCRIPTS):
            p = os.path.join(TRANSCRIPTS, f)
            if not f.endswith('.jsonl'):
                continue
            mt = os.path.getmtime(p)
            if now - mt < SEAL_LIVE_GUARD:
                continue   # live session — sealed on the next lock
            if mt >= since or f[:-len('.jsonl')] in vaulted:
                if subprocess.run(['age', '-r', rec, '-o', p + '.age', p]).returncode == 0:
                    os.remove(p)
                    n += 1
    print(f'Sealed {n} transcript(s) (restored on unlock)')


def cmd_lock(args):
    since = 0.0
    if os.path.exists(MARKER):
        try:
            since = float(open(MARKER).read().strip())
        except ValueError:
            pass
        os.remove(MARKER)
    if os.path.isdir(OPEN):
        if os.listdir(OPEN):
            buf = tempfile.TemporaryFile()
            with tarfile.open(fileobj=buf, mode='w') as t:
                t.add(OPEN, arcname='.')
            buf.seek(0)
            tmp = STORE + '.tmp'
            r = subprocess.run(['age', '-r', recipient(), '-o', tmp], stdin=buf)
            buf.close()
            if r.returncode != 0:
                die('Reseal failed — keeping open/')
            os.replace(tmp, STORE)
        shutil.rmtree(OPEN)
    if os.path.exists(FT_CACHE):
        os.remove(FT_CACHE)   # also remove tokens from open/ left in the cache
    if since:
        _seal_transcripts(since)
    print('Locked')


def cmd_watch(args):
    while True:
        time.sleep(300)
        if not os.path.exists(MARKER):
            return
        if time.time() - os.path.getmtime(MARKER) > IDLE_H * 3600:
            cmd_lock(args)
            return


def _register_sid(text):
    """Session id ledger — stops sealed sessions from being captured again (UUIDs only, no content)"""
    sm = re.search(r'(?m)^(?:claude_)?session_id:\s*(\S+)', text)
    if not sm:
        return
    sid = sm.group(1).strip('"')
    known = (open(VAULTED_IDS, encoding='utf-8').read().split()
             if os.path.exists(VAULTED_IDS) else [])
    if sid not in known:
        with open(VAULTED_IDS, 'a', encoding='utf-8') as f:
            f.write(sid + '\n')


def cmd_add(args):
    """Move files into open/. For a summary, its thread_ref thread and its recall
    ledger line move too. They are sealed into the container on lock."""
    if not is_open():
        die('Locked — run unlock first')
    moved = []
    for path in args.files:
        src = os.path.abspath(path)
        if not src.startswith(VAULT + os.sep) or not os.path.exists(src):
            print(f'(ignored: {path} — outside the vault or missing)')
            continue
        rel = os.path.relpath(src, VAULT)
        text = open(src, encoding='utf-8').read() if src.endswith('.md') else ''
        dst = os.path.join(OPEN, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.move(src, dst)
        moved.append(rel)
        _register_sid(text)
        m = re.search(r'^thread_ref:\s*(\S+)', text, re.M)
        if m and os.path.exists(os.path.join(VAULT, m.group(1))):
            args.files.append(os.path.join(VAULT, m.group(1)))
        if '/summaries/' in rel:
            name = os.path.splitext(os.path.basename(rel))[0]
            for ledger in (RECALL, RECALL_COLD):
                lines = open(ledger, encoding='utf-8').readlines()
                keep = [l for l in lines if f'[[{name}' not in l]
                if len(keep) != len(lines):
                    with open(ledger, 'w', encoding='utf-8') as f:
                        f.writelines(keep)
                    vaulted = os.path.join(OPEN, RECALL_VAULTED)
                    os.makedirs(os.path.dirname(vaulted), exist_ok=True)
                    with open(vaulted, 'a', encoding='utf-8') as f:
                        f.writelines(l for l in lines if f'[[{name}' in l)
    if os.path.exists(FT_CACHE):
        os.remove(FT_CACHE)
    if moved and os.path.isdir(RAW_INDEX):
        # The raw-text index and model pairs hold snippets of moved summaries; a rebuild drops them
        subprocess.run([sys.executable, os.path.join(VAULT, '99_Scripts', 'recall_ppr.py'), 'build'])
    for rel in moved:
        print(f'moved in: {rel}')
    if moved:
        print('Sealed on lock. Copies in git history must be removed separately.')


def cmd_seal(args):
    """Seal with the public key — works while locked (for freshly captured queue files). Deletes the original."""
    rec = recipient()
    for path in args.files:
        if not os.path.exists(path):
            print(f'(ignored: {path})')
            continue
        text = open(path, encoding='utf-8').read() if path.endswith('.md') else ''
        r = subprocess.run(['age', '-r', rec, '-o', path + '.age', path])
        if r.returncode == 0:
            _register_sid(text)
            os.remove(path)
            print(f'sealed: {path}.age')


def cmd_status(args):
    state = 'open' if is_open() else 'locked'
    idle = ''
    if os.path.exists(MARKER):
        idle = f' · idle {(time.time() - os.path.getmtime(MARKER)) / 60:.0f} min'
    store = f'{os.path.getsize(STORE):,}B' if os.path.exists(STORE) else 'none'
    print(f'{state}{idle} · container {store} · key {"present" if os.path.exists(PUB) else "none"}')


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('init').set_defaults(func=cmd_init)
    pu = sub.add_parser('unlock')
    pu.add_argument('-i', '--identity', help='plain private key file (for tests or paper-backup recovery)')
    pu.set_defaults(func=cmd_unlock)
    sub.add_parser('lock').set_defaults(func=cmd_lock)
    sub.add_parser('watch').set_defaults(func=cmd_watch)
    pa = sub.add_parser('add')
    pa.add_argument('files', nargs='+')
    pa.set_defaults(func=cmd_add)
    ps = sub.add_parser('seal')
    ps.add_argument('files', nargs='+')
    ps.set_defaults(func=cmd_seal)
    sub.add_parser('status').set_defaults(func=cmd_status)
    args = ap.parse_args()
    args.func(args)


if __name__ == '__main__':
    main()
