#!/usr/bin/env python3
from __future__ import annotations

import json
import logging
import os
import signal
import sys
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import oci


LOGGER = logging.getLogger("oci_metrics_forwarder")


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
    level_name = os.environ.get("METRICS_FORWARDER_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format="[metrics-forwarder] %(asctime)s %(levelname)s %(message)s",
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


def resolve_monitoring_ingestion_endpoint(
    client: oci.monitoring.MonitoringClient,
    region: str | None,
) -> str:
    explicit_endpoint = os.environ.get("OCI_MONITORING_INGESTION_ENDPOINT", "").strip()
    if explicit_endpoint:
        return explicit_endpoint

    current_endpoint = str(getattr(client.base_client, "endpoint", "") or "").strip()
    if current_endpoint:
        if "://telemetry-ingestion." in current_endpoint:
            return current_endpoint
        if "://monitoring." in current_endpoint:
            return current_endpoint.replace("://monitoring.", "://telemetry-ingestion.", 1)

    if region:
        return f"https://telemetry-ingestion.{region}.oraclecloud.com"

    raise ValueError(
        "unable to determine OCI Monitoring telemetry ingestion endpoint; "
        "set OCI_REGION or OCI_MONITORING_INGESTION_ENDPOINT"
    )


def build_monitoring_client() -> oci.monitoring.MonitoringClient:
    explicit_region = os.environ.get("OCI_REGION", "").strip() or None
    signer = oci.auth.signers.get_resource_principals_signer()
    region = resolve_region(explicit_region, signer)
    config = {"region": region} if region else {}
    client = oci.monitoring.MonitoringClient(config=config, signer=signer)
    if region:
        client.base_client.set_region(region)
    client.base_client.endpoint = resolve_monitoring_ingestion_endpoint(client, region)
    LOGGER.info("using OCI Monitoring telemetry ingestion endpoint %s", client.base_client.endpoint)
    return client


def parse_metric_timestamp(value: object | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    if isinstance(value, str):
        normalized = value.strip()
        if not normalized:
            return datetime.now(timezone.utc)
        normalized = normalized.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    raise ValueError(f"unsupported timestamp value: {value!r}")


@dataclass
class TrackedFile:
    path: str
    inode: int
    offset: int


@dataclass
class FileReadBatch:
    source_path: str
    inode: int
    end_offset: int
    lines: list[str]


@dataclass
class MetricRecord:
    name: str
    value: float
    timestamp: str
    dimensions: dict[str, str]
    resource_group: str | None
    metadata: dict[str, str]
    namespace: str
    compartment_id: str


@dataclass
class MetricBatch:
    source_path: str
    inode: int
    end_offset: int
    records: list[MetricRecord]


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

    def write_batch(self, batch: MetricBatch) -> None:
        payload = {
            "source_path": batch.source_path,
            "inode": batch.inode,
            "end_offset": batch.end_offset,
            "records": [asdict(record) for record in batch.records],
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        batch_name = f"{time.time_ns():020d}-{uuid.uuid4().hex}.json"
        tmp_path = self.spool_dir / f".{batch_name}.tmp"
        final_path = self.spool_dir / batch_name
        tmp_path.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp_path, final_path)

    def read_batch(self, path: Path) -> MetricBatch:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return MetricBatch(
            source_path=str(payload["source_path"]),
            inode=int(payload["inode"]),
            end_offset=int(payload["end_offset"]),
            records=[MetricRecord(**record) for record in payload["records"]],
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
                    "current file inode=%s moved but no rotated file was found; unread metric data may be lost",
                    current_entry.inode,
                )
                self.tracked_files.remove(current_entry)
            current_entry = None

        if current_entry is None:
            existing = next((tracked for tracked in self.tracked_files if tracked.inode == current_inode), None)
            if existing is None:
                self.tracked_files.append(TrackedFile(path=current_path, inode=current_inode, offset=0))
            else:
                existing.path = current_path
                existing.offset = min(existing.offset, current_size)
        else:
            if current_size < current_entry.offset:
                LOGGER.warning(
                    "current metric file %s shrank from %s bytes to %s bytes; rewinding tracked offset",
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
                    "tracked rotated metric file inode=%s disappeared before it was fully consumed",
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

    def read_batch(self, max_lines: int, max_bytes: int) -> FileReadBatch | None:
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
                        "tracked metric file inode=%s disappeared before it could be consumed",
                        tracked.inode,
                    )
                    self.tracked_files.remove(tracked)
                    self._persist_state()
                    continue
                tracked.path = str(found)
                self._persist_state()
                return self.read_batch(max_lines, max_bytes)

            if lines:
                return FileReadBatch(
                    source_path=tracked.path,
                    inode=tracked.inode,
                    end_offset=end_offset,
                    lines=lines,
                )

            self._drop_if_drained(tracked)

        return None

    def mark_consumed(self, source_path: str, inode: int, end_offset: int) -> None:
        tracked = next(
            (item for item in self.tracked_files if item.inode == inode and item.path == source_path),
            None,
        )
        if tracked is None:
            tracked = next((item for item in self.tracked_files if item.inode == inode), None)
        if tracked is None:
            tracked = TrackedFile(path=source_path, inode=inode, offset=end_offset)
            self.tracked_files.append(tracked)
        else:
            tracked.path = source_path
            tracked.offset = max(tracked.offset, end_offset)

        current_path = str(self.path)
        rotated = [item for item in self.tracked_files if item.path != current_path]
        current = [item for item in self.tracked_files if item.path == current_path]
        self.tracked_files = rotated + current
        self._persist_state()


class OciMetricsForwarder:
    def __init__(self) -> None:
        self.client = build_monitoring_client()
        self.metric_namespace = getenv_required("OCI_MONITORING_NAMESPACE")
        self.compartment_id = getenv_required("OCI_MONITORING_COMPARTMENT_ID")
        self.resource_group = os.environ.get("OCI_MONITORING_RESOURCE_GROUP", "").strip() or None
        self.flush_interval_seconds = parse_duration_seconds(os.environ.get("METRICS_FORWARDER_FLUSH_INTERVAL", "5s"))
        self.chunk_limit_bytes = parse_size_bytes(os.environ.get("METRICS_FORWARDER_CHUNK_LIMIT_SIZE", "1m"))
        self.max_queued_batches = int(os.environ.get("METRICS_FORWARDER_QUEUED_BATCH_LIMIT", "64"))
        self.max_batch_entries = int(os.environ.get("OCI_MAX_BATCH_ENTRIES", "50"))
        self.poll_interval_seconds = float(os.environ.get("METRIC_POLL_INTERVAL_SECONDS", "1"))
        self.disk_usage_log_interval_seconds = parse_duration_seconds(
            os.environ.get("METRICS_FORWARDER_DISK_USAGE_LOG_INTERVAL", "5m")
        )
        self.retry_initial_seconds = float(os.environ.get("OCI_RETRY_INITIAL_SECONDS", "1"))
        self.retry_max_seconds = float(os.environ.get("OCI_RETRY_MAX_SECONDS", "30"))

        metric_file_path = Path(getenv_required("METRIC_FILE_PATH"))
        state_dir = Path(os.environ.get("METRICS_FORWARDER_STATE_DIR", "/var/lib/oci-metrics-forwarder/state"))
        spool_dir = Path(os.environ.get("METRICS_FORWARDER_SPOOL_DIR", "/var/lib/oci-metrics-forwarder/spool"))
        state_path = Path(os.environ.get("METRIC_STATE_FILE", str(state_dir / "input.json")))
        self.spool_queue = SpoolQueue(Path(os.environ.get("METRIC_QUEUE_DIR", str(spool_dir))))
        read_from_head = parse_bool(os.environ.get("READ_FROM_HEAD", "true"))
        recovered_offsets = self.spool_queue.recover_offsets()
        self.file_tracker = FileTracker(metric_file_path, state_path, read_from_head, recovered_offsets)
        self.stop_requested = False
        self.last_flush_at = 0.0
        self.next_disk_usage_log_at = 0.0

    def request_stop(self, signum: int, _frame: object) -> None:
        LOGGER.info("received signal %s; draining metric spool before exit", signum)
        self.stop_requested = True

    def start(self) -> int:
        signal.signal(signal.SIGINT, self.request_stop)
        signal.signal(signal.SIGTERM, self.request_stop)

        LOGGER.info("starting OCI metrics forwarder")
        LOGGER.info("source file: %s", self.file_tracker.path)
        LOGGER.info("OCI auth mode: resource_principal")
        LOGGER.info("OCI Monitoring namespace: %s", self.metric_namespace)
        LOGGER.info("OCI Monitoring compartment id: %s", self.compartment_id)
        self.log_metric_file_usage_if_due(force=True)

        while not self.stop_requested:
            self.log_metric_file_usage_if_due()
            self.flush_spool()

            if self.spool_queue.count() < self.max_queued_batches:
                file_batch = self.file_tracker.read_batch(
                    max_lines=self.max_batch_entries,
                    max_bytes=self.chunk_limit_bytes,
                )
                if file_batch is not None:
                    metric_batch = self.build_metric_batch(file_batch)
                    self.file_tracker.mark_consumed(file_batch.source_path, file_batch.inode, file_batch.end_offset)
                    if metric_batch is None:
                        continue
                    self.spool_queue.write_batch(metric_batch)
                    continue

            time.sleep(self.poll_interval_seconds)

        self.flush_spool(stop_when_empty=True)
        return 0

    def build_metric_batch(self, file_batch: FileReadBatch) -> MetricBatch | None:
        records: list[MetricRecord] = []
        for line in file_batch.lines:
            record = self.parse_metric_line(line)
            if record is None:
                continue
            records.append(record)

        if not records:
            LOGGER.warning("skipping fully invalid metric batch from %s", file_batch.source_path)
            return None

        return MetricBatch(
            source_path=file_batch.source_path,
            inode=file_batch.inode,
            end_offset=file_batch.end_offset,
            records=records,
        )

    def parse_metric_line(self, line: str) -> MetricRecord | None:
        try:
            payload = json.loads(line)
            if not isinstance(payload, dict):
                raise ValueError("metric line must be a JSON object")

            name = str(payload["name"]).strip()
            if not name:
                raise ValueError("metric name must not be empty")

            value = float(payload["value"])
            timestamp = parse_metric_timestamp(payload.get("timestamp")).isoformat()
            dimensions = {
                str(key): str(value)
                for key, value in dict(payload.get("dimensions", {})).items()
            }
            metadata = {
                str(key): str(value)
                for key, value in dict(payload.get("metadata", {})).items()
            }
            resource_group = payload.get("resource_group", self.resource_group)
            if resource_group is not None:
                resource_group = str(resource_group).strip() or None
            namespace = str(payload.get("namespace", self.metric_namespace)).strip() or self.metric_namespace
            compartment_id = str(payload.get("compartment_id", self.compartment_id)).strip() or self.compartment_id
            return MetricRecord(
                name=name,
                value=value,
                timestamp=timestamp,
                dimensions=dimensions,
                resource_group=resource_group,
                metadata=metadata,
                namespace=namespace,
                compartment_id=compartment_id,
            )
        except Exception as exc:
            LOGGER.warning("dropping invalid metric line %r: %s", line, exc)
            return None

    def log_metric_file_usage_if_due(self, force: bool = False) -> None:
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
            "metric files consume %s (%s) across %s file(s) under %s",
            total_bytes,
            format_size_bytes(total_bytes),
            file_count,
            self.file_tracker.path.parent,
        )
        self.next_disk_usage_log_at = now + self.disk_usage_log_interval_seconds

    def flush_spool(self, stop_when_empty: bool = False) -> None:
        backoff = self.retry_initial_seconds

        while True:
            batch_paths = self.spool_queue.list_batches()
            if not batch_paths:
                if stop_when_empty:
                    LOGGER.info("drained all pending metric batches")
                return

            now = time.monotonic()
            if not stop_when_empty and self.last_flush_at and now - self.last_flush_at < self.flush_interval_seconds:
                return

            batch_path = batch_paths[0]
            batch = self.spool_queue.read_batch(batch_path)
            try:
                self.put_batch(batch)
            except Exception as exc:
                LOGGER.exception("failed to push %s metric(s) to OCI Monitoring: %s", len(batch.records), exc)
                time.sleep(backoff)
                backoff = min(backoff * 2, self.retry_max_seconds)
                return

            batch_path.unlink()
            self.last_flush_at = time.monotonic()
            backoff = self.retry_initial_seconds

    def put_batch(self, batch: MetricBatch) -> None:
        metric_data = []
        for record in batch.records:
            datapoint = oci.monitoring.models.Datapoint(
                timestamp=parse_metric_timestamp(record.timestamp),
                value=record.value,
            )
            metric_data.append(
                oci.monitoring.models.MetricDataDetails(
                    namespace=record.namespace,
                    compartment_id=record.compartment_id,
                    name=record.name,
                    dimensions=record.dimensions,
                    metadata=record.metadata,
                    resource_group=record.resource_group,
                    datapoints=[datapoint],
                )
            )

        details = oci.monitoring.models.PostMetricDataDetails(metric_data=metric_data)
        self.client.post_metric_data(details)
        LOGGER.info("pushed %s metric(s) to OCI Monitoring", len(batch.records))


def main() -> int:
    configure_logging()
    try:
        forwarder = OciMetricsForwarder()
        return forwarder.start()
    except Exception as exc:
        LOGGER.exception("metrics forwarder failed: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
