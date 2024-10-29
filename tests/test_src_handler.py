import copy
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple, cast
from unittest.mock import MagicMock

from cfdppy import (
    CfdpState,
    IndicationCfg,
    LocalEntityCfg,
    RemoteEntityCfg,
    RemoteEntityCfgTable,
)
from cfdppy.exceptions import UnretrievedPdusToBeSent
from cfdppy.handler import FsmResult, SourceHandler
from cfdppy.handler.source import TransactionStep
from cfdppy.request import PutRequest
from cfdppy.user import TransactionFinishedParams, TransactionParams
from crcmod.predefined import PredefinedCrc
from pyfakefs.fake_filesystem_unittest import TestCase
from spacepackets.cfdp import (
    ChecksumType,
    ConditionCode,
    FinishedParams,
    PduHolder,
    PduType,
    TransactionId,
    TransmissionMode,
)
from spacepackets.cfdp.pdu import DirectiveType, EofPdu, FileDataPdu, MetadataPdu
from spacepackets.seqcount import SeqCountProvider
from spacepackets.util import ByteFieldU16, ByteFieldU32

from .cfdp_fault_handler_mock import FaultHandler
from .cfdp_user_mock import CfdpUser
from .common import CheckTimerProviderForTest


@dataclass
class TransactionStartParams:
    id: TransactionId
    metadata_pdu: MetadataPdu
    file_size: Optional[int]
    crc_32: Optional[bytes]


