from __future__ import annotations

import argparse
from contextlib import contextmanager
import ctypes
from dataclasses import dataclass
from datetime import datetime, timezone
import errno
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import secrets
import shutil
import sqlite3
import stat
import struct
import sys
from tempfile import TemporaryDirectory
from typing import BinaryIO, Iterator
import zipfile

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.db import (
    expected_migration_versions,
    get_live_read_only_connection,
    migration_state_is_current,
)
from backend.app.settings import Settings, get_settings


BACKUP_FORMAT = "interface-v2-operational-backup"
BACKUP_FORMAT_VERSION = 1
DATABASE_MEMBER = "app.db"
METADATA_MEMBER = "backup_metadata.json"
MANIFEST_MEMBER = "manifest.json"
AUDIO_PREFIX = "audio/"
JSON_MEMBER_LIMIT = 1024 * 1024
ARCHIVE_OVERHEAD_ALLOWANCE = 64 * 1024 * 1024
STREAM_CHUNK_BYTES = 1024 * 1024


class BackupError(RuntimeError):
    pass


class BackupVerificationError(BackupError):
    pass


class DestinationParentChangedError(OSError):
    pass


class SourceIdentityChangedError(OSError):
    pass


@dataclass(frozen=True)
class BackupLimits:
    max_member_bytes: int = 17_179_869_184
    max_total_uncompressed_bytes: int = 274_877_906_944
    max_compression_ratio: float = 200.0
    max_central_directory_bytes: int = 67_108_864
    max_members: int = 100_000

    def __post_init__(self) -> None:
        if (
            self.max_member_bytes < 1
            or self.max_total_uncompressed_bytes < 1
            or self.max_compression_ratio < 1
            or self.max_central_directory_bytes < 1
            or self.max_members < 1
        ):
            raise ValueError("backup_limits_must_be_positive")

    @classmethod
    def from_settings(cls, settings: Settings) -> BackupLimits:
        return cls(
            max_member_bytes=settings.backup_max_member_bytes,
            max_total_uncompressed_bytes=(
                settings.backup_max_total_uncompressed_bytes
            ),
            max_compression_ratio=settings.backup_max_compression_ratio,
            max_central_directory_bytes=(
                settings.backup_max_central_directory_bytes
            ),
            max_members=settings.backup_max_members,
        )


@dataclass(frozen=True)
class BackupVerification:
    archive_path: Path
    audio_members: tuple[str, ...]
    member_count: int


@dataclass(frozen=True)
class ManifestEntry:
    path: str
    size: int
    sha256: str

    def as_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "size": self.size,
            "sha256": self.sha256,
        }


@dataclass(frozen=True)
class AudioEvidence:
    member_name: str
    sha256: str


@dataclass(frozen=True)
class OpenDestinationParent:
    path: Path
    descriptor: int
    device: int
    inode: int
    ancestry_descriptors: tuple[int, ...]
    ancestry_identities: tuple[FileIdentity, ...]


@dataclass(frozen=True)
class PrivateStagingContainer:
    name: str
    descriptor: int
    device: int
    inode: int


FileIdentity = tuple[int, int]


class ByteBudget:
    def __init__(self, limit: int, *, error_code: str) -> None:
        self._remaining = limit
        self._error_code = error_code

    def consume(self, byte_count: int) -> None:
        self._remaining -= byte_count
        if self._remaining < 0:
            raise BackupVerificationError(self._error_code)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create or verify a complete operational backup.",
    )
    action = parser.add_mutually_exclusive_group()
    action.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Backup archive path outside DATA_DIR.",
    )
    action.add_argument(
        "--verify",
        type=Path,
        metavar="ARCHIVE",
        help="Verify an existing backup without writing restored data.",
    )
    return parser.parse_args()


def default_output_path(settings: Settings, *, now: datetime | None = None) -> Path:
    generated_at = now or datetime.now(timezone.utc)
    timestamp = generated_at.strftime("%Y%m%d-%H%M%S")
    return settings.data_dir.resolve().parent / "backups" / f"backup-{timestamp}.zip"


def _is_within(path: Path, root: Path) -> bool:
    return path.resolve().is_relative_to(root.resolve())


def _validate_external_path(path: Path, *, live_data_dir: Path) -> None:
    if _is_within(path, live_data_dir):
        raise BackupError("backup_destination_must_be_outside_live_data")


