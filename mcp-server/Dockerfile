FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml ./
COPY src ./src

RUN pip install --no-cache-dir .

# Streamable-http is the enterprise-hosted mode — stdio only makes sense for
# local interactive use, never inside a container.
ENV EBSMCP_TRANSPORT=streamable-http
ENV EBSMCP_HTTP_HOST=0.0.0.0
ENV EBSMCP_HTTP_PORT=8080
EXPOSE 8080

USER nobody
CMD ["ebsmcp-server"]
