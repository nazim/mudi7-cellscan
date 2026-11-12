# Changelog

## 0.1.0 - 2026-11-12

- relay: bridges diag_socket_log on the router and SCAT on the computer over TCP
- relay: accepts the DIAG connection only from the router address
- scan: reads MIB, SIB1, SIB4, SIB5, SIB6 and SIB24 from GSMTAP
- scan: `--reselect` turns the radio off for 10 s to read SIB1 again
- scan: a failed scan restores the router, prints the result so far and exits with status 1
- summary: PLMN, TAC, ECI, eNB and sector, band, EARFCN, bandwidth, barred state, neighbour lists, paging count
- restore: radio on, bridge stopped, Rayhunter started again, each step runs even if one fails
- files: raw GSMTAP, PCAP and SCAT log per run
- output: header panel, step marks, countdown with packet counts, result tables
- output: CellMapper map link for the cell's MCC and MNC
