#!/usr/bin/env python3
"""This example shows a end-to-end transfer of a small file using the CFDP high level
components provided by the tmtccmd package."""
import argparse
import logging
import os
import select
import socket
import threading
import time
from dataclasses import dataclass
from datetime import timedelta
from logging import basicConfig
from pathlib import Path
from queue import Empty
from typing import Any

from common import REMOTE_ENTITY_ID as REMOTE_ENTITY_ID_RAW
from common import SOURCE_ENTITY_ID as SOURCE_ENTITY_ID_RAW
from common import UDP_SERVER_PORT, UDP_TM_SERVER_PORT, REMOTE_CFG_FOR_DEST_ENTITY
from spacepackets.cfdp import (
    ConditionCode,
    TransactionId,
    TransmissionMode,
)
from spacepackets.cfdp.pdu.helper import PduFactory
from spacepackets.util import ByteFieldU16, UnsignedByteField

from tmtccmd.cfdp import CfdpState
from tmtccmd.cfdp.exceptions import InvalidSourceId
from tmtccmd.cfdp.handler import SourceHandler
from tmtccmd.cfdp.mib import (
    CheckTimerProvider,
    DefaultFaultHandlerBase,
    EntityType,
    IndicationCfg,
    LocalEntityCfg,
    RemoteEntityCfgTable,
)
from tmtccmd.cfdp.request import PutRequest
from tmtccmd.cfdp.user import (
    CfdpUserBase,
    FileSegmentRecvdParams,
    MetadataRecvParams,
    TransactionFinishedParams,
)
from tmtccmd.util.countdown import Countdown
from tmtccmd.util.seqcnt import SeqCountProvider

SOURCE_ENTITY_ID = ByteFieldU16(SOURCE_ENTITY_ID_RAW)
DEST_ENTITY_ID = ByteFieldU16(REMOTE_ENTITY_ID_RAW)

FILE_CONTENT = "Hello World!"
SOURCE_FILE = Path("files/local.txt")
DEST_FILE = Path("files/remote.txt")


@dataclass
class TransferParams:
    transmission_mode: TransmissionMode
    verbose_level: int
    no_closure: bool


_LOGGER = logging.getLogger()


class CfdpFaultHandler(DefaultFaultHandlerBase):
    def notice_of_suspension_cb(
        self, transaction_id: TransactionId, cond: ConditionCode, progress: int
    ):
        _LOGGER.warning(
            f"Received Notice of Suspension for transaction {transaction_id!r} with condition "
            f"code {cond!r}. Progress: {progress}"
        )

    def notice_of_cancellation_cb(
        self, transaction_id: TransactionId, cond: ConditionCode, progress: int
    ):
        _LOGGER.warning(
            f"Received Notice of Cancellation for transaction {transaction_id!r} with condition "
            f"code {cond!r}. Progress: {progress}"
        )

    def abandoned_cb(
        self, transaction_id: TransactionId, cond: ConditionCode, progress: int
    ):
        _LOGGER.warning(
            f"Received Abanadoned Fault for transaction {transaction_id!r} with condition "
            f"code {cond!r}. Progress: {progress}"
        )

    def ignore_cb(
        self, transaction_id: TransactionId, cond: ConditionCode, progress: int
    ):
        _LOGGER.warning(
            f"Ignored Fault for transaction {transaction_id!r} with condition "
            f"code {cond!r}. Progress: {progress}"
        )


