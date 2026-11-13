# Release + preview image of demo-todo, built by the worker (worker/delivery.py) from a Gitea commit.
# Owned by the platform, not the app repo: the agent can't change how the app is packaged.
# /version.txt is what Kargo's smoke test and the PR-env readiness check compare against.
FROM node:22.11-bookworm-slim
WORKDIR /app
COPY --chown=node:node . .
ARG VERSION=dev
ARG GIT_SHA=unknown
RUN echo "$VERSION" > public/version.txt && echo "$GIT_SHA" > public/commit.txt
USER node
ENV PORT=3000 NODE_ENV=production
EXPOSE 3000
CMD ["node", "src/server.js"]
