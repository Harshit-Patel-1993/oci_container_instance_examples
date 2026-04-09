# Understanding This Repository

This document explains the **current** architecture of the repository.

It focuses on what exists today:

- the generator container
- the forwarder container
- the shared storage model
- the OCI resources created by Terraform
- how logs move from a local file into OCI Logging

It does **not** cover older approaches or discarded designs.

---

## 1. Repository Purpose

This repository demonstrates a simple OCI logging pattern:

1. one container writes application logs to a shared file
2. another container reads that file
3. the second container sends those log lines to OCI Logging

The pattern is implemented for **OCI Container Instances** and uses **resource principal authentication** for OCI API access.

---

## 2. Repository Layout

The main directories are:

```text
generator/          HTTP log producer image
forwarder/          OCI log forwarder image
container_instance/ Terraform for OCI infrastructure and runtime
blog/               repository documentation
```

Each directory has a clear role:

- `generator/` produces log lines
- `forwarder/` ships log lines
- `container_instance/` provisions the OCI environment that runs both containers

---

## 3. End-to-End Flow

At a high level, the runtime flow looks like this:

```text
Client --> generator container --> shared log file --> forwarder container --> OCI Logging
```

```mermaid
flowchart LR
    C[Client] --> G[Generator Container]
    G --> F1[/Shared Log File<br>/mnt/logs/app.log/]
    F1 --> FW[Forwarder Container]
    FW --> L[OCI Logging Custom Log]
```

More concretely:

1. the generator receives an HTTP request
2. it appends a formatted line to `/mnt/logs/app.log`
3. the forwarder waits for that file to exist
4. the forwarder tails the file and any rotated successors
5. the forwarder sends batches to OCI Logging with the OCI Python SDK

---

## 4. Generator Container

The generator is a small HTTP service.

Its responsibilities are:

- create the shared log file
- expose a health endpoint
- accept log-writing requests
- append formatted lines to the shared file

### Endpoints

The generator exposes:

- `GET /health`
- `POST /log`

### Log file ownership

The generator is the component that creates the shared log file.

That is an explicit contract in this repository:

- the **generator creates the file**
- the **forwarder waits for it**

### Example request

```bash
curl -X POST http://<host>:8080/log \
  -H 'Content-Type: application/json' \
  -d '{"level":"INFO","message":"hello"}'
```

That produces a log line in the shared file.

---

## 5. Forwarder Container

The forwarder is responsible for getting local log lines into OCI Logging.

It is built on:

- Oracle Linux 9
- Python 3
- the OCI Python SDK
- `logrotate`

### What the forwarder does

At startup, the forwarder:

1. validates its required environment
2. waits for the generator-created log file
3. prepares the logrotate config
4. starts a background logrotate loop
5. starts the Python shipping process

### What the Python shipper does

The Python shipper:

1. gets a resource principal signer
2. creates an OCI Logging ingestion client
3. tracks the active file and rotated files by inode
4. reads log lines
5. writes pending batches to disk
6. retries `PutLogs` requests until OCI accepts them
7. removes queued batch files only after a successful send

### Forwarder algorithm at a glance

The easiest way to read `oci_log_forwarder.py` is to split it into four cooperating parts:

- OCI client setup
- file tracking and rotation handling
- on-disk spool management
- the main loop that alternates between flushing and reading

```mermaid
flowchart TD
    START[Process starts] --> CLIENT[Build OCI Logging client<br/>with resource principal signer]
    CLIENT --> RECOVER[Recover queued spool files<br/>and previous read offsets]
    RECOVER --> TRACKER[Initialize FileTracker<br/>for app.log and rotated files]
    TRACKER --> LOOP[Enter main loop]

    LOOP --> USAGE[Log total log-file space usage<br/>on startup and on interval]
    USAGE --> FLUSH[Flush oldest queued batch<br/>to OCI Logging]
    FLUSH --> CHECK{Queued batches<br/>below limit?}
    CHECK -->|Yes| READ[Read next batch from tracked file]
    CHECK -->|No| SLEEP[Sleep for poll interval]
    READ --> FOUND{Any lines read?}
    FOUND -->|Yes| NORMALIZE[Normalize and truncate<br/>oversized entries if needed]
    NORMALIZE --> SPOOL[Write batch JSON to spool directory]
    SPOOL --> OFFSET[Persist new file offset]
    OFFSET --> LOOP
    FOUND -->|No| SLEEP
    SLEEP --> LOOP
```

