"""Frozen object-API reference entry, intentionally bypassing the file CLI path."""
from dataclasses import asdict
import argparse
import json
import sqlite3
from xbrl_local import Taxonomy, Instance, Warehouse, InputError, QualificationError


def main():
    parser = argparse.ArgumentParser()
    for name in ('db', 'dataset', 'batch', 'taxonomy', 'instance'):
        parser.add_argument('--' + name, required=True)
    args = parser.parse_args()
    try:
        taxonomy = Taxonomy.from_file(args.taxonomy)
        instance = Instance.from_file(args.instance)
        with Warehouse(args.db) as warehouse:
            result = asdict(warehouse.ingest(args.dataset, args.batch, taxonomy, instance))
        status = 0
    except (InputError, OSError, sqlite3.Error) as exc:
        result = {'error': str(exc)}
        if isinstance(exc, QualificationError):
            result['issues'] = [asdict(issue) for issue in exc.issues]
        status = 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return status


if __name__ == '__main__':
    raise SystemExit(main())
