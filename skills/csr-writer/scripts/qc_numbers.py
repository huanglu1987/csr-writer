#!/usr/bin/env python3
"""Run all three numeric guards through the same fail-closed resolve command."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from csr_writer.cli import main


raise SystemExit(main(["resolve", *sys.argv[1:]]))