This diagram matches the actual structure of the file: `SpoolQueue` handles on-disk batches, `FileTracker` handles inode-aware reading, and `OciLogForwarder.start()` orchestrates the loop.

### Startup and state recovery

Before the forwarder can ship any new lines, it reconstructs what it was doing before a restart.

```mermaid
flowchart TD
    A[Read environment variables] --> B[Create OCI Logging client]
    B --> C[Open spool directory]
    C --> D[Recover highest end_offset per inode<br/>from queued batch JSON files]
    D --> E[Load state file with tracked_files]
    E --> F[Merge recovered spool offsets<br/>with saved tracker state]
    F --> G[Ensure current app.log exists]
    G --> H[Resolve current inode and file size]
    H --> I[Rebuild tracked_files list]
    I --> J[Put rotated files first<br/>current app.log last]
    J --> K[Persist normalized tracker state]
```

The recovery logic matters because the forwarder does not trust just one source of truth:

- the state file remembers tracked files and offsets
- the spool directory remembers batches already read but not yet acknowledged by OCI

By merging both, the forwarder avoids re-reading lines that are already safely queued on disk.

### Authentication model

The forwarder supports **resource principal authentication only**.

That means:

- no mounted OCI config
- no user API keys
- no separate credential file in the container

OCI injects the runtime identity, and IAM policy controls what that identity may do.

---

## 6. Shared Storage Model

The two containers share storage inside the container instance.

```mermaid
flowchart TB
    subgraph CI[OCI Container Instance]
        G[Generator]
        FW[Forwarder]
        LOGS[(logs EMPTYDIR)]
        STATE[(forwarder-state EMPTYDIR)]
    end

    G -->|write /mnt/logs/app.log| LOGS
    FW -->|read /mnt/logs/app.log| LOGS
    FW -->|checkpoint + spool| STATE
```

### Shared logs volume

Both containers mount the same `EMPTYDIR` volume at `/mnt/logs`.

The generator writes:

```text
/mnt/logs/app.log
```

The forwarder reads that same file.

### Forwarder state volume

The forwarder also mounts a second `EMPTYDIR` volume at:

```text
/var/lib/oci-log-forwarder
```

This volume stores:

- the file read checkpoint
- the on-disk spool of unsent batches

This lets the forwarder survive container restarts within the same container instance without losing everything it had already queued.

---

## 7. Log Rotation Behavior

The repository uses `logrotate` to manage the shared log file.

The active file is rotated by **rename and create**:

1. the current file is renamed
2. a new file is created at the original path

The forwarder tracks files by inode, which allows it to:

- continue draining the rotated file
- switch to the new active file without abandoning unread data from the old inode

This is important because the generator and forwarder run concurrently.

### Rotation-handling algorithm

The important detail is that the forwarder does **not** assume the pathname is stable. It treats inode identity as the durable reference and pathname as something that can move.

```mermaid
sequenceDiagram
    participant G as Generator
    participant LR as logrotate
    participant FT as FileTracker
    participant FS as Filesystem

    G->>FS: append lines to /mnt/logs/app.log (inode 101)
    FT->>FS: track app.log inode 101 offset N
    LR->>FS: rename app.log to app.log-20260409
    LR->>FS: create new app.log (inode 202)
    FT->>FS: stat current app.log
    FT->>FT: detect pathname now points to inode 202
    FT->>FS: search siblings by old inode 101
    FS-->>FT: old inode found at rotated path
    FT->>FT: keep inode 101 as rotated file
    FT->>FT: add inode 202 as current app.log
    FT->>FS: continue reading unread bytes from inode 101
    FT->>FS: then switch to inode 202 at app.log
```

That is why the tracker stores:

- `path`
- `inode`
- `offset`

