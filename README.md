# Quad Inverter Full-Rate Telemetry — user guide

`send_qi_full_rate_telemetry.py` arms the Signal Analysis Data Engine (SADE) on a quad
inverter so it publishes full-rate (39 kHz nominal) selectable telemetry, and receives that
telemetry.

It exists to send a real activation command and confirm the algo core logger arms.

```
GROUND (this script)                                QUAD INVERTER 1A (192.168.144.35)
  CSignalAnalysisDataEngineCommandMessage  ------>   FlightComputerTransport
  {8x channel id, 8x decimation} @ >=1Hz               |> CSignalAnalysisDataEngineQuadInverter
  (the command is its own keep-alive)                     |> ActivateFullRateLogger()

  CInverterFullRateTelemetryMessage         <------   1 msg / channel / 1 ms
  {Channel, Decimation, FrameIndex, 44x float32}      addressed to the FSDR
  (received unless `--transmit-only`)                 192.168.144.226:1771
```

## Naming

Two distinct messages:
| Message | Direction | Role |
| --- | --- | --- |
| `CSignalAnalysisDataEngineCommandMessage` | ground -> inverter | Selects channels, arms the logger |
| `CInverterFullRateTelemetryMessage` | inverter -> FSDR | the telemetry that comes back |

By default this script does both in one process: it streams the first one and reports the
second one as it arrives. That only works where one namespace sees both addresses; under
`run.sh` it does not, so split it into `--transmit-only` and `--receive-only` halves. See
[Receiving the telemetry](#receiving-the-telemetry).

## Running it

```bash
./run.sh --tx                        # stream at 2 Hz until Ctrl-C
./run.sh --tx --channels 21,22,23 --rate 5

./run.sh --rx                        # in a second terminal: receive the telemetry
./run.sh --rx --csv /captures/run1.csv --dump 2
```

Run bare (`./run.sh`), the script streams *and* receives in one process — convenient on a
bench where one namespace owns both addresses, but not under `run.sh`, which can only join
one container's netns. There, always pass `--tx` or `--rx`.

`run.sh` does everything: on first use it builds a venv in the DDE image (a minute or two,
then cached in `~/.cache/sendmsg-dde`), and every run wraps the script in `docker run`.
Arguments pass straight through. It picks the network namespace from them: `--receive-only`
/ `--rx` runs in the FSDR's, everything else in a flight computer's.

Two reasons it cannot just be `python send_qi_full_rate_telemetry.py`:

- `libcmessage_decoder.so` is built against **glibc 2.38**. This workstation is Ubuntu 22.04
  (glibc 2.35), so the decoder will not load natively; the DDE image is glibc 2.39 /
  Python 3.12. The venv in `~/Desktop/sendmsg` is a host Python 3.10 one — fine for
  `--help` and reading the code, useless for sending.
- The packets have to be sourced from a flight computer address (see below).

Expected output:

```
schema CMessageUnifiedSchemaIdentifier(usid='a6c9ac2467680ff5'),
  CSignalAnalysisDataEngineCommandMessage is msg id 827
bound 192.168.144.1:1928 -> 192.168.144.35:1929 (startup count 1789496423)
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

### Every run must present a new startup count

The second silent-drop trap, and the reason each run prints a `startup count`.

The inverter's `CSequenceChecker` caches `(startup count, sequence number)` per sender node
and keeps it until the inverter reboots. `cmessage_asyncio` restarts its sequence number at
`1` for every new connection. So if two runs present the *same* startup count, the second
one's packets look stale:

| | startup count | seqnos sent | verdict |
| --- | --- | --- | --- |
| Run 1 | 1 | 1…11 | accepted; inverter caches `seq=11` |
| Run 2 | 1 (same) | 1…5 | `seqDelta = 1-11 = -10` → `eOutOfSequence`, **discarded** |

Discarded packets do not update the cache, so *everything* at or below the previous run's
high-water mark drops, and `evaluatePacket()` logs nothing on that path at all. Symptom: one
command appears to land and the rest vanish, erratically, depending on where the seqnos fall.

The fix is built in — the startup count defaults to `int(time.time())`, so every run
presents a new one, which reads as `eNodeRestart` and resets the inverter's cached sequence
number. It does **not** need to be monotonic: a *lower* count reads as
`eOutOfSequenceStartup`, which `IsInSequence()` also accepts. Override with
`--node-start-count` if you ever need a specific value.

## Receiving the telemetry

The telemetry socket is bound by default — the port the inverter unicasts telemetry to —
and what arrives is reported. `--receive-only` (short: `--rx`) sends nothing while doing it,
so it runs in its own terminal alongside a `--transmit-only` (`--tx`) sender whenever the two
ends need different source addresses.

```
schema CMessageUnifiedSchemaIdentifier(usid='a6c9ac2467680ff5'), receive only
listening for CInverterFullRateTelemetryMessage on 0.0.0.0:1771
waiting for telemetry; Ctrl-C to stop
  +   1.0s  8021 msg total, 8 stream(s)
    source           pos    ch    msg/s  dec frame step  irreg         min        mean         max
    192.168.144.35   e1A     1     1002    0         44      0      -412.7      0.1382       413.1
    ...
```

`msg/s` is that stream's rate in the window just printed, `frame step` the `FrameIndex`
increment it usually advances by, and `irreg` the number of messages since startup that did
not follow that step — dropped, duplicated or reordered data. `min`/`mean`/`max` cover every
sample received in the window, not just the message the line was printed for.

### Where the socket has to sit

The telemetry is **not** addressed to whoever sent the command. `quad_inverter.yaml`
registers `eInverterFullRateTelemetryMessage → fsdr`, and the inverter's
`FreeStandingDataRelayTransport` unicasts the `eTelemetry` topic to
`FreeStandingDataRelayAircraft.1` — node 112 — on the `FreeStandingDataRelayUnicast_SendOnly`
port, over both flight critical networks:

| Destination | |
| --- | --- |
| `192.168.144.226:1771` | blue, the `free_standing_data_relay_plugin_blue_air_1` container |
| `192.168.145.226:1771` | green, nothing owns this address in the sim lab |

So the receiving socket has to be somewhere `.226` traffic lands, and the sending socket has
to be a flight computer. No namespace in the sim lab is both, which is why the combined
default cannot be used there: `run.sh --rx` shares the FSDR container's netns, everything
else shares `flight_computer_2p1_remote_1`'s.

Two consequences worth knowing:

- If the FSDR plugin is ever started for real it will own port 1771, and the bind fails with
  `Address already in use` rather than quietly splitting datagrams with it (the socket
  deliberately does not set `SO_REUSEPORT`). Stop the plugin, or move `--telemetry-port`.
- One inverter shows up as up to two sources, one per network. Statistics are kept per
  `(source, channel)`, so those copies stay on separate lines instead of reading as double
  rate.

### Rate, and what to do with it

Eight channels at one message per millisecond per network is ~16k messages/s, each carrying
44 float32 samples, so nothing prints per message:

- the per-stream summary every `--summary-interval` seconds (default 1.0) is the default view;
- `--dump N` prints the first N messages in full, all 44 samples, for eyeballing the shape;
- `--csv PATH` writes one row per message — about 8 MB/s at full rate. Under `run.sh` only
  `/captures` is writable, which is `./captures/` on the host: `--csv /captures/run1.csv`.

Every one of those views is subject to the float32 caveat under [Channels](#channels):
integer and boolean channels read as garbage until the channel policy reaches `Read()`.

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
| `--node-start-count` | `int(time.time())` | packet-header startup count; must differ per run — see above |
| `--schema-hash` | local build | purple_rain USID to encode with |
| `--disarm-on-exit` | | disable all slots on exit instead of waiting out the watchdog |
| `--verbose` | | debug logging, and print every send |

Receiving:

| Option | Default | Notes |
| --- | --- | --- |
| `--receive-only` / `--rx` | | send nothing, only receive telemetry — needs the FSDR's netns |
| `--transmit-only` / `--tx` | | only stream the command, do not bind the telemetry socket |
| `--telemetry-ip` | `0.0.0.0` | local IP for the telemetry socket |
| `--telemetry-port` | `1771` | the port the inverter unicasts telemetry to the FSDR on |
| `--summary-interval` | `1.0` s | seconds between per-stream summaries |
| `--csv` | | one row per message; `/captures/x.csv` under `run.sh` |
| `--dump` | `0` | print the first N messages in full |

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

The check is to receive the telemetry, in a second terminal, while the first one streams the
command:

```bash
./run.sh --tx               # terminal 1: arm channels 1..8 at 2 Hz
./run.sh --rx               # terminal 2: count what comes back
```

Eight streams at ~1000 msg/s each means the SADE armed and is publishing. `no telemetry`
there, with the command accepted, is the interesting case — see
[Receiving the telemetry](#receiving-the-telemetry) for where the socket has to sit, and the
last two Troubleshooting entries for the states the sim lab is usually in.

Failing that, a capture on the bench wire proves the packets exist without decoding them:

```bash
sudo tcpdump -ni netA-air 'src 192.168.144.35 and udp port 1771' -c 20
```

## Troubleshooting

**`could not load libcmessage_decoder.so: ... GLIBC_2.38 not found`**
Running on the host instead of in the DDE container — use `./run.sh`. The script reports the
host's glibc alongside the raw `dlerror` text, so an unrelated load failure (missing file,
wrong arch) prints the same way; read the quoted error, not just the hint.

**`CSignalAnalysisDataEngineCommandMessage is not in schema ...`**
The selected purple_rain package predates the SADE. The message only exists on
`feature/S4TC-85572-qi-full-ra`; the script expects the locally built
`~/Joby/builds/flight_simulation_2p1_gcc/purple_rain/purple_rain-v*.zip`
(currently `v2.1.23-a6c9ac2467680ff5`). Pass `--schema-hash` to pin a different one.

**It silently picks a stale schema**
The shim finds the local build via `joby_root.get_joby_root()`, which needs `JOBY_ROOT` set
or the CWD inside the Joby repo. Worse than it sounds: the fallback is the *git root of the
CWD*, and `~/Desktop/sendmsg` is itself a git repo, so from here it returns a confidently
wrong answer, skips the local zip, and encodes against whatever is cached in
`~/.Joby/purple_rain/downloads` — it was picking up `v2.1.17` this way. The script sets
`JOBY_ROOT=~/Joby` before the purple_rain imports to prevent that (it has to be the env var:
`purple_rain_constants` snapshots it at import and `from_version()` takes no search-root
argument). Override the env var to change it.

**`dropping packet from node N; unexpected sender`**
Wrong source IP. See the table above. Node 109 is this workstation.

**Commands land at first, then silently stop being accepted**
Sequence-checker drop. See [Every run must present a new startup
count](#every-run-must-present-a-new-startup-count). If you passed `--node-start-count`
explicitly, pass a different value or drop the flag to get the clock default back.

**Nothing at all happens, no console output either**
Check the target is actually running the SADE firmware. Also worth confirming what is at
`.35`: in the sim lab `192.168.144.36`–`.46` map to the `quad_inverter_2p1_1B` … `6B`
containers, but **no container owns `.35`** — its MAC sits on the physical NIC
(`enp132s0f0`) with a static ARP entry, i.e. 1A is an off-box bench unit. Unlike the
containers it does not emit ICMP port-unreachable, so probing a port tells you nothing about
whether anything is listening.

**No telemetry anywhere, command accepted**
As of last check the sim lab is provisioned but not launched — `quad_inverter_2p1_1B` and the
FSDR at `.226` run only `sim-component-service`, with no blue_sky app sockets bound. Nothing
is producing telemetry in that state, so `--receive-only` sits at `no telemetry`; capture on
the wire instead.

**Telemetry is on the wire but `--receive-only` reports nothing**
Check the socket is in the right namespace first (`run.sh --rx` handles that). The
other way to get silence is a subscription name mismatch: `cmessage_asyncio` keys
subscriptions on the decoded message's own name, which is the `EMessageType` spelling
(`eInverterFullRateTelemetryMessage`), not the class spelling the schema is indexed by
(`CInverterFullRateTelemetryMessage`). Subscribing under the class name raises nothing and
matches nothing. The script converts with the decoder shim's own
`CMessageShapeCpp.normalize_cmessage('e', ...)`.

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
- telemetry routing — `purple_rain/.../lru_interface_cmessage_registration/lru/quad_inverter.yaml`
  (`eInverterFullRateTelemetryMessage: [fsdr]`), and the transport that acts on it,
  `blue_sky/foundations/common_vehicle/src/configurations/c_common_project_configuration_interface.cpp:39`
- telemetry port — the same `port_configuration_s4_2p1_configs.meta.yaml`
  (`FreeStandingDataRelayUnicast_SendOnly`: send 1771)
- FSDR node/address — node 112 = `.226` on both flight critical networks, in the same
  `c_s4_2p1_network_interface_configuration_container.cpp`
- telemetry shape — `CInverterFullRateTelemetryMessage` in
  `~/.Joby/purple_rain/downloads/<version>/output/c_message_definitions.yaml`
