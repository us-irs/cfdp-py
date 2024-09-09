from __future__ import annotations

import enum
import logging
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Deque, Optional, Tuple

from cfdppy import (
    CfdpUserBase,
    LocalEntityCfg,
    RemoteEntityCfg,
)
from cfdppy.defs import CfdpState
from cfdppy.exceptions import (
    InvalidDestinationId,
    InvalidNakPdu,
    InvalidPduDirection,
    InvalidPduForSourceHandler,
    InvalidSourceId,
    InvalidTransactionSeqNum,
    NoRemoteEntityCfgFound,
    PduIgnoredForSource,
    PduIgnoredForSourceReason,
    SourceFileDoesNotExist,
    UnretrievedPdusToBeSent,
)
from cfdppy.handler.common import _PositiveAckProcedureParams
from cfdppy.handler.defs import (
    _FileParamsBase,
)
from cfdppy.mib import CheckTimerProvider, EntityType, RemoteEntityCfgTable
from cfdppy.request import PutRequest
from cfdppy.user import TransactionFinishedParams, TransactionParams
from spacepackets.cfdp import (
    ConditionCode,
    CrcFlag,
    Direction,
    FaultHandlerCode,
    GenericPduPacket,
    LargeFileFlag,
    PduConfig,
    PduType,
    TransactionId,
    TransmissionMode,
)
from spacepackets.cfdp.defs import ChecksumType
from spacepackets.cfdp.pdu import (
    AbstractFileDirectiveBase,
    AckPdu,
    DeliveryCode,
    DirectiveType,
    EofPdu,
    FileDataPdu,
    FileStatus,
    MetadataParams,
    MetadataPdu,
    PduHolder,
    TransactionStatus,
)
from spacepackets.cfdp.pdu.file_data import (
    FileDataParams,
    get_max_file_seg_len_for_max_packet_len_and_pdu_cfg,
)
from spacepackets.cfdp.pdu.finished import FinishedParams
from spacepackets.cfdp.tlv import ProxyMessageType
from spacepackets.countdown import Countdown
from spacepackets.seqcount import ProvidesSeqCount
from spacepackets.util import ByteFieldGenerator, UnsignedByteField

_LOGGER = logging.getLogger(__name__)


class TransactionStep(enum.Enum):
    IDLE = 0
    TRANSACTION_START = 1
    # The following three are used for the Copy File Procedure
    SENDING_METADATA = 3
    SENDING_FILE_DATA = 4
    RETRANSMITTING = 5
    """Re-transmitting missing packets in acknowledged mode."""
    SENDING_EOF = 6
    WAITING_FOR_EOF_ACK = 7
    WAITING_FOR_FINISHED = 8
    SENDING_ACK_OF_FINISHED = 9
    NOTICE_OF_COMPLETION = 10


@dataclass
class _SourceFileParams(_FileParamsBase):
    # This flag accounts for the empty file case where an EOF still needs to be sent.
    empty_file: bool = False

    @classmethod
    def empty(cls) -> _SourceFileParams:
        return cls(
            progress=0,
            segment_len=0,
            crc32=bytes(),
            file_size=0,
            empty_file=False,
            metadata_only=False,
        )

    def reset(self):
        self.empty_file = False
        super().reset()


@dataclass
class SourceStateWrapper:
    state: CfdpState = CfdpState.IDLE
    step: TransactionStep = TransactionStep.IDLE
    _num_packets_ready: int = 0

    @property
    def num_packets_ready(self) -> int:
        return self._num_packets_ready

    @property
    def packets_ready(self) -> bool:
        return self.num_packets_ready > 0


class _AckedModeParams:
    def __init__(self) -> None:
        self.step_before_retransmission: Optional[TransactionStep] = None
        self.segment_reqs_to_handle: Optional[Tuple[int, int]] = None
        self.segment_req_index: int = 0


