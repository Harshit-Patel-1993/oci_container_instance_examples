# OCI Container Instance Logging And Metrics

This repository contains sidecar forwarding patterns for **OCI Container Instances**:

- `generator/`: an HTTP service that creates and appends to a shared log file
- `log_forwarder/`: an optional resource-principal-only log shipper that reads that file and sends lines to OCI Logging
- `metrics_forwarder/`: a resource-principal-only metrics shipper that reads JSON-lines metric records from a shared file and sends them to OCI Monitoring
- `container_instance/`: Terraform that provisions the OCI infrastructure and the container instance runtime

If you want the architecture walkthrough first, read [architecture-walkthrough.md](/home/harpapat/Repos/oci_container_instance_examples/oci-logging-sidecar/blog/architecture-walkthrough.md).

## Architecture

The runtime model is:

1. the generator creates `/mnt/logs/app.log`
2. if enabled, the log forwarder waits for that file
3. if enabled, the log forwarder tails the file and rotated successors
4. if enabled, the log forwarder spools pending batches to disk
5. if enabled, the log forwarder sends batches to OCI Logging with the OCI Go SDK

When enabled, the log forwarder uses:

- resource principal authentication only
- optional rename/create log rotation
- inode-aware file tracking
- on-disk spool and checkpoint state

The metrics forwarder uses the same general model, but ships custom metrics to OCI Monitoring instead of log lines to OCI Logging.

## Repository Layout

```text
generator/          generator container image
log_forwarder/      log forwarder container image
metrics_forwarder/  metrics forwarder container image
container_instance/ Terraform for OCI resources and runtime
blog/               architecture notes
```

## Recommended Path

The primary deployment path is Terraform in [container_instance/](/home/harpapat/Repos/oci_container_instance_examples/oci-logging-sidecar/container_instance).

That Terraform creates:

- a VCN, subnet, route table, security list, and internet gateway
- an optional OCI log group and custom log for the log forwarder
- a dynamic group and IAM policy for the container instance resource principal, including OCI Monitoring write access
- the container instance running the generator and log forwarder, with an optional metrics forwarder sidecar

Use [container_instance/README.md](/home/harpapat/Repos/oci_container_instance_examples/oci-logging-sidecar/container_instance/README.md) for the exact `terraform init`, `plan`, and `apply` steps.

## Local Image Build

Build the generator:

```bash
docker build -t oci-generator ./generator
```

Build the log forwarder:

```bash
docker build -t oci-log-forwarder ./log_forwarder
```

The image is built from a compiled Go binary plus a small Alpine runtime.

Build the metrics forwarder:

```bash
docker build -t oci-metrics-forwarder ./metrics_forwarder
```

The image is built from a compiled Go binary plus a small Alpine runtime.

Build and push all three images to OCIR:

```bash
./scripts/push-ocir-images.sh <ocir-registry> <namespace> [tag]
```

Example:

```bash
./scripts/push-ocir-images.sh uk-london-1.ocir.io axwtwdagdjcl latest
```

## Generator

The generator exposes:

- `GET /health`
- `POST /log`
- `POST /metric`
- `POST /random/logs`
- `POST /random/metrics`

Example:

```bash
docker run --rm \
  -e LOG_FILE_PATH=/logs/app.log \
  -e HTTP_PORT=8080 \
  -e DEFAULT_LOG_LEVEL=INFO \
  -p 8080:8080 \
  -v "$PWD/logs:/logs" \
  oci-generator
```

Then:

```bash
curl -X POST http://localhost:8080/log \
  -H 'Content-Type: application/json' \
  -d '{"level":"INFO","message":"hello from generator"}'
```

Send a test metric:

```bash
curl -X POST http://localhost:8080/metric \
  -H 'Content-Type: application/json' \
  -d '{"name":"request_count","value":1,"dimensions":{"service":"generator"}}'
```

Enable random log generation:

```bash
curl -X POST http://localhost:8080/random/logs \
  -H 'Content-Type: application/json' \
  -d '{"enabled":true}'
```

Enable random metric generation:

```bash
curl -X POST http://localhost:8080/random/metrics \
  -H 'Content-Type: application/json' \
  -d '{"enabled":true}'
```

