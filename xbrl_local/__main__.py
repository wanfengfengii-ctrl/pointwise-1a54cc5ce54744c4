import argparse
from dataclasses import asdict
import json
import sys
import sqlite3
from . import Taxonomy, Warehouse, InputError, QualificationError
from .stream_import import prepare_file


def main(argv=None):
    parser = argparse.ArgumentParser(description='Import a complete local explicit-dimension batch')
    parser.add_argument('--db', required=True)
    commands = parser.add_subparsers(dest='command', required=True)
    ingest = commands.add_parser('import')
    ingest.add_argument('--dataset', required=True)
    ingest.add_argument('--batch', required=True)
    ingest.add_argument('--taxonomy', required=True)
    ingest.add_argument('--instance', required=True)
    read = commands.add_parser('read')
    read.add_argument('--dataset', required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'import':
            taxonomy = Taxonomy.from_file(args.taxonomy)
            # Parse, validate and stage the whole file before touching the
            # warehouse, so input failures cannot leave any business state.
            # Qualification then runs before the publishing transaction, which
            # mirrors the object path's ordering of parse and ingest errors.
            with prepare_file(args.instance, taxonomy) as prepared:
                with Warehouse(args.db) as warehouse:
                    result = asdict(prepared.publish(
                        warehouse, args.dataset, args.batch, taxonomy))
        else:
            with Warehouse(args.db) as warehouse:
                result = warehouse.read(args.dataset)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (InputError, OSError, sqlite3.Error) as exc:
        result = {'error': str(exc)}
        if isinstance(exc, QualificationError):
            result['issues'] = [asdict(issue) for issue in exc.issues]
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 2


if __name__ == '__main__':
    sys.exit(main())
