import os
import struct
from pyfakefs.fake_filesystem_unittest import TestCase
from tempfile import gettempdir
from pathlib import Path
from cfdppy.handler.crc import CrcHelper, calc_modular_checksum
from cfdppy.user import HostFilestore
from spacepackets.cfdp import ChecksumType


EXAMPLE_DATA_CFDP = bytes(
    [
        0x00,
        0x01,
        0x02,
        0x03,
        0x04,
        0x05,
        0x06,
        0x07,
        0x08,
        0x09,
        0x0A,
        0x0B,
        0x0C,
        0x0D,
        0x0E,
    ]
)


class TestChecksumHelper(TestCase):
    def setUp(self):
        self.setUpPyfakefs()
        self.crc_helper = CrcHelper(ChecksumType.NULL_CHECKSUM, HostFilestore())
        self.file_path = Path(f"{gettempdir()}/crc_file")
        with open(self.file_path, "wb") as file:
            file.write(EXAMPLE_DATA_CFDP)
        # Kind of re-writing the modular checksum impl here which we are trying to test, but the
        # numbers/correctness were verified manually using calculators, so this is okay.
        segments_to_add = []
        for i in range(4):
            if (i + 1) * 4 > len(EXAMPLE_DATA_CFDP):
                data_to_add = EXAMPLE_DATA_CFDP[i * 4 :].ljust(4, bytes([0]))
            else:
                data_to_add = EXAMPLE_DATA_CFDP[i * 4 : (i + 1) * 4]
            segments_to_add.append(
                int.from_bytes(
                    data_to_add,
                    byteorder="big",
                    signed=False,
                )
            )
        full_sum = sum(segments_to_add)
        full_sum %= 2**32

        self.expected_checksum_for_example = struct.pack("!I", full_sum)

    def test_modular_checksum(self):
        self.assertEqual(
            calc_modular_checksum(self.file_path), self.expected_checksum_for_example
        )

    def tearDown(self):
        if self.file_path.exists():
            os.remove(self.file_path)
