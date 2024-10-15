import os
import random
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

from cfdppy import PutRequest
from cfdppy.defs import CfdpState
from cfdppy.exceptions import (
    InvalidDestinationId,
    InvalidPduDirection,
    InvalidSourceId,
)
from cfdppy.handler.source import TransactionStep
from spacepackets.cfdp import (
    ConditionCode,
    Direction,
    DirectiveType,
    PduConfig,
    TransmissionMode,
)
from spacepackets.cfdp.pdu import DeliveryCode, FileStatus, FinishedPdu
from spacepackets.cfdp.pdu.finished import FinishedParams
from spacepackets.util import (
    ByteFieldEmpty,
    ByteFieldU8,
    ByteFieldU16,
)

from .test_src_handler import TestCfdpSourceHandler


class TestCfdpSourceHandlerWithClosure(TestCfdpSourceHandler):
    def setUp(self) -> None:
        self.common_setup(True, TransmissionMode.UNACKNOWLEDGED)
        self.seq_num_provider.get_and_increment = MagicMock(return_value=2)

    def test_empty_file_pdu_generation_nacked_by_remote_cfg(self):
        transaction_id, metadata_pdu, _ = self._common_empty_file_test(None)
        self._pass_simple_finish_pdu_to_source_handler(metadata_pdu.pdu_header.pdu_conf)
        # Transaction should be finished
        fsm_res = self.source_handler.state_machine()
        self._verify_transaction_finished_indication(
            transaction_id,
            FinishedParams(
                condition_code=ConditionCode.NO_ERROR,
                file_status=FileStatus.FILE_RETAINED,
                delivery_code=DeliveryCode.DATA_COMPLETE,
            ),
        )
        self._state_checker(fsm_res, False, CfdpState.IDLE, TransactionStep.IDLE)

    def test_empty_file_pdu_generation_nacked_explicitely(self):
        self.default_remote_cfg.default_transmission_mode = (
            TransmissionMode.ACKNOWLEDGED
        )
        transaction_id, metadata_pdu, _ = self._common_empty_file_test(
            TransmissionMode.UNACKNOWLEDGED
        )
        self._pass_simple_finish_pdu_to_source_handler(metadata_pdu.pdu_header.pdu_conf)
        # Transaction should be finished
        fsm_res = self.source_handler.state_machine()
        self._verify_transaction_finished_indication(
            transaction_id,
            FinishedParams(
                condition_code=ConditionCode.NO_ERROR,
                file_status=FileStatus.FILE_RETAINED,
                delivery_code=DeliveryCode.DATA_COMPLETE,
            ),
        )
        self.expected_cfdp_state = CfdpState.IDLE
        self._state_checker(fsm_res, False, CfdpState.IDLE, TransactionStep.IDLE)

    def test_small_file_pdu_generation(self):
        file_content = "Hello World\n".encode()
        transaction_id, metadata_pdu, _, _ = self._common_small_file_test(
            TransmissionMode.UNACKNOWLEDGED, True, file_content
        )
        self._verify_eof_indication(transaction_id)
        self.source_handler.state_machine()
        self._pass_simple_finish_pdu_to_source_handler(metadata_pdu.pdu_header.pdu_conf)
        # Transaction should be finished
        fsm_res = self.source_handler.state_machine()
        self.expected_cfdp_state = CfdpState.IDLE
        self._state_checker(fsm_res, False, CfdpState.IDLE, TransactionStep.IDLE)

    def test_invalid_dir_pdu_passed(self):
        self.dest_id = ByteFieldU16(2)
        source_path = Path(f"{tempfile.gettempdir()}/dummy.txt")
        self._generate_file(source_path, bytes())
        metadata_pdu, _ = self._start_source_transaction(
            self._generate_generic_put_req(source_path, Path("dummy.txt"))
        )
        finish_pdu = self._prepare_finish_pdu(metadata_pdu.pdu_header.pdu_conf)
        finish_pdu.pdu_file_directive.pdu_header.direction = Direction.TOWARDS_RECEIVER
        with self.assertRaises(InvalidPduDirection):
            self.source_handler.state_machine(finish_pdu)

    def test_cancelled_transaction(self):
        # This tests generates two file data PDUs
        if sys.version_info >= (3, 9):
            rand_data = random.randbytes(self.file_segment_len * 2)
        else:
            rand_data = os.urandom(self.file_segment_len * 2)
        self.source_id = ByteFieldU8(1)
        self.dest_id = ByteFieldU8(2)
        self._update_seq_num_to_use(3)
        source_path = Path(f"{tempfile.gettempdir()}/two-segments.bin")
        dest_path = Path(f"{tempfile.gettempdir()}/two-segments-copy.bin")
        # The calculated CRC in the EOF (Cancel) PDU will only be calculated for the first segment
        tparams = self._transaction_with_file_data_wrapper(
            self._generate_generic_put_req(source_path, dest_path),
            rand_data[0 : self.file_segment_len],
        )
        self._generic_file_segment_handling(0, rand_data[0 : self.file_segment_len])
        self.assertTrue(self.source_handler.cancel_request(tparams.id))
        self._state_checker(None, 1, CfdpState.IDLE, TransactionStep.IDLE)
        next_packet = self.source_handler.get_next_packet()
        assert next_packet is not None
        self.assertTrue(next_packet.is_file_directive)
        self.assertEqual(next_packet.pdu_directive_type, DirectiveType.EOF_PDU)
        eof_pdu = next_packet.to_eof_pdu()
        self.assertEqual(tparams.crc_32, eof_pdu.file_checksum)
        self.assertEqual(eof_pdu.file_size, self.file_segment_len)
        self.assertEqual(eof_pdu.file_size, tparams.file_size)
        fsm_res = self.source_handler.state_machine()
        self.expected_cfdp_state = CfdpState.IDLE
        self._state_checker(fsm_res, False, CfdpState.IDLE, TransactionStep.IDLE)

    def test_invalid_source_id_pdu_passed(self):
        source_path = Path(f"{tempfile.gettempdir()}/test.txt")
        self._update_seq_num_to_use(2)
        self._generate_file(source_path, bytes())
        put_req = self._generate_dest_dummy_put_req(source_path)
        finish_pdu = self._regular_transaction_start(put_req)
        finish_pdu.pdu_file_directive.pdu_conf.source_entity_id = ByteFieldEmpty()
        with self.assertRaises(InvalidSourceId) as cm:
            self.source_handler.state_machine(finish_pdu)
        exception = cm.exception
        self.assertEqual(exception.found_src_id, ByteFieldEmpty())
        self.assertEqual(exception.expected_src_id, ByteFieldU16(1))

    def test_invalid_dest_id_pdu_passed(self):
        source_path = Path(f"{tempfile.gettempdir()}/test.txt")
        self._update_seq_num_to_use(2)
        self.dest_id = ByteFieldU16(3)
        self._generate_file(source_path, bytes())
        put_req = self._generate_dest_dummy_put_req(source_path)
        finish_pdu = self._regular_transaction_start(put_req)
        finish_pdu.pdu_file_directive.pdu_conf.dest_entity_id = ByteFieldEmpty()
        with self.assertRaises(InvalidDestinationId) as cm:
            self.source_handler.state_machine(finish_pdu)
        exception = cm.exception
        self.assertEqual(exception.found_dest_id, ByteFieldEmpty())
        self.assertEqual(exception.expected_dest_id, ByteFieldU16(3))

    def _update_seq_num_to_use(self, seq_num: int):
        self.expected_seq_num = seq_num
        self.seq_num_provider.get_and_increment = MagicMock(
            return_value=self.expected_seq_num
        )

    def _regular_transaction_start(self, put_req: PutRequest) -> FinishedPdu:
        metadata_pdu, _ = self._start_source_transaction(put_req)
        finish_pdu = self._prepare_finish_pdu(metadata_pdu.pdu_header.pdu_conf)
        self.assertEqual(finish_pdu.transaction_seq_num.value, self.expected_seq_num)
        return finish_pdu

    def _pass_simple_finish_pdu_to_source_handler(self, base_conf: PduConfig):
        self._state_checker(
            None,
            False,
            CfdpState.BUSY,
            TransactionStep.WAITING_FOR_FINISHED,
        )
        self.source_handler.state_machine(self._prepare_finish_pdu(base_conf))

    def _prepare_finish_pdu(self, base_conf: PduConfig):
        params = FinishedParams(
            delivery_code=DeliveryCode.DATA_COMPLETE,
            condition_code=ConditionCode.NO_ERROR,
            file_status=FileStatus.FILE_RETAINED,
        )
        return FinishedPdu(
            params=params,
            pdu_conf=base_conf,
        )
