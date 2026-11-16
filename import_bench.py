"""Measure complete import subprocess RSS; generation and checks run in the parent."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from import_workload import write, check_database

ROOT = Path(__file__).resolve().parent
TAXONOMY = ROOT / 'examples' / 'taxonomy.xml'


def command(mode, db, instance, batch='batch', taxonomy=TAXONOMY):
    args = ['--db', str(db)]
    if mode == 'target':
        prefix = [sys.executable, '-B', '-m', 'xbrl_local']
        args += ['import']
    elif mode == 'reference':
        prefix = [sys.executable, '-B', str(ROOT / 'reference_import.py')]
    else:
        raise ValueError(mode)
    return prefix + args + ['--dataset', 'load', '--batch', batch,
                           '--taxonomy', str(taxonomy), '--instance', str(instance)]


def launch(argv, directory, timeout=600):
    directory = Path(directory)
    scratch = directory / 'scratch'
    scratch.mkdir(exist_ok=True)
    env = dict(os.environ, TMPDIR=str(scratch), TMP=str(scratch),
               TEMP=str(scratch), PYTHONDONTWRITEBYTECODE='1')
    # No pipes: arbitrarily large qualification diagnostics cannot block the child.
    with (directory / 'stdout').open('wb') as out, (directory / 'stderr').open('wb') as err:
        process = subprocess.Popen(argv, cwd=ROOT, env=env, stdout=out, stderr=err,
                                   start_new_session=True)
        timed_out = threading.Event()
        def stop():
            timed_out.set()
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        timer = threading.Timer(timeout, stop)
        start = time.perf_counter()
        timer.start()
        try:
            _, status, usage = os.wait4(process.pid, 0)
            process.returncode = os.waitstatus_to_exitcode(status)
        finally:
            timer.cancel()
            timer.join()
            if process.returncode is None:
                stop()
                process.wait()
        if timed_out.is_set():
            raise TimeoutError(argv)
    output = (directory / 'stdout').read_text(encoding='utf-8')
    error = (directory / 'stderr').read_text(encoding='utf-8')
    assert not error, error
    assert not list(scratch.iterdir()), 'temporary resources leaked'
    rss = usage.ru_maxrss / (1024 * 1024 if sys.platform == 'darwin' else 1024)
    return {'status': process.returncode, 'result': json.loads(output),
            'rss_mib': rss, 'seconds': time.perf_counter() - start}


def run(mode):
    results = []
    digest = hashlib.sha256(TAXONOMY.read_bytes().decode('utf-8').encode('utf-8')).hexdigest()
    with tempfile.TemporaryDirectory(prefix='xbrl-rss-') as name:
        work = Path(name)
        for kind in ('late', 'scoped'):
            medians = []
            for n in (25000, 100000):
                instance = work / (kind + str(n) + '.xml')
                with instance.open('w', encoding='utf-8', newline='') as stream:
                    write(stream, kind, n)
                runs = []
                for repeat in range(3):
                    trial = work / ('%s-%s-%s' % (kind, n, repeat))
                    trial.mkdir()
                    db = trial / 'warehouse.sqlite'
                    measured = launch(command(mode, db, instance), trial)
                    assert measured['status'] == 0, measured
                    assert measured['result'] == {'dataset': 'load', 'batch_id': 'batch', 'inserted': n}
                    measured['verified'] = check_database(db, digest, n)
                    runs.append(measured)
                median = statistics.median(r['rss_mib'] for r in runs)
                medians.append(median)
                results.append({'kind': kind, 'n': n, 'rss_median_mib': median, 'runs': runs})
            if mode == 'target':
                assert all(m <= 96 for m in medians), (kind, medians)
                assert medians[1] - medians[0] <= 12, (kind, medians)
    print(json.dumps({'mode': mode, 'measure': 'complete-child-ru_maxrss', 'results': results},
                     ensure_ascii=False, sort_keys=True))
    return results


def demo():
    digest = hashlib.sha256(TAXONOMY.read_bytes()).hexdigest()
    with tempfile.TemporaryDirectory(prefix='xbrl-file-demo-') as name:
        work = Path(name)
        source = work / 'late.xml'
        with source.open('w', encoding='utf-8', newline='') as stream:
            write(stream, 'scoped', 8)
        db = work / 'warehouse.sqlite'
        first = launch(command('target', db, source), work)
        second = launch(command('target', db, source), work)
        assert first['status'] == second['status'] == 0
        assert first['result'] == second['result'] == {'dataset': 'load', 'batch_id': 'batch', 'inserted': 8}
        checked = check_database(db, digest, 8)
        print(json.dumps({'first': first, 'replay': second, 'stored': checked}, sort_keys=True))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=('reference', 'target', 'demo'))
    args = parser.parse_args()
    demo() if args.mode == 'demo' else run(args.mode)
