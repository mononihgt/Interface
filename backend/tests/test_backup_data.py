from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import stat
import struct
import sys
import zipfile

import pytest

from backend.app.db import get_connection, run_migrations
from backend.app.settings import Settings


def _load_script(module_name: str, filename: str):
    script_path = Path(__file__).resolve().parents[2] / "scripts" / filename
    assert script_path.exists(), f"{filename} has not been implemented"
    spec = importlib.util.spec_from_file_location(module_name, script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


backup_data = _load_script("backup_data", "backup_data.py")


@pytest.fixture
def backup_settings(tmp_path: Path) -> Settings:
    data_dir = tmp_path / "live" / "data"
    database_path = data_dir / "production.db"
    settings = Settings(
        app_env="production",
        data_dir=data_dir,
        database_url=f"sqlite:///{database_path}",
        yizhan_api_key="YIZHAN_BACKUP_SENTINEL",
        aabao_api_key="AABAO_BACKUP_SENTINEL",
        packyapi_api_key="PACKY_BACKUP_SENTINEL",
        tencent_secret_id="TENCENT_ID_BACKUP_SENTINEL",
        tencent_secret_key="TENCENT_KEY_BACKUP_SENTINEL",
        admin_password_hash="ADMIN_BACKUP_SENTINEL",
        app_secret_key="APP_BACKUP_SENTINEL",
    )
    for directory_name in ("audio", "exports", "logs"):
        (data_dir / directory_name).mkdir(parents=True, exist_ok=True)

    audio_path = data_dir / "audio" / "session-1" / "turn-1.webm"
    audio_path.parent.mkdir(parents=True)
    audio_path.write_bytes(b"required raw audio")
    (data_dir / "audio" / "orphan.webm").write_bytes(b"unreferenced audio")
    (data_dir / "exports" / "generated.zip").write_bytes(b"generated export")
    (data_dir / "logs" / "server.log").write_text("secret log", encoding="utf-8")
    (data_dir / ".env").write_text("SECRET=do-not-back-up", encoding="utf-8")

    conn = get_connection(settings)
    try:
        run_migrations(conn)
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute(
            """
            INSERT INTO asr_attempts (
                session_id,
                turn_index,
                attempt_no,
                user_audio_path,
                user_audio_sha256,
                asr_status
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                999,
                1,
                1,
                "audio/session-1/turn-1.webm",
                hashlib.sha256(b"required raw audio").hexdigest(),
                "success",
            ),
        )
    finally:
        conn.close()
    return settings


def _restore_module():
    return _load_script("restore_backup", "restore_backup.py")


def _rewrite_archive(
    source: Path,
    destination: Path,
    *,
    replacements: dict[str, bytes] | None = None,
    omitted: set[str] | None = None,
    extra: tuple[str, bytes] | None = None,
) -> None:
    replacements = replacements or {}
    omitted = omitted or set()
    with zipfile.ZipFile(source) as input_archive, zipfile.ZipFile(
        destination,
        "w",
        compression=zipfile.ZIP_DEFLATED,
    ) as output_archive:
        for info in input_archive.infolist():
            if info.filename in omitted:
                continue
            output_archive.writestr(
                info.filename,
                replacements.get(info.filename, input_archive.read(info.filename)),
            )
        if extra is not None:
            output_archive.writestr(*extra)


def _rewrite_manifest_consistent_archive(
    source: Path,
    destination: Path,
    *,
    replacements: dict[str, bytes],
) -> None:
    with zipfile.ZipFile(source) as input_archive:
        payloads = {
            info.filename: input_archive.read(info)
            for info in input_archive.infolist()
        }
    manifest = json.loads(payloads["manifest.json"])
    for member_name, replacement in replacements.items():
        payloads[member_name] = replacement
        entry = next(
            item for item in manifest["members"] if item["path"] == member_name
        )
        entry["size"] = len(replacement)
        entry["sha256"] = hashlib.sha256(replacement).hexdigest()
    payloads["manifest.json"] = json.dumps(manifest, sort_keys=True).encode("utf-8")
    with zipfile.ZipFile(
        destination,
        "w",
        compression=zipfile.ZIP_DEFLATED,
    ) as output_archive:
        for member_name, payload in payloads.items():
            output_archive.writestr(member_name, payload)


def _insert_conversation_audio_reference(
    settings: Settings,
    *,
    session_id: int = 999,
    audio_hash: str,
) -> None:
    conn = sqlite3.connect(settings.data_dir / "production.db")
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute(
            """
            INSERT INTO conversation_turns (
                session_id,
                turn_index,
                user_input_mode,
                user_audio_path,
                user_audio_sha256,
                asr_status,
                error_presentation
            ) VALUES (?, 1, 'voice', ?, ?, 'success', 'none')
            """,
            (
                session_id,
                "audio/session-1/turn-1.webm",
                audio_hash,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def test_backup_includes_snapshot_and_all_referenced_audio_only(
    backup_settings: Settings,
    tmp_path: Path,
):
    output_path = tmp_path / "off-host" / "backup.zip"

    created_path = backup_data.create_backup(backup_settings, output_path=output_path)

    assert created_path == output_path
    verification = backup_data.verify_backup(output_path)
    assert verification.audio_members == ("audio/session-1/turn-1.webm",)
    with zipfile.ZipFile(output_path) as archive:
        names = set(archive.namelist())
        metadata_text = archive.read("backup_metadata.json").decode("utf-8")
        manifest = json.loads(archive.read("manifest.json"))
    assert names == {
        "app.db",
        "audio/session-1/turn-1.webm",
        "backup_metadata.json",
        "manifest.json",
    }
    assert {entry["path"] for entry in manifest["members"]} == names - {"manifest.json"}
    assert "orphan.webm" not in metadata_text
    assert "generated.zip" not in metadata_text
    assert "server.log" not in metadata_text
    for secret in (
        backup_settings.yizhan_api_key,
        backup_settings.aabao_api_key,
        backup_settings.packyapi_api_key,
        backup_settings.tencent_secret_id,
        backup_settings.tencent_secret_key,
        backup_settings.admin_password_hash,
        backup_settings.app_secret_key,
    ):
        assert secret is not None
        assert secret not in metadata_text


def test_backup_default_destination_is_outside_live_data(backup_settings: Settings):
    output_path = backup_data.default_output_path(backup_settings)

    assert not output_path.resolve().is_relative_to(backup_settings.data_dir.resolve())


def test_backup_rejects_destination_inside_live_data(backup_settings: Settings):
    unsafe_output = backup_settings.data_dir / "exports" / "backup.zip"

    with pytest.raises(backup_data.BackupError, match="outside_live_data"):
        backup_data.create_backup(backup_settings, output_path=unsafe_output)

    assert not unsafe_output.exists()


def test_backup_rejects_world_writable_nonsticky_destination_ancestor(
    backup_settings: Settings,
    tmp_path: Path,
):
    unsafe_ancestor = tmp_path / "unsafe-ancestor"
    unsafe_ancestor.mkdir()
    unsafe_ancestor.chmod(0o777)
    destination = unsafe_ancestor / "off-host" / "backup.zip"

    with pytest.raises(backup_data.BackupError, match="destination_ancestry_untrusted"):
        backup_data.create_backup(backup_settings, output_path=destination)

    assert not destination.parent.exists()


def test_backup_allows_sticky_root_style_and_service_owned_ancestry(
    backup_settings: Settings,
    tmp_path: Path,
):
    sticky_ancestor = tmp_path / "sticky-ancestor"
    sticky_ancestor.mkdir()
    sticky_ancestor.chmod(0o1777)
    destination_parent = sticky_ancestor / "service-owned"
    destination_parent.mkdir(mode=0o700)
    destination = destination_parent / "backup.zip"

    created = backup_data.create_backup(backup_settings, output_path=destination)

    assert created == destination
    assert destination.is_file()


def test_backup_native_publish_uses_private_source_dirfd_after_identity_check(
    backup_settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    destination = tmp_path / "off-host" / "backup.zip"
    original_native_publish = getattr(
        backup_data,
        "_native_publish_noreplace",
        None,
    )
    observed: dict[str, object] = {}

    def inspect_native_publish(
        source_descriptor,
        source_name,
        destination_descriptor,
        destination_name,
    ):
        source_directory = os.fstat(source_descriptor)
        observed.update(
            source_descriptor=source_descriptor,
            source_name=source_name,
            destination_descriptor=destination_descriptor,
            destination_name=destination_name,
            source_directory=source_directory,
        )
        assert original_native_publish is not None
        return original_native_publish(
            source_descriptor,
            source_name,
            destination_descriptor,
            destination_name,
        )

    monkeypatch.setattr(
        backup_data,
        "_native_publish_noreplace",
        inspect_native_publish,
        raising=False,
    )

    backup_data.create_backup(backup_settings, output_path=destination)

    source_directory = observed["source_directory"]
    assert isinstance(source_directory, os.stat_result)
    assert observed["source_descriptor"] != observed["destination_descriptor"]
    assert observed["source_name"] == "payload"
    assert observed["destination_name"] == destination.name
    assert source_directory.st_uid == os.geteuid()
    assert stat.S_IMODE(source_directory.st_mode) == 0o700


def test_backup_trusted_ancestry_blocks_parent_movement_at_native_publish(
    backup_settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    trusted_ancestor = tmp_path / "trusted-ancestor"
    destination_parent = trusted_ancestor / "off-host"
    destination_parent.mkdir(parents=True, mode=0o700)
    trusted_ancestor.chmod(0o555)
    destination = destination_parent / "backup.zip"
    moved_parent = trusted_ancestor / "moved"
    original_publish = backup_data.atomic_publish_noreplace
    original_native_publish = getattr(
        backup_data,
        "_native_publish_noreplace",
        None,
    )
    retained_parent = None
    movement_blocked = False

    def retain_parent(source, target, *args, **kwargs):
        nonlocal retained_parent
        retained_parent = kwargs["parent"]
        return original_publish(source, target, *args, **kwargs)

    def attempt_parent_movement(*args):
        nonlocal movement_blocked
        assert retained_parent is not None
        for descriptor in retained_parent.ancestry_descriptors:
            assert stat.S_ISDIR(os.fstat(descriptor).st_mode)
        try:
            destination_parent.rename(moved_parent)
        except PermissionError:
            movement_blocked = True
        else:
            pytest.fail("trusted ancestry allowed destination-parent movement")
        assert original_native_publish is not None
        return original_native_publish(*args)

    monkeypatch.setattr(
        backup_data,
        "atomic_publish_noreplace",
        retain_parent,
    )
    monkeypatch.setattr(
        backup_data,
        "_native_publish_noreplace",
        attempt_parent_movement,
        raising=False,
    )
    try:
        backup_data.create_backup(backup_settings, output_path=destination)
    finally:
        trusted_ancestor.chmod(0o700)

    assert movement_blocked is True
    assert destination.is_file()


def test_backup_retains_parent_identity_across_staging_and_publication(
    backup_settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    destination_parent = tmp_path / "off-host"
    destination_parent.mkdir()
    owned_parent = tmp_path / "off-host-owned"
    destination = destination_parent / "backup.zip"
    original_publish = backup_data.atomic_publish_noreplace
    attacker_staging: Path | None = None

    def swap_parent_before_publish(source, target, *args, **kwargs):
        nonlocal attacker_staging
        destination_parent.rename(owned_parent)
        destination_parent.symlink_to(
            backup_settings.data_dir,
            target_is_directory=True,
        )
        attacker_staging = backup_settings.data_dir / source.name
        attacker_staging.write_bytes(b"attacker-owned")
        return original_publish(source, target, *args, **kwargs)

    monkeypatch.setattr(
        backup_data,
        "atomic_publish_noreplace",
        swap_parent_before_publish,
    )

    with pytest.raises(backup_data.BackupError, match="destination_parent_changed"):
        backup_data.create_backup(backup_settings, output_path=destination)

    assert attacker_staging is not None
    assert attacker_staging.read_bytes() == b"attacker-owned"
    assert not (backup_settings.data_dir / destination.name).exists()
    preserved_staging = list(owned_parent.iterdir())
    assert len(preserved_staging) == 1
    assert preserved_staging[0].name.startswith(f".{destination.name}.")
    assert (preserved_staging[0] / "payload").is_file()


def test_backup_rejects_ancestor_symlink_swap_while_opening_destination(
    backup_settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    safe_root = tmp_path / "safe-root"
    destination_parent = safe_root / "off-host"
    destination_parent.mkdir(parents=True)
    owned_safe_root = tmp_path / "safe-root-owned"
    live_replacement_parent = backup_settings.data_dir / destination_parent.name
    live_replacement_parent.mkdir()
    destination = destination_parent / "backup.zip"
    original_os_open = backup_data.os.open
    swapped = False

    def swap_ancestor_before_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        is_legacy_parent_open = dir_fd is None and Path(path) == destination_parent
        is_component_open = dir_fd is not None and path == safe_root.name
        if not swapped and (is_legacy_parent_open or is_component_open):
            swapped = True
            safe_root.rename(owned_safe_root)
            safe_root.symlink_to(
                backup_settings.data_dir,
                target_is_directory=True,
            )
        return original_os_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(backup_data.os, "open", swap_ancestor_before_open)

    with pytest.raises(backup_data.BackupError, match="destination_parent_unsafe_type"):
        backup_data.create_backup(backup_settings, output_path=destination)

    assert swapped is True
    assert not (live_replacement_parent / destination.name).exists()
    assert list((owned_safe_root / destination_parent.name).iterdir()) == []


def test_backup_rejects_staging_name_replacement_before_publication(
    backup_settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    destination = tmp_path / "off-host" / "backup.zip"
    original_publish = backup_data.atomic_publish_noreplace
    attacker_staging: Path | None = None
    owned_staging: Path | None = None

    def replace_staging_before_publish(source, target, *args, **kwargs):
        nonlocal attacker_staging, owned_staging
        attacker_staging = source
        owned_staging = source.with_name(f"{source.name}.owned")
        source.rename(owned_staging)
        source.write_bytes(b"attacker-owned")
        return original_publish(source, target, *args, **kwargs)

    monkeypatch.setattr(
        backup_data,
        "atomic_publish_noreplace",
        replace_staging_before_publish,
    )

    with pytest.raises(backup_data.BackupError, match="staging_identity_changed"):
        backup_data.create_backup(backup_settings, output_path=destination)

    assert attacker_staging is not None
    assert attacker_staging.read_bytes() == b"attacker-owned"
    assert owned_staging is not None
    assert owned_staging.is_file()
    assert not destination.exists()


def test_verify_rejects_manifest_hash_mismatch(
    backup_settings: Settings,
    tmp_path: Path,
):
    original = backup_data.create_backup(
        backup_settings,
        output_path=tmp_path / "original.zip",
    )
    tampered = tmp_path / "tampered.zip"
    _rewrite_archive(
        original,
        tampered,
        replacements={"audio/session-1/turn-1.webm": b"tampered raw audio"},
    )

    with pytest.raises(backup_data.BackupVerificationError, match="member_hash_mismatch"):
        backup_data.verify_backup(tampered)


@pytest.mark.parametrize(
    ("omitted", "extra"),
    [
        ({"audio/session-1/turn-1.webm"}, None),
        (set(), ("exports/unexpected.zip", b"unexpected")),
    ],
)
def test_verify_rejects_missing_or_extra_members(
    backup_settings: Settings,
    tmp_path: Path,
    omitted: set[str],
    extra: tuple[str, bytes] | None,
):
    original = backup_data.create_backup(
        backup_settings,
        output_path=tmp_path / "original.zip",
    )
    invalid = tmp_path / "invalid.zip"
    _rewrite_archive(original, invalid, omitted=omitted, extra=extra)

    with pytest.raises(backup_data.BackupVerificationError, match="member_set_mismatch"):
        backup_data.verify_backup(invalid)


def test_verify_rejects_sqlite_corruption_after_valid_hashes(
    backup_settings: Settings,
    tmp_path: Path,
):
    original = backup_data.create_backup(
        backup_settings,
        output_path=tmp_path / "original.zip",
    )
    corrupt_database = b"not a sqlite database"
    with zipfile.ZipFile(original) as archive:
        manifest = json.loads(archive.read("manifest.json"))
    database_entry = next(
        entry for entry in manifest["members"] if entry["path"] == "app.db"
    )
    database_entry["size"] = len(corrupt_database)
    database_entry["sha256"] = hashlib.sha256(corrupt_database).hexdigest()
    corrupt = tmp_path / "corrupt.zip"
    _rewrite_archive(
        original,
        corrupt,
        replacements={
            "app.db": corrupt_database,
            "manifest.json": json.dumps(manifest, sort_keys=True).encode("utf-8"),
        },
    )

    with pytest.raises(backup_data.BackupVerificationError, match="sqlite_integrity_failed"):
        backup_data.verify_backup(corrupt)


def test_restore_verifies_then_atomically_publishes_non_live_destination(
    backup_settings: Settings,
    tmp_path: Path,
):
    archive_path = backup_data.create_backup(
        backup_settings,
        output_path=tmp_path / "backup.zip",
    )
    destination = tmp_path / "restore-drill" / "restored-data"

    restored_path = _restore_module().restore_backup(
        archive_path,
        destination=destination,
        live_data_dir=backup_settings.data_dir,
    )

    assert restored_path == destination
    assert (destination / "audio" / "session-1" / "turn-1.webm").read_bytes() == b"required raw audio"
    restored_conn = sqlite3.connect(destination / "app.db")
    try:
        assert restored_conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        restored_conn.close()
    assert (backup_settings.data_dir / "production.db").exists()


def test_restore_rejects_live_destination_and_corruption_without_partial_output(
    backup_settings: Settings,
    tmp_path: Path,
):
    archive_path = backup_data.create_backup(
        backup_settings,
        output_path=tmp_path / "backup.zip",
    )
    restore_module = _restore_module()

    with pytest.raises(restore_module.RestoreError, match="outside_live_data"):
        restore_module.restore_backup(
            archive_path,
            destination=backup_settings.data_dir / "restore",
            live_data_dir=backup_settings.data_dir,
        )

    corrupt = tmp_path / "corrupt.zip"
    _rewrite_archive(
        archive_path,
        corrupt,
        replacements={"audio/session-1/turn-1.webm": b"tampered raw audio"},
    )
    destination = tmp_path / "failed-restore"
    with pytest.raises(backup_data.BackupVerificationError, match="member_hash_mismatch"):
        restore_module.restore_backup(
            corrupt,
            destination=destination,
            live_data_dir=backup_settings.data_dir,
        )
    assert not destination.exists()


def test_backup_rejects_migration_drift_without_mutating_live_database(
    backup_settings: Settings,
    tmp_path: Path,
):
    conn = sqlite3.connect(backup_settings.data_dir / "production.db")
    try:
        removed_version = conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version DESC LIMIT 1"
        ).fetchone()[0]
        conn.execute(
            "DELETE FROM schema_migrations WHERE version = ?",
            (removed_version,),
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(backup_data.BackupError, match="migration_state_mismatch"):
        backup_data.create_backup(
            backup_settings,
            output_path=tmp_path / "drifted.zip",
        )

    conn = sqlite3.connect(backup_settings.data_dir / "production.db")
    try:
        assert conn.execute(
            "SELECT 1 FROM schema_migrations WHERE version = ?",
            (removed_version,),
        ).fetchone() is None
    finally:
        conn.close()


def test_backup_rejects_missing_audio_hash_evidence(
    backup_settings: Settings,
    tmp_path: Path,
):
    conn = sqlite3.connect(backup_settings.data_dir / "production.db")
    try:
        conn.execute("UPDATE asr_attempts SET user_audio_sha256 = ''")
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(backup_data.BackupError, match="audio_hash_evidence_missing"):
        backup_data.create_backup(
            backup_settings,
            output_path=tmp_path / "missing-hash.zip",
        )


def test_backup_rejects_conflicting_audio_hash_evidence(
    backup_settings: Settings,
    tmp_path: Path,
):
    _insert_conversation_audio_reference(
        backup_settings,
        audio_hash=hashlib.sha256(b"conflicting evidence").hexdigest(),
    )

    with pytest.raises(backup_data.BackupError, match="audio_hash_evidence_conflict"):
        backup_data.create_backup(
            backup_settings,
            output_path=tmp_path / "conflicting-hash.zip",
        )


def test_backup_rejects_conversation_audio_without_matching_asr_evidence(
    backup_settings: Settings,
    tmp_path: Path,
):
    expected_hash = hashlib.sha256(b"required raw audio").hexdigest()
    conn = sqlite3.connect(backup_settings.data_dir / "production.db")
    try:
        conn.execute("DELETE FROM asr_attempts")
        conn.commit()
    finally:
        conn.close()
    _insert_conversation_audio_reference(
        backup_settings,
        audio_hash=expected_hash,
    )

    with pytest.raises(backup_data.BackupError, match="audio_asr_evidence_missing"):
        backup_data.create_backup(
            backup_settings,
            output_path=tmp_path / "missing-asr-evidence.zip",
        )


def test_backup_rejects_audio_bytes_that_do_not_match_snapshot_hash(
    backup_settings: Settings,
    tmp_path: Path,
):
    audio_path = backup_settings.data_dir / "audio" / "session-1" / "turn-1.webm"
    audio_path.write_bytes(b"mismatched raw audio")

    with pytest.raises(backup_data.BackupError, match="required_audio_hash_mismatch"):
        backup_data.create_backup(
            backup_settings,
            output_path=tmp_path / "mismatched-audio.zip",
        )


def test_backup_rejects_symlinked_audio_source(
    backup_settings: Settings,
    tmp_path: Path,
):
    audio_path = backup_settings.data_dir / "audio" / "session-1" / "turn-1.webm"
    outside_audio = tmp_path / "outside.webm"
    outside_audio.write_bytes(b"required raw audio")
    audio_path.unlink()
    audio_path.symlink_to(outside_audio)

    with pytest.raises(backup_data.BackupError, match="required_audio_unsafe_type"):
        backup_data.create_backup(
            backup_settings,
            output_path=tmp_path / "symlink-audio.zip",
        )


def test_backup_hashes_the_same_audio_descriptor_it_copies(
    backup_settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    audio_path = backup_settings.data_dir / "audio" / "session-1" / "turn-1.webm"
    original_os_open = backup_data.os.open
    swapped = False

    def swap_before_leaf_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        if path == "turn-1.webm" and dir_fd is not None and not swapped:
            swapped = True
            audio_path.replace(audio_path.with_suffix(".original"))
            audio_path.write_bytes(b"swapped raw audio")
        return original_os_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(backup_data.os, "open", swap_before_leaf_open)

    with pytest.raises(backup_data.BackupError, match="required_audio_hash_mismatch"):
        backup_data.create_backup(
            backup_settings,
            output_path=tmp_path / "swapped-audio.zip",
        )
    assert swapped is True


def test_restore_uses_the_exact_archive_bytes_that_were_verified(
    backup_settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    archive_path = backup_data.create_backup(
        backup_settings,
        output_path=tmp_path / "backup.zip",
    )
    malicious_archive = tmp_path / "malicious.zip"
    with zipfile.ZipFile(malicious_archive, "w") as archive:
        archive.writestr("../outside.txt", b"outside write")
    destination = tmp_path / "restore" / "data"
    outside_path = destination.parent / "outside.txt"
    restore_module = _restore_module()
    original_verify = restore_module.verify_backup

    def verify_then_swap(path, *args, **kwargs):
        verification = original_verify(path, *args, **kwargs)
        archive_path.unlink()
        malicious_archive.replace(archive_path)
        return verification

    monkeypatch.setattr(restore_module, "verify_backup", verify_then_swap)

    restore_module.restore_backup(
        archive_path,
        destination=destination,
        live_data_dir=backup_settings.data_dir,
    )

    assert not outside_path.exists()
    assert (destination / "audio" / "session-1" / "turn-1.webm").read_bytes() == b"required raw audio"


def test_restore_rejects_intermediate_directory_swap_during_extraction(
    backup_settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    archive_path = backup_data.create_backup(
        backup_settings,
        output_path=tmp_path / "backup.zip",
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    original_os_open = backup_data.os.open
    swapped = False

    def swap_directory_before_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        if path == "audio" and dir_fd is not None and not swapped:
            swapped = True
            os.rename("audio", "audio-owned", src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
            os.symlink(outside, "audio", dir_fd=dir_fd, target_is_directory=True)
        return original_os_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(backup_data.os, "open", swap_directory_before_open)

    with pytest.raises(backup_data.BackupError, match="unsafe_archive_member_path"):
        _restore_module().restore_backup(
            archive_path,
            destination=tmp_path / "restored",
            live_data_dir=backup_settings.data_dir,
        )

    assert swapped is True
    assert not (outside / "session-1" / "turn-1.webm").exists()


def test_restore_rejects_symlinked_archive_source(
    backup_settings: Settings,
    tmp_path: Path,
):
    archive_path = backup_data.create_backup(
        backup_settings,
        output_path=tmp_path / "backup.zip",
    )
    symlink_path = tmp_path / "backup-link.zip"
    symlink_path.symlink_to(archive_path)

    with pytest.raises(backup_data.BackupError, match="backup_archive_unsafe_type"):
        _restore_module().restore_backup(
            symlink_path,
            destination=tmp_path / "restored",
            live_data_dir=backup_settings.data_dir,
        )


def test_restore_no_replace_preserves_destination_created_during_race(
    backup_settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    archive_path = backup_data.create_backup(
        backup_settings,
        output_path=tmp_path / "backup.zip",
    )
    destination = tmp_path / "restore-race"
    restore_module = _restore_module()
    original_create_staging = restore_module.create_private_staging_container

    def create_destination_after_staging(*args, **kwargs):
        staging_container = original_create_staging(*args, **kwargs)
        destination.mkdir()
        return staging_container

    monkeypatch.setattr(
        restore_module,
        "create_private_staging_container",
        create_destination_after_staging,
    )

    with pytest.raises(restore_module.RestoreError, match="destination_already_exists"):
        restore_module.restore_backup(
            archive_path,
            destination=destination,
            live_data_dir=backup_settings.data_dir,
        )

    assert destination.is_dir()
    assert list(destination.iterdir()) == []


def test_restore_rejects_group_writable_nonsticky_destination_parent(
    backup_settings: Settings,
    tmp_path: Path,
):
    archive_path = backup_data.create_backup(
        backup_settings,
        output_path=tmp_path / "backup.zip",
    )
    destination_parent = tmp_path / "unsafe-restore-parent"
    destination_parent.mkdir()
    destination_parent.chmod(0o770)
    destination = destination_parent / "restored"
    restore_module = _restore_module()

    with pytest.raises(
        restore_module.RestoreError,
        match="destination_ancestry_untrusted",
    ):
        restore_module.restore_backup(
            archive_path,
            destination=destination,
            live_data_dir=backup_settings.data_dir,
        )

    assert not destination.exists()


def test_restore_native_publish_uses_private_source_dirfd_after_identity_check(
    backup_settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    archive_path = backup_data.create_backup(
        backup_settings,
        output_path=tmp_path / "backup.zip",
    )
    destination = tmp_path / "restore-parent" / "restored"
    restore_module = _restore_module()
    original_native_publish = getattr(
        backup_data,
        "_native_publish_noreplace",
        None,
    )
    observed: dict[str, object] = {}

    def inspect_native_publish(
        source_descriptor,
        source_name,
        destination_descriptor,
        destination_name,
    ):
        source_directory = os.fstat(source_descriptor)
        observed.update(
            source_descriptor=source_descriptor,
            source_name=source_name,
            destination_descriptor=destination_descriptor,
            destination_name=destination_name,
            source_directory=source_directory,
        )
        assert original_native_publish is not None
        return original_native_publish(
            source_descriptor,
            source_name,
            destination_descriptor,
            destination_name,
        )

    monkeypatch.setattr(
        backup_data,
        "_native_publish_noreplace",
        inspect_native_publish,
        raising=False,
    )

    restore_module.restore_backup(
        archive_path,
        destination=destination,
        live_data_dir=backup_settings.data_dir,
    )

    source_directory = observed["source_directory"]
    assert isinstance(source_directory, os.stat_result)
    assert observed["source_descriptor"] != observed["destination_descriptor"]
    assert observed["source_name"] == "payload"
    assert observed["destination_name"] == destination.name
    assert source_directory.st_uid == os.geteuid()
    assert stat.S_IMODE(source_directory.st_mode) == 0o700


def test_atomic_publish_noreplace_never_replaces_existing_empty_directory(
    tmp_path: Path,
):
    source = tmp_path / "staged"
    destination = tmp_path / "destination"
    source.mkdir()
    (source / "restored.txt").write_text("restored", encoding="utf-8")
    destination.mkdir()

    with pytest.raises(FileExistsError):
        _restore_module().atomic_publish_noreplace(source, destination)

    assert destination.is_dir()
    assert list(destination.iterdir()) == []
    assert (source / "restored.txt").read_text(encoding="utf-8") == "restored"


def test_restore_retains_parent_identity_across_staging_and_publication(
    backup_settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    archive_path = backup_data.create_backup(
        backup_settings,
        output_path=tmp_path / "backup.zip",
    )
    destination_parent = tmp_path / "restore-parent"
    destination_parent.mkdir()
    owned_parent = tmp_path / "restore-parent-owned"
    destination = destination_parent / "restored"
    restore_module = _restore_module()
    original_publish = restore_module.atomic_publish_noreplace
    attacker_staging: Path | None = None

    def swap_parent_before_publish(source, target, *args, **kwargs):
        nonlocal attacker_staging
        destination_parent.rename(owned_parent)
        destination_parent.symlink_to(
            backup_settings.data_dir,
            target_is_directory=True,
        )
        attacker_staging = backup_settings.data_dir / source.name
        attacker_staging.mkdir()
        (attacker_staging / "attacker.txt").write_text(
            "attacker-owned",
            encoding="utf-8",
        )
        return original_publish(source, target, *args, **kwargs)

    monkeypatch.setattr(
        restore_module,
        "atomic_publish_noreplace",
        swap_parent_before_publish,
    )

    with pytest.raises(restore_module.RestoreError, match="destination_parent_changed"):
        restore_module.restore_backup(
            archive_path,
            destination=destination,
            live_data_dir=backup_settings.data_dir,
        )

    assert attacker_staging is not None
    assert (attacker_staging / "attacker.txt").read_text(encoding="utf-8") == "attacker-owned"
    assert not (backup_settings.data_dir / destination.name).exists()
    preserved_staging = list(owned_parent.iterdir())
    assert len(preserved_staging) == 1
    assert preserved_staging[0].name.startswith(f".{destination.name}.restore-")
    assert (preserved_staging[0] / "payload" / "app.db").is_file()


def test_restore_rejects_staging_name_replacement_before_publication(
    backup_settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    archive_path = backup_data.create_backup(
        backup_settings,
        output_path=tmp_path / "backup.zip",
    )
    destination = tmp_path / "restore-parent" / "restored"
    restore_module = _restore_module()
    original_publish = restore_module.atomic_publish_noreplace
    attacker_staging: Path | None = None
    owned_staging: Path | None = None

    def replace_staging_before_publish(source, target, *args, **kwargs):
        nonlocal attacker_staging, owned_staging
        attacker_staging = source
        owned_staging = source.with_name(f"{source.name}.owned")
        source.rename(owned_staging)
        source.mkdir()
        (source / "attacker.txt").write_text(
            "attacker-owned",
            encoding="utf-8",
        )
        return original_publish(source, target, *args, **kwargs)

    monkeypatch.setattr(
        restore_module,
        "atomic_publish_noreplace",
        replace_staging_before_publish,
    )

    with pytest.raises(restore_module.RestoreError, match="staging_identity_changed"):
        restore_module.restore_backup(
            archive_path,
            destination=destination,
            live_data_dir=backup_settings.data_dir,
        )

    assert attacker_staging is not None
    assert (attacker_staging / "attacker.txt").read_text(encoding="utf-8") == "attacker-owned"
    assert owned_staging is not None
    assert (owned_staging / "app.db").is_file()
    assert not destination.exists()


def test_restore_retains_private_container_when_its_name_is_replaced(
    backup_settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    archive_path = backup_data.create_backup(
        backup_settings,
        output_path=tmp_path / "backup.zip",
    )
    destination = tmp_path / "restore-parent" / "restored"
    outside = tmp_path / "attacker-owned"
    outside.mkdir()
    restore_module = _restore_module()
    original_create_staging = restore_module.create_private_staging_container
    attacker_staging: Path | None = None
    owned_staging: Path | None = None

    def replace_container_name_after_open(parent, *args, **kwargs):
        nonlocal attacker_staging, owned_staging
        staging_container = original_create_staging(parent, *args, **kwargs)
        attacker_staging = parent.path / staging_container.name
        owned_staging = parent.path / f"{staging_container.name}.owned"
        os.rename(
            staging_container.name,
            owned_staging.name,
            src_dir_fd=parent.descriptor,
            dst_dir_fd=parent.descriptor,
        )
        os.symlink(
            outside,
            staging_container.name,
            dir_fd=parent.descriptor,
            target_is_directory=True,
        )
        return staging_container

    monkeypatch.setattr(
        restore_module,
        "create_private_staging_container",
        replace_container_name_after_open,
    )

    restored = restore_module.restore_backup(
        archive_path,
        destination=destination,
        live_data_dir=backup_settings.data_dir,
    )

    assert restored == destination
    assert (destination / "app.db").is_file()
    assert attacker_staging is not None
    assert attacker_staging.is_symlink()
    assert attacker_staging.resolve() == outside
    assert owned_staging is not None
    assert owned_staging.is_dir()
    assert list(owned_staging.iterdir()) == []


@pytest.mark.parametrize(
    ("kind", "expected_error"),
    [
        ("traversal", "unsafe_archive_member_path"),
        ("duplicate", "duplicate_archive_member"),
        ("special", "unsafe_archive_member_type"),
    ],
)
def test_verify_rejects_traversal_duplicate_and_special_members(
    tmp_path: Path,
    kind: str,
    expected_error: str,
):
    archive_path = tmp_path / f"{kind}.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        if kind == "traversal":
            archive.writestr("../outside", b"bad")
        elif kind == "duplicate":
            archive.writestr("manifest.json", b"first")
            with pytest.warns(UserWarning, match="Duplicate name"):
                archive.writestr("manifest.json", b"second")
        else:
            special = zipfile.ZipInfo("device")
            special.create_system = 3
            special.external_attr = (stat.S_IFCHR | 0o600) << 16
            archive.writestr(special, b"bad")

    with pytest.raises(backup_data.BackupVerificationError, match=expected_error):
        backup_data.verify_backup(archive_path)


def test_verify_rejects_member_over_uncompressed_limit(
    backup_settings: Settings,
    tmp_path: Path,
):
    archive_path = backup_data.create_backup(
        backup_settings,
        output_path=tmp_path / "backup.zip",
    )
    limits = backup_data.BackupLimits(
        max_member_bytes=8,
        max_total_uncompressed_bytes=1024 * 1024,
        max_compression_ratio=1000,
    )

    with pytest.raises(backup_data.BackupVerificationError, match="archive_member_too_large"):
        backup_data.verify_backup(archive_path, limits=limits)


def test_verify_rejects_aggregate_uncompressed_limit(
    backup_settings: Settings,
    tmp_path: Path,
):
    archive_path = backup_data.create_backup(
        backup_settings,
        output_path=tmp_path / "backup.zip",
    )
    with zipfile.ZipFile(archive_path) as archive:
        total_size = sum(info.file_size for info in archive.infolist())
    limits = backup_data.BackupLimits(
        max_member_bytes=1024 * 1024 * 1024,
        max_total_uncompressed_bytes=total_size - 1,
        max_compression_ratio=1000,
    )

    with pytest.raises(backup_data.BackupVerificationError, match="archive_total_too_large"):
        backup_data.verify_backup(archive_path, limits=limits)


def test_verify_rejects_manifest_consistent_high_compression_ratio(
    backup_settings: Settings,
    tmp_path: Path,
):
    original = backup_data.create_backup(
        backup_settings,
        output_path=tmp_path / "original.zip",
    )
    high_ratio = tmp_path / "high-ratio.zip"
    with zipfile.ZipFile(original) as archive:
        metadata = archive.read("backup_metadata.json")
    _rewrite_manifest_consistent_archive(
        original,
        high_ratio,
        replacements={"backup_metadata.json": metadata + b" " * 100_000},
    )
    limits = backup_data.BackupLimits(
        max_member_bytes=1024 * 1024,
        max_total_uncompressed_bytes=1024 * 1024 * 1024,
        max_compression_ratio=10,
    )

    with pytest.raises(backup_data.BackupVerificationError, match="archive_compression_ratio_exceeded"):
        backup_data.verify_backup(high_ratio, limits=limits)


def test_verify_rejects_physical_archive_limit_before_zip_parsing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    archive_path = tmp_path / "oversized.zip"
    with archive_path.open("wb") as handle:
        handle.truncate(backup_data.ARCHIVE_OVERHEAD_ALLOWANCE + 2)
    limits = backup_data.BackupLimits(
        max_member_bytes=1,
        max_total_uncompressed_bytes=1,
        max_compression_ratio=10,
        max_central_directory_bytes=1024,
        max_members=10,
    )
    monkeypatch.setattr(
        backup_data.zipfile,
        "ZipFile",
        lambda *args, **kwargs: pytest.fail("ZipFile parsed an oversized archive"),
    )

    with pytest.raises(backup_data.BackupVerificationError, match="backup_archive_too_large"):
        backup_data.verify_backup(archive_path, limits=limits)


@pytest.mark.parametrize(
    ("limit_updates", "expected_error"),
    [
        ({"max_central_directory_bytes": 1}, "archive_central_directory_too_large"),
        ({"max_members": 1}, "archive_member_count_exceeded"),
    ],
)
def test_verify_rejects_central_directory_bounds_before_zip_parsing(
    backup_settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    limit_updates: dict[str, int],
    expected_error: str,
):
    archive_path = backup_data.create_backup(
        backup_settings,
        output_path=tmp_path / "backup.zip",
    )
    limit_values = {
        "max_member_bytes": 1024 * 1024 * 1024,
        "max_total_uncompressed_bytes": 1024 * 1024 * 1024,
        "max_compression_ratio": 1000,
        "max_central_directory_bytes": 1024 * 1024,
        "max_members": 100,
        **limit_updates,
    }
    limits = backup_data.BackupLimits(**limit_values)
    monkeypatch.setattr(
        backup_data.zipfile,
        "ZipFile",
        lambda *args, **kwargs: pytest.fail("ZipFile parsed an over-limit directory"),
    )

    with pytest.raises(backup_data.BackupVerificationError, match=expected_error):
        backup_data.verify_backup(archive_path, limits=limits)


def test_verify_counts_central_headers_instead_of_trusting_eocd_count(
    backup_settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    archive_path = backup_data.create_backup(
        backup_settings,
        output_path=tmp_path / "backup.zip",
    )
    archive_bytes = bytearray(archive_path.read_bytes())
    eocd_offset = archive_bytes.rfind(b"PK\x05\x06")
    assert eocd_offset >= 0
    struct.pack_into("<H", archive_bytes, eocd_offset + 8, 1)
    struct.pack_into("<H", archive_bytes, eocd_offset + 10, 1)
    archive_path.write_bytes(archive_bytes)
    limits = backup_data.BackupLimits(
        max_member_bytes=1024 * 1024 * 1024,
        max_total_uncompressed_bytes=1024 * 1024 * 1024,
        max_compression_ratio=1000,
        max_central_directory_bytes=1024 * 1024,
        max_members=1,
    )
    monkeypatch.setattr(
        backup_data.zipfile,
        "ZipFile",
        lambda *args, **kwargs: pytest.fail("ZipFile trusted a forged EOCD count"),
    )

    with pytest.raises(backup_data.BackupVerificationError, match="archive_member_count_exceeded"):
        backup_data.verify_backup(archive_path, limits=limits)
