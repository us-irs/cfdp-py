from datetime import timedelta

from spacepackets.countdown import Countdown
from spacepackets.util import UnsignedByteField

from cfdppy.mib import CheckTimerProvider, EntityType


class CheckTimerProviderForTest(CheckTimerProvider):
    def __init__(
        self, timeout_dest_entity_ms: int = 50, timeout_source_entity_ms: int = 50
    ) -> None:
        self.timeout_dest_entity_ms = timeout_dest_entity_ms
        self.timeout_src_entity_ms = timeout_source_entity_ms

    def provide_check_timer(
        self,
        local_entity_id: UnsignedByteField,
        remote_entity_id: UnsignedByteField,
        entity_type: EntityType,
    ) -> Countdown:
        if entity_type == EntityType.RECEIVING:
            return Countdown(timedelta(milliseconds=self.timeout_dest_entity_ms))
        return Countdown(timedelta(milliseconds=self.timeout_src_entity_ms))