The path helps reopen a file, but the inode is what lets the algorithm realize that "the current filename changed, but the old file still exists and still has unread bytes."

---

## 8. Reliability Model

The forwarder uses an on-disk spool instead of relying only on memory.

That means:

- lines are read from the log file
- queued batches are written to disk
- only then are they considered pending for delivery

If OCI Logging is temporarily unavailable, the forwarder retries.

If the forwarder container restarts, the spool files remain available on the mounted forwarder-state volume.

This gives the system better durability during:

- transient OCI API failures
- forwarder restarts
- bursts of log volume

### Spool and retry algorithm

The reliability model comes from a strict ordering rule:

1. read lines from the log file
2. write them to a spool file
3. advance the tracked offset
4. send the spool file to OCI
5. delete the spool file only after success

```mermaid
flowchart TD
    R[Read batch from tracked file] --> N[Normalize each line]
    N --> W[Write spool JSON file]
    W --> M[Mark source offset as spooled]
    M --> F[Attempt to flush oldest spool file]
    F --> S{PutLogs succeeds?}
    S -->|Yes| D[Delete spool file]
    D --> NEXT[Continue with next batch]
    S -->|No| B[Log exception and back off]
    B --> RETRY[Keep spool file on disk]
    RETRY --> F
```

Because delivery happens from the spool rather than directly from the live file handle, temporary OCI failures do not force the forwarder to reread the source file from scratch.

### Main loop behavior

Once running, `OciLogForwarder.start()` repeats a small control loop.

```mermaid
flowchart TD
    L0[Loop start] --> U[Maybe log total size of app.log<br/>and rotated siblings]
    U --> F0[flush_spool]
    F0 --> Q{spool count < limit?}
    Q -->|No| P[Sleep poll interval]
    Q -->|Yes| R0[file_tracker.read_batch]
    R0 --> H{Batch returned?}
    H -->|No| P
    H -->|Yes| W0[Write batch to spool]
    W0 --> O0[Persist offset with mark_spooled]
    O0 --> L0
    P --> L0
```

This is intentionally simple:

- always try delivery first
- only read more from disk when the queue has room
- sleep when there is nothing useful to do

That keeps the implementation understandable while still handling rotation, retries, and restart recovery.

---

## 9. OCI Resources Created by Terraform

The `container_instance/` directory contains Terraform that provisions the OCI side of the system.

```mermaid
flowchart TD
    VCN[VCN] --> SUBNET[Subnet]
    IGW[Internet Gateway] --> RT[Route Table]
    RT --> SUBNET
    SL[Security List] --> SUBNET

    DG[Dynamic Group] --> POL[IAM Policy]
    LG[Log Group] --> LOG[Custom Log]

    SUBNET --> CI[Container Instance]
    POL --> CI
    LOG --> CI
```

The main resources are:

- VCN
- internet gateway
- route table
- security list
- subnet
- log group
- custom log
- dynamic group
- IAM policy
- container instance

### Reading `container_instance/main.tf` from top to bottom

The main Terraform file is organized in the same order that a reader would usually reason about the deployment:

1. configure the OCI provider
2. define a small local value used for naming
3. build the network
4. create the logging destination
5. create the IAM identity the runtime will use
6. wait for IAM and logging propagation
7. launch the container instance

That makes `main.tf` a useful "single source of truth" for the full deployment.

#### Provider and local value

The file starts with:

- `provider "oci"` using `var.region`
- `locals { name_prefix = replace(var.display_name, "_", "-") }`

The provider block tells Terraform which OCI region to target.

The `local.name_prefix` value is a convenience for resource naming. It converts underscores in the container instance display name into hyphens, which are safer for names that are reused across network and VNIC resources.

### VCN

The VCN provides the network boundary for the deployment.

In `main.tf`, that is:

- `oci_core_vcn.logging_test`

It uses:

- `var.compartment_id` to decide where the VCN lives
- `var.vcn_cidr_block` for the address space
- `${local.name_prefix}-vcn` for a predictable display name

Every network resource later in the file points back to this VCN.

### Internet gateway and route table

