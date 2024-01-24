#!/usr/bin/env python3
"""This example shows a end-to-end transfer of a small file using the CFDP high level
components provided by the tmtccmd package."""
import argparse
import copy
import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import timedelta
from logging import basicConfig
from multiprocessing import Queue
from pathlib import Path
from queue import Empty
from typing import Any

from spacepackets.cfdp import GenericPduPacket, TransactionId
from spacepackets.cfdp.defs import ChecksumType, ConditionCode, TransmissionMode
from spacepackets.cfdp.pdu import AbstractFileDirectiveBase
from spacepackets.util import ByteFieldU16, UnsignedByteField

from tmtccmd.cfdp import CfdpState
from tmtccmd.cfdp.handler.dest import DestHandler
from tmtccmd.cfdp.handler.source import SourceHandler
from tmtccmd.cfdp.mib import (
    CheckTimerProvider,
    DefaultFaultHandlerBase,
    EntityType,
    IndicationCfg,
    LocalEntityCfg,
    RemoteEntityCfg,
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

SOURCE_ENTITY_ID = ByteFieldU16(1)
DEST_ENTITY_ID = ByteFieldU16(2)

FILE_CONTENT = "Hello World!\n"
FILE_SEGMENT_SIZE = len(FILE_CONTENT)
MAX_PACKET_LEN = 512
SOURCE_FILE = Path("/tmp/cfdp-test-source.txt")
DEST_FILE = Path("/tmp/cfdp-test-dest.txt")


@dataclass
class TransferParams:
    transmission_mode: TransmissionMode
    verbose_level: int
    no_closure: bool


_LOGGER = logging.getLogger()


REMOTE_CFG_FOR_SOURCE_ENTITY = RemoteEntityCfg(
    entity_id=SOURCE_ENTITY_ID,
    max_packet_len=MAX_PACKET_LEN,
    max_file_segment_len=FILE_SEGMENT_SIZE,
    closure_requested=True,
    crc_on_transmission=False,
    default_transmission_mode=TransmissionMode.ACKNOWLEDGED,
    crc_type=ChecksumType.CRC_32,
)
REMOTE_CFG_FOR_DEST_ENTITY = copy.copy(REMOTE_CFG_FOR_SOURCE_ENTITY)
REMOTE_CFG_FOR_DEST_ENTITY.entity_id = DEST_ENTITY_ID

# These queues will be used to exchange PDUs between threads.
SOURCE_TO_DEST_QUEUE = Queue()
DEST_TO_SOURCE_QUEUE = Queue()


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
        "This mini application shows the source and destination entity handlers in action. "
        "You can configure the transmission mode with the -t argument, which defaults to the "
        "acknowledged mode. It is also possible to increase the verbosity level to print all "
        "packets being exchanged."
    )
    parser = argparse.ArgumentParser(
        prog="CFDP File Copy Example Application", description=help_txt
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
    if DEST_FILE.exists():
        os.remove(DEST_FILE)
    with open(SOURCE_FILE, "w") as file:
        file.write(FILE_CONTENT)

    remote_cfg_table = RemoteEntityCfgTable()
    remote_cfg_table.add_config(REMOTE_CFG_FOR_SOURCE_ENTITY)
    remote_cfg_table.add_config(REMOTE_CFG_FOR_DEST_ENTITY)

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
    # Spawn a new thread and move the source handler there. This is scalable: If multiple number
    # of concurrent file operations are required, a new thread with a new source handler can
    # be spawned for each one.
    source_thread = threading.Thread(
        target=source_entity_handler, args=[transfer_params, source_handler]
    )

    # Enable all indications.
    dest_indication_cfg = IndicationCfg()
    dest_fault_handler = CfdpFaultHandler()
    dest_entity_cfg = LocalEntityCfg(
        DEST_ENTITY_ID, dest_indication_cfg, dest_fault_handler
    )
    dest_user = CfdpUser("DEST ENTITY")
    dest_handler = DestHandler(
        cfg=dest_entity_cfg,
        user=dest_user,
        remote_cfg_table=remote_cfg_table,
        check_timer_provider=check_timer_provider,
    )
    # Spawn a new thread and move the destination handler there. This is scalable. One example
    # approach could be to keep a dictionary of active file copy operations, where the transaction
    # ID is the key. If a new Metadata PDU with a new transaction ID is detected, a new
    # destination handler in a new thread could be spawned to handle the file copy operation.
    dest_thread = threading.Thread(
        target=dest_entity_handler, args=[transfer_params, dest_handler]
    )

    source_thread.start()
    dest_thread.start()
    source_thread.join()
    dest_thread.join()

    src_file_content = None
    with open(SOURCE_FILE) as file:
        src_file_content = file.read()
    dest_file_content = None
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
    transfer_params: TransferParams, source_handler: SourceHandler
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
    assert source_handler.put_request(put_request)
    while True:
        try:
            # We are getting the packets from a Queue here, they could for example also be polled
            # from a network.
            packet: AbstractFileDirectiveBase = DEST_TO_SOURCE_QUEUE.get(False)
            source_handler.insert_packet(packet)
            no_packet_received = False
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
                SOURCE_TO_DEST_QUEUE.put(next_pdu_wrapper.pdu)
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


def dest_entity_handler(transfer_params: TransferParams, dest_handler: DestHandler):
    first_packet = True
    no_packet_received = False
    while True:
        try:
            packet: GenericPduPacket = SOURCE_TO_DEST_QUEUE.get(False)
            dest_handler.insert_packet(packet)
            no_packet_received = False
            if first_packet:
                first_packet = False
        except Empty:
            no_packet_received = True
        fsm_result = dest_handler.state_machine()
        if fsm_result.states.num_packets_ready > 0:
            no_packet_sent = False
            while fsm_result.states.num_packets_ready > 0:
                next_pdu_wrapper = dest_handler.get_next_packet()
                assert next_pdu_wrapper is not None
                if transfer_params.verbose_level >= 1:
                    _LOGGER.debug(
                        f"DEST Handler: Sending packet {next_pdu_wrapper.pdu}"
                    )
                DEST_TO_SOURCE_QUEUE.put(next_pdu_wrapper.pdu)
        else:
            no_packet_sent = True
        # If there is no work to do, put the thread to sleep.
        if no_packet_received and no_packet_sent:
            time.sleep(0.5)
        # Transaction done
        if not first_packet and fsm_result.states.state == CfdpState.IDLE:
            _LOGGER.info("Destination entity operation done.")
            break
    with open(DEST_FILE) as file:
        file_content = file.read()
        print(f"File content of destination file {DEST_FILE}: {file_content}")


if __name__ == "__main__":
    main()
