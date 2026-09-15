#!/usr/bin/env python3
"""Arm quad-inverter full-rate telemetry over CMessages (cmessage-asyncio).

Streams CSignalAnalysisDataEngineCommandMessage at the Signal Analysis Data Engine
on a quad inverter, which arms the algo-core LANScope logger and makes the inverter
publish CInverterFullRateTelemetryMessage (1 msg / channel / 1ms) to the FSDR.

Defaults target Quad Inverter 1A at 192.168.144.35.

Two things about this message matter operationally:

  * It is its own keep-alive.  CSignalAnalysisDataEngine restarts a 1000 ms soft
    timer on every command; when that timer expires the engine stops collecting
    and publishing.  So this script streams the command at --rate (>= 1 Hz) until
    interrupted rather than sending it once.
  * The engine ENSUREs exactly 8 channels and 8 decimations, so the arrays are
    always padded out to 8.  Channel id 0 is the "slot disabled" sentinel and is
    deliberately absent from the algo core's channel table.

The inverter receives this on its FlightComputer transport
(NSequoia::EPortId::eQuadInverterFlightComputer), so the command goes to UDP port
1929 and this script binds 1928 -- i.e. it stands in for the flight computer on
the FC<->QI link.

The source address matters as much as the port.  That transport is built with
.AddReceiveCategory(ECategory::eFlightComputer), and CUdpComms derives the sender's
node id from the source IP, so a packet from anywhere else is dropped with
"dropping packet from node N; unexpected sender".  Only the flight computer
addresses work: 192.168.144.1/.2/.3 (nodes 0/1/2).  A lab workstation on
192.168.144.240 is node 109, eCpuLoadTester.1, and gets dropped.

In the sim lab those addresses belong to the flight_computer_2p1_remote_N
containers, so the least invasive way to source from one is to share its network
namespace rather than add a conflicting address to the host:

    docker run --rm --network container:flight_computer_2p1_remote_1 ... \
        send_qi_full_rate_telemetry.py --local-ip 192.168.144.1

Requires the C++ decoder shim, which needs a purple_rain package whose schema
contains CSignalAnalysisDataEngineCommandMessage.  With no --schema-hash the shim
picks up the locally built ~/Joby/builds/flight_simulation_2p1_gcc/purple_rain/
purple_rain-v*.zip -- but only if JOBY_ROOT is set, so this script sets it.

libcmessage_decoder.so is built against glibc 2.38, so this needs a host newer
than Ubuntu 22.04; inside the DDE container works.
"""

import argparse
import asyncio
import os
import signal
import sys
import time

DEFAULT_JOBY_ROOT = os.path.expanduser('~/Joby')

# joby_root.get_joby_root() resolves JOBY_ROOT, else the git root of the CWD.  Run from
# anywhere else and it finds nothing, the shim silently skips the locally built package,
# and you encode against whatever stale zip happens to be in ~/.Joby/purple_rain/downloads.
# Has to happen before purple_rain_constants is imported -- it reads this at import time.
if not os.environ.get('JOBY_ROOT') and os.path.isdir(DEFAULT_JOBY_ROOT):
    os.environ['JOBY_ROOT'] = DEFAULT_JOBY_ROOT

from cmessage_asyncio.udp import CMessageUdpConnection          # noqa: E402
from cmessage_decoder_shim.cmessage_shape_cpp import CMessageShapeCpp   # noqa: E402

COMMAND_MESSAGE = 'CSignalAnalysisDataEngineCommandMessage'
TELEMETRY_MESSAGE = 'CInverterFullRateTelemetryMessage'

# CSignalAnalysisDataEngine::kuiNumChannels -- the engine ENSUREs this length.
NUM_CHANNELS = 8

# Quad Inverter 1A: vehicle_manifest 2p1/aircraft/inverter_project.yaml (host_ip 35).
DEFAULT_TARGET_IP = '192.168.144.35'
DEFAULT_CATEGORY = 'eQuadInverter'
DEFAULT_POSITION = 'e1A'

# port_configuration s4_2p1: "QuadInverter_FlightComputer" send 1928 / listen 1929.
DEFAULT_TARGET_PORT = 1929
DEFAULT_LOCAL_PORT = 1928