These provide outbound connectivity so the runtime can:

- reach OCI APIs
- pull container images

In `main.tf`, these are:

- `oci_core_internet_gateway.logging_test`
- `oci_core_route_table.logging_test`

The route table adds a `0.0.0.0/0` route to the internet gateway. That means traffic leaving the subnet can reach public OCI endpoints such as:

- image repositories
- Logging ingestion endpoints
- other OCI control-plane APIs used at runtime

Without this pairing, the container instance could exist but fail at startup or fail to ship logs.

### Security list

The security list allows:

- all outbound traffic
- optional inbound access to the generator HTTP port from configured CIDRs

In `main.tf`, this is `oci_core_security_list.logging_test`.

The file does two important things here:

- it allows all egress traffic to `0.0.0.0/0`
- it generates ingress rules dynamically from `var.generator_ingress_cidrs`

That `dynamic "ingress_security_rules"` block is worth calling out. Terraform iterates over every CIDR in `var.generator_ingress_cidrs` and creates a TCP rule that opens only `var.generator_http_port`.

This means inbound access to the generator is:

- optional
- restricted to the configured client CIDRs
- restricted to one TCP port instead of being broadly open

### Subnet

The subnet is where the container instance VNIC is attached.

In `main.tf`, that is `oci_core_subnet.logging_test`.

It ties the network together by referencing:

- the VCN ID
- the route table ID
- the security list ID

The file sets `prohibit_public_ip_on_vnic = false`, which allows the VNIC to receive a public IP when the container instance later asks for one.

### Log group and custom log

These are the OCI Logging destination resources.

The forwarder sends log batches to the custom log.

In `main.tf`, these are:

- `oci_logging_log_group.forwarder`
- `oci_logging_log.forwarder`

The log group is just the parent container. The custom log is the actual destination whose OCID is injected into the forwarder container as `OCI_LOG_OBJECT_ID`.

That wiring matters because the forwarder does not discover the destination dynamically. Terraform creates the log first, then passes the resulting log OCID directly into the runtime environment.

### Dynamic group

The dynamic group identifies the container instance runtime as an IAM principal.

In `main.tf`, that is `oci_identity_dynamic_group.forwarder_runtime`.

The matching rule is:

```text
ALL {resource.type = 'computecontainerinstance', resource.compartment.id = '<compartment_ocid>'}
```

This means any container instance in the target compartment can match the group. The repository then relies on policy scope to limit what that principal can actually do.

### IAM policy

The IAM policy grants that principal the permissions it needs, including:

- reading container repositories
- sending log content to OCI Logging

In `main.tf`, that is `oci_identity_policy.forwarder_runtime`.

The statements allow the dynamic group to:

- read repos in the tenancy
- use `log-content` for the specific log group created by Terraform

That combination supports the two runtime actions that matter most:

1. OCI can pull the configured container images
2. the forwarder can call the Logging ingestion API with resource principal auth

The policy is intentionally attached at the tenancy level because dynamic groups and IAM policies are tenancy-scoped resources in OCI.

### Container instance

The container instance is the runtime resource that launches:

- the generator container
- the forwarder container

It also defines:

- the shape and memory/CPU
- the shared volumes
- the VNIC placement
- the environment variables for both containers

In `main.tf`, this is the largest block: `oci_container_instances_container_instance.logging_test`.

This block is where the infrastructure definition becomes an application deployment.

#### Shape and lifecycle settings

At the top of the resource, Terraform sets:

- availability domain
- compartment
- display name
- restart policy
- shape
- desired state

Then the nested `shape_config` block applies `var.shape_ocpus` and `var.shape_memory_in_gbs`.

This means compute sizing is fully parameterized without changing the structure of the deployment.

#### Generator container block

The first `containers {}` block defines `oci-log-generator`.

It passes three environment variables:

- `LOG_FILE_PATH`
- `HTTP_PORT`
- `DEFAULT_LOG_LEVEL`

It also mounts the `logs` volume at `/mnt/logs`.

That expresses the generator contract very clearly: it listens on the configured port and writes to the shared log path on the shared volume.

