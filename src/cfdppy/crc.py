import struct
from pathlib import Path


def calc_modular_checksum(file_path: Path) -> bytes:
    """Calculates the modular checksum for a file in one go."""
    checksum = 0

    with open(file_path, "rb") as file:
        while True:
            data = file.read(4)
            if not data:
                break
            checksum += int.from_bytes(
                data.ljust(4, b"\0"), byteorder="big", signed=False
            )

    checksum %= 2**32
    return struct.pack("!I", checksum)