class CfdpUser(CfdpUserBase):
    def __init__(self, base_str: str):
        self.base_str = base_str
        super().__init__()

    def transaction_indication(self, transaction_id: TransactionId):
        """This indication is used to report the transaction ID to the CFDP user"""
        _LOGGER.info(f"{self.base_str}: Transaction.indication for {transaction_id}")

    def eof_sent_indication(self, transaction_id: TransactionId):
        _LOGGER.info(f"{self.base_str}: EOF-Sent.indication for {transaction_id}")

    def transaction_finished_indication(self, params: TransactionFinishedParams):
        _LOGGER.info(
            f"{self.base_str}: Transaction-Finished.indication for {params.transaction_id}."
        )

    def metadata_recv_indication(self, params: MetadataRecvParams):
        _LOGGER.info(
            f"{self.base_str}: Metadata-Recv.indication for {params.transaction_id}."
        )

    def file_segment_recv_indication(self, params: FileSegmentRecvdParams):
        _LOGGER.info(
            f"{self.base_str}: File-Segment-Recv.indication for {params.transaction_id}."
        )

    def report_indication(self, transaction_id: TransactionId, status_report: Any):
        # TODO: p.28 of the CFDP standard specifies what information the status report parameter
        #       could contain. I think it would be better to not hardcode the type of the status
        #       report here, but something like Union[any, CfdpStatusReport] with CfdpStatusReport
        #       being an implementation which supports all three information suggestions would be
        #       nice
        pass

    def suspended_indication(
        self, transaction_id: TransactionId, cond_code: ConditionCode
    ):
        _LOGGER.info(
            f"{self.base_str}: Suspended.indication for {transaction_id} | Condition Code: {cond_code}"
        )

    def resumed_indication(self, transaction_id: TransactionId, progress: int):
        _LOGGER.info(
            f"{self.base_str}: Resumed.indication for {transaction_id} | Progress: {progress} bytes"
        )

    def fault_indication(
        self, transaction_id: TransactionId, cond_code: ConditionCode, progress: int
    ):
        _LOGGER.info(
            f"{self.base_str}: Fault.indication for {transaction_id} | Condition Code: {cond_code} | "
            f"Progress: {progress} bytes"
        )

    def abandoned_indication(
        self, transaction_id: TransactionId, cond_code: ConditionCode, progress: int
    ):
        _LOGGER.info(
            f"{self.base_str}: Abandoned.indication for {transaction_id} | Condition Code: {cond_code} |"
            f" Progress: {progress} bytes"
        )

    def eof_recv_indication(self, transaction_id: TransactionId):
        _LOGGER.info(f"{self.base_str}: EOF-Recv.indication for {transaction_id}")


class CustomCheckTimerProvider(CheckTimerProvider):
    def provide_check_timer(
        self,
        local_entity_id: UnsignedByteField,
        remote_entity_id: UnsignedByteField,
        entity_type: EntityType,
    ) -> Countdown:
        return Countdown(timedelta(seconds=5.0))


