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
        self.assertEqual(s['plmn'], ['001-01', '001-02'])
        self.assertEqual((s['tac'], s['eci'], s['band'], s['barred']),
                         (0x0102, 0x0012345, 3, 'notBarred'))
        self.assertEqual(s['earfcn'], {1300})

    def test_mib_bandwidth(self):
        s = cellscan.summarize([gsmtap(4, encode(RRC.BCCH_BCH_Message, MIB))])
        self.assertEqual(s['bandwidth'], 'n50')

    def test_neighbour_lists(self):
        s = cellscan.summarize(
            [gsmtap(5, encode(RRC.BCCH_DL_SCH_Message, SIB4_5))])
        self.assertEqual(s['neigh_intra'], [7, 8])
        self.assertEqual(s['neigh_inter'], {1300: (5, [9])})

    def test_paging_counted_other_types_ignored(self):
        s = cellscan.summarize(
            [gsmtap(6, b'\x40'),
             gsmtap(5, b'\x00', gsmtap_type=0x12), b'junk'])
        self.assertEqual((s['paging'], s['errors'], s['earfcn']), (1, 0, set()))

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
        self.assertNotIn('eci', s)


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
