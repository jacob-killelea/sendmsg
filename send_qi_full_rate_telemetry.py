#!/usr/bin/env python3
"""Arm quad-inverter full-rate telemetry over CMessages and receive it. See README.md."""

import argparse
import asyncio
import contextlib
import csv
import math
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
from system_enums.sys_enums import SysEnums                            # noqa: E402

COMMAND_MESSAGE = 'CSignalAnalysisDataEngineCommandMessage'
TELEMETRY_MESSAGE = 'CInverterFullRateTelemetryMessage'

# Subscriptions are keyed on the decoded message's own name, which is the EMessageType
# spelling ('e...'), not the class spelling ('C...') the schema is indexed by. Subscribing
# under the class name silently matches nothing -- no error, just no telemetry.
TELEMETRY_SUBSCRIPTION = CMessageShapeCpp.normalize_cmessage('e', TELEMETRY_MESSAGE)

DEFAULT_TARGET_IP = '192.168.144.35'
DEFAULT_TARGET_PORT = 1929
DEFAULT_LOCAL_PORT = 1928
DEFAULT_CATEGORY = 'eQuadInverter'
DEFAULT_POSITION = 'e1A'
DEFAULT_RATE_HZ = 2.0

# The inverter accepts only senders whose node id resolves to ECategory::eFlightComputer.
FLIGHT_COMPUTER_IPS = ('192.168.144.1', '192.168.144.2', '192.168.144.3')
DEFAULT_LOCAL_IP = FLIGHT_COMPUTER_IPS[0]

# The telemetry is addressed to the FSDR, not to whoever sent the command: the inverter's
# FreeStandingDataRelayTransport unicasts the eTelemetry topic to node 112 on port 1771
# (FreeStandingDataRelayUnicast_SendOnly) over both flight critical networks. So the
# receiving socket has to sit somewhere that .226 traffic lands -- see README.md.
DEFAULT_TELEMETRY_PORT = 1771
DEFAULT_TELEMETRY_IP = '0.0.0.0'
FSDR_IPS = ('192.168.144.226', '192.168.145.226')
DEFAULT_SUMMARY_INTERVAL_S = 1.0

# Inert value per schema type. Arrays stay empty; the encoder pads them to arrayLength.
INERT_BY_TYPE = {'bool': False, 'Float32_t': 0.0}

# Left out of the command so send_dict stamps the CMessage time base itself, picking
# whichever of these two names the schema uses. Including it would pin it to 0.
SEND_DICT_STAMPS = ('SynchronizedSystemTimeInMicroseconds', 'UtcPosixTimeInMicroseconds')

SCHEMA_HINT = ('Pass --schema-hash, or build a purple_rain package from a branch that has\n'
               'the Signal Analysis Data Engine.')

CSV_COLUMNS = ('ReceiveTimeUtcInMicroseconds', 'SourceIp', 'EAirframePosition', 'Channel',
               'Decimation', 'FrameIndex', 'SignalCapturedTime')

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


def stamptime(command):
    """Copy the template with a fresh capture time.

    The copy is required, not defensive: send_dict mutates what it is handed and fills the
    CMessage time field only when absent, so reusing one dict freezes it at the first tick.
    """
    return dict(command, SignalProducedInFlightControllerCaptureTimeUtc=int(time.time() * 1e6))


def position_names(shape):
    """Map EPosition values to names, 27 -> 'e1A'.

    The decoder hands back the raw number. The names live in the schema package's flattened
    system_enums.yaml, which only exists beside an extracted purple_rain zip, so an empty
    map -- positions printed as numbers -- is a fine answer.
    """
    extract_dir = getattr(shape.decoder_shim_obj, 'extract_dir', '')
    path = os.path.join(extract_dir or '', 'output', 'system_enums.yaml')
    if not os.path.isfile(path):
        return {}

    try:
        enums = SysEnums(enumsFilename=path).get_enum_vals('EPosition')['enums']
        return {entry['value']: name for name, entry in enums.items()}
    except Exception as exc:  # pylint: disable=broad-except
        # Cosmetic: whatever is wrong with the yaml is not worth losing a capture over, and
        # SysEnums raises anything from KeyError to its own EnumNotFoundException.
        print(f'warning: EPosition names unavailable ({exc}); positions print as numbers',
              file=sys.stderr)
        return {}