def _safe_member_name(value: object) -> str:
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        raise BackupVerificationError("unsafe_archive_member_path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        raise BackupVerificationError("unsafe_archive_member_path")
    if ":" in path.parts[0]:
        raise BackupVerificationError("unsafe_archive_member_path")
    return path.as_posix()


def _valid_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _stream_copy(
    source: BinaryIO,
    target: BinaryIO,
    *,
    max_bytes: int,
    budget: ByteBudget,
    expected_size: int | None = None,
) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    for chunk in iter(lambda: source.read(STREAM_CHUNK_BYTES), b""):
        size += len(chunk)
        if size > max_bytes:
            raise BackupVerificationError("archive_member_too_large")
        budget.consume(len(chunk))
        target.write(chunk)
        digest.update(chunk)
    if expected_size is not None and size != expected_size:
        raise BackupVerificationError("member_size_mismatch")
    return size, digest.hexdigest()


@contextmanager
def _open_regular_nofollow(path: Path) -> Iterator[BinaryIO]:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        if exc.errno != errno.ELOOP:
            raise BackupVerificationError("backup_archive_unreadable") from exc
        raise BackupVerificationError("backup_archive_unsafe_type") from exc
    try:
        descriptor_stat = os.fstat(descriptor)
        if not stat.S_ISREG(descriptor_stat.st_mode):
            raise BackupVerificationError("backup_archive_unsafe_type")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            yield handle
    finally:
        os.close(descriptor)


@contextmanager
def _open_audio_descriptor(data_dir: Path, member_name: str) -> Iterator[BinaryIO]:
    parts = PurePosixPath(member_name).parts
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    directory_flags = flags | getattr(os, "O_DIRECTORY", 0)
    descriptors: list[int] = []
    try:
        root_descriptor = os.open(data_dir, directory_flags)
        descriptors.append(root_descriptor)
        current_descriptor = root_descriptor
        for part in parts[:-1]:
            current_descriptor = os.open(
                part,
                directory_flags,
                dir_fd=current_descriptor,
            )
            descriptors.append(current_descriptor)
        leaf_descriptor = os.open(
            parts[-1],
            flags,
            dir_fd=current_descriptor,
        )
        descriptors.append(leaf_descriptor)
        leaf_stat = os.fstat(leaf_descriptor)
        if not stat.S_ISREG(leaf_stat.st_mode):
            raise BackupError("required_audio_unsafe_type")
        with os.fdopen(leaf_descriptor, "rb", closefd=False) as handle:
            yield handle
    except BackupError:
        raise
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise BackupError("required_audio_unsafe_type") from exc
        if exc.errno == errno.ENOENT:
            raise BackupError("required_audio_missing") from exc
        raise BackupError("required_audio_unreadable") from exc
    finally:
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass


def _write_file_member(
    archive: zipfile.ZipFile,
    *,
    source: Path,
    member_name: str,
    limits: BackupLimits,
    budget: ByteBudget,
) -> ManifestEntry:
    with source.open("rb") as source_handle, archive.open(member_name, "w") as target_handle:
        size, digest = _stream_copy(
            source_handle,
            target_handle,
            max_bytes=limits.max_member_bytes,
            budget=budget,
        )
    return ManifestEntry(path=member_name, size=size, sha256=digest)


def _write_audio_member(
    archive: zipfile.ZipFile,
    *,
    data_dir: Path,
    evidence: AudioEvidence,
    limits: BackupLimits,
    budget: ByteBudget,
) -> ManifestEntry:
    with _open_audio_descriptor(data_dir, evidence.member_name) as source_handle:
        with archive.open(evidence.member_name, "w") as target_handle:
            size, digest = _stream_copy(
                source_handle,
                target_handle,
                max_bytes=limits.max_member_bytes,
                budget=budget,
            )
    if digest != evidence.sha256:
        raise BackupError("required_audio_hash_mismatch")
    return ManifestEntry(path=evidence.member_name, size=size, sha256=digest)


def _write_bytes_member(
    archive: zipfile.ZipFile,
    *,
    payload: bytes,
    member_name: str,
    limits: BackupLimits,
    budget: ByteBudget,
) -> ManifestEntry:
    if len(payload) > limits.max_member_bytes:
        raise BackupVerificationError("archive_member_too_large")
    budget.consume(len(payload))
    archive.writestr(member_name, payload)
    return ManifestEntry(
        path=member_name,
        size=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
    )


def _normalized_audio_evidence(
    path_value: object,
    hash_value: object,
) -> tuple[str, str]:
    member_name = _safe_member_name(path_value)
    if not member_name.startswith(AUDIO_PREFIX):
        raise BackupError("referenced_audio_outside_managed_audio")
    if not _valid_sha256(hash_value):
        raise BackupError("audio_hash_evidence_missing")
    return member_name, str(hash_value)


def _referenced_audio_evidence(conn: sqlite3.Connection) -> tuple[AudioEvidence, ...]:
    asr_rows = conn.execute(
        """
        SELECT session_id, turn_index, user_audio_path, user_audio_sha256, asr_status
        FROM asr_attempts
        WHERE TRIM(user_audio_path) != ''
        """
    ).fetchall()
    conversation_rows = conn.execute(
        """
        SELECT session_id, turn_index, user_audio_path, user_audio_sha256
        FROM conversation_turns
        WHERE user_audio_path IS NOT NULL AND TRIM(user_audio_path) != ''
        """
    ).fetchall()

    hashes_by_member: dict[str, set[str]] = {}
    successful_asr_evidence: set[tuple[int, int, str, str]] = set()
    for row in asr_rows:
        member_name, audio_hash = _normalized_audio_evidence(row[2], row[3])
        hashes_by_member.setdefault(member_name, set()).add(audio_hash)
        if str(row[4]) == "success":
            successful_asr_evidence.add(
                (int(row[0]), int(row[1]), member_name, audio_hash)
            )

    conversation_evidence: list[tuple[int, int, str, str]] = []
    for row in conversation_rows:
        member_name, audio_hash = _normalized_audio_evidence(row[2], row[3])
        hashes_by_member.setdefault(member_name, set()).add(audio_hash)
        conversation_evidence.append(
            (int(row[0]), int(row[1]), member_name, audio_hash)
        )

    if any(len(hashes) != 1 for hashes in hashes_by_member.values()):
        raise BackupError("audio_hash_evidence_conflict")
    if any(evidence not in successful_asr_evidence for evidence in conversation_evidence):
        raise BackupError("audio_asr_evidence_missing")

    return tuple(
        AudioEvidence(member_name=member_name, sha256=next(iter(hashes)))
        for member_name, hashes in sorted(hashes_by_member.items())
    )


def _sqlite_integrity(path: Path) -> None:
    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            rows = conn.execute("PRAGMA integrity_check").fetchall()
            if rows != [("ok",)]:
                raise BackupVerificationError("sqlite_integrity_failed")
        finally:
            conn.close()
    except BackupVerificationError:
        raise
    except (OSError, sqlite3.Error) as exc:
        raise BackupVerificationError("sqlite_integrity_failed") from exc


def _build_metadata(settings: Settings, *, generated_at: datetime) -> bytes:
    metadata = {
        "format": BACKUP_FORMAT,
        "format_version": BACKUP_FORMAT_VERSION,
        "generated_at": generated_at.astimezone(timezone.utc).isoformat(),
        "application": {
            "name": settings.app_name,
            "environment": settings.app_env,
        },
        "restore_layout": {
            "database_member": DATABASE_MEMBER,
            "audio_prefix": AUDIO_PREFIX,
        },
        "migrations": list(expected_migration_versions()),
        "excluded": [
            "generated_exports",
            "environment_files",
            "provider_credentials",
            "admin_credentials",
            "logs",
            "caches",
            "unrelated_runtime_files",
        ],
    }
    return json.dumps(
        metadata,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ).encode("utf-8")


def _raise_rename_error(result: int) -> None:
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number in (errno.EEXIST, errno.ENOTEMPTY):
        raise FileExistsError(error_number, "destination_already_exists")
    raise OSError(error_number, "atomic_no_replace_failed")


def _file_identity(file_stat: os.stat_result) -> FileIdentity:
    return file_stat.st_dev, file_stat.st_ino


def _directory_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )


def _absolute_lexical_path(path: Path) -> Path:
    expanded = path.expanduser()
    if not expanded.is_absolute():
        raise ValueError("absolute_path_required")
    return Path(os.path.abspath(expanded))


def _open_absolute_directory(
    path: Path,
    *,
    create: bool,
    forbidden_identity: FileIdentity | None = None,
) -> int:
    absolute_path = _absolute_lexical_path(path)
    descriptor = os.open(absolute_path.anchor, _directory_flags())
    try:
        if forbidden_identity == _file_identity(os.fstat(descriptor)):
            raise BackupError("destination_parent_inside_live_data")
        for component in absolute_path.parts[1:]:
            if create:
                try:
                    os.mkdir(component, mode=0o777, dir_fd=descriptor)
                except FileExistsError:
                    pass
            next_descriptor = os.open(
                component,
                _directory_flags(),
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = next_descriptor
            if forbidden_identity == _file_identity(os.fstat(descriptor)):
                raise BackupError("destination_parent_inside_live_data")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _assert_trusted_directory(directory_stat: os.stat_result) -> None:
    if (
        not stat.S_ISDIR(directory_stat.st_mode)
        or directory_stat.st_uid not in (0, os.geteuid())
    ):
        raise BackupError("destination_ancestry_untrusted")
    untrusted_write_bits = stat.S_IWGRP | stat.S_IWOTH
    if (
        directory_stat.st_mode & untrusted_write_bits
        and not directory_stat.st_mode & stat.S_ISVTX
    ):
        raise BackupError("destination_ancestry_untrusted")


def _open_trusted_destination_chain(
    path: Path,
    *,
    create: bool,
    forbidden_identity: FileIdentity | None = None,
) -> tuple[tuple[int, ...], tuple[FileIdentity, ...]]:
    absolute_path = _absolute_lexical_path(path)
    descriptors = [os.open(absolute_path.anchor, _directory_flags())]
    identities: list[FileIdentity] = []
    try:
        root_stat = os.fstat(descriptors[0])
        _assert_trusted_directory(root_stat)
        identities.append(_file_identity(root_stat))
        if forbidden_identity == identities[-1]:
            raise BackupError("destination_parent_inside_live_data")
        for component in absolute_path.parts[1:]:
            current_descriptor = descriptors[-1]
            if create:
                try:
                    os.mkdir(component, mode=0o700, dir_fd=current_descriptor)
                except FileExistsError:
                    pass
            next_descriptor = os.open(
                component,
                _directory_flags(),
                dir_fd=current_descriptor,
            )
            descriptors.append(next_descriptor)
            next_stat = os.fstat(next_descriptor)
            _assert_trusted_directory(next_stat)
            identities.append(_file_identity(next_stat))
            if forbidden_identity == identities[-1]:
                raise BackupError("destination_parent_inside_live_data")
        return tuple(descriptors), tuple(identities)
    except BaseException:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
        raise


def _descriptor_ancestry_contains(
    descriptor: int,
    identity: FileIdentity,
) -> bool:
    current_descriptor = os.dup(descriptor)
    try:
        while True:
            current_identity = _file_identity(os.fstat(current_descriptor))
            if current_identity == identity:
                return True
            parent_descriptor = os.open(
                "..",
                _directory_flags(),
                dir_fd=current_descriptor,
            )
            parent_identity = _file_identity(os.fstat(parent_descriptor))
            if parent_identity == current_identity:
                os.close(parent_descriptor)
                return False
            os.close(current_descriptor)
            current_descriptor = parent_descriptor
    finally:
        os.close(current_descriptor)


def open_destination_parent(
    destination: Path,
    *,
    live_data_dir: Path | None,
) -> OpenDestinationParent:
    parent_path = _absolute_lexical_path(destination.parent)
    if live_data_dir is None:
        try:
            descriptors, identities = _open_trusted_destination_chain(
                parent_path,
                create=True,
            )
        except BackupError:
            raise
        except (OSError, ValueError) as exc:
            raise BackupError("destination_parent_unsafe_type") from exc
        descriptor = descriptors[-1]
        descriptor_stat = os.fstat(descriptor)
        return OpenDestinationParent(
            path=parent_path,
            descriptor=descriptor,
            device=descriptor_stat.st_dev,
            inode=descriptor_stat.st_ino,
            ancestry_descriptors=descriptors,
            ancestry_identities=identities,
        )
    live_path = _absolute_lexical_path(live_data_dir)
    try:
        live_descriptor = _open_absolute_directory(live_path, create=False)
    except (OSError, ValueError) as exc:
        raise BackupError("live_data_directory_unsafe_type") from exc
    try:
        live_identity = _file_identity(os.fstat(live_descriptor))
        try:
            descriptors, identities = _open_trusted_destination_chain(
                parent_path,
                create=True,
                forbidden_identity=live_identity,
            )
        except BackupError:
            raise
        except (OSError, ValueError) as exc:
            raise BackupError("destination_parent_unsafe_type") from exc
        descriptor = descriptors[-1]
        try:
            descriptor_stat = os.fstat(descriptor)
            if (
                not stat.S_ISDIR(descriptor_stat.st_mode)
                or _descriptor_ancestry_contains(descriptor, live_identity)
            ):
                raise BackupError("destination_parent_inside_live_data")
        except BaseException:
            for open_descriptor in reversed(descriptors):
                os.close(open_descriptor)
            raise
    finally:
        os.close(live_descriptor)
    return OpenDestinationParent(
        path=parent_path,
        descriptor=descriptor,
        device=descriptor_stat.st_dev,
        inode=descriptor_stat.st_ino,
        ancestry_descriptors=descriptors,
        ancestry_identities=identities,
    )


def close_destination_parent(parent: OpenDestinationParent) -> None:
    for descriptor in reversed(parent.ancestry_descriptors):
        try:
            os.close(descriptor)
        except OSError:
            pass


def destination_exists_at(parent: OpenDestinationParent, name: str) -> bool:
    try:
        os.stat(name, dir_fd=parent.descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def create_private_staging_container(
    parent: OpenDestinationParent,
    *,
    prefix: str,
) -> PrivateStagingContainer:
    for _ in range(100):
        name = f"{prefix}{secrets.token_hex(16)}"
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent.descriptor)
        except FileExistsError:
            continue
        try:
            descriptor = os.open(
                name,
                _directory_flags(),
                dir_fd=parent.descriptor,
            )
        except OSError:
            raise
        container_stat = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(container_stat.st_mode)
            or container_stat.st_uid != os.geteuid()
            or stat.S_IMODE(container_stat.st_mode) != 0o700
        ):
            os.close(descriptor)
            raise BackupError("private_staging_container_untrusted")
        return PrivateStagingContainer(
            name=name,
            descriptor=descriptor,
            device=container_stat.st_dev,
            inode=container_stat.st_ino,
        )
    raise BackupError("staging_name_exhausted")


