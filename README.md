# mudi7-cellscan

[![Status: beta](https://img.shields.io/badge/status-beta-yellow)](CHANGELOG.md)
[![Firmware 4.10.0](https://img.shields.io/badge/GL.iNet_firmware-4.10.0-blue)](#requirements)
[![Device: GL-E5800](https://img.shields.io/badge/device-GL--E5800_Mudi_7-blue)](#requirements)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](#requirements)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

Show which LTE cell a GL.iNet Mudi 7 (GL-E5800) is camped on, and the
neighbour cells that cell announces. It works without a SIM card.

The tool runs on your computer. It reads the modem's diagnostic (DIAG) stream
over the network, turns it into GSMTAP with [SCAT](https://github.com/fgsect/scat),
and decodes the cell broadcasts with [pycrate](https://github.com/pycrate-org/pycrate).

## Requirements

- GL.iNet Mudi 7 (GL-E5800), firmware 4.10.0. Other firmware is not tested.
- A computer on the router's LAN, with Python 3.10 or newer.
- An open SSH session to the router that uses a control socket (see below).
- Nothing else may read DIAG during a scan. The tool stops Rayhunter for the
  scan and starts it again afterwards.

## Install

```sh
git clone https://github.com/nazim/mudi7-cellscan.git
cd mudi7-cellscan
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Use

1. Open an SSH session in another terminal and leave it open:

   ```sh
   ssh -o ControlMaster=auto -o ControlPath=/tmp/mudi7.sock -o ControlPersist=4h root@192.168.8.1
   ```

2. Run the scan:

   ```sh
   .venv/bin/python cellscan.py --reselect
   ```

   `--reselect` turns the modem radio off for 10 seconds and back on. The modem
   then selects a cell again and reads its SIB1. Without it, you usually get no
   SIB1, because the modem reads SIB1 only when it selects a cell.

### Options

| Option | Default | Meaning |
|---|---|---|
| `--host` | `192.168.8.1` | Router address |
| `--control-path` | `/tmp/mudi7.sock` | SSH control socket of the open session |
| `--seconds` | `30` | Listening time after the bridge connects |
| `--reselect` | off | Turn the radio off and on to read SIB1 again |
| `--out` | `captures` | Folder for the files of each run |

### Output

The scan starts with a header panel. Each step prints ✓ or ✗, and a
countdown shows the packets received so far. At the end you get two tables:

- **Serving cell (SIB1):** PLMN, TAC, ECI, eNB and sector, band, EARFCN,
  bandwidth and barred state.
- **Neighbours announced by the cell:** LTE (SIB4, SIB5), 3G (SIB6) and
  5G NR (SIB24) frequencies, with priority and PCIs.

Below the tables are the paging count, decode errors, signal measurements
and a CellMapper link:

```text
CellMapper: https://www.cellmapper.net/map?MCC=1&MNC=1&type=LTE  (search eNB 291)
```

The link opens the map for the cell's network. CellMapper has no public API,
so the tool cannot open the cell itself. Type the eNB number into the
CellMapper search box. On OpenCelliD, search for MCC, MNC, TAC and ECI.

Each run writes a folder in `captures/`:

- `gsmtap.txt`: every GSMTAP packet, one hex line each
- `capture.pcap`: the same packets, for Wireshark
- `scat.log`: SCAT output

## What the tool changes, and how it restores

During a scan the tool:

1. Stops Rayhunter, if it runs.
2. Starts `diag_socket_log` on the router, pointed at your computer (TCP 25010).
3. With `--reselect`, sends `AT+CFUN=4`, then `AT+CFUN=1`.

When the scan ends, fails or you press Ctrl+C, the tool:

1. Sends `AT+CFUN=1` if the radio is still off.
2. Stops SCAT and the router's `diag_socket_log`.
3. Starts Rayhunter again, if it ran before, and waits for its DIAG bridge.
4. Prints the radio state.

Each restore step runs even if an earlier one fails. A failed step is printed.

## Limits

- No signal strength without a SIM. The modem sends no RSRP measurements in
  this state, so the tool cannot estimate distance.
- One cell only. The modem reads SIB1 only from the cell it camps on. Other
  cells appear only as PCI numbers in the neighbour lists.
- LTE only. 5G NR cells appear only as frequencies in SIB24.
- EARFCN comes from the GSMTAP header, which keeps 14 bits. Values above 16383
  are cut.

## Privacy

- The files in `captures/` show where you were: the cell IDs map to places.
- Paging messages carry temporary IDs (S-TMSI) of other phones near you.

Do not share or commit `captures/`. It is in `.gitignore`.

## Tests

```sh
.venv/bin/python -m unittest discover -s tests
```

The tests use made-up cell values (PLMN 001-01) encoded with pycrate.

## License

MIT, see [LICENSE](LICENSE). SCAT (GPL-2.0-or-later) runs as a separate
program. pycrate is LGPL-2.1-or-later.