class _TransferFieldWrapper:
    def __init__(self, local_entity_id: UnsignedByteField):
        self.transaction_id: Optional[TransactionId] = None
        self.check_timer: Optional[Countdown] = None
        self.positive_ack_params: _PositiveAckProcedureParams = (
            _PositiveAckProcedureParams()
        )
        self.cond_code_eof: Optional[ConditionCode] = None
        self.ack_params: _AckedModeParams = _AckedModeParams()
        self.fp: _SourceFileParams = _SourceFileParams.empty()
        self.finished_params: Optional[FinishedParams] = None
        self.remote_cfg: Optional[RemoteEntityCfg] = None
        self.closure_requested: bool = False
        self.pdu_conf = PduConfig.empty()
        self.pdu_conf.source_entity_id = local_entity_id

    @property
    def source_id(self) -> UnsignedByteField:
        return self.pdu_conf.source_entity_id

    @source_id.setter
    def source_id(self, source_id: UnsignedByteField):
        self.pdu_conf.source_entity_id = source_id

    @property
    def positive_ack_counter(self) -> int:
        return self.positive_ack_params.ack_counter

    @property
    def dest_id(self):
        return self.pdu_conf.dest_entity_id

    @dest_id.setter
    def dest_id(self, dest_id: UnsignedByteField):
        self.pdu_conf.dest_entity_id = dest_id

    @property
    def transmission_mode(self) -> TransmissionMode:
        return self.pdu_conf.trans_mode

    @transmission_mode.setter
    def transmission_mode(self, trans_mode: TransmissionMode):
        self.pdu_conf.trans_mode = trans_mode

    @property
    def transaction_seq_num(self) -> UnsignedByteField:
        return self.pdu_conf.transaction_seq_num

    @transaction_seq_num.setter
    def transaction_seq_num(self, seq_num: UnsignedByteField):
        self.pdu_conf.transaction_seq_num = seq_num

    def reset(self):
        self.fp.reset()
        self.remote_cfg = None
        self.transaction_id = None
        self.check_timer = None
        self.cond_code_eof = None
        self.closure_requested = False
        self.pdu_conf = PduConfig.empty()
        self.finished_params = None
        self.positive_ack_params = _PositiveAckProcedureParams()


class FsmResult:
    def __init__(self, states: SourceStateWrapper):
        self.states = states


