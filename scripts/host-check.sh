#!/usr/bin/env bash
# Milestone 1: tools present; Landlock in the kernel that runs the kind nodes.
set -uo pipefail
fail=0
for t in docker kind kubectl helm go python3 temporal tmux; do
  if command -v "$t" >/dev/null; then printf "ok    %s\n" "$t"; else printf "MISS  %s\n" "$t"; fail=1; fi
done
for t in openshell kubectl-ate; do
  command -v "$t" >/dev/null && printf "ok    %s\n" "$t" || printf "later %s (installed by make openshell-up / substrate-up)\n" "$t"
done
py=$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])')
python3 -c 'import sys;sys.exit(sys.version_info<(3,11))' && echo "ok    python $py" || { echo "MISS  python>=3.11 (have $py)"; fail=1; }
# On macOS the kind nodes run in Docker Desktop's Linux VM, so check that kernel, not the host's.
read -r kernel lsm < <(docker run --rm --privileged alpine:3.20 sh -c \
  'mount -t securityfs none /sys/kernel/security 2>/dev/null; echo "$(uname -r) $(cat /sys/kernel/security/lsm)"' 2>/dev/null)
echo "      node kernel $kernel, lsm: $lsm"
major=${kernel%%.*}; rest=${kernel#*.}; minor=${rest%%.*}
if (( major > 6 || (major == 6 && minor >= 2) )); then echo "ok    kernel >= 6.2"; else echo "MISS  kernel >= 6.2"; fail=1; fi
[[ "$lsm" == *landlock* ]] && echo "ok    landlock listed" || { echo "MISS  landlock (OpenShell only)"; fail=1; }
exit $fail
