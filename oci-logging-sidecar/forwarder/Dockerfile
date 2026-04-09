FROM oraclelinux:9-slim

ENV LOG_FORWARDER_SPOOL_DIR=/var/lib/oci-log-forwarder/spool \
    LOG_FORWARDER_STATE_DIR=/var/lib/oci-log-forwarder/state \
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
      /var/lib/oci-log-forwarder/spool \
      /var/lib/oci-log-forwarder/state \
      /var/lib/logrotate \
      /opt/oci-log-forwarder

COPY app/oci_log_forwarder.py /opt/oci-log-forwarder/oci_log_forwarder.py
COPY logrotate/log-file.conf.template /etc/logrotate.d/log-file.conf.template
COPY scripts/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

RUN chmod 0755 /usr/local/bin/docker-entrypoint.sh /opt/oci-log-forwarder/oci_log_forwarder.py

VOLUME ["/logs", "/var/lib/oci-log-forwarder"]

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