def remove_empty_private_staging_container(
    parent: OpenDestinationParent,
    container: PrivateStagingContainer,
) -> None:
    try:
        current = os.stat(
            container.name,
            dir_fd=parent.descriptor,
            follow_symlinks=False,
        )
        if _file_identity(current) != (container.device, container.inode):
            return
        os.rmdir(container.name, dir_fd=parent.descriptor)
    except OSError:
        return


def _assert_destination_parent_identity(
    parent: OpenDestinationParent,
) -> None:
    try:
        for descriptor, expected_identity in zip(
            parent.ancestry_descriptors,
            parent.ancestry_identities,
            strict=True,
        ):
            retained_stat = os.fstat(descriptor)
            _assert_trusted_directory(retained_stat)
            if _file_identity(retained_stat) != expected_identity:
                raise DestinationParentChangedError(
                    errno.ESTALE,
                    "destination_parent_changed",
                )
    except BackupError as exc:
        raise DestinationParentChangedError(
            errno.ESTALE,
            "destination_parent_changed",
        ) from exc
    try:
        current_descriptor = _open_absolute_directory(
            parent.path,
            create=False,
        )
    except (OSError, ValueError) as exc:
        raise DestinationParentChangedError(
            errno.ESTALE,
            "destination_parent_changed",
        ) from exc
    try:
        current_identity = _file_identity(os.fstat(current_descriptor))
    finally:
        os.close(current_descriptor)
    if current_identity != (parent.device, parent.inode):
        raise DestinationParentChangedError(
            errno.ESTALE,
            "destination_parent_changed",
        )


def _assert_source_identity(
    source_descriptor: int,
    name: str,
    expected_identity: FileIdentity,
) -> None:
    try:
        current_source = os.stat(
            name,
            dir_fd=source_descriptor,
            follow_symlinks=False,
        )
    except OSError as exc:
        raise SourceIdentityChangedError(
            errno.ESTALE,
            "source_identity_changed",
        ) from exc
    if _file_identity(current_source) != expected_identity:
        raise SourceIdentityChangedError(
            errno.ESTALE,
            "source_identity_changed",
        )


def _native_publish_noreplace(
    source_descriptor: int,
    source_name: str,
    destination_descriptor: int,
    destination_name: str,
) -> None:
    library = ctypes.CDLL(None, use_errno=True)
    encoded_source_name = os.fsencode(source_name)
    encoded_destination_name = os.fsencode(destination_name)
    if sys.platform == "darwin":
        try:
            rename_function = library.renameatx_np
        except AttributeError as exc:
            raise OSError(
                errno.ENOTSUP,
                "atomic_no_replace_unsupported",
            ) from exc
        rename_function.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        rename_function.restype = ctypes.c_int
        rename_flags = 0x00000004
    elif sys.platform.startswith("linux"):
        try:
            rename_function = library.renameat2
        except AttributeError as exc:
            raise OSError(
                errno.ENOTSUP,
                "atomic_no_replace_unsupported",
            ) from exc
        rename_function.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        rename_function.restype = ctypes.c_int
        rename_flags = 0x00000001
    else:
        raise OSError(errno.ENOTSUP, "atomic_no_replace_unsupported")
    result = rename_function(
        source_descriptor,
        encoded_source_name,
        destination_descriptor,
        encoded_destination_name,
        rename_flags,
    )
    _raise_rename_error(result)


def atomic_publish_noreplace(
    source: Path,
    destination: Path,
    *,
    parent: OpenDestinationParent | None = None,
    expected_source_identity: FileIdentity | None = None,
    source_parent_descriptor: int | None = None,
) -> None:
    if source_parent_descriptor is None and source.parent != destination.parent:
        raise OSError(errno.EXDEV, "atomic_publish_requires_same_parent")
    owns_parent = parent is None
    active_parent = parent or open_destination_parent(
        destination,
        live_data_dir=None,
    )
    active_source_descriptor = (
        source_parent_descriptor
        if source_parent_descriptor is not None
        else active_parent.descriptor
    )
    try:
        if source_parent_descriptor is not None:
            source_parent_stat = os.fstat(active_source_descriptor)
            if (
                not stat.S_ISDIR(source_parent_stat.st_mode)
                or source_parent_stat.st_uid != os.geteuid()
                or stat.S_IMODE(source_parent_stat.st_mode) != 0o700
            ):
                raise OSError(
                    errno.EPERM,
                    "private_source_directory_untrusted",
                )
        if expected_source_identity is None:
            try:
                initial_source = os.stat(
                    source.name,
                    dir_fd=active_source_descriptor,
                    follow_symlinks=False,
                )
            except OSError as exc:
                raise SourceIdentityChangedError(
                    errno.ESTALE,
                    "source_identity_changed",
                ) from exc
            expected_source_identity = _file_identity(initial_source)
        _assert_destination_parent_identity(active_parent)
        _assert_source_identity(
            active_source_descriptor,
            source.name,
            expected_source_identity,
        )
        _native_publish_noreplace(
            active_source_descriptor,
            source.name,
            active_parent.descriptor,
            destination.name,
        )
    finally:
        if owns_parent:
            close_destination_parent(active_parent)


