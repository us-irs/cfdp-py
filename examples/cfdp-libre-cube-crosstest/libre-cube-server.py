#!/usr/bin/env python3
import logging

import cfdp
from cfdp.transport.udp import UdpTransport
from cfdp.filestore import NativeFileStore
from common import REMOTE_ENTITY_ID, UDP_SERVER_PORT


logging.basicConfig(level=logging.DEBUG)

udp_transport = UdpTransport(routing={"*": [("127.0.0.1", 5111)]})
udp_transport.bind("127.0.0.1", UDP_SERVER_PORT)

cfdp_entity = cfdp.CfdpEntity(
    entity_id=REMOTE_ENTITY_ID, filestore=NativeFileStore("."), transport=udp_transport
)

input("Running. Press <Enter> to stop...\n")

cfdp_entity.shutdown()
udp_transport.unbind()
