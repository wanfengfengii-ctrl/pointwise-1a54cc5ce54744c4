"""Streaming file import path: bounded memory over the same instance semantics.

The public object API (``Instance.from_file``/``from_xml``) keeps its whole-tree
behaviour and remains the reference entry.  The CLI import instead walks the
instance file twice with ``streamio`` and holds at most one top-level element in
memory at a time:

* pass 1 validates every top-level element in document order (missing or
  duplicate XML id, context/unit syntax) and stages compact definitions in a
  private, disposable SQLite staging database under the system temporary
  directory; facts are only counted;
* pass 2 re-reads the same elements, validates each fact, resolves its context
  and unit against the staged definitions and stages the canonical payload with
  its input sequence.

Global XML-id uniqueness lives in a staging table (not a Python set), so no
per-element state is retained in memory.  Qualification then runs over the
staged rows in input order (keeping ``issues`` ordered exactly like the object
path), and the batch fingerprint is folded incrementally over the staged
payloads in fact-id order.  Only a single ``BEGIN IMMEDIATE`` transaction
publishes rows and the receipt; if anything fails before or inside it no
business row, dataset binding or receipt is left behind, and the staging
database is closed and unlinked on every exit path.
"""
import hashlib
import json
import os
import sqlite3
import tempfile
from .errors import InputError, QualificationError, Issue
from .instance import parse_context, parse_fact, parse_unit
from .streamio import stream_children
from .warehouse import _json
from .xmlio import I, tag, required

_CONTEXT = tag(I, 'context')
_UNIT = tag(I, 'unit')
_SCHEMA = (
    'CREATE TABLE ids(id TEXT PRIMARY KEY)',
    'CREATE TABLE contexts(id TEXT PRIMARY KEY, scheme TEXT NOT NULL, '
    'entity TEXT NOT NULL, period TEXT NOT NULL, segment TEXT NOT NULL, '
    'scenario TEXT NOT NULL)',
    'CREATE TABLE units(id TEXT PRIMARY KEY, measure TEXT NOT NULL)',
    'CREATE TABLE facts(seq INTEGER PRIMARY KEY, fact_id TEXT NOT NULL UNIQUE, '
    'metric TEXT NOT NULL, context_id TEXT NOT NULL, payload TEXT NOT NULL)',
)


class Staging:
    """Disposable on-disk scratch store for one file import."""

    def __init__(self):
        descriptor, self.path = tempfile.mkstemp(prefix='xbrl-stage-', suffix='.sqlite')
        os.close(descriptor)
        self.db = None
        self._closed = False
        try:
            self.db = sqlite3.connect(self.path, isolation_level=None)
            # Scratch data is regenerable: skip journal and sync while keeping
            # the page cache small so RSS stays flat as the instance grows.
            self.db.execute('PRAGMA journal_mode=OFF')
            self.db.execute('PRAGMA synchronous=OFF')
            self.db.execute('PRAGMA cache_size=-2048')
            self.db.execute('BEGIN')
            for statement in _SCHEMA:
                self.db.execute(statement)
        except BaseException:
            self.close()
            raise

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            if self.db is not None:
                self.db.close()
        finally:
            self.db = None
            try:
                os.unlink(self.path)
            except FileNotFoundError:
                pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _stage_definitions(staging, path):
    """Pass 1: validate ids and definitions, staging them on disk."""
    db = staging.db
    facts = 0
    context_sql = 'INSERT INTO contexts VALUES (?, ?, ?, ?, ?, ?)'
    unit_sql = 'INSERT INTO units VALUES (?, ?)'
    for element, scopes in stream_children(path, tag(I, 'xbrl')):
        identity = required(element, 'id')
        try:
            db.execute('INSERT INTO ids VALUES (?)', (identity,))
        except sqlite3.IntegrityError:
            raise InputError('duplicate XML id ' + identity) from None
        if element.tag == _CONTEXT:
            context = parse_context(element, identity, scopes)
            db.execute(context_sql, (
                identity, context.scheme, context.entity, context.period,
                json.dumps(dict(context.segment), ensure_ascii=False, sort_keys=True),
                json.dumps(dict(context.scenario), ensure_ascii=False, sort_keys=True)))
        elif element.tag == _UNIT:
            db.execute(unit_sql, (identity, parse_unit(element, scopes)))
        else:
            # Fact syntax is validated in pass 2, where targets are resolved.
            facts += 1
    if facts == 0:
        raise InputError('empty fact batch')
    db.execute('COMMIT')
    db.execute('BEGIN')