def create_backup(
    settings: Settings,
    *,
    output_path: Path | None = None,
) -> Path:
    destination = (output_path or default_output_path(settings)).expanduser()
    if not destination.is_absolute():
        destination = PROJECT_ROOT / destination
    _validate_external_path(destination, live_data_dir=settings.data_dir)
    limits = BackupLimits.from_settings(settings)
    parent = open_destination_parent(
        destination,
        live_data_dir=settings.data_dir,
    )
    if destination_exists_at(parent, destination.name):
        close_destination_parent(parent)
        raise BackupError("backup_destination_already_exists")

    try:
        conn = get_live_read_only_connection(settings)
    except (FileNotFoundError, sqlite3.Error, ValueError) as exc:
        close_destination_parent(parent)
        raise BackupError("backup_database_unavailable") from exc
    staging_container: PrivateStagingContainer | None = None
    temporary_archive_descriptor: int | None = None
    temporary_archive_identity: FileIdentity | None = None
    published = False
    try:
        if not migration_state_is_current(conn):
            raise BackupError("migration_state_mismatch")
        with TemporaryDirectory(prefix="interface-v2-backup-") as temporary_directory:
            snapshot_path = Path(temporary_directory) / DATABASE_MEMBER
            snapshot_conn = sqlite3.connect(snapshot_path)
            try:
                conn.backup(snapshot_conn)
            finally:
                snapshot_conn.close()
            _sqlite_integrity(snapshot_path)

            snapshot_read_conn = sqlite3.connect(snapshot_path)
            try:
                audio_evidence = _referenced_audio_evidence(snapshot_read_conn)
            finally:
                snapshot_read_conn.close()

            staging_container = create_private_staging_container(
                parent,
                prefix=f".{destination.name}.staging-",
            )
            temporary_archive_name = "payload"
            temporary_archive_descriptor = os.open(
                temporary_archive_name,
                os.O_RDWR
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=staging_container.descriptor,
            )
            temporary_archive = (
                parent.path
                / staging_container.name
                / temporary_archive_name
            )
            temporary_archive_identity = _file_identity(
                os.fstat(temporary_archive_descriptor)
            )

            budget = ByteBudget(
                limits.max_total_uncompressed_bytes,
                error_code="archive_total_too_large",
            )
            manifest_entries: list[ManifestEntry] = []
            with os.fdopen(
                os.dup(temporary_archive_descriptor),
                "w+b",
            ) as temporary_file:
                with zipfile.ZipFile(
                    temporary_file,
                    mode="w",
                    compression=zipfile.ZIP_DEFLATED,
                    allowZip64=True,
                ) as archive:
                    manifest_entries.append(
                        _write_file_member(
                            archive,
                            source=snapshot_path,
                            member_name=DATABASE_MEMBER,
                            limits=limits,
                            budget=budget,
                        )
                    )
                    for evidence in audio_evidence:
                        manifest_entries.append(
                            _write_audio_member(
                                archive,
                                data_dir=settings.data_dir,
                                evidence=evidence,
                                limits=limits,
                                budget=budget,
                            )
                        )
                    manifest_entries.append(
                        _write_bytes_member(
                            archive,
                            payload=_build_metadata(
                                settings,
                                generated_at=datetime.now(timezone.utc),
                            ),
                            member_name=METADATA_MEMBER,
                            limits=limits,
                            budget=budget,
                        )
                    )
                    manifest_payload = json.dumps(
                        {
                            "format": BACKUP_FORMAT,
                            "format_version": BACKUP_FORMAT_VERSION,
                            "members": [
                                entry.as_dict() for entry in manifest_entries
                            ],
                        },
                        indent=2,
                        sort_keys=True,
                    ).encode("utf-8")
                    _write_bytes_member(
                        archive,
                        payload=manifest_payload,
                        member_name=MANIFEST_MEMBER,
                        limits=limits,
                        budget=budget,
                    )

            os.lseek(temporary_archive_descriptor, 0, os.SEEK_SET)
            with os.fdopen(
                os.dup(temporary_archive_descriptor),
                "rb",
            ) as temporary_file:
                _verify_backup_handle(
                    temporary_file,
                    archive_path=temporary_archive,
                    limits=limits,
                )
            try:
                atomic_publish_noreplace(
                    temporary_archive,
                    destination,
                    parent=parent,
                    expected_source_identity=temporary_archive_identity,
                    source_parent_descriptor=staging_container.descriptor,
                )
            except DestinationParentChangedError as exc:
                raise BackupError("backup_destination_parent_changed") from exc
            except SourceIdentityChangedError as exc:
                raise BackupError("backup_staging_identity_changed") from exc
            except FileExistsError as exc:
                raise BackupError("backup_destination_already_exists") from exc
            except OSError as exc:
                raise BackupError("backup_publication_failed") from exc
            published = True
    finally:
        conn.close()
        if temporary_archive_descriptor is not None:
            os.close(temporary_archive_descriptor)
        if staging_container is not None:
            if published:
                remove_empty_private_staging_container(
                    parent,
                    staging_container,
                )
            os.close(staging_container.descriptor)
        close_destination_parent(parent)
    return destination


def _validate_zip_info(info: zipfile.ZipInfo) -> None:
    _safe_member_name(info.filename)
    if info.is_dir():
        raise BackupVerificationError("unsafe_archive_member_type")
    unix_mode = (info.external_attr >> 16) & 0xFFFF
    file_type = stat.S_IFMT(unix_mode)
    if file_type not in (0, stat.S_IFREG):
        raise BackupVerificationError("unsafe_archive_member_type")