# The QI's FC transport only accepts senders whose node id resolves to
# ECategory::eFlightComputer -- FlightComputer.1/.2/.3, last octet 1/2/3.  Anything else
# is dropped as "unexpected sender", so default to the FC the lab runs as remote_1 rather
# than to the workstation's own netA-air address.
FLIGHT_COMPUTER_IPS = ('192.168.144.1', '192.168.144.2', '192.168.144.3')
DEFAULT_LOCAL_IP = FLIGHT_COMPUTER_IPS[0]

# Watchdog is 1000 ms; 2 Hz leaves margin for a dropped datagram.
DEFAULT_RATE_HZ = 2.0

CHANNEL_HELP = """\
channel ids (NSignalAnalysisChannelTable, mirrors the algo core's mapIdToAddress):
  0             slot disabled
  1..8          propulsion   Ia Ib Ic Idc Ua Ub Uc Udc
  11..18        fan/pump     Ia Ib Ic, VP hall, Ua Ub Uc Udc
  21..30        tilt         Ia Ib Ic, accel, Ua Ub Uc, motor/output resolver
  31..40        variable pitch
  41..67        ADC data as consumed by the control loops
  70..99        FPGA DMA / accelerometer / housekeeping
  1xx/2xx/3xx/4xx  per-core channels: propulsion / pump-fan / tilt / variable pitch
full table: blue_sky/applications/inverter_projects/common/shared_memory/hardware/
  inverter_shared_memory_interface/include/inverter_shared_memory_interface/
  shared_memory_interface/n_signal_analysis_channel_table.h
"""


def parse_uint_list(text, what):
    """Parse a comma-separated list of non-negative integers."""
    values = []
    for token in text.split(','):
        token = token.strip()
        if not token:
            continue
        try:
            value = int(token, 0)
        except ValueError:
            raise argparse.ArgumentTypeError(f'{what}: {token!r} is not an integer') from None
        if value < 0:
            raise argparse.ArgumentTypeError(f'{what}: {value} is negative')
        values.append(value)
    if len(values) > NUM_CHANNELS:
        raise argparse.ArgumentTypeError(f'{what}: at most {NUM_CHANNELS} values, got {len(values)}')
    return values


def pad_to_slots(values):
    """Pad a list out to the engine's fixed slot count with the disabled sentinel."""
    return list(values) + [0] * (NUM_CHANNELS - len(values))


def build_command(channels, decimation, args):
    """Build the CSignalAnalysisDataEngineCommandMessage field dict.

    Every member of the message and of its ancestors has to be present -- cmessage_pack
    raises MemberVarValueMissingException on anything it can't find -- except the three
    CMessage fields that send_dict fills in (EMessageType, NodeIdOfOriginator,
    SynchronizedSystemTimeInMicroseconds).

    The inherited CUpdateCommandMessage actuator fields are all sent inert: the SADE
    ignores them, and this message is TypeFiltered to the SADE alone, but there is no
    reason for a capture of this traffic to contain anything that reads as a real
    actuator command.
    """
    return {
        # CSignalAnalysisDataEngineCommandMessage
        'Channels': channels,
        'Decimation': decimation,
        # CUpdateCommandMessage -- addressing
        'EControllableElement': args.category,
        'EAirframePosition': args.position,
        # CUpdateCommandMessage -- actuator fields, deliberately inert
        'CommandUpdateValue': 0.0,
        'CommandUpdateValid': False,
        'Enable': False,
        'EnableValid': False,
        'LossofFlightComputers': False,
        'SignalProducedInFlightControllerCaptureTimeUtc': int(time.time() * 1e6),
        'ShutdownInhibit': False,
        'ShutdownInhibitValid': False,
        'ResetIsm': False,
        'IsmReadmittanceReset': False,
        'ForceShutdown': False,
        # CMessage
        'NodeIdOfOriginator': args.node_id,
    }


