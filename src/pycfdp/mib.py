import abc
import enum
from abc import ABC
from dataclasses import dataclass
from typing import Optional, Dict, Sequence

from spacepackets.cfdp.defs import (
    FaultHandlerCode,
    ChecksumType,
    TransmissionMode,
    CFDP_VERSION_2,
    ConditionCode,
    TransactionId,
)
from spacepackets.util import UnsignedByteField
from spacepackets.countdown import Countdown


class DefaultFaultHandlerBase(ABC):
    """This base class provides a way to implement the fault handling procedures as specified
    in chapter 4.8 of the CFDP standard.

    It is passed into the CFDP handlers as part of the local entity configuration and provides
    a way to specify custom user error handlers.

    It does so by mapping each applicable CFDP :py:class:`ConditionCode` to a fault handler which
    is denoted by the four :py:class:`spacepackets.cfdp.defs.FaultHandlerCode` s. This code is used
    to dispatch to a user-provided callback function:

     1. ``IGNORE_ERROR`` -> :py:meth:`ignore_cb`
     2. ``NOTICE_OF_CANCELLATION`` -> :py:meth:`notice_of_cancellation_cb`
     3. ``NOTICE_OF_SUSPENSION`` -> :py:meth:`notice_of_suspension_cb`
     4. ``ABANDON_TRANSACTION`` -> :py:meth:`abandon_transaction_cb`

    For each error reported by :py:meth:`report_fault`, the appropriate fault handler callback
    will be called. The user provides the callbacks by providing a custom class which implements
    this base class and all abstract fault handler callbacks. This allows logging of the errors
    as specified in chapter 4.8.3.

    Some note on the provided default settings:

    - Checksum failures will be ignored by default. This is because for unacknowledged transfers,
      cancelling the transfer immediately would interfere with the check limit mechanism specified
      in chapter 4.6.3.3.
    - Unsupported checksum types will also be ignored by default. Even if the checksum type is
      not supported the file transfer might still have worked properly.

    """

    def __init__(self):
        # The initial default handle will be to cancel the transaction
        self._handler_dict: Dict[ConditionCode, FaultHandlerCode] = {
            ConditionCode.CANCEL_REQUEST_RECEIVED: FaultHandlerCode.NOTICE_OF_CANCELLATION,
            ConditionCode.POSITIVE_ACK_LIMIT_REACHED: FaultHandlerCode.NOTICE_OF_CANCELLATION,
            ConditionCode.KEEP_ALIVE_LIMIT_REACHED: FaultHandlerCode.NOTICE_OF_CANCELLATION,
            ConditionCode.INVALID_TRANSMISSION_MODE: FaultHandlerCode.NOTICE_OF_CANCELLATION,
            ConditionCode.FILE_CHECKSUM_FAILURE: FaultHandlerCode.IGNORE_ERROR,
            ConditionCode.FILE_SIZE_ERROR: FaultHandlerCode.NOTICE_OF_CANCELLATION,
            ConditionCode.FILESTORE_REJECTION: FaultHandlerCode.NOTICE_OF_CANCELLATION,
            ConditionCode.NAK_LIMIT_REACHED: FaultHandlerCode.NOTICE_OF_CANCELLATION,
            ConditionCode.INACTIVITY_DETECTED: FaultHandlerCode.NOTICE_OF_CANCELLATION,
            ConditionCode.CHECK_LIMIT_REACHED: FaultHandlerCode.NOTICE_OF_CANCELLATION,
            ConditionCode.UNSUPPORTED_CHECKSUM_TYPE: FaultHandlerCode.IGNORE_ERROR,
        }

    def get_fault_handler(self, condition: ConditionCode) -> Optional[FaultHandlerCode]:
        return self._handler_dict.get(condition)

    def set_handler(self, condition: ConditionCode, handler: FaultHandlerCode):
        """
        Raises
        -------

        ValueError
            Invalid condition code which is not applicable for fault handling procedures.
        """
        if condition not in self._handler_dict:
            raise ValueError(
                f"condition code {condition!r} not applicable for fault handling procedures"
            )
        self._handler_dict.update({condition: handler})

    def report_fault(
        self, transaction_id: TransactionId, condition: ConditionCode, progress: int
    ):
        """
        Raises
        -------

        ValueError
            Invalid condition code which is not applicable for fault handling procedures.
        """
        if condition not in self._handler_dict:
            raise ValueError(
                f"condition code {condition!r} not applicable for fault handling procedures"
            )
        fh_code = self._handler_dict.get(condition)
        if fh_code == FaultHandlerCode.NOTICE_OF_CANCELLATION:
            self.notice_of_cancellation_cb(transaction_id, condition, progress)
        elif fh_code == FaultHandlerCode.NOTICE_OF_SUSPENSION:
            self.notice_of_suspension_cb(transaction_id, condition, progress)
        elif fh_code == FaultHandlerCode.IGNORE_ERROR:
            self.ignore_cb(transaction_id, condition, progress)
        elif fh_code == FaultHandlerCode.ABANDON_TRANSACTION:
            self.abandoned_cb(transaction_id, condition, progress)

    @abc.abstractmethod
    def notice_of_suspension_cb(
        self, transaction_id: TransactionId, cond: ConditionCode, progress: int
    ):
        pass

    @abc.abstractmethod
    def notice_of_cancellation_cb(
        self, transaction_id: TransactionId, cond: ConditionCode, progress: int
    ):
        pass

    @abc.abstractmethod
    def abandoned_cb(
        self, transaction_id: TransactionId, cond: ConditionCode, progress: int
    ):
        pass

    @abc.abstractmethod
    def ignore_cb(
        self, transaction_id: TransactionId, cond: ConditionCode, progress: int
    ):
        pass


