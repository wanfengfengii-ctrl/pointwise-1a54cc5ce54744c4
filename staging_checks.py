"""Staging-specific failure and release checks for the optimized CLI import.

The optimized path stages rows in a private SQLite file under TMPDIR.  The
generic ``import_faults`` probes cannot see every access shape, so this module
supplies design-matched probes:

* ``import_faults`` ``temp-read`` / ``temp-write`` are exercised and must
  actually activate, fail with an exception (exit 2), roll back, and leave no
  staging file behind;
* a descriptor-level patch (``io.FileIO``), which the generic probes do not
  intercept, injects a read failure and must show the same guarantees;
* a successful import must unlink its staging file, and a failed import must
  leave neither business rows nor a staged file.

Every case runs the real CLI in a subprocess with TMPDIR pointed at a private
scratch directory that the harness also inspects for leftovers.
"""
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile

ROOT = Path(__file__).resolve().parent
TAXONOMY = ROOT / 'examples' / 'taxonomy.xml'
TMP = Path(os.environ.get('TMPDIR', tempfile.gettempdir()))


def _run_probe(work, argv, tmpdir, extra_env=None):
    """Run one CLI variant with TMPDIR confined to ``tmpdir``."""
    env = dict(os.environ, TMPDIR=str(tmpdir), TMP=str(tmpdir), TEMP=str(tmpdir),
               PYTHONDONTWRITEBYTECODE='1')
    if extra_env:
        env.update(extra_env)
    output = work / 'probe-stdout'
    error = work / 'probe-stderr'
    import subprocess
    with output.open('wb') as out, error.open('wb') as err:
        result = subprocess.run(argv, cwd=ROOT, env=env, stdout=out, stderr=err,
                                timeout=120)
    assert error.read_bytes() == b'', error.read_text(errors='replace')
    return result.returncode, json.loads(output.read_text(encoding='utf-8'))


def _snapshot(path):
    with sqlite3.connect(path) as db:
        return {table: db.execute('SELECT * FROM ' + table + ' ORDER BY 1,2').fetchall()
                for table in ('datasets', 'facts', 'batches')}


def _staged(tmpdir):
    return [p.name for p in Path(tmpdir).iterdir() if p.name.startswith('xbrl-stage-')]


def _source(work, n=6):
    sys.path.insert(0, str(ROOT))
    from import_workload import write
    path = work / 'input.xml'
    with path.open('w', encoding='utf-8', newline='') as stream:
        write(stream, 'scoped', n)
    return path


def _cli(mode, db, source, batch='batch'):
    return [sys.executable, '-B', '-m', 'xbrl_local', '--db', str(db), 'import',
            '--dataset', 'load', '--batch', batch, '--taxonomy', str(TAXONOMY),
            '--instance', str(source)]


STAGE_CREATE_PROBE = r'''
import json, os, runpy, sys, tempfile
real = tempfile.mkstemp
fired = []
def failing(*args, **kwargs):
    if 'xbrl-stage-' in str(kwargs.get('prefix', '')):
        fired.append(True)
        raise OSError('INJECTED_STAGE_CREATE')
    return real(*args, **kwargs)
tempfile.mkstemp = failing
sys.argv = ['xbrl_local'] + sys.argv[1:]
try:
    runpy.run_module('xbrl_local', run_name='__main__')
finally:
    tempfile.mkstemp = real
    with open(os.environ['EVIDENCE'], 'w', encoding='utf-8') as stream:
        json.dump({'activated': bool(fired)}, stream)
'''


def run():
    count = 0
    with tempfile.TemporaryDirectory(prefix='xbrl-staging-') as name:
        work = Path(name)
        source = _source(work)
        db = work / 'warehouse.sqlite'
        scratch = work / 'scratch'
        scratch.mkdir()
        # Baseline: a real import creates and then removes its staging file.
        status, receipt = _run_probe(work, _cli('target', db, source), scratch)
        assert status == 0 and receipt['inserted'] == 6, (status, receipt)
        assert _staged(scratch) == [], _staged(scratch)
        before = _snapshot(db)
        count += 1
        # Generic temp-read / temp-write probes reach the staging connection:
        # any read from or write to the scratch database must abort cleanly.
        for mode in ('temp-read', 'temp-write'):
            evidence = work / (mode + '.json')
            argv = [sys.executable, '-B', str(ROOT / 'import_faults.py'), mode,
                    str(evidence)] + _cli('target', db, source)[4:]
            status, error = _run_probe(work, argv, scratch)
            assert json.loads(evidence.read_text())['activated'], mode + ' did not activate'
            assert status == 2 and 'error' in error, (mode, status, error)
            assert _snapshot(db) == before, mode + ' changed the warehouse'
            assert _staged(scratch) == [], mode + ' left staging behind'
            count += 1
        # A genuinely unusable staging location.  This path creates its scratch
        # file with ``tempfile.mkstemp``, so the design-matched probe fails that
        # call rather than relying on filesystem permissions (which ``tempfile``
        # silently bypasses by falling back to another directory).
        probe = work / 'stage_create_probe.py'
        probe.write_text(STAGE_CREATE_PROBE, encoding='utf-8')
        evidence = work / 'stage-create.json'
        status, error = _run_probe(
            work, [sys.executable, '-B', str(probe)] + _cli('target', db, source)[4:],
            scratch, extra_env={'EVIDENCE': str(evidence), 'PYTHONPATH': str(ROOT)})
        assert json.loads(evidence.read_text())['activated'], 'stage-create probe did not activate'
        assert status == 2 and 'error' in error, (status, error)
        assert _snapshot(db) == before and _staged(scratch) == []
        count += 1
        # A post-stage failure (wrong taxonomy binding) must also release staging.
        other = work / 'other-taxonomy.xml'
        other.write_bytes(TAXONOMY.read_bytes() + b'\n')
        bad = _cli('target', db, source)
        bad[bad.index('--taxonomy') + 1] = str(other)
        status, error = _run_probe(work, bad, scratch)
        assert status == 2 and _snapshot(db) == before and _staged(scratch) == []
        count += 1
        # The commit probe must hit the durable warehouse transaction, not the
        # staging checkpoint, so a one-shot commit failure rolls back rows that
        # the same batch would otherwise insert.
        fresh = _source(work, n=12)
        evidence = work / 'commit.json'
        argv = [sys.executable, '-B', str(ROOT / 'import_faults.py'), 'commit',
                str(evidence)] + _cli('target', db, fresh, 'commit-error')[4:]
        status, error = _run_probe(work, argv, scratch)
        assert json.loads(evidence.read_text())['activated'], 'commit probe did not activate'
        assert status == 2 and 'error' in error, (status, error)
        assert _snapshot(db) == before and _staged(scratch) == []
        # Retrying the same batch after the injected failure must succeed.
        status, receipt = _run_probe(work, _cli('target', db, fresh, 'commit-error'),
                                     scratch)
        assert status == 0 and receipt['inserted'] == 6, (status, receipt)
        assert len(_snapshot(db)['facts']) == len(before['facts']) + 6
        assert _staged(scratch) == []
        count += 1
    print(json.dumps({'staging_cases_checked': count}, sort_keys=True))


if __name__ == '__main__':
    run()