def _read_exact_at(handle: BinaryIO, *, offset: int, size: int) -> bytes:
    handle.seek(offset)
    payload = handle.read(size)
    if len(payload) != size:
        raise BackupVerificationError("backup_archive_unreadable")
    return payload


def _preflight_archive(handle: BinaryIO, *, limits: BackupLimits) -> None:
    handle.seek(0, os.SEEK_END)
    archive_size = handle.tell()
    if archive_size > (
        limits.max_total_uncompressed_bytes + ARCHIVE_OVERHEAD_ALLOWANCE
    ):
        raise BackupVerificationError("backup_archive_too_large")

    tail_size = min(archive_size, 22 + 65_535)
    tail = _read_exact_at(
        handle,
        offset=archive_size - tail_size,
        size=tail_size,
    )
    eocd_position = tail.rfind(b"PK\x05\x06")
    if eocd_position < 0 or len(tail) - eocd_position < 22:
        raise BackupVerificationError("backup_archive_unreadable")
    (
        _,
        disk_number,
        central_disk_number,
        disk_members,
        total_members,
        central_size,
        central_offset,
        comment_size,
    ) = struct.unpack_from("<4s4H2LH", tail, eocd_position)
    if eocd_position + 22 + comment_size != len(tail):
        raise BackupVerificationError("backup_archive_unreadable")
    if disk_number != 0 or central_disk_number != 0 or disk_members != total_members:
        raise BackupVerificationError("multi_disk_archive_unsupported")

    if (
        total_members == 0xFFFF
        or central_size == 0xFFFFFFFF
        or central_offset == 0xFFFFFFFF
    ):
        absolute_eocd_offset = archive_size - tail_size + eocd_position
        if absolute_eocd_offset < 20:
            raise BackupVerificationError("backup_archive_unreadable")
        locator = _read_exact_at(
            handle,
            offset=absolute_eocd_offset - 20,
            size=20,
        )
        locator_signature, locator_disk, zip64_offset, total_disks = struct.unpack(
            "<4sLQL",
            locator,
        )
        if (
            locator_signature != b"PK\x06\x07"
            or locator_disk != 0
            or total_disks != 1
        ):
            raise BackupVerificationError("backup_archive_unreadable")
        zip64_header = _read_exact_at(
            handle,
            offset=zip64_offset,
            size=56,
        )
        (
            zip64_signature,
            _,
            _,
            _,
            zip64_disk,
            zip64_central_disk,
            zip64_disk_members,
            total_members,
            central_size,
            central_offset,
        ) = struct.unpack("<4sQ2H2L4Q", zip64_header)
        if (
            zip64_signature != b"PK\x06\x06"
            or zip64_disk != 0
            or zip64_central_disk != 0
            or zip64_disk_members != total_members
        ):
            raise BackupVerificationError("multi_disk_archive_unsupported")

    if total_members > limits.max_members:
        raise BackupVerificationError("archive_member_count_exceeded")
    if central_size > limits.max_central_directory_bytes:
        raise BackupVerificationError("archive_central_directory_too_large")
    if central_offset + central_size > archive_size:
        raise BackupVerificationError("backup_archive_unreadable")

    central_directory = _read_exact_at(
        handle,
        offset=central_offset,
        size=central_size,
    )
    observed_members = 0
    cursor = 0
    while cursor < len(central_directory):
        if len(central_directory) - cursor < 46:
            raise BackupVerificationError("backup_archive_unreadable")
        header = struct.unpack_from(
            "<4s6H3L5H2L",
            central_directory,
            cursor,
        )
        if header[0] != b"PK\x01\x02":
            raise BackupVerificationError("backup_archive_unreadable")
        record_size = 46 + header[10] + header[11] + header[12]
        if record_size > len(central_directory) - cursor:
            raise BackupVerificationError("backup_archive_unreadable")
        cursor += record_size
        observed_members += 1
        if observed_members > limits.max_members:
            raise BackupVerificationError("archive_member_count_exceeded")
    if observed_members != total_members:
        raise BackupVerificationError("archive_member_count_mismatch")
    handle.seek(0)


def _validate_archive_structure(
    infos: list[zipfile.ZipInfo],
    *,
    limits: BackupLimits,
) -> dict[str, zipfile.ZipInfo]:
    if len(infos) > limits.max_members:
        raise BackupVerificationError("archive_member_count_exceeded")
    total_size = 0
    names: list[str] = []
    for info in infos:
        _validate_zip_info(info)
        names.append(info.filename)
        if info.file_size > limits.max_member_bytes:
            raise BackupVerificationError("archive_member_too_large")
        total_size += info.file_size
        if total_size > limits.max_total_uncompressed_bytes:
            raise BackupVerificationError("archive_total_too_large")
        if info.file_size:
            ratio = info.file_size / max(info.compress_size, 1)
            if ratio > limits.max_compression_ratio:
                raise BackupVerificationError(
                    "archive_compression_ratio_exceeded"
                )
    if len(names) != len(set(names)):
        raise BackupVerificationError("duplicate_archive_member")
    return {info.filename: info for info in infos}


def _read_member_bytes(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    *,
    limits: BackupLimits,
    max_bytes: int,
    error_code: str,
) -> bytes:
    if info.file_size > min(limits.max_member_bytes, max_bytes):
        raise BackupVerificationError(error_code)
    output = bytearray()
    budget = ByteBudget(
        limits.max_total_uncompressed_bytes,
        error_code="archive_total_too_large",
    )
    try:
        with archive.open(info) as source:
            while chunk := source.read(STREAM_CHUNK_BYTES):
                output.extend(chunk)
                budget.consume(len(chunk))
                if len(output) > min(limits.max_member_bytes, max_bytes):
                    raise BackupVerificationError(error_code)
    except BackupVerificationError:
        raise
    except (KeyError, RuntimeError, zipfile.BadZipFile, OSError) as exc:
        raise BackupVerificationError(error_code) from exc
    if len(output) != info.file_size:
        raise BackupVerificationError("member_size_mismatch")
    return bytes(output)


