from pathlib import Path
from typing import Optional, List, cast

from spacepackets.cfdp import (
    SegmentationControl,
    TransmissionMode,
    FaultHandlerOverrideTlv,
    FlowLabelTlv,
    MessageToUserTlv,
    FileStoreRequestTlv,
)
from spacepackets.cfdp.tlv import ProxyMessageType, ReservedCfdpMessage
from spacepackets.util import UnsignedByteField
import dataclasses


@dataclasses.dataclass
class PutRequest:
    """This is the base class modelling put request. You can create this class from the simplified
    :py:class:`tmtccmd.config.defs.CfdpParams` class with the generic
    :py:func:`tmtccmd.config.cfdp.generic_cfdp_params_to_put_request` API and/or all related specific
    APIs."""

    destination_id: UnsignedByteField
    # All the following fields are optional because a put request can also be a metadata-only
    # request
    source_file: Optional[Path]
    dest_file: Optional[Path]
    trans_mode: Optional[TransmissionMode]
    closure_requested: Optional[bool]
    seg_ctrl: Optional[SegmentationControl] = (
        SegmentationControl.NO_RECORD_BOUNDARIES_PRESERVATION
    )
    fault_handler_overrides: Optional[List[FaultHandlerOverrideTlv]] = None
    flow_label_tlv: Optional[FlowLabelTlv] = None
    msgs_to_user: Optional[List[MessageToUserTlv]] = None
    fs_requests: Optional[List[FileStoreRequestTlv]] = None

    @property
    def metadata_only(self):
        if self.source_file is None and self.dest_file is None:
            return True
        return False

    def __str__(self):
        src_file_str = "Unknown source file"
        dest_file_str = "Unknown destination file"
        if not self.metadata_only:
            src_file_str = f"Source File: {self.source_file}"
            dest_file_str = f"Destination File: {self.dest_file}"
        if self.trans_mode is not None:
            if self.trans_mode == TransmissionMode.ACKNOWLEDGED:
                trans_mode_str = "Transmission Mode: Class 2 Acknowledged"
            else:
                trans_mode_str = "Transmission Mode: Class 1 Unacknowledged"
        else:
            trans_mode_str = "Transmission Mode from MIB"
        if self.closure_requested is not None:
            if self.closure_requested:
                closure_str = "Closure requested"
            else:
                closure_str = "No closure requested"
        else:
            closure_str = "Closure Requested from MIB"
        if not self.metadata_only:
            print_str = (
                f"Destination ID {self.destination_id.value}\n\t"
                f"{src_file_str}\n\t{dest_file_str}\n\t{trans_mode_str}\n\t{closure_str}"
            )
        else:
            print_str = self.__str_for_metadata_only()
        return print_str

    def __str_for_metadata_only(self) -> str:
        print_str = (
            f"Metadata Only Put Request with Destination ID {self.destination_id.value}"
        )
        if self.msgs_to_user is not None:
            for idx, msg_to_user in enumerate(self.msgs_to_user):
                msg_to_user = cast(MessageToUserTlv, msg_to_user)
                if msg_to_user.is_reserved_cfdp_message():
                    reserved_msg = msg_to_user.to_reserved_msg_tlv()
                    assert reserved_msg is not None
                    print_str = PutRequest.__str_for_reserved_cfdp_msg(
                        idx, reserved_msg, print_str
                    )
        return print_str

    @staticmethod
    def __str_for_reserved_cfdp_msg(
        idx: int, reserved_msg: ReservedCfdpMessage, print_str: str
    ) -> str:
        if reserved_msg.is_cfdp_proxy_operation():
            proxy_msg_type = reserved_msg.get_cfdp_proxy_message_type()
            print_str += f"\nMessage to User {idx}: Proxy operation {proxy_msg_type!r}"
            if proxy_msg_type == ProxyMessageType.PUT_REQUEST:
                print_str = PutRequest.__str_for_put_req(reserved_msg, print_str)
            elif proxy_msg_type == ProxyMessageType.PUT_RESPONSE:
                print_str = PutRequest.__str_for_put_response(reserved_msg, print_str)
        elif reserved_msg.is_originating_transaction_id():
            print_str += (
                f"\nMessage to User {idx}: Originating Transaction ID "
                f"{reserved_msg.get_originating_transaction_id()}"
            )
        return print_str

    @staticmethod
    def __str_for_put_req(reserved_msg: ReservedCfdpMessage, print_str: str) -> str:
        put_request_params = reserved_msg.get_proxy_put_request_params()
        assert put_request_params is not None
        print_str += (
            f"\n\tProxy Put Dest Entity ID: {put_request_params.dest_entity_id.value}"
        )
        print_str += (
            f"\n\tSource file: {put_request_params.source_file_name.value.decode()}"
        )
        print_str += (
            f"\n\tDest file: {put_request_params.dest_file_name.value.decode()}"
        )
        return print_str

    @staticmethod
    def __str_for_put_response(
        reserved_msg: ReservedCfdpMessage, print_str: str
    ) -> str:
        put_response_params = reserved_msg.get_proxy_put_response_params()
        assert put_response_params is not None
        print_str += f"\n\tCondition Code: {put_response_params.condition_code!r}"
        print_str += f"\n\tDelivery Code: {put_response_params.delivery_code!r}"
        print_str += f"\n\tFile Status: {put_response_params.file_status!r}"
        return print_str
