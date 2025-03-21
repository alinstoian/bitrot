#!/usr/bin/env python3

# Copyright (C) 2013 by Łukasz Langa

# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:

# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.

# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.

from __future__ import annotations

import argparse
import atexit
import datetime
import errno
import hashlib
import os
import shutil
import sqlite3
import stat
import sys
import tempfile
import time
import unicodedata

from concurrent.futures import ProcessPoolExecutor, as_completed
from multiprocessing import freeze_support
from importlib.metadata import version, PackageNotFoundError


DEFAULT_CHUNK_SIZE = 16384  # block size in HFS+; 4X the block size in ext4
DOT_THRESHOLD = 200
IGNORED_FILE_SYSTEM_ERRORS = {errno.ENOENT, errno.EACCES}
FSENCODING = sys.getfilesystemencoding()
try:
    VERSION = version("bitrot")
except PackageNotFoundError:
    VERSION = "1.0.1"


def normalize_path(path):
    path_uni = path.decode(FSENCODING)
    if FSENCODING in ('utf-8', 'UTF-8'):
        return unicodedata.normalize('NFKD', path_uni)

    return path_uni


def sha1(path, chunk_size):
    digest = hashlib.sha1()
    with open(path, 'rb') as f:
        d = f.read(chunk_size)
        while d:
            digest.update(d)
            d = f.read(chunk_size)
    return digest.hexdigest()


def ts():
    return datetime.datetime.now(datetime.UTC).strftime('%Y-%m-%d %H:%M:%S%z')


def get_sqlite3_cursor(path, copy=False):
    path = path.decode(FSENCODING)
    if copy:
        if not os.path.exists(path):
            raise ValueError("error: bitrot database at {} does not exist."
                             "".format(path))
        db_copy = tempfile.NamedTemporaryFile(prefix='bitrot_', suffix='.db',
                                              delete=False)
        with open(path, 'rb') as db_orig:
            try:
                shutil.copyfileobj(db_orig, db_copy)
            finally:
                db_copy.close()
        path = db_copy.name
        atexit.register(os.unlink, path)
    conn = sqlite3.connect(path)
    atexit.register(conn.close)
    cur = conn.cursor()
    tables = set(t for t, in cur.execute('SELECT name FROM sqlite_master'))
    if 'bitrot' not in tables:
        cur.execute('CREATE TABLE bitrot (path TEXT PRIMARY KEY, '
                    'mtime INTEGER, hash TEXT, timestamp TEXT)')
    if 'bitrot_hash_idx' not in tables:
        cur.execute('CREATE INDEX bitrot_hash_idx ON bitrot (hash)')
    atexit.register(conn.commit)
    return conn


def list_existing_paths(directory, expected=(), ignored=(), follow_links=False):
    """list_existing_paths(b'/dir') -> ([path1, path2, ...], total_size)

    Returns a tuple with a set of existing files in `directory` and its subdirectories
    and their `total_size`. If directory was a bytes object, so will be the returned
    paths.

    Doesn't add entries listed in `ignored`.  Doesn't add symlinks if
    `follow_links` is False (the default).  All entries present in `expected`
    must be files (can't be directories or symlinks).
    """
    paths = set()
    total_size = 0
    for path, _, files in os.walk(directory):
        for f in files:
            p = os.path.join(path, f)
            try:
                p_uni = p.decode(FSENCODING)
            except UnicodeDecodeError:
                binary_stderr = getattr(sys.stderr, 'buffer', sys.stderr)
                binary_stderr.write(b"warning: cannot decode file name: ")
                binary_stderr.write(p)
                binary_stderr.write(b"\n")
                continue

            try:
                if follow_links or p_uni in expected:
                    st = os.stat(p)
                else:
                    st = os.lstat(p)
            except OSError as ex:
                if ex.errno not in IGNORED_FILE_SYSTEM_ERRORS:
                    raise
            else:
                if not stat.S_ISREG(st.st_mode) or p in ignored:
                    continue
                paths.add(p)
                total_size += st.st_size
    return paths, total_size


def compute_one(path, chunk_size):
    """Return a tuple with (unicode path, size, mtime, sha1). Takes a binary path."""
    p_uni = normalize_path(path)
    try:
        st = os.stat(path)
    except OSError as ex:
        if ex.errno in IGNORED_FILE_SYSTEM_ERRORS:
            # The file disappeared between listing existing paths and
            # this run or is (temporarily?) locked with different
            # permissions. We'll just skip it for now.
            print(
                '\rwarning: `{}` is currently unavailable for '
                'reading: {}'.format(
                    p_uni, ex,
                ),
                file=sys.stderr,
            )
            raise BitrotException

        raise   # Not expected? https://github.com/ambv/bitrot/issues/

    try:
        new_sha1 = sha1(path, chunk_size)
    except (IOError, OSError) as e:
        print(
            '\rwarning: cannot compute hash of {} [{}]'.format(
                p_uni, errno.errorcode[e.args[0]],
            ),
            file=sys.stderr,
        )
        raise BitrotException

    return p_uni, st.st_size, int(st.st_mtime), new_sha1


