from __future__ import annotations

import struct
from typing import Callable


def _mp4_atom(atom_type: bytes, payload: bytes) -> bytes:
    return struct.pack(">I4s", len(payload) + 8, atom_type) + payload


def build_mp4_audio(duration_seconds: float) -> bytes:
    full_box = b"\x00\x00\x00\x00"
    duration_ms = round(duration_seconds * 1_000)
    movie_header = _mp4_atom(
        b"mvhd",
        full_box + struct.pack(">IIII", 0, 0, 1_000, duration_ms) + b"\x00" * 80,
    )
    media_header = _mp4_atom(
        b"mdhd",
        full_box + struct.pack(">IIII", 0, 0, 1_000, duration_ms) + b"\x00" * 4,
    )
    handler = _mp4_atom(
        b"hdlr",
        full_box + b"\x00" * 4 + b"soun" + b"\x00" * 12 + b"Audio\x00",
    )
    audio_sample = _mp4_atom(
        b"mp4a",
        b"\x00" * 6
        + struct.pack(">H", 1)
        + b"\x00" * 8
        + struct.pack(">HHHHI", 1, 16, 0, 0, 48_000 << 16),
    )
    sample_description = _mp4_atom(
        b"stsd",
        full_box + struct.pack(">I", 1) + audio_sample,
    )
    time_to_sample = _mp4_atom(
        b"stts",
        full_box + struct.pack(">III", 1, 1, duration_ms),
    )
    sample_to_chunk = _mp4_atom(
        b"stsc",
        full_box + struct.pack(">IIII", 1, 1, 1, 1),
    )
    sample_sizes = _mp4_atom(b"stsz", full_box + struct.pack(">II", 4, 1))
    chunk_offsets = _mp4_atom(b"stco", full_box + struct.pack(">II", 1, 0))
    sample_table = _mp4_atom(
        b"stbl",
        sample_description
        + time_to_sample
        + sample_to_chunk
        + sample_sizes
        + chunk_offsets,
    )
    media_info = _mp4_atom(
        b"minf",
        _mp4_atom(b"smhd", full_box + b"\x00" * 4)
        + _mp4_atom(b"dinf", b"")
        + sample_table,
    )
    track = _mp4_atom(b"trak", _mp4_atom(b"mdia", media_header + handler + media_info))
    file_type = _mp4_atom(b"ftyp", b"isom\x00\x00\x02\x00isommp42")
    return file_type + _mp4_atom(b"moov", movie_header + track) + _mp4_atom(b"mdat", b"\x00" * 4)


def _ebml_element(element_id: bytes, payload: bytes) -> bytes:
    return element_id + bytes([0x80 + len(payload)]) + payload


def build_webm_audio(duration_seconds: float) -> bytes:
    ebml_header = bytes.fromhex(
        "1a45dfa39f4286810142f7810142f2810442f381084282847765626d4287810242858102"
    )
    info = (
        b"\x2a\xd7\xb1\x83\x0f\x42\x40"
        + b"\x44\x89\x88"
        + struct.pack(">d", duration_seconds * 1_000)
    )
    opus_head = (
        b"OpusHead"
        + bytes([1, 1])
        + struct.pack("<H", 312)
        + struct.pack("<I", 48_000)
        + struct.pack("<h", 0)
        + bytes([0])
    )
    audio = _ebml_element(b"\xb5", struct.pack(">d", 48_000.0)) + _ebml_element(
        b"\x9f",
        b"\x01",
    )
    track_entry = (
        _ebml_element(b"\xd7", b"\x01")
        + _ebml_element(b"\x73\xc5", b"\x01")
        + _ebml_element(b"\x83", b"\x02")
        + _ebml_element(b"\x86", b"A_OPUS")
        + _ebml_element(b"\x63\xa2", opus_head)
        + _ebml_element(b"\xe1", audio)
    )
    tracks = _ebml_element(
        bytes.fromhex("1654ae6b"),
        _ebml_element(b"\xae", track_entry),
    )
    return (
        ebml_header
        + bytes.fromhex("1853806701ffffffffffffff")
        + _ebml_element(bytes.fromhex("1549a966"), info)
        + tracks
    )


def _ogg_crc(payload: bytes) -> int:
    checksum = 0
    for byte in payload:
        checksum ^= byte << 24
        for _ in range(8):
            checksum = (
                ((checksum << 1) ^ 0x04C11DB7) & 0xFFFFFFFF
                if checksum & 0x80000000
                else (checksum << 1) & 0xFFFFFFFF
            )
    return checksum


def _ogg_page(*, flags: int, granule: int, sequence: int, payload: bytes) -> bytes:
    header = (
        b"OggS"
        + bytes([0, flags])
        + struct.pack("<QII", granule, 1, sequence)
        + b"\x00" * 4
        + bytes([1, len(payload)])
    )
    page = header + payload
    return page[:22] + struct.pack("<I", _ogg_crc(page)) + page[26:]


def build_ogg_audio(duration_seconds: float) -> bytes:
    pre_skip = 312
    opus_head = (
        b"OpusHead"
        + bytes([1, 1])
        + struct.pack("<H", pre_skip)
        + struct.pack("<I", 48_000)
        + struct.pack("<h", 0)
        + bytes([0])
    )
    opus_tags = b"OpusTags" + struct.pack("<II", 0, 0)
    final_granule = pre_skip + round(duration_seconds * 48_000)
    return _ogg_page(flags=2, granule=0, sequence=0, payload=opus_head) + _ogg_page(
        flags=4,
        granule=final_granule,
        sequence=1,
        payload=opus_tags,
    )


AUDIO_CONTAINERS: dict[str, tuple[str, Callable[[float], bytes]]] = {
    "audio/webm": ("turn.webm", build_webm_audio),
    "audio/mp4": ("turn.mp4", build_mp4_audio),
    "audio/ogg": ("turn.ogg", build_ogg_audio),
}
VALID_WEBM_AUDIO = build_webm_audio(1.5)