class StreamStats:
    """Counters for one (source, channel) stream.

    `total` and the frame steps run for the whole session; the rest is a window that each
    summary print resets, so min/mean/max describe the signal now rather than ever.
    """

    def __init__(self):
        self.total = 0
        self.position = ''
        self.last_frame = None
        self.frame_deltas = {}
        self.reset_window()

    def reset_window(self):
        self.count = 0
        self.decimation = 0
        self.minimum = math.inf
        self.maximum = -math.inf
        self.total_measurement = 0.0
        self.samples = 0

    def update(self, cmesg_obj, position, measurement):
        self.total += 1
        self.count += 1
        self.position = position
        self.decimation = cmesg_obj.Decimation
        if self.last_frame is not None:
            delta = cmesg_obj.FrameIndex - self.last_frame
            self.frame_deltas[delta] = self.frame_deltas.get(delta, 0) + 1
        self.last_frame = cmesg_obj.FrameIndex
        self.minimum = min(self.minimum, min(measurement))
        self.maximum = max(self.maximum, max(measurement))
        self.total_measurement += sum(measurement)
        self.samples += len(measurement)

    def frame_step(self):
        """The usual frame step, and how many messages did not follow it (gaps, reorders)."""
        step = max(self.frame_deltas, key=self.frame_deltas.get, default=None)
        return step, self.total - 1 - self.frame_deltas.get(step, 0)


class TelemetryCollector:
    """Aggregates received telemetry.

    Full rate is one message per channel per millisecond, and the inverter emits on both
    flight critical networks, so a line per message is not an option -- everything is
    accumulated per (source, channel) and printed on an interval instead. Keying on the
    source keeps two armed inverters, and the two copies of one inverter, apart.
    """

    HEADER = (f'{"source":<15} {"pos":>4} {"ch":>5} {"msg/s":>8} {"dec":>4} '
              f'{"frame step":>10} {"irreg":>6} {"min":>11} {"mean":>11} {"max":>11}')

    def __init__(self, positions, dump=0, csv_writer=None):
        self.positions = positions
        self.dump_remaining = dump
        self.csv_writer = csv_writer
        self.streams = {}
        self.messages = 0
        self.started = time.monotonic()
        self.window_started = self.started

    def on_message(self, cmesg_obj, _cmesg_shape, src_addr):
        measurement = list(cmesg_obj.Measurement)
        source = src_addr[0]
        raw_position = getattr(cmesg_obj, 'EAirframePosition', None)
        position = self.positions.get(raw_position) or str(raw_position)

        stats = self.streams.get((source, cmesg_obj.Channel))
        if stats is None:
            stats = self.streams[(source, cmesg_obj.Channel)] = StreamStats()
        stats.update(cmesg_obj, position, measurement)
        self.messages += 1

        if self.csv_writer is not None:
            self.csv_writer.writerow([int(time.time() * 1e6), source, position,
                                      cmesg_obj.Channel, cmesg_obj.Decimation,
                                      cmesg_obj.FrameIndex,
                                      getattr(cmesg_obj, 'SignalCapturedTime', '')]
                                     + measurement)
        if self.dump_remaining:
            self.dump_remaining -= 1
            self._dump(cmesg_obj, src_addr, position, measurement)

    def _dump(self, cmesg_obj, src_addr, position, measurement):
        print(f'  msg #{self.messages} from {src_addr[0]}:{src_addr[1]} {position} '
              f'ch={cmesg_obj.Channel} dec={cmesg_obj.Decimation} '
              f'frame={cmesg_obj.FrameIndex} ({len(measurement)} samples)')
        for offset in range(0, len(measurement), 8):
            row = ' '.join(f'{value: 11.4g}' for value in measurement[offset:offset + 8])
            print(f'    [{offset:>2}] {row}')

    def report(self):
        """Print one block of per-stream rates, then start a new window."""
        now = time.monotonic()
        elapsed = max(now - self.window_started, 1e-9)
        active = sorted((key, stats) for key, stats in self.streams.items() if stats.count)

        if not active:
            print(f'  +{now - self.started:6.1f}s  no telemetry')
        else:
            print(f'  +{now - self.started:6.1f}s  {self.messages} msg total, '
                  f'{len(active)} stream(s)')
            print(f'    {self.HEADER}')
            for (source, channel), stats in active:
                step, irregular = stats.frame_step()
                print(f'    {source:<15} {stats.position:>4} {channel:>5} '
                      f'{stats.count / elapsed:>8.0f} {stats.decimation:>4} '
                      f'{"-" if step is None else step:>10} {irregular:>6} '
                      f'{stats.minimum:>11.4g} '
                      f'{stats.total_measurement / stats.samples:>11.4g} '
                      f'{stats.maximum:>11.4g}')

        self.window_started = now
        for stats in self.streams.values():
            stats.reset_window()

    def summary(self):
        elapsed = time.monotonic() - self.started
        if not self.messages:
            print(f'no {TELEMETRY_MESSAGE} received in {elapsed:.1f}s. The inverter addresses '
                  f'it to the FSDR\n({" / ".join(FSDR_IPS)}), so this socket only sees it from '
                  "inside the FSDR's\nnetwork namespace. See README.md.")
            return
        print(f'{self.messages} telemetry message(s) in {elapsed:.1f}s')
        for (source, channel), stats in sorted(self.streams.items()):
            step, irregular = stats.frame_step()
            print(f'  {source} {stats.position} ch {channel}: {stats.total} msg, '
                  f'frame step {step}, last frame {stats.last_frame}, '
                  f'{irregular} irregular step(s)')


