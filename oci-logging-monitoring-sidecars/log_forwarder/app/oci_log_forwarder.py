#!/usr/bin/env python3
from __future__ import annotations

import json
import logging
import os
import signal
import socket
import sys
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import oci


LOGGER = logging.getLogger("oci_log_forwarder")


def getenv_required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"missing required environment variable: {name}")
    return value


def parse_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def parse_duration_seconds(value: str) -> float:
    raw = value.strip().lower()
    if raw.endswith("ms"):
        return float(raw[:-2]) / 1000.0
    if raw.endswith("s"):
        return float(raw[:-1])
    if raw.endswith("m"):
        return float(raw[:-1]) * 60.0
    if raw.endswith("h"):
        return float(raw[:-1]) * 3600.0
    return float(raw)


def parse_size_bytes(value: str) -> int:
    raw = value.strip().lower()
    multipliers = {
        "k": 1024,
        "kb": 1024,
        "m": 1024 * 1024,
        "mb": 1024 * 1024,
        "g": 1024 * 1024 * 1024,
        "gb": 1024 * 1024 * 1024,
    }
    for suffix, multiplier in multipliers.items():
        if raw.endswith(suffix):
            return int(float(raw[: -len(suffix)]) * multiplier)
    return int(raw)


def format_size_bytes(size_bytes: int) -> str:
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    size = float(size_bytes)
    unit = units[0]
    for unit in units:
        if size < 1024.0 or unit == units[-1]:
            break
        size /= 1024.0
    if unit == "B":
        return f"{int(size)} {unit}"
    return f"{size:.1f} {unit}"


def configure_logging() -> None:
    level_name = os.environ.get("LOG_FORWARDER_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format="[log-forwarder] %(asctime)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )


def resolve_region(explicit_region: str | None, signer: object | None) -> str | None:
    if explicit_region:
        return explicit_region

    for name in (
        "OCI_REGION",
        "OCI_RESOURCE_PRINCIPAL_REGION",
        "OCI_RESOURCE_PRINCIPAL_REGION_FOR_LEAF_RESOURCE",
    ):
        value = os.environ.get(name, "").strip()
        if value:
            return value

    signer_region = getattr(signer, "region", None)
    if signer_region:
        return str(signer_region)

    return None


def build_logging_client() -> oci.loggingingestion.LoggingClient:
    explicit_region = os.environ.get("OCI_REGION", "").strip() or None
    signer = oci.auth.signers.get_resource_principals_signer()
    region = resolve_region(explicit_region, signer)
    config = {"region": region} if region else {}
    client = oci.loggingingestion.LoggingClient(config=config, signer=signer)
    if region:
        client.base_client.set_region(region)
    return client


@dataclass
class TrackedFile:
    path: str
    inode: int
    offset: int


@dataclass
class ReadBatch:
    source_path: str
    inode: int
    end_offset: int
    lines: list[str]


class SpoolQueue:
    def __init__(self, spool_dir: Path) -> None:
        self.spool_dir = spool_dir
        self.spool_dir.mkdir(parents=True, exist_ok=True)

    def recover_offsets(self) -> list[TrackedFile]:
        tracked: dict[int, TrackedFile] = {}
        for batch_path in self.list_batches():
            try:
                payload = json.loads(batch_path.read_text(encoding="utf-8"))
                inode = int(payload["inode"])
                end_offset = int(payload["end_offset"])
                source_path = str(payload["source_path"])
            except Exception as exc:
                LOGGER.warning("ignoring unreadable spool file %s: %s", batch_path, exc)
                continue

            existing = tracked.get(inode)
            if existing is None or end_offset > existing.offset:
                tracked[inode] = TrackedFile(path=source_path, inode=inode, offset=end_offset)
        return list(tracked.values())

    def count(self) -> int:
        return len(self.list_batches())

    def list_batches(self) -> list[Path]:
        return sorted(self.spool_dir.glob("*.json"))

    def write_batch(self, batch: ReadBatch) -> None:
        payload = {
            "source_path": batch.source_path,
            "inode": batch.inode,
            "end_offset": batch.end_offset,
            "lines": batch.lines,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        batch_name = f"{time.time_ns():020d}-{uuid.uuid4().hex}.json"
        tmp_path = self.spool_dir / f".{batch_name}.tmp"
        final_path = self.spool_dir / batch_name
        tmp_path.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp_path, final_path)

    def read_batch(self, path: Path) -> ReadBatch:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return ReadBatch(
            source_path=str(payload["source_path"]),
            inode=int(payload["inode"]),
            end_offset=int(payload["end_offset"]),
            lines=list(payload["lines"]),
        )


