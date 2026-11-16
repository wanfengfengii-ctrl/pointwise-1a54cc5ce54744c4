"""Keep every original check, then verify file semantics and the chosen RSS track."""
from pathlib import Path
import argparse
import subprocess
import sys

ROOT = Path(__file__).resolve().parent

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=('baseline', 'target'))
    args = parser.parse_args()
    for argv in (
        [sys.executable, '-B', 'checks.py'],
        [sys.executable, '-B', 'scenarios/qualification.py'],
        [sys.executable, '-B', 'demo.py'],
        [sys.executable, '-B', 'import_checks.py'],
        [sys.executable, '-B', 'import_bench.py',
         'reference' if args.mode == 'baseline' else 'target'],
    ):
        subprocess.run(argv, cwd=ROOT, check=True)
