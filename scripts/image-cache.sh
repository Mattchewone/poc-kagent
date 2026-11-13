#!/usr/bin/env bash
# Image cache for fresh clusters: export every image from the node once, import on the next create.
#   scripts/image-cache.sh save   node containerd -> .cache/image-cache/*.tar (skips ones already saved)
#   scripts/image-cache.sh load   .cache/image-cache/*.tar -> node containerd (parallel)
#
# Replaces registry pull-through caches, which stalled on large layers (registry:2 proxy) and
# could not cover pulls made by Substrate's atelet. Loading ~40 images from disk takes about a minute;
# pulling them took ~30 minutes on a slow link.
set -euo pipefail
cd "$(dirname "$0")/.."; source scripts/versions.env
NODE="$CLUSTER-control-plane"; DIR=.cache/image-cache; mkdir -p "$DIR"
ARCH=$(uname -m | sed 's/aarch64/arm64/;s/x86_64/amd64/')
file_for() { echo "$DIR/$(echo "$1" | tr '/:@' '___').tar"; }

case "${1:-}" in
  save)
    refs=$(docker exec "$NODE" ctr -n k8s.io images ls -q | grep -vE '^sha256:' | grep -v '@sha256:' | sort -u)
    # Digest-only refs (kagent pins images by digest) are saved under their digest name too.
    refs="$refs
$(docker exec "$NODE" ctr -n k8s.io images ls -q | grep '@sha256:' | sort -u)"
    n=0
    for r in $refs; do
      [ -n "$r" ] || continue
      f=$(file_for "$r"); [ -s "$f" ] && continue
      case "$r" in localhost:5001/*|*/mock-poc-*) continue;; esac  # ours: rebuilt and pushed by images.sh
      # Streamed through docker exec: the node's /tmp is a tmpfs that `docker cp` cannot see.
      if docker exec "$NODE" ctr -n k8s.io images export --platform "linux/$ARCH" /var/tmp/img.tar "$r" >/dev/null 2>&1 \
        && docker exec "$NODE" cat /var/tmp/img.tar > "$f.tmp"; then mv "$f.tmp" "$f"; n=$((n+1)); else echo "skip $r"; fi
    done
    docker exec "$NODE" rm -f /var/tmp/img.tar; rm -f "$DIR"/*.tmp
    echo "saved $n new images to $DIR ($(du -sh "$DIR" | cut -f1) total)"
    ;;
  load)
    ls "$DIR"/*.tar >/dev/null 2>&1 || { echo "image cache empty (first run pulls from the internet)"; exit 0; }
    for f in "$DIR"/*.tar; do
      ( b=$(basename "$f")
        docker exec -i "$NODE" sh -c "cat > /var/tmp/$b" < "$f" \
          && docker exec "$NODE" ctr -n k8s.io images import "/var/tmp/$b" >/dev/null 2>&1 || echo "failed $b"
        docker exec "$NODE" rm -f "/var/tmp/$b" ) &
      while [ "$(jobs -r | wc -l)" -ge 6 ]; do sleep 0.2; done
    done
    wait
    echo "loaded $(ls "$DIR"/*.tar | wc -l | tr -d ' ') images from $DIR"
    ;;
  *) echo "usage: $0 save|load"; exit 2 ;;
esac
