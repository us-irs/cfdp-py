import dataclasses
import os
import tempfile
from pathlib import Path
from typing import List, Optional, cast
from unittest.mock import MagicMock

from cfdppy import (
    IndicationCfg,
    LocalEntityCfg,
    RemoteEntityCfg,
    RemoteEntityCfgTable,
)
from cfdppy.defs import CfdpState
from cfdppy.handler.dest import (
    DestHandler,
    FsmResult,
    TransactionStep,
)
from cfdppy.user import (
    FileSegmentRecvdParams,
    MetadataRecvParams,
    TransactionFinishedParams,
)
from pyfakefs.fake_filesystem_unittest import TestCase
from spacepackets.cfdp import (
    NULL_CHECKSUM_U32,
    ChecksumType,
    ConditionCode,
    Direction,
    DirectiveType,
    EntityIdTlv,
    FinishedParams,
    PduConfig,
    PduType,
    TransactionId,
    TransmissionMode,
)
from spacepackets.cfdp.pdu import (
    DeliveryCode,
    EofPdu,
    FileDataPdu,
    FileStatus,
    FinishedPdu,
    MetadataPdu,
)
from spacepackets.cfdp.pdu.file_data import FileDataParams
from spacepackets.cfdp.pdu.metadata import MetadataParams
from spacepackets.cfdp.tlv import (
    MessageToUserTlv,
    OriginatingTransactionId,
    ProxyPutResponse,
    ProxyPutResponseParams,
    TlvList,
)
from spacepackets.util import ByteFieldU8, ByteFieldU16

from .cfdp_fault_handler_mock import FaultHandler
from .cfdp_user_mock import CfdpUser
from .common import CheckTimerProviderForTest


@dataclasses.dataclass
class FileInfo:
    rand_data: bytes
    file_size: int
    crc32: bytes