#### Forwarder container block

The second `containers {}` block defines `oci-log-forwarder`.

Several details in this block are central to the architecture:

- `is_resource_principal_disabled = false` enables resource principal access inside the container
- `OCI_LOG_OBJECT_ID = oci_logging_log.forwarder.id` gives the shipper the exact custom log destination
- `OCI_AUTH_TYPE = "resource_principal"` forces the intended auth mode
- the remaining environment variables tune batching, queue depth, and log rotation behavior

This block also mounts:

- the shared `logs` volume at `/mnt/logs`
- the `forwarder-state` volume at `/var/lib/oci-log-forwarder`

That pairing is what lets the forwarder both read the application log and persist its own checkpoint and spool state separately.

#### VNIC block

The nested `vnics {}` block attaches the container instance to `oci_core_subnet.logging_test.id`.

It also controls:

- the VNIC display name
- the hostname label
- whether a public IP is assigned through `var.assign_public_ip`

This is the network bridge between the OCI network resources created earlier in the file and the actual running containers.

#### Volume blocks

The two `volumes {}` blocks define:

- `logs` as `EMPTYDIR`
- `forwarder-state` as `EMPTYDIR`

These are ephemeral volumes that live with the container instance lifecycle.

That is enough for this example because the design goal is:

- shared access between the two containers
- short-lived spool durability during container restarts within the same container instance

It is not trying to provide durable storage across full container instance replacement.

#### Explicit dependency on the wait

The container instance has:

```text
depends_on = [time_sleep.before_container_instance]
```

This forces Terraform to create the runtime only after the artificial wait has completed. It is a practical safeguard against IAM and logging propagation delays.

### Why `main.tf` is structured this way

The file is not just a list of OCI resources. It deliberately moves from prerequisites to runtime:

1. network path
2. logging destination
3. runtime identity and permissions
4. propagation delay
5. container launch

That ordering matches the actual dependency chain of the system. If you are reading the repository for the first time, `container_instance/main.tf` is the best place to understand how the architecture becomes a running OCI deployment.

---

## 10. Why There Is a Delay Before Container Creation

Terraform waits before creating the container instance.

The delay exists so that the surrounding resources have time to settle, especially:

- IAM resources
- logging resources

In this repository, the delay is modeled with a `time_sleep` resource before the container instance is created.

---

## 11. Terraform Apply Sequence

When you run Terraform, the sequence is:

```mermaid
sequenceDiagram
    participant T as Terraform
    participant N as Network Resources
    participant L as Logging Resources
    participant I as IAM Resources
    participant W as time_sleep
    participant C as Container Instance

    T->>N: Create VCN, IGW, route table, security list, subnet
    T->>L: Create log group and custom log
    T->>I: Create dynamic group and policy
    T->>W: Wait for configured delay
    T->>C: Create container instance
```

1. create the network resources
2. create the log group and custom log
3. create the dynamic group and IAM policy
4. wait for the configured delay period
5. create the container instance

This sequence makes the deployment easier to reason about because all required OCI resources are declared in one place.

---

## 12. Runtime Sequence

After the infrastructure exists, the runtime behavior is:

```mermaid
sequenceDiagram
    participant OCI as OCI Runtime
    participant G as Generator
    participant FS as Shared Log File
    participant FW as Forwarder
    participant Q as Disk Spool
    participant LOG as OCI Logging

    OCI->>G: Start container
    OCI->>FW: Start container
    G->>FS: Create app.log
    FW->>FS: Wait until file exists
    G->>FS: Append log lines
    FW->>FS: Read lines
    FW->>Q: Persist batch
    FW->>LOG: PutLogs
    LOG-->>FW: Success
    FW->>Q: Remove delivered batch
```

1. OCI starts the container instance
2. OCI pulls the generator and forwarder images
3. the generator starts and creates the shared log file
4. the forwarder waits until that file exists
5. the generator receives `POST /log` requests and appends lines
6. the forwarder reads those lines and spools them to disk
7. the forwarder sends them to OCI Logging
8. log rotation continues in the background as the file grows

---

