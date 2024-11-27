from __future__ import annotations  # Python 3.9 compatibility for | syntax

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from cfdppy.filestore import NativeFilestore, VirtualFilestore

if TYPE_CHECKING:
    from spacepackets.cfdp.defs import ConditionCode, TransactionId
    from spacepackets.cfdp.pdu.file_data import SegmentMetadata
    from spacepackets.cfdp.pdu.finished import FinishedParams
    from spacepackets.cfdp.tlv import MessageToUserTlv
    from spacepackets.util import UnsignedByteField

_LOGGER = logging.getLogger(__name__)


@dataclass
class TransactionParams:
    """Not wholly standard conformant here, but supplying the originating transaction ID
    makes the implementation of handling with proxy put requests easier."""

    transaction_id: TransactionId
    originating_transaction_id: TransactionId | None = None


@dataclass
class MetadataRecvParams:
    transaction_id: TransactionId
    source_id: UnsignedByteField
    file_size: int | None
    source_file_name: str | None
    dest_file_name: str | None
    msgs_to_user: list[MessageToUserTlv] | None = None


@dataclass
class TransactionFinishedParams:
    transaction_id: TransactionId
    finished_params: FinishedParams
    status_report: Any | None = None


@dataclass
class FileSegmentRecvdParams:
    """The length of the segment metadata is not supplied as an extra parameter as it can be
    simply queried with len(segment_metadata)
    """

    transaction_id: TransactionId
    offset: int
    length: int
    segment_metadata: SegmentMetadata | None


class CfdpUserBase(ABC):
    """This user base class provides the primary user interface to interact with CFDP handlers.
    It is also used to pass the Virtual Filestore (VFS) implementation to the CFDP handlers
    so the filestore operations can be mapped to the underlying filestore.

    This class is used by implementing it in a child class and then passing it to the CFDP
    handler objects. The base class provides default implementation for the user indication
    primitives specified in the CFDP standard. The user can override these implementations
    to provide custom indication handlers.
    """

    def __init__(self, vfs: VirtualFilestore | None = None):
        if vfs is None:
            vfs = NativeFilestore()
        self.vfs = vfs

    @abstractmethod
    def transaction_indication(
        self,
        transaction_indication_params: TransactionParams,
    ) -> None:
        """This indication is used to report the transaction ID to the CFDP user"""
        _LOGGER.info(f"Transaction.indication for {transaction_indication_params.transaction_id}")

    @abstractmethod
    def eof_sent_indication(self, transaction_id: TransactionId) -> None:
        _LOGGER.info(f"EOF-Sent.indication for {transaction_id}")

    @abstractmethod
    def transaction_finished_indication(self, params: TransactionFinishedParams) -> None:
        """This is the ``Transaction-Finished.Indication`` as specified in chapter 3.4.8 of the
        standard.

        The user implementation of this function could be used to keep a (failed) transaction
        history, which might be useful for the positive ACK procedures expected from a receiving
        CFDP entity."""
        _LOGGER.info(f"Transaction-Finished.indication for {params.transaction_id}. Parameters:")
        print(params)

    @abstractmethod
    def metadata_recv_indication(self, params: MetadataRecvParams) -> None:
        _LOGGER.info(f"Metadata-Recv.indication for {params.transaction_id}. Parameters:")
        print(params)

    @abstractmethod
    def file_segment_recv_indication(self, params: FileSegmentRecvdParams) -> None:
        _LOGGER.info(f"File-Segment-Recv.indication for {params.transaction_id}. Parameters:")
        print(params)

    @abstractmethod
    def report_indication(
        self,
        transaction_id: TransactionId,
        status_report: Any,  # noqa ANN401
    ) -> None:
        # TODO: p.28 of the CFDP standard specifies what information the status report parameter
        #       could contain. I think it would be better to not hardcode the type of the status
        #       report here, but something like Union[any, CfdpStatusReport] with CfdpStatusReport
        #       being an implementation which supports all three information suggestions would be
        #       nice
        pass

    @abstractmethod
    def suspended_indication(self, transaction_id: TransactionId, cond_code: ConditionCode) -> None:
        _LOGGER.info(f"Suspended.indication for {transaction_id} | Condition Code: {cond_code}")

    @abstractmethod
    def resumed_indication(self, transaction_id: TransactionId, progress: int) -> None:
        _LOGGER.info(f"Resumed.indication for {transaction_id} | Progress: {progress} bytes")

    @abstractmethod
    def fault_indication(
        self, transaction_id: TransactionId, cond_code: ConditionCode, progress: int
    ) -> None:
        """This is the ``Fault.Indication`` as specified in chapter 3.4.14 of the
        standard.

        The user implementation of this function could be used to keep a (failed) transaction
        history, which might be useful for the positive ACK procedures expected from a receiving
        CFDP entity."""
        _LOGGER.warning(
            f"Fault.indication for {transaction_id} | Condition Code: {cond_code} | "
            f"Progress: {progress} bytes"
        )

    @abstractmethod
    def abandoned_indication(
        self, transaction_id: TransactionId, cond_code: ConditionCode, progress: int
    ) -> None:
        """This is the ``Fault.Indication`` as specified in chapter 3.4.15 of the
        standard.

        The user implementation of this function could be used to keep a (failed) transaction
        history, which might be useful for the positive ACK procedures expected from a receiving
        CFDP entity."""
        _LOGGER.warning(
            f"Abandoned.indication for {transaction_id} | Condition Code: {cond_code} |"
            f" Progress: {progress} bytes"
        )

    @abstractmethod
    def eof_recv_indication(self, transaction_id: TransactionId) -> None:
        _LOGGER.info(f"EOF-Recv.indication for {transaction_id}")
