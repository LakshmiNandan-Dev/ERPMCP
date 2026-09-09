FROM python:3.12-slim

WORKDIR /app

# Oracle Instant Client — required only for THICK mode
# (EBSMCP_ORACLE_THICK_MODE=true), which is how we authenticate against EBS
# instances running SEC_CASE_SENSITIVE_LOGON=FALSE (the 10g verifier that thin
# mode rejects with DPY-3015). Harmless when thick mode is off. Basic Lite is
# the smallest package that supports connectivity; libaio is its one OS
# dependency.
#
# libaio1 vs libaio1t64: Debian 13 (trixie) renamed the package to libaio1t64
# in the 64-bit time_t transition, so newer python:3.x-slim bases don't have
# libaio1. Try the new name first, fall back to the old one for bookworm-based
# images. The client needs the SONAME libaio.so.1; if only the t64 variant is
# present, symlink it so Oracle's loader finds it.
RUN apt-get update \
 && apt-get install -y --no-install-recommends wget unzip ca-certificates \
 && (apt-get install -y --no-install-recommends libaio1t64 \
     || apt-get install -y --no-install-recommends libaio1) \
 && ( ldconfig -p | grep -q 'libaio\.so\.1$' \
      || ln -sf "$(ldconfig -p | grep -oE '/[^ ]*libaio\.so\.1t64' | head -1)" /lib/libaio.so.1 ) \
 && wget -q https://download.oracle.com/otn_software/linux/instantclient/1928000/instantclient-basiclite-linux.x64-19.28.0.0.0dbru.zip \
 && unzip -q instantclient-basiclite-linux.x64-19.28.0.0.0dbru.zip -d /opt/oracle \
 && rm instantclient-basiclite-linux.x64-19.28.0.0.0dbru.zip \
 && echo /opt/oracle/instantclient_19_28 > /etc/ld.so.conf.d/oracle-instantclient.conf \
 && ldconfig \
 && apt-get purge -y wget unzip && apt-get autoremove -y && apt-get clean \
 && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY src ./src

# Operational scripts, run with `docker compose run` rather than by the
# server itself. Shipped in the image so an operator can validate database
# connectivity from a host that has no Python or oracledb installed — the
# image already carries both.
COPY scripts ./scripts

RUN pip install --no-cache-dir .

# Streamable-http is the enterprise-hosted mode — stdio only makes sense for
# local interactive use, never inside a container.
ENV EBSMCP_TRANSPORT=streamable-http
ENV EBSMCP_HTTP_HOST=0.0.0.0
ENV EBSMCP_HTTP_PORT=8080
EXPOSE 8080

USER nobody
CMD ["ebsmcp-server"]