def _stage_facts(staging, path):
    """Pass 2: validate every fact, resolve targets and stage canonical rows."""
    db = staging.db
    context_sql = ('SELECT scheme, entity, period, segment, scenario '
                   'FROM contexts WHERE id=?')
    unit_sql = 'SELECT measure FROM units WHERE id=?'
    fact_sql = 'INSERT INTO facts VALUES (?, ?, ?, ?, ?)'
    seq = 0
    for element, scopes in stream_children(path, tag(I, 'xbrl')):
        if element.tag in (_CONTEXT, _UNIT):
            continue
        identity, metric, context_id, unit_id, value, decimals = parse_fact(element)
        context = db.execute(context_sql, (context_id,)).fetchone()
        unit = db.execute(unit_sql, (unit_id,)).fetchone()
        if context is None or unit is None:
            raise InputError('unresolved context or unit')
        scheme, entity, period, segment, scenario = context
        payload = _json({
            'id': identity, 'metric': metric, 'context_id': context_id,
            'entity': {'scheme': scheme, 'identifier': entity},
            'period': period,
            'dimensions': {'segment': json.loads(segment),
                           'scenario': json.loads(scenario)},
            'unit': unit[0], 'value': value, 'decimals': decimals})
        db.execute(fact_sql, (seq, identity, metric, context_id, payload))
        seq += 1
    db.execute('COMMIT')
    db.execute('BEGIN')


def _qualify(staging, taxonomy):
    """Qualify staged facts in input order; raise with ordered issues.

    Decisions are not cached: an unbounded per-fact cache would grow with the
    batch, while every context in a large instance is usually distinct.
    """
    db = staging.db
    issues = []
    for identity, metric, context_id, segment, scenario in db.execute(
            'SELECT f.fact_id, f.metric, f.context_id, c.segment, c.scenario '
            'FROM facts f JOIN contexts c ON c.id=f.context_id ORDER BY f.seq'):
        if not taxonomy.qualifies(metric, json.loads(segment), json.loads(scenario)):
            issues.append(Issue(identity, metric, context_id))
    if issues:
        raise QualificationError(issues)


def _fingerprint(staging, taxonomy_digest):
    """Fold the canonical batch payload exactly like ``Warehouse.ingest``."""
    digest = hashlib.sha256()
    digest.update(b'[' + _json(taxonomy_digest).encode('utf-8') + b',[')
    first = True
    for (payload,) in staging.db.execute(
            'SELECT payload FROM facts ORDER BY fact_id'):
        if not first:
            digest.update(b',')
        digest.update(payload.encode('utf-8'))
        first = False
    digest.update(b']]')
    return digest.hexdigest()


def _validate_identifiers(dataset, batch_id):
    if not isinstance(dataset, str) or not dataset.strip() or \
            not isinstance(batch_id, str) or not batch_id.strip():
        raise InputError('dataset and batch_id must be nonempty strings')


class PreparedImport:
    """A parsed and staged file import; publishing happens in one transaction."""

    def __init__(self, staging):
        self._staging = staging

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        self._staging.close()

    def publish(self, warehouse, dataset, batch_id, taxonomy):
        """Check identifiers, qualify in input order, fingerprint, then publish.

        This is the same error ordering as ``Warehouse.ingest``: identifier
        validation, then ordered qualification, then the single transaction.
        """
        _validate_identifiers(dataset, batch_id)
        _qualify(self._staging, taxonomy)
        fingerprint = _fingerprint(self._staging, taxonomy.digest)
        rows = self._staging.db.execute(
            'SELECT fact_id, payload FROM facts ORDER BY fact_id')
        return warehouse.publish_stream(
            dataset, batch_id, taxonomy.digest, fingerprint, rows)


def prepare_file(path, taxonomy):
    """Parse and stage one instance file; no business result is published yet.

    Contexts and units are validated for the whole file (including unused
    definitions) before facts are resolved, mirroring the object path where
    instance parsing completes before ingestion begins.
    """
    staging = Staging()
    try:
        _stage_definitions(staging, path)
        _stage_facts(staging, path)
    except BaseException:
        staging.close()
        raise
    return PreparedImport(staging)


def ingest_file(warehouse, dataset, batch_id, taxonomy, path):
    """Import one instance file through the bounded-memory path.

    Returns the same ``Receipt`` as ``Warehouse.ingest`` for the same input,
    with identical canonical payloads, dataset binding and batch fingerprint.
    """
    with prepare_file(path, taxonomy) as prepared:
        return prepared.publish(warehouse, dataset, batch_id, taxonomy)
