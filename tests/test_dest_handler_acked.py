import struct
import time
from typing import List, Tuple

from cfdppy.defs import CfdpState
from cfdppy.handler.dest import FsmResult, TransactionStep
from cfdppy.user import MetadataRecvParams, TransactionFinishedParams
from spacepackets.cfdp import (
    NULL_CHECKSUM_U32,
    ConditionCode,
    DirectiveType,
    EntityIdTlv,
    PduType,
    TransmissionMode,
)
from spacepackets.cfdp.pdu import (
    AckPdu,
    DeliveryCode,
    EofPdu,
    FileStatus,
    FinishedParams,
    FinishedPdu,
    TransactionStatus,
)
from spacepackets.crc import mkPredefinedCrcFun

from .test_dest_handler import TestDestHandlerBase


class TestDestHandlerAcked(TestDestHandlerBase):
    def setUp(self) -> None:
        self.common_setup(TransmissionMode.ACKNOWLEDGED)

    def test_acked_empty_transfer(self):
        # Basic acknowledged empty file transfer.
        self._generic_regular_transfer_init(0)
        fsm_res = self._generic_insert_eof_pdu(0, NULL_CHECKSUM_U32)
        self._generic_eof_recv_indication_check(fsm_res)
        self._generic_verify_eof_ack_packet(fsm_res)
        fsm_res = self.dest_handler.state_machine()
        finished_pdu = self._generic_no_error_finished_pdu_check_acked(fsm_res)
        self._generic_verify_transfer_completion(fsm_res, bytes())
        self._generic_insert_finished_pdu_ack(finished_pdu)

    def test_acked_small_file_transfer(self):
        file_content = "Hello World!".encode()
        with open(self.src_file_path, "wb") as of:
            of.write(file_content)
        crc32_func = mkPredefinedCrcFun("crc32")
        crc32 = struct.pack("!I", crc32_func(file_content))
        # Basic acknowledged empty file transfer.
        self._generic_regular_transfer_init(len(file_content))
        self._insert_file_segment(file_content, 0)
        fsm_res = self._generic_insert_eof_pdu(len(file_content), crc32)
        self._generic_eof_recv_indication_check(fsm_res)
        self._generic_verify_eof_ack_packet(fsm_res)
        fsm_res = self.dest_handler.state_machine()
        finished_pdu = self._generic_no_error_finished_pdu_check_acked(fsm_res)
        self._generic_verify_transfer_completion(fsm_res, file_content)
        self._generic_insert_finished_pdu_ack(finished_pdu)

    def test_cancelled_file_transfer(self):
        file_content = "Hello World!".encode()
        with open(self.src_file_path, "wb") as of:
            of.write(file_content)
        # Basic acknowledged empty file transfer.
        self._generic_regular_transfer_init(len(file_content))
        # Cancel the transfer by sending an EOF PDU with the appropriate parameters.
        eof_pdu = EofPdu(
            file_size=0,
            file_checksum=NULL_CHECKSUM_U32,
            pdu_conf=self.src_pdu_conf,
            condition_code=ConditionCode.CANCEL_REQUEST_RECEIVED,
        )
        fsm_res = self.dest_handler.state_machine(eof_pdu)
        # Should contain an ACK PDU now.
        self._generic_verify_eof_ack_packet(
            fsm_res, condition_code_of_acked_pdu=ConditionCode.CANCEL_REQUEST_RECEIVED
        )
        fsm_res = self.dest_handler.state_machine()
        finished_pdu = self._generic_finished_pdu_check_acked(
            fsm_res,
            expected_condition_code=ConditionCode.CANCEL_REQUEST_RECEIVED,
            expected_fault_location=EntityIdTlv(self.src_entity_id.as_bytes),
        )
        self._generic_verify_transfer_completion(
            fsm_res,
            expected_file_data=None,
            expected_finished_params=FinishedParams(
                condition_code=ConditionCode.CANCEL_REQUEST_RECEIVED,
                delivery_code=DeliveryCode.DATA_INCOMPLETE,
                file_status=FileStatus.FILE_RETAINED,
                fault_location=EntityIdTlv(self.src_entity_id.as_bytes),
            ),
        )
        self._generic_insert_finished_pdu_ack(finished_pdu)

    def test_deferred_missing_file_segment_handling(self):
        file_content = "Hello World!".encode()
        with open(self.src_file_path, "wb") as of:
            of.write(file_content)
        crc32_func = mkPredefinedCrcFun("crc32")
        crc32 = struct.pack("!I", crc32_func(file_content))
        self._generic_regular_transfer_init(len(file_content))
        self._insert_file_segment(file_content[0:5], 0)
        fsm_res = self._generic_insert_eof_pdu(len(file_content), crc32)
        self._generic_eof_recv_indication_check(fsm_res)
        self._generic_verify_eof_ack_packet(fsm_res)
        self.dest_handler.state_machine()
        self._state_checker(
            fsm_res, 1, CfdpState.BUSY, TransactionStep.WAITING_FOR_MISSING_DATA
        )
        self.assertTrue(self.dest_handler.deferred_lost_segment_procedure_active)
        self.assertEqual(self.dest_handler.nak_activity_counter, 0)
        self._generic_verify_missing_segment_requested(
            0, len(file_content), [(5, len(file_content))]
        )
        fsm_res = self._insert_file_segment(
            file_content[5:],
            5,
            expected_packets=1,
            expected_step=TransactionStep.WAITING_FOR_FINISHED_ACK,
        )
        finished_pdu = self._generic_no_error_finished_pdu_check_acked(fsm_res)
        self._generic_verify_transfer_completion(fsm_res, file_content)
        self._generic_insert_finished_pdu_ack(finished_pdu)

    def test_immediate_missing_file_seg_handling_0(self):
        file_content = "Hello World!".encode()
        with open(self.src_file_path, "wb") as of:
            of.write(file_content)
        crc32_func = mkPredefinedCrcFun("crc32")
        crc32 = struct.pack("!I", crc32_func(file_content))
        self._generic_regular_transfer_init(len(file_content))
        # A middle segment is missing now, with the expected lost segment tuple to be (3, 6). The
        # lost segment is immediately supplied.
        self._insert_file_segment(file_content[0:3], 0)
        self._insert_file_segment(file_content[6:], 6, 1)
        self._generic_verify_missing_segment_requested(0, len(file_content), [(3, 6)])
        # Insert the missing file content.
        self._insert_file_segment(file_content[3:6], 3)
        # All lost segments were delivered, regular transfer finish.
        fsm_res = self._generic_insert_eof_pdu(len(file_content), crc32)
        self._generic_eof_recv_indication_check(fsm_res)
        self._generic_verify_eof_ack_packet(fsm_res)
        fsm_res = self.dest_handler.state_machine()
        finished_pdu = self._generic_no_error_finished_pdu_check_acked(fsm_res)
        self._generic_verify_transfer_completion(fsm_res, file_content)
        self._generic_insert_finished_pdu_ack(finished_pdu)

    def test_immediate_missing_file_seg_handling_1(self):
        file_content = "Hello World!".encode()
        with open(self.src_file_path, "wb") as of:
            of.write(file_content)
        crc32_func = mkPredefinedCrcFun("crc32")
        crc32 = struct.pack("!I", crc32_func(file_content))
        self._generic_regular_transfer_init(len(file_content))
        # Simulate the second segment being lost, with more than one segment following after that.
        self._insert_file_segment(file_content[0:2], 0)
        self._insert_file_segment(file_content[4:6], 4, 1)
        self._generic_verify_missing_segment_requested(0, 6, [(2, 4)])
        # Insert the last file segment.
        self._insert_file_segment(file_content[6:], 6)
        # Now insert the missing segment.
        self._insert_file_segment(file_content[2:4], 2)
        # All lost segments were delivered, regular transfer finish.
        fsm_res = self._generic_insert_eof_pdu(len(file_content), crc32)
        self._generic_eof_recv_indication_check(fsm_res)
        self._generic_verify_eof_ack_packet(fsm_res)
        fsm_res = self.dest_handler.state_machine()
        finished_pdu = self._generic_no_error_finished_pdu_check_acked(fsm_res)
        self._generic_verify_transfer_completion(fsm_res, file_content)
        self._generic_insert_finished_pdu_ack(finished_pdu)

    def test_immediate_multi_missing_segment_handling(self):
        file_content = "Hello World!".encode()
        with open(self.src_file_path, "wb") as of:
            of.write(file_content)
        crc32_func = mkPredefinedCrcFun("crc32")
        crc32 = struct.pack("!I", crc32_func(file_content))
        self._generic_regular_transfer_init(len(file_content))
        self._insert_file_segment(file_content[0:2], 0)
        self._insert_file_segment(file_content[4:6], 4, 1)
        # First missing segment.
        self._generic_verify_missing_segment_requested(0, 6, [(2, 4)])

        # Second missing segment directly after that.
        self._insert_file_segment(file_content[8:], 8, 1)
        self._generic_verify_missing_segment_requested(0, len(file_content), [(6, 8)])

        # Supply the 2 missing file segments.
        self._insert_file_segment(file_content[2:4], 2)
        self._insert_file_segment(file_content[6:8], 6)
        # All lost segments were delivered, regular transfer finish.
        fsm_res = self._generic_insert_eof_pdu(len(file_content), crc32)
        self._generic_eof_recv_indication_check(fsm_res)
        self._generic_verify_eof_ack_packet(fsm_res)
        fsm_res = self.dest_handler.state_machine()
        finished_pdu = self._generic_no_error_finished_pdu_check_acked(fsm_res)
        self._generic_verify_transfer_completion(fsm_res, file_content)
        self._generic_insert_finished_pdu_ack(finished_pdu)

    def test_immediate_missing_segment_also_rerequested_after_eof(self):
        file_content = "Hello World!".encode()
        with open(self.src_file_path, "wb") as of:
            of.write(file_content)
        crc32_func = mkPredefinedCrcFun("crc32")
        crc32 = struct.pack("!I", crc32_func(file_content))
        self._generic_regular_transfer_init(len(file_content))
        # Missing middle segment
        self._insert_file_segment(file_content[0:2], 0)
        self._insert_file_segment(file_content[6:], 6, 1)
        # Missing segment immediately re-requested.
        self._generic_verify_missing_segment_requested(0, len(file_content), [(2, 6)])

        # All lost segments were delivered, regular transfer finish.
        fsm_res = self._generic_insert_eof_pdu(len(file_content), crc32)
        self._generic_eof_recv_indication_check(fsm_res)
        self._generic_verify_eof_ack_packet(fsm_res)

        self.dest_handler.state_machine()
        self._state_checker(
            fsm_res, 1, CfdpState.BUSY, TransactionStep.WAITING_FOR_MISSING_DATA
        )
        self.assertTrue(self.dest_handler.deferred_lost_segment_procedure_active)
        self.assertEqual(self.dest_handler.nak_activity_counter, 0)
        self._generic_verify_missing_segment_requested(0, len(file_content), [(2, 6)])
        fsm_res = self._insert_file_segment(
            file_content[2:6],
            2,
            1,
            expected_step=TransactionStep.WAITING_FOR_FINISHED_ACK,
        )
        finished_pdu = self._generic_no_error_finished_pdu_check_acked(fsm_res)
        self._generic_verify_transfer_completion(fsm_res, file_content)
        self._generic_insert_finished_pdu_ack(finished_pdu)

    def test_multi_segment_missing_deferred_handling(self):
        file_content = "Hello World!".encode()
        with open(self.src_file_path, "wb") as of:
            of.write(file_content)
        crc32_func = mkPredefinedCrcFun("crc32")
        crc32 = struct.pack("!I", crc32_func(file_content))
        self._generic_regular_transfer_init(len(file_content))
        self._insert_file_segment(file_content[0:2], 0)
        self._insert_file_segment(file_content[4:6], 4, 1)
        # First missing segment.
        self._generic_verify_missing_segment_requested(0, 6, [(2, 4)])

        # Second missing segment directly after that.
        self._insert_file_segment(file_content[8:], 8, 1)
        self._generic_verify_missing_segment_requested(0, len(file_content), [(6, 8)])

        fsm_res = self._generic_insert_eof_pdu(len(file_content), crc32)
        self._generic_eof_recv_indication_check(fsm_res)
        self._generic_verify_eof_ack_packet(fsm_res)

        self.dest_handler.state_machine()
        self._state_checker(
            fsm_res, 1, CfdpState.BUSY, TransactionStep.WAITING_FOR_MISSING_DATA
        )
        self.assertTrue(self.dest_handler.deferred_lost_segment_procedure_active)
        self.assertEqual(self.dest_handler.nak_activity_counter, 0)
        # We now receive a NAK sequence with both missing file segments.
        self._generic_verify_missing_segment_requested(
            0, len(file_content), [(2, 4), (6, 8)]
        )
        # We insert both missing file segments.
        fsm_res = self._insert_file_segment(
            file_content[2:4],
            2,
            expected_packets=0,
            expected_step=TransactionStep.WAITING_FOR_MISSING_DATA,
        )
        fsm_res = self._insert_file_segment(
            file_content[6:8],
            6,
            expected_packets=1,
            expected_step=TransactionStep.WAITING_FOR_FINISHED_ACK,
        )
        # Done.
        finished_pdu = self._generic_no_error_finished_pdu_check_acked(fsm_res)
        self._generic_verify_transfer_completion(fsm_res, file_content)
        self._generic_insert_finished_pdu_ack(finished_pdu)

    def test_missing_metadata_pdu(self):
        file_content = "Hello World!".encode()
        with open(self.src_file_path, "wb") as of:
            of.write(file_content)
        crc32_func = mkPredefinedCrcFun("crc32")
        crc32 = struct.pack("!I", crc32_func(file_content))
        self._insert_file_segment(
            file_content[0:2],
            0,
            expected_packets=1,
            check_indication=False,
            expected_step=TransactionStep.WAITING_FOR_METADATA,
        )
        next_pdu = self.dest_handler.get_next_packet()
        self.assertIsNotNone(next_pdu)
        self.assertEqual(next_pdu.pdu_type, PduType.FILE_DIRECTIVE)
        self.assertEqual(next_pdu.pdu_directive_type, DirectiveType.NAK_PDU)
        nak_pdu = next_pdu.to_nak_pdu()
        self.assertEqual(nak_pdu.start_of_scope, 0)
        self.assertEqual(nak_pdu.end_of_scope, 2)
        # Metadata and the segment we just sent are immediately re-requested.
        self.assertEqual(nak_pdu.segment_requests, [(0, 0), (0, 2)])

        self._generic_transfer_init(
            len(file_content),
            expected_init_packets=0,
            expected_init_state=CfdpState.BUSY,
            expected_init_step=TransactionStep.WAITING_FOR_METADATA,
        )
        self._insert_file_segment(
            file_content[0:2],
            0,
        )
        self._insert_file_segment(
            file_content[2:],
            2,
        )
        # All lost segments were delivered, regular transfer finish.
        fsm_res = self._generic_insert_eof_pdu(len(file_content), crc32)
        self._generic_eof_recv_indication_check(fsm_res)
        self._generic_verify_eof_ack_packet(fsm_res)
        fsm_res = self.dest_handler.state_machine()
        finished_pdu = self._generic_no_error_finished_pdu_check_acked(fsm_res)
        self._generic_verify_transfer_completion(fsm_res, file_content)
        self._generic_insert_finished_pdu_ack(finished_pdu)

    def test_metadata_eof_only_missing_metadata(self):
        fsm_res = self._generic_insert_eof_pdu(0, NULL_CHECKSUM_U32)
        self._generic_eof_recv_indication_check(fsm_res)
        self._generic_verify_eof_ack_packet(fsm_res)
        self.dest_handler.state_machine()
        self._state_checker(
            fsm_res, 1, CfdpState.BUSY, TransactionStep.WAITING_FOR_METADATA
        )
        self._generic_verify_missing_segment_requested(0, 0, [(0, 0)])
        fsm_res = self._generic_transfer_init(
            0,
            expected_init_packets=0,
            expected_init_state=CfdpState.BUSY,
            expected_init_step=TransactionStep.WAITING_FOR_METADATA,
        )
        self._state_checker(
            fsm_res, 1, CfdpState.BUSY, TransactionStep.WAITING_FOR_FINISHED_ACK
        )
        finished_pdu = self._generic_no_error_finished_pdu_check_acked(fsm_res)
        self._generic_verify_transfer_completion(fsm_res, bytes())
        self._generic_insert_finished_pdu_ack(finished_pdu)

    def _generic_deferred_lost_segment_handling_with_timeout(self, file_content: bytes):
        with open(self.src_file_path, "wb") as of:
            of.write(file_content)
        crc32_func = mkPredefinedCrcFun("crc32")
        crc32 = struct.pack("!I", crc32_func(file_content))
        self._generic_regular_transfer_init(len(file_content))
        self._insert_file_segment(file_content[0:2], 0)
        self._insert_file_segment(file_content[4:6], 4, 1)
        # First missing segment.
        self._generic_verify_missing_segment_requested(0, 6, [(2, 4)])

        # Second missing segment directly after that.
        self._insert_file_segment(file_content[8:], 8, 1)
        self._generic_verify_missing_segment_requested(0, len(file_content), [(6, 8)])

        # This should trigger deferred EOF handling.
        fsm_res = self._generic_insert_eof_pdu(len(file_content), crc32)
        self._generic_eof_recv_indication_check(fsm_res)
        self._generic_verify_eof_ack_packet(fsm_res)
        self.dest_handler.state_machine()
        self._state_checker(
            fsm_res, 1, CfdpState.BUSY, TransactionStep.WAITING_FOR_MISSING_DATA
        )
        self.assertTrue(self.dest_handler.deferred_lost_segment_procedure_active)
        self.assertEqual(self.dest_handler.nak_activity_counter, 0)
        # We now receive a NAK sequence with both missing file segments.
        self._generic_verify_missing_segment_requested(
            0, len(file_content), [(2, 4), (6, 8)]
        )
        time.sleep(self.timeout_nak_procedure_seconds * 1.1)
        self.dest_handler.state_machine()
        self.assertTrue(self.dest_handler.deferred_lost_segment_procedure_active)
        self.assertEqual(self.dest_handler.nak_activity_counter, 1)
        # We now receive a NAK sequence with both missing file segments.
        self._generic_verify_missing_segment_requested(
            0, len(file_content), [(2, 4), (6, 8)]
        )

    def test_deferred_lost_segment_handling_after_timeout(self):
        file_content = "Hello World!".encode()
        self._generic_deferred_lost_segment_handling_with_timeout(file_content)
        time.sleep(self.timeout_nak_procedure_seconds * 1.1)
        fsm_res = self.dest_handler.state_machine()
        self._generic_finished_pdu_with_error_check(
            fsm_res,
            cond_code=ConditionCode.NAK_LIMIT_REACHED,
            delivery_code=DeliveryCode.DATA_INCOMPLETE,
            file_status=FileStatus.FILE_RETAINED,
        )

    def test_deferred_lost_segment_handling_after_timeout_activity_reset(self):
        file_content = "Hello World!".encode()
        self._generic_deferred_lost_segment_handling_with_timeout(file_content)
        # Insert one segment, which should reset the NAK activity parameters.
        self._insert_file_segment(
            file_content[2:4],
            2,
            expected_packets=0,
            expected_step=TransactionStep.WAITING_FOR_MISSING_DATA,
        )
        self.dest_handler.state_machine()
        # Now that we inserted a packet, the NAK activity counter should be reset.
        self.assertTrue(self.dest_handler.deferred_lost_segment_procedure_active)
        self.assertEqual(self.dest_handler.nak_activity_counter, 0)
        self._state_checker(
            None, 0, CfdpState.BUSY, TransactionStep.WAITING_FOR_MISSING_DATA
        )
        time.sleep(self.timeout_nak_procedure_seconds * 1.1)
        self.dest_handler.state_machine()
        self.assertTrue(self.dest_handler.deferred_lost_segment_procedure_active)
        self.assertEqual(self.dest_handler.nak_activity_counter, 1)
        # We now receive a NAK sequence with the only file segment missing
        self._generic_verify_missing_segment_requested(0, len(file_content), [(6, 8)])
        fsm_res = self._insert_file_segment(
            file_content[6:8],
            6,
            expected_packets=1,
            expected_step=TransactionStep.WAITING_FOR_FINISHED_ACK,
        )
        finished_pdu = self._generic_no_error_finished_pdu_check_acked(fsm_res)
        self._generic_verify_transfer_completion(fsm_res, file_content)
        self._generic_insert_finished_pdu_ack(finished_pdu)

    def test_positive_ack_procedure_finished_pdu(self):
        # Basic acknowledged empty file transfer.
        self._generic_regular_transfer_init(0)
        fsm_res = self._generic_insert_eof_pdu(0, NULL_CHECKSUM_U32)
        self._generic_eof_recv_indication_check(fsm_res)
        self._generic_verify_eof_ack_packet(fsm_res)
        fsm_res = self.dest_handler.state_machine()
        self._generic_no_error_finished_pdu_check_acked(fsm_res)
        self._generic_verify_transfer_completion(fsm_res, bytes())
        time.sleep(self.timeout_positive_ack_procedure_seconds * 1.1)
        fsm_res = self.dest_handler.state_machine()
        self.assertEqual(self.dest_handler.positive_ack_counter, 1)
        self._state_checker(
            fsm_res, 1, CfdpState.BUSY, TransactionStep.WAITING_FOR_FINISHED_ACK
        )
        self._generic_no_error_finished_pdu_check_acked(fsm_res)
        fsm_res = self.dest_handler.state_machine()
        self.assertEqual(self.dest_handler.positive_ack_counter, 1)
        time.sleep(self.timeout_positive_ack_procedure_seconds * 1.1)
        fsm_res = self.dest_handler.state_machine()
        self._generic_finished_pdu_with_error_check(
            fsm_res,
            ConditionCode.POSITIVE_ACK_LIMIT_REACHED,
            DeliveryCode.DATA_COMPLETE,
            FileStatus.FILE_RETAINED,
        )

    def test_metadata_only_transfer(self):
        options = self._generate_put_response_opts()
        metadata_pdu = self._generate_metadata_only_metadata(options)
        fsm_res = self.dest_handler.state_machine(metadata_pdu)
        # Done immediately. The only thing we need to do is check the two user indications.
        self.cfdp_user.metadata_recv_indication.assert_called_once()
        self.cfdp_user.metadata_recv_indication.assert_called_with(
            MetadataRecvParams(
                self.transaction_id,
                self.src_pdu_conf.source_entity_id,
                None,
                None,
                None,
                options,
            )
        )
        self.cfdp_user.transaction_finished_indication.assert_called_once()
        self.cfdp_user.transaction_finished_indication.assert_called_with(
            TransactionFinishedParams(
                self.transaction_id,
                FinishedParams(
                    delivery_code=DeliveryCode.DATA_COMPLETE,
                    condition_code=ConditionCode.NO_ERROR,
                    file_status=FileStatus.FILE_STATUS_UNREPORTED,
                ),
            )
        )
        finished_pdu = self._generic_no_error_finished_pdu_check_acked(
            fsm_res, expected_file_status=FileStatus.FILE_STATUS_UNREPORTED
        )
        self._generic_verify_transfer_completion(
            fsm_res,
            expected_file_data=None,
            expected_finished_params=FinishedParams(
                condition_code=ConditionCode.NO_ERROR,
                file_status=FileStatus.FILE_STATUS_UNREPORTED,
                delivery_code=DeliveryCode.DATA_COMPLETE,
            ),
        )
        self._generic_insert_finished_pdu_ack(finished_pdu)

    def _generic_finished_pdu_with_error_check(
        self,
        fsm_res: FsmResult,
        cond_code: ConditionCode,
        delivery_code: DeliveryCode,
        file_status: FileStatus,
    ):
        self._state_checker(
            fsm_res, 1, CfdpState.BUSY, TransactionStep.WAITING_FOR_FINISHED_ACK
        )
        next_pdu = self.dest_handler.get_next_packet()
        self.assertIsNotNone(next_pdu)
        self.assertEqual(next_pdu.pdu_type, PduType.FILE_DIRECTIVE)
        self.assertEqual(next_pdu.pdu_directive_type, DirectiveType.FINISHED_PDU)
        finished_pdu = next_pdu.to_finished_pdu()
        self.assertEqual(finished_pdu.condition_code, cond_code)
        self.assertEqual(finished_pdu.delivery_code, delivery_code)
        self.assertEqual(finished_pdu.file_status, file_status)

    def _generic_verify_missing_segment_requested(
        self,
        start_of_scope: int,
        end_of_scope: int,
        segment_reqs: List[Tuple[int, int]],
    ):
        next_pdu = self.dest_handler.get_next_packet()
        assert next_pdu is not None
        self.assertEqual(next_pdu.pdu_type, PduType.FILE_DIRECTIVE)
        self.assertEqual(next_pdu.pdu_directive_type, DirectiveType.NAK_PDU)
        nak_pdu = next_pdu.to_nak_pdu()
        self.assertEqual(nak_pdu.start_of_scope, start_of_scope)
        self.assertEqual(nak_pdu.end_of_scope, end_of_scope)
        self.assertEqual(nak_pdu.segment_requests, segment_reqs)

    def _generic_verify_eof_ack_packet(
        self,
        fsm_res: FsmResult,
        condition_code_of_acked_pdu: ConditionCode = ConditionCode.NO_ERROR,
    ):
        self._state_checker(
            fsm_res,
            1,
            CfdpState.BUSY,
            TransactionStep.SENDING_EOF_ACK_PDU,
        )
        next_pdu = self.dest_handler.get_next_packet()
        assert next_pdu is not None
        self.assertEqual(next_pdu.pdu_type, PduType.FILE_DIRECTIVE)
        self.assertEqual(next_pdu.pdu_directive_type, DirectiveType.ACK_PDU)
        ack_pdu = next_pdu.to_ack_pdu()
        self.assertEqual(ack_pdu.directive_code_of_acked_pdu, DirectiveType.EOF_PDU)
        self.assertEqual(
            ack_pdu.condition_code_of_acked_pdu, condition_code_of_acked_pdu
        )
        self.assertEqual(ack_pdu.transaction_status, TransactionStatus.ACTIVE)

    def _generic_insert_finished_pdu_ack(self, finished_pdu: FinishedPdu):
        ack_pdu = AckPdu(
            finished_pdu.pdu_header.pdu_conf,
            DirectiveType.FINISHED_PDU,
            finished_pdu.condition_code,
            TransactionStatus.ACTIVE,
        )
        fsm_res = self.dest_handler.state_machine(ack_pdu)
        self._state_checker(fsm_res, 0, CfdpState.IDLE, TransactionStep.IDLE)
