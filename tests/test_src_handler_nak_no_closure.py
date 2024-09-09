import os
import random
import sys
import tempfile
from pathlib import Path

from cfdppy.defs import CfdpState
from cfdppy.handler import FsmResult
from cfdppy.handler.source import TransactionStep
from cfdppy.request import PutRequest
from cfdppy.user import TransactionFinishedParams, TransactionParams
from spacepackets.cfdp import (
    CfdpLv,
    ConditionCode,
    DirectiveType,
    PduType,
    TransactionId,
    TransmissionMode,
)
from spacepackets.cfdp.defs import ChecksumType
from spacepackets.cfdp.pdu import DeliveryCode, FileStatus
from spacepackets.cfdp.pdu.finished import FinishedParams
from spacepackets.cfdp.tlv import (
    OriginatingTransactionId,
    ProxyPutRequest,
    ProxyPutRequestParams,
    ProxyPutResponse,
    ProxyPutResponseParams,
)
from spacepackets.util import ByteFieldU8, ByteFieldU16

from .test_src_handler import TestCfdpSourceHandler


class TestCfdpSourceHandlerNackedNoClosure(TestCfdpSourceHandler):
    def setUp(self) -> None:
        self.common_setup(False, TransmissionMode.UNACKNOWLEDGED)

    def test_empty_file_nacked_by_def_config(self):
        transaction_id, _, _ = self._common_empty_file_test(
            transmission_mode=None,
        )
        fsm_res = self.source_handler.state_machine()
        self.source_handler.state_machine()
        self._verify_transaction_finished_indication(
            transaction_id,
            FinishedParams(
                condition_code=ConditionCode.NO_ERROR,
                file_status=FileStatus.FILE_STATUS_UNREPORTED,
                delivery_code=DeliveryCode.DATA_COMPLETE,
            ),
        )
        self.expected_cfdp_state = CfdpState.IDLE
        self._state_checker(fsm_res, False, CfdpState.IDLE, TransactionStep.IDLE)

    def test_empty_file_explicit_nacked(self):
        self.default_remote_cfg.default_transmission_mode = (
            TransmissionMode.ACKNOWLEDGED
        )
        transaction_id, _, _ = self._common_empty_file_test(
            transmission_mode=TransmissionMode.UNACKNOWLEDGED,
        )
        fsm_res = self.source_handler.state_machine()
        self.source_handler.state_machine()
        self._verify_transaction_finished_indication(
            transaction_id,
            FinishedParams(
                condition_code=ConditionCode.NO_ERROR,
                file_status=FileStatus.FILE_STATUS_UNREPORTED,
                delivery_code=DeliveryCode.DATA_COMPLETE,
            ),
        )
        self.expected_cfdp_state = CfdpState.IDLE
        self._state_checker(fsm_res, False, CfdpState.IDLE, TransactionStep.IDLE)

    def test_small_file_pdu_generation(self):
        file_content = "Hello World\n".encode()
        transaction_id, _, _, _ = self._common_small_file_test(
            None, False, file_content
        )
        self._verify_eof_indication(transaction_id)
        self._test_transaction_completion()

    def test_perfectly_segmented_file_pdu_generation(self):
        # This tests generates two file data PDUs
        if sys.version_info >= (3, 9):
            rand_data = random.randbytes(self.file_segment_len * 2)
        else:
            rand_data = os.urandom(self.file_segment_len * 2)
        self.source_id = ByteFieldU8(1)
        self.dest_id = ByteFieldU8(2)
        source_path = Path(f"{tempfile.gettempdir()}/two-segments.bin")
        dest_path = Path(f"{tempfile.gettempdir()}/two-segments-copy.bin")
        tparams = self._transaction_with_file_data_wrapper(
            self._generate_generic_put_req(source_path, dest_path), rand_data
        )
        self._generic_file_segment_handling(0, rand_data[0 : self.file_segment_len])
        self._generic_file_segment_handling(
            self.file_segment_len, rand_data[self.file_segment_len :]
        )
        fsm_res = self.source_handler.state_machine()
        self._test_eof_file_pdu(fsm_res, tparams.file_size, tparams.crc_32)
        self._test_transaction_completion()

    def test_segmented_file_pdu_generation(self):
        # This tests generates two file data PDUs, but the second one does not have a
        # full segment length
        if sys.version_info >= (3, 9):
            rand_data = random.randbytes(round(self.file_segment_len * 1.3))
        else:
            rand_data = os.urandom(round(self.file_segment_len * 1.3))
        self.source_id = ByteFieldU16(2)
        self.dest_id = ByteFieldU16(3)
        self.source_handler.source_id = self.source_id
        source_path = Path(f"{tempfile.gettempdir()}/hello-source.txt")
        dest_path = Path(f"{tempfile.gettempdir()}/hello-dest.txt")
        tparams = self._transaction_with_file_data_wrapper(
            self._generate_generic_put_req(source_path, dest_path), rand_data
        )
        self._generic_file_segment_handling(0, rand_data[0 : self.file_segment_len])
        self._generic_file_segment_handling(
            self.file_segment_len, rand_data[self.file_segment_len :]
        )
        fsm_res = self.source_handler.state_machine()
        self._test_eof_file_pdu(fsm_res, tparams.file_size, tparams.crc_32)
        self._test_transaction_completion()

    def test_proxy_get_request(self):
        proxy_op_params = ProxyPutRequestParams(
            self.local_cfg.local_entity_id,
            CfdpLv.from_str(f"{tempfile.gettempdir()}/source.txt"),
            CfdpLv.from_str(f"{tempfile.gettempdir()}/dest.txt"),
        )
        proxy_op = ProxyPutRequest(proxy_op_params)
        generic_tlv = proxy_op.to_generic_msg_to_user_tlv()
        put_req = PutRequest(
            destination_id=self.dest_id,
            source_file=None,
            dest_file=None,
            # Let the transmission mode be auto-determined by the remote MIB
            trans_mode=None,
            closure_requested=None,
            msgs_to_user=[generic_tlv],
        )
        self.source_handler.put_request(put_req)
        fsm_res = self.source_handler.state_machine()
        next_packet = self.source_handler.get_next_packet()
        assert next_packet is not None
        self.assertEqual(next_packet.pdu_type, PduType.FILE_DIRECTIVE)
        self.assertEqual(next_packet.pdu_directive_type, DirectiveType.METADATA_PDU)
        metadata_pdu = next_packet.to_metadata_pdu()
        self.assertIsNotNone(metadata_pdu.options)
        self.assertEqual(len(metadata_pdu.options), 1)
        self.assertEqual(metadata_pdu.options[0], generic_tlv)
        self.assertIsNone(metadata_pdu.source_file_name)
        self.assertIsNone(metadata_pdu.dest_file_name)
        self.assertEqual(fsm_res.states.state, CfdpState.BUSY)
        self.assertEqual(
            self.source_handler.transmission_mode, TransmissionMode.UNACKNOWLEDGED
        )
        self.assertEqual(fsm_res.states.step, TransactionStep.SENDING_METADATA)
        expected_id = TransactionId(
            metadata_pdu.source_entity_id, metadata_pdu.transaction_seq_num
        )
        self.cfdp_user.transaction_indication.assert_called_once_with(
            TransactionParams(expected_id)
        )
        # Now the state machine should be finished.
        fsm_res = self.source_handler.state_machine()
        finished_params = TransactionFinishedParams(
            transaction_id=expected_id,
            finished_params=FinishedParams(
                condition_code=ConditionCode.NO_ERROR,
                file_status=FileStatus.FILE_STATUS_UNREPORTED,
                delivery_code=DeliveryCode.DATA_COMPLETE,
            ),
        )
        self.cfdp_user.transaction_finished_indication.assert_called_once_with(
            finished_params
        )
        self._state_checker(fsm_res, 0, CfdpState.IDLE, TransactionStep.IDLE)

    def test_put_req_by_proxy_op(self):
        file_content = "Hello World\n".encode()
        dest_path = Path(f"{tempfile.gettempdir()}/dest.txt")
        originating_id = TransactionId(
            ByteFieldU16(5), ByteFieldU16(self.expected_seq_num)
        )
        originating_id_msg = OriginatingTransactionId(originating_id)
        put_req = PutRequest(
            destination_id=self.dest_id,
            source_file=Path(f"{tempfile.gettempdir()}/source.txt"),
            dest_file=dest_path,
            # Let the transmission mode be auto-determined by the remote MIB.
            trans_mode=None,
            closure_requested=None,
            msgs_to_user=[originating_id_msg.to_generic_msg_to_user_tlv()],
        )

        self.source_id = ByteFieldU8(1)
        self.dest_id = ByteFieldU8(2)
        self.source_handler.source_id = self.source_id
        tparams = self._transaction_with_file_data_wrapper(
            put_req, file_content, originating_id
        )
        self._generic_file_segment_handling(0, file_content)
        fsm_res = self.source_handler.state_machine()
        self._test_eof_file_pdu(fsm_res, tparams.file_size, tparams.crc_32)
        self._test_transaction_completion()

    def test_proxy_put_response_no_originating_id(self):
        """Proxy put responses should not pass the originating ID to the CFDP user to avoid
        permanent loops of trying to finish a proxy put request."""
        originating_id = TransactionId(
            ByteFieldU16(5), ByteFieldU16(self.expected_seq_num)
        )
        originating_id_msg = OriginatingTransactionId(originating_id)
        put_req = PutRequest(
            destination_id=self.dest_id,
            source_file=None,
            dest_file=None,
            # Let the transmission mode be auto-determined by the remote MIB.
            trans_mode=None,
            closure_requested=None,
            msgs_to_user=[
                ProxyPutResponse(
                    ProxyPutResponseParams.from_finished_params(
                        FinishedParams(
                            DeliveryCode.DATA_COMPLETE,
                            ConditionCode.NO_ERROR,
                            FileStatus.FILE_RETAINED,
                        )
                    )
                ).to_generic_msg_to_user_tlv(),
                originating_id_msg.to_generic_msg_to_user_tlv(),
            ],
        )

        self.source_id = ByteFieldU8(1)
        self.dest_id = ByteFieldU8(2)
        self.source_handler.source_id = self.source_id
        self._transaction_with_file_data_wrapper(
            put_req,
            data=None,
            originating_transaction_id=None,
            crc_type=ChecksumType.NULL_CHECKSUM,
        )
        self.source_handler.state_machine()
        self._test_transaction_completion()

    def _test_eof_file_pdu(self, fsm_res: FsmResult, file_size: int, crc32: bytes):
        self._state_checker(fsm_res, 1, CfdpState.IDLE, TransactionStep.IDLE)
        next_packet = self.source_handler.get_next_packet()
        assert next_packet is not None
        self.assertEqual(next_packet.pdu_type, PduType.FILE_DIRECTIVE)
        self.assertEqual(next_packet.pdu_directive_type, DirectiveType.EOF_PDU)
        eof_pdu = next_packet.to_eof_pdu()
        self.assertEqual(eof_pdu.file_size, file_size)
        self.assertEqual(eof_pdu.file_checksum, crc32)

    def _test_transaction_completion(self):
        fsm_res = self.source_handler.state_machine()
        self._state_checker(fsm_res, 0, CfdpState.IDLE, TransactionStep.IDLE)
        self.cfdp_user.transaction_finished_indication.assert_called_once()
        self.assertEqual(self.cfdp_user.transaction_finished_indication.call_count, 1)
