# Quad Inverter Full-Rate Telemetry — user guide

`send_qi_full_rate_telemetry.py` arms the Signal Analysis Data Engine (SADE) on a quad
inverter so it publishes full-rate (39 kHz nominal) selectable telemetry.

It is the ground half of the loop described in
`~/Joby/SIGNAL_ANALYSIS_DATA_ENGINE_TODO.md`, and exists to exercise the **B1 end-to-end**
item: send a real activation command and confirm the algo core logger arms.

```
GROUND (this script)                                QUAD INVERTER 1A (192.168.144.35)
  CSignalAnalysisDataEngineCommandMessage  ──────▶   FlightComputerTransport
  {8x channel id, 8x decimation} @ >=1Hz               └▶ CSignalAnalysisDataEngineQuadInverter
  (the command is its own keep-alive)                     └▶ ActivateFullRateLogger()

  CInverterFullRateTelemetryMessage         ◀──────   1 msg / channel / 1 ms
  {Channel, Decimation, FrameIndex, 44x float32}      routed to the FSDR, not to us
```

## Naming

There is no `CInverterFullRateTelemetryCommand`. Two distinct messages:

| Message | Direction | Role |
| --- | --- | --- |
| `CSignalAnalysisDataEngineCommandMessage` | ground → inverter | **the command.** Selects channels, arms the logger |
| `CInverterFullRateTelemetryMessage` | inverter → FSDR | the telemetry that comes back |

This script sends the first one.

## One-time setup

`libcmessage_decoder.so` is built against **glibc 2.38**. This workstation is Ubuntu 22.04
(glibc 2.35), so the script cannot run natively here — it needs the DDE image (glibc 2.39,
Python 3.12). Build a venv inside that image:

```bash
mkdir -p ~/Desktop/sendmsg/dde
docker run --rm -v ~/.config/pip/pip.conf:/etc/pip.conf:ro \
  -v ~/Desktop/sendmsg/dde:/scratch --entrypoint /bin/bash dde:latest \
  -c 'python3 -m venv /scratch/ddevenv && /scratch/ddevenv/bin/pip install -q cmessage-asyncio'
```

The `~/Desktop/sendmsg` venv is a host Python 3.10 one and is **not** usable for this — it
is fine for `--help` and reading the code, nothing more.

## Running it

Streams the command at 2 Hz until Ctrl-C:

```bash
docker run --rm -it --network container:flight_computer_2p1_remote_1 \
  -v ~/Joby:$HOME/Joby:ro -v ~/.Joby:$HOME/.Joby \
  -v ~/Desktop/sendmsg/dde:/scratch \
  -v ~/Desktop/sendmsg/send_qi_full_rate_telemetry.py:/tmp/send_qi.py:ro \
  -e HOME=$HOME --entrypoint /bin/bash dde:latest \
  -c '/scratch/ddevenv/bin/python /tmp/send_qi.py --local-ip 192.168.144.1'
```

Expected output:

```
schema CMessageUnifiedSchemaIdentifier(usid='a6c9ac2467680ff5'),
  CSignalAnalysisDataEngineCommandMessage is msg id 827
bound 192.168.144.1:1928 -> 192.168.144.35:1929
arming channels [1, 2, 3, 4, 5, 6, 7, 8] at 2 Hz (engine stops 1000 ms after the last command)
sent #1
```

### `--network container:flight_computer_2p1_remote_1` is not optional

This is the part that is easy to get wrong, and it fails *silently on the ground side* —
the script reports a clean send and the inverter drops the packet.

The inverter's FC transport is built with
`.AddReceiveCategory(ECategory::eFlightComputer)`, and `CUdpComms::populateIpToNetworkAndNodeMap()`
resolves the sender's node id **from the source IP address**. Anything that is not a flight
computer is rejected:

| Source IP | Node | Category | Result |
| --- | --- | --- | --- |
| `192.168.144.240` (this workstation, `netA-air`) | 109 | `eCpuLoadTester.1` | **dropped** |
| `192.168.144.1` / `.2` / `.3` | 0 / 1 / 2 | `eFlightComputer.1/2/3` | accepted |

On the inverter console a rejection looks like:

```
[ERROR] Ms: 4891027: FlightComputerTransport: dropping packet from node 109; unexpected sender
```

The FC addresses belong to the `flight_computer_2p1_remote_1/2/3` containers, so sharing one
container's network namespace sources the packets from `192.168.144.1` without adding a
conflicting address to the host. Do not `ip addr add 192.168.144.1` on the host — it collides
with the running container.

The script warns if `--local-ip` is not one of the three FC addresses.

## Options

| Option | Default | Notes |
| --- | --- | --- |
| `--to` | `192.168.144.35` | Quad Inverter 1A (`host_ip: 35` in the vehicle manifest) |
| `--port` | `1929` | the QI's FC-transport listen port |
| `--local-ip` | `192.168.144.1` | must be an FC address — see above |
| `--local-port` | `1928` | the FC side of the link |
| `--channels` | `1,2,3,4,5,6,7,8` | up to 8 ids; `0` disables a slot |
| `--decimation` | `0` | one value for all active channels, or one per channel |
| `--rate` | `2.0` Hz | must exceed 1 Hz, see watchdog below |
| `--count` | `0` (forever) | stop after N commands |
| `--category` / `--position` | `eQuadInverter` / `e1A` | the recipient LRU |
| `--node-id` | `0` | `NodeIdOfOriginator` stamped on the message |
| `--schema-hash` | local build | purple_rain USID to encode with |
| `--stop` | | one command with all slots disabled, then exit |
| `--disarm-on-exit` | | disable all slots on exit instead of waiting out the watchdog |
| `--watch` | | print any `CInverterFullRateTelemetryMessage` on this socket |
| `--dry-run` | | print the message and exit without sending |
| `--verbose` | | debug logging, and print every send |