def on_telemetry(cmesg_obj, src_addr):
    """Print a one-line summary of a full-rate telemetry message."""
    measurements = list(cmesg_obj.Measurement)
    head = ' '.join(f'{value: .4g}' for value in measurements[:6])
    print(f'  {src_addr[0]}:{src_addr[1]} ch={cmesg_obj.Channel} '
          f'dec={cmesg_obj.Decimation} frame={cmesg_obj.FrameIndex} [{head} ...]')


async def run(args):
    """Load the schema, open the socket and stream the command."""
    try:
        shape = CMessageShapeCpp.from_version(
            schema_hash=args.schema_hash,
            log_level='debug' if args.verbose else 'warning')
    except OSError as exc:
        if 'GLIBC' not in str(exc):
            raise
        print(f'could not load libcmessage_decoder.so:\n  {exc}\n'
              f'It is built against a newer glibc than this host provides. '
              f'Run this from the DDE container.', file=sys.stderr)
        return 2

    # cmessage_names holds the 'e'-prefixed names; get_cmessage_id accepts either form.
    try:
        message_id = shape.decoder_shim_obj.get_cmessage_id(COMMAND_MESSAGE)
    except ValueError:
        print(f'{COMMAND_MESSAGE} is not in schema {shape.version}.\n'
              f'Build or select a purple_rain package from a branch that has the Signal\n'
              f'Analysis Data Engine, or pass --schema-hash.', file=sys.stderr)
        return 2
    print(f'schema {shape.version}, {COMMAND_MESSAGE} is msg id {message_id}')

    channels = pad_to_slots(args.channels)
    decimation = pad_to_slots(args.decimation)
    if len(args.decimation) not in (0, 1, len(args.channels)):
        print(f'warning: {len(args.decimation)} decimation values for '
              f'{len(args.channels)} channels; unmatched slots get 0', file=sys.stderr)
    if len(args.decimation) == 1:
        decimation = [args.decimation[0] if channel else 0 for channel in channels]

    if args.stop:
        channels = [0] * NUM_CHANNELS
        decimation = [0] * NUM_CHANNELS

    dst_addr = (args.to, args.port)
    command = build_command(channels, decimation, args)

    if args.dry_run:
        print(f'would send {COMMAND_MESSAGE} to {dst_addr[0]}:{dst_addr[1]} '
              f'from {args.local_ip}:{args.local_port}')
        for key, value in command.items():
            print(f'  {key} = {value}')
        return 0

    connection = await CMessageUdpConnection.connect(
        args.local_ip, args.local_port,
        default_shape=shape,
        verbose=args.verbose,
        reuse_port=True)
    print(f'bound {args.local_ip}:{args.local_port} -> {dst_addr[0]}:{dst_addr[1]}')

    if args.watch:
        # Note: the inverter routes CInverterFullRateTelemetryMessage to the FSDR
        # (lru_interface_cmessage_registration/lru/quad_inverter.yaml), not to this
        # socket, so this only sees traffic if the FSDR forwards it here.
        connection.subscribe(TELEMETRY_MESSAGE, on_telemetry)
        print(f'watching for {TELEMETRY_MESSAGE}')

    active = [channel for channel in channels if channel]
    if args.stop:
        print(f'stopping: all {NUM_CHANNELS} slots disabled')
    else:
        print(f'arming channels {active} at {args.rate:g} Hz '
              f'(engine stops 1000 ms after the last command)')

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signal_name in ('SIGINT', 'SIGTERM'):
        loop.add_signal_handler(getattr(signal, signal_name), stop_event.set)

    period = 1.0 / args.rate
    sent = 0
    next_send = loop.time()
    try:
        while not stop_event.is_set():
            # send_dict mutates what it is handed (name, EMessageType, the CMessage time
            # fields), so hand it a fresh copy each tick rather than the template.
            outbound = dict(command)
            outbound['SignalProducedInFlightControllerCaptureTimeUtc'] = int(time.time() * 1e6)
            connection.send_dict(COMMAND_MESSAGE, outbound, dst_addr=dst_addr)
            sent += 1
            if args.verbose or sent == 1:
                print(f'sent #{sent}')
            if args.count and sent >= args.count:
                break
            next_send += period
            try:
                await asyncio.wait_for(stop_event.wait(),
                                       timeout=max(0.0, next_send - loop.time()))
            except asyncio.TimeoutError:
                pass
    finally:
        print(f'\n{sent} command(s) sent')
        if args.disarm_on_exit and not args.stop and sent:
            disarm = build_command([0] * NUM_CHANNELS, [0] * NUM_CHANNELS, args)
            connection.send_dict(COMMAND_MESSAGE, disarm, dst_addr=dst_addr)
            print('sent disarm (all slots 0)')
        connection.disconnect()
    return 0


