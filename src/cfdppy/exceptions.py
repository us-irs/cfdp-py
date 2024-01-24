import enum
from pathlib import Path
from spacepackets.util import UnsignedByteField
from spacepackets.cfdp.defs import ChecksumType, Direction
from spacepackets.cfdp import GenericPduPacket
from spacepackets.cfdp.pdu import AbstractFileDirectiveBase


class NoRemoteEntityCfgFound(Exception):
    def __init__(self, entity_id: UnsignedByteField, *args, **kwargs):
        super().__init__(args, kwargs)
        self.remote_entity_id = entity_id

    def __str__(self):
        return f"No remote entity found for entity ID {self.remote_entity_id}"


class FsmNotCalledAfterPacketInsertion(Exception):
    def __init__(self):
        super().__init__("Call the state machine before inserting the next packet")


class SourceFileDoesNotExist(Exception):
    def __init__(self, file: Path):
        self.file = file
        super().__init__(f"Source file {self.file} does not exist")


class ChecksumNotImplemented(Exception):
    def __init__(self, checksum_type: ChecksumType):
        self.checksum_type = checksum_type
        super().__init__(f"{self.checksum_type} not implemented")


class UnretrievedPdusToBeSent(Exception):
    pass


class InvalidNakPdu(Exception):
    pass


class InvalidPduDirection(Exception):
    def __init__(self, expected_dir: Direction, found_dir: Direction):
        self.expected_dir = expected_dir
        self.found_dir = found_dir
        super().__init__(
            f"Expected direction {self.expected_dir!r}, got {self.found_dir!r}"
        )


class InvalidSourceId(Exception):
    """Invalid source entity ID. This is not necessarily the sender of a packet but actually the
    entity that started a transaction, or the entity which is transferring a file"""

    def __init__(
        self,
        expected_src_id: UnsignedByteField,
        found_src_id: UnsignedByteField,
    ):
        self.expected_src_id = expected_src_id
        self.found_src_id = found_src_id
        super().__init__(
            f"expected source {self.expected_src_id}, got {self.found_src_id}"
        )


class InvalidDestinationId(Exception):
    """Invalid destination entity ID. This is not necessarily the receiver of a packet but actually
    the recipient of a file, or the entity receiving file data and metadata PDUs"""

    def __init__(
        self,
        expected_dest_id: UnsignedByteField,
        found_dest_id: UnsignedByteField,
    ):
        self.expected_dest_id = expected_dest_id
        self.found_dest_id = found_dest_id
        super().__init__(
            f"expected destination {self.expected_dest_id}, got {self.found_dest_id}"
        )


class InvalidTransactionSeqNum(Exception):
    def __init__(self, expected: UnsignedByteField, received: UnsignedByteField):
        self.expected = expected
        self.received = received
        super().__init__(
            f"expected sequence number {expected}, reiceved {self.received}"
        )


class BusyError(Exception):
    pass


class InvalidPduForSourceHandler(Exception):
    def __init__(self, packet: AbstractFileDirectiveBase):
        self.packet = packet
        super().__init__(f"Invalid packet {self.packet} for source handler")


class PduIgnoredForSourceReason(enum.IntEnum):
    # The received PDU can only be used for acknowledged mode.
    ACK_MODE_PACKET_INVALID_MODE = 0
    # Received a Finished PDU, but source handler is currently not expecting one.
    NOT_WAITING_FOR_FINISHED_PDU = 1
    # Received a ACK PDU, but source handler is currently not expecting one.
    NOT_WAITING_FOR_ACK = 2


class PduIgnoredForSource(Exception):
    def __init__(
        self,
        reason: PduIgnoredForSourceReason,
        ignored_packet: AbstractFileDirectiveBase,
    ):
        self.ignored_packet = ignored_packet
        self.reason = reason
        super().__init__(f"ignored PDU packet at source handler: {reason!r}")


class InvalidPduForDestHandler(Exception):
    def __init__(self, packet: GenericPduPacket):
        self.packet = packet
        super().__init__(f"Invalid packet {self.packet} for source handler")


class PduIgnoredForDestReason(enum.IntEnum):
    FIRST_PACKET_NOT_METADATA_PDU = 0
    """First packet received was not a metadata PDU for the unacknowledged mode."""
    INVALID_MODE_FOR_ACKED_MODE_PACKET = 1
    """The received PDU can only be handled in acknowledged mode."""
    FIRST_PACKET_IN_ACKED_MODE_NOT_METADATA_NOT_EOF_NOT_FD = 2
    """For the acknowledged mode, the first packet that was received with
    no metadata received previously was not a File Data PDU or EOF PDU."""


class PduIgnoredForDest(Exception):
    def __init__(
        self, reason: PduIgnoredForDestReason, ignored_packet: GenericPduPacket
    ):
        self.ignored_packet = ignored_packet
        self.reason = reason
        super().__init__(f"ignored PDU packet at destination handler: {reason!r}")