class TestDestHandlerBase(TestCase):
    def common_setup(self, trans_mode: TransmissionMode):
        self.setUpPyfakefs()
        self.indication_cfg = IndicationCfg(True, True, True, True, True, True)
        self.fault_handler = FaultHandler()
        self.fault_handler.notice_of_cancellation_cb = MagicMock()
        self.entity_id = ByteFieldU16(2)
        self.local_cfg = LocalEntityCfg(
            self.entity_id, self.indication_cfg, self.fault_handler
        )
        self.src_entity_id = ByteFieldU16(1)
        self.src_pdu_conf = PduConfig(
            source_entity_id=self.src_entity_id,
            dest_entity_id=self.entity_id,
            transaction_seq_num=ByteFieldU8(1),
            trans_mode=trans_mode,
        )
        self.transaction_id = TransactionId(self.src_entity_id, ByteFieldU8(1))
        self.expected_mode = trans_mode
        self.closure_requested = False
        self.cfdp_user = CfdpUser()
        self.cfdp_user.transaction_indication = MagicMock()
        self.cfdp_user.eof_recv_indication = MagicMock()
        self.cfdp_user.file_segment_recv_indication = MagicMock()
        self.cfdp_user.metadata_recv_indication = MagicMock()
        self.cfdp_user.transaction_finished_indication = MagicMock()
        self.file_segment_len = 128
        self.remote_cfg_table = RemoteEntityCfgTable()
        self.timeout_nak_procedure_seconds = 0.05
        self.timeout_positive_ack_procedure_seconds = 0.05
        self.remote_cfg = RemoteEntityCfg(
            entity_id=self.src_entity_id,
            check_limit=2,
            crc_type=ChecksumType.CRC_32,
            closure_requested=False,
            crc_on_transmission=False,
            default_transmission_mode=TransmissionMode.UNACKNOWLEDGED,
            max_file_segment_len=self.file_segment_len,
            max_packet_len=self.file_segment_len,
            nak_timer_expiration_limit=2,
            nak_timer_interval_seconds=self.timeout_nak_procedure_seconds,
            positive_ack_timer_interval_seconds=self.timeout_positive_ack_procedure_seconds,
            positive_ack_timer_expiration_limit=2,
        )
        self.remote_cfg_table.add_config(self.remote_cfg)
        self.timeout_check_limit_handling_ms = 50
        self.dest_handler = DestHandler(
            self.local_cfg,
            self.cfdp_user,
            self.remote_cfg_table,
            CheckTimerProviderForTest(
                timeout_dest_entity_ms=self.timeout_check_limit_handling_ms
            ),
        )
        self.src_file_path, self.dest_file_path = Path(
            f"{tempfile.gettempdir()}/source"
        ), Path(f"{tempfile.gettempdir()}/dest")

    def _state_checker(
        self,
        fsm_res: Optional[FsmResult],
        num_packets_ready: int,
        expected_state: CfdpState,
        expected_transaction: TransactionStep,
    ):
        if fsm_res is not None:
            self.assertEqual(fsm_res.states.state, expected_state)
            self.assertEqual(fsm_res.states.step, expected_transaction)
            self.assertEqual(fsm_res.states.num_packets_ready, num_packets_ready)
            if num_packets_ready > 0:
                self.assertTrue(fsm_res.states.packets_ready)
        if expected_state != CfdpState.IDLE:
            self.assertEqual(self.dest_handler.transmission_mode, self.expected_mode)
        self.assertEqual(self.dest_handler.states.state, expected_state)
        self.assertEqual(self.dest_handler.states.step, expected_transaction)
        self.assertEqual(self.dest_handler.state, expected_state)
        self.assertEqual(self.dest_handler.step, expected_transaction)
        self.assertEqual(self.dest_handler.num_packets_ready, num_packets_ready)

    def _generic_regular_transfer_init(
        self,
        file_size: int,
        expected_msgs_to_user: Optional[List[MessageToUserTlv]] = None,
    ):
        fsm_res = self._generic_transfer_init(
            file_size, 0, CfdpState.IDLE, TransactionStep.IDLE
        )
        self._state_checker(
            fsm_res, 0, CfdpState.BUSY, TransactionStep.RECEIVING_FILE_DATA
        )
        self.cfdp_user.metadata_recv_indication.assert_called_once()
        self.cfdp_user.metadata_recv_indication.assert_called_with(
            MetadataRecvParams(
                self.transaction_id,
                self.src_pdu_conf.source_entity_id,
                file_size,
                self.src_file_path.as_posix(),
                self.dest_file_path.as_posix(),
                expected_msgs_to_user,
            ),
        )

    def _generic_transfer_init(
        self,
        file_size: int,
        expected_init_packets: int,
        expected_init_state: CfdpState,
        expected_init_step: TransactionStep,
        expected_originating_id: Optional[TransactionId] = None,
    ) -> FsmResult:
        checksum_type = ChecksumType.NULL_CHECKSUM
        if file_size > 0:
            checksum_type = ChecksumType.CRC_32
        metadata_params = MetadataParams(
            checksum_type=checksum_type,
            closure_requested=self.closure_requested,
            source_file_name=self.src_file_path.as_posix(),
            dest_file_name=self.dest_file_path.as_posix(),
            file_size=file_size,
        )
        file_transfer_init = MetadataPdu(
            params=metadata_params, pdu_conf=self.src_pdu_conf
        )
        self._state_checker(
            None, expected_init_packets, expected_init_state, expected_init_step
        )
        fsm_res = self.dest_handler.state_machine(file_transfer_init)
        return fsm_res

    def _insert_file_segment(
        self,
        segment: bytes,
        offset: int,
        expected_packets: int = 0,
        expected_step: TransactionStep = TransactionStep.RECEIVING_FILE_DATA,
        check_indication: bool = True,
    ) -> FsmResult:
        fd_params = FileDataParams(file_data=segment, offset=offset)
        file_data_pdu = FileDataPdu(params=fd_params, pdu_conf=self.src_pdu_conf)
        fsm_res = self.dest_handler.state_machine(file_data_pdu)
        if (
            self.indication_cfg.file_segment_recvd_indication_required
            and check_indication
        ):
            self.cfdp_user.file_segment_recv_indication.assert_called_once()
            self.assertEqual(self.cfdp_user.file_segment_recv_indication.call_count, 1)
            seg_recv_params = cast(
                FileSegmentRecvdParams,
                self.cfdp_user.file_segment_recv_indication.call_args.args[0],
            )
            self.assertEqual(seg_recv_params.transaction_id, self.transaction_id)
            self.cfdp_user.file_segment_recv_indication.reset_mock()
        self._state_checker(
            fsm_res,
            expected_packets,
            CfdpState.BUSY,
            expected_step,
        )
        return fsm_res

    def _generic_insert_eof_pdu(self, file_size: int, checksum: bytes) -> FsmResult:
        eof_pdu = EofPdu(
            file_size=file_size, file_checksum=checksum, pdu_conf=self.src_pdu_conf
        )
        fsm_res = self.dest_handler.state_machine(eof_pdu)
        if self.expected_mode == TransmissionMode.UNACKNOWLEDGED:
            if self.closure_requested:
                self._state_checker(
                    fsm_res,
                    1,
                    CfdpState.IDLE,
                    TransactionStep.IDLE,
                )
            else:
                self._state_checker(fsm_res, 0, CfdpState.IDLE, TransactionStep.IDLE)
        return fsm_res

    def _generic_eof_recv_indication_check(self, fsm_res: FsmResult):
        self.cfdp_user.eof_recv_indication.assert_called_once()
        self.assertEqual(
            self.cfdp_user.eof_recv_indication.call_args.args[0], self.transaction_id
        )
        self.assertEqual(fsm_res.states.transaction_id, self.transaction_id)

    def _generic_no_error_finished_pdu_check_and_done_check(
        self,
        fsm_res: FsmResult,
        expected_file_status: FileStatus = FileStatus.FILE_RETAINED,
        expected_condition_code: ConditionCode = ConditionCode.NO_ERROR,
    ) -> FinishedPdu:
        return self._generic_finished_pdu_check(
            fsm_res,
            CfdpState.IDLE,
            TransactionStep.IDLE,
            expected_file_status,
            expected_condition_code,
        )

    def _generic_finished_pdu_check_acked(
        self,
        fsm_res: FsmResult,
        expected_condition_code: ConditionCode,
        expected_file_status: FileStatus = FileStatus.FILE_RETAINED,
        expected_fault_location: Optional[EntityIdTlv] = None,
    ) -> FinishedPdu:
        return self._generic_finished_pdu_check(
            fsm_res,
            CfdpState.BUSY,
            TransactionStep.WAITING_FOR_FINISHED_ACK,
            expected_file_status,
            expected_condition_code,
            expected_fault_location=expected_fault_location,
        )

    def _generic_no_error_finished_pdu_check_acked(
        self,
        fsm_res: FsmResult,
        expected_file_status: FileStatus = FileStatus.FILE_RETAINED,
    ) -> FinishedPdu:
        return self._generic_finished_pdu_check(
            fsm_res,
            CfdpState.BUSY,
            TransactionStep.WAITING_FOR_FINISHED_ACK,
            expected_file_status,
        )

    def _generic_no_error_finished_pdu_check(
        self,
        fsm_res: FsmResult,
        expected_state: CfdpState,
        expected_step: TransactionStep,
        expected_file_status: FileStatus = FileStatus.FILE_RETAINED,
    ) -> FinishedPdu:
        return self._generic_finished_pdu_check(
            fsm_res,
            expected_state,
            expected_step,
            expected_file_status,
        )

    def _generic_finished_pdu_check(
        self,
        fsm_res: FsmResult,
        expected_state: CfdpState,
        expected_step: TransactionStep,
        expected_file_status: FileStatus = FileStatus.FILE_RETAINED,
        expected_condition_code: ConditionCode = ConditionCode.NO_ERROR,
        expected_fault_location: Optional[EntityIdTlv] = None,
    ) -> FinishedPdu:
        self._state_checker(fsm_res, 1, expected_state, expected_step)
        self.assertTrue(fsm_res.states.packets_ready)
        next_pdu = self.dest_handler.get_next_packet()
        self.assertIsNotNone(next_pdu)
        self.assertEqual(next_pdu.pdu_type, PduType.FILE_DIRECTIVE)
        self.assertEqual(next_pdu.pdu_directive_type, DirectiveType.FINISHED_PDU)

        finished_pdu = next_pdu.to_finished_pdu()
        self.assertEqual(finished_pdu.condition_code, expected_condition_code)
        self.assertEqual(finished_pdu.file_status, expected_file_status)
        if expected_condition_code == ConditionCode.NO_ERROR:
            self.assertEqual(finished_pdu.delivery_code, DeliveryCode.DATA_COMPLETE)
        else:
            self.assertEqual(finished_pdu.delivery_code, DeliveryCode.DATA_INCOMPLETE)
        self.assertEqual(finished_pdu.direction, Direction.TOWARDS_SENDER)
        self.assertEqual(finished_pdu.fault_location, expected_fault_location)
        self.assertEqual(len(finished_pdu.file_store_responses), 0)
        return finished_pdu

    def _generic_verify_transfer_completion(
        self,
        fsm_res: FsmResult,
        expected_file_data: Optional[bytes],
        expected_finished_params: FinishedParams = FinishedParams(
            condition_code=ConditionCode.NO_ERROR,
            file_status=FileStatus.FILE_RETAINED,
            delivery_code=DeliveryCode.DATA_COMPLETE,
        ),
    ):
        self._generic_transfer_finished_indication_check(
            fsm_res, expected_finished_params
        )
        if expected_file_data is not None:
            self.assertTrue(self.dest_file_path.exists())
            self.assertEqual(
                self.dest_file_path.stat().st_size, len(expected_file_data)
            )
            with open(self.dest_file_path, "rb") as file:
                self.assertEqual(expected_file_data, file.read())
        fsm_res = self.dest_handler.state_machine()
        if self.expected_mode == TransmissionMode.UNACKNOWLEDGED:
            self._state_checker(fsm_res, 0, CfdpState.IDLE, TransactionStep.IDLE)
        else:
            self._state_checker(
                fsm_res, 0, CfdpState.BUSY, TransactionStep.WAITING_FOR_FINISHED_ACK
            )

    def _generic_transfer_finished_indication_check(
        self, fsm_res: FsmResult, expected_finished_params: FinishedParams
    ):
        self.cfdp_user.transaction_finished_indication.assert_called_once()
        finished_params_from_callback = cast(
            TransactionFinishedParams,
            self.cfdp_user.transaction_finished_indication.call_args.args[0],
        )
        self.assertEqual(
            finished_params_from_callback.transaction_id, self.transaction_id
        )
        self.assertEqual(fsm_res.states.transaction_id, self.transaction_id)
        self.assertEqual(
            finished_params_from_callback.finished_params, expected_finished_params
        )

    def _generate_put_response_opts(self) -> TlvList:
        return [
            OriginatingTransactionId(
                TransactionId(ByteFieldU16(1), ByteFieldU16(2))
            ).to_generic_msg_to_user_tlv(),
            ProxyPutResponse(
                ProxyPutResponseParams(
                    ConditionCode.NO_ERROR,
                    DeliveryCode.DATA_COMPLETE,
                    FileStatus.FILE_RETAINED,
                )
            ).to_generic_msg_to_user_tlv(),
        ]

    def _generate_metadata_only_metadata(
        self, options: Optional[TlvList]
    ) -> MetadataPdu:
        metadata_params = MetadataParams(
            checksum_type=NULL_CHECKSUM_U32,
            closure_requested=self.closure_requested,
            source_file_name=None,
            dest_file_name=None,
            file_size=0,
        )
        return MetadataPdu(
            params=metadata_params, pdu_conf=self.src_pdu_conf, options=options
        )

    def tearDown(self) -> None:
        if self.dest_file_path.exists():
            os.remove(self.dest_file_path)
        if self.src_file_path.exists():
            os.remove(self.src_file_path)
