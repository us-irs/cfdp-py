from __future__ import annotations

import enum


class CfdpRequestType(enum.Enum):
    PUT = 0
    REPORT = 1
    CANCEL = 2
    SUSPEND = 3
    RESUME = 4


class CfdpIndication(enum.Enum):
    TRANSACTION = 0
    EOF = 1
    FINISHED = 2
    METADATA = 3
    FILE_SEGMENT_RECV = 4
    REPORT = 5
    SUSPENDED = 6
    RESUMED = 7
    FAULT = 8
    ABANDONED = 9
    EOF_RECV = 10


class CfdpState(enum.Enum):
    IDLE = 0
    BUSY = 1
    SUSPENDED = 2


class SequenceNumberOverflow(Exception):
    pass