## Log Forwarder

The log forwarder is optional. When enabled, it requires:

- `LOG_FILE_PATH`
- `OCI_LOG_OBJECT_ID`
- OCI resource principal credentials injected by the runtime

Example container start:

```bash
docker run --rm \
  -e LOG_FILE_PATH=/logs/app.log \
  -e OCI_LOG_OBJECT_ID=ocid1.log.oc1.iad.exampleuniqueID \
  -e OCI_AUTH_TYPE=resource_principal \
  -v "$PWD/logs:/logs" \
  oci-log-forwarder
```

This is only expected to work in a runtime that injects OCI resource principal credentials. A plain local Docker host is not enough.

## Log Forwarder Environment

| Variable | Required | Default | Purpose |
| --- | --- | --- | --- |
| `LOG_FILE_PATH` | yes | none | File to tail and rotate |
| `OCI_LOG_OBJECT_ID` | yes | none | Target OCI custom log OCID |
| `OCI_AUTH_TYPE` | no | `resource_principal` | Compatibility guard; any other value is rejected |
| `OCI_LOG_TYPE` | no | `app.log` | Log batch type sent to OCI Logging |
| `READ_FROM_HEAD` | no | `true` | Read existing content on first startup |
| `LOG_FORWARDER_LOG_LEVEL` | no | `INFO` | Log forwarder log level |
| `LOG_FORWARDER_FLUSH_INTERVAL` | no | `5s` | Batch flush interval |
| `LOG_FORWARDER_CHUNK_LIMIT_SIZE` | no | `1m` | Max batch payload before immediate send |
| `LOG_FORWARDER_QUEUED_BATCH_LIMIT` | no | `64` | Max queued on-disk batches before reads pause |
| `LOG_FORWARDER_DISK_USAGE_LOG_INTERVAL` | no | `5m` | How often the log forwarder logs total size of `app.log` plus rotated siblings |
| `LOGROTATE_ENABLED` | no | `false` | Whether the entrypoint starts the internal logrotate loop |
| `OCI_MAX_BATCH_ENTRIES` | no | `1000` | Max log lines per `PutLogs` request |
| `OCI_MAX_ENTRY_SIZE_BYTES` | no | `900000` | Oversize lines are truncated to this limit |
| `LOG_FORWARDER_STATE_DIR` | no | `/var/lib/oci-log-forwarder/state` | Checkpoint directory |
| `LOG_FORWARDER_SPOOL_DIR` | no | `/var/lib/oci-log-forwarder/spool` | On-disk spool directory |
| `LOG_QUEUE_DIR` | no | `${LOG_FORWARDER_SPOOL_DIR}` | Explicit spool path override |
| `LOGROTATE_FREQUENCY` | no | `hourly` | Rotation cadence when `LOGROTATE_ENABLED=true` |
| `LOGROTATE_SIZE` | no | `50M` | Rotate after this size when `LOGROTATE_ENABLED=true` |
| `LOGROTATE_ROTATE_COUNT` | no | `24` | Number of rotated files to retain when `LOGROTATE_ENABLED=true` |
| `LOGROTATE_INTERVAL_SECONDS` | no | `60` | How often logrotate runs when `LOGROTATE_ENABLED=true` |

## Metrics Forwarder

The metrics forwarder requires:

- `METRIC_FILE_PATH`
- `OCI_MONITORING_NAMESPACE`
- `OCI_MONITORING_COMPARTMENT_ID`
- OCI resource principal credentials injected by the runtime

The metrics source file is newline-delimited JSON. Each line should be a JSON object with at least:

- `name`
- `value`

The metrics forwarder also accepts optional fields such as `timestamp`, `dimensions`, `metadata`, `resource_group`, `namespace`, and `compartment_id`.

Example metric line:

```json
{"name":"request_count","value":1,"dimensions":{"service":"generator","route":"/log"}}
```

Example container start:

```bash
docker run --rm \
  -e METRIC_FILE_PATH=/metrics/metrics.jsonl \
  -e OCI_MONITORING_NAMESPACE=my_sidecar_metrics \
  -e OCI_MONITORING_COMPARTMENT_ID=ocid1.compartment.oc1..exampleuniqueID \
  -e OCI_AUTH_TYPE=resource_principal \
  -v "$PWD/metrics:/metrics" \
  oci-metrics-forwarder
```

