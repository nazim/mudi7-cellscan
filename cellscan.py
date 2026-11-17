#!/usr/bin/env python3
"""Read the LTE cell a GL.iNet Mudi 7 is camped on, through its DIAG socket.

The Mudi 7 has no /dev/diag. Its diag_socket_log tool connects out to a TCP
peer, and SCAT also connects out, so this script listens on both sides and
relays between them. SCAT turns DIAG into GSMTAP; pycrate decodes MIB and SIBs.
"""

import argparse
import datetime
import pathlib
import re
import shlex
import shutil
import signal
import socket
import statistics
import subprocess
import sys
import threading
import time

from pycrate_asn1dir import RRCLTE
from rich import box
from rich import console as rich_console
from rich import panel
from rich import table
from rich import text

__version__ = '0.1.1'

DEVICE_PORT = 25010
SCAT_PORT = 25012
GSMTAP_PORT = 47291
GSMTAP_LTE_RRC = 0x0D
SUB_BCCH_BCH = 4
SUB_BCCH_DL_SCH = 5
SUB_PCCH = 6
STOP_BRIDGE = ('diag_socket_log -k >/dev/null 2>&1; '
               'killall diag_socket_log 2>/dev/null; true')

_RRC = RRCLTE.EUTRA_RRC_Definitions
console = rich_console.Console()
LOGO = '  ((( • )))\n     /|\\\n    / | \\'


def ok(msg):
    """Print a passed step."""
    console.print(f'[green]✓[/] {msg}')


def fail(msg):
    """Print a failed step."""
    console.print(f'[red]✗[/] {msg}')


def parse_gsmtap(pkt):
    """Return (type, subtype, arfcn, payload) of a GSMTAP v2 packet, or None."""
    if len(pkt) < 16 or pkt[0] != 2:
        return None
    hdr_len = pkt[1] * 4
    arfcn = int.from_bytes(pkt[4:6], 'big') & 0x3FFF
    return pkt[2], pkt[12], arfcn, pkt[hdr_len:]


def decode_bcch(subtype, payload):
    """Decode a BCCH-BCH (MIB) or BCCH-DL-SCH (SIB) payload with pycrate."""
    msg = _RRC.BCCH_DL_SCH_Message
    if subtype == SUB_BCCH_BCH:
        msg = _RRC.BCCH_BCH_Message
    msg.from_uper(payload)
    return msg.get_val()


def _plmns(info):
    out, mcc = [], None
    for entry in info['plmn-IdentityList']:
        plmn = entry['plmn-Identity']
        mcc = plmn.get('mcc', mcc)
        out.append(''.join(map(str, mcc)) + '-' +
                   ''.join(map(str, plmn['mnc'])))
    return out


def _read_sib(cell, val):
    kind, body = val[1]
    if kind == 'systemInformationBlockType1':
        info = body['cellAccessRelatedInfo']
        # One update: a message that lacks a field leaves no half-filled cell.
        cell.update({
            'plmn': _plmns(info),
            'tac': info['trackingAreaCode'][0],
            'eci': info['cellIdentity'][0],
            'barred': info['cellBarred'],
            'band': body['freqBandIndicator']
        })
        return
    for sib, sib_val in body['criticalExtensions'][1]['sib-TypeAndInfo']:
        if sib == 'sib4':
            cell['neigh_intra'] = [
                c['physCellId']
                for c in sib_val.get('intraFreqNeighCellList', [])
            ]
        elif sib == 'sib5':
            for f in sib_val['interFreqCarrierFreqList']:
                cell['neigh_inter'][f['dl-CarrierFreq']] = (
                    f.get('cellReselectionPriority'), [
                        c['physCellId']
                        for c in f.get('interFreqNeighCellList', [])
                    ])
        elif sib == 'sib6':
            for f in sib_val.get('carrierFreqListUTRA-FDD', []):
                cell['utra'][f['carrierFreq']] = f.get(
                    'cellReselectionPriority')
        elif sib == 'sib24-v1530':
            for f in sib_val.get('carrierFreqListNR-r15', []):
                cell['nr'][f['carrierFreq-r15']] = (f.get(
                    'multiBandInfoList-r15',
                    []), f.get('cellReselectionPriority-r15'))