class TestCfdpSourceHandler(TestCase):
    """It should be noted that this only verifies the correct generation of PDUs. There is
    no reception handler in play here which would be responsible for generating the files
    from these PDUs
    """

    def common_setup(
        self, closure_requested: bool, default_transmission_mode: TransmissionMode
    ):
        self.setUpPyfakefs()
        self.closure_requested = closure_requested
        self.indication_cfg = IndicationCfg(True, True, True, True, True, True)
        self.fault_handler = FaultHandler()
        self.fault_handler.notice_of_cancellation_cb = MagicMock()
        self.fault_handler.notice_of_suspension_cb = MagicMock()
        self.fault_handler.abandoned_cb = MagicMock()
        self.fault_handler.ignore_cb = MagicMock()
        print(self.fault_handler.notice_of_cancellation_cb)
        self.local_cfg = LocalEntityCfg(
            ByteFieldU16(1), self.indication_cfg, self.fault_handler
        )
        self.cfdp_user = CfdpUser()
        self.cfdp_user.eof_sent_indication = MagicMock()
        self.cfdp_user.transaction_indication = MagicMock()
        self.cfdp_user.transaction_finished_indication = MagicMock()
        self.seq_num_provider = SeqCountProvider(bit_width=8)
        self.expected_seq_num = 0
        self.expected_mode = default_transmission_mode
        self.source_id = ByteFieldU16(1)
        self.dest_id = ByteFieldU16(2)
        self.alternative_dest_id = ByteFieldU16(3)
        self.file_segment_len = 64
        self.max_packet_len = 256
        self.positive_ack_intvl_seconds = 0.02
        self.default_remote_cfg = RemoteEntityCfg(
            entity_id=self.dest_id,
            max_packet_len=self.max_packet_len,
            max_file_segment_len=self.file_segment_len,
            closure_requested=closure_requested,
            crc_on_transmission=False,
            default_transmission_mode=default_transmission_mode,
            positive_ack_timer_interval_seconds=self.positive_ack_intvl_seconds,
            positive_ack_timer_expiration_limit=2,
            crc_type=ChecksumType.CRC_32,
            check_limit=2,
        )
        self.alternative_remote_cfg = copy.copy(self.default_remote_cfg)
        self.alternative_remote_cfg.entity_id = self.alternative_dest_id
        self.remote_cfg_table = RemoteEntityCfgTable()
        self.remote_cfg_table.add_config(self.default_remote_cfg)
        self.remote_cfg_table.add_config(self.alternative_remote_cfg)
        # Create an empty file and send it via CFDP
        self.source_handler = SourceHandler(
            cfg=self.local_cfg,
            user=self.cfdp_user,
            remote_cfg_table=self.remote_cfg_table,
            seq_num_provider=self.seq_num_provider,
            check_timer_provider=CheckTimerProviderForTest(),
        )

    def _common_empty_file_test(
        self, transmission_mode: Optional[TransmissionMode]
    ) -> Tuple[TransactionId, MetadataPdu, EofPdu]:
        source_path = Path(f"{tempfile.gettempdir()}/hello.txt")
        dest_path = Path(f"{tempfile.gettempdir()}/hello_copy.txt")
        self._generate_file(source_path, bytes())
        self.seq_num_provider.get_and_increment = MagicMock(
            return_value=self.expected_seq_num
        )
        put_req = PutRequest(
            destination_id=self.dest_id,
            source_file=source_path,
            dest_file=dest_path,
            # Let the transmission mode be auto-determined by the remote MIB
            trans_mode=transmission_mode,
            closure_requested=None,
        )
        metadata_pdu, transaction_id = self._start_source_transaction(put_req)
        crc32 = PredefinedCrc("crc32").digest()
        eof_pdu = self._handle_eof_pdu(transaction_id, crc32, 0)
        return transaction_id, metadata_pdu, eof_pdu

    def _handle_eof_pdu(
        self,
        id: TransactionId,
        expected_checksum: bytes,
        expected_file_size: int,
    ) -> EofPdu:
        self.assertEqual(self.source_handler.transaction_seq_num, id.seq_num)
        transmission_mode = self.source_handler.transmission_mode
        fsm_res = self.source_handler.state_machine_no_packet()
        if transmission_mode == TransmissionMode.UNACKNOWLEDGED:
            if self.closure_requested:
                self._state_checker(
                    fsm_res, 1, CfdpState.BUSY, TransactionStep.WAITING_FOR_FINISHED
                )
            else:
                self._state_checker(fsm_res, 1, CfdpState.IDLE, TransactionStep.IDLE)
        else:
            self._state_checker(
                fsm_res, 1, CfdpState.BUSY, TransactionStep.WAITING_FOR_EOF_ACK
            )
        next_packet = self.source_handler.get_next_packet()
        self.assertIsNotNone(next_packet)
        assert next_packet is not None
        self.assertEqual(next_packet.pdu_type, PduType.FILE_DIRECTIVE)
        self.assertEqual(next_packet.pdu_directive_type, DirectiveType.EOF_PDU)
        eof_pdu = next_packet.to_eof_pdu()
        self.assertEqual(eof_pdu.transaction_seq_num, id.seq_num)
        # For an empty file, checksum verification does not really make sense, so we expect
        # a null checksum here.
        self.assertEqual(eof_pdu.file_checksum, expected_checksum)
        self.assertEqual(eof_pdu.file_size, expected_file_size)
        self.assertEqual(eof_pdu.condition_code, ConditionCode.NO_ERROR)
        self.assertEqual(eof_pdu.fault_location, None)
        self._verify_eof_indication(id)
        if self.expected_mode == TransmissionMode.ACKNOWLEDGED:
            self._state_checker(
                None,
                0,
                CfdpState.BUSY,
                TransactionStep.WAITING_FOR_EOF_ACK,
            )
        return eof_pdu

    def _common_small_file_test(
        self,
        transmission_mode: Optional[TransmissionMode],
        closure_requested: bool,
        file_content: bytes,
    ) -> Tuple[TransactionId, MetadataPdu, FileDataPdu, EofPdu]:
        source_path = Path(f"{tempfile.gettempdir()}/hello.txt")
        dest_path = Path(f"{tempfile.gettempdir()}/hello_copy.txt")
        self.source_id = ByteFieldU32(1)
        self.dest_id = ByteFieldU32(2)
        self.seq_num_provider.get_and_increment = MagicMock(
            return_value=self.expected_seq_num
        )
        self.source_handler.entity_id = self.source_id
        crc32 = self._gen_crc32(file_content)
        self._generate_file(source_path, file_content)
        put_req = PutRequest(
            destination_id=self.dest_id,
            source_file=source_path,
            dest_file=dest_path,
            # Let the transmission mode be auto-determined by the remote MIB
            trans_mode=transmission_mode,
            closure_requested=closure_requested,
        )
        file_size = source_path.stat().st_size
        metadata_pdu, transaction_id = self._start_source_transaction(put_req)
        self.assertEqual(transaction_id.source_id, self.source_handler.entity_id)
        self.assertEqual(transaction_id.seq_num.value, self.expected_seq_num)
        self.assertEqual(
            self.source_handler.transaction_seq_num.value, self.expected_seq_num
        )
        fsm_res = self.source_handler.state_machine_no_packet()
        with self.assertRaises(UnretrievedPdusToBeSent):
            self.source_handler.state_machine_no_packet()
        next_packet = self.source_handler.get_next_packet()
        assert next_packet is not None
        file_data_pdu = self._check_fsm_and_contained_file_data(fsm_res, next_packet)
        eof_pdu = self._handle_eof_pdu(transaction_id, crc32, file_size)
        return transaction_id, metadata_pdu, file_data_pdu, eof_pdu

    def _gen_crc32(self, file_content: bytes) -> bytes:
        crc32 = PredefinedCrc("crc32")
        crc32.update(file_content)
        return crc32.digest()

    def _generate_file(self, path: Path, file_content: bytes):
        with open(path, "wb") as of:
            data = file_content
            of.write(data)

    def _generate_dummy_put_req(self) -> PutRequest:
        return self._generate_generic_put_req(
            Path("dummy-source.txt"), Path("dummy-dest.txt")
        )

    def _generate_dest_dummy_put_req(self, source_path: Path) -> PutRequest:
        return self._generate_generic_put_req(source_path, Path("dummy-dest.txt"))

    def _generate_generic_put_req(
        self,
        source_path: Path,
        dest_path: Path,
    ) -> PutRequest:
        return PutRequest(
            destination_id=self.dest_id,
            source_file=source_path,
            dest_file=dest_path,
            # Let the transmission mode be auto-determined by the remote MIB
            trans_mode=None,
            closure_requested=False,
        )

    def _transaction_with_file_data_wrapper(
        self,
        put_req: PutRequest,
        data: Optional[bytes],
        originating_transaction_id: Optional[TransactionId] = None,
        crc_type: ChecksumType = ChecksumType.CRC_32,
    ) -> TransactionStartParams:
        file_size = None
        crc32 = None
        if data is not None:
            crc32 = self._gen_crc32(data)
            self._generate_file(put_req.source_file, data)
            file_size = put_req.source_file.stat().st_size
        self.local_cfg.local_entity_id = self.source_id
        metadata_pdu, transaction_id = self._start_source_transaction(
            put_req, originating_transaction_id, crc_type
        )
        self.assertEqual(transaction_id.source_id.value, self.source_id.value)
        self.assertEqual(transaction_id.seq_num.value, self.expected_seq_num)
        return TransactionStartParams(transaction_id, metadata_pdu, file_size, crc32)

    def _generic_file_segment_handling(
        self, expected_offset: int, expected_data: bytes
    ) -> FileDataPdu:
        fsm_res = self.source_handler.state_machine()
        self.assertEqual(fsm_res.states.state, CfdpState.BUSY)
        self.assertEqual(self.source_handler.transmission_mode, self.expected_mode)
        self.assertEqual(fsm_res.states.step, TransactionStep.SENDING_FILE_DATA)
        next_packet = self.source_handler.get_next_packet()
        assert next_packet is not None
        self.assertFalse(next_packet.is_file_directive)
        fd_pdu = next_packet.to_file_data_pdu()
        self.assertEqual(fd_pdu.file_data, expected_data)
        self.assertEqual(fd_pdu.offset, expected_offset)
        self.assertEqual(fd_pdu.transaction_seq_num.value, self.expected_seq_num)
        self.assertEqual(fd_pdu.transmission_mode, self.expected_mode)
        return fd_pdu

    def _check_fsm_and_contained_file_data(
        self, fsm_res: FsmResult, pdu_holder: PduHolder
    ) -> FileDataPdu:
        self._state_checker(
            fsm_res,
            0,
            CfdpState.BUSY,
            TransactionStep.SENDING_FILE_DATA,
        )
        self.assertFalse(pdu_holder.is_file_directive)
        return pdu_holder.to_file_data_pdu()

    def _start_source_transaction(
        self,
        put_request: PutRequest,
        expected_originating_id: Optional[TransactionId] = None,
        crc_type: ChecksumType = ChecksumType.CRC_32,
    ) -> Tuple[MetadataPdu, TransactionId]:
        self.source_handler.put_request(put_request)
        fsm_res = self.source_handler.state_machine()
        self._state_checker(
            fsm_res,
            1,
            CfdpState.BUSY,
            TransactionStep.SENDING_METADATA,
        )
        transaction_id = self.source_handler.transaction_id
        assert transaction_id is not None
        self._verify_transaction_indication(expected_originating_id)
        next_packet = self.source_handler.get_next_packet()
        assert next_packet is not None
        self.assertEqual(next_packet.pdu_type, PduType.FILE_DIRECTIVE)
        self.assertEqual(next_packet.pdu_directive_type, DirectiveType.METADATA_PDU)
        metadata_pdu = next_packet.to_metadata_pdu()
        if put_request.closure_requested is not None:
            self.assertEqual(
                metadata_pdu.params.closure_requested, put_request.closure_requested
            )
        self.assertEqual(metadata_pdu.checksum_type, crc_type)
        source_file_as_posix = None
        if put_request.source_file is not None:
            source_file_as_posix = put_request.source_file.as_posix()
        self.assertEqual(metadata_pdu.source_file_name, source_file_as_posix)
        dest_file_as_posix = None
        if put_request.dest_file is not None:
            dest_file_as_posix = put_request.dest_file.as_posix()
        self.assertEqual(metadata_pdu.dest_file_name, dest_file_as_posix)
        self.assertEqual(metadata_pdu.dest_entity_id.value, self.dest_id.value)
        return metadata_pdu, transaction_id

    def _verify_transaction_indication(
        self, expected_originating_id: Optional[TransactionId]
    ):
        self.cfdp_user.transaction_indication.assert_called_once()
        self.assertEqual(self.cfdp_user.transaction_indication.call_count, 1)
        transaction_params = cast(
            TransactionParams,
            self.cfdp_user.transaction_indication.call_args.args[0],
        )
        self.assertEqual(
            transaction_params.transaction_id, self.source_handler.transaction_id
        )
        self.assertEqual(
            transaction_params.originating_transaction_id, expected_originating_id
        )

    def _verify_eof_indication(self, expected_transaction_id: TransactionId):
        self.source_handler.state_machine()
        self.cfdp_user.eof_sent_indication.assert_called_once()
        self.assertEqual(self.cfdp_user.eof_sent_indication.call_count, 1)
        call_args = self.cfdp_user.eof_sent_indication.call_args[0]
        self.assertEqual(len(call_args), 1)
        self.assertEqual(call_args[0], expected_transaction_id)

    def _state_checker(
        self,
        fsm_res: Optional[FsmResult],
        num_packets_ready: int,
        expected_state: CfdpState,
        expected_step: TransactionStep,
    ):
        if fsm_res is not None:
            self.assertEqual(fsm_res.states.state, expected_state)
            self.assertEqual(fsm_res.states.step, expected_step)
            if num_packets_ready > 0:
                if fsm_res.states.num_packets_ready != num_packets_ready:
                    self.assertEqual(
                        fsm_res.states.num_packets_ready, num_packets_ready
                    )
            elif num_packets_ready == 0 and fsm_res.states.num_packets_ready > 0:
                packets = []
                while True:
                    pdu_holder = self.source_handler.get_next_packet()
                    if pdu_holder is None:
                        break
                    else:
                        packets.append(pdu_holder.pdu)
                raise AssertionError(f"Expected no packets, found: {packets}")
        if num_packets_ready > 0:
            self.assertTrue(self.source_handler.packets_ready)
        if expected_state != CfdpState.IDLE:
            self.assertEqual(self.source_handler.transmission_mode, self.expected_mode)
        self.assertEqual(self.source_handler.states.state, expected_state)
        self.assertEqual(self.source_handler.states.step, expected_step)
        self.assertEqual(
            self.source_handler.states.num_packets_ready, num_packets_ready
        )
        self.assertEqual(self.source_handler.state, expected_state)
        self.assertEqual(self.source_handler.step, expected_step)
        self.assertEqual(self.source_handler.num_packets_ready, num_packets_ready)

    def _verify_transaction_finished_indication(
        self, expected_id: TransactionId, expected_finish_params: FinishedParams
    ):
        self.cfdp_user.transaction_finished_indication.assert_called_once()
        self.assertEqual(self.cfdp_user.transaction_finished_indication.call_count, 1)
        transaction_finished_params = cast(
            TransactionFinishedParams,
            self.cfdp_user.transaction_finished_indication.call_args.args[0],
        )
        self.assertEqual(transaction_finished_params.transaction_id, expected_id)
        self.assertEqual(
            transaction_finished_params.finished_params, expected_finish_params
        )

    def _generate_test_file(self) -> Path:
        source_path = Path(f"{tempfile.gettempdir()}/hello.txt")
        return source_path

    def tearDown(self) -> None:
        pass
