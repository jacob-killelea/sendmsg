#!/usr/bin/env python3
"""Arm quad-inverter full-rate telemetry over CMessages. See README.md."""

import argparse
import asyncio
import contextlib
import os
import platform
import signal
import sys
import time

DEFAULT_JOBY_ROOT = os.path.expanduser('~/Joby')

# Must precede the purple_rain imports: purple_rain_constants snapshots JOBY_ROOT at import
# time and from_version() takes no search-root argument, so the env var is the only API.
# Without it joby_root falls back to the git root of the CWD -- which for this directory is
# this repo, a confidently wrong answer that silently encodes against a stale download.
if not os.environ.get('JOBY_ROOT') and os.path.isdir(DEFAULT_JOBY_ROOT):
    os.environ['JOBY_ROOT'] = DEFAULT_JOBY_ROOT

from cmessage_asyncio.udp import CMessageUdpConnection                 # noqa: E402
from cmessage_decoder_shim.cmessage_shape_cpp import CMessageShapeCpp  # noqa: E402

COMMAND_MESSAGE = 'CSignalAnalysisDataEngineCommandMessage'
TELEMETRY_MESSAGE = 'CInverterFullRateTelemetryMessage'

DEFAULT_TARGET_IP = '192.168.144.35'
DEFAULT_TARGET_PORT = 1929
DEFAULT_LOCAL_PORT = 1928
DEFAULT_CATEGORY = 'eQuadInverter'
DEFAULT_POSITION = 'e1A'
DEFAULT_RATE_HZ = 2.0

# The inverter accepts only senders whose node id resolves to ECategory::eFlightComputer.
FLIGHT_COMPUTER_IPS = ('192.168.144.1', '192.168.144.2', '192.168.144.3')
DEFAULT_LOCAL_IP = FLIGHT_COMPUTER_IPS[0]

# Inert value per schema type. Arrays stay empty; the encoder pads them to arrayLength.
INERT_BY_TYPE = {'bool': False, 'Float32_t': 0.0}

# Left out of the command so send_dict stamps the CMessage time base itself, picking
# whichever of these two names the schema uses. Including it would pin it to 0.
SEND_DICT_STAMPS = ('SynchronizedSystemTimeInMicroseconds', 'UtcPosixTimeInMicroseconds')

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


def parse_uint_list(text):
    values = []
    for token in text.split(','):
        token = token.strip()
        if not token:
            continue
        try:
            value = int(token, 0)
        except ValueError:
            raise argparse.ArgumentTypeError(f'{token!r} is not an integer') from None
        if value < 0:
            raise argparse.ArgumentTypeError(f'{value} is negative')
        values.append(value)
    return values


def build_command(spec, args):
    """Build the command from the schema's flattened member vars.

    Every inherited member has to be present or cmessage_pack raises
    MemberVarValueMissingException, so unset ones are sent inert.
    """
    command = {name: [] if var.get('arrayLength') else INERT_BY_TYPE.get(var['type'], 0)
               for name, var in spec.items()
               if name != 'id' and name not in SEND_DICT_STAMPS}
    command.update({
        'EMessageType': spec['id'],
        'EControllableElement': args.category,
        'EAirframePosition': args.position,
        'NodeIdOfOriginator': args.node_id,
        'Channels': args.channels,
        'Decimation': args.decimation,
    })
    return command


def stamped(command):
    """Copy the template with a fresh capture time.

    The copy is required, not defensive: send_dict mutates what it is handed and fills the
    CMessage time field only when absent, so reusing one dict freezes it at the first tick.
    """
    return dict(command, SignalProducedInFlightControllerCaptureTimeUtc=int(time.time() * 1e6))


def on_telemetry(cmesg_obj, _cmesg_shape, src_addr):
    head = ' '.join(f'{value: .4g}' for value in list(cmesg_obj.Measurement)[:6])
    print(f'  {src_addr[0]}:{src_addr[1]} ch={cmesg_obj.Channel} '
          f'dec={cmesg_obj.Decimation} frame={cmesg_obj.FrameIndex} [{head} ...]')