This is only expected to work in a runtime that injects OCI resource principal credentials. A plain local Docker host is not enough.
The forwarder posts metrics to the OCI telemetry ingestion endpoint, not the standard Monitoring query endpoint. By default it derives that ingestion endpoint from `OCI_REGION`, and you can override it with `OCI_MONITORING_INGESTION_ENDPOINT` if your environment needs an explicit value.

## Metrics Forwarder Environment

| Variable | Required | Default | Purpose |
| --- | --- | --- | --- |
| `METRIC_FILE_PATH` | yes | none | JSON-lines metric file to tail and rotate |
| `OCI_REGION` | no | runtime-derived | Region used to resolve the OCI telemetry ingestion endpoint |
| `OCI_MONITORING_NAMESPACE` | yes | none | Default OCI Monitoring namespace for emitted metrics |
| `OCI_MONITORING_COMPARTMENT_ID` | yes | none | Default compartment OCID for emitted metrics |
| `OCI_MONITORING_RESOURCE_GROUP` | no | none | Optional default OCI Monitoring resource group |
| `OCI_MONITORING_INGESTION_ENDPOINT` | no | derived from `OCI_REGION` | Explicit OCI telemetry ingestion endpoint override |
| `OCI_AUTH_TYPE` | no | `resource_principal` | Compatibility guard; any other value is rejected |
| `READ_FROM_HEAD` | no | `true` | Read existing content on first startup |
| `METRICS_FORWARDER_LOG_LEVEL` | no | `INFO` | Metrics forwarder log level |
| `METRICS_FORWARDER_FLUSH_INTERVAL` | no | `5s` | Metric batch flush interval |
| `METRICS_FORWARDER_CHUNK_LIMIT_SIZE` | no | `1m` | Max metric batch payload before immediate send |
| `METRICS_FORWARDER_QUEUED_BATCH_LIMIT` | no | `64` | Max queued on-disk metric batches before reads pause |
| `METRICS_FORWARDER_DISK_USAGE_LOG_INTERVAL` | no | `5m` | How often the metrics forwarder logs total size of the source metric files plus rotated siblings |
| `METRICS_FORWARDER_STATE_DIR` | no | `/var/lib/oci-metrics-forwarder/state` | Checkpoint directory |
| `METRICS_FORWARDER_SPOOL_DIR` | no | `/var/lib/oci-metrics-forwarder/spool` | On-disk spool directory |
| `METRIC_QUEUE_DIR` | no | `${METRICS_FORWARDER_SPOOL_DIR}` | Explicit metric spool path override |
| `METRIC_STATE_FILE` | no | `${METRICS_FORWARDER_STATE_DIR}/input.json` | Explicit metric tracker state path override |
| `METRIC_POLL_INTERVAL_SECONDS` | no | `1` | How often to poll for new metric lines |
| `OCI_MAX_BATCH_ENTRIES` | no | `50` | Max metric records per `post_metric_data` call |
| `LOGROTATE_ENABLED` | no | `false` | Whether the entrypoint starts the internal metric-file logrotate loop |
| `LOGROTATE_FREQUENCY` | no | `hourly` | Rotation cadence when `LOGROTATE_ENABLED=true` |
| `LOGROTATE_SIZE` | no | `50M` | Rotate after this size when `LOGROTATE_ENABLED=true` |
| `LOGROTATE_ROTATE_COUNT` | no | `24` | Number of rotated metric files to retain when `LOGROTATE_ENABLED=true` |
| `LOGROTATE_INTERVAL_SECONDS` | no | `60` | How often logrotate runs when `LOGROTATE_ENABLED=true` |

## Current Contract

- The generator creates the shared log file.
- When enabled, the log forwarder waits for that file indefinitely.
- The log forwarder never creates the shared log file itself.
- When enabled, the log forwarder sends raw lines as-is; it does not parse structured logs before ingestion.
- The metrics forwarder reads newline-delimited JSON metric records from a shared file and forwards them to OCI Monitoring.
