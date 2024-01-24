from pathlib import Path
from unittest import TestCase

from spacepackets.cfdp import CfdpLv, TransactionId
from spacepackets.cfdp.tlv import (
    OriginatingTransactionId,
    ProxyMessageType,
    ProxyPutRequest,
    ProxyPutRequestParams,
)
from spacepackets.util import ByteFieldU16

from cfdppy import PutRequest


class TestRequest(TestCase):
    def setUp(self):
        pass

    def test_printout_0(self):
        put_req = PutRequest(
            destination_id=ByteFieldU16(5),
            source_file=None,
            dest_file=None,
            trans_mode=None,
            closure_requested=None,
        )
        print_str = str(put_req)
        self.assertEqual(print_str, "Metadata Only Put Request with Destination ID 5")

    def test_printout_1(self):
        put_req = PutRequest(
            destination_id=ByteFieldU16(5),
            source_file=Path("/tmp/test.txt"),
            dest_file=Path("/tmp/test2.txt"),
            trans_mode=None,
            closure_requested=None,
        )
        print_str = str(put_req)
        self.assertTrue("Destination ID 5" in print_str)
        self.assertTrue(str(Path("/tmp/test.txt")) in print_str)
        self.assertTrue(str(Path("/tmp/test2.txt")) in print_str)
        self.assertTrue("Transmission Mode from MIB" in print_str)
        self.assertTrue("Closure Requested from MIB" in print_str)

    def test_printout_2(self):
        proxy_put = ProxyPutRequest(
            ProxyPutRequestParams(
                dest_entity_id=ByteFieldU16(2),
                source_file_name=CfdpLv.from_str("/tmp/test.txt"),
                dest_file_name=CfdpLv.from_str("/tmp/test2.txt"),
            )
        ).to_generic_msg_to_user_tlv()
        orig_id = TransactionId(
            source_entity_id=ByteFieldU16(1), transaction_seq_num=ByteFieldU16(5)
        )
        orig_id_msg = OriginatingTransactionId(orig_id).to_generic_msg_to_user_tlv()
        put_req = PutRequest(
            destination_id=ByteFieldU16(5),
            source_file=None,
            dest_file=None,
            trans_mode=None,
            closure_requested=None,
            msgs_to_user=[proxy_put, orig_id_msg],
        )
        print_str = str(put_req)
        print(print_str)
        self.assertTrue("Metadata Only Put Request with Destination ID 5" in print_str)
        self.assertTrue(
            f"Message to User 0: Proxy Operation {ProxyMessageType.PUT_REQUEST!r}"
        )
        self.assertTrue("/tmp/test.txt" in print_str)
        self.assertTrue("/tmp/test2.txt" in print_str)
        self.assertTrue("Message to User 1: Originating Transaction ID" in print_str)
