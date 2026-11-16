"""File-path correctness checks independent of the RSS thresholds."""
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
from import_bench import ROOT, TAXONOMY, command, launch
from import_workload import ROOT as XML_ROOT, END, write, context, unit, fact, row


def snapshot(path):
    with sqlite3.connect(path) as db:
        return {table: db.execute('SELECT * FROM ' + table + ' ORDER BY 1,2').fetchall()
                for table in ('datasets', 'facts', 'batches')}


def source(kind='scoped', n=4):
    out = io.StringIO()
    write(out, kind, n)
    return out.getvalue()


def run():
    count = 0
    with tempfile.TemporaryDirectory(prefix='xbrl-boundaries-') as name:
        work = Path(name)
        src = work / 'input.xml'
        reference = work / 'reference.sqlite'
        target = work / 'target.sqlite'
        text = source()
        def call(mode, db, xml, batch='batch', taxonomy=TAXONOMY):
            src.write_text(xml, encoding='utf-8')
            return launch(command(mode, db, src, batch, taxonomy), work)
        a = call('reference', reference, text)
        b = call('target', target, text)
        assert a['status'] == b['status'] == 0
        assert a['result'] == b['result'] == {'dataset': 'load', 'batch_id': 'batch', 'inserted': 4}
        assert snapshot(reference) == snapshot(target)
        with sqlite3.connect(target) as db:
            stored = [json.loads(x[0]) for x in db.execute('SELECT payload FROM facts ORDER BY fact_id')]
        assert stored == [row(k) for k in range(4)]
        count += 1
        # Definitions can be unused; input order cannot alter a batch identity.
        reordered = XML_ROOT + ''.join(fact(k) for k in reversed(range(4)))
        reordered += ''.join(context(k, True) + unit(k, True) for k in range(5)) + END
        before = snapshot(target)
        replay = call('target', target, reordered)
        assert replay['status'] == 0 and replay['result'] == b['result']
        assert snapshot(target) == before
        count += 1
        bad_cases = {
            'tail': text[:-len(END)],
            'unresolved': text.replace('contextRef="c000003"', 'contextRef="missing"'),
            'cross-kind-id': text.replace('id="u000003"', 'id="f000000"'),
            'unused-invalid': text.replace(END, context(77).replace('2026-10-31', '2026-02-30') + END),
            'dtd': '<!DOCTYPE i:xbrl []>' + text,
            'unbound': text.replace('dimension="q:Region"', 'dimension="absent:Region"', 1),
            'namespace': text.replace('xmlns:q="urn:lab"', 'xmlns:q="urn:other"', 1),
            'qualification-order': text.replace('q:North', 'q:Hidden'),
            'fact-conflict': text.replace('+001.2500', '99', 1),
        }
        for label, bad in bad_cases.items():
            old_ref, old_target = snapshot(reference), snapshot(target)
            expected = call('reference', reference, bad, label)
            actual = call('target', target, bad, label)
            assert expected['status'] == actual['status'] == 2, (label, expected, actual)
            # Error wording may differ; observable error category and ordered issues may not.
            assert isinstance(actual['result'].get('error'), str)
            assert actual['result'].get('issues') == expected['result'].get('issues'), (label, expected, actual)
            if label == 'qualification-order':
                assert [i['fact_id'] for i in actual['result']['issues']] == [
                    'f000000', 'f000001', 'f000002', 'f000003']
            assert snapshot(reference) == old_ref and snapshot(target) == old_target, label
            count += 1
        # Existing taxonomy binding is byte-sensitive, even if qualification is unchanged.
        changed_tax = work / 'changed-taxonomy.xml'
        changed_tax.write_bytes(TAXONOMY.read_bytes() + b'\n')
        for mode, db in (('reference', reference), ('target', target)):
            prior = snapshot(db)
            result = call(mode, db, text, 'new-taxonomy', changed_tax)
            assert result['status'] == 2 and snapshot(db) == prior
        count += 1
        # A storage error after earlier inserts must not publish any part of a new batch.
        extra = XML_ROOT + ''.join(fact(k) + context(k, True) + unit(k, True)
                                  for k in range(4, 8)) + END
        with sqlite3.connect(target) as db:
            db.execute("CREATE TRIGGER reject_tail BEFORE INSERT ON facts "
                       "WHEN NEW.fact_id='f000007' BEGIN SELECT RAISE(ABORT,'INJECTED_SQL'); END")
        prior = snapshot(target)
        result = call('target', target, extra, 'storage-error')
        assert result['status'] == 2 and snapshot(target) == prior
        with sqlite3.connect(target) as db:
            db.execute('DROP TRIGGER reject_tail')
        count += 1
        # The commit boundary uses a one-shot failure, leaving rollback available.
        src.write_text(extra, encoding='utf-8')
        evidence = work / 'fault.json'
        argv = [sys.executable, '-B', str(ROOT / 'import_faults.py'), 'commit', str(evidence)]
        argv += command('target', target, src, 'commit-error')[4:]
        prior = snapshot(target)
        result = launch(argv, work)
        assert json.loads(evidence.read_text())['activated'], 'commit probe did not activate'
        assert result['status'] == 2 and snapshot(target) == prior
        count += 1
        # Invalid UTF-8 must reject without changing the existing warehouse.
        src.write_bytes(text.encode('utf-8') + b'\xff')
        prior = snapshot(target)
        result = launch(command('target', target, src, 'encoding-error'), work)
        assert result['status'] == 2 and snapshot(target) == prior
        count += 1
        # Recovery of the same failed batch identity must remain usable.
        good = call('target', target, extra, 'storage-error')
        assert good['status'] == 0 and good['result']['inserted'] == 4
        count += 1
    print(json.dumps({'file_cases_checked': count}, sort_keys=True))


if __name__ == '__main__':
    run()