def load_shape(args):
    try:
        return CMessageShapeCpp.from_version(
            schema_hash=args.schema_hash,
            log_level='debug' if args.verbose else 'warning')
    except OSError as exc:
        # Broad on purpose: load_library() does a bare ctypes.cdll.LoadLibrary, so there is
        # no typed error to match on, and matching dlerror() text would leave every other
        # failure (missing file, wrong arch) as a bare traceback.
        print(f'could not load libcmessage_decoder.so:\n  {exc}\n'
              f'This host has glibc {platform.libc_ver()[1]}; the .so is usually built '
              'against a newer one. Run it in the DDE container.', file=sys.stderr)
        return None


def message_spec(shape, name):
    try:
        return shape.cmesg_by_name(name)
    except (KeyError, ValueError):
        print(f'{name} is not in schema {shape.version}.\n{SCHEMA_HINT}', file=sys.stderr)
        return None


async def open_receiver(args, shape):
    """Bind the FSDR-side telemetry socket."""
    try:
        connection = await CMessageUdpConnection.connect(
            args.telemetry_ip, args.telemetry_port,
            default_shape=shape,
            verbose=args.verbose)
    except OSError as exc:
        # Deliberately not reuse_port: sharing the port with a running FSDR would split the
        # datagrams between the two sockets and quietly halve everything reported here.
        print(f'could not bind {args.telemetry_ip}:{args.telemetry_port} for telemetry:\n'
              f'  {exc}\nSomething already owns the FSDR telemetry port, most likely the '
              'free_standing_data_relay_plugin_blue_air_1\nplugin. Stop it, or point '
              '--telemetry-port elsewhere.', file=sys.stderr)
        return None

    print(f'listening for {TELEMETRY_MESSAGE} on {args.telemetry_ip}:{args.telemetry_port}')
    return connection


async def report_periodically(collector, interval):
    while True:
        await asyncio.sleep(interval)
        collector.report()