def _new_cell():
    return {'neigh_intra': [], 'neigh_inter': {}, 'utra': {}, 'nr': {}}


def summarize(packets):
    """Collect cell facts per EARFCN from raw GSMTAP packets.

    MIB and SIB2+ carry no cell identity, so the GSMTAP EARFCN is the only key
    shared by all broadcasts of one cell. 'last' is the EARFCN of the last
    broadcast, i.e. the cell the modem read most recently.
    """
    summary = {'cells': {}, 'last': None, 'errors': 0, 'paging': 0}
    for pkt in packets:
        parsed = parse_gsmtap(pkt)
        if not parsed or parsed[0] != GSMTAP_LTE_RRC:
            continue
        _, sub, arfcn, payload = parsed
        if sub == SUB_PCCH:
            summary['paging'] += 1
        if sub not in (SUB_BCCH_BCH, SUB_BCCH_DL_SCH):
            continue
        cell = summary['cells'].setdefault(arfcn, _new_cell())
        summary['last'] = arfcn
        try:
            val = decode_bcch(sub, payload)['message']
            if sub == SUB_BCCH_BCH:
                cell['bandwidth'] = val['dl-Bandwidth']
            else:
                _read_sib(cell, val)
        except Exception:  # pylint: disable=broad-exception-caught
            summary['errors'] += 1
    return summary


# SCAT log line for one LTE serving-cell measurement (ML1).
_MEAS = re.compile(r'LTE SCell: EARFCN: (\d+), PCI: \d+, '
                   r'Measured RSRP: (-?[\d.]+), .*Measured RSRQ: (-?[\d.]+)')


def measurements(log_lines):
    """Group SCAT LTE serving-cell measurements by EARFCN.

    Returns ({earfcn: [(rsrp, rsrq), ...]}, other SCell/NCell lines). The
    EARFCN is cut to 14 bits to match the GSMTAP key.
    """
    by_cell, other = {}, []
    for line in log_lines:
        m = _MEAS.search(line)
        if m:
            by_cell.setdefault(int(m[1]) & 0x3FFF, []).append(
                (float(m[2]), float(m[3])))
        elif ('SCell' in line or 'NCell' in line) and 'LTE RRC' not in line:
            other.append(line.strip())
    return by_cell, other


def cellmapper_url(plmn):
    """Link to the CellMapper map for an MCC-MNC pair."""
    mcc, mnc = plmn.split('-')
    return ('https://www.cellmapper.net/map'
            f'?MCC={int(mcc)}&MNC={int(mnc)}&type=LTE')


def _ids(values):
    return ', '.join(map(str, values)) or '-'


def _stats(values):
    return (f'{statistics.median(values):.1f} '
            f'[dim](min {min(values):.1f}, max {max(values):.1f})[/]')


def _cell_table(earfcn, cell, meas, last):
    title = f'Cell on EARFCN {earfcn}' + (' [bold](read last)[/]'
                                           if last else '')
    t = table.Table(title=title,
                    box=box.ROUNDED,
                    show_header=False,
                    title_justify='left')
    t.add_column(style='bold')
    t.add_column()
    rows = []
    if 'eci' in cell:
        eci = cell['eci']
        rows += [('PLMN', ', '.join(cell['plmn'])),
                 ('TAC', f"{cell['tac']} (0x{cell['tac']:04X})"),
                 ('ECI', f'{eci} (0x{eci:07X})'),
                 ('eNB / sector', f'[bold cyan]{eci >> 8}[/] / {eci & 0xFF}'),
                 ('Band', str(cell['band'])), ('Barred', cell['barred'])]
    else:
        rows.append(('SIB1', '[yellow]not received[/], try --reselect'))
    rows.append(('Bandwidth', cell.get('bandwidth', '?')))
    if meas:
        rows += [('RSRP dBm', _stats([m[0] for m in meas])),
                 ('RSRQ dB', _stats([m[1] for m in meas])),
                 ('Measurements', str(len(meas)))]
    for key, value in rows:
        t.add_row(key, value)
    return t