class BitrotException(Exception):
    pass


class Bitrot(object):
    def __init__(
        self, verbosity=1, test=False, follow_links=False, commit_interval=300,
        chunk_size=DEFAULT_CHUNK_SIZE, workers=os.cpu_count(),
    ):
        self.verbosity = verbosity
        self.test = test
        self.follow_links = follow_links
        self.commit_interval = commit_interval
        self.chunk_size = chunk_size
        self._last_reported_size = ''
        self._last_commit_ts = 0
        self.pool = ProcessPoolExecutor(max_workers=workers)

    def maybe_commit(self, conn):
        if time.time() < self._last_commit_ts + self.commit_interval:
            # no time for commit yet!
            return

        conn.commit()
        self._last_commit_ts = time.time()

    def run(self):
        check_sha512_integrity(verbosity=self.verbosity)

        bitrot_db = get_path()
        bitrot_sha512 = get_path(ext=b'sha512')
        try:
            conn = get_sqlite3_cursor(bitrot_db, copy=self.test)
        except ValueError:
            raise BitrotException(
                2,
                'No database exists so cannot test. Run the tool once first.',
            )

        cur = conn.cursor()
        new_paths = []
        updated_paths = []
        renamed_paths = []
        errors = []
        current_size = 0
        missing_paths = self.select_all_paths(cur)
        hashes = self.select_all_hashes(cur)
        paths, total_size = list_existing_paths(
            b'.', expected=missing_paths, ignored={bitrot_db, bitrot_sha512},
            follow_links=self.follow_links,
        )
        paths_uni = set(normalize_path(p) for p in paths)
        futures = [self.pool.submit(compute_one, p, self.chunk_size) for p in paths]

        for future in as_completed(futures):
            try:
                p_uni, new_size, new_mtime, new_sha1 = future.result()
            except BitrotException:
                continue

            current_size += new_size
            if self.verbosity:
                self.report_progress(current_size, total_size)

            if p_uni not in missing_paths:
                # We are not expecting this path, it wasn't in the database yet.
                # It's either new or a rename. Let's handle that.
                stored_path = self.handle_unknown_path(
                    cur, p_uni, new_mtime, new_sha1, paths_uni, hashes
                )
                self.maybe_commit(conn)
                if p_uni == stored_path:
                    new_paths.append(p_uni)
                    missing_paths.discard(p_uni)
                else:
                    renamed_paths.append((stored_path, p_uni))
                    missing_paths.discard(stored_path)
                continue

            # At this point we know we're seeing an expected file.
            missing_paths.discard(p_uni)
            cur.execute('SELECT mtime, hash, timestamp FROM bitrot WHERE path=?',
                        (p_uni,))
            row = cur.fetchone()
            if not row:
                print(
                    '\rwarning: path disappeared from the database while running:',
                    p_uni,
                    file=sys.stderr,
                )
                continue

            stored_mtime, stored_sha1, stored_ts = row
            if int(stored_mtime) != new_mtime:
                updated_paths.append(p_uni)
                cur.execute('UPDATE bitrot SET mtime=?, hash=?, timestamp=? '
                            'WHERE path=?',
                            (new_mtime, new_sha1, ts(), p_uni))
                self.maybe_commit(conn)
                continue

            if stored_sha1 != new_sha1:
                errors.append(p_uni)
                print(
                    '\rerror: SHA1 mismatch for {}: expected {}, got {}.'
                    ' Last good hash checked on {}.'.format(
                        p_uni, stored_sha1, new_sha1, stored_ts
                    ),
                    file=sys.stderr,
                )

        for path in missing_paths:
            cur.execute('DELETE FROM bitrot WHERE path=?', (path,))

        conn.commit()

        if not self.test:
            cur.execute('vacuum')

        if self.verbosity:
            cur.execute('SELECT COUNT(path) FROM bitrot')
            all_count = cur.fetchone()[0]
            self.report_done(
                total_size,
                all_count,
                len(errors),
                new_paths,
                updated_paths,
                renamed_paths,
                missing_paths,
            )

        update_sha512_integrity(verbosity=self.verbosity)

        if errors:
            raise BitrotException(
                1, 'There were {} errors found.'.format(len(errors)), errors,
            )

    def select_all_paths(self, cur):
        """Return a set of all distinct paths in the bitrot database.

        The paths are Unicode and are normalized if FSENCODING was UTF-8.
        """
        result = set()
        cur.execute('SELECT path FROM bitrot')
        row = cur.fetchone()
        while row:
            result.add(row[0])
            row = cur.fetchone()
        return result

    def select_all_hashes(self, cur):
        """Return a dict where keys are hashes and values are sets of paths.

        The paths are Unicode and are normalized if FSENCODING was UTF-8.
        """
        result = {}
        cur.execute('SELECT hash, path FROM bitrot')
        row = cur.fetchone()
        while row:
            rhash, rpath = row
            result.setdefault(rhash, set()).add(rpath)
            row = cur.fetchone()
        return result

    def report_progress(self, current_size, total_size):
        size_fmt = '\r{:>6.1%}'.format(current_size/(total_size or 1))
        if size_fmt == self._last_reported_size:
            return

        sys.stdout.write(size_fmt)
        sys.stdout.flush()
        self._last_reported_size = size_fmt

    def report_done(
        self, total_size, all_count, error_count, new_paths, updated_paths,
        renamed_paths, missing_paths):
        """Print a report on what happened.  All paths should be Unicode here."""
        print('\rFinished. {:.2f} MiB of data read. {} errors found.'
            ''.format(total_size/1024/1024, error_count))
        if self.verbosity == 1:
            print(
                '{} entries in the database, {} new, {} updated, '
                '{} renamed, {} missing.'.format(
                    all_count, len(new_paths), len(updated_paths),
                    len(renamed_paths), len(missing_paths),
                ),
            )
        elif self.verbosity > 1:
            print('{} entries in the database.'.format(all_count), end=' ')
            if new_paths:
                print('{} entries new:'.format(len(new_paths)))
                new_paths.sort()
                for path in new_paths:
                    print(' ', path)
            if updated_paths:
                print('{} entries updated:'.format(len(updated_paths)))
                updated_paths.sort()
                for path in updated_paths:
                    print(' ', path)
            if renamed_paths:
                print('{} entries renamed:'.format(len(renamed_paths)))
                renamed_paths.sort()
                for path in renamed_paths:
                    print(
                        ' from',
                        path[0],
                        'to',
                        path[1],
                    )
            if missing_paths:
                print('{} entries missing:'.format(len(missing_paths)))
                missing_paths = sorted(missing_paths)
                for path in missing_paths:
                    print(' ', path)
            if not any((new_paths, updated_paths, missing_paths)):
                print()
        if self.test and self.verbosity:
            print('warning: database file not updated on disk (test mode).')

    def handle_unknown_path(self, cur, new_path, new_mtime, new_sha1, paths_uni, hashes):
        """Either add a new entry to the database or update the existing entry
        on rename.

        `cur` is the database cursor. `new_path` is the new Unicode path.
        `paths_uni` are Unicode paths seen on disk during this run of Bitrot.
        `hashes` is a dictionary selected from the database, keys are hashes, values
        are sets of Unicode paths that are stored in the DB under the given hash.

        Returns `new_path` if the entry was indeed new or the `old_path` (e.g.
        outdated path stored in the database for this hash) if there was a rename.
        """

        for old_path in hashes.get(new_sha1, ()):
            if old_path not in paths_uni:
                # File of the same hash used to exist but no longer does.
                # Let's treat `new_path` as a renamed version of that `old_path`.
                cur.execute(
                    'UPDATE bitrot SET mtime=?, path=?, timestamp=? WHERE path=?',
                    (new_mtime, new_path, ts(), old_path),
                )
                return old_path

        else:
            # Either we haven't found `new_sha1` at all in the database, or all
            # currently stored paths for this hash still point to existing files.
            # Let's insert a new entry for what appears to be a new file.
            cur.execute(
                'INSERT INTO bitrot VALUES (?, ?, ?, ?)',
                (new_path, new_mtime, new_sha1, ts()),
            )
            return new_path

