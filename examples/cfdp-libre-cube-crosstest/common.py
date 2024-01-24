from spacepackets.cfdp import TransmissionMode, ChecksumType
from spacepackets.util import ByteFieldU16
from tmtccmd.cfdp.mib import RemoteEntityCfg

SOURCE_ENTITY_ID = 2
REMOTE_ENTITY_ID = 3

UDP_TM_SERVER_PORT = 5111
UDP_SERVER_PORT = 5222

FILE_SEGMENT_SIZE = 128
MAX_PACKET_LEN = 512

REMOTE_CFG_FOR_DEST_ENTITY = RemoteEntityCfg(
    entity_id=ByteFieldU16(REMOTE_ENTITY_ID),
    max_packet_len=MAX_PACKET_LEN,
    max_file_segment_len=FILE_SEGMENT_SIZE,
    closure_requested=True,
    crc_on_transmission=False,
    default_transmission_mode=TransmissionMode.ACKNOWLEDGED,
    crc_type=ChecksumType.MODULAR,
)