def _neigh_table(earfcn, cell):
    t = table.Table(title=f'Neighbours announced on EARFCN {earfcn}',
                    box=box.ROUNDED,
                    title_justify='left')
    for col in ('Layer', 'Frequency', 'Priority', 'PCIs'):
        t.add_column(col, overflow='fold')
    if cell['neigh_intra']:
        t.add_row('LTE same freq (SIB4)', f'EARFCN {earfcn}', '-',
                  _ids(cell['neigh_intra']))
    for freq, (prio, pcis) in cell['neigh_inter'].items():
        t.add_row('LTE (SIB5)', f'EARFCN {freq}', str(prio), _ids(pcis))
    for freq, prio in cell['utra'].items():
        t.add_row('3G (SIB6)', f'UARFCN {freq}', str(prio), '-')
    for freq, (bands, prio) in cell['nr'].items():
        t.add_row('5G NR (SIB24)',
                  f"NR-ARFCN {freq} (n{','.join(map(str, bands))})",
                  str(prio), '-')
    return t


def render(summary, log_lines):
    """Build one table pair per cell, then counts, other lines and the link."""
    by_cell, other = measurements(log_lines)
    out = []
    for earfcn, cell in summary['cells'].items():
        out.append(
            _cell_table(earfcn, cell, by_cell.get(earfcn),
                        earfcn == summary['last'] and
                        len(summary['cells']) > 1))
        neigh = _neigh_table(earfcn, cell)
        if neigh.row_count:
            out.append(neigh)
    if not any('eci' in c for c in summary['cells'].values()):
        out.append('[yellow]No SIB1 received.[/] Try --reselect.')
    out.append(f"Paging messages: {summary['paging']}   "
               f"Decode errors: {summary['errors']}")
    if not by_cell:
        out.append('Signal measurements: [dim]none received[/]')
    out += other[-5:]
    identified = [c for c in summary['cells'].values() if 'eci' in c]
    if identified:
        enbs = _ids(sorted({c['eci'] >> 8 for c in identified}))
        out.append(f"CellMapper: {cellmapper_url(identified[-1]['plmn'][0])}"
                   f'  [dim](search eNB {enbs})[/]')
    return out


class Device:
    """Run shell commands on the router over an existing SSH control socket."""

    def __init__(self, host, control_path):
        self.base = [
            'ssh', '-o', f'ControlPath={control_path}', '-o', 'BatchMode=yes',
            f'root@{host}'
        ]

    def sh(self, cmd, check=True):
        """Run a shell command on the router and return its stdout."""
        r = subprocess.run(self.base + [cmd],
                           capture_output=True,
                           text=True,
                           timeout=60,
                           check=False)
        if check and r.returncode:
            raise RuntimeError(f'{cmd!r} failed: {r.stderr.strip()}')
        return r.stdout

    def at(self, cmd):
        """Send an AT command to the modem and return the reply."""
        return self.sh(f'gl_modem -B cpu AT {shlex.quote(cmd)}')

    def wait(self, cmd, timeout):
        """Poll a shell command until it prints something; False on timeout."""
        end = time.time() + timeout
        while time.time() < end:
            if self.sh(cmd, check=False).strip():
                return True
            time.sleep(1)
        return False


def _listen(host, port):
    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    sock.listen(1)
    sock.settimeout(20)
    return sock


def _pipe(src, dst):
    try:
        while data := src.recv(65536):
            dst.sendall(data)
    except OSError:
        pass