## 13. How to Verify Logs Are Reaching OCI Logging

After deployment, the most useful verification approach is to check the system from both ends:

- confirm that the generator is producing log lines
- confirm that the forwarder is shipping them
- confirm that the custom log in OCI Logging is receiving them

### Step 1: Confirm the generator is alive

The generator exposes a health endpoint:

```bash
curl http://<generator-host>:8080/health
```

You should get a JSON response showing:

- `"status": "ok"`
- the configured log file path

That tells you the generator container is running and ready to accept log writes.

### Step 2: Send a known test message

Send a log line with a message that is easy to search for later:

```bash
curl -X POST http://<generator-host>:8080/log \
  -H 'Content-Type: application/json' \
  -d '{"level":"INFO","message":"verification-message-001"}'
```

This gives you a unique marker to look for in OCI Logging.

### Step 3: Check that the shared file is being written

If you have access to the runtime or container logs, verify that:

- the generator accepted the request
- the forwarder started successfully
- the forwarder did not report OCI auth or `PutLogs` errors

Typical forwarder startup signals are:

- it is waiting for or found the shared log file
- it started the OCI log forwarder process
- it is pushing log lines to OCI Logging

### Step 4: Open the custom log in OCI Logging

In the OCI Console, navigate to:

1. **Logging**
2. the log group created by Terraform
3. the custom log used by the forwarder

Open the log search or log explorer view for that custom log.

### Step 5: Search for the exact test message

Search for the unique string you sent, for example:

```text
verification-message-001
```

If the system is working correctly, you should see the log line appear in the custom log.

### Step 6: Verify repeated delivery

Send several messages instead of just one:

```bash
for i in $(seq 1 20); do
  curl -s -X POST http://<generator-host>:8080/log \
    -H 'Content-Type: application/json' \
    -d "{\"level\":\"INFO\",\"message\":\"verification-batch-$i\"}" >/dev/null
done
```

Then confirm that all or nearly all expected messages appear in OCI Logging.

This is a better test than a single message because it exercises:

- repeated file appends
- forwarder batching
- OCI Logging ingestion over several requests

### Step 7: Verify behavior during rotation

To verify that the system still works during rotation:

1. lower the rotation threshold in Terraform or forwarder env
2. generate a larger batch of log lines
3. confirm logs continue appearing in OCI Logging across multiple rotations

This checks the inode-aware rotation handling in the forwarder.

### Step 8: Verify recovery after interruption

To verify the on-disk spool behavior:

1. generate log traffic
2. interrupt or restart the forwarder container
3. allow it to start again
4. confirm that queued messages eventually appear in OCI Logging

This checks that pending batches survive restart on the forwarder-state volume.

### What successful verification looks like

You can consider the pipeline healthy when all of the following are true:

- the generator health endpoint responds
- `POST /log` requests succeed
- the forwarder shows no auth or ingestion errors
- your test messages appear in the OCI custom log
- logs continue to arrive during repeated writes and rotation

### What failure usually means

If logs do not appear in OCI Logging, the most common causes are:

- the generator was never reached
- the forwarder could not obtain a resource principal signer
- IAM policy or dynamic group propagation is not complete yet
- the wrong custom log OCID was injected
- OCI Logging ingestion calls are failing

In practice, checking both the forwarder container output and the custom log search results usually isolates the problem quickly.

---

## 14. How to Think About the System

The simplest way to understand the repository is to split it into three layers.

### Layer 1: log producer

The generator writes log lines to a file.

### Layer 2: log shipper

The forwarder watches that file and sends its contents to OCI Logging.

### Layer 3: OCI infrastructure

Terraform creates the network, identity, logging, and runtime resources needed for the two containers to operate.

That is the core model of the repository.

---

## 15. What a Reader Should Remember

If you want the short version, remember these points:

1. the generator creates and writes the shared log file
2. the forwarder waits for that file and ships its contents
3. the forwarder uses resource principal authentication
4. Terraform provisions both the OCI infrastructure and the container instance runtime
5. the forwarder uses inode-aware rotation handling and an on-disk spool for reliability

That is the current architecture of this repository.