class EntityType(enum.IntEnum):
    SENDING = 0
    RECEIVING = 1


class CheckTimerProvider(ABC):
    @abc.abstractmethod
    def provide_check_timer(
        self,
        local_entity_id: UnsignedByteField,
        remote_entity_id: UnsignedByteField,
        entity_type: EntityType,
    ) -> Countdown:
        pass


@dataclass
class IndicationCfg:
    eof_sent_indication_required: bool = True
    eof_recv_indication_required: bool = True
    file_segment_recvd_indication_required: bool = True
    transaction_finished_indication_required: bool = True
    suspended_indication_required: bool = True
    resumed_indication_required: bool = True


@dataclass
class LocalEntityCfg:
    """This models the remote entity configuration information as specified in chapter 8.2
    of the CFDP standard."""

    local_entity_id: UnsignedByteField
    indication_cfg: IndicationCfg
    default_fault_handlers: DefaultFaultHandlerBase


@dataclass
class RemoteEntityCfg:
    """This models the remote entity configuration information as specified in chapter 8.3
    of the CFDP standard.

    Some of the fields which were not considered necessary for the Python implementation
    were omitted. Some other fields which are not contained inside the standard but are considered
    necessary for the Python implementation are included.

    **Notes on Positive Acknowledgment Procedures**

    The ``positive_ack_timer_interval_seconds`` and ``positive_ack_timer_expiration_limit`` will
    be used for positive acknowledgement procedures as specified in CFDP chapter 4.7. The sending
    entity will start the timer for any PDUs where an acknowledgment is required (e.g. EOF PDU).
    Once the expected ACK response has not been received for that interval, as counter will be
    incremented and the timer will be reset. Once the counter exceeds the
    ``positive_ack_timer_expiration_limit``, a Positive ACK Limit Reached fault will be declared.

    **Notes on Deferred Lost Segment Procedures**

    This procedure will be active if an EOF (No Error) PDU is received in acknowledged mode. After
    issuing the NAK sequence which has the whole file scope, a timer will be started. The timer is
    reset when missing segments or missing metadata is received. The timer will be deactivated if
    all missing data is received. If the timer expires, a new NAK sequence will be issued and a
    counter will be incremented, which can lead to a NAK Limit Reached fault being declared.

    Parameters
    -----------

    entity_id
        The ID of the remote entity.
    max_packet_len
        This determines of all PDUs generated for that remote entity in addition to the
        `max_file_segment_len` attribute which also determines the size of file data PDUs.
    max_file_segment_len
        The maximum file segment length which determines the maximum size
        of file data PDUs in addition to the `max_packet_len` attribute. If this field is set
        to None, the maximum file segment length will be derived from the maximum packet length.
        If this has some value which is smaller than the segment value derived from
        `max_packet_len`, this value will be picked.
    closure_requested
        If the closure requested field is not supplied as part of the Put Request, it will be
        determined from this field in the remote configuration.
    crc_on_transmission
        If the CRC option is not supplied as part of the Put Request, it will be
        determined from this field in the remote configuration.
    default_transmission_mode
        If the transmission mode is not supplied as part of the Put Request, it will be
        determined from this field in the remote configuration.
    disposition_on_cancellation
        Determines whether an incomplete received file is discard on transaction cancellation.
        Defaults to False.
    crc_type
        Default checksum type used to calculate for all file transmissions to this remote entity.
    check_limit
        this timer determines the expiry period for incrementing a check counter after an EOF PDU
        is received for an incomplete file transfer. This allows out-of-order reception of file
        data PDUs and EOF PDUs. Also see 4.6.3.3 of the CFDP standard. Defaults to 2, so the
        check limit timer may expire twice.
    positive_ack_timer_interval_seconds
        See the notes on the Positive Acknowledgment Procedures inside the class documentation.
        Expected as floating point seconds. Defaults to 10 seconds.
    positive_ack_timer_expiration_limit
        See the notes on the Positive Acknowledgment Procedures inside the class documentation.
        Defaults to 2, so the timer may expire twice.
    immediate_nak_mode:
        Specifies whether a NAK sequence should be issued immediately when a file data gap or
        lost metadata is detected in the acknowledged mode. Defaults to True.
    nak_timer_interval_seconds:
        See the notes on the Deferred Lost Segment Procedure inside the class documentation.
        Expected as floating point seconds. Defaults to 10 seconds.
    nak_timer_expiration_limit:
        See the notes on the Deferred Lost Segment Procedure inside the class documentation.
        Defaults to 2, so the timer may expire two times.

    """

    entity_id: UnsignedByteField
    max_file_segment_len: Optional[int]
    max_packet_len: int
    closure_requested: bool
    crc_on_transmission: bool
    default_transmission_mode: TransmissionMode
    crc_type: ChecksumType
    positive_ack_timer_interval_seconds: float = 10.0
    positive_ack_timer_expiration_limit: int = 2
    check_limit: int = 2
    disposition_on_cancellation: bool = False
    immediate_nak_mode: bool = True
    nak_timer_interval_seconds: float = 10.0
    nak_timer_expiration_limit: int = 2
    # NOTE: Only this version is supported
    cfdp_version: int = CFDP_VERSION_2


class RemoteEntityCfgTable:
    """Thin abstraction for a dictionary containing remote configurations with the remote entity ID
    being used as a key."""

    def __init__(self, init_cfgs: Optional[Sequence[RemoteEntityCfg]] = None):
        self._remote_entity_dict = dict()
        if init_cfgs is not None:
            self.add_configs(init_cfgs)

    def add_config(self, cfg: RemoteEntityCfg) -> bool:
        if cfg.entity_id in self._remote_entity_dict:
            return False
        self._remote_entity_dict.update({cfg.entity_id.value: cfg})
        return True

    def add_configs(self, cfgs: Sequence[RemoteEntityCfg]):
        for cfg in cfgs:
            if cfg.entity_id in self._remote_entity_dict:
                continue
            self._remote_entity_dict.update({cfg.entity_id.value: cfg})

    def get_cfg(self, remote_entity_id: UnsignedByteField) -> Optional[RemoteEntityCfg]:
        return self._remote_entity_dict.get(remote_entity_id.value)
