from __future__ import annotations

import argparse
import filecmp
import logging
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence, Union

LOGGER_NAME = "folder_sync"
LOG_FORMAT = "%(asctime)s | %(levelname)-7s | %(message)s"


class ArgumentError(ValueError):
    """Raised when command line arguments are missing or invalid."""


@dataclass(frozen=True)
class SyncConfig:

    source: Path
    replica: Path
    interval: float
    count: int
    log_file: Path

    def __post_init__(self) -> None:
        if not self.source.is_dir():
            raise ArgumentError(f"source folder does not exist: {self.source}")
        source, replica = self.source.resolve(), self.replica.resolve()
        if source == replica or source in replica.parents or replica in source.parents:
            raise ArgumentError("source and replica folders must not be nested or equal")


class _ArgumentParser(argparse.ArgumentParser):

    def error(self, message: str):  # type: ignore[override]
        raise ArgumentError(message)


def _non_negative(number_type):
    def convert(value: str):
        number = number_type(value)
        if number < 0:
            raise ValueError("must not be negative")
        return number

    convert.__name__ = f"non-negative {number_type.__name__}"
    return convert


def parse_config(argv: Sequence[str]) -> SyncConfig:
    parser = _ArgumentParser(prog="folder_sync", add_help=False)
    parser.add_argument("source", type=Path)
    parser.add_argument("replica", type=Path)
    parser.add_argument("interval", type=_non_negative(float))
    parser.add_argument("count", type=_non_negative(int))
    parser.add_argument("log_file", type=Path)
    args = parser.parse_args(argv)
    return SyncConfig(args.source, args.replica, args.interval, args.count, args.log_file)


class Logger:

    def __init__(
        self,
        level: Union[int, str] = logging.INFO,
        fmt: str = LOG_FORMAT,
        name: str = LOGGER_NAME,
    ) -> None:
        self._formatter = logging.Formatter(fmt)
        self.logger = logging.getLogger(name)
        self.logger.setLevel(level)  # accepts logging.INFO or "INFO"
        self.logger.propagate = False
        self._handlers: List[logging.Handler] = []
        self._add_handler(logging.StreamHandler(sys.stdout))

    def add_file(self, log_file: Path) -> bool:
        try:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            self._add_handler(logging.FileHandler(log_file, encoding="utf-8"))
        except OSError as error:
            self.logger.error("Cannot write to log file %s: %s", log_file, error)
            return False
        return True

    def close(self) -> None:
        for handler in self._handlers:
            self.logger.removeHandler(handler)
            handler.close()
        self._handlers.clear()

    def __enter__(self) -> "Logger":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def _add_handler(self, handler: logging.Handler) -> None:
        handler.setFormatter(self._formatter)
        self.logger.addHandler(handler)
        self._handlers.append(handler)


class FolderSynchronizer:

    def __init__(self, source: Path, replica: Path, log: logging.Logger) -> None:
        self._source = source
        self._replica = replica
        self._log = log

    def synchronize(self) -> None:
        """Run a single synchronization pass."""
        filecmp.clear_cache()
        self._ensure_directory(self._replica)
        self._sync_directory(self._source, self._replica)

    def _sync_directory(self, source_dir: Path, replica_dir: Path) -> None:
        self._remove_extraneous(source_dir, replica_dir)
        for source_entry in sorted(source_dir.iterdir()):
            self._sync_entry(source_entry, replica_dir / source_entry.name)

    def _remove_extraneous(self, source_dir: Path, replica_dir: Path) -> None:
        for replica_entry in sorted(replica_dir.iterdir()):
            if not (source_dir / replica_entry.name).exists():
                self._remove(replica_entry)

    def _sync_entry(self, source_entry: Path, replica_entry: Path) -> None:
        if source_entry.is_dir():
            self._replace_if_type_differs(replica_entry, want_dir=True)
            self._ensure_directory(replica_entry)
            self._sync_directory(source_entry, replica_entry)
        elif source_entry.is_file():
            self._replace_if_type_differs(replica_entry, want_dir=False)
            if not replica_entry.exists() or not filecmp.cmp(source_entry, replica_entry, shallow=False):
                self._copy_file(source_entry, replica_entry)
        else:
            self._log.warning("Skipped unsupported entry: %s", source_entry)

    def _replace_if_type_differs(self, replica_entry: Path, want_dir: bool) -> None:
        exists = replica_entry.exists() or replica_entry.is_symlink()
        if exists and (replica_entry.is_symlink() or replica_entry.is_dir() != want_dir):
            self._remove(replica_entry)

    def _ensure_directory(self, path: Path) -> None:
        if not path.is_dir():
            path.mkdir(parents=True)
            self._log.info("Created directory: %s", path)

    def _copy_file(self, source: Path, replica: Path) -> None:
        shutil.copy2(source, replica)
        self._log.info("Copied file: %s -> %s", source, replica)

    def _remove(self, path: Path) -> None:
        was_directory = path.is_dir() and not path.is_symlink()
        if was_directory:
            shutil.rmtree(path)
        else:
            path.unlink()
        self._log.info("Removed %s: %s", "directory" if was_directory else "file", path)


def run(config: SyncConfig, log: logging.Logger) -> None:
    synchronizer = FolderSynchronizer(config.source, config.replica, log)
    for number in range(1, config.count + 1):
        log.info("Synchronization %d/%d started", number, config.count)
        try:
            synchronizer.synchronize()
        except OSError as error:
            log.error("Synchronization %d/%d failed: %s", number, config.count, error)
        else:
            log.info("Synchronization %d/%d finished", number, config.count)
        if number < config.count:
            log.info("Waiting before next iteration... ")
            time.sleep(config.interval)


def main(argv: Optional[Sequence[str]] = None) -> None:
    with Logger() as log:
        try:
            config = parse_config(sys.argv[1:] if argv is None else argv)
        except ArgumentError as error:
            log.logger.error("Invalid arguments: %s", error)
            log.logger.error("Usage: sync_folders <source> <replica> <interval> <count> <log_file>")
            return
        if log.add_file(config.log_file):
            run(config, log.logger)


if __name__ == "__main__":
    main()
