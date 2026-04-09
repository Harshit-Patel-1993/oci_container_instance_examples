FROM oraclelinux:9-slim

ENV LOG_FILE_PATH=/logs/app.log \
    HTTP_HOST=0.0.0.0 \
    HTTP_PORT=8080 \
    DEFAULT_LOG_LEVEL=INFO

RUN microdnf install -y python3 \
    && microdnf clean all

COPY http_log_server.py /usr/local/bin/http_log_server.py

RUN chmod 0755 /usr/local/bin/http_log_server.py

VOLUME ["/logs"]

EXPOSE 8080

ENTRYPOINT ["python3", "/usr/local/bin/http_log_server.py"]
