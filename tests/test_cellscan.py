"""Tests for cellscan. Run from the repository root."""

import unittest

from pycrate_asn1dir import RRCLTE
from rich import console as rich_console

import cellscan

RRC = RRCLTE.EUTRA_RRC_Definitions

SIB1 = {
    'message': ('c1', ('systemInformationBlockType1', {
        'cellAccessRelatedInfo': {
            'plmn-IdentityList': [
                {
                    'plmn-Identity': {
                        'mcc': [0, 0, 1],
                        'mnc': [0, 1]
                    },
                    'cellReservedForOperatorUse': 'notReserved'
                },
                {
                    'plmn-Identity': {
                        'mnc': [0, 2]
                    },
                    'cellReservedForOperatorUse': 'notReserved'
                },
            ],
            'trackingAreaCode': (0x0102, 16),
            'cellIdentity': (0x0012345, 28),
            'cellBarred': 'notBarred',
            'intraFreqReselection': 'allowed',
            'csg-Indication': False,
        },
        'cellSelectionInfo': {
            'q-RxLevMin': -60
        },
        'freqBandIndicator': 3,
        'schedulingInfoList': [{
            'si-Periodicity': 'rf16',
            'sib-MappingInfo': []
        }],
        'si-WindowLength': 'ms10',
        'systemInfoValueTag': 0,
    }))
}

MIB = {
    'message': {
        'dl-Bandwidth': 'n50',
        'phich-Config': {
            'phich-Duration': 'normal',
            'phich-Resource': 'one'
        },
        'systemFrameNumber': (0, 8),
        'schedulingInfoSIB1-BR-r13': 0,
        'systemInfoUnchanged-BR-r15': False,
        'partEARFCN-r17': ('spare', (0, 2)),
        'spare': (0, 1),
    }
}

SIB4_5 = {
    'message': ('c1', ('systemInformation', {
        'criticalExtensions': ('systemInformation-r8', {
            'sib-TypeAndInfo': [
                ('sib4', {
                    'intraFreqNeighCellList': [{
                        'physCellId': 7,
                        'q-OffsetCell': 'dB0'
                    }, {
                        'physCellId': 8,
                        'q-OffsetCell': 'dB2'
                    }]
                }),
                ('sib5', {
                    'interFreqCarrierFreqList': [{
                        'dl-CarrierFreq':
                            1300,
                        'q-RxLevMin':
                            -60,
                        't-ReselectionEUTRA':
                            2,
                        'threshX-High':
                            4,
                        'threshX-Low':
                            4,
                        'allowedMeasBandwidth':
                            'mbw6',
                        'presenceAntennaPort1':
                            True,
                        'cellReselectionPriority':
                            5,
                        'neighCellConfig': (2, 2),
                        'q-OffsetFreq':
                            'dB0',
                        'interFreqNeighCellList': [{
                            'physCellId': 9,
                            'q-OffsetCell': 'dB0'
                        }]
                    }]
                }),
            ]
        })
    }))
}


def encode(asn, value):
    asn.set_val(value)
    return asn.to_uper()


def gsmtap(subtype,
           payload,
           arfcn=1300,
           gsmtap_type=cellscan.GSMTAP_LTE_RRC,
           version=2):
    header = bytes([version, 4, gsmtap_type, 0]) + arfcn.to_bytes(
        2, 'big') + bytes(6) + bytes([subtype, 0, 0, 0])
    return header + payload


class ParseGsmtapTest(unittest.TestCase):

    def test_reads_header_fields(self):
        self.assertEqual(
            cellscan.parse_gsmtap(gsmtap(5, b'\x01\x02', arfcn=100)),
            (0x0D, 5, 100, b'\x01\x02'))

    def test_strips_flag_bits_from_arfcn(self):
        self.assertEqual(
            cellscan.parse_gsmtap(gsmtap(5, b'', arfcn=0x4000 | 6400))[2], 6400)

    def test_rejects_short_and_other_versions(self):
        self.assertIsNone(cellscan.parse_gsmtap(b'\x02\x04'))
        self.assertIsNone(cellscan.parse_gsmtap(gsmtap(5, b'\x00', version=3)))