class FileTracker:
    def __init__(self, path: Path, state_path: Path, read_from_head: bool, recovered_offsets: list[TrackedFile]) -> None:
        self.path = path
        self.state_path = state_path
        self.read_from_head = read_from_head
        self.tracked_files: list[TrackedFile] = []
        self._load_state()
        self._merge_recovered_offsets(recovered_offsets)
        self._ensure_state()

    def _load_state(self) -> None:
        if not self.state_path.exists():
            return
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
            self.tracked_files = [
                TrackedFile(
                    path=str(item["path"]),
                    inode=int(item["inode"]),
                    offset=int(item["offset"]),
                )
                for item in payload.get("tracked_files", [])
            ]
        except Exception as exc:
            LOGGER.warning("ignoring unreadable state file %s: %s", self.state_path, exc)

    def _merge_recovered_offsets(self, recovered_offsets: list[TrackedFile]) -> None:
        merged: dict[int, TrackedFile] = {tracked.inode: tracked for tracked in self.tracked_files}
        for recovered in recovered_offsets:
            existing = merged.get(recovered.inode)
            if existing is None:
                merged[recovered.inode] = recovered
                continue
            existing.offset = max(existing.offset, recovered.offset)
            if existing.path != recovered.path:
                existing.path = recovered.path
        self.tracked_files = list(merged.values())

    def _persist_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"tracked_files": [asdict(tracked) for tracked in self.tracked_files]}
        tmp_path = self.state_path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp_path, self.state_path)

    def _ensure_log_file(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.touch()

    def _find_path_by_inode(self, inode: int) -> Path | None:
        for candidate in self.path.parent.glob(f"{self.path.name}*"):
            try:
                if candidate.stat().st_ino == inode:
                    return candidate
            except FileNotFoundError:
                continue
        return None

    def _ensure_state(self) -> None:
        self._ensure_log_file()
        current_stat = self.path.stat()
        current_inode = current_stat.st_ino
        current_size = current_stat.st_size

        resolved: list[TrackedFile] = []
        current_present = False

        for tracked in self.tracked_files:
            resolved_path = Path(tracked.path)
            try:
                if resolved_path.exists() and resolved_path.stat().st_ino == tracked.inode:
                    pass
                else:
                    found = self._find_path_by_inode(tracked.inode)
                    if found is None:
                        LOGGER.warning(
                            "dropping unreadable tracked file inode=%s offset=%s path=%s",
                            tracked.inode,
                            tracked.offset,
                            tracked.path,
                        )
                        continue
                    resolved_path = found
            except FileNotFoundError:
                continue

            tracked.path = str(resolved_path)
            if tracked.inode == current_inode:
                tracked.path = str(self.path)
                tracked.offset = min(tracked.offset, current_size)
                current_present = True
            resolved.append(tracked)

        if not resolved:
            initial_offset = 0 if self.read_from_head else current_size
            resolved.append(TrackedFile(path=str(self.path), inode=current_inode, offset=initial_offset))
            current_present = True

        if not current_present:
            resolved.append(TrackedFile(path=str(self.path), inode=current_inode, offset=0))

        rotated = [tracked for tracked in resolved if tracked.path != str(self.path)]
        current = [tracked for tracked in resolved if tracked.path == str(self.path)]
        self.tracked_files = rotated + current
        self._persist_state()

    def _refresh_current_file(self) -> None:
        self._ensure_log_file()
        current_stat = self.path.stat()
        current_inode = current_stat.st_ino
        current_size = current_stat.st_size
        current_path = str(self.path)

        current_entry = next((tracked for tracked in self.tracked_files if tracked.path == current_path), None)
        if current_entry is not None and current_entry.inode != current_inode:
            found = self._find_path_by_inode(current_entry.inode)
            if found is not None:
                current_entry.path = str(found)
            else:
                LOGGER.warning(
                    "current file inode=%s moved but no rotated file was found; unread data may be lost",
                    current_entry.inode,
                )
                self.tracked_files.remove(current_entry)
            current_entry = None

        if current_entry is None:
            existing = next(
                (tracked for tracked in self.tracked_files if tracked.inode == current_inode),
                None,
            )
            if existing is None:
                self.tracked_files.append(TrackedFile(path=current_path, inode=current_inode, offset=0))
            else:
                existing.path = current_path
                existing.offset = min(existing.offset, current_size)
        else:
            if current_size < current_entry.offset:
                LOGGER.warning(
                    "current file %s shrank from %s bytes to %s bytes; rewinding tracked offset",
                    current_path,
                    current_entry.offset,
                    current_size,
                )
                current_entry.offset = current_size

        rotated = [tracked for tracked in self.tracked_files if tracked.path != current_path]
        current = [tracked for tracked in self.tracked_files if tracked.path == current_path]
        self.tracked_files = rotated + current
        self._persist_state()

    def _drop_if_drained(self, tracked: TrackedFile) -> bool:
        if tracked.path == str(self.path):
            return False

        candidate = Path(tracked.path)
        try:
            size = candidate.stat().st_size
        except FileNotFoundError:
            found = self._find_path_by_inode(tracked.inode)
            if found is None:
                LOGGER.warning(
                    "tracked rotated file inode=%s disappeared before it was fully consumed",
                    tracked.inode,
                )
                self.tracked_files.remove(tracked)
                self._persist_state()
                return True
            tracked.path = str(found)
            size = found.stat().st_size

        if tracked.offset >= size:
            self.tracked_files.remove(tracked)
            self._persist_state()
            return True
        return False

    def read_batch(self, max_lines: int, max_bytes: int) -> ReadBatch | None:
        self._refresh_current_file()

        for tracked in list(self.tracked_files):
            if self._drop_if_drained(tracked):
                continue

            candidate = Path(tracked.path)
            try:
                with candidate.open("r", encoding="utf-8", errors="replace") as handle:
                    handle.seek(tracked.offset)
                    lines: list[str] = []
                    total_bytes = 0
                    end_offset = tracked.offset

                    while len(lines) < max_lines:
                        start_offset = handle.tell()
                        raw_line = handle.readline()
                        if not raw_line:
                            break

                        line = raw_line.rstrip("\r\n")
                        line_bytes = len(line.encode("utf-8"))
                        if lines and total_bytes + line_bytes > max_bytes:
                            handle.seek(start_offset)
                            break

                        lines.append(line)
                        total_bytes += line_bytes
                        end_offset = handle.tell()
            except FileNotFoundError:
                found = self._find_path_by_inode(tracked.inode)
                if found is None:
                    LOGGER.warning(
                        "tracked file inode=%s disappeared before it could be consumed",
                        tracked.inode,
                    )
                    self.tracked_files.remove(tracked)
                    self._persist_state()
                    continue
                tracked.path = str(found)
                self._persist_state()
                return self.read_batch(max_lines, max_bytes)

            if lines:
                return ReadBatch(
                    source_path=tracked.path,
                    inode=tracked.inode,
                    end_offset=end_offset,
                    lines=lines,
                )

            self._drop_if_drained(tracked)

        return None

    def mark_spooled(self, batch: ReadBatch) -> None:
        tracked = next(
            (
                item
                for item in self.tracked_files
                if item.inode == batch.inode and item.path == batch.source_path
            ),
            None,
        )
        if tracked is None:
            tracked = next((item for item in self.tracked_files if item.inode == batch.inode), None)
        if tracked is None:
            tracked = TrackedFile(path=batch.source_path, inode=batch.inode, offset=batch.end_offset)
            self.tracked_files.append(tracked)
        else:
            tracked.path = batch.source_path
            tracked.offset = max(tracked.offset, batch.end_offset)

        current_path = str(self.path)
        rotated = [item for item in self.tracked_files if item.path != current_path]
        current = [item for item in self.tracked_files if item.path == current_path]
        self.tracked_files = rotated + current
        self._persist_state()


class OciLogForwarder:
    def __init__(self) -> None:
        self.client = build_logging_client()
        self.log_id = getenv_required("OCI_LOG_OBJECT_ID")
        self.log_source = os.environ.get("OCI_SOURCE", socket.gethostname())
        self.log_subject = os.environ.get("OCI_SUBJECT", getenv_required("LOG_FILE_PATH"))
        self.log_type = os.environ.get("OCI_LOG_TYPE", "app.log")
        self.flush_interval_seconds = parse_duration_seconds(os.environ.get("LOG_FORWARDER_FLUSH_INTERVAL", "5s"))
        self.chunk_limit_bytes = parse_size_bytes(os.environ.get("LOG_FORWARDER_CHUNK_LIMIT_SIZE", "1m"))
        self.max_queued_batches = int(os.environ.get("LOG_FORWARDER_QUEUED_BATCH_LIMIT", "64"))
        self.max_batch_entries = int(os.environ.get("OCI_MAX_BATCH_ENTRIES", "1000"))
        self.max_entry_size_bytes = int(os.environ.get("OCI_MAX_ENTRY_SIZE_BYTES", "900000"))
        self.poll_interval_seconds = float(os.environ.get("LOG_POLL_INTERVAL_SECONDS", "1"))
        self.disk_usage_log_interval_seconds = parse_duration_seconds(
            os.environ.get("LOG_FORWARDER_DISK_USAGE_LOG_INTERVAL", "5m")
        )
        self.retry_initial_seconds = float(os.environ.get("OCI_RETRY_INITIAL_SECONDS", "1"))
        self.retry_max_seconds = float(os.environ.get("OCI_RETRY_MAX_SECONDS", "30"))

        log_file_path = Path(getenv_required("LOG_FILE_PATH"))
        state_dir = Path(os.environ.get("LOG_FORWARDER_STATE_DIR", "/var/lib/oci-log-forwarder/state"))
        spool_dir = Path(os.environ.get("LOG_FORWARDER_SPOOL_DIR", "/var/lib/oci-log-forwarder/spool"))
        state_path = Path(os.environ.get("LOG_STATE_FILE", str(state_dir / "input.json")))
        self.spool_queue = SpoolQueue(Path(os.environ.get("LOG_QUEUE_DIR", str(spool_dir))))
        read_from_head = parse_bool(os.environ.get("READ_FROM_HEAD", "true"))
        recovered_offsets = self.spool_queue.recover_offsets()
        self.file_tracker = FileTracker(log_file_path, state_path, read_from_head, recovered_offsets)
        self.stop_requested = False
        self.last_flush_at = 0.0
        self.next_disk_usage_log_at = 0.0

    def request_stop(self, signum: int, _frame: object) -> None:
        LOGGER.info("received signal %s; draining disk spool before exit", signum)
        self.stop_requested = True

    def start(self) -> int:
        signal.signal(signal.SIGINT, self.request_stop)
        signal.signal(signal.SIGTERM, self.request_stop)

        LOGGER.info("starting OCI log forwarder")
        LOGGER.info("source file: %s", self.file_tracker.path)
        LOGGER.info("OCI auth mode: resource_principal")
        LOGGER.info("OCI log object id: %s", self.log_id)
        self.log_log_storage_usage_if_due(force=True)

        while not self.stop_requested:
            self.log_log_storage_usage_if_due()
            self.flush_spool()

            if self.spool_queue.count() < self.max_queued_batches:
                batch = self.file_tracker.read_batch(
                    max_lines=self.max_batch_entries,
                    max_bytes=self.chunk_limit_bytes,
                )
                if batch is not None:
                    batch.lines = [self.normalize_line(line) for line in batch.lines]
                    self.spool_queue.write_batch(batch)
                    self.file_tracker.mark_spooled(batch)
                    continue

            time.sleep(self.poll_interval_seconds)

        self.flush_spool(stop_when_empty=True)
        return 0

    def log_log_storage_usage_if_due(self, force: bool = False) -> None:
        if self.disk_usage_log_interval_seconds <= 0:
            return

        now = time.monotonic()
        if not force and now < self.next_disk_usage_log_at:
            return

        total_bytes = 0
        file_count = 0
        for candidate in sorted(self.file_tracker.path.parent.glob(f"{self.file_tracker.path.name}*")):
            try:
                if not candidate.is_file():
                    continue
                total_bytes += candidate.stat().st_size
                file_count += 1
            except FileNotFoundError:
                continue

        LOGGER.info(
            "log files consume %s (%s) across %s file(s) under %s",
            total_bytes,
            format_size_bytes(total_bytes),
            file_count,
            self.file_tracker.path.parent,
        )
        self.next_disk_usage_log_at = now + self.disk_usage_log_interval_seconds

    def normalize_line(self, line: str) -> str:
        data = line.encode("utf-8")
        if len(data) <= self.max_entry_size_bytes:
            return line

        marker = " [truncated]"
        allowed = max(0, self.max_entry_size_bytes - len(marker.encode("utf-8")))
        truncated = data[:allowed].decode("utf-8", errors="ignore")
        LOGGER.warning("truncating oversized log entry from %s bytes to %s bytes", len(data), self.max_entry_size_bytes)
        return f"{truncated}{marker}"

    def flush_spool(self, stop_when_empty: bool = False) -> None:
        backoff = self.retry_initial_seconds

        while True:
            batch_paths = self.spool_queue.list_batches()
            if not batch_paths:
                if stop_when_empty:
                    LOGGER.info("drained all pending log batches")
                return

            now = time.monotonic()
            if not stop_when_empty and self.last_flush_at and now - self.last_flush_at < self.flush_interval_seconds:
                return

            batch_path = batch_paths[0]
            batch = self.spool_queue.read_batch(batch_path)
            try:
                self.put_batch(batch)
            except Exception as exc:
                LOGGER.exception("failed to push %s log lines to OCI Logging: %s", len(batch.lines), exc)
                if stop_when_empty and self.stop_requested:
                    time.sleep(backoff)
                else:
                    time.sleep(backoff)
                backoff = min(backoff * 2, self.retry_max_seconds)
                return

            batch_path.unlink()
            self.last_flush_at = time.monotonic()
            backoff = self.retry_initial_seconds

    def put_batch(self, batch: ReadBatch) -> None:
        timestamp = datetime.now(timezone.utc)
        entries = [
            oci.loggingingestion.models.LogEntry(
                data=line,
                id=str(uuid.uuid4()),
                time=timestamp,
            )
            for line in batch.lines
        ]
        log_entry_batch = oci.loggingingestion.models.LogEntryBatch(
            entries=entries,
            source=self.log_source,
            type=self.log_type,
            subject=self.log_subject,
            defaultlogentrytime=timestamp,
        )
        put_logs_details = oci.loggingingestion.models.PutLogsDetails(
            specversion="1.0",
            log_entry_batches=[log_entry_batch],
        )
        self.client.put_logs(self.log_id, put_logs_details)
        LOGGER.info("pushed %s log lines to OCI Logging", len(batch.lines))


def main() -> int:
    configure_logging()
    try:
        log_forwarder = OciLogForwarder()
        return log_forwarder.start()
    except Exception as exc:
        LOGGER.exception("log forwarder failed: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
