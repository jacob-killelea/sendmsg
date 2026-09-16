#!/usr/bin/env bash
# Arm full-rate telemetry on Quad Inverter 1A from inside the DDE container.
# Arguments pass through, e.g. ./run.sh --channels 21,22,23 --rate 5 | ./run.sh --stop
# See README.md for why this needs a container and the FC network namespace.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/sendmsg-dde"
FC_CONTAINER="flight_computer_2p1_remote_1"
FC_IP="192.168.144.1"

# Create bind-mount sources ourselves. If the docker daemon creates a missing one it makes
# it root-owned, and the container's unprivileged joby user then cannot write into it.
mkdir -p "$VENV_DIR"

# The host venv is Python 3.10 and cannot load libcmessage_decoder.so (needs glibc 2.38);
# the DDE image is Python 3.12 on glibc 2.39. Built once, then reused.
if [[ ! -x "$VENV_DIR/ddevenv/bin/python" ]]; then
   echo "building cmessage-asyncio venv in the DDE image (one time)..."
   docker run --rm \
      -v "$HOME/.config/pip/pip.conf:/etc/pip.conf:ro" \
      -v "$VENV_DIR:/scratch" \
      --entrypoint /bin/bash dde:latest \
      -c 'python3 -m venv /scratch/ddevenv && /scratch/ddevenv/bin/pip install -q cmessage-asyncio'
fi

# docker refuses -t when stdin/stdout are not a terminal, which would break piping.
TTY_FLAGS=(); [[ -t 0 && -t 1 ]] && TTY_FLAGS=(-it)

# --network container: shares the FC's netns so packets are sourced from 192.168.144.1;
# the inverter drops any other source IP as "unexpected sender".
exec docker run --rm "${TTY_FLAGS[@]}" \
   --network "container:$FC_CONTAINER" \
   -v "$HOME/Joby:$HOME/Joby:ro" \
   -v "$HOME/.Joby:$HOME/.Joby" \
   -v "$VENV_DIR:/scratch" \
   -v "$HERE:/work:ro" \
   -e "HOME=$HOME" \
   --entrypoint /scratch/ddevenv/bin/python dde:latest \
   /work/send_qi_full_rate_telemetry.py --local-ip "$FC_IP" "$@"
