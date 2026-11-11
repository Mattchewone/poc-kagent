# One image, two entrypoints:
#   default CMD  -> HTTP server on :8080 (Substrate actors)
#   override     -> python /opt/poc/agent/fake_agent.py <op>  (OpenShell, local docker)
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends git curl ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir pytest==8.3.3

COPY target/ /opt/poc/target/
COPY agent/  /opt/poc/agent/

ENV TARGET_DIR=/opt/poc/target \
    AGENT_WORKSPACE=/workspace \
    PORT=8080 \
    PYTHONUNBUFFERED=1
RUN mkdir -p /workspace && chmod 0777 /workspace
WORKDIR /workspace
EXPOSE 8080

CMD ["python", "/opt/poc/agent/fake_agent.py", "serve"]
