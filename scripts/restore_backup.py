from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.settings import get_settings

from backup_data import (
    BackupError,
    BackupLimits,
    DestinationParentChangedError,
    PrivateStagingContainer,
    SourceIdentityChangedError,
    atomic_publish_noreplace,
    close_destination_parent,
    create_private_staging_container,
    destination_exists_at,
    extract_archive_members,
    open_destination_parent,
    owned_archive_copy,
    remove_empty_private_staging_container,
    verify_backup,
)


class RestoreError(RuntimeError):
    pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify and restore an operational backup to a non-live destination.",
    )
    parser.add_argument("archive", type=Path, help="Backup archive to restore.")
    parser.add_argument(
        "--destination",
        type=Path,
        required=True,
        help="New, non-live data directory to publish after verification.",
    )
    return parser.parse_args()


def _is_within(path: Path, root: Path) -> bool:
    return path.resolve().is_relative_to(root.resolve())


def restore_backup(
    archive_path: Path,
    *,
    destination: Path,
    live_data_dir: Path,
    limits: BackupLimits | None = None,
) -> Path:
    target = destination.expanduser()
    if not target.is_absolute():
        target = PROJECT_ROOT / target
    if _is_within(target, live_data_dir):
        raise RestoreError("restore_destination_must_be_outside_live_data")
    if target.exists():
        raise RestoreError("restore_destination_already_exists")

    active_limits = limits or BackupLimits()
    with owned_archive_copy(archive_path, limits=active_limits) as owned_archive:
        verify_backup(owned_archive, limits=active_limits)

        try:
            parent = open_destination_parent(
                target,
                live_data_dir=live_data_dir,
            )
        except BackupError as exc:
            raise RestoreError(str(exc)) from exc
        staging_container: PrivateStagingContainer | None = None
        staging_descriptor: int | None = None
        staging_identity: tuple[int, int] | None = None
        published = False
        try:
            if destination_exists_at(parent, target.name):
                raise RestoreError("restore_destination_already_exists")
            staging_container = create_private_staging_container(
                parent,
                prefix=f".{target.name}.restore-staging-",
            )
            staging_name = "payload"
            os.mkdir(
                staging_name,
                mode=0o700,
                dir_fd=staging_container.descriptor,
            )
            staging_path = parent.path / staging_container.name / staging_name
            directory_flags = (
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0)
            )
            staging_descriptor = os.open(
                staging_name,
                directory_flags,
                dir_fd=staging_container.descriptor,
            )
            staging_stat = os.fstat(staging_descriptor)
            staging_identity = (staging_stat.st_dev, staging_stat.st_ino)
            extract_archive_members(
                owned_archive,
                destination=staging_path,
                limits=active_limits,
                destination_descriptor=staging_descriptor,
            )
            try:
                atomic_publish_noreplace(
                    staging_path,
                    target,
                    parent=parent,
                    expected_source_identity=staging_identity,
                    source_parent_descriptor=staging_container.descriptor,
                )
            except DestinationParentChangedError as exc:
                raise RestoreError("restore_destination_parent_changed") from exc
            except SourceIdentityChangedError as exc:
                raise RestoreError("restore_staging_identity_changed") from exc
            except FileExistsError as exc:
                raise RestoreError("restore_destination_already_exists") from exc
            except OSError as exc:
                raise RestoreError("restore_publication_failed") from exc
            published = True
        except (BackupError, RestoreError):
            raise
        except OSError as exc:
            raise RestoreError("restore_publication_failed") from exc
        finally:
            if staging_descriptor is not None:
                os.close(staging_descriptor)
            if staging_container is not None:
                if published:
                    remove_empty_private_staging_container(
                        parent,
                        staging_container,
                    )
                os.close(staging_container.descriptor)
            close_destination_parent(parent)
    return target


def main() -> int:
    args = parse_args()
    try:
        settings = get_settings()
        destination = restore_backup(
            args.archive,
            destination=args.destination,
            live_data_dir=settings.data_dir,
            limits=BackupLimits.from_settings(settings),
        )
        print(f"backup restored and verified: {destination}")
        return 0
    except (BackupError, RestoreError) as exc:
        print(f"restore failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
