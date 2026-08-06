#!/usr/bin/env python3
from csr_writer.cli import main


if __name__ == "__main__":
    raise SystemExit(main(["tfl-result-apply-rtf", *__import__("sys").argv[1:]]))
