from __future__ import annotations  # Python 3.9 compatibility for | syntax

import enum
import logging
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from spacepackets.cfdp import (
    ChecksumType,
    ConditionCode,
    Direction,
    EntityIdTlv,
    FaultHandlerCode,
    PduConfig,
    PduType,
    TlvType,
    TransactionId,
    TransmissionMode,
)
from spacepackets.cfdp.pdu import (
    AckPdu,
    DirectiveType,
    EofPdu,
    FileDataPdu,
    FinishedPdu,
    MetadataPdu,
    NakPdu,
)
from spacepackets.cfdp.pdu.ack import TransactionStatus
from spacepackets.cfdp.pdu.finished import DeliveryCode, FileStatus, FinishedParams
from spacepackets.cfdp.pdu.helper import GenericPduPacket, PduHolder
from spacepackets.cfdp.pdu.nak import get_max_seg_reqs_for_max_packet_size_and_pdu_cfg
from spacepackets.cfdp.tlv import MessageToUserTlv
from spacepackets.countdown import Countdown

from cfdppy.defs import CfdpState
from cfdppy.exceptions import (
    AbstractFileDirectiveBase,
    InvalidDestinationId,
    InvalidPduDirection,
    InvalidPduForDestHandler,
    NoRemoteEntityConfigFound,
    PduIgnoredForDest,
    PduIgnoredForDestReason,
    UnretrievedPdusToBeSent,
)
from cfdppy.handler.common import (
    PacketDestination,
    _PositiveAckProcedureParams,
    get_packet_destination,
)
from cfdppy.handler.defs import (
    _FileParamsBase,
)
from cfdppy.mib import (
    CheckTimerProvider,
    EntityType,
    LocalEntityConfig,
    RemoteEntityConfig,
    RemoteEntityConfigTable,
)
from cfdppy.user import (
    CfdpUserBase,
    FileSegmentRecvdParams,
    MetadataRecvParams,
    TransactionFinishedParams,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from spacepackets.util import UnsignedByteField

_LOGGER = logging.getLogger(__name__)


class CompletionDisposition(enum.Enum):
    COMPLETED = 0
    CANCELED = 1


@dataclass
class _DestFileParams(_FileParamsBase):
    file_name: Path

    @classmethod
    def empty(cls) -> _DestFileParams:
        return cls(
            progress=0,
            segment_len=0,
            crc32=b"",
            file_size=None,
            file_name=Path(),
            metadata_only=False,
        )

    def reset(self) -> None:
        super().reset()
        self.file_name = Path()


class TransactionStep(enum.Enum):
    IDLE = 0
    TRANSACTION_START = 1
    """Metadata was received, which triggered a transaction start."""

    WAITING_FOR_METADATA = 2
    """Special state which is only used for acknowledged mode. The CFDP entity is still waiting
    for a missing metadata PDU to be re-sent. Until then, all arriving file data PDUs will only
    update the internal lost segment tracker. When the EOF PDU arrives, the state will be left.
    Please note that deferred lost segment handling might also be active when this state is set."""

    RECEIVING_FILE_DATA = 3

    RECV_FILE_DATA_WITH_CHECK_LIMIT_HANDLING = 4
    """This is the check timer step as specified in chapter 4.6.3.3 b) of the standard.
    The destination handler will still check for file data PDUs which might lead to a full
    file transfer completion."""

    WAITING_FOR_MISSING_DATA = 6
    """Only relevant for acknowledged mode: Wait for lost metadata and file segments as part of
    the deferred lost segments detection procedure."""

    TRANSFER_COMPLETION = 7
    """File transfer complete. Perform checksum verification and notice of completion. Please
    note that this does not necessarily mean that the file transfer was completed successfully."""

    SENDING_FINISHED_PDU = 8
    WAITING_FOR_FINISHED_ACK = 9


@dataclass
class DestStateWrapper:
    state: CfdpState = CfdpState.IDLE
    step: TransactionStep = TransactionStep.IDLE
    transaction_id: TransactionId | None = None
    _num_packets_ready: int = 0

    @property
    def num_packets_ready(self) -> int:
        return self._num_packets_ready

    @property
    def packets_ready(self) -> bool:
        return self.num_packets_ready > 0


class LostSegmentTracker:
    def __init__(self):
        self.lost_segments: list[tuple[int, int]] = []

    @property
    def num_lost_segments(self) -> int:
        return len(self.lost_segments)

    def reset(self) -> None:
        self.lost_segments.clear()

    def sort(self) -> None:
        self.lost_segments = sorted(self.lost_segments, key=lambda t: t[0])

    def add_lost_segment(self, lost_seg: tuple[int, int]) -> None:
        self.lost_segments.append(lost_seg)

    def coalesce_lost_segments(self) -> None:
        """
        Merge overlapping or adjacent segments in self.lost_segments (List[Tuple[int, int]]).
        After the call the list is sorted and fully coalesced.
        """
        if not self.lost_segments:  # empty list → nothing to do
            return

        # 1. Sort by segment start
        self.lost_segments.sort(key=lambda s: s[0])

        merged: list[tuple[int, int]] = []
        cur_start, cur_end = self.lost_segments[0]

        for seg_start, seg_end in self.lost_segments[1:]:
            # overlap or adjacency
            if seg_start <= cur_end:
                # extend current segment
                cur_end = max(cur_end, seg_end)
            # gap found
            else:
                merged.append((cur_start, cur_end))
                cur_start, cur_end = seg_start, seg_end

        merged.append((cur_start, cur_end))  # append the last segment
        self.lost_segments = merged

    def remove_lost_segment(self, segment_to_remove: tuple[int, int]) -> bool:
        """Remove *exactly* the requested span from the lost-segment list.

        Any partial overlap with an existing segment raises ValueError. However, removing segments
        which are a subset of an existing segment are allowed.

        Returns True when the list was modified, else False.
        """
        if segment_to_remove[1] <= segment_to_remove[0]:  # empty span
            return False
        self.sort()

        r_start, r_end = segment_to_remove
        new_segments: list[tuple[int, int]] = []
        changed = False

        for s_start, s_end in self.lost_segments:
            # Case 1: no overlap - keep segment as-is
            if r_end <= s_start or r_start >= s_end:
                new_segments.append((s_start, s_end))
                continue

            # ----- partial-overlap detection -----
            # 1. removal sticks out on the left
            # 2. removal sticks out on the right
            # 3. removal is strictly inside the segment
            if r_start < s_start or r_end > s_end:
                raise ValueError("Partial overlap with an existing lost segment")

            changed = True
            # Left remainder
            if r_start > s_start:
                new_segments.append((s_start, r_start))
            # Right remainder
            if r_end < s_end:
                new_segments.append((r_end, s_end))

        self.sort()
        if changed:
            self.lost_segments = new_segments
        return changed


@dataclass
class _AckedModeParams:
    lost_seg_tracker: LostSegmentTracker = field(default=LostSegmentTracker())
    # Extra parameter: Missing metadata is not tracked inside the lost segment tracker, so we
    # need an extra parameter for this.
    metadata_missing: bool = False
    last_start_offset: int = 0
    last_end_offset: int = 0
    deferred_lost_segment_detection_active: bool = False
    procedure_timer: Countdown | None = None
    nak_activity_counter: int = 0


class _DestFieldWrapper:
    """Private wrapper class for internal use only."""

    def __init__(self):
        self.transaction_id: TransactionId | None = None
        self.remote_cfg: RemoteEntityConfig | None = None
        self.check_timer: Countdown | None = None
        self.current_check_count: int = 0
        self.closure_requested: bool = False
        self.checksum_type: ChecksumType = ChecksumType.NULL_CHECKSUM
        self.finished_params: FinishedParams = FinishedParams(
            delivery_code=DeliveryCode.DATA_INCOMPLETE,
            file_status=FileStatus.FILE_STATUS_UNREPORTED,
            condition_code=ConditionCode.NO_ERROR,
        )
        self.completion_disposition: CompletionDisposition = CompletionDisposition.COMPLETED
        self.pdu_conf: PduConfig = PduConfig.empty()
        self.fp: _DestFileParams = _DestFileParams.empty()

        self.acked_params: _AckedModeParams = _AckedModeParams()
        self.positive_ack_params: _PositiveAckProcedureParams = _PositiveAckProcedureParams()


class FsmResult:
    def __init__(self, states: DestStateWrapper):
        self.states: DestStateWrapper = states


def acknowledge_inactive_eof_pdu(eof_pdu: EofPdu, status: TransactionStatus) -> AckPdu:
    """This function can be used to fulfill chapter 4.7.2 of the CFDP standard: Every EOF PDU
    received from the CFDP sender entity MUST be acknowledged, even if the transaction ID of
    the EOF PDU is not active at the receiver entity. The
    :py:class:`spacepackets.cfdp.pdu.ack.TransactionStatus` is user provided with the following
    options:

    1. ``UNDEFINED``: The CFDP implementation does not retain a transaction history, so it might
       have been formerly active and terminated since then, or never active at all.
    2. ``TERMINATED``: The CFDP implementation does retain a transaction history and is known
       to have been active at this entity.
    3. ``UNRECOGNIZED``: The CFDP implementation does retain a
       transaction history and has never been active at this entity.

    See the :py:class:`tmtccmd.cfdp.user.CfdpUserBase` and the documentation for a possible way to
    keep a transaction history.
    """
    if status == TransactionStatus.ACTIVE:
        raise ValueError("invalid transaction status for inactive transaction")
    pdu_conf = eof_pdu.pdu_header.pdu_conf
    pdu_conf.direction = Direction.TOWARDS_SENDER
    return AckPdu(pdu_conf, DirectiveType.EOF_PDU, eof_pdu.condition_code, status)


class DestHandler:
    """This is the primary CFDP destination handler. It models the CFDP source entity, which is
    primarily responsible for receiving files sent from another CFDP entity. It performs the
    reception side of File Copy Operations.

    This handler supports both acknowledged and unacknowledged CFDP file transfers.
    The following core functions are the primary interface for interacting with the destination
    handler:

     1. :py:meth:`state_machine`: This state machine processes inserted packets while also
        generating the packets which need to be sent back to the initiator of a file copy
        operation. This call can also be used to insert packets into the destination
        handler. Please note that the destination handler can also only process Metadata, EOF and
        Prompt PDUs in addition to ACK PDUs where the acknowledged PDU is the Finished PDU.
        Right now, the handler processes one packet at a time, and each packer insertion needs
        another :py:meth:`state_machine` call.
     2. :py:meth:`get_next_packet`: Retrieve next packet to be sent back to the remote CFDP source
        entity.

    A new file transfer (Metadata PDU reception) is only be accepted if the handler is in the IDLE
    state. Furthermore, packet insertion is not allowed until all packets to send were retrieved
    after a state machine call.

    This handler is able to deal with file copy operations to directories, similarly to how the
    UNIX tool `cp` works. If the destination path is a directory instead of a regular  full path,
    the source path base file name will be appended to the destination path to form the resulting
    new full path.

    This handler also does not support concurrency out of the box but is flexible enough to be used
    in different concurrent contexts. For example, you can dynamically create new handlers and
    run them inside a thread pool, or move the newly created handler to a new thread."""

    def __init__(
        self,
        cfg: LocalEntityConfig,
        user: CfdpUserBase,
        remote_cfg_table: RemoteEntityConfigTable,
        check_timer_provider: CheckTimerProvider,
    ) -> None:
        self.cfg: LocalEntityConfig = cfg
        self.remote_cfg_table: RemoteEntityConfigTable = remote_cfg_table
        self.states: DestStateWrapper = DestStateWrapper()
        self.user: CfdpUserBase = user
        self.check_timer_provider: CheckTimerProvider = check_timer_provider
        self._params: _DestFieldWrapper = _DestFieldWrapper()
        self._pdus_to_be_sent: deque[PduHolder] = deque()

    @property
    def entity_id(self) -> UnsignedByteField:
        return self.cfg.local_entity_id

    @property
    def closure_requested(self) -> bool:
        """Returns whether a closure was requested for the current transaction. Please note that
        this variable is only valid as long as the state is not IDLE"""
        return self._params.closure_requested

    @property
    def transmission_mode(self) -> TransmissionMode | None:
        if self.states.state == CfdpState.IDLE:
            return None
        return self._params.pdu_conf.trans_mode

    @property
    def progress(self) -> int:
        return self._params.fp.progress

    @property
    def file_size(self) -> int | None:
        """The file size property which was retrieved from the Metadata PDU. This will be None
        if no transfer is active or more specifically if no Metadata PDU was received yet.
        """
        return self._params.fp.file_size

    @property
    def state(self) -> CfdpState:
        return self.states.state

    @property
    def step(self) -> TransactionStep:
        return self.states.step

    @property
    def transaction_id(self) -> TransactionId | None:
        return self._params.transaction_id

    @property
    def current_check_counter(self) -> int:
        """This is the check count used for the check limit mechanism for incomplete unacknowledged
        file transfers. A Check Limit Reached fault will be declared once this check counter
        reaches the configured check limit. More information can be found in chapter 4.6.3.3 b) of
        the standard."""
        return self._params.current_check_count

    @property
    def deferred_lost_segment_procedure_active(self) -> bool:
        return self._params.acked_params.deferred_lost_segment_detection_active

    @property
    def nak_activity_counter(self) -> int:
        return self._params.acked_params.nak_activity_counter

    @property
    def positive_ack_counter(self) -> int:
        return self._params.positive_ack_params.ack_counter

    @property
    def packets_ready(self) -> bool:
        return self.states.packets_ready

    @property
    def num_packets_ready(self) -> int:
        return self.states.num_packets_ready

    def state_machine(self, packet: GenericPduPacket | None = None) -> FsmResult:
        """This is the primary state machine which performs the CFDP procedures like PDU
        generation or assembly of received file data PDUs into a file. The packets generated by
        this finite-state machine (FSM) need to be sent by the user and can be retrieved using the
        :py:meth:`get_next_packet` method.

        This method also allows inserting packets into the state machine via the optional packet
        argument.

        Raises
        --------

        NoRemoteEntityConfigFound
            No remote configuration found for source entity ID extracted from the PDU packet.
        InvalidPduDirection
            PDU direction bit is invalid.
        InvalidDestinationId
            The PDU destination entity ID is not equal to the configured local ID.
        InvalidPduForDestHandler
            The PDU type can not be handled by the destination handler
        PduIgnoredForDest
            The PDU was ignored because it can not be handled for the current transmission mode or
            internal state.
        """
        if packet is not None:
            self._check_inserted_packet(packet)
        if self.states.state == CfdpState.IDLE:
            self.__idle_fsm(packet)
            # Calling the FSM immediately would lead to an exception, user must send any PDUs which
            # might have been generated (e.g. NAK PDUs to re-request metadata) first.
            if self.packets_ready:
                return FsmResult(self.states)
        if self.states.state == CfdpState.BUSY:
            self.__non_idle_fsm(packet)
        return FsmResult(self.states)

    def _check_inserted_packet(self, packet: GenericPduPacket) -> None:
        if packet.direction != Direction.TOWARDS_RECEIVER:
            raise InvalidPduDirection(Direction.TOWARDS_RECEIVER, packet.pdu_header.direction)
        if packet.dest_entity_id.value != self.cfg.local_entity_id.value:
            raise InvalidDestinationId(self.cfg.local_entity_id, packet.dest_entity_id)
        if self.remote_cfg_table.get_cfg(packet.source_entity_id) is None:
            raise NoRemoteEntityConfigFound(entity_id=packet.dest_entity_id)
        if get_packet_destination(packet) == PacketDestination.SOURCE_HANDLER:
            raise InvalidPduForDestHandler(packet)

        if (self.states.state == CfdpState.IDLE) and (
            packet.pdu_type == PduType.FILE_DATA
            or (
                isinstance(packet, AbstractFileDirectiveBase)
                and packet.directive_type != DirectiveType.METADATA_PDU
            )
        ):
            self._handle_first_packet_not_metadata_pdu(packet)
        if packet.pdu_type == PduType.FILE_DIRECTIVE and (
            isinstance(packet, AbstractFileDirectiveBase)
            and packet.directive_type in [DirectiveType.ACK_PDU, DirectiveType.PROMPT_PDU]
            and self.states.state == CfdpState.BUSY
            and self.transmission_mode == TransmissionMode.UNACKNOWLEDGED
        ):
            raise PduIgnoredForDest(
                PduIgnoredForDestReason.INVALID_MODE_FOR_ACKED_MODE_PACKET, packet
            )

    def get_next_packet(self) -> PduHolder | None:
        """Retrieve the next packet which should be sent to the remote CFDP source entity."""
        if len(self._pdus_to_be_sent) == 0:
            return None
        self.states._num_packets_ready -= 1
        return self._pdus_to_be_sent.popleft()

    def cancel_request(self, transaction_id: TransactionId) -> bool:
        """This function models the Cancel.request CFDP primtive and is the recommended way
        to cancel a transaction. It will cause a Notice Of Cancellation at this entity.
        Please note that the state machine might still be active because a canceled transfer
        might still require some packets to be sent to the remote sender entity.

        Returns
        --------
        True
            Current transfer was cancelled
        False
            The state machine is in the IDLE state or there is a transaction ID missmatch.
        """
        if self.states.state == CfdpState.IDLE:
            return False
        if self.states.packets_ready:
            raise UnretrievedPdusToBeSent
        if (
            self._params.transaction_id is not None
            and transaction_id == self._params.transaction_id
        ):
            self._trigger_notice_of_completion_canceled(
                ConditionCode.CANCEL_REQUEST_RECEIVED,
                EntityIdTlv(self.cfg.local_entity_id.as_bytes),
            )
            self.states.step = TransactionStep.TRANSFER_COMPLETION
            return True
        return False

    def _reset_internal(self, clear_packet_queue: bool) -> None:
        self._params = _DestFieldWrapper()
        self.states.state = CfdpState.IDLE
        self.states.step = TransactionStep.IDLE
        if clear_packet_queue:
            self._pdus_to_be_sent.clear()

    def reset(self) -> None:
        """This function is public to allow completely resetting the handler, but it is explicitly
        discouraged to do this. CFDP generally has mechanism to detect issues and errors on itself.
        """
        self._reset_internal(False)

    def __idle_fsm(self, packet: GenericPduPacket | None) -> None:
        if packet is None:
            return
        pdu_holder = PduHolder(packet)
        if pdu_holder.pdu_type == PduType.FILE_DATA:
            file_data_pdu = pdu_holder.to_file_data_pdu()
            self._start_transaction_first_packet_file_data(file_data_pdu)
        else:
            assert pdu_holder.pdu_directive_type is not None
            if pdu_holder.pdu_directive_type == DirectiveType.EOF_PDU:
                eof_pdu = pdu_holder.to_eof_pdu()
                self._start_transaction_first_packet_eof(eof_pdu)
            elif pdu_holder.pdu_directive_type == DirectiveType.METADATA_PDU:
                metadata_pdu = pdu_holder.to_metadata_pdu()
                self._start_transaction(metadata_pdu)
            else:
                raise ValueError(
                    f"unexpected configuration error: {pdu_holder.pdu} in IDLE state machine"
                )

    def __non_idle_fsm(self, packet: GenericPduPacket | None) -> None:
        self._assert_all_packets_were_sent()
        pdu_holder = PduHolder(packet)
        if (
            self.states.step
            in [
                TransactionStep.RECEIVING_FILE_DATA,
                TransactionStep.RECV_FILE_DATA_WITH_CHECK_LIMIT_HANDLING,
            ]
            and packet is not None
        ):
            self._handle_fd_or_eof_pdu(pdu_holder)
        if self.states.step == TransactionStep.WAITING_FOR_METADATA:
            self._handle_waiting_for_missing_metadata(pdu_holder)
            if self._params.acked_params.deferred_lost_segment_detection_active:
                self._deferred_lost_segment_handling()
        if self.states.step == TransactionStep.RECV_FILE_DATA_WITH_CHECK_LIMIT_HANDLING:
            self._check_limit_handling()
        if self.states.step == TransactionStep.WAITING_FOR_MISSING_DATA:
            if packet is not None and pdu_holder.pdu_type == PduType.FILE_DATA:
                self._handle_fd_pdu(pdu_holder.to_file_data_pdu())
            if self._params.acked_params.deferred_lost_segment_detection_active:
                self._deferred_lost_segment_handling()
        if self.states.step == TransactionStep.TRANSFER_COMPLETION:
            self._handle_transfer_completion()
        if self.states.step == TransactionStep.SENDING_FINISHED_PDU:
            self._prepare_finished_pdu()
            self._handle_finished_pdu_sent()
        if self.states.step == TransactionStep.WAITING_FOR_FINISHED_ACK:
            self._handle_waiting_for_finished_ack(pdu_holder)

    def _assert_all_packets_were_sent(self) -> None:
        """Advance the internal FSM after all packets to be sent were retrieved from the handler."""
        if len(self._pdus_to_be_sent) > 0:
            raise UnretrievedPdusToBeSent(f"{len(self._pdus_to_be_sent)} packets left to send")

    def _start_transaction(self, metadata_pdu: MetadataPdu) -> bool:
        if self.states.state != CfdpState.IDLE:
            return False
        self._params = _DestFieldWrapper()
        self._common_first_packet_handler(metadata_pdu)
        self._handle_metadata_packet(metadata_pdu)
        return True

    def _handle_first_packet_not_metadata_pdu(self, packet: GenericPduPacket) -> None:
        if packet.transmission_mode == TransmissionMode.UNACKNOWLEDGED:
            raise PduIgnoredForDest(PduIgnoredForDestReason.FIRST_PACKET_NOT_METADATA_PDU, packet)
        if packet.transmission_mode == TransmissionMode.ACKNOWLEDGED and (
            packet.pdu_type == PduType.FILE_DIRECTIVE
            and packet.directive_type != DirectiveType.EOF_PDU  # type: ignore
        ):
            raise PduIgnoredForDest(
                PduIgnoredForDestReason.FIRST_PACKET_IN_ACKED_MODE_NOT_METADATA_NOT_EOF_NOT_FD,
                packet,
            )

    def _start_transaction_first_packet_eof(self, eof_pdu: EofPdu) -> None:
        self._common_first_packet_not_metadata_pdu_handler(eof_pdu)
        self._handle_eof_without_previous_metadata(eof_pdu)

    # This function is only called in acknowledged mode.
    def _handle_eof_without_previous_metadata(self, eof_pdu: EofPdu) -> None:
        self._params.fp.progress = eof_pdu.file_size
        self._params.fp.file_size = eof_pdu.file_size
        self._params.acked_params.metadata_missing = True
        if self._params.fp.progress > 0:
            # Clear old list, deferred procedure for the whole file is now active.
            self._params.acked_params.lost_seg_tracker.reset()
            # Add the whole file to the lost segments map for now.
            self._params.acked_params.lost_seg_tracker.add_lost_segment((0, eof_pdu.file_size))
        if self.cfg.indication_cfg.eof_recv_indication_required:
            assert self._params.transaction_id is not None
            self.user.eof_recv_indication(self._params.transaction_id)
        if eof_pdu.condition_code != ConditionCode.NO_ERROR:
            self._handle_eof_cancel(eof_pdu)
        self._prepare_eof_ack_packet()
        self._eof_ack_pdu_done()

    def _eof_ack_pdu_done(self) -> None:
        if self._params.completion_disposition == CompletionDisposition.CANCELED:
            self.states.step = TransactionStep.TRANSFER_COMPLETION
            return
        if (
            self._params.acked_params.lost_seg_tracker.num_lost_segments > 0
            or self._params.acked_params.metadata_missing
        ):
            self._start_deferred_lost_segment_handling()
            return
        assert self._params.fp.crc32 is not None
        self.states.step = TransactionStep.TRANSFER_COMPLETION

    def _start_transaction_first_packet_file_data(self, fd_pdu: FileDataPdu) -> None:
        self._common_first_packet_not_metadata_pdu_handler(fd_pdu)
        self._handle_file_data_without_previous_metadata(fd_pdu)

    def _handle_finished_pdu_sent(self) -> None:
        if (
            self.states.state == CfdpState.BUSY
            and self.transmission_mode == TransmissionMode.ACKNOWLEDGED
        ):
            self._start_positive_ack_procedure()
            self.states.step = TransactionStep.WAITING_FOR_FINISHED_ACK
            return
        self._reset_internal(False)

    def _handle_file_data_without_previous_metadata(self, fd_pdu: FileDataPdu) -> None:
        self._params.fp.progress = fd_pdu.offset + len(fd_pdu.file_data)
        if len(fd_pdu.file_data) > 0:
            # Add this file segment (and all others which came before and might be missing
            # as well) to the lost segment list.
            self._params.acked_params.lost_seg_tracker.add_lost_segment(
                (0, self._params.fp.progress)
            )
            # This is a bit tricky: We need to set those variables to an appropriate value so
            # the removal of handled lost segments works properly. However, we can not set the
            # start offset to the regular value because we have to treat the current segment
            # like a lost segment as well.
            self._params.acked_params.last_start_offset = self._params.fp.progress
            self._params.acked_params.last_end_offset = self._params.fp.progress
        assert self._params.remote_cfg is not None
        # Re-request the metadata PDU.
        if self._params.remote_cfg.immediate_nak_mode:
            lost_segments: list[tuple[int, int]] = []
            lost_segments.append((0, 0))
            if len(fd_pdu.file_data) > 0:
                lost_segments.append((0, self._params.fp.progress))
            if len(lost_segments) > 0:
                self._add_packet_to_be_sent(
                    NakPdu(
                        self._params.pdu_conf,
                        start_of_scope=0,
                        end_of_scope=self._params.fp.progress,
                        segment_requests=lost_segments,
                    )
                )

    def _common_first_packet_not_metadata_pdu_handler(self, pdu: GenericPduPacket) -> None:
        self._params = _DestFieldWrapper()
        self._common_first_packet_handler(pdu)
        self.states.step = TransactionStep.WAITING_FOR_METADATA
        self._params.acked_params.metadata_missing = True

    def _common_first_packet_handler(self, pdu: GenericPduPacket) -> bool | None:
        if self.states.state != CfdpState.IDLE:
            return False
        self.states.state = CfdpState.BUSY
        self._params.pdu_conf = pdu.pdu_header.pdu_conf
        self._params.pdu_conf.direction = Direction.TOWARDS_SENDER
        self._params.transaction_id = TransactionId(
            source_entity_id=pdu.source_entity_id,
            transaction_seq_num=pdu.transaction_seq_num,
        )
        self.states.transaction_id = self._params.transaction_id
        self._params.remote_cfg = self.remote_cfg_table.get_cfg(pdu.source_entity_id)
        return None

    def _handle_metadata_packet(self, metadata_pdu: MetadataPdu) -> None:
        assert self._params.transaction_id is not None
        self._params.checksum_type = metadata_pdu.checksum_type
        self._params.closure_requested = metadata_pdu.closure_requested
        self._params.acked_params.metadata_missing = False
        if metadata_pdu.dest_file_name is None or metadata_pdu.source_file_name is None:
            self._params.fp.metadata_only = True
            self._params.finished_params.delivery_code = DeliveryCode.DATA_COMPLETE
        else:
            self._params.fp.file_name = Path(metadata_pdu.dest_file_name)
        self._params.fp.file_size = metadata_pdu.file_size
        # To be fully standard-compliant or at least allow the flexibility to be standard-compliant
        # in the future, we should require that a remote entity configuration exists for each CFDP
        # sender.
        if self._params.remote_cfg is None:
            _LOGGER.warning(
                f"No remote configuration found for remote ID {metadata_pdu.dest_entity_id}"
            )
            raise NoRemoteEntityConfigFound(metadata_pdu.dest_entity_id)
        if not self._params.fp.metadata_only:
            self.states.step = TransactionStep.RECEIVING_FILE_DATA
            assert metadata_pdu.source_file_name is not None
            self._init_vfs_handling(Path(metadata_pdu.source_file_name).name)
        else:
            self.states.step = TransactionStep.TRANSFER_COMPLETION
        msgs_to_user_list: None | list[MessageToUserTlv] = None
        options = metadata_pdu.options_as_tlv()
        if options is not None:
            msgs_to_user_list = []
            for tlv in options:
                if tlv.tlv_type == TlvType.MESSAGE_TO_USER:
                    msgs_to_user_list.append(MessageToUserTlv.from_tlv(tlv))
        file_size_for_indication = (
            None if metadata_pdu.source_file_name is None else metadata_pdu.file_size
        )
        params = MetadataRecvParams(
            transaction_id=self._params.transaction_id,
            file_size=file_size_for_indication,
            source_id=metadata_pdu.source_entity_id,
            dest_file_name=metadata_pdu.dest_file_name,
            source_file_name=metadata_pdu.source_file_name,
            msgs_to_user=msgs_to_user_list,
        )
        self.user.metadata_recv_indication(params)

    def _init_vfs_handling(self, source_base_name: str) -> None:
        try:
            # If the destination is a directory, append the base name to the directory
            # Example: For source path /tmp/hello.txt and dest path /tmp, build /tmp/hello.txt for
            # the destination.
            if self.user.vfs.is_directory(self._params.fp.file_name):
                self._params.fp.file_name = self._params.fp.file_name.joinpath(source_base_name)
            if self.user.vfs.file_exists(self._params.fp.file_name):
                self.user.vfs.truncate_file(self._params.fp.file_name)
            else:
                self.user.vfs.create_file(self._params.fp.file_name)
            self._params.finished_params.file_status = FileStatus.FILE_RETAINED
        except PermissionError:
            self._params.finished_params.file_status = FileStatus.DISCARDED_FILESTORE_REJECTION
            self._declare_fault(ConditionCode.FILESTORE_REJECTION)

    def _handle_fd_or_eof_pdu(self, packet_holder: PduHolder) -> None:
        """Returns whether to exit the FSM prematurely."""
        if packet_holder.pdu_type == PduType.FILE_DATA:  # type: ignore
            self._handle_fd_pdu(packet_holder.to_file_data_pdu())
        elif packet_holder.pdu_directive_type == DirectiveType.EOF_PDU:  # type: ignore
            self._handle_eof_pdu(packet_holder.to_eof_pdu())

    def _handle_waiting_for_missing_metadata(self, packet_holder: PduHolder) -> None:
        if packet_holder.pdu is None:
            return
        if packet_holder.pdu_type == PduType.FILE_DATA:
            self._handle_file_data_without_previous_metadata(packet_holder.to_file_data_pdu())
        elif packet_holder.pdu_directive_type == DirectiveType.METADATA_PDU:
            self._handle_metadata_packet(packet_holder.to_metadata_pdu())
            # Reception of missing segments resets the NAK activity parameters. See CFDP 4.6.4.7.
            if self._params.acked_params.deferred_lost_segment_detection_active:
                self._reset_nak_activity_parameters()
        elif packet_holder.pdu_directive_type == DirectiveType.EOF_PDU:  # type: ignore
            self._handle_eof_without_previous_metadata(packet_holder.to_eof_pdu())

    def _reset_nak_activity_parameters(self) -> None:
        assert self._params.acked_params.procedure_timer is not None
        self._params.acked_params.nak_activity_counter = 0
        self._params.acked_params.procedure_timer.reset()

    def _handle_waiting_for_finished_ack(self, packet_holder: PduHolder) -> None:
        """Returns False if the FSM should be called again."""
        if (
            packet_holder.pdu is None
            or packet_holder.pdu_type == PduType.FILE_DATA
            or packet_holder.pdu_directive_type != DirectiveType.ACK_PDU
        ):
            self._handle_positive_ack_procedures()
            return
        if (
            packet_holder.pdu_type == PduType.FILE_DIRECTIVE
            and packet_holder.pdu_directive_type == DirectiveType.ACK_PDU
        ):
            ack_pdu = packet_holder.to_ack_pdu()
            if ack_pdu.directive_code_of_acked_pdu != DirectiveType.FINISHED_PDU:
                _LOGGER.warning(
                    f"received ACK PDU with invalid ACKed directive code "
                    f" {ack_pdu.directive_code_of_acked_pdu}"
                )
            # We are done.
            self._reset_internal(False)

    def _handle_positive_ack_procedures(self) -> FsmResult | None:
        """Positive ACK procedures according to chapter 4.7.1 of the CFDP standard.
        Returns False if the FSM should be called again."""
        assert self._params.positive_ack_params.ack_timer is not None
        assert self._params.remote_cfg is not None
        if self._params.positive_ack_params.ack_timer.timed_out():
            if (
                self._params.positive_ack_params.ack_counter + 1
                >= self._params.remote_cfg.positive_ack_timer_expiration_limit
            ):
                self._declare_fault(ConditionCode.POSITIVE_ACK_LIMIT_REACHED)
                # This is a bit of a hack: We want the transfer completion and the corresponding
                # Finished PDU to be re-sent in the same FSM cycle. However, the call
                # order in the FSM prevents this from happening, so we just call the state machine
                # again manually.
                if self._params.completion_disposition == CompletionDisposition.CANCELED:
                    return self.state_machine()
            self._params.positive_ack_params.ack_timer.reset()
            self._params.positive_ack_params.ack_counter += 1
            self._prepare_finished_pdu()
            return None
        return None

    def _handle_fd_pdu(self, file_data_pdu: FileDataPdu) -> None:
        data = file_data_pdu.file_data
        offset = file_data_pdu.offset
        if self.cfg.indication_cfg.file_segment_recvd_indication_required:
            file_segment_indic_params = FileSegmentRecvdParams(
                transaction_id=self._params.transaction_id,  # type: ignore
                length=len(file_data_pdu.file_data),
                offset=offset,
                segment_metadata=file_data_pdu.segment_metadata,
            )
            self.user.file_segment_recv_indication(file_segment_indic_params)
        try:
            next_expected_progress = offset + len(data)
            self.user.vfs.write_data(self._params.fp.file_name, data, offset)
            self._params.finished_params.file_status = FileStatus.FILE_RETAINED

            if (
                self._params.fp.file_size is not None
                and (offset + len(file_data_pdu.file_data) > self._params.fp.file_size)
                and (
                    self._declare_fault(ConditionCode.FILE_SIZE_ERROR)
                    != FaultHandlerCode.IGNORE_ERROR
                )
            ):
                # CFDP 4.6.1.2.7 c): If the sum of the FD PDU offset and segment size exceeds
                # the file size indicated in the first previously received EOF (No Error) PDU, if
                # any, then a File Size Error fault shall be declared.
                return
            # Ensure that the progress value is always incremented
            self._params.fp.progress = max(next_expected_progress, self._params.fp.progress)
            if self.transmission_mode == TransmissionMode.ACKNOWLEDGED:
                self._lost_segment_handling(offset, len(data))
        except FileNotFoundError:
            if self._params.finished_params.file_status != FileStatus.FILE_RETAINED:
                self._params.finished_params.file_status = FileStatus.DISCARDED_FILESTORE_REJECTION
                _ = self._declare_fault(ConditionCode.FILESTORE_REJECTION)
        except PermissionError:
            if self._params.finished_params.file_status != FileStatus.FILE_RETAINED:
                self._params.finished_params.file_status = FileStatus.DISCARDED_FILESTORE_REJECTION
                _ = self._declare_fault(ConditionCode.FILESTORE_REJECTION)

    def _handle_transfer_completion(self) -> None:
        self._notice_of_completion()
        if (
            self.transmission_mode == TransmissionMode.UNACKNOWLEDGED
            and self._params.closure_requested
        ) or self.transmission_mode == TransmissionMode.ACKNOWLEDGED:
            self.states.step = TransactionStep.SENDING_FINISHED_PDU
        else:
            self._reset_internal(False)

    def _lost_segment_handling(self, offset: int, data_len: int) -> None:
        """Lost segment detection: 4.6.4.3.1 a) and b) are covered by this code. c) is covered
        by dedicated code which is run when the EOF PDU is handled."""
        if offset > self._params.acked_params.last_end_offset:
            lost_segment = (self._params.acked_params.last_end_offset, offset)
            self._params.acked_params.lost_seg_tracker.add_lost_segment(lost_segment)
            assert self._params.remote_cfg is not None
            if self._params.remote_cfg.immediate_nak_mode:
                self._add_packet_to_be_sent(
                    NakPdu(
                        self._params.pdu_conf,
                        0,
                        offset + data_len,
                        segment_requests=[lost_segment],
                    )
                )
        if offset >= self._params.acked_params.last_end_offset:
            self._params.acked_params.last_start_offset = offset
            self._params.acked_params.last_end_offset = offset + data_len
        if offset + data_len <= self._params.acked_params.last_start_offset:
            # Might be a re-requested FD PDU.
            removed = self._params.acked_params.lost_seg_tracker.remove_lost_segment(
                (offset, offset + data_len)
            )
            # Reception of missing segments resets the NAK activity parameters.
            # See CFDP 4.6.4.7.
            if removed and self._params.acked_params.deferred_lost_segment_detection_active:
                self._reset_nak_activity_parameters()

    @staticmethod
    def _iter_segment_requests(
        lost_segments: Iterator[tuple[int, int]],
        metadata_missing: bool,
        max_per_pdu: int,
    ) -> Iterator[list[tuple[int, int]]]:
        """
        Yield lists of segment requests that fit into a single NAK PDU.

        If metadata is missing we prepend a (0, 0) request exactly once.
        """
        batch: list[tuple[int, int]] = []
        if metadata_missing:
            batch.append((0, 0))

        for start, end in lost_segments:
            batch.append((start, end))
            if len(batch) == max_per_pdu:
                yield batch
                batch = []
        # final partial batch
        if batch:
            yield batch

    def _deferred_lost_segment_handling(self) -> None:
        assert self._params.remote_cfg is not None
        assert self._params.fp.file_size is not None
        if (
            self._params.acked_params.lost_seg_tracker.num_lost_segments == 0
            and not self._params.acked_params.metadata_missing
        ):
            # We are done and have received everything.
            assert self._params.fp.crc32 is not None
            if self._checksum_verify(self._params.fp.progress, self._params.fp.crc32):
                self._params.finished_params.delivery_code = DeliveryCode.DATA_COMPLETE
                self._params.finished_params.condition_code = ConditionCode.NO_ERROR
            else:
                self._params.finished_params.delivery_code = DeliveryCode.DATA_INCOMPLETE
                self._params.finished_params.condition_code = ConditionCode.FILE_CHECKSUM_FAILURE
            self.states.step = TransactionStep.TRANSFER_COMPLETION
            self._params.acked_params.deferred_lost_segment_detection_active = False
            return
        first_nak_issuance = self._params.acked_params.procedure_timer is None
        # This is the case if this is the first issuance of NAK PDUs
        # A timer needs to be instantiated, but we do not increment the activity counter yet.
        if first_nak_issuance:
            self._params.acked_params.procedure_timer = Countdown.from_seconds(
                self._params.remote_cfg.nak_timer_interval_seconds
            )
        elif self._params.acked_params.procedure_timer.busy():  # pyright: ignore
            # There were or there was a previous NAK sequence(s). Wait for timeout before issuing
            # a new NAK sequence.
            return
        if (
            not first_nak_issuance
            and self._params.acked_params.nak_activity_counter + 1
            == self._params.remote_cfg.nak_timer_expiration_limit
        ):
            self._params.finished_params.delivery_code = DeliveryCode.DATA_INCOMPLETE
            _ = self._declare_fault(ConditionCode.NAK_LIMIT_REACHED)
            return

        # This is not the first NAK issuance and the timer expired.
        max_segments_in_one_pdu = get_max_seg_reqs_for_max_packet_size_and_pdu_cfg(
            self._params.remote_cfg.max_packet_len, self._params.pdu_conf
        )

        batches = list(
            self._iter_segment_requests(
                iter(self._params.acked_params.lost_seg_tracker.lost_segments),
                self._params.acked_params.metadata_missing,
                max_segments_in_one_pdu,
            )
        )

        for idx, batch in enumerate(batches):
            # first batch → clamp start to 0
            start = 0 if idx == 0 else batch[0][0]
            # last batch → clamp end to file size
            end = self._params.fp.file_size if idx == len(batches) - 1 else batch[-1][1]
            self._add_packet_to_be_sent(NakPdu(self._params.pdu_conf, start, end, batch))

        if not first_nak_issuance:
            self._params.acked_params.nak_activity_counter += 1
            self._params.acked_params.procedure_timer.reset()  # pyright: ignore

    def _handle_eof_pdu(self, eof_pdu: EofPdu) -> None:
        self._params.fp.crc32 = eof_pdu.file_checksum
        self._params.fp.file_size = eof_pdu.file_size
        if self.cfg.indication_cfg.eof_recv_indication_required:
            assert self._params.transaction_id is not None
            self.user.eof_recv_indication(self._params.transaction_id)
        if eof_pdu.condition_code == ConditionCode.NO_ERROR:
            regular_completion = self._handle_eof_no_error(eof_pdu.file_checksum)
            if not regular_completion:
                return
        else:
            self._handle_eof_cancel(eof_pdu)
        if self.transmission_mode == TransmissionMode.UNACKNOWLEDGED:
            self.states.step = TransactionStep.TRANSFER_COMPLETION
        elif self.transmission_mode == TransmissionMode.ACKNOWLEDGED:
            self._prepare_eof_ack_packet()
            self._eof_ack_pdu_done()

    def _handle_eof_cancel(self, eof_pdu: EofPdu) -> None:
        assert self._params.remote_cfg is not None
        # This is an EOF (Cancel), perform Cancel Response Procedures according to chapter
        # 4.6.6 of the standard. Set remote ID as fault location.
        self._trigger_notice_of_completion_canceled(
            eof_pdu.condition_code,
            EntityIdTlv(self._params.remote_cfg.entity_id.as_bytes),
        )
        # Store this as progress for the checksum calculation as well.
        self._params.fp.progress = eof_pdu.file_size
        if self._params.acked_params.metadata_missing:
            self._params.finished_params.delivery_code = DeliveryCode.DATA_INCOMPLETE
            return
        if self._params.fp.file_size == 0:
            # Empty file, no file data PDU.
            self._params.finished_params.delivery_code = DeliveryCode.DATA_COMPLETE
            return
        if self._checksum_verify(self._params.fp.progress, eof_pdu.file_checksum):
            self._params.finished_params.delivery_code = DeliveryCode.DATA_COMPLETE
            return
        self._params.finished_params.delivery_code = DeliveryCode.DATA_INCOMPLETE

    def _handle_eof_no_error(self, crc32: bytes) -> bool:
        """Returns whether the transfer can be completed regularly."""
        assert self._params.fp.file_size is not None
        # CFDP 4.6.1.2.9: Declare file size error if progress exceeds file size
        if self._params.fp.progress > self._params.fp.file_size:
            if self._declare_fault(ConditionCode.FILE_SIZE_ERROR) != FaultHandlerCode.IGNORE_ERROR:
                return False
        elif (
            self._params.fp.progress < self._params.fp.file_size
        ) and self.transmission_mode == TransmissionMode.ACKNOWLEDGED:
            # CFDP 4.6.4.3.1: The end offset of the last received file segment and the file
            # size as stated in the EOF PDU is not the same, so we need to add that segment to
            # the lost segments for the deferred lost segment detection procedure.
            self._params.acked_params.lost_seg_tracker.add_lost_segment(
                (self._params.fp.progress, self._params.fp.file_size)  # type: ignore
            )
        if self.transmission_mode == TransmissionMode.UNACKNOWLEDGED and not self._checksum_verify(
            self._params.fp.progress, crc32
        ):
            self._start_check_limit_handling()
            return False
        self._params.finished_params.delivery_code = DeliveryCode.DATA_COMPLETE
        self._params.finished_params.condition_code = ConditionCode.NO_ERROR
        return True

    def _start_deferred_lost_segment_handling(self) -> None:
        assert self._params.fp.file_size is not None
        if self._params.acked_params.metadata_missing:
            self.states.step = TransactionStep.WAITING_FOR_METADATA
        else:
            self.states.step = TransactionStep.WAITING_FOR_MISSING_DATA
        self._params.acked_params.deferred_lost_segment_detection_active = True
        self._params.acked_params.lost_seg_tracker.coalesce_lost_segments()
        self._params.acked_params.last_start_offset = self._params.fp.file_size
        self._params.acked_params.last_end_offset = self._params.fp.file_size
        self._deferred_lost_segment_handling()

    def _prepare_eof_ack_packet(self) -> None:
        ack_pdu = AckPdu(
            self._params.pdu_conf,
            DirectiveType.EOF_PDU,
            self._params.finished_params.condition_code,
            TransactionStatus.ACTIVE,
        )
        self._add_packet_to_be_sent(ack_pdu)

    def _checksum_verify(self, verify_len: int, expected_crc32: bytes) -> bool:
        if (
            self._params.checksum_type == ChecksumType.NULL_CHECKSUM
            or self._params.fp.metadata_only
        ):
            return True
        crc32 = self.user.vfs.calculate_checksum(
            self._params.checksum_type,
            self._params.fp.file_name,
            verify_len,
        )
        if crc32 == expected_crc32:
            return True
        self._declare_fault(ConditionCode.FILE_CHECKSUM_FAILURE)
        return False

    def _trigger_notice_of_completion_canceled(
        self, condition_code: ConditionCode, fault_location: EntityIdTlv
    ) -> None:
        self._params.completion_disposition = CompletionDisposition.CANCELED
        self._params.finished_params.condition_code = condition_code
        self._params.finished_params.fault_location = fault_location

    def _start_check_limit_handling(self) -> None:
        self.states.step = TransactionStep.RECV_FILE_DATA_WITH_CHECK_LIMIT_HANDLING
        assert self._params.remote_cfg is not None
        self._params.check_timer = self.check_timer_provider.provide_check_timer(
            self.cfg.local_entity_id,
            self._params.remote_cfg.entity_id,
            EntityType.RECEIVING,
        )
        self._params.current_check_count = 0

    def _notice_of_completion(self) -> None:
        assert self._params.transaction_id is not None
        if self._params.completion_disposition == CompletionDisposition.COMPLETED:
            # TODO: Execute any filestore requests
            pass
        elif self._params.completion_disposition == CompletionDisposition.CANCELED:
            assert self._params.remote_cfg is not None
            if (
                self._params.remote_cfg.disposition_on_cancellation
                and self._params.finished_params.delivery_code == DeliveryCode.DATA_INCOMPLETE
            ):
                self.user.vfs.delete_file(self._params.fp.file_name)
                self._params.finished_params.file_status = FileStatus.DISCARDED_DELIBERATELY
        if self.cfg.indication_cfg.transaction_finished_indication_required:
            finished_indic_params = TransactionFinishedParams(
                transaction_id=self._params.transaction_id,
                finished_params=self._params.finished_params,
                status_report=None,
            )
            self.user.transaction_finished_indication(finished_indic_params)

    def _prepare_finished_pdu(self) -> None:
        # TODO: Fault location handling. Set remote entity ID for file copy
        # operations cancelled with an EOF (Cancel) PDU, and the local ID for file
        # copy operations cancelled with the local API.
        finished_pdu = FinishedPdu(
            params=self._params.finished_params,
            # The configuration was cached when the first metadata arrived
            pdu_conf=self._params.pdu_conf,
        )
        self._add_packet_to_be_sent(finished_pdu)

    def _start_positive_ack_procedure(self) -> None:
        assert self._params.remote_cfg is not None
        self._params.positive_ack_params.ack_timer = Countdown.from_seconds(
            self._params.remote_cfg.positive_ack_timer_interval_seconds
        )
        self._params.positive_ack_params.ack_counter = 0

    def _add_packet_to_be_sent(self, packet: GenericPduPacket) -> None:
        self._pdus_to_be_sent.append(PduHolder(packet))
        self.states._num_packets_ready += 1

    def _check_limit_handling(self) -> None:
        assert self._params.check_timer is not None
        assert self._params.remote_cfg is not None
        assert self._params.fp.crc32 is not None
        if self._params.check_timer.timed_out():
            if self._checksum_verify(self._params.fp.progress, self._params.fp.crc32):
                self._params.finished_params.delivery_code = DeliveryCode.DATA_COMPLETE
                self._params.finished_params.condition_code = ConditionCode.NO_ERROR
                self.states.step = TransactionStep.TRANSFER_COMPLETION
                return
            if self._params.current_check_count + 1 >= self._params.remote_cfg.check_limit:
                self._declare_fault(ConditionCode.CHECK_LIMIT_REACHED)
            else:
                self._params.current_check_count += 1
                self._params.check_timer.reset()

    def _declare_fault(self, cond: ConditionCode) -> FaultHandlerCode:
        fh = self.cfg.default_fault_handlers.get_fault_handler(cond)
        transaction_id = self._params.transaction_id
        progress = self._params.fp.progress
        assert transaction_id is not None
        if fh is None:
            raise ValueError(f"invalid condition code {cond!r} for fault declaration")
        if fh == FaultHandlerCode.NOTICE_OF_CANCELLATION:
            self._notice_of_cancellation(cond)
        elif fh == FaultHandlerCode.NOTICE_OF_SUSPENSION:
            self._notice_of_suspension()
        elif fh == FaultHandlerCode.ABANDON_TRANSACTION:
            self._abandon_transaction()
        self.cfg.default_fault_handlers.report_fault(transaction_id, cond, progress)
        return fh

    def _notice_of_cancellation(self, condition_code: ConditionCode) -> None:
        self.states.step = TransactionStep.TRANSFER_COMPLETION
        self._params.finished_params.condition_code = condition_code
        self._params.completion_disposition = CompletionDisposition.CANCELED

    def _notice_of_suspension(self) -> None:
        # TODO: Implement
        pass

    def _abandon_transaction(self) -> None:
        # I guess an abandoned transaction just stops whatever it is doing. The implementation
        # for this is quite easy.
        self.reset()
