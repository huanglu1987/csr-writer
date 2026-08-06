"""Small deterministic and integrity-preserving I/O helpers."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unicodedata
from pathlib import Path
from typing import Any, Iterable

import yaml

from .errors import ConfigurationError


def paths_alias(left: str | Path, right: str | Path) -> bool:
    """Conservatively identify one filesystem target, including macOS aliases."""
    left_path, right_path = Path(left), Path(right)
    try:
        if left_path.exists() and right_path.exists() and left_path.samefile(right_path):
            return True
    except OSError:
        pass
    left_resolved = left_path.resolve(strict=False)
    right_resolved = right_path.resolve(strict=False)
    if left_resolved == right_resolved:
        return True
    # APFS commonly folds case and Unicode normalization. Apply the same conservative
    # identity before either not-yet-created output is opened.
    left_key = unicodedata.normalize("NFC", os.fspath(left_resolved)).casefold()
    right_key = unicodedata.normalize("NFC", os.fspath(right_resolved)).casefold()
    return left_key == right_key


class UniqueKeyLoader(yaml.SafeLoader):
    """Safe YAML loader that refuses silent last-value-wins key replacement."""


def _construct_unique_mapping(loader: UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False):
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise ConfigurationError(
                f"YAML contains duplicate key {key!r} at line {key_node.start_mark.line + 1}"
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def load_yaml(path: str | Path) -> Any:
    source = Path(path)
    try:
        with source.open("r", encoding="utf-8") as handle:
            return yaml.load(handle, Loader=UniqueKeyLoader)
    except ConfigurationError:
        raise
    except Exception as exc:
        raise ConfigurationError(f"Cannot read YAML {source}: {exc}") from exc


def load_json_unique(path: str | Path) -> Any:
    source = Path(path)

    def unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ConfigurationError(f"JSON contains duplicate key {key!r}: {source}")
            result[key] = value
        return result

    try:
        with source.open("r", encoding="utf-8") as handle:
            return json.load(handle, object_pairs_hook=unique_pairs)
    except ConfigurationError:
        raise
    except Exception as exc:
        raise ConfigurationError(f"Cannot read JSON {source}: {exc}") from exc


def load_data(path: str | Path) -> Any:
    source = Path(path)
    return load_json_unique(source) if source.suffix.lower() == ".json" else load_yaml(source)


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write_text_bundle(entries: Iterable[tuple[str | Path, str]]) -> None:
    """Stage every text payload before publishing any destination; roll back on failure."""
    prepared = [(Path(path), text) for path, text in entries]
    if not prepared:
        return
    for index, (destination, _) in enumerate(prepared):
        for other, _ in prepared[index + 1 :]:
            if paths_alias(destination, other):
                raise ConfigurationError(
                    f"Atomic output paths alias each other: {destination} and {other}"
                )
        if destination.is_dir():
            raise ConfigurationError(f"Atomic output path is a directory: {destination}")
        if destination.parent.exists() and not destination.parent.is_dir():
            raise ConfigurationError(
                f"Atomic output parent is not a directory: {destination.parent}"
            )
    for destination, _ in prepared:
        destination.parent.mkdir(parents=True, exist_ok=True)

    staged: dict[Path, Path] = {}
    backup_holders: dict[Path, Path] = {}
    allocated_backups: list[Path] = []
    committed: list[Path] = []
    try:
        for destination, text in prepared:
            fd, temporary = tempfile.mkstemp(
                prefix=f".{destination.name}.staged.", dir=destination.parent
            )
            temporary_path = Path(temporary)
            staged[destination] = temporary_path
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())

        for destination, _ in prepared:
            if not destination.exists() and not destination.is_symlink():
                continue
            fd, backup = tempfile.mkstemp(
                prefix=f".{destination.name}.rollback.", dir=destination.parent
            )
            os.close(fd)
            backup_path = Path(backup)
            allocated_backups.append(backup_path)
            os.replace(destination, backup_path)
            backup_holders[destination] = backup_path

        for destination, _ in prepared:
            os.replace(staged[destination], destination)
            committed.append(destination)
    except Exception:
        for destination in reversed(committed):
            try:
                destination.unlink()
            except FileNotFoundError:
                pass
        for destination, backup in reversed(list(backup_holders.items())):
            if backup.exists() or backup.is_symlink():
                os.replace(backup, destination)
        for temporary in staged.values():
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        for backup in allocated_backups:
            try:
                backup.unlink()
            except FileNotFoundError:
                pass
        raise
    else:
        for backup in allocated_backups:
            try:
                backup.unlink()
            except FileNotFoundError:
                pass


def atomic_write_text(path: str | Path, text: str) -> None:
    atomic_write_text_bundle([(path, text)])


def atomic_write_json_bundle(entries: Iterable[tuple[str | Path, Any]]) -> None:
    atomic_write_text_bundle(
        (
            path,
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
        for path, payload in entries
    )


def atomic_write_json(path: str | Path, payload: Any) -> None:
    atomic_write_json_bundle([(path, payload)])