async def stream_commands(args, shape, command, stop_event):
    """Stream the command until stopped. It is its own keep-alive, so it cannot be sent once."""
    dst_addr = (args.to, args.port)

    # Each run must present a startup count the last one did not use, or the inverter's
    # sequence checker silently drops every packet at or below the previous run's seqno
    # (cmessage_asyncio restarts seqno at 1 per connection). The clock gives that for free:
    # it need not be monotonic, since a lower count reads as eOutOfSequenceStartup, which
    # is also accepted and also resets the inverter's cache. Fits int32 until 2038.
    connection = await CMessageUdpConnection.connect(
        args.local_ip, args.local_port,
        default_shape=shape,
        verbose=args.verbose,
        node_start_count=1,
        reuse_port=True)
    print(f'bound {args.local_ip}:{args.local_port} -> {args.to}:{args.port}')

    active = [channel for channel in args.channels if channel]
    if active:
        print(f'arming channels {active} at {args.rate:g} Hz '
              '(engine stops 1000 ms after the last command)')
    else:
        print('stopping: all slots disabled')

    loop = asyncio.get_running_loop()
    period = 1.0 / args.rate
    sent = 0
    next_send = loop.time()
    try:
        while not stop_event.is_set():
            connection.send_dict(COMMAND_MESSAGE, stamptime(command), dst_addr=dst_addr)
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
            disarm = stamptime(dict(command, Channels=[], Decimation=[]))
            connection.send_dict(COMMAND_MESSAGE, disarm, dst_addr=dst_addr)
            print('sent disarm (all slots 0)')
        connection.disconnect()
    return 0


async def run(args):
    shape = load_shape(args)
    if shape is None:
        return 2

    telemetry = None
    if args.watching:
        telemetry = message_spec(shape, TELEMETRY_MESSAGE)
        if telemetry is None:
            return 2

    command = None
    if not args.receive_only:
        spec = message_spec(shape, COMMAND_MESSAGE)
        if spec is None:
            return 2
        print(f'schema {shape.version}, {COMMAND_MESSAGE} is msg id {spec["id"]}')

        slots = spec['Channels']['arrayLength']
        for name in ('channels', 'decimation'):
            if len(getattr(args, name)) > slots:
                print(f'--{name}: at most {slots} values, got {len(getattr(args, name))}',
                      file=sys.stderr)
                return 2

        command = build_command(spec, args)
        if args.dry_run:
            print(f'would send {COMMAND_MESSAGE} to {args.to}:{args.port} '
                  f'from {args.local_ip}:{args.local_port}')
            for key, value in stamptime(command).items():
                print(f'  {key} = {value}')
            return 0
    else:
        print(f'schema {shape.version}, receive only')

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signal_name in ('SIGINT', 'SIGTERM'):
        loop.add_signal_handler(getattr(signal, signal_name), stop_event.set)

    collector = receiver = csv_file = reporter = None
    try:
        if args.watching:
            receiver = await open_receiver(args, shape)
            if receiver is None:
                return 2

            csv_writer = None
            if args.csv:
                csv_file = open(args.csv, 'w', newline='')
                csv_writer = csv.writer(csv_file)
                csv_writer.writerow(list(CSV_COLUMNS) + [
                    f'Measurement{index}'
                    for index in range(telemetry['Measurement']['arrayLength'])])

            collector = TelemetryCollector(position_names(shape), dump=args.dump,
                                           csv_writer=csv_writer)
            # No src_addr, so every source is accepted: the inverter's send port is
            # ephemeral, and one inverter emits on both flight critical networks.
            receiver.subscribe(TELEMETRY_SUBSCRIPTION, collector.on_message)
            reporter = asyncio.create_task(
                report_periodically(collector, args.summary_interval))

        if args.receive_only:
            print('waiting for telemetry; Ctrl-C to stop')
            await stop_event.wait()
            return 0
        return await stream_commands(args, shape, command, stop_event)
    finally:
        if reporter is not None:
            reporter.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reporter
        if receiver is not None:
            receiver.disconnect()
        if collector is not None:
            collector.summary()
        if csv_file is not None:
            csv_file.close()
            print(f'{collector.messages} row(s) written to {args.csv}')


