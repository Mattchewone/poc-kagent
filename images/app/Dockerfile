# Verify image: node, git, the supervisor. No app baked in: the worker sends the
# base tree and the agent's diff, so this image never goes stale when main moves.
FROM node:22.11-bookworm-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && mkdir -p /work && chown node:node /work
COPY images/app/supervisor.mjs /opt/poc/supervisor.mjs
USER node
ENV WORK_DIR=/work NODE_ENV=production
EXPOSE 8080
CMD ["node", "/opt/poc/supervisor.mjs", "8080"]
