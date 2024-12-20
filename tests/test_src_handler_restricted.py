from __future__ import annotations  # Python 3.9 compatibility for | syntax

import copy
import shutil
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import MagicMock

from spacepackets.cfdp import (
    ChecksumType,
    ConditionCode,
    CrcFlag,
    Direction,
    DirectiveType,
    FinishedParams,
    LargeFileFlag,
    PduConfig,
    SegmentationControl,
    TransmissionMode,
)
from spacepackets.cfdp.pdu import (
    AckPdu,
    EofPdu,
    FileDataPdu,
    FinishedPdu,
    MetadataPdu,
    TransactionStatus,
)
from spacepackets.seqcount import SeqCountProvider
from spacepackets.util import ByteFieldU16

from cfdppy import (
    CfdpState,
    IndicationCfg,
    LocalEntityCfg,
    PutRequest,
    RemoteEntityCfg,
    RemoteEntityCfgTable,
    RestrictedFilestore,
)
from cfdppy.handler import SourceHandler
from tests.cfdp_fault_handler_mock import FaultHandler
from tests.cfdp_user_mock import CfdpUser
from tests.common import CheckTimerProviderForTest


class TestSrcHandlerRestrictedFileStore(TestCase):
    def setUp(self):
        super().setUp()
        self.temp_dir = Path(tempfile.mkdtemp())
        self.closure_requested = False
        self.indication_cfg = IndicationCfg(True, True, True, True, True, True)
        self.fault_handler = MagicMock()
        self.fault_handler.mock_add_spec(spec=FaultHandler, spec_set=True)
        self.local_cfg = LocalEntityCfg(ByteFieldU16(1), self.indication_cfg, self.fault_handler)
        self.cfdp_user = CfdpUser(vfs=RestrictedFilestore(self.temp_dir))
        self.seq_num_provider = SeqCountProvider(bit_width=8)
        self.expected_seq_num = 0
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
            closure_requested=self.closure_requested,
            crc_on_transmission=False,
            default_transmission_mode=TransmissionMode.ACKNOWLEDGED,
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

    def tearDown(self):
        shutil.rmtree(self.temp_dir)

    def test_src_handler_restricted(self):
        file_content = "Hello, World!"
        with open(self.temp_dir.joinpath("hello.txt"), "w") as f:
            f.write(file_content)
        source_path = Path("hello.txt")
        dest_path = Path("hello_copy.txt")
        self.seq_num_provider.get_and_increment = MagicMock(return_value=self.expected_seq_num)
        self.source_handler.entity_id = self.source_id
        put_req = PutRequest(
            destination_id=self.dest_id,
            source_file=source_path,
            dest_file=dest_path,
            # Let the transmission mode be auto-determined by the remote MIB
            trans_mode=TransmissionMode.ACKNOWLEDGED,
            closure_requested=True,
        )
        self.source_handler.put_request(put_req)

        fsm = self.source_handler.state_machine()
        self.assertTrue(fsm.states.packets_ready)
        self.assertEqual(fsm.states.num_packets_ready, 1)
        next_pdu = self.source_handler.get_next_packet()
        self.assertIsInstance(next_pdu.base, MetadataPdu)
        fsm = self.source_handler.state_machine()
        self.assertTrue(fsm.states.packets_ready)
        file_data = self.source_handler.get_next_packet()
        self.assertIsInstance(file_data.base, FileDataPdu)
        fsm = self.source_handler.state_machine()
        self.assertTrue(fsm.states.packets_ready)
        eof_data = self.source_handler.get_next_packet()
        self.assertIsInstance(eof_data.base, EofPdu)
        # Send ACK
        pdu_conf = PduConfig(
            direction=Direction.TOWARDS_SENDER,
            transaction_seq_num=eof_data.base.transaction_seq_num,
            source_entity_id=self.source_id,
            dest_entity_id=self.dest_id,
            trans_mode=TransmissionMode.ACKNOWLEDGED,
            file_flag=LargeFileFlag.NORMAL,
            crc_flag=CrcFlag.NO_CRC,
            seg_ctrl=SegmentationControl.NO_RECORD_BOUNDARIES_PRESERVATION,
        )
        eof_ack = AckPdu(
            pdu_conf=pdu_conf,
            directive_code_of_acked_pdu=DirectiveType.EOF_PDU,
            condition_code_of_acked_pdu=ConditionCode.NO_ERROR,
            transaction_status=TransactionStatus.ACTIVE,
        )
        fsm = self.source_handler.state_machine(packet=eof_ack)
        self.assertFalse(fsm.states.packets_ready)

        finished = FinishedPdu(pdu_conf=pdu_conf, params=FinishedParams.success_params())
        fsm = self.source_handler.state_machine(packet=finished)
        self.assertTrue(fsm.states.packets_ready)
        finished_ack = self.source_handler.get_next_packet()
        self.assertIsInstance(finished_ack.base, AckPdu)
        fsm = self.source_handler.state_machine()
        self.assertEqual(fsm.states.state, CfdpState.IDLE)
