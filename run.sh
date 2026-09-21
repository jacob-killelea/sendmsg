#!/usr/bin/env bash
# Arm full-rate telemetry on Quad Inverter 1A from inside the DDE container, or receive the
# telemetry it publishes. Arguments pass through:
#   ./run.sh --tx --channels 21,22,23 --rate 5   # arm, from a flight computer address
#   ./run.sh --rx --dump 2                       # receive, from the FSDR's address
# A container joins one netns, so the script's send-and-receive default cannot work here:
# pass --tx or --rx.
# See README.md for why this needs a container and someone else's network namespace.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/sendmsg-dde"
CAPTURE_DIR="$HERE/captures"
FC_CONTAINER="flight_computer_2p1_remote_1"
FC_IP="192.168.144.1"
FSDR_CONTAINER="free_standing_data_relay_plugin_blue_air_1"

# Create bind-mount sources ourselves. If the docker daemon creates a missing one it makes
# it root-owned, and the unprivileged container joby user then can't write in it.
mkdir -p "$VENV_DIR" "$CAPTURE_DIR"

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

# Sending and receiving need different addresses, and so different namespaces: the inverter
# drops commands that are not sourced from a flight computer, and it unicasts the telemetry
# to the FSDR. Nothing owns both addresses, so --rx is its own process.
NETWORK_CONTAINER="$FC_CONTAINER"
SCRIPT_ARGS=(--local-ip "$FC_IP")
SIDE=""
for arg in "$@"; do
   case $arg in
      --receive-only|--rx)
         NETWORK_CONTAINER="$FSDR_CONTAINER"
         SCRIPT_ARGS=()
         SIDE="rx"
         ;;
      --transmit-only|--tx)
         SIDE="tx"
         ;;
   esac
done

if [[ -z $SIDE ]]; then
   cat >&2 <<WARN
warning: neither --tx nor --rx given, so the script will send and receive in one process --
  but this container only has $FC_CONTAINER's addresses ($FC_IP), and the inverter unicasts
  the telemetry to the FSDR at .226, which nothing here owns. Expect the command to land and
  no telemetry to arrive. Run --tx here and --rx in a second terminal instead.
WARN
fi

# docker refuses -t when stdin/stdout are not a terminal, which would break piping.
TTY_FLAGS=(); [[ -t 0 && -t 1 ]] && TTY_FLAGS=(-it)

# /captures is the one writable mount, for --csv.
exec docker run --rm "${TTY_FLAGS[@]}" \
   --network "container:$NETWORK_CONTAINER" \
   -v "$HOME/Joby:$HOME/Joby:ro" \
   -v "$HOME/.Joby:$HOME/.Joby" \
   -v "$VENV_DIR:/scratch" \
   -v "$CAPTURE_DIR:/captures" \
   -v "$HERE:/work:ro" \
   -e "HOME=$HOME" \
   --entrypoint /scratch/ddevenv/bin/python dde:latest \
   /work/send_qi_full_rate_telemetry.py "${SCRIPT_ARGS[@]+"${SCRIPT_ARGS[@]}"}" "$@"