class SummarizeTest(unittest.TestCase):

    def test_sib1_fields_and_inherited_mcc(self):
        s = cellscan.summarize(
            [gsmtap(5, encode(RRC.BCCH_DL_SCH_Message, SIB1))])
        cell = s['cells'][1300]
        self.assertEqual(cell['plmn'], ['001-01', '001-02'])
        self.assertEqual((cell['tac'], cell['eci'], cell['band'],
                          cell['barred']), (0x0102, 0x0012345, 3, 'notBarred'))
        self.assertEqual(list(s['cells']), [1300])

    def test_mib_bandwidth(self):
        s = cellscan.summarize([gsmtap(4, encode(RRC.BCCH_BCH_Message, MIB))])
        self.assertEqual(s['cells'][1300]['bandwidth'], 'n50')

    def test_neighbour_lists(self):
        s = cellscan.summarize(
            [gsmtap(5, encode(RRC.BCCH_DL_SCH_Message, SIB4_5))])
        self.assertEqual(s['cells'][1300]['neigh_intra'], [7, 8])
        self.assertEqual(s['cells'][1300]['neigh_inter'], {1300: (5, [9])})

    def test_paging_counted_other_types_ignored(self):
        s = cellscan.summarize(
            [gsmtap(6, b'\x40'),
             gsmtap(5, b'\x00', gsmtap_type=0x12), b'junk'])
        self.assertEqual((s['paging'], s['errors'], s['cells']), (1, 0, {}))

    def test_broken_payload_counted_not_raised(self):
        s = cellscan.summarize([gsmtap(5, b'')])
        self.assertEqual(s['errors'], 1)

    def test_unhandled_message_variants_counted_not_raised(self):
        extension = {'message': ('messageClassExtension', {})}
        future = {
            'message': ('c1', ('systemInformation', {
                'criticalExtensions': ('criticalExtensionsFuture-r15',
                                       ('criticalExtensionsFuture', {}))
            }))
        }
        s = cellscan.summarize([
            gsmtap(5, encode(RRC.BCCH_DL_SCH_Message, v))
            for v in (extension, future)
        ])
        self.assertEqual(s['errors'], 2)
        self.assertNotIn('eci', s['cells'][1300])

    def test_two_cells_kept_apart(self):
        body = dict(SIB1['message'][1][1])
        body['cellAccessRelatedInfo'] = dict(body['cellAccessRelatedInfo'],
                                             cellIdentity=(0x0012399, 28))
        body['freqBandIndicator'] = 1
        other = {'message': ('c1', ('systemInformationBlockType1', body))}
        s = cellscan.summarize([
            gsmtap(5, encode(RRC.BCCH_DL_SCH_Message, SIB1), arfcn=1300),
            gsmtap(4, encode(RRC.BCCH_BCH_Message, MIB), arfcn=1300),
            gsmtap(5, encode(RRC.BCCH_DL_SCH_Message, SIB4_5), arfcn=1300),
            gsmtap(5, encode(RRC.BCCH_DL_SCH_Message, other), arfcn=400),
        ])
        a, b = s['cells'][1300], s['cells'][400]
        self.assertEqual((a['eci'], a['band'], a['bandwidth']),
                         (0x0012345, 3, 'n50'))
        self.assertEqual((b['eci'], b['band']), (0x0012399, 1))
        self.assertNotIn('bandwidth', b)
        self.assertEqual((a['neigh_intra'], b['neigh_intra']), ([7, 8], []))
        self.assertEqual(s['last'], 400)


SCAT_LOG = [
    'Radio 0: LTE SCell: EARFCN: 1300, PCI: 5, Measured RSRP: -88.00, '
    'Measured RSSI: -55.00, Measured RSRQ: -12.00',
    'Radio 0: LTE RRC SCell Info: EARFCN: 1300/19300, Band: 3, PCI: 5',
    'Radio 0: LTE SCell: EARFCN: 1300, PCI: 5, Measured RSRP: -90.00, '
    'Measured RSSI: -57.00, Measured RSRQ: -14.00',
    'Radio 0: LTE SCell: EARFCN: 400, PCI: 5, Measured RSRP: -100.50, '
    'Measured RSSI: -70.00, Measured RSRQ: -11.25',
    'Radio 0: NR RRC SCell Info: NR-ARFCN: 365070/345092, PCI: 781',
    'unrelated line',
]


class MeasurementsTest(unittest.TestCase):

    def test_grouped_by_earfcn_rest_kept(self):
        by_cell, other = cellscan.measurements(SCAT_LOG)
        self.assertEqual(by_cell, {
            1300: [(-88.0, -12.0), (-90.0, -14.0)],
            400: [(-100.5, -11.25)]
        })
        self.assertEqual(other, [SCAT_LOG[4]])

    def test_earfcn_cut_to_gsmtap_bits(self):
        line = SCAT_LOG[0].replace('EARFCN: 1300', 'EARFCN: 66786')
        self.assertEqual(list(cellscan.measurements([line])[0]),
                         [66786 & 0x3FFF])


def as_text(items):
    console = rich_console.Console(record=True, width=200, color_system=None)
    for item in items:
        console.print(item)
    return console.export_text()


class RenderTest(unittest.TestCase):

    def test_enb_sector_and_link(self):
        s = cellscan.summarize(
            [gsmtap(5, encode(RRC.BCCH_DL_SCH_Message, SIB1))])
        text = as_text(cellscan.render(s, []))
        self.assertIn('291 / 69', text)
        self.assertIn('258 (0x0102)', text)
        self.assertIn('Signal measurements: none received', text)
        self.assertIn('https://www.cellmapper.net/map?MCC=1&MNC=1&type=LTE',
                      text)
        self.assertIn('search eNB 291', text)

    def test_neighbour_rows(self):
        s = cellscan.summarize(
            [gsmtap(5, encode(RRC.BCCH_DL_SCH_Message, SIB4_5))])
        text = as_text(cellscan.render(s, []))
        self.assertIn('LTE same freq (SIB4)', text)
        self.assertIn('EARFCN 1300', text)

    def test_measurements_in_their_cell_and_last_marked(self):
        s = cellscan.summarize([
            gsmtap(5, encode(RRC.BCCH_DL_SCH_Message, SIB1), arfcn=400),
            gsmtap(5, encode(RRC.BCCH_DL_SCH_Message, SIB1), arfcn=1300),
        ])
        text = as_text(cellscan.render(s, SCAT_LOG))
        first, second = text.split('Cell on EARFCN 1300')
        self.assertIn('-100.5', first)
        self.assertNotIn('-89.0', first)
        self.assertIn('(read last)', second[:20])
        self.assertIn('-89.0 (min -90.0, max -88.0)', second)
        self.assertNotIn('none received', text)
        self.assertIn('NR RRC SCell Info', text)

    def test_no_sib1_hint_and_no_link(self):
        text = as_text(cellscan.render(cellscan.summarize([]), []))
        self.assertIn('No SIB1 received. Try --reselect.', text)
        self.assertNotIn('cellmapper', text)


class CellmapperUrlTest(unittest.TestCase):

    def test_drops_leading_zeros(self):
        self.assertEqual(cellscan.cellmapper_url('001-01'),
                         'https://www.cellmapper.net/map?MCC=1&MNC=1&type=LTE')


if __name__ == '__main__':
    unittest.main()