### The command is a keep-alive

`CSignalAnalysisDataEngine` restarts a **1000 ms** soft timer on every command received, and
stops collecting and publishing when it expires. So the command has to be *streamed*, not
sent once — that is why the default is 2 Hz and runs until interrupted. Ctrl-C stops
telemetry within a second; `--disarm-on-exit` stops it immediately.

### Channels

Exactly 8 channel and 8 decimation slots are always sent — the engine `ENSURE`s that length.
Shorter `--channels` lists are padded with `0`, the "slot disabled" sentinel.

| IDs | Signals |
| --- | --- |
| `0` | slot disabled (deliberately absent from the algo core table) |
| `1..8` | propulsion — Ia Ib Ic Idc Ua Ub Uc Udc |
| `11..18` | fan/pump — Ia Ib Ic, VP hall, Ua Ub Uc Udc |
| `21..30` | tilt — Ia Ib Ic, accel, Ua Ub Uc, motor/output resolver |
| `31..40` | variable pitch |
| `41..67` | ADC data as consumed by the control loops |
| `70..99` | FPGA DMA / accelerometer / housekeeping |
| `1xx` `2xx` `3xx` `4xx` | per-core: propulsion / pump-fan / tilt / variable pitch |

Authoritative list, including word types:
`blue_sky/applications/inverter_projects/common/shared_memory/hardware/inverter_shared_memory_interface/include/inverter_shared_memory_interface/shared_memory_interface/n_signal_analysis_channel_table.h`

Note the known limitation in the current slice: raw ring words are always reinterpreted as
float32, so integer and boolean channels will read as garbage until the channel policy is
wired into `Read()`.

## Confirming it worked

**Absence of the "unexpected sender" error is not proof of success.** That log is rate-limited
per `(transport, node id)`, but the key folds in the node, so a *newly* rejected node does
print a fresh line. What silence cannot distinguish is:

- the SADE received the command and armed — it logs nothing on success; versus
- the firmware on the target has no SADE tenant, so the message was published to no
  recipient and silently ignored.

The reliable check is a capture on the bench wire while the script streams:

```bash
sudo tcpdump -ni netA-air 'src 192.168.144.35 and udp' -c 20
```

`--watch` will usually show nothing even on success: `quad_inverter.yaml` registers
`eInverterFullRateTelemetryMessage → fsdr`, so the telemetry is addressed to the FSDR at
`192.168.144.226`, not to this socket.

## Troubleshooting

**`could not load libcmessage_decoder.so: ... GLIBC_2.38 not found`**
Running on the host instead of in the DDE container. See [One-time setup](#one-time-setup).

**`CSignalAnalysisDataEngineCommandMessage is not in schema ...`**
The selected purple_rain package predates the SADE. The message only exists on
`feature/S4TC-85572-qi-full-ra`; the script expects the locally built
`~/Joby/builds/flight_simulation_2p1_gcc/purple_rain/purple_rain-v*.zip`
(currently `v2.1.23-a6c9ac2467680ff5`). Pass `--schema-hash` to pin a different one.

**It silently picks a stale schema**
The shim finds the local build via `joby_root.get_joby_root()`, which needs `JOBY_ROOT` set
or the CWD inside the Joby repo. Run from anywhere else and it falls back to whatever is
cached in `~/.Joby/purple_rain/downloads` — it was picking up `v2.1.17` this way. The script
sets `JOBY_ROOT=~/Joby` at import time to prevent that; override the env var to change it.

**`dropping packet from node N; unexpected sender`**
Wrong source IP. See the table above. Node 109 is this workstation.

**Nothing at all happens, no console output either**
Check the target is actually running the SADE firmware. Also worth confirming what is at
`.35`: in the sim lab `192.168.144.36`–`.46` map to the `quad_inverter_2p1_1B` … `6B`
containers, but **no container owns `.35`** — its MAC sits on the physical NIC
(`enp132s0f0`) with a static ARP entry, i.e. 1A is an off-box bench unit. Unlike the
containers it does not emit ICMP port-unreachable, so probing a port tells you nothing about
whether anything is listening.

**No telemetry anywhere, command accepted**
As of last check the sim lab is provisioned but not launched — `quad_inverter_2p1_1B` and the
FSDR at `.226` run only `sim-component-service`, with no blue_sky app sockets bound. The
telemetry has no receiver in that state; capture on the wire instead.

## Reference

Derived from, in case any of it moves:

- ports — `purple_rain/.../port_configuration/documents/s4_2p1/port_configuration_s4_2p1_configs.meta.yaml`
  (`QuadInverter_FlightComputer`: send 1928 / listen 1929)
- command routing — `blue_sky/applications/inverter_projects/quad_inverter_projects/tenant_task_configurations/common_vehicle/target/src/configuration/c_quad_inverter_common_target_configuration.cpp:153`
- sender validation — `blue_sky/foundations/xylem_transport/src/c_transport.cpp:405`,
  `c_udp_comms.cpp:203`
- node id ↔ IP table — `builds/flight_simulation_2p1_gcc/common_configurations/src/configuration_containers/s4_2p1/c_s4_2p1_network_interface_configuration_container.cpp`
- engine behaviour — `blue_sky/foundations/signal_analysis_data_engine/src/c_signal_analysis_data_engine.cpp`
- addressing — `purple_rain/.../vehicle_manifest/documents/2p1/aircraft/inverter_project.yaml`