class SourceHandler:
    """This is the primary CFDP source handler. It models the CFDP source entity, which is
    primarily responsible for handling put requests to send files to another CFDP destination
    entity.

    As such, it contains a state machine to perform all operations necessary to perform a
    source-to-destination file transfer. This class does not send the CFDP PDU packets directly
    to allow for greater flexibility. For example, a user might want to wrap the CFDP packet
    entities into a CCSDS space packet or into a special frame type. The handler can handle
    both unacknowledged (class 1) and acknowledged (class 2) file tranfers.

    The following core functions are the primary interface:

     1. :py:meth:`put_request` can be used to start transactions, most notably to start
        and perform a Copy File procedure to send a file or to send a Proxy Put Request to request
        a file.
     2. :py:meth:`state_machine` is the primary interface to execute an
        active file transfer. It generates the necessary CFDP PDUs for this process.
        This method is also used to insert received packets with the appropriate destination ID
        and target handler type into the state machine.
     3. :py:meth:`get_next_packet` retrieves the next packet which should be sent to the remote
        destination entity of a file copy operation.

    A put request will only be accepted if the handler is in the idle state. Furthermore,
    packet insertion is not allowed until all packets to send were retrieved after a state machine
    call.

    This handler also does not support concurrency out of the box but is flexible enough to be used
    in different concurrent contexts. For example, you can dynamically create new handlers and
    run them inside a thread pool, or move the newly created handler to a new thread."""

    def __init__(
        self,
        cfg: LocalEntityCfg,
        user: CfdpUserBase,
        remote_cfg_table: RemoteEntityCfgTable,
        check_timer_provider: CheckTimerProvider,
        seq_num_provider: ProvidesSeqCount,
    ):
        self.states = SourceStateWrapper()
        self.cfg = cfg
        self.user = user
        self.remote_cfg_table = remote_cfg_table
        self.seq_num_provider = seq_num_provider
        self.check_timer_provider = check_timer_provider
        self._params = _TransferFieldWrapper(cfg.local_entity_id)
        self._put_req: Optional[PutRequest] = None
        self._pdus_to_be_sent: Deque[PduHolder] = deque()

    @property
    def entity_id(self) -> UnsignedByteField:
        return self.cfg.local_entity_id

    @entity_id.setter
    def entity_id(self, entity_id: UnsignedByteField):
        self.cfg.local_entity_id = entity_id
        self._params.source_id = entity_id

    @property
    def transaction_seq_num(self) -> UnsignedByteField:
        return self.pdu_conf.transaction_seq_num

    @property
    def pdu_conf(self) -> PduConfig:
        return self._params.pdu_conf

    @property
    def positive_ack_counter(self) -> int:
        return self._params.positive_ack_counter

    @property
    def transmission_mode(self) -> Optional[TransmissionMode]:
        if self.state == CfdpState.IDLE:
            return None
        return self._params.transmission_mode

    @property
    def state(self) -> CfdpState:
        return self.states.state

    @property
    def step(self) -> TransactionStep:
        return self.states.step

    @property
    def packets_ready(self) -> bool:
        return self.states.packets_ready

    @property
    def num_packets_ready(self) -> int:
        return self.states.num_packets_ready

    def put_request(self, request: PutRequest):
        """This function is used to pass a put request to the source handler, which is
        also used to start a file copy operation. As such, this function models the Put.request
        CFDP primtiive.

        Please note that the source handler can also process one put request at a time.
        The caller is responsible of creating a new source handler, one handler can only handle
        one file copy request at a time.


        Raises
        --------

        ValueError
            Invalid transmission mode detected.
        NoRemoteEntityCfgFound
            No remote configuration found for destination ID specified in the Put Request.
        SourceFileDoesNotExist
            File specified for Put Request does not exist.

        Returns
        --------

        False if the handler is busy. True if the handling of the request was successfull.
        """
        if self.states.state != CfdpState.IDLE:
            _LOGGER.debug("CFDP source handler is busy, can't process put request")
            return False
        self._put_req = request
        if self._put_req.source_file is not None:
            assert isinstance(self._put_req.source_file, Path)
            if not self.user.vfs.file_exists(self._put_req.source_file):
                raise SourceFileDoesNotExist(self._put_req.source_file)
        if self._put_req.dest_file is not None:
            assert isinstance(self._put_req.dest_file, Path)
        self._params.remote_cfg = self.remote_cfg_table.get_cfg(request.destination_id)
        if self._params.remote_cfg is None:
            raise NoRemoteEntityCfgFound(entity_id=request.destination_id)
        self._params.dest_id = request.destination_id
        self.states._num_packets_ready = 0
        self.states.state = CfdpState.BUSY
        self._setup_transmission_params()
        if self._params.transmission_mode == TransmissionMode.UNACKNOWLEDGED:
            _LOGGER.debug("Starting Put Request handling in NAK mode")
        elif self._params.transmission_mode == TransmissionMode.ACKNOWLEDGED:
            _LOGGER.debug("Starting Put Request handling in ACK mode")
        else:
            raise ValueError(
                f"Invalid transmission mode {self._params.transmission_mode} passed"
            )
        return True

    def cancel_request(self, transaction_id: TransactionId) -> bool:
        """This function models the Cancel.request CFDP primtive and is the recommended way
        to cancel a transaction. It will cause a Notice Of Cancellation at this entity.
        Please note that the state machine might still be active because a canceled transfer
        might still require some packets to be sent to the remote receiver entity.

        Returns
        --------
        True
            Current transfer was cancelled
        False
            The state machine is in the IDLE state or there is a transaction ID missmatch.
        """
        if self.states.step == CfdpState.IDLE:
            return False
        if self.states.packets_ready:
            raise UnretrievedPdusToBeSent()
        if (
            self._params.transaction_id is not None
            and transaction_id == self._params.transaction_id
        ):
            self._notice_of_cancellation(ConditionCode.CANCEL_REQUEST_RECEIVED)
            return True
        return False

    def _check_inserted_packet(self, packet: AbstractFileDirectiveBase):
        if packet.direction != Direction.TOWARDS_SENDER:
            raise InvalidPduDirection(
                Direction.TOWARDS_SENDER, packet.pdu_header.direction
            )
        if packet.source_entity_id.value != self.entity_id.value:
            raise InvalidSourceId(self.entity_id, packet.source_entity_id)
        # TODO: This can happen if a packet is received for which no transaction was started..
        #       A better exception might be worth a thought..
        if self._params.remote_cfg is None:
            raise NoRemoteEntityCfgFound(entity_id=packet.dest_entity_id)
        if packet.dest_entity_id.value != self._params.remote_cfg.entity_id.value:
            raise InvalidDestinationId(
                self._params.remote_cfg.entity_id, packet.dest_entity_id
            )

        if packet.transaction_seq_num.value != self._params.transaction_seq_num.value:
            raise InvalidTransactionSeqNum(
                self._params.transaction_seq_num, packet.transaction_seq_num
            )
        if packet.directive_type in [
            DirectiveType.METADATA_PDU,
            DirectiveType.EOF_PDU,
            DirectiveType.PROMPT_PDU,
        ]:
            raise InvalidPduForSourceHandler(packet)
        if self._params.transmission_mode == TransmissionMode.UNACKNOWLEDGED and (
            packet.directive_type == DirectiveType.KEEP_ALIVE_PDU
            or packet.directive_type == DirectiveType.NAK_PDU
        ):
            raise PduIgnoredForSource(
                reason=PduIgnoredForSourceReason.ACK_MODE_PACKET_INVALID_MODE,
                ignored_packet=packet,
            )
        if packet.directive_type != DirectiveType.NAK_PDU:
            if (
                self.states.step == TransactionStep.WAITING_FOR_EOF_ACK
                and packet.directive_type != DirectiveType.ACK_PDU
            ):
                raise PduIgnoredForSource(
                    reason=PduIgnoredForSourceReason.NOT_WAITING_FOR_ACK,
                    ignored_packet=packet,
                )
            if (
                self.states.step == TransactionStep.WAITING_FOR_FINISHED
                and packet.directive_type != DirectiveType.FINISHED_PDU
            ):
                raise PduIgnoredForSource(
                    reason=PduIgnoredForSourceReason.NOT_WAITING_FOR_FINISHED_PDU,
                    ignored_packet=packet,
                )

    def get_next_packet(self) -> Optional[PduHolder]:
        """Retrieve the next packet which should be sent to the remote CFDP destination entity."""
        if len(self._pdus_to_be_sent) == 0:
            return None
        self.states._num_packets_ready -= 1
        return self._pdus_to_be_sent.popleft()

    def state_machine_no_packet(self) -> FsmResult:
        """Helper method to call :py:meth:`state_machine` with None as the packet argument."""
        return self.state_machine(None)

    def state_machine(
        self, packet: Optional[AbstractFileDirectiveBase] = None
    ) -> FsmResult:
        """This is the primary state machine which performs the CFDP procedures like  PDU
        generation or CRC calculation. The packets generated by this finite-state machine (FSM)
        need to be sent by the user and can be retrieved using the :py:meth:`get_next_packet`
        method.

        This method also allows inserting packets into the state machine via the optional packet
        argument.

        Raises
        --------
        InvalidPduDirection
            PDU direction field wrong.
        InvalidPduForSourceHandler
            Invalid PDU file directive type.
        PduIgnoredForSource
            The specified PDU can not be handled in the current state.
        NoRemoteEntityCfgFound
            No remote configuration found for specified destination entity.
        InvalidSourceId
            Source ID not identical to local entity ID.
        InvalidDestinationId
            Destination ID was found, but there is a mismatch between the packet destination ID
            and the remote configuration entity ID.
        UnretrievedPdusToBeSent
            There are still PDUs which need to be sent before calling the FSM again.
        ChecksumNotImplemented
            Right now, only a subset of the checksums specified for the CFDP standard are implemented.
        SourceFileDoesNotExist
            The source file for which a transaction was requested does not exist. This can happen
            if the file is deleted during a transaction.
        """
        if packet is not None:
            self._check_inserted_packet(packet)
        if self.states.state == CfdpState.IDLE:
            return FsmResult(self.states)
        self._fsm_non_idle(packet)
        return FsmResult(self.states)

    @property
    def transaction_id(self) -> Optional[TransactionId]:
        return self._params.transaction_id

    def _reset_internal(self, clear_packet_queue: bool):
        """This function is public to allow completely resetting the handler, but it is explicitely
        discouraged to do this. CFDP generally has mechanism to detect issues and errors on itself.
        """
        self.states.step = TransactionStep.IDLE
        self.states.state = CfdpState.IDLE
        if clear_packet_queue:
            self._pdus_to_be_sent.clear()
        self._params.reset()

    def reset(self):
        """This function is public to allow completely resetting the handler, but it is explicitely
        discouraged to do this. CFDP generally has mechanism to detect issues and errors on itself.
        """
        self._reset_internal(True)

    def _fsm_non_idle(self, packet: Optional[AbstractFileDirectiveBase]):
        self._fsm_advancement_after_packets_were_sent()
        packet_holder = PduHolder(packet)
        if self._put_req is None:
            return
        if self.states.step == TransactionStep.IDLE:
            self.states.step = TransactionStep.TRANSACTION_START
        if self.states.step == TransactionStep.TRANSACTION_START:
            self._transaction_start()
            self.states.step = TransactionStep.SENDING_METADATA
        if self.states.step == TransactionStep.SENDING_METADATA:
            self._prepare_metadata_pdu()
            return
        if self.states.step == TransactionStep.SENDING_FILE_DATA:
            if self._sending_file_data_fsm(packet_holder):
                return
        if self.states.step == TransactionStep.SENDING_EOF:
            self._prepare_eof_pdu(
                self._checksum_calculation(self._params.fp.file_size),
            )
            self._handle_eof_sent(False)
        if self.states.step == TransactionStep.WAITING_FOR_EOF_ACK:
            self._handle_waiting_for_ack(packet_holder)
        if self.states.step == TransactionStep.WAITING_FOR_FINISHED:
            self._handle_wait_for_finish(packet_holder)
        if self.states.step == TransactionStep.NOTICE_OF_COMPLETION:
            self._notice_of_completion()

    def _transaction_start(self):
        originating_transaction_id = self._check_for_originating_id()
        self._prepare_file_params()
        self._prepare_pdu_conf(self._params.fp.file_size)
        self._get_next_transfer_seq_num()
        self._calculate_max_file_seg_len()
        self._params.transaction_id = TransactionId(
            source_entity_id=self.cfg.local_entity_id,
            transaction_seq_num=self.transaction_seq_num,
        )
        self.user.transaction_indication(
            TransactionParams(self._params.transaction_id, originating_transaction_id)
        )

    def _check_for_originating_id(self) -> Optional[TransactionId]:
        """This function only returns an originating ID for if not proxy put response is
        contained in the message to user list. This special logic is in place to avoid permanent
        loop which would occur when the user uses the orignating ID to register active proxy put
        request, and this ID would also be generated for proxy put responses."""
        contains_proxy_put_response = False
        contains_originating_id = False
        originating_id = None
        if self._put_req.msgs_to_user is None:
            return None
        for msgs_to_user in self._put_req.msgs_to_user:
            if msgs_to_user.is_reserved_cfdp_message():
                reserved_cfdp_msg = msgs_to_user.to_reserved_msg_tlv()
                if reserved_cfdp_msg.is_originating_transaction_id():
                    contains_originating_id = True
                    originating_id = reserved_cfdp_msg.get_originating_transaction_id()
                if (
                    reserved_cfdp_msg.is_cfdp_proxy_operation()
                    and reserved_cfdp_msg.get_cfdp_proxy_message_type()
                    == ProxyMessageType.PUT_RESPONSE
                ):
                    contains_proxy_put_response = True
        if not contains_proxy_put_response and contains_originating_id:
            return originating_id
        return None

    def _prepare_file_params(self):
        assert self._put_req is not None
        if self._put_req.metadata_only:
            self._params.fp.metadata_only = True
        else:
            assert self._put_req.source_file is not None
            if not self._put_req.source_file.exists():
                # TODO: Handle this exception in the handler, reset CFDP state machine
                raise SourceFileDoesNotExist(self._put_req.source_file)
            file_size = self.user.vfs.file_size(self._put_req.source_file)
            if file_size == 0:
                self._params.fp.empty_file = True
            else:
                self._params.fp.file_size = file_size

    def _prepare_pdu_conf(self, file_size: int):
        # Please note that the transmission mode and closure requested field were set in
        # a previous step.
        assert self._put_req is not None
        assert self._params.remote_cfg is not None
        if not self._params.fp.metadata_only:
            if file_size > pow(2, 32) - 1:
                self._params.pdu_conf.file_flag = LargeFileFlag.LARGE
            else:
                self._params.pdu_conf.file_flag = LargeFileFlag.NORMAL
        if self._put_req.seg_ctrl is not None:
            self._params.pdu_conf.seg_ctrl = self._put_req.seg_ctrl
        # Both the source entity and destination entity ID field must have the same size.
        # We use the larger of either the Put Request destination ID or the local entity ID
        # as the size for the new entity IDs.
        larger_entity_width = max(
            self.cfg.local_entity_id.byte_len, self._put_req.destination_id.byte_len
        )
        if larger_entity_width != self.cfg.local_entity_id.byte_len:
            self._params.pdu_conf.source_entity_id = UnsignedByteField(
                self.cfg.local_entity_id.value, larger_entity_width
            )
        else:
            self._params.pdu_conf.source_entity_id = self.cfg.local_entity_id

        if larger_entity_width != self._put_req.destination_id.byte_len:
            self._params.pdu_conf.dest_entity_id = UnsignedByteField(
                self._put_req.destination_id.value, larger_entity_width
            )
        else:
            self._params.pdu_conf.dest_entity_id = self._put_req.destination_id

        self._params.pdu_conf.crc_flag = CrcFlag(
            self._params.remote_cfg.crc_on_transmission
        )
        self._params.pdu_conf.direction = Direction.TOWARDS_RECEIVER

    def _calculate_max_file_seg_len(self):
        assert self._params.remote_cfg is not None
        derived_max_seg_len = get_max_file_seg_len_for_max_packet_len_and_pdu_cfg(
            self._params.pdu_conf, self._params.remote_cfg.max_packet_len
        )
        self._params.fp.segment_len = derived_max_seg_len
        if (
            self._params.remote_cfg.max_file_segment_len is not None
            and self._params.remote_cfg.max_file_segment_len < derived_max_seg_len
        ):
            self._params.fp.segment_len = self._params.remote_cfg.max_file_segment_len

    def _prepare_metadata_pdu(self):
        assert self._put_req is not None
        options = []
        if self._put_req.metadata_only:
            assert self._params.remote_cfg is not None
            params = MetadataParams(
                closure_requested=self._params.closure_requested,
                checksum_type=ChecksumType.NULL_CHECKSUM,
                file_size=0,
                dest_file_name=None,
                source_file_name=None,
            )
        else:
            # Funny name.
            params = self._prepare_metadata_base_params_with_metadata()
        if self._put_req.fs_requests is not None:
            for fs_request in self._put_req.fs_requests:
                options.append(fs_request)
        if self._put_req.fault_handler_overrides is not None:
            for fh_override in self._put_req.fault_handler_overrides:
                options.append(fh_override)
        if self._put_req.flow_label_tlv is not None:
            options.append(self._put_req.flow_label_tlv)
        if self._put_req.msgs_to_user is not None:
            for msg_to_user in self._put_req.msgs_to_user:
                options.append(msg_to_user)
        self._add_packet_to_be_sent(
            MetadataPdu(pdu_conf=self._params.pdu_conf, params=params, options=options)
        )

    def _prepare_metadata_base_params_with_metadata(self) -> MetadataParams:
        assert self._params.remote_cfg is not None
        return MetadataParams(
            dest_file_name=self._put_req.dest_file.as_posix(),  # type: ignore
            source_file_name=self._put_req.source_file.as_posix(),  # type: ignore
            checksum_type=self._params.remote_cfg.crc_type,
            closure_requested=self._params.closure_requested,
            file_size=self._params.fp.file_size,
        )

    def _sending_file_data_fsm(self, packet_holder: PduHolder) -> bool:
        # This function returns whether the FSM should return or not.
        # During the PDU send phase, handle the re-transmission of missing files in
        # acknowledged mode.
        if self.transmission_mode == TransmissionMode.ACKNOWLEDGED:
            if self.__handle_retransmission(packet_holder):
                return True

        # No need to send a file data PDU for an empty file
        if (
            not self._params.fp.metadata_only
            and self._params.fp.progress < self._params.fp.file_size
        ):
            self._prepare_progressing_file_data_pdu()
            # Not finished yet. We exit here to allow the user to do flow control.
            return True
        if self._params.fp.empty_file:
            # Special case: Empty file, EOF still required.
            self._params.cond_code_eof = ConditionCode.NO_ERROR
            self.states.step = TransactionStep.SENDING_EOF
        elif self._params.fp.metadata_only:
            # Special case: Metadata Only, no EOF required.
            if self._params.closure_requested:
                self.states.step = TransactionStep.WAITING_FOR_FINISHED
            else:
                self.states.step = TransactionStep.NOTICE_OF_COMPLETION
        return False

    def __handle_retransmission(self, packet_holder: PduHolder) -> bool:
        """Returns whether a packet was generated and re-transmission is active."""
        if packet_holder.pdu is None:
            return False
        if packet_holder.pdu_directive_type != DirectiveType.NAK_PDU:
            return False
        nak_pdu = packet_holder.to_nak_pdu()
        for segment_req in nak_pdu.segment_requests:
            self._handle_segment_req(segment_req)
        self._params.ack_params.step_before_retransmission = self.states.step
        self.states.step = TransactionStep.RETRANSMITTING
        return True

    def _handle_segment_req(self, segment_req: Tuple[int, int]):
        # Special case: Metadata PDU is re-requested
        if segment_req[0] == 0 and segment_req[1] == 0:
            # Re-transmit the metadata PDU
            self._prepare_metadata_pdu()
        else:
            if segment_req[1] < segment_req[0]:
                raise InvalidNakPdu("end offset larger than start offset")
            elif segment_req[0] > self._params.fp.progress:
                raise InvalidNakPdu("start offset larger than current file progress")

            missing_chunk_len = segment_req[1] - segment_req[0]
            current_offset = segment_req[0]
            while missing_chunk_len > 0:
                chunk_size = min(missing_chunk_len, self._params.fp.segment_len)
                self._prepare_file_data_pdu(current_offset, chunk_size)
                current_offset += chunk_size
                missing_chunk_len -= chunk_size

    def _handle_waiting_for_ack(self, packet_holder: PduHolder):
        if self.transmission_mode == TransmissionMode.UNACKNOWLEDGED:
            _LOGGER.error(
                f"invalid ACK waiting function call for transmission mode "
                f"{self.transmission_mode!r}"
            )
        if self.__handle_retransmission(packet_holder):
            return
        if packet_holder.pdu is None or (
            packet_holder.pdu_type == PduType.FILE_DIRECTIVE
            and packet_holder.pdu_directive_type != DirectiveType.ACK_PDU
        ):
            self._handle_positive_ack_procedures()
            return
        ack_pdu = packet_holder.to_ack_pdu()
        if ack_pdu.directive_code_of_acked_pdu == DirectiveType.EOF_PDU:
            # TODO: Equality check required? I am not sure why the condition code is supplied
            #       as part of the ACK packet.
            self.states.step = TransactionStep.WAITING_FOR_FINISHED
        else:
            _LOGGER.error(
                f"received ACK PDU with invalid acked directive code"
                f" {ack_pdu.directive_code_of_acked_pdu!r}"
            )

    def _handle_positive_ack_procedures(self):
        """Positive ACK procedures according to chapter 4.7.1 of the CFDP standard."""
        assert self._params.positive_ack_params.ack_timer is not None
        assert self._params.remote_cfg is not None
        if self._params.positive_ack_params.ack_timer.timed_out():
            if (
                self._params.positive_ack_params.ack_counter + 1
                >= self._params.remote_cfg.positive_ack_timer_expiration_limit
            ):
                self._declare_fault(ConditionCode.POSITIVE_ACK_LIMIT_REACHED)
                return
            self._params.positive_ack_params.ack_timer.reset()
            self._params.positive_ack_params.ack_counter += 1
            self._prepare_eof_pdu(
                self._checksum_calculation(self._params.fp.file_size),
            )

    def _handle_wait_for_finish(self, packet_holder: PduHolder):
        if (
            self.transmission_mode == TransmissionMode.ACKNOWLEDGED
            and self.__handle_retransmission(packet_holder)
        ):
            return
        if (
            packet_holder.pdu is None
            or packet_holder.pdu_directive_type is None
            or packet_holder.pdu_directive_type != DirectiveType.FINISHED_PDU
        ):
            if self._params.check_timer is not None:
                if self._params.check_timer.timed_out():
                    self._declare_fault(ConditionCode.CHECK_LIMIT_REACHED)
            return
        finished_pdu = packet_holder.to_finished_pdu()
        self._params.finished_params = finished_pdu.finished_params
        if self.transmission_mode == TransmissionMode.ACKNOWLEDGED:
            self._prepare_finished_ack_packet(finished_pdu.condition_code)
            self.states.step = TransactionStep.SENDING_ACK_OF_FINISHED
        else:
            self.states.step = TransactionStep.NOTICE_OF_COMPLETION

    def _notice_of_completion(self):
        if self.cfg.indication_cfg.transaction_finished_indication_required:
            assert self._params.transaction_id is not None
            # This happens for unacknowledged file copy operation with no closure.
            if self._params.finished_params is None:
                self._params.finished_params = FinishedParams(
                    condition_code=ConditionCode.NO_ERROR,
                    delivery_code=DeliveryCode.DATA_COMPLETE,
                    file_status=FileStatus.FILE_STATUS_UNREPORTED,
                )
            indication_params = TransactionFinishedParams(
                transaction_id=self._params.transaction_id,
                finished_params=self._params.finished_params,
            )
            self.user.transaction_finished_indication(indication_params)
        # Transaction finished
        self._reset_internal(False)

    def _fsm_advancement_after_packets_were_sent(self):
        """Advance the internal FSM after all packets to be sent were retrieved from the handler."""
        if len(self._pdus_to_be_sent) > 0:
            raise UnretrievedPdusToBeSent(
                f"{len(self._pdus_to_be_sent)} packets left to send"
            )
        if self.states.step == TransactionStep.SENDING_METADATA:
            self.states.step = TransactionStep.SENDING_FILE_DATA
        elif self.states.step == TransactionStep.RETRANSMITTING:
            assert self._params.ack_params.step_before_retransmission is not None
            self.states.step = self._params.ack_params.step_before_retransmission
        elif self.states.step == TransactionStep.SENDING_FILE_DATA:
            self._handle_file_data_sent()
        elif self.states.step == TransactionStep.SENDING_ACK_OF_FINISHED:
            self.states.step = TransactionStep.NOTICE_OF_COMPLETION

    def _handle_eof_sent(self, cancel_eof: bool):
        if self.transmission_mode == TransmissionMode.ACKNOWLEDGED:
            self._start_positive_ack_procedure()
            return
        if cancel_eof:
            self._reset_internal(False)
            return
        if self._params.closure_requested:
            assert self._params.remote_cfg is not None
            self._params.check_timer = self.check_timer_provider.provide_check_timer(
                local_entity_id=self.cfg.local_entity_id,
                remote_entity_id=self._params.remote_cfg.entity_id,
                entity_type=EntityType.SENDING,
            )
            self.states.step = TransactionStep.WAITING_FOR_FINISHED
        else:
            self.states.step = TransactionStep.NOTICE_OF_COMPLETION

    def _handle_file_data_sent(self):
        if self._params.fp.progress == self._params.fp.file_size:
            self._params.cond_code_eof = ConditionCode.NO_ERROR
            self.states.step = TransactionStep.SENDING_EOF

    def _prepare_finished_ack_packet(self, condition_code: ConditionCode):
        ack_pdu = AckPdu(
            self._params.pdu_conf,
            DirectiveType.FINISHED_PDU,
            condition_code,
            TransactionStatus.ACTIVE,
        )
        self._add_packet_to_be_sent(ack_pdu)

    def _start_positive_ack_procedure(self):
        assert self._params.remote_cfg is not None
        self.states.step = TransactionStep.WAITING_FOR_EOF_ACK
        self._params.positive_ack_params.ack_timer = Countdown.from_seconds(
            self._params.remote_cfg.positive_ack_timer_interval_seconds
        )
        self._params.positive_ack_params.ack_counter = 0

    def _setup_transmission_params(self):
        assert self._put_req is not None
        assert self._params.remote_cfg is not None
        # Transmission mode settings in the put request override settings from the remote MIB
        trans_mode_to_set = self._put_req.trans_mode
        if trans_mode_to_set is None:
            trans_mode_to_set = self._params.remote_cfg.default_transmission_mode
        closure_req_to_set = self._put_req.closure_requested
        if closure_req_to_set is None:
            closure_req_to_set = self._params.remote_cfg.closure_requested
        # This also sets the field of the PDU configuration struct.
        self._params.transmission_mode = trans_mode_to_set
        self._params.closure_requested = closure_req_to_set

    def _add_packet_to_be_sent(self, packet: GenericPduPacket):
        self._pdus_to_be_sent.append(PduHolder(packet))
        self.states._num_packets_ready += 1

    def _prepare_progressing_file_data_pdu(self):
        """Prepare the next file data PDU, which also progresses the file copy operation.

        :return: True if a packet was prepared, False if PDU handling is done and the next steps
            in the Copy File procedure can be performed
        """
        if self._params.fp.file_size < self._params.fp.segment_len:
            read_len = self._params.fp.file_size
        else:
            if (
                self._params.fp.progress + self._params.fp.segment_len
                > self._params.fp.file_size
            ):
                read_len = self._params.fp.file_size - self._params.fp.progress
            else:
                read_len = self._params.fp.segment_len
        self._prepare_file_data_pdu(self._params.fp.progress, read_len)
        self._params.fp.progress += read_len

    def _prepare_file_data_pdu(self, offset: int, read_len: int):
        """Generic function to prepare a file data PDU. This function can also be used to
        re-transmit file data PDUs of segments which were already sent."""
        assert self._put_req is not None
        assert self._put_req.source_file is not None
        with open(self._put_req.source_file, "rb") as of:
            file_data = self.user.vfs.read_from_opened_file(of, offset, read_len)
            # TODO: Support for record continuation state not implemented yet. Segment metadata
            #       flag is therefore always set to False. Segment metadata support also omitted
            #       for now. Implementing those generically could be done in form of a callback,
            #       e.g. abstractmethod of this handler as a first way, another one being
            #       to expect the user to supply some helper class to split up a file
            fd_params = FileDataParams(
                file_data=file_data, offset=offset, segment_metadata=None
            )
            file_data_pdu = FileDataPdu(
                pdu_conf=self._params.pdu_conf, params=fd_params
            )
            self._add_packet_to_be_sent(file_data_pdu)

    def _prepare_eof_pdu(self, checksum: bytes):
        assert self._params.cond_code_eof is not None
        self._add_packet_to_be_sent(
            EofPdu(
                file_checksum=checksum,
                file_size=self._params.fp.progress,
                pdu_conf=self._params.pdu_conf,
                condition_code=self._params.cond_code_eof,
            )
        )
        if self.cfg.indication_cfg.eof_sent_indication_required:
            assert self._params.transaction_id is not None
            self.user.eof_sent_indication(self._params.transaction_id)

    def _get_next_transfer_seq_num(self):
        next_seq_num = self.seq_num_provider.get_and_increment()
        if self.seq_num_provider.max_bit_width not in [8, 16, 32]:
            raise ValueError(
                "Invalid bit width for sequence number provider, must be one of [8,"
                " 16, 32]"
            )
        self._params.pdu_conf.transaction_seq_num = ByteFieldGenerator.from_int(
            self.seq_num_provider.max_bit_width // 8, next_seq_num
        )

    def _declare_fault(self, cond: ConditionCode):
        fh = self.cfg.default_fault_handlers.get_fault_handler(cond)
        # Cache those for later, because a notice of cancellation might lead to a reset of the
        # handler.
        transaction_id = self._params.transaction_id
        progress = self._params.fp.progress
        assert transaction_id is not None
        if fh == FaultHandlerCode.NOTICE_OF_CANCELLATION:
            if not self._notice_of_cancellation(cond):
                return
        elif fh == FaultHandlerCode.NOTICE_OF_SUSPENSION:
            self._notice_of_suspension()
        elif fh == FaultHandlerCode.ABANDON_TRANSACTION:
            self._abandon_transaction()
        self.cfg.default_fault_handlers.report_fault(transaction_id, cond, progress)

    def _notice_of_cancellation(self, condition_code: ConditionCode) -> bool:
        """Returns whether the fault declaration handler can returns prematurely."""
        # CFDP standard 4.11.2.2.3: Any fault declared in the course of transferring
        # the EOF (cancel) PDU must result in abandonment of the transaction.
        if (
            self._params.cond_code_eof is not None
            and self._params.cond_code_eof != ConditionCode.NO_ERROR
        ):
            assert self._params.transaction_id is not None
            # We still call the abandonment callback to ensure the fault is logged.
            self.cfg.default_fault_handlers.abandoned_cb(
                self._params.transaction_id,
                self._params.cond_code_eof,
                self._params.fp.progress,
            )
            self._abandon_transaction()
            return False
        self._params.cond_code_eof = condition_code
        # As specified in 4.11.2.2, prepare an EOF PDU to be sent to the remote entity. Supply
        # the checksum for the file copy progress sent so far.
        self._prepare_eof_pdu(self._checksum_calculation(self._params.fp.progress))
        self._handle_eof_sent(True)
        return True

    def _notice_of_suspension(self):
        # TODO: Implement
        pass

    def _abandon_transaction(self):
        # I guess an abandoned transaction just stops whatever it is doing.. The implementation
        # for this is quite easy.
        self.reset()

    def _checksum_calculation(self, size_to_calculate: int) -> bytes:
        assert self._put_req is not None
        assert self._put_req.source_file is not None
        assert self._params.remote_cfg is not None

        return self.user.vfs.calculate_checksum(
            checksum_type=self._params.remote_cfg.crc_type,
            file_path=self._put_req.source_file,
            size_to_verify=size_to_calculate,
            segment_len=self._params.fp.segment_len,
        )