def get_path(directory=b'.', ext=b'db'):
    """Compose the path to the selected bitrot file."""
    return os.path.join(directory, b'.bitrot.' + ext)


def stable_sum(bitrot_db=None):
    """Calculates a stable SHA512 of all entries in the database.

    Useful for comparing if two directories hold the same data, as it ignores
    timing information."""
    if bitrot_db is None:
        bitrot_db = get_path()
    digest = hashlib.sha512()
    conn = get_sqlite3_cursor(bitrot_db)
    cur = conn.cursor()
    cur.execute('SELECT hash FROM bitrot ORDER BY path')
    row = cur.fetchone()
    while row:
        digest.update(row[0].encode('ascii'))
        row = cur.fetchone()
    return digest.hexdigest()


def check_sha512_integrity(verbosity=1):
    sha512_path = get_path(ext=b'sha512')
    if not os.path.exists(sha512_path):
        return

    if verbosity:
        print('Checking bitrot.db integrity... ', end='')
        sys.stdout.flush()
    with open(sha512_path, 'rb') as f:
        old_sha512 = f.read().strip()
    bitrot_db = get_path()
    digest = hashlib.sha512()
    with open(bitrot_db, 'rb') as f:
        digest.update(f.read())
    new_sha512 = digest.hexdigest().encode('ascii')
    if new_sha512 != old_sha512:
        if verbosity:
            if len(old_sha512) == 128:
                print(
                    "error: SHA512 of the file is different, bitrot.db might "
                    "be corrupt.",
                )
            else:
                print(
                    "error: SHA512 of the file is different but bitrot.sha512 "
                    "has a suspicious length. It might be corrupt.",
                )
            print(
                "If you'd like to continue anyway, delete the .bitrot.sha512 "
                "file and try again.",
                file=sys.stderr,
            )
        raise BitrotException(
            3, 'bitrot.db integrity check failed, cannot continue.',
        )

    if verbosity:
        print('ok.')

