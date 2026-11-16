"""One-shot failure launcher. Writes activation evidence outside the measured process."""
import builtins
import io
import json
import os
from pathlib import Path
import runpy
import sqlite3
import sys

MODE = sys.argv[1]
EVIDENCE = Path(sys.argv[2])
CLI_ARGS = sys.argv[3:]
TEMP = Path(os.environ['TMPDIR']).resolve()
fired = False
real_open = builtins.open
real_io_open = io.open
real_connect = sqlite3.connect


def trip():
    global fired
    if not fired:
        fired = True
        raise OSError('INJECTED_IMPORT_IO')


def owned(path):
    if not isinstance(path, (str, bytes, os.PathLike)):
        return False
    try:
        return Path(os.fsdecode(path)).resolve().is_relative_to(TEMP)
    except (OSError, ValueError):
        return False


class Handle:
    def __init__(self, handle):
        self.handle = handle
    def __getattr__(self, name):
        return getattr(self.handle, name)
    def __enter__(self):
        self.handle.__enter__()
        return self
    def __exit__(self, *args):
        return self.handle.__exit__(*args)
    def read(self, *args):
        if MODE == 'temp-read':
            trip()
        return self.handle.read(*args)
    def write(self, *args):
        if MODE == 'temp-write':
            trip()
        return self.handle.write(*args)


def wrap_open(original):
    def opening(path, *args, **kwargs):
        handle = original(path, *args, **kwargs)
        return Handle(handle) if owned(path) else handle
    return opening


def sql_fault(connection, sql):
    text = sql.lstrip().upper()
    if MODE == 'commit' and text.startswith('COMMIT'):
        trip()
    if connection.temporary and MODE in ('temp-read', 'temp-write'):
        is_read = text.startswith(('SELECT', 'WITH'))
        if (MODE == 'temp-read') == is_read:
            trip()


class Cursor(sqlite3.Cursor):
    def execute(self, sql, *args, **kwargs):
        sql_fault(self.connection, sql)
        return super().execute(sql, *args, **kwargs)
    def executemany(self, sql, *args, **kwargs):
        sql_fault(self.connection, sql)
        return super().executemany(sql, *args, **kwargs)
    def executescript(self, sql, *args, **kwargs):
        sql_fault(self.connection, sql)
        return super().executescript(sql, *args, **kwargs)


class Connection(sqlite3.Connection):
    temporary = False
    def cursor(self, factory=Cursor):
        return super().cursor(factory)
    def execute(self, sql, *args, **kwargs):
        sql_fault(self, sql)
        return super().execute(sql, *args, **kwargs)
    def executemany(self, sql, *args, **kwargs):
        sql_fault(self, sql)
        return super().executemany(sql, *args, **kwargs)
    def executescript(self, sql, *args, **kwargs):
        sql_fault(self, sql)
        return super().executescript(sql, *args, **kwargs)


def connect(database, *args, **kwargs):
    # Custom factories and raw descriptors require a design-specific probe.
    kwargs.setdefault('factory', Connection)
    connection = real_connect(database, *args, **kwargs)
    if isinstance(connection, Connection):
        connection.temporary = owned(database) or database == ''
    return connection


builtins.open = wrap_open(real_open)
io.open = wrap_open(real_io_open)
sqlite3.connect = connect
try:
    sys.argv = ['xbrl_local'] + CLI_ARGS
    runpy.run_module('xbrl_local', run_name='__main__')
finally:
    builtins.open = real_open
    io.open = real_io_open
    sqlite3.connect = real_connect
    with real_open(EVIDENCE, 'w', encoding='utf-8') as stream:
        json.dump({'mode': MODE, 'activated': fired}, stream)
