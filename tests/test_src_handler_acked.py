import os
import random
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock

from cfdppy.defs import CfdpState
from cfdppy.handler.source import TransactionStep
from spacepackets.cfdp import (
    NULL_CHECKSUM_U32,
    ConditionCode,
    Direction,
    DirectiveType,
    FinishedParams,
    PduConfig,
    PduType,
    TransactionId,
    TransmissionMode,
)
from spacepackets.cfdp.pdu import (
    AckPdu,
    DeliveryCode,
    EofPdu,
    FileStatus,
    FinishedPdu,
    NakPdu,
    TransactionStatus,
)

from .test_src_handler import TestCfdpSourceHandler


class TestSourceHandlerAcked(TestCfdpSourceHandler):
    def setUp(self) -> None:
        self.common_setup(True, TransmissionMode.ACKNOWLEDGED)

    def test_empty_file_transfer(self):
        transaction_id, _, eof_pdu = self._common_empty_file_test(None)
        self._generic_acked_transfer_completion_full_success(transaction_id, eof_pdu)

    def test_small_file_transfer(self):
        transaction_id, _, _, eof_pdu = self._common_small_file_test(
            TransmissionMode.ACKNOWLEDGED,
            True,
            "Hello World!".encode(),
        )
        self._generic_acked_transfer_completion_full_success(transaction_id, eof_pdu)

    def test_missing_metadata_pdu_retransmission(self):
        transaction_id, first_metadata_pdu, eof_pdu = self._common_empty_file_test(None)
        # Generate appropriate NAK PDU and insert it.
        nak_missing_metadata = NakPdu(eof_pdu.pdu_header.pdu_conf, 0, 0, [(0, 0)])
        self.source_handler.state_machine(nak_missing_metadata)
        self._state_checker(
            None,
            True,
            CfdpState.BUSY,
            TransactionStep.RETRANSMITTING,
        )
        next_pdu = self.source_handler.get_next_packet()
        assert next_pdu is not None
        self.assertEqual(next_pdu.pdu_type, PduType.FILE_DIRECTIVE)
        self.assertEqual(next_pdu.pdu_directive_type, DirectiveType.METADATA_PDU)
        metadata_pdu = next_pdu.to_metadata_pdu()
        self.assertEqual(metadata_pdu, first_metadata_pdu)
        self.source_handler.state_machine_no_packet()
        self._generic_acked_transfer_completion_full_success(transaction_id, eof_pdu)

    def test_missing_filedata_pdu_retransmission(self):
        file_content = "Hello World!".encode()
        transaction_id, _, first_fd_pdu, eof_pdu = self._common_small_file_test(
            TransmissionMode.ACKNOWLEDGED, True, file_content
        )
        end_of_scope = len(file_content)
        # Generate appropriate NAK PDU and insert it.
        nak_missing_metadata = NakPdu(
            eof_pdu.pdu_header.pdu_conf, 0, end_of_scope, [(0, end_of_scope)]
        )
        self.source_handler.state_machine(nak_missing_metadata)
        self._state_checker(
            None,
            1,
            CfdpState.BUSY,
            TransactionStep.RETRANSMITTING,
        )
        next_pdu = self.source_handler.get_next_packet()
        assert next_pdu is not None
        self.assertEqual(next_pdu.pdu_type, PduType.FILE_DATA)
        fd_pdu = next_pdu.to_file_data_pdu()
        self.assertEqual(fd_pdu, first_fd_pdu)
        self.source_handler.state_machine_no_packet()
        self._generic_acked_transfer_completion_full_success(transaction_id, eof_pdu)

    def test_positive_ack_procedure(self):
        # 1. Send EOF PDU.
        # 2. Verify EOF PDU is sent again after ACK limit is reached once.
        _, _, initial_eof_pdu = self._common_empty_file_test(None)
        self.assertEqual(self.source_handler.positive_ack_counter, 0)
        time.sleep(self.positive_ack_intvl_seconds * 1.2)
        self._verify_eof_pdu_for_positive_ack(initial_eof_pdu, 1)

    def test_ack_limit_reached(self):
        # This tests fully checks the case where the ACK limit is reached and a corresponding
        # fault is declared.
        transaction_id, _, initial_eof_pdu = self._common_empty_file_test(None)
        self.assertEqual(self.source_handler.positive_ack_counter, 0)
        # 100 ms timeout, sleep of 150 ms should trigger EOF re-send.
        time.sleep(self.positive_ack_intvl_seconds * 1.2)
        self._verify_eof_pdu_for_positive_ack(initial_eof_pdu, 1)
        time.sleep(self.positive_ack_intvl_seconds * 1.2)
        self.source_handler.state_machine_no_packet()
        # That's odd but expected. The ACK limit reached fault will trigger a notice
        # of cancellation by default, which causes it to send an EOF PDU with the appropriate
        # condition code. There is little chance that this PDU will arrive after all the
        # others did not arrive in a real use case, but this is standard conformant behaviour.
        # If the the positive ACK procedure fails for this EOF PDU, the standard behaviour
        # is abandonment of the transaction, irrespective of any fault handler configuration,
        # according to section 4.11.2.2.3 of the standard.
        self._state_checker(
            None,
            1,
            CfdpState.BUSY,
            TransactionStep.WAITING_FOR_EOF_ACK,
        )
        cancelation_cb_mock: MagicMock = self.fault_handler.notice_of_cancellation_cb  # type: ignore
        cancelation_cb_mock.assert_called_once()
        cancelation_cb_mock.assert_called_with(
            transaction_id, ConditionCode.POSITIVE_ACK_LIMIT_REACHED, 0
        )

        next_pdu = self.source_handler.get_next_packet()
        assert next_pdu is not None
        self.assertEqual(next_pdu.pdu_type, PduType.FILE_DIRECTIVE)
        self.assertEqual(next_pdu.pdu_directive_type, DirectiveType.EOF_PDU)
        eof_pdu_for_cancellation = next_pdu.to_eof_pdu()
        self.assertEqual(
            eof_pdu_for_cancellation.condition_code,
            ConditionCode.POSITIVE_ACK_LIMIT_REACHED,
        )
        self.assertEqual(eof_pdu_for_cancellation.file_checksum, NULL_CHECKSUM_U32)
        self.assertEqual(eof_pdu_for_cancellation.file_size, 0)
        # Calling the state machine again confirms we sent or handled the EOF packet,
        # and only then will the positive ACK counter be reset.
        self.source_handler.state_machine()
        self._state_checker(
            None, 0, CfdpState.BUSY, TransactionStep.WAITING_FOR_EOF_ACK
        )
        self.assertEqual(self.source_handler.positive_ack_counter, 0)
        time.sleep(self.positive_ack_intvl_seconds * 1.2)
        self._verify_eof_pdu_for_positive_ack(eof_pdu_for_cancellation, 1)
        time.sleep(self.positive_ack_intvl_seconds * 1.2)
        self.source_handler.state_machine()
        self.expected_cfdp_state = CfdpState.IDLE
        # Transaction was abandoned.
        self._state_checker(
            None,
            0,
            CfdpState.IDLE,
            TransactionStep.IDLE,
        )
        abandoned_cb_mock: MagicMock = self.fault_handler.abandoned_cb  # type: ignore
        abandoned_cb_mock.assert_called_once()
        abandoned_cb_mock.assert_called_with(
            transaction_id, ConditionCode.POSITIVE_ACK_LIMIT_REACHED, 0
        )

    def test_ack_procedure_success(self):
        # 1. Send EOF PDU.
        # 2. Verify EOF PDU is sent again after ACK limit is reached once.
        self.assertEqual(self.source_handler.positive_ack_counter, 0)
        transaction_id, _, initial_eof_pdu = self._common_empty_file_test(None)
        time.sleep(self.positive_ack_intvl_seconds * 1.2)
        self._verify_eof_pdu_for_positive_ack(initial_eof_pdu, 1)
        self._generic_acked_transfer_completion_full_success(
            transaction_id, initial_eof_pdu
        )

    def test_large_missing_chunk_retransmission(self):
        # This tests generates three file data PDUs
        if sys.version_info >= (3, 9):
            rand_data = random.randbytes(self.file_segment_len * 3)
        else:
            rand_data = os.urandom(self.file_segment_len * 3)
        crc32 = self._gen_crc32(rand_data)
        source_path = Path(f"{tempfile.gettempdir()}/rand-three-segs.bin")
        self._generate_file(source_path, rand_data)
        dest_path = Path(f"{tempfile.gettempdir()}/rand-three-segs-copy.bin")
        transaction_params = self._transaction_with_file_data_wrapper(
            self._generate_generic_put_req(source_path, dest_path), rand_data
        )
        current_idx = 0
        fd_pdu_list = []
        while current_idx < len(rand_data):
            fd_pdu_list.append(
                self._generic_file_segment_handling(
                    current_idx,
                    rand_data[current_idx : current_idx + self.file_segment_len],
                )
            )
            current_idx += self.file_segment_len
        eof_pdu = self._handle_eof_pdu(transaction_params.id, crc32, len(rand_data))
        all_missing_filedata = NakPdu(
            pdu_conf=eof_pdu.pdu_header.pdu_conf,
            start_of_scope=0,
            end_of_scope=len(rand_data),
            segment_requests=[(0, len(rand_data))],
        )
        fsm_res = self.source_handler.state_machine(all_missing_filedata)
        self._state_checker(fsm_res, 3, CfdpState.BUSY, TransactionStep.RETRANSMITTING)
        # All file data PDUs should be re-sent now.
        for i in range(3):
            next_pdu = self.source_handler.get_next_packet()
            assert next_pdu is not None
            self.assertEqual(fd_pdu_list[i], next_pdu.pdu)
        next_pdu = self.source_handler.get_next_packet()
        assert next_pdu is None
        self.source_handler.state_machine()
        self._generic_acked_transfer_completion_full_success(
            transaction_params.id, eof_pdu
        )

    def _generic_acked_transfer_completion_full_success(
        self, transaction_id: TransactionId, eof_pdu: EofPdu
    ):
        self._generic_acked_transfer_completion(
            transaction_id,
            eof_pdu,
            FinishedParams(
                condition_code=ConditionCode.NO_ERROR,
                file_status=FileStatus.FILE_RETAINED,
                delivery_code=DeliveryCode.DATA_COMPLETE,
            ),
        )

    def _generic_acked_transfer_completion(
        self,
        transaction_id: TransactionId,
        eof_pdu: EofPdu,
        expected_finished_params: FinishedParams,
    ):
        self._state_checker(
            None,
            0,
            CfdpState.BUSY,
            TransactionStep.WAITING_FOR_EOF_ACK,
        )
        pdu_conf = eof_pdu.pdu_header.pdu_conf
        self._insert_eof_ack_packet(eof_pdu)
        self._insert_finished_pdu(pdu_conf)
        self._acknowledge_finished_pdu(pdu_conf)
        self.source_handler.state_machine()
        self._verify_transaction_finished_indication(
            transaction_id, expected_finished_params
        )
        self.expected_cfdp_state = CfdpState.IDLE
        self._state_checker(
            None,
            False,
            CfdpState.IDLE,
            TransactionStep.IDLE,
        )

    def _insert_finished_pdu(self, pdu_conf: PduConfig):
        finished_pdu = FinishedPdu(
            pdu_conf,
            FinishedParams(
                delivery_code=DeliveryCode.DATA_COMPLETE,
                file_status=FileStatus.FILE_RETAINED,
                condition_code=ConditionCode.NO_ERROR,
            ),
        )
        self.source_handler.state_machine(finished_pdu)
        self._state_checker(
            None,
            True,
            CfdpState.BUSY,
            TransactionStep.SENDING_ACK_OF_FINISHED,
        )

    def _verify_eof_pdu_for_positive_ack(
        self, expected_eof: EofPdu, expected_ack_counter: int
    ):
        self.source_handler.state_machine()
        self.assertEqual(self.source_handler.packets_ready, True)
        self.assertEqual(self.source_handler.num_packets_ready, 1)
        self.assertEqual(self.source_handler.positive_ack_counter, expected_ack_counter)
        next_pdu = self.source_handler.get_next_packet()
        assert next_pdu is not None
        self.assertEqual(next_pdu.pdu_type, PduType.FILE_DIRECTIVE)
        self.assertEqual(next_pdu.pdu_directive_type, DirectiveType.EOF_PDU)
        eof_pdu = next_pdu.to_eof_pdu()
        self.assertEqual(eof_pdu, expected_eof)

    def _acknowledge_finished_pdu(self, pdu_conf: PduConfig):
        next_pdu = self.source_handler.get_next_packet()
        assert next_pdu is not None
        self.assertEqual(next_pdu.pdu_type, PduType.FILE_DIRECTIVE)
        self.assertEqual(next_pdu.pdu_directive_type, DirectiveType.ACK_PDU)
        ack_pdu = next_pdu.to_ack_pdu()
        self.assertEqual(ack_pdu.direction, Direction.TOWARDS_RECEIVER)
        self.assertEqual(
            ack_pdu.directive_code_of_acked_pdu, DirectiveType.FINISHED_PDU
        )
        self.assertEqual(ack_pdu.transaction_status, TransactionStatus.ACTIVE)
        # Set this correctly for test explicitely. Every other field should be the same.
        pdu_conf.direction = Direction.TOWARDS_RECEIVER
        self.assertEqual(ack_pdu.pdu_header.pdu_conf, pdu_conf)

    def _insert_eof_ack_packet(self, eof_pdu: EofPdu):
        pdu_conf = eof_pdu.pdu_header.pdu_conf
        ack_pdu = AckPdu(
            pdu_conf,
            DirectiveType.EOF_PDU,
            ConditionCode.NO_ERROR,
            TransactionStatus.ACTIVE,
        )
        self.assertEqual(ack_pdu.direction, Direction.TOWARDS_SENDER)
        self.source_handler.state_machine(ack_pdu)
        self._state_checker(
            None,
            False,
            CfdpState.BUSY,
            TransactionStep.WAITING_FOR_FINISHED,
        )