def main():
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        epilog=CHANNEL_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--to', default=DEFAULT_TARGET_IP,
                        help=f'inverter IP (default %(default)s = Quad Inverter 1A)')
    parser.add_argument('--port', type=int, default=DEFAULT_TARGET_PORT,
                        help='inverter UDP port, its FC-transport listen port (default %(default)s)')
    parser.add_argument('--local-ip', default=DEFAULT_LOCAL_IP,
                        help='local IP to bind; the inverter only accepts flight computer '
                             'addresses here (default %(default)s)')
    parser.add_argument('--local-port', type=int, default=DEFAULT_LOCAL_PORT,
                        help='local UDP port to bind (default %(default)s, the FC side of the link)')
    parser.add_argument('--channels', type=lambda text: parse_uint_list(text, 'channels'),
                        default=[1, 2, 3, 4, 5, 6, 7, 8],
                        help='up to 8 comma-separated channel ids, 0 disables a slot '
                             '(default 1..8, the propulsion phase currents and voltages)')
    parser.add_argument('--decimation', type=lambda text: parse_uint_list(text, 'decimation'),
                        default=[0],
                        help='one value applied to every active channel, or one per channel '
                             '(default 0 = no decimation)')
    parser.add_argument('--rate', type=float, default=DEFAULT_RATE_HZ,
                        help='command rate in Hz, must exceed 1 Hz to hold the engine '
                             'watchdog open (default %(default)s)')
    parser.add_argument('--count', type=int, default=0,
                        help='stop after this many commands (default 0 = until interrupted)')
    parser.add_argument('--category', default=DEFAULT_CATEGORY,
                        help='ECategory of the target LRU (default %(default)s)')
    parser.add_argument('--position', default=DEFAULT_POSITION,
                        help='EPosition of the target LRU (default %(default)s = 1A)')
    parser.add_argument('--node-id', type=int, default=0,
                        help='NodeIdOfOriginator to stamp on the command (default %(default)s)')
    parser.add_argument('--schema-hash', default=None,
                        help='purple_rain USID to decode/encode with '
                             '(default: the locally built flight_simulation_2p1_gcc package)')
    parser.add_argument('--stop', action='store_true',
                        help='send one command with all slots disabled and exit')
    parser.add_argument('--disarm-on-exit', action='store_true',
                        help='send an all-slots-disabled command on exit instead of letting '
                             'the 1 s watchdog time out')
    parser.add_argument('--watch', action='store_true',
                        help=f'print any {TELEMETRY_MESSAGE} that arrives on this socket')
    parser.add_argument('--dry-run', action='store_true',
                        help='print the message that would be sent and exit')
    parser.add_argument('--verbose', action='store_true', help='verbose logging')
    args = parser.parse_args()

    if args.stop:
        args.count = 1
    if args.rate <= 1.0 and not args.stop and args.count != 1:
        print(f'warning: {args.rate:g} Hz does not hold the engine watchdog open; '
              f'telemetry will start and stop', file=sys.stderr)
    if args.rate <= 0:
        parser.error('--rate must be positive')
    if not any(args.channels) and not args.stop:
        parser.error('no channels selected; every slot is the disabled sentinel 0')
    if args.local_ip not in FLIGHT_COMPUTER_IPS:
        print(f'warning: {args.local_ip} is not a flight computer address '
              f'({", ".join(FLIGHT_COMPUTER_IPS)}); the inverter resolves the sender node '
              f'from the source IP and its FlightComputerTransport will log "dropping '
              f'packet from node N; unexpected sender"', file=sys.stderr)

    return asyncio.run(run(args))


if __name__ == '__main__':
    sys.exit(main())