def _read_json_member(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    *,
    limits: BackupLimits,
    error_code: str,
) -> object:
    try:
        return json.loads(
            _read_member_bytes(
                archive,
                info,
                limits=limits,
                max_bytes=JSON_MEMBER_LIMIT,
                error_code=error_code,
            ).decode("utf-8")
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BackupVerificationError(error_code) from exc


def _parse_manifest(payload: object) -> dict[str, ManifestEntry]:
    if not isinstance(payload, dict):
        raise BackupVerificationError("manifest_invalid")
    if payload.get("format") != BACKUP_FORMAT or payload.get("format_version") != BACKUP_FORMAT_VERSION:
        raise BackupVerificationError("manifest_invalid")
    raw_members = payload.get("members")
    if not isinstance(raw_members, list):
        raise BackupVerificationError("manifest_invalid")

    entries: dict[str, ManifestEntry] = {}
    for raw_entry in raw_members:
        if not isinstance(raw_entry, dict) or set(raw_entry) != {"path", "size", "sha256"}:
            raise BackupVerificationError("manifest_invalid")
        path = _safe_member_name(raw_entry["path"])
        size = raw_entry["size"]
        sha256 = raw_entry["sha256"]
        if (
            path == MANIFEST_MEMBER
            or path in entries
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or not _valid_sha256(sha256)
        ):
            raise BackupVerificationError("manifest_invalid")
        entries[path] = ManifestEntry(path=path, size=size, sha256=str(sha256))
    return entries


def _validate_metadata(payload: object) -> None:
    if not isinstance(payload, dict):
        raise BackupVerificationError("backup_metadata_invalid")
    layout = payload.get("restore_layout")
    if (
        payload.get("format") != BACKUP_FORMAT
        or payload.get("format_version") != BACKUP_FORMAT_VERSION
        or not isinstance(layout, dict)
        or layout.get("database_member") != DATABASE_MEMBER
        or layout.get("audio_prefix") != AUDIO_PREFIX
        or payload.get("migrations") != list(expected_migration_versions())
    ):
        raise BackupVerificationError("backup_metadata_invalid")


def _copy_zip_member(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    destination: Path,
    *,
    limits: BackupLimits,
    budget: ByteBudget,
) -> str:
    try:
        with archive.open(info) as source, destination.open("xb") as target:
            _, digest = _stream_copy(
                source,
                target,
                max_bytes=limits.max_member_bytes,
                budget=budget,
                expected_size=info.file_size,
            )
    except BackupVerificationError:
        raise
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        raise BackupVerificationError("member_read_failed") from exc
    return digest


def _copy_zip_member_at(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    *,
    root_descriptor: int,
    member_name: str,
    limits: BackupLimits,
    budget: ByteBudget,
) -> None:
    parts = PurePosixPath(member_name).parts
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    leaf_flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    descriptors: list[int] = []
    try:
        current_descriptor = os.dup(root_descriptor)
        descriptors.append(current_descriptor)
        for part in parts[:-1]:
            try:
                os.mkdir(part, mode=0o700, dir_fd=current_descriptor)
            except FileExistsError:
                pass
            current_descriptor = os.open(
                part,
                directory_flags,
                dir_fd=current_descriptor,
            )
            descriptors.append(current_descriptor)
        leaf_descriptor = os.open(
            parts[-1],
            leaf_flags,
            0o600,
            dir_fd=current_descriptor,
        )
        descriptors.append(leaf_descriptor)
        if not stat.S_ISREG(os.fstat(leaf_descriptor).st_mode):
            raise BackupVerificationError("unsafe_archive_member_type")
        with archive.open(info) as source, os.fdopen(
            leaf_descriptor,
            "wb",
            closefd=False,
        ) as target:
            _stream_copy(
                source,
                target,
                max_bytes=limits.max_member_bytes,
                budget=budget,
                expected_size=info.file_size,
            )
    except BackupVerificationError:
        raise
    except OSError as exc:
        if exc.errno in (
            errno.EEXIST,
            errno.ELOOP,
            errno.ENOTDIR,
        ):
            raise BackupVerificationError("unsafe_archive_member_path") from exc
        raise BackupVerificationError("member_read_failed") from exc
    finally:
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass


def _verify_database_payload(
    archive: zipfile.ZipFile,
    *,
    info: zipfile.ZipInfo,
    manifest: dict[str, ManifestEntry],
    limits: BackupLimits,
) -> tuple[str, ...]:
    with TemporaryDirectory(prefix="interface-v2-backup-verify-") as temporary_directory:
        database_path = Path(temporary_directory) / DATABASE_MEMBER
        _copy_zip_member(
            archive,
            info,
            database_path,
            limits=limits,
            budget=ByteBudget(
                limits.max_total_uncompressed_bytes,
                error_code="archive_total_too_large",
            ),
        )
        _sqlite_integrity(database_path)
        try:
            conn = sqlite3.connect(
                f"{database_path.resolve().as_uri()}?mode=ro",
                uri=True,
            )
            try:
                audio_evidence = _referenced_audio_evidence(conn)
                applied_migrations = tuple(
                    sorted(
                        str(row[0])
                        for row in conn.execute(
                            "SELECT version FROM schema_migrations"
                        ).fetchall()
                    )
                )
            finally:
                conn.close()
        except BackupError as exc:
            raise BackupVerificationError(str(exc)) from exc
        except sqlite3.Error as exc:
            raise BackupVerificationError("sqlite_backup_structure_invalid") from exc
        if applied_migrations != tuple(sorted(expected_migration_versions())):
            raise BackupVerificationError("sqlite_migration_state_mismatch")
        for evidence in audio_evidence:
            manifest_entry = manifest.get(evidence.member_name)
            if manifest_entry is None:
                raise BackupVerificationError("required_audio_member_mismatch")
            if manifest_entry.sha256 != evidence.sha256:
                raise BackupVerificationError("required_audio_hash_mismatch")
        return tuple(evidence.member_name for evidence in audio_evidence)


def _verify_backup_handle(
    archive_handle: BinaryIO,
    *,
    archive_path: Path,
    limits: BackupLimits,
) -> BackupVerification:
    try:
        _preflight_archive(archive_handle, limits=limits)
        with zipfile.ZipFile(archive_handle, mode="r") as archive:
            info_by_name = _validate_archive_structure(
                archive.infolist(),
                limits=limits,
            )
            if MANIFEST_MEMBER not in info_by_name:
                raise BackupVerificationError("manifest_missing")
            manifest = _parse_manifest(
                _read_json_member(
                    archive,
                    info_by_name[MANIFEST_MEMBER],
                    limits=limits,
                    error_code="manifest_invalid",
                )
            )
            if set(info_by_name) != set(manifest) | {MANIFEST_MEMBER}:
                raise BackupVerificationError("member_set_mismatch")
            if DATABASE_MEMBER not in manifest or METADATA_MEMBER not in manifest:
                raise BackupVerificationError("required_member_missing")

            budget = ByteBudget(
                limits.max_total_uncompressed_bytes,
                error_code="archive_total_too_large",
            )
            for member_name, expected in manifest.items():
                info = info_by_name[member_name]
                if info.file_size != expected.size:
                    raise BackupVerificationError("member_size_mismatch")
                try:
                    with archive.open(info) as member_handle:
                        _, actual_hash = _stream_copy(
                            member_handle,
                            BinaryNullWriter(),
                            max_bytes=limits.max_member_bytes,
                            budget=budget,
                            expected_size=expected.size,
                        )
                except BackupVerificationError:
                    raise
                except (RuntimeError, zipfile.BadZipFile, OSError) as exc:
                    raise BackupVerificationError("member_read_failed") from exc
                if actual_hash != expected.sha256:
                    raise BackupVerificationError("member_hash_mismatch")

            _validate_metadata(
                _read_json_member(
                    archive,
                    info_by_name[METADATA_MEMBER],
                    limits=limits,
                    error_code="backup_metadata_invalid",
                )
            )
            allowed_audio_members = _verify_database_payload(
                archive,
                info=info_by_name[DATABASE_MEMBER],
                manifest=manifest,
                limits=limits,
            )
            allowed_members = {
                DATABASE_MEMBER,
                METADATA_MEMBER,
                *allowed_audio_members,
            }
            if set(manifest) != allowed_members:
                raise BackupVerificationError("required_audio_member_mismatch")
    except BackupVerificationError:
        raise
    except (FileNotFoundError, PermissionError, zipfile.BadZipFile, OSError) as exc:
        raise BackupVerificationError("backup_archive_unreadable") from exc
    return BackupVerification(
        archive_path=archive_path,
        audio_members=allowed_audio_members,
        member_count=len(manifest),
    )


def verify_backup(
    archive_path: Path,
    *,
    limits: BackupLimits | None = None,
) -> BackupVerification:
    path = archive_path.expanduser()
    active_limits = limits or BackupLimits()
    with _open_regular_nofollow(path) as archive_handle:
        return _verify_backup_handle(
            archive_handle,
            archive_path=path,
            limits=active_limits,
        )


class BinaryNullWriter:
    def write(self, payload: bytes) -> int:
        return len(payload)


@contextmanager
def owned_archive_copy(
    source_path: Path,
    *,
    limits: BackupLimits,
) -> Iterator[Path]:
    with TemporaryDirectory(prefix="interface-v2-restore-source-") as temporary_directory:
        owned_path = Path(temporary_directory) / "backup.zip"
        maximum_archive_bytes = (
            limits.max_total_uncompressed_bytes + ARCHIVE_OVERHEAD_ALLOWANCE
        )
        try:
            with _open_regular_nofollow(source_path) as source, owned_path.open("xb") as target:
                _preflight_archive(source, limits=limits)
                copied = 0
                while chunk := source.read(STREAM_CHUNK_BYTES):
                    copied += len(chunk)
                    if copied > maximum_archive_bytes:
                        raise BackupVerificationError("backup_archive_too_large")
                    target.write(chunk)
        except BackupVerificationError:
            raise
        except OSError as exc:
            raise BackupVerificationError("backup_archive_unreadable") from exc
        owned_path.chmod(0o400)
        yield owned_path


def extract_archive_members(
    archive_path: Path,
    *,
    destination: Path,
    limits: BackupLimits,
    destination_descriptor: int | None = None,
) -> None:
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    if destination_descriptor is None:
        try:
            root_descriptor = os.open(destination, directory_flags)
        except OSError as exc:
            raise BackupVerificationError("unsafe_restore_staging_directory") from exc
    else:
        root_descriptor = os.dup(destination_descriptor)
    try:
        if not stat.S_ISDIR(os.fstat(root_descriptor).st_mode):
            raise BackupVerificationError("unsafe_restore_staging_directory")
        with _open_regular_nofollow(archive_path) as archive_handle:
            _preflight_archive(archive_handle, limits=limits)
            with zipfile.ZipFile(archive_handle, mode="r") as archive:
                info_by_name = _validate_archive_structure(
                    archive.infolist(),
                    limits=limits,
                )
                budget = ByteBudget(
                    limits.max_total_uncompressed_bytes,
                    error_code="archive_total_too_large",
                )
                for info in info_by_name.values():
                    member_name = _safe_member_name(info.filename)
                    _copy_zip_member_at(
                        archive,
                        info,
                        root_descriptor=root_descriptor,
                        member_name=member_name,
                        limits=limits,
                        budget=budget,
                    )
    finally:
        os.close(root_descriptor)


def main() -> int:
    args = parse_args()
    try:
        settings = get_settings()
        limits = BackupLimits.from_settings(settings)
        if args.verify is not None:
            verification = verify_backup(args.verify, limits=limits)
            print(
                f"backup verified: {verification.archive_path} "
                f"({verification.member_count} payload members)"
            )
            return 0

        output_path = create_backup(settings, output_path=args.output)
        print(f"backup written and verified: {output_path}")
        return 0
    except BackupError as exc:
        print(f"backup failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