def main():
    # This streams progress for as long as it runs and is usually piped to tee or grep,
    # where stdout would otherwise block-buffer and show nothing until it exits.
    sys.stdout.reconfigure(line_buffering=True)

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
    parser.add_argument('--schema-hash', default=None,
                        help='purple_rain USID to decode/encode with '
                             '(default: the locally built flight_simulation_2p1_gcc package)')
    parser.add_argument('--disarm-on-exit', action='store_true',
                        help='send an all-slots-disabled command on exit instead of letting '
                             'the 1 s watchdog time out')
    parser.add_argument('--watch', action='store_true',
                        help=f'also bind the telemetry port and report the '
                             f'{TELEMETRY_MESSAGE} that arrives (only useful where the '
                             "FSDR's address lands -- see README.md)")
    parser.add_argument('--receive-only', action='store_true',
                        help='send nothing, just receive telemetry; run this in the FSDR '
                             "container's network namespace while a second instance streams "
                             'the command from a flight computer address')
    parser.add_argument('--telemetry-ip', default=DEFAULT_TELEMETRY_IP,
                        help='local IP for the telemetry socket (default %(default)s, i.e. '
                             f'whichever of {" / ".join(FSDR_IPS)} this namespace owns)')
    parser.add_argument('--telemetry-port', type=int, default=DEFAULT_TELEMETRY_PORT,
                        help='local UDP port for the telemetry socket (default %(default)s, '
                             'the port the inverter unicasts telemetry to the FSDR on)')
    parser.add_argument('--summary-interval', type=float, default=DEFAULT_SUMMARY_INTERVAL_S,
                        help='seconds between per-stream telemetry summaries '
                             '(default %(default)s)')
    parser.add_argument('--csv', default=None, metavar='PATH',
                        help='write every received message to this CSV file, one row per '
                             'message (~8 MB/s at full rate)')
    parser.add_argument('--dump', type=int, default=0, metavar='N',
                        help='print the first N received messages in full, all samples '
                             '(default 0)')
    parser.add_argument('--dry-run', action='store_true',
                        help='print the message that would be sent and exit')
    parser.add_argument('--verbose', action='store_true', help='verbose logging')
    args = parser.parse_args()

    args.watching = args.watch or args.receive_only

    if (args.csv or args.dump) and not args.watching:
        parser.error('--csv/--dump only apply to received telemetry; add --watch or '
                     '--receive-only')
    if args.summary_interval <= 0:
        parser.error('--summary-interval must be positive')
    if args.watching and args.telemetry_ip not in FSDR_IPS + (DEFAULT_TELEMETRY_IP,):
        print(f'warning: the inverter unicasts telemetry to the FSDR '
              f'({", ".join(FSDR_IPS)}),\nso nothing will arrive on {args.telemetry_ip}',
              file=sys.stderr)

    if not args.receive_only:
        if args.rate <= 0:
            parser.error('--rate must be positive')
        if not any(args.channels):
            parser.error('no channels selected; every slot is the disabled sentinel 0')
        if args.local_ip not in FLIGHT_COMPUTER_IPS:
            print(f'warning: {args.local_ip} is not a flight computer address '
                  f'({", ".join(FLIGHT_COMPUTER_IPS)}); the inverter resolves the sender node '
                  'from the source IP and its FlightComputerTransport will log "dropping '
                  'packet from node N; unexpected sender"', file=sys.stderr)

        if len(args.decimation) == 1:
            args.decimation = [args.decimation[0] if channel else 0
                               for channel in args.channels]
        elif len(args.decimation) not in (0, len(args.channels)):
            print(f'warning: {len(args.decimation)} decimation values for '
                  f'{len(args.channels)} channels; unmatched slots get 0', file=sys.stderr)

        if args.rate <= 1.0 and args.count != 1:
            print(f'warning: {args.rate:g} Hz does not hold the engine watchdog open; '
                  'telemetry will start and stop', file=sys.stderr)

    return asyncio.run(run(args))


if __name__ == '__main__':
    sys.exit(main())
