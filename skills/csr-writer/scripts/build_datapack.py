#!/usr/bin/env python3
"""Alias kept for the §2.1 layout; parsing and building are one validated transaction."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from csr_writer.cli import main


raise SystemExit(main(["build-datapack", *sys.argv[1:]]))

