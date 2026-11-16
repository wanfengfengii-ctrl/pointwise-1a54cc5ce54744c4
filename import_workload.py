"""Deterministic XML workloads and an independent literal payload oracle."""
import hashlib
import json
import sqlite3

I = 'http://www.xbrl.org/2003/instance'
D = 'http://xbrl.org/2006/xbrldi'
LAB = 'urn:lab'
UNIT = 'http://www.xbrl.org/2003/iso4217'
ROOT = ('<i:xbrl xmlns:i="' + I + '" xmlns:d="' + D +
        '" xmlns:m="urn:lab" xmlns:q="urn:unused" xmlns:money="' + UNIT + '">')
END = '</i:xbrl>'


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':')).encode('utf-8')


def fact(k):
    return ('<m:Revenue id="f%06d" contextRef="c%06d" unitRef="u%06d" '
            'decimals="+0">+001.2500</m:Revenue>') % (k, k, k)


def context(k, scoped=False):
    # The same prefix has different meanings in sibling elements.
    member = ('<d:explicitMember xmlns:q="urn:lab" dimension="q:Region">'
              'q:North</d:explicitMember>') if scoped else (
              '<d:explicitMember dimension="m:Region">m:North</d:explicitMember>')
    return ('<i:context id="c%06d"><i:entity><i:identifier scheme="urn:e">'
            '机构%06d</i:identifier><i:segment>%s</i:segment></i:entity>'
            '<i:period><i:instant>2026-10-31</i:instant></i:period></i:context>') % (k, k, member)


def unit(k, scoped=False):
    measure = ('<i:measure xmlns:q="' + UNIT + '">q:USD</i:measure>') if scoped else (
               '<i:measure>money:USD</i:measure>')
    return '<i:unit id="u%06d">%s</i:unit>' % (k, measure)


def write(stream, kind, n):
    if kind not in ('late', 'scoped') or n < 1:
        raise ValueError('unknown workload or empty size')
    stream.write(ROOT)
    if kind == 'late':
        for k in range(n):
            stream.write(fact(k))
        for k in range(n):
            stream.write(context(k))
        for k in range(n):
            stream.write(unit(k))
    else:
        for k in range(n):
            stream.write(fact(k))
            stream.write(context(k, True))
            stream.write(unit(k, True))
    stream.write(END)


def row(k):
    return {'id': 'f%06d' % k, 'metric': '{urn:lab}Revenue',
            'context_id': 'c%06d' % k,
            'entity': {'scheme': 'urn:e', 'identifier': '机构%06d' % k},
            'period': 'instant:2026-10-31',
            'dimensions': {'segment': {'{urn:lab}Region': '{urn:lab}North'},
                           'scenario': {}},
            'unit': '{' + UNIT + '}USD', 'value': '+001.2500', 'decimals': '+0'}


def expected_fingerprint(taxonomy_digest, n):
    digest = hashlib.sha256()
    digest.update(b'[' + encoded(taxonomy_digest) + b',[')
    for k in range(n):
        if k:
            digest.update(b',')
        digest.update(encoded(row(k)))
    digest.update(b']]')
    return digest.hexdigest()


def check_database(path, taxonomy_digest, n):
    with sqlite3.connect(path) as db:
        assert db.execute('PRAGMA integrity_check').fetchone() == ('ok',)
        assert db.execute('PRAGMA user_version').fetchone() == (1,)
        assert db.execute('SELECT * FROM datasets ORDER BY name').fetchall() == [('load', taxonomy_digest)]
        cursor = db.execute('SELECT fact_id,payload FROM facts WHERE dataset=? ORDER BY fact_id', ('load',))
        for k in range(n):
            record = cursor.fetchone()
            expected = row(k)
            assert record == (expected['id'], encoded(expected).decode('utf-8')), (k, record)
        assert cursor.fetchone() is None
        assert db.execute('SELECT count(*) FROM facts').fetchone() == (n,)
        assert db.execute('SELECT * FROM batches').fetchall() == [
            ('load', 'batch', expected_fingerprint(taxonomy_digest, n), n)]
    return {'facts': n, 'fingerprint': expected_fingerprint(taxonomy_digest, n)}
