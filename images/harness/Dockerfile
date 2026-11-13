# kagent's Claude harness (Claude Code 2.1.285 + kagent-claude adapter, v1.0.1 release catalog)
# with Node added, so the agent can run the demo app's tests itself before submitting.
# The digest must match files/builtin-harness-images.json in the kagent chart you install.
ARG HARNESS_BASE=ghcr.io/kagent-dev/kagent/claude-harness@sha256:111d28bc8552469b028d91ecd2352c9d44811aba07be12b1e6868db7948f904e
FROM ${HARNESS_BASE}
USER root
RUN apk add --no-cache nodejs npm
USER 65532:65532
