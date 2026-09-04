"""CRC-16 as implemented by the HiSilicon boot ROM and burn agent.

The algorithm is transcribed from ``calc_crc16()`` in U-Boot's
``common/download_process.c`` (HiSilicon, GPL-2.0). It is CRC-16/CCITT with
the polynomial 0x1021, a zero seed, and a two-byte zero flush at the end:

.. code-block:: c

   for (i = 0; i < length; i++)
       crc16 = ((crc16 << 8) | packet[i]) ^ crc16_table[(crc16 >> 8) & 0xFF];
   for (i = 0; i < 2; i++)
       crc16 = ((crc16 << 8) | 0)         ^ crc16_table[(crc16 >> 8) & 0xFF];

Note this is *not* plain CRC-16/CCITT-FALSE: the message byte is mixed into
the low half of the register rather than XORed into the high half. Reusing a
stock CRC routine here produces frames the device silently NAKs, so the
transcription is deliberate.
"""

from __future__ import annotations

POLYNOMIAL = 0x1021


def _build_table() -> tuple[int, ...]:
    table = []
    for index in range(256):
        register = index << 8
        for _ in range(8):
            if register & 0x8000:
                register = ((register << 1) ^ POLYNOMIAL) & 0xFFFF
            else:
                register = (register << 1) & 0xFFFF
        table.append(register)
    return tuple(table)


CRC_TABLE = _build_table()


def crc16(data: bytes) -> int:
    """Return the 16-bit checksum the device expects for ``data``."""
    register = 0
    for byte in data:
        register = (((register << 8) | byte) & 0xFFFF) ^ CRC_TABLE[(register >> 8) & 0xFF]
    for _ in range(2):
        register = ((register << 8) & 0xFFFF) ^ CRC_TABLE[(register >> 8) & 0xFF]
    return register & 0xFFFF


def append_crc(data: bytes) -> bytes:
    """Append the big-endian checksum, the byte order used by every frame."""
    return data + crc16(data).to_bytes(2, "big")


def check_crc(frame: bytes) -> bool:
    """Verify a frame whose last two bytes are its big-endian checksum."""
    if len(frame) < 3:
        return False
    return crc16(frame[:-2]) == int.from_bytes(frame[-2:], "big")
