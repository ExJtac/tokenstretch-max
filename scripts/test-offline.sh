#!/bin/sh
# Run the full suite with OS-level external networking denied, including children.
# Loopback stays available for the inherited local dashboard tests.
# No fallback: an unavailable sandbox is a failure, not an offline test pass.
set -eu

tsmax_repo=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
tsmax_python=${TSMAX_TEST_PYTHON:-python3}
cd "$tsmax_repo"

case $(uname -s) in
    Darwin)
        if [ ! -x /usr/bin/sandbox-exec ]; then
            echo "Offline suite requires macOS sandbox-exec." >&2
            exit 2
        fi
        exec /usr/bin/sandbox-exec -p '
            (version 1)
            (allow default)
            (deny network*)
            (allow network-inbound (local ip "localhost:*"))
            (allow network-outbound (remote ip "localhost:*"))
        ' "$tsmax_python" -m pytest -q --tb=short "$@"
        ;;
    Linux)
        if ! command -v unshare >/dev/null 2>&1; then
            echo "Offline suite requires Linux util-linux unshare." >&2
            exit 2
        fi
        exec unshare -rn sh -c '
            ip link set lo up 2>/dev/null || true
            export TSMAX_P00_INNER=1
            exec "$@"
        ' sh "$tsmax_python" -m pytest -q --tb=short "$@"
        ;;
    *)
        echo "Offline suite requires macOS sandbox-exec or Linux unshare." >&2
        exit 2
        ;;
esac