async def run(args):
    try:
        shape = CMessageShapeCpp.from_version(
            schema_hash=args.schema_hash,
            log_level='debug' if args.verbose else 'warning')
    except OSError as exc:
        # Broad on purpose: load_library() does a bare ctypes.cdll.LoadLibrary, so there is
        # no typed error to match on, and matching dlerror() text would leave every other
        # failure (missing file, wrong arch) as a bare traceback.
        print(f'could not load libcmessage_decoder.so:\n  {exc}\n'
              f'This host has glibc {platform.libc_ver()[1]}; the .so is usually built '
              'against a newer one. Run it in the DDE container.', file=sys.stderr)
        return 2

    try:
        spec = shape.cmesg_by_name(COMMAND_MESSAGE)
    except (KeyError, ValueError):
        print(f'{COMMAND_MESSAGE} is not in schema {shape.version}.\n'
              'Pass --schema-hash, or build a purple_rain package from a branch that has\n'
              'the Signal Analysis Data Engine.', file=sys.stderr)
        return 2
    print(f'schema {shape.version}, {COMMAND_MESSAGE} is msg id {spec["id"]}')

    slots = spec['Channels']['arrayLength']
    for name in ('channels', 'decimation'):
        if len(getattr(args, name)) > slots:
            print(f'--{name}: at most {slots} values, got {len(getattr(args, name))}',
                  file=sys.stderr)
            return 2

    command = build_command(spec, args)
    dst_addr = (args.to, args.port)

    if args.dry_run:
        print(f'would send {COMMAND_MESSAGE} to {args.to}:{args.port} '
              f'from {args.local_ip}:{args.local_port}')
        for key, value in stamped(command).items():
            print(f'  {key} = {value}')
        return 0

    # Each run must present a startup count the last one did not use, or the inverter's
    # sequence checker silently drops every packet at or below the previous run's seqno
    # (cmessage_asyncio restarts seqno at 1 per connection). The clock gives that for free:
    # it need not be monotonic, since a lower count reads as eOutOfSequenceStartup, which
    # is also accepted and also resets the inverter's cache. Fits int32 until 2038.
    startup_count = (args.node_start_count if args.node_start_count is not None
                     else int(time.time()))
    connection = await CMessageUdpConnection.connect(
        args.local_ip, args.local_port,
        default_shape=shape,
        verbose=args.verbose,
        node_start_count=startup_count,
        reuse_port=True)
    print(f'bound {args.local_ip}:{args.local_port} -> {args.to}:{args.port} '
          f'(startup count {startup_count})')

    if args.watch:
        # The inverter routes this to the FSDR, not here, so it only shows up if the FSDR
        # forwards it on. See README.md.
        connection.subscribe(TELEMETRY_MESSAGE, on_telemetry)
        print(f'watching for {TELEMETRY_MESSAGE}')

    active = [channel for channel in args.channels if channel]
    if active:
        print(f'arming channels {active} at {args.rate:g} Hz '
              '(engine stops 1000 ms after the last command)')
    else:
        print(f'stopping: all {slots} slots disabled')

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signal_name in ('SIGINT', 'SIGTERM'):
        loop.add_signal_handler(getattr(signal, signal_name), stop_event.set)

    period = 1.0 / args.rate
    sent = 0
    next_send = loop.time()
    try:
        while not stop_event.is_set():
            connection.send_dict(COMMAND_MESSAGE, stamped(command), dst_addr=dst_addr)
            sent += 1
            if args.verbose or sent == 1:
                print(f'sent #{sent}')
            if args.count and sent >= args.count:
                break
            next_send += period
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop_event.wait(),
                                       timeout=max(0.0, next_send - loop.time()))
    finally:
        print(f'\n{sent} command(s) sent')
        if args.disarm_on_exit and sent:
            disarm = stamped(dict(command, Channels=[], Decimation=[]))
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
                        help='inverter IP (default %(default)s = Quad Inverter 1A)')
    parser.add_argument('--port', type=int, default=DEFAULT_TARGET_PORT,
                        help='inverter UDP port, its FC-transport listen port (default %(default)s)')
    parser.add_argument('--local-ip', default=DEFAULT_LOCAL_IP,
                        help='local IP to bind; the inverter only accepts flight computer '
                             'addresses here (default %(default)s)')
    parser.add_argument('--local-port', type=int, default=DEFAULT_LOCAL_PORT,
                        help='local UDP port to bind (default %(default)s, the FC side of the link)')
    parser.add_argument('--channels', type=parse_uint_list,
                        default=[1, 2, 3, 4, 5, 6, 7, 8],
                        help='comma-separated channel ids, 0 disables a slot '
                             '(default 1..8, the propulsion phase currents and voltages)')
    parser.add_argument('--decimation', type=parse_uint_list, default=[0],
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
    parser.add_argument('--node-start-count', type=int, default=None,
                        help='packet-header startup count (default: an incrementing persisted '
                             'value, so each run looks like a restarted node to the '
                             "inverter's sequence checker rather than a stale sender)")
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

    if args.rate <= 0:
        parser.error('--rate must be positive')
    if not any(args.channels) and not args.stop:
        parser.error('no channels selected; every slot is the disabled sentinel 0')
    if args.local_ip not in FLIGHT_COMPUTER_IPS:
        print(f'warning: {args.local_ip} is not a flight computer address '
              f'({", ".join(FLIGHT_COMPUTER_IPS)}); the inverter resolves the sender node '
              'from the source IP and its FlightComputerTransport will log "dropping '
              'packet from node N; unexpected sender"', file=sys.stderr)

    if len(args.decimation) == 1:
        args.decimation = [args.decimation[0] if channel else 0 for channel in args.channels]
    elif len(args.decimation) not in (0, len(args.channels)):
        print(f'warning: {len(args.decimation)} decimation values for '
              f'{len(args.channels)} channels; unmatched slots get 0', file=sys.stderr)

    # --stop is just "all slots disabled, once, no disarm".
    if args.stop:
        args.channels, args.decimation = [], []
        args.count = 1
        args.disarm_on_exit = False
    if args.rate <= 1.0 and args.count != 1:
        print(f'warning: {args.rate:g} Hz does not hold the engine watchdog open; '
              'telemetry will start and stop', file=sys.stderr)

    return asyncio.run(run(args))


if __name__ == '__main__':
    sys.exit(main())