def main():
    help_txt = (
        "This mini application cross tests the tmtccmd CFDP support and the LibreCube CFDP "
        "implementation "
    )
    parser = argparse.ArgumentParser(
        prog="CFDP Libre Cube Cross Testing Application", description=help_txt
    )
    parser.add_argument("-t", "--type", choices=["nak", "ack"], default="ack")
    parser.add_argument("-v", "--verbose", action="count", default=0)
    parser.add_argument("--no-closure", action="store_true", default=False)
    args = parser.parse_args()
    if args.type == "nak":
        transmission_mode = TransmissionMode.UNACKNOWLEDGED
    elif args.type == "ack":
        transmission_mode = TransmissionMode.ACKNOWLEDGED
    else:
        transmission_mode = None
    if args.verbose == 0:
        logging_level = logging.INFO
    elif args.verbose >= 1:
        logging_level = logging.DEBUG
    transfer_params = TransferParams(transmission_mode, args.verbose, args.no_closure)
    basicConfig(level=logging_level)

    # If the test files already exist, delete them.
    if SOURCE_FILE.exists():
        os.remove(SOURCE_FILE)
    with open(SOURCE_FILE, "w") as file:
        file.write(FILE_CONTENT)

    remote_cfg_table = RemoteEntityCfgTable([REMOTE_CFG_FOR_DEST_ENTITY])

    # Enable all indications.
    src_indication_cfg = IndicationCfg()
    src_fault_handler = CfdpFaultHandler()
    src_entity_cfg = LocalEntityCfg(
        SOURCE_ENTITY_ID, src_indication_cfg, src_fault_handler
    )
    # 16 bit sequence count for transactions.
    src_seq_count_provider = SeqCountProvider(16)
    src_user = CfdpUser("SRC ENTITY")
    check_timer_provider = CustomCheckTimerProvider()
    source_handler = SourceHandler(
        cfg=src_entity_cfg,
        seq_num_provider=src_seq_count_provider,
        remote_cfg_table=remote_cfg_table,
        user=src_user,
        check_timer_provider=check_timer_provider,
    )

    udp_client = socket.socket(family=socket.AF_INET, type=socket.SOCK_DGRAM)
    udp_tm_server = socket.socket(family=socket.AF_INET, type=socket.SOCK_DGRAM)
    udp_tm_server.bind(("127.0.0.1", UDP_TM_SERVER_PORT))
    udp_tm_server.setblocking(False)

    # Spawn a new thread and move the source handler there. This is scalable: If multiple number
    # of concurrent file operations are required, a new thread with a new source handler can
    # be spawned for each one.
    source_thread = threading.Thread(
        target=source_entity_handler,
        args=[udp_client, udp_tm_server, transfer_params, source_handler],
    )

    source_thread.start()
    source_thread.join()

    src_file_content = None
    with open(SOURCE_FILE) as file:
        src_file_content = file.read()
    dest_file_content = None
    if not DEST_FILE.exists():
        raise ValueError("Destination file does not exist!")
    with open(DEST_FILE) as file:
        dest_file_content = file.read()
    assert src_file_content == dest_file_content
    _LOGGER.info("Source and destination file content are equal. Deleting files.")
    if SOURCE_FILE.exists():
        os.remove(SOURCE_FILE)
    if DEST_FILE.exists():
        os.remove(DEST_FILE)
    _LOGGER.info("Done.")


def source_entity_handler(
    tc_client: socket.socket,
    tm_client: socket.socket,
    transfer_params: TransferParams,
    source_handler: SourceHandler,
):
    # This put request could in principle also be sent from something like a front end application.
    put_request = PutRequest(
        destination_id=DEST_ENTITY_ID,
        source_file=SOURCE_FILE,
        dest_file=DEST_FILE,
        trans_mode=transfer_params.transmission_mode,
        closure_requested=not transfer_params.no_closure,
    )
    no_packet_received = False
    print(f"SRC HANDLER: Inserting Put Request: {put_request}")
    with open(SOURCE_FILE) as file:
        file_content = file.read()
        print(f"File content of source file {SOURCE_FILE}: {file_content}")
    source_handler.put_request(put_request)
    while True:
        try:
            ready = select.select([tm_client], [], [], 0)
            if ready[0]:
                data, _ = tm_client.recvfrom(4096)
                packet = PduFactory.from_raw(data)
                try:
                    source_handler.insert_packet(packet)
                except InvalidSourceId:
                    _LOGGER.warning(f"invalid source ID in packet {packet}")
                no_packet_received = False
            else:
                no_packet_received = True
        except Empty:
            no_packet_received = True
        fsm_result = source_handler.state_machine()
        no_packet_sent = False
        if fsm_result.states.num_packets_ready > 0:
            while fsm_result.states.num_packets_ready > 0:
                next_pdu_wrapper = source_handler.get_next_packet()
                assert next_pdu_wrapper is not None
                if transfer_params.verbose_level >= 1:
                    _LOGGER.debug(f"SRC Handler: Sending packet {next_pdu_wrapper.pdu}")
                # Send all packets which need to be sent.
                tc_client.sendto(
                    next_pdu_wrapper.pdu.pack(), ("127.0.0.1", UDP_SERVER_PORT)
                )
            no_packet_sent = False
        else:
            no_packet_sent = True
        # If there is no work to do, put the thread to sleep.
        if no_packet_received and no_packet_sent:
            time.sleep(0.5)
        # Transaction done
        if fsm_result.states.state == CfdpState.IDLE:
            _LOGGER.info("Source entity operation done.")
            break


if __name__ == "__main__":
    main()