def _local_ip(host):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.connect((host, 9))
        return sock.getsockname()[0]


def banner(args):
    """Header panel shown when a scan starts."""
    info = text.Text.assemble(
        text.Text(LOGO, style='cyan'), '\n\n', ('mudi7-cellscan ', 'bold'),
        __version__, '\n', f'router {args.host} · listen {args.seconds} s · '
        f"reselect {'on' if args.reselect else 'off'}")
    return panel.Panel.fit(info,
                           border_style='cyan',
                           subtitle='LTE cell scan over DIAG')


def _wait(label, seconds, packets):
    with console.status('') as status:
        for left in range(seconds, 0, -1):
            bcch = sum(1 for p in list(packets)
                       if (g := parse_gsmtap(p)) and g[0] == GSMTAP_LTE_RRC and
                       g[1] in (SUB_BCCH_BCH, SUB_BCCH_DL_SCH))
            status.update(f'{label} · {left} s left · {len(packets)} packets · '
                          f'{bcch} broadcast')
            time.sleep(1)


def run(args):
    """Scan once, restore the router, then print the result."""
    console.print(banner(args))
    out = pathlib.Path(
        args.out) / datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
    out.mkdir(parents=True)
    scat_bin = shutil.which('scat',
                            path=str(pathlib.Path(
                                sys.executable).parent)) or shutil.which('scat')
    if not scat_bin:
        sys.exit('scat not found; install signalcat in this environment')
    device = Device(args.host, args.control_path)
    try:
        device.sh('true')
    except (RuntimeError, subprocess.TimeoutExpired) as e:
        sys.exit(f'No SSH session to {args.host}: {e}')
    local_ip = _local_ip(args.host)
    ok(f'SSH to {args.host} works, this computer is {local_ip}')

    gsmtap_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    gsmtap_sock.bind(('127.0.0.1', GSMTAP_PORT))
    gsmtap_sock.settimeout(0.5)
    packets, stop = [], threading.Event()

    def collect():
        with open(out / 'gsmtap.txt', 'w') as f:
            while not stop.is_set():
                try:
                    pkt = gsmtap_sock.recv(65535)
                except socket.timeout:
                    continue
                packets.append(pkt)
                f.write(f'{time.time():.3f} {pkt.hex()}\n')

    rayhunter = bool(device.sh('pidof rayhunter-daemon', check=False).strip())
    radio_off = failed = False
    scat = None
    device_listener, scat_listener = _listen(local_ip, DEVICE_PORT), _listen(
        '127.0.0.1', SCAT_PORT)
    threading.Thread(target=collect, daemon=True).start()
    try:
        with console.status('Freeing DIAG'):
            if rayhunter:
                device.sh('/etc/init.d/rayhunter stop')
            device.wait(
                'ps w | grep -qE "[r]ayhunter-diag-bridge|[d]iag_socket_log" '
                '|| echo gone', 15)
            device.sh(STOP_BRIDGE)
        ok('Rayhunter stopped (DIAG has one reader at a time)'
           if rayhunter else 'DIAG is free')
        with console.status('Waiting for the router bridge'):
            device.sh(
                f'(nohup diag_socket_log -a {local_ip} -p {DEVICE_PORT} -r 30 '
                '>/dev/null 2>&1 &)')
            device_sock, peer = device_listener.accept()
            # The port is open on the LAN; relay DIAG only to the router.
            _require(peer[0] == socket.gethostbyname(args.host),
                     f'unexpected peer {peer[0]} on port {DEVICE_PORT}')
            device_sock.settimeout(None)
        ok(f'Router bridge connected to {local_ip}:{DEVICE_PORT}')
        with console.status('Starting SCAT'):
            with open(out / 'scat.log', 'w') as log:
                scat = subprocess.Popen([
                    scat_bin, '-t', 'qc', '-T', f'127.0.0.1:{SCAT_PORT}', '-P',
                    str(GSMTAP_PORT), '-F',
                    str(out / 'capture.pcap')
                ],
                                        stdout=log,
                                        stderr=subprocess.STDOUT)
            scat_sock, _ = scat_listener.accept()
            scat_sock.settimeout(None)
            threading.Thread(target=_pipe,
                             args=(device_sock, scat_sock),
                             daemon=True).start()
            threading.Thread(target=_pipe,
                             args=(scat_sock, device_sock),
                             daemon=True).start()
            time.sleep(3)
        ok('SCAT reads DIAG')
        if args.reselect:
            radio_off = True
            device.at('AT+CFUN=4')
            _wait('Radio off for a new cell selection', 10, packets)
            device.at('AT+CFUN=1')
            radio_off = False
            ok('Radio on again')
        _wait('Listening', args.seconds, packets)
        ok(f'Listened {args.seconds} s, {len(packets)} packets')
    except KeyboardInterrupt:
        fail('Interrupted, restoring the router')
    except Exception as e:  # pylint: disable=broad-exception-caught
        failed = True
        fail(f'Scan failed, restoring the router: {e or type(e).__name__}')
    finally:
        stop.set()
        console.rule('Restore')
        restore(device, scat, radio_off, rayhunter)
    log_file = out / 'scat.log'
    log_lines = log_file.read_text().splitlines() if log_file.exists() else []
    console.rule('Result')
    for item in render(summarize(packets), log_lines):
        console.print(item)
    console.print(f'Files: {out}')
    if failed:
        sys.exit(1)


