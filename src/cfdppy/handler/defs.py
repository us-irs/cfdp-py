from __future__ import annotations

from dataclasses import dataclass


@dataclass
class _FileParamsBase:
    progress: int
    segment_len: int
    crc32: bytes | None
    metadata_only: bool
    file_size: int | None

    @classmethod
    def empty(cls) -> _FileParamsBase:
        return cls(progress=0, segment_len=0, crc32=None, file_size=None, metadata_only=False)

    def reset(self) -> None:
        self.progress = 0
        self.segment_len = 0
        self.crc32 = None
        self.file_size = None
        self.metadata_only = False