def update_sha512_integrity(verbosity=1):
    old_sha512 = 0
    sha512_path = get_path(ext=b'sha512')
    if os.path.exists(sha512_path):
        with open(sha512_path, 'rb') as f:
            old_sha512 = f.read().strip()
    bitrot_db = get_path()
    digest = hashlib.sha512()
    with open(bitrot_db, 'rb') as f:
        digest.update(f.read())
    new_sha512 = digest.hexdigest().encode('ascii')
    if new_sha512 != old_sha512:
        if verbosity:
            print('Updating bitrot.sha512... ', end='')
            sys.stdout.flush()
        with open(sha512_path, 'wb') as f:
            f.write(new_sha512)
        if verbosity:
            print('done.')

def run_from_command_line():
    global FSENCODING

    freeze_support()

    parser = argparse.ArgumentParser(prog='bitrot')
    parser.add_argument(
        '-l', '--follow-links', action='store_true',
        help='follow symbolic links and store target files\' hashes. Once '
             'a path is present in the database, it will be checked against '
             'changes in content even if it becomes a symbolic link. In '
             'other words, if you run `bitrot -l`, on subsequent runs '
             'symbolic links registered during the first run will be '
             'properly followed and checked even if you run without `-l`.')
    parser.add_argument(
        '-q', '--quiet', action='store_true',
        help='don\'t print anything besides checksum errors')
    parser.add_argument(
        '-s', '--sum', action='store_true',
        help='using only the data already gathered, return a SHA-512 sum '
             'of hashes of all the entries in the database. No timestamps '
             'are used in calculation.')
    parser.add_argument(
        '-v', '--verbose', action='store_true',
        help='list new, updated and missing entries')
    parser.add_argument(
        '-t', '--test', action='store_true',
        help='just test against an existing database, don\'t update anything')
    parser.add_argument(
        '--version', action='version',
        version=f"%(prog)s {VERSION}")
    parser.add_argument(
        '--commit-interval', type=float, default=300,
        help='min time in seconds between commits '
             '(0 commits on every operation)')
    parser.add_argument(
        '-w', '--workers', type=int, default=os.cpu_count(),
        help='run this many workers (use -w1 for slow magnetic disks)')
    parser.add_argument(
        '--chunk-size', type=int, default=DEFAULT_CHUNK_SIZE,
        help='read files this many bytes at a time')
    parser.add_argument(
        '--fsencoding', default='',
        help='override the codec to decode filenames, otherwise taken from '
             'the LANG environment variables')
    args = parser.parse_args()
    if args.sum:
        try:
            print(stable_sum())
        except RuntimeError as e:
            print(str(e).encode('utf8'), file=sys.stderr)
    else:
        verbosity = 1
        if args.quiet:
            verbosity = 0
        elif args.verbose:
            verbosity = 2
        bt = Bitrot(
            verbosity=verbosity,
            test=args.test,
            follow_links=args.follow_links,
            commit_interval=args.commit_interval,
            chunk_size=args.chunk_size,
            workers=args.workers,
        )
        if args.fsencoding:
            FSENCODING = args.fsencoding
        try:
            bt.run()
        except BitrotException as bre:
            print('error:', bre.args[1], file=sys.stderr)
            sys.exit(bre.args[0])


if __name__ == '__main__':
    run_from_command_line()