def _stop_scat(scat):
    # SIGINT runs SCAT's handler: it turns DIAG logging off and exits cleanly,
    # which flushes the PCAP file. SIGTERM would leave it empty.
    scat.send_signal(signal.SIGINT)
    try:
        scat.wait(10)
    except subprocess.TimeoutExpired:
        scat.kill()
        scat.wait()


def _require(cond, message):
    if not cond:
        raise RuntimeError(message)


def restore(device, scat, radio_off, rayhunter):
    """Undo every change. Each step runs even if an earlier one fails."""
    steps = []
    if radio_off:
        steps.append(('Radio on', lambda: device.at('AT+CFUN=1')))
    if scat:
        steps.append(('SCAT stopped', lambda: _stop_scat(scat)))
    steps.append(('Router bridge stopped', lambda: device.sh(STOP_BRIDGE)))
    if rayhunter:
        steps.append(('Rayhunter started',
                      lambda: device.sh('/etc/init.d/rayhunter start')))
        steps.append(('Rayhunter DIAG bridge connected', lambda: _require(
            device.wait('netstat -nt | grep "127.0.0.1:25011 .*ESTABLISHED"', 30
                       ), 'not connected after 30 s')))
    for name, step in steps:
        try:
            step()
            ok(name)
        except Exception as e:  # pylint: disable=broad-exception-caught
            fail(f'{name}: {e}')
    try:
        cfun = device.at('AT+CFUN?')
        state = next((ln for ln in cfun.splitlines() if 'CFUN:' in ln),
                     cfun.strip())
        (ok if state.endswith(' 1') else fail)(f'Radio state {state}')
    except Exception as e:  # pylint: disable=broad-exception-caught
        fail(f'Could not read radio state: {e}')


def main():
    """Parse the command line and run a scan."""
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument('--host', default='192.168.8.1')
    p.add_argument('--control-path',
                   default='/tmp/mudi7.sock',
                   help='SSH ControlPath of an open session')
    p.add_argument('--seconds',
                   type=int,
                   default=30,
                   help='listening time after the bridge connects')
    p.add_argument('--reselect',
                   action='store_true',
                   help='turn the radio off and on to read SIB1 again')
    p.add_argument('--out', default='captures')
    run(p.parse_args())


if __name__ == '__main__':
    main()
