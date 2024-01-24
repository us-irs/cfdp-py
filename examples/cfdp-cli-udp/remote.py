#!/usr/bin/env python3
"""This component simulates the remote component."""
import argparse
import logging
from logging import basicConfig
from multiprocessing import Queue

from common import (
    INDICATION_CFG,
    REMOTE_ENTITY_ID,
    REMOTE_PORT,
    REMOTE_CFG_OF_LOCAL_ENTITY,
    CfdpFaultHandler,
    CfdpUser,
    CustomCheckTimerProvider,
    DestEntityHandler,
    SourceEntityHandler,
    UdpServer,
)

from tmtccmd.cfdp.handler.dest import DestHandler
from tmtccmd.cfdp.handler.source import SourceHandler
from tmtccmd.cfdp.mib import (
    LocalEntityCfg,
    RemoteEntityCfgTable,
)
from tmtccmd.util.seqcnt import SeqCountProvider

_LOGGER = logging.getLogger(__name__)


BASE_STR_SRC = "REMOTE SRC"
BASE_STR_DEST = "REMOTE DEST"

# This queue is used to send put requests.
PUT_REQ_QUEUE = Queue()
# All telecommands which should go to the source handler should be put into this queue by
# the UDP server.
SOURCE_ENTITY_QUEUE = Queue()
# All telecommands which should go to the destination handler should be put into this queue by
# the UDP server.
DEST_ENTITY_QUEUE = Queue()
# All telemetry which should be sent to the local entity is put into this queue and will then
# be sent by the UDP server.
TM_QUEUE = Queue()


def main():
    parser = argparse.ArgumentParser(prog="CFDP Remote Entity Application")
    parser.add_argument("-v", "--verbose", action="count", default=0)
    args = parser.parse_args()
    logging_level = logging.INFO
    if args.verbose >= 1:
        logging_level = logging.DEBUG
    basicConfig(level=logging_level)

    src_fault_handler = CfdpFaultHandler(BASE_STR_SRC)
    # 16 bit sequence count for transactions.
    src_seq_count_provider = SeqCountProvider(16)
    src_user = CfdpUser(BASE_STR_SRC, PUT_REQ_QUEUE)
    remote_cfg_table = RemoteEntityCfgTable()
    remote_cfg_table.add_config(REMOTE_CFG_OF_LOCAL_ENTITY)
    check_timer_provider = CustomCheckTimerProvider()
    source_handler = SourceHandler(
        cfg=LocalEntityCfg(REMOTE_ENTITY_ID, INDICATION_CFG, src_fault_handler),
        user=src_user,
        remote_cfg_table=remote_cfg_table,
        check_timer_provider=check_timer_provider,
        seq_num_provider=src_seq_count_provider,
    )
    source_entity_task = SourceEntityHandler(
        BASE_STR_SRC,
        logging_level,
        source_handler,
        PUT_REQ_QUEUE,
        SOURCE_ENTITY_QUEUE,
        TM_QUEUE,
    )

    # Enable all indications.
    dest_fault_handler = CfdpFaultHandler(BASE_STR_DEST)
    dest_user = CfdpUser(BASE_STR_DEST, PUT_REQ_QUEUE)
    dest_handler = DestHandler(
        cfg=LocalEntityCfg(REMOTE_ENTITY_ID, INDICATION_CFG, dest_fault_handler),
        user=dest_user,
        remote_cfg_table=remote_cfg_table,
        check_timer_provider=check_timer_provider,
    )
    dest_entity_task = DestEntityHandler(
        BASE_STR_DEST,
        logging_level,
        dest_handler,
        DEST_ENTITY_QUEUE,
        TM_QUEUE,
    )

    # Address Any to accept CFDP packets from other address than localhost.
    local_addr = "0.0.0.0"
    udp_server = UdpServer(
        sleep_time=0.1,
        addr=(local_addr, REMOTE_PORT),
        # No explicit remote address, remote server only responds to requests.
        explicit_remote_addr=None,
        tx_queue=TM_QUEUE,
        source_entity_rx_queue=SOURCE_ENTITY_QUEUE,
        dest_entity_rx_queue=DEST_ENTITY_QUEUE,
    )

    source_entity_task.start()
    dest_entity_task.start()
    udp_server.start()
    source_entity_task.join()
    dest_entity_task.join()
    udp_server.join()


if __name__ == "__main__":
    main()
