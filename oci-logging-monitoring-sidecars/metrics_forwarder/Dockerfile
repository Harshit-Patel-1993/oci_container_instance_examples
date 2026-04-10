FROM oraclelinux:9-slim

ENV METRICS_FORWARDER_SPOOL_DIR=/var/lib/oci-metrics-forwarder/spool \
    METRICS_FORWARDER_STATE_DIR=/var/lib/oci-metrics-forwarder/state \
    LOGROTATE_STATE_FILE=/var/lib/logrotate/logrotate.status \
    LOGROTATE_INTERVAL_SECONDS=60

RUN microdnf install -y \
      ca-certificates \
      findutils \
      gzip \
      hostname \
      logrotate \
      python3 \
      python3-pip \
      procps-ng \
      shadow-utils \
      which \
    && python3 -m pip install --no-cache-dir --upgrade pip \
    && python3 -m pip install --no-cache-dir oci==2.161.1 \
    && microdnf clean all

RUN mkdir -p \
      /etc/logrotate.d \
      /var/lib/oci-metrics-forwarder/spool \
      /var/lib/oci-metrics-forwarder/state \
      /var/lib/logrotate \
      /opt/oci-metrics-forwarder

COPY app/oci_metrics_forwarder.py /opt/oci-metrics-forwarder/oci_metrics_forwarder.py
COPY logrotate/metric-file.conf.template /etc/logrotate.d/metric-file.conf.template
COPY scripts/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

RUN chmod 0755 /usr/local/bin/docker-entrypoint.sh /opt/oci-metrics-forwarder/oci_metrics_forwarder.py

VOLUME ["/metrics", "/var/lib/oci-metrics-forwarder"]

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
