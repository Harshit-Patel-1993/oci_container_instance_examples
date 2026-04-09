# OCI Container Instance Logging

This repository contains a two-container logging setup for **OCI Container Instances**:

- `generator/`: an HTTP service that creates and appends to a shared log file
- `forwarder/`: a resource-principal-only log shipper that reads that file and sends lines to OCI Logging
- `container_instance/`: Terraform that provisions the OCI infrastructure and the container instance runtime

If you want the architecture walkthrough first, read [architecture-walkthrough.md](/home/harpapat/Repos/oci_container_instance_examples/oci-logging-sidecar/blog/architecture-walkthrough.md).

## Architecture

The runtime model is:

1. the generator creates `/mnt/logs/app.log`
2. the forwarder waits for that file
3. the forwarder tails the file and rotated successors
4. the forwarder spools pending batches to disk
5. the forwarder sends batches to OCI Logging with the OCI Python SDK

The forwarder uses:

- resource principal authentication only
- rename/create log rotation
- inode-aware file tracking
- on-disk spool and checkpoint state

## Repository Layout

```text
generator/          generator container image
forwarder/          forwarder container image
container_instance/ Terraform for OCI resources and runtime
blog/               architecture notes
```

## Recommended Path

The primary deployment path is Terraform in [container_instance/](/home/harpapat/Repos/container-instance-oci-logging/container_instance).

That Terraform creates:

- a VCN, subnet, route table, security list, and internet gateway
- an OCI log group and custom log
- a dynamic group and IAM policy for the container instance resource principal
- the container instance running both containers

Use [container_instance/README.md](/home/harpapat/Repos/container-instance-oci-logging/container_instance/README.md) for the exact `terraform init`, `plan`, and `apply` steps.

## Local Image Build

Build the generator:

```bash
docker build -t oci-log-generator ./generator
```

Build the forwarder:

```bash
docker build -t oci-log-forwarder ./forwarder
```

## Generator

The generator exposes:

- `GET /health`
- `POST /log`

Example:

```bash
docker run --rm \
  -e LOG_FILE_PATH=/logs/app.log \
  -e HTTP_PORT=8080 \
  -e DEFAULT_LOG_LEVEL=INFO \
  -p 8080:8080 \
  -v "$PWD/logs:/logs" \
  oci-log-generator
```

Then:

```bash
curl -X POST http://localhost:8080/log \
  -H 'Content-Type: application/json' \
  -d '{"level":"INFO","message":"hello from generator"}'
```

## Forwarder

The forwarder requires:

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

## Forwarder Environment

| Variable | Required | Default | Purpose |
| --- | --- | --- | --- |
| `LOG_FILE_PATH` | yes | none | File to tail and rotate |
| `OCI_LOG_OBJECT_ID` | yes | none | Target OCI custom log OCID |
| `OCI_AUTH_TYPE` | no | `resource_principal` | Compatibility guard; any other value is rejected |
| `OCI_LOG_TYPE` | no | `app.log` | Log batch type sent to OCI Logging |
| `READ_FROM_HEAD` | no | `true` | Read existing content on first startup |
| `LOG_FORWARDER_LOG_LEVEL` | no | `INFO` | Forwarder log level |
| `LOG_FORWARDER_FLUSH_INTERVAL` | no | `5s` | Batch flush interval |
| `LOG_FORWARDER_CHUNK_LIMIT_SIZE` | no | `1m` | Max batch payload before immediate send |
| `LOG_FORWARDER_QUEUED_BATCH_LIMIT` | no | `64` | Max queued on-disk batches before reads pause |
| `OCI_MAX_BATCH_ENTRIES` | no | `1000` | Max log lines per `PutLogs` request |
| `OCI_MAX_ENTRY_SIZE_BYTES` | no | `900000` | Oversize lines are truncated to this limit |
| `LOG_FORWARDER_STATE_DIR` | no | `/var/lib/oci-log-forwarder/state` | Checkpoint directory |
| `LOG_FORWARDER_SPOOL_DIR` | no | `/var/lib/oci-log-forwarder/spool` | On-disk spool directory |
| `LOG_QUEUE_DIR` | no | `${LOG_FORWARDER_SPOOL_DIR}` | Explicit spool path override |
| `LOGROTATE_FREQUENCY` | no | `hourly` | Rotation cadence |
| `LOGROTATE_SIZE` | no | `50M` | Rotate after this size |
| `LOGROTATE_ROTATE_COUNT` | no | `24` | Number of rotated files to retain |
| `LOGROTATE_INTERVAL_SECONDS` | no | `60` | How often logrotate runs |

## Current Contract

- The generator creates the shared log file.
- The forwarder waits for that file indefinitely.
- The forwarder never creates the shared log file itself.
- The forwarder sends raw lines as-is; it does not parse structured logs before ingestion.
