import os
import random
import struct
import sys
import time
from typing import cast

from cfdppy import (
    RemoteEntityCfgTable,
)
from cfdppy.defs import CfdpState
from cfdppy.exceptions import NoRemoteEntityCfgFound
from cfdppy.handler.dest import (
    DestHandler,
    PduIgnoredForDest,
    TransactionStep,
)
from cfdppy.user import MetadataRecvParams, TransactionFinishedParams
from crcmod.predefined import mkPredefinedCrcFun
from spacepackets.cfdp import (
    NULL_CHECKSUM_U32,
    ChecksumType,
    ConditionCode,
    DeliveryCode,
    EntityIdTlv,
    FileStatus,
    TransmissionMode,
)
from spacepackets.cfdp.pdu import (
    EofPdu,
    FileDataPdu,
    FinishedParams,
    MetadataParams,
    MetadataPdu,
)
from spacepackets.cfdp.pdu.file_data import FileDataParams

from tests.common import CheckTimerProviderForTest
from tests.test_dest_handler import FileInfo, TestDestHandlerBase


class TestCfdpDestHandler(TestDestHandlerBase):
    def setUp(self) -> None:
        self.common_setup(TransmissionMode.UNACKNOWLEDGED)

    def _generic_empty_file_test(self):
        self._generic_regular_transfer_init(0)
        fsm_res = self._generic_insert_eof_pdu(0, NULL_CHECKSUM_U32)
        self._generic_eof_recv_indication_check(fsm_res)
        if self.closure_requested:
            self._generic_no_error_finished_pdu_check_and_done_check(fsm_res)
        self._generic_verify_transfer_completion(fsm_res, bytes())

    def test_empty_file_reception(self):
        self._generic_empty_file_test()

    def test_empty_file_reception_with_closure(self):
        self.closure_requested = True
        self._generic_empty_file_test()

    def _generic_small_file_test(self):
        data = "Hello World\n".encode()
        with open(self.src_file_path, "wb") as of:
            of.write(data)
        crc32_func = mkPredefinedCrcFun("crc32")
        crc32 = struct.pack("!I", crc32_func(data))
        file_size = self.src_file_path.stat().st_size
        self._generic_regular_transfer_init(
            file_size=file_size,
        )
        self._insert_file_segment(segment=data, offset=0)
        fsm_res = self._generic_insert_eof_pdu(file_size, crc32)
        self._generic_eof_recv_indication_check(fsm_res)
        if self.closure_requested:
            self._generic_no_error_finished_pdu_check_and_done_check(fsm_res)
        self._generic_verify_transfer_completion(fsm_res, data)

    def test_small_file_reception_no_closure(self):
        self._generic_small_file_test()

    def test_small_file_reception_with_closure(self):
        self.closure_requested = True
        self._generic_small_file_test()

    def _generic_larger_file_reception_test(self):
        # This tests generates two file data PDUs, but the second one does not have a
        # full segment length
        file_info = self._random_data_two_file_segments()
        self._state_checker(None, False, CfdpState.IDLE, TransactionStep.IDLE)
        self._generic_regular_transfer_init(
            file_size=file_info.file_size,
        )
        self._insert_file_segment(file_info.rand_data[0 : self.file_segment_len], 0)
        self._insert_file_segment(
            file_info.rand_data[self.file_segment_len :], offset=self.file_segment_len
        )
        fsm_res = self._generic_insert_eof_pdu(file_info.file_size, file_info.crc32)
        self._generic_eof_recv_indication_check(fsm_res)
        if self.closure_requested:
            self._generic_no_error_finished_pdu_check_and_done_check(fsm_res)
        self._generic_verify_transfer_completion(fsm_res, file_info.rand_data)

    def test_larger_file_reception(self):
        self._generic_larger_file_reception_test()

    def test_larger_file_reception_with_closure(self):
        self.closure_requested = True
        self._generic_larger_file_reception_test()

    def test_remote_cfg_does_not_exist(self):
        # Re-create empty table
        self.remote_cfg_table = RemoteEntityCfgTable()
        self.dest_handler = DestHandler(
            self.local_cfg,
            self.cfdp_user,
            self.remote_cfg_table,
            CheckTimerProviderForTest(5),
        )
        metadata_params = MetadataParams(
            checksum_type=ChecksumType.NULL_CHECKSUM,
            closure_requested=False,
            source_file_name=self.src_file_path.as_posix(),
            dest_file_name=self.dest_file_path.as_posix(),
            file_size=0,
        )
        file_transfer_init = MetadataPdu(
            params=metadata_params, pdu_conf=self.src_pdu_conf
        )
        self._state_checker(None, False, CfdpState.IDLE, TransactionStep.IDLE)
        with self.assertRaises(NoRemoteEntityCfgFound):
            self.dest_handler.state_machine(file_transfer_init)

    def test_check_timer_mechanism(self):
        file_data = "Hello World\n".encode()
        self._generic_check_limit_test(file_data)
        fd_params = FileDataParams(
            file_data=file_data,
            offset=0,
        )
        file_data_pdu = FileDataPdu(params=fd_params, pdu_conf=self.src_pdu_conf)
        fsm_res = self.dest_handler.state_machine(file_data_pdu)
        self._state_checker(
            fsm_res,
            False,
            CfdpState.BUSY,
            TransactionStep.RECV_FILE_DATA_WITH_CHECK_LIMIT_HANDLING,
        )
        self.assertFalse(self.dest_handler.packets_ready)
        time.sleep(self.timeout_check_limit_handling_ms * 1.15 / 1000.0)
        fsm_res = self.dest_handler.state_machine()
        self._state_checker(
            fsm_res,
            False,
            CfdpState.IDLE,
            TransactionStep.IDLE,
        )

    def test_cancelled_transfer_via_eof_pdu(self):
        data = "Hello World\n".encode()
        with open(self.src_file_path, "wb") as of:
            of.write(data)
        file_size = self.src_file_path.stat().st_size
        self._generic_regular_transfer_init(
            file_size=file_size,
        )
        self._insert_file_segment(segment=data, offset=0)
        # Cancel the transfer by sending an EOF PDU with the appropriate parameters.
        eof_pdu = EofPdu(
            file_size=0,
            file_checksum=NULL_CHECKSUM_U32,
            pdu_conf=self.src_pdu_conf,
            condition_code=ConditionCode.CANCEL_REQUEST_RECEIVED,
        )
        fsm_res = self.dest_handler.state_machine(eof_pdu)
        self._generic_eof_recv_indication_check(fsm_res)
        if self.closure_requested:
            self._generic_finished_pdu_check(
                fsm_res,
                expected_state=CfdpState.IDLE,
                expected_step=TransactionStep.IDLE,
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

    def test_cancelled_transfer_via_cancel_request(self):
        data = "Hello World\n".encode()
        with open(self.src_file_path, "wb") as of:
            of.write(data)
        file_size = self.src_file_path.stat().st_size
        self._generic_regular_transfer_init(
            file_size=file_size,
        )
        self._insert_file_segment(segment=data, offset=0)
        # Cancel the transfer with the cancel API
        self.dest_handler.cancel_request(self.transaction_id)
        fsm_res = self.dest_handler.state_machine()
        if self.closure_requested:
            self._generic_finished_pdu_check(
                fsm_res,
                expected_state=CfdpState.IDLE,
                expected_step=TransactionStep.IDLE,
                expected_condition_code=ConditionCode.CANCEL_REQUEST_RECEIVED,
                expected_fault_location=EntityIdTlv(self.entity_id.as_bytes),
            )
        self._generic_verify_transfer_completion(
            fsm_res,
            expected_file_data=None,
            expected_finished_params=FinishedParams(
                condition_code=ConditionCode.CANCEL_REQUEST_RECEIVED,
                delivery_code=DeliveryCode.DATA_INCOMPLETE,
                file_status=FileStatus.FILE_RETAINED,
                fault_location=EntityIdTlv(self.entity_id.as_bytes),
            ),
        )

    def test_check_limit_reached(self):
        data = "Hello World\n".encode()
        self._generic_check_limit_test(data)
        transaction_id = self.dest_handler.transaction_id
        assert transaction_id is not None
        # Check counter should be incremented by one.
        time.sleep(self.timeout_check_limit_handling_ms * 1.25 / 1000.0)
        fsm_res = self.dest_handler.state_machine()
        self._state_checker(
            fsm_res,
            0,
            CfdpState.BUSY,
            TransactionStep.RECV_FILE_DATA_WITH_CHECK_LIMIT_HANDLING,
        )
        self.assertEqual(self.dest_handler.current_check_counter, 1)
        # After this delay, the expiry limit (2) is reached and a check limit fault
        # is declared
        time.sleep(self.timeout_check_limit_handling_ms * 1.25 / 1000.0)
        fsm_res = self.dest_handler.state_machine()
        self.assertEqual(self.dest_handler.current_check_counter, 0)
        self._state_checker(
            fsm_res,
            0,
            CfdpState.IDLE,
            TransactionStep.IDLE,
        )
        self.fault_handler.notice_of_cancellation_cb.assert_called_once()
        self.fault_handler.notice_of_cancellation_cb.assert_called_with(
            transaction_id, ConditionCode.CHECK_LIMIT_REACHED, 0
        )
        self.cfdp_user.transaction_finished_indication.assert_called_once()
        self.cfdp_user.transaction_finished_indication.assert_called_with(
            TransactionFinishedParams(
                transaction_id,
                FinishedParams(
                    condition_code=ConditionCode.CHECK_LIMIT_REACHED,
                    delivery_code=DeliveryCode.DATA_INCOMPLETE,
                    file_status=FileStatus.FILE_RETAINED,
                ),
            )
        )

    def test_file_is_overwritten(self):
        with open(self.dest_file_path, "w") as of:
            of.write("This file will be truncated")
        self.test_small_file_reception_no_closure()

    def test_file_data_pdu_before_metadata_is_discarded(self):
        file_info = self._random_data_two_file_segments()
        with self.assertRaises(PduIgnoredForDest):
            # Pass file data PDU first. Will be discarded
            fsm_res = self._insert_file_segment(
                file_info.rand_data[0 : self.file_segment_len], 0
            )
            self._state_checker(fsm_res, False, CfdpState.IDLE, TransactionStep.IDLE)
        self._generic_regular_transfer_init(
            file_size=file_info.file_size,
        )
        fsm_res = self._insert_file_segment(
            segment=file_info.rand_data[: self.file_segment_len],
            offset=0,
        )
        fsm_res = self._insert_file_segment(
            segment=file_info.rand_data[self.file_segment_len :],
            offset=self.file_segment_len,
        )
        eof_pdu = EofPdu(
            file_size=file_info.file_size,
            file_checksum=file_info.crc32,
            pdu_conf=self.src_pdu_conf,
        )
        fsm_res = self.dest_handler.state_machine(eof_pdu)
        self.cfdp_user.transaction_finished_indication.assert_called_once()
        finished_args = cast(
            TransactionFinishedParams,
            self.cfdp_user.transaction_finished_indication.call_args.args[0],
        )
        # At least one segment was stored
        self.assertEqual(
            finished_args.finished_params.file_status,
            FileStatus.FILE_RETAINED,
        )
        self.assertEqual(
            finished_args.finished_params.condition_code,
            ConditionCode.NO_ERROR,
        )
        self._state_checker(fsm_res, False, CfdpState.IDLE, TransactionStep.IDLE)

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
                    condition_code=ConditionCode.NO_ERROR,
                    file_status=FileStatus.FILE_STATUS_UNREPORTED,
                    delivery_code=DeliveryCode.DATA_COMPLETE,
                ),
            )
        )
        self._state_checker(fsm_res, 0, CfdpState.IDLE, TransactionStep.IDLE)

    def test_permission_error(self):
        with open(self.src_file_path, "w") as of:
            of.write("Hello World\n")
        self.src_file_path.chmod(0o444)
        # TODO: This will cause permission errors, but the error handling for this has not been
        #       implemented properly
        """
        file_size = src_file.stat().st_size
        self._source_simulator_transfer_init_with_metadata(
            checksum=ChecksumTypes.CRC_32,
            file_size=file_size,
            file_path=src_file.as_posix(),
        )
        with open(src_file, "rb") as rf:
            read_data = rf.read()
        fd_params = FileDataParams(file_data=read_data, offset=0)
        file_data_pdu = FileDataPdu(params=fd_params, pdu_conf=self.src_pdu_conf)
        self.dest_handler.pass_packet(file_data_pdu)
        fsm_res = self.dest_handler.state_machine()
        self._state_checker(
            fsm_res, CfdpStates.BUSY_CLASS_1_NACKED, TransactionStep.RECEIVING_FILE_DATA
        )
        """
        self.src_file_path.chmod(0o777)

    def _random_data_two_file_segments(self):
        if sys.version_info >= (3, 9):
            rand_data = random.randbytes(round(self.file_segment_len * 1.3))
        else:
            rand_data = os.urandom(round(self.file_segment_len * 1.3))
        file_size = len(rand_data)
        crc32_func = mkPredefinedCrcFun("crc32")
        crc32 = struct.pack("!I", crc32_func(rand_data))
        return FileInfo(file_size=file_size, crc32=crc32, rand_data=rand_data)

    def _generic_check_limit_test(self, file_data: bytes):
        with open(self.src_file_path, "wb") as of:
            of.write(file_data)
        crc32_func = mkPredefinedCrcFun("crc32")
        crc32 = struct.pack("!I", crc32_func(file_data))
        file_size = self.src_file_path.stat().st_size
        self._generic_regular_transfer_init(
            file_size=file_size,
        )
        eof_pdu = EofPdu(
            file_size=file_size,
            file_checksum=crc32,
            pdu_conf=self.src_pdu_conf,
        )
        fsm_res = self.dest_handler.state_machine(eof_pdu)
        self._state_checker(
            fsm_res,
            False,
            CfdpState.BUSY,
            TransactionStep.RECV_FILE_DATA_WITH_CHECK_LIMIT_HANDLING,
        )
        self._generic_eof_recv_indication_check(fsm_res)
