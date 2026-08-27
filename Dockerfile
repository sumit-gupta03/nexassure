# Multi-stage build. The runtime image carries no compiler and no build tools.
FROM python:3.12-slim AS builder

WORKDIR /build
RUN python -m pip install --no-cache-dir --upgrade pip build

COPY pyproject.toml README.md LICENSE NOTICE ./
COPY src ./src
RUN python -m build --wheel --outdir /dist


FROM python:3.12-slim

LABEL org.opencontainers.image.title="NexAssure" \
      org.opencontainers.image.description="Open-source data testing for the modern warehouse" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.source="https://github.com/sumit-gupta03/nexassure"

# unixodbc is needed by pyodbc for SQL Server and Synapse. The Microsoft ODBC
# driver itself is not redistributable here; mount it or extend this image if
# you need those two engines.
RUN apt-get update \
 && apt-get install -y --no-install-recommends unixodbc libpq5 \
 && rm -rf /var/lib/apt/lists/*

COPY --from=builder /dist/*.whl /tmp/
RUN python -m pip install --no-cache-dir /tmp/*.whl[postgres,redshift,snowflake,mysql,duckdb,mcp,server,notify] \
 && rm -rf /tmp/*.whl

# Run unprivileged. The metastore lives in the home directory, so it survives
# only if you mount a volume there.
RUN useradd --create-home --uid 10001 nexassure
USER nexassure
WORKDIR /project
ENV NEXASSURE_HOME=/home/nexassure/.nexassure \
    NEXASSURE_LOG_FORMAT=json \
    PYTHONUNBUFFERED=1

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD nexassure version || exit 1

ENTRYPOINT ["nexassure"]
CMD ["--help"]
