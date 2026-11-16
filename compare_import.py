"""Independent reference-vs-target comparison over real files.

Runs the frozen object-API entry (``reference_import.py``) and the optimized
file CLI side by side on independently generated workloads and compares the
complete SQLite state: dataset binding, every canonical fact payload, batch
fingerprints and receipts.  It also replays a reordered batch and checks that
unused definitions and locally scoped prefixes do not change the outcome.

Nothing here imports the optimized implementation; payload expectations come
from ``import_workload`` (a literal oracle) and snapshots come from plain
``sqlite3``.
"""
import io
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TAXONOMY = ROOT / 'examples' / 'taxonomy.xml'
sys.path.insert(0, str(ROOT))

from import_bench import command, launch  # noqa: E402
from import_workload import END, ROOT as XML_ROOT, context, fact, unit, write  # noqa: E402


def snapshot(path):
    with sqlite3.connect(path) as db:
        return {table: db.execute('SELECT * FROM ' + table + ' ORDER BY 1,2').fetchall()
                for table in ('datasets', 'facts', 'batches')}


def run_case(work, label, text, batch='batch'):
    source = work / (label + '.xml')
    source.write_text(text, encoding='utf-8')
    reference = work / (label + '-reference.sqlite')
    target = work / (label + '-target.sqlite')
    expected = launch(command('reference', reference, source, batch), work)
    actual = launch(command('target', target, source, batch), work)
    assert expected['status'] == actual['status'], (label, expected, actual)
    assert expected['result'] == actual['result'], (label, expected, actual)
    assert snapshot(reference) == snapshot(target), label
    return actual['result']


def run():
    checked = 0
    with tempfile.TemporaryDirectory(prefix='xbrl-compare-') as name:
        work = Path(name)
        for kind in ('late', 'scoped'):
            for n in (1500, 4000):
                stream = io.StringIO()
                write(stream, kind, n)
                receipt = run_case(work, '%s-%s' % (kind, n), stream.getvalue())
                assert receipt['inserted'] == n, receipt
                checked += 1
        # Same facts in a different order, with extra unused definitions, must
        # replay the original receipt and leave every row untouched.
        body = ''.join(fact(k) for k in reversed(range(400)))
        body += ''.join(context(k, True) + unit(k, True) for k in range(450))
        sample = work / 'reordered.xml'
        sample.write_text(XML_ROOT + body + END, encoding='utf-8')
        db = work / 'replay.sqlite'
        first = launch(command('target', db, sample, 'replay'), work)
        before = snapshot(db)
        replay = launch(command('target', db, sample, 'replay'), work)
        assert first['status'] == replay['status'] == 0
        assert first['result'] == replay['result'] == {'dataset': 'load', 'batch_id': 'replay', 'inserted': 400}
        assert snapshot(db) == before
        reference = work / 'replay-reference.sqlite'
        mirror = launch(command('reference', reference, sample, 'replay'), work)
        assert mirror['result'] == first['result'] and snapshot(reference) == before
        checked += 1
        # Local prefix scoping and unused definitions compared over both paths.
        scoped = io.StringIO()
        write(scoped, 'scoped', 300)
        run_case(work, 'scoped-local', scoped.getvalue())
        checked += 1
    print(json.dumps({'compared_cases': checked}, sort_keys=True))


if __name__ == '__main__':
    run()
