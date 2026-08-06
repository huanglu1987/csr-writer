#!/usr/bin/env python3
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from csr_writer.cli import main


raise SystemExit(main(["qc-format", *sys.argv[1:]]))

