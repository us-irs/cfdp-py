"""Wrapper to restrict filestore access to a specific directory.

This class will limit the filestore access to a specific directory.
All relative paths will be relative to this directory.
All absolute paths will be converted to subpaths of the restricted path e.g.
    /tmp/file.txt -> /restricted_path/tmp/file.txt

This is not a security feature but a convenience feature to limit filestore
access to a specific directory.
"""

from __future__ import annotations  # Python 3.9 compatibility for | syntax

from typing import TYPE_CHECKING

from cfdppy.filestore import NativeFilestore

if TYPE_CHECKING:
    from pathlib import Path

    from spacepackets.cfdp import ChecksumType, FilestoreResponseStatusCode


class RestrictedFilestore(NativeFilestore):
    """Wrapper to restrict filestore access to a specific directory."""

    def __init__(self, restricted_path: Path):
        """Create a new RestrictedFilestore instance.

        The path is used to restrict all paths as relative to this path.
        Absolute paths will be converted to subpaths of the restricted path
         keeping the original path structure.

        :param restricted_path: Path to restrict the filestore to
        """
        super().__init__()
        self.restricted_path = restricted_path

    def __make_local(self, file: Path) -> Path:
        """Make file paths subfolders of the restricted path.

        :param file: File to make relative to the restricted path
        :return: New Path
        """
        if not file.is_relative_to(self.restricted_path):
            if file.is_absolute():
                return self.restricted_path.joinpath(file.relative_to(file.anchor))
            return self.restricted_path.joinpath(file)
        return file

    def read_data(self, file: Path, offset: int | None, read_len: int | None = None) -> bytes:
        """Read data from file."""
        return super().read_data(self.__make_local(file), offset, read_len)

    def is_directory(self, path: Path) -> bool:
        """Check if path is a directory."""
        return super().is_directory(self.__make_local(path))

    def filename_from_full_path(self, path: Path) -> str | None:
        """Get filename from full path."""
        return super().filename_from_full_path(self.__make_local(path))

    def file_exists(self, path: Path) -> bool:
        """Check if file exists."""
        return super().file_exists(self.__make_local(path))

    def truncate_file(self, file: Path) -> None:
        """Truncate file."""
        return super().truncate_file(self.__make_local(file))

    def file_size(self, file: Path) -> int:
        """Get file size."""
        return super().file_size(self.__make_local(file))

    def write_data(self, file: Path, data: bytes, offset: int | None) -> None:
        """Write data to file."""
        return super().write_data(self.__make_local(file), data, offset)

    def create_file(self, file: Path) -> FilestoreResponseStatusCode:
        """Create file."""
        return super().create_file(self.__make_local(file))

    def delete_file(self, file: Path) -> FilestoreResponseStatusCode:
        """Delete file."""
        return super().delete_file(self.__make_local(file))

    def rename_file(self, _old_file: Path, _new_file: Path) -> FilestoreResponseStatusCode:
        """Rename file."""
        return super().rename_file(self.__make_local(_old_file), self.__make_local(_new_file))

    def replace_file(self, _replaced_file: Path, _source_file: Path) -> FilestoreResponseStatusCode:
        """Replace file."""
        return super().replace_file(
            self.__make_local(_replaced_file), self.__make_local(_source_file)
        )

    def create_directory(self, _dir_name: Path) -> FilestoreResponseStatusCode:
        """Create directory."""
        return super().create_directory(self.__make_local(_dir_name))

    def remove_directory(
        self, dir_name: Path, recursive: bool = False
    ) -> FilestoreResponseStatusCode:
        """Remove directory."""
        return super().remove_directory(dir_name=self.__make_local(dir_name), recursive=recursive)

    def list_directory(
        self, _dir_name: Path, _file_name: Path, _recursive: bool = False
    ) -> FilestoreResponseStatusCode:
        """List directory contents."""
        return super().list_directory(
            self.__make_local(_dir_name), self.__make_local(_file_name), _recursive
        )

    def calculate_checksum(
        self,
        checksum_type: ChecksumType,
        file_path: Path,
        size_to_verify: int,
        segment_len: int = 4096,
    ) -> bytes:
        """Calculate checksum of file.

        :param checksum_type: Type of checksum
        :param file_path: Path to file
        :param size_to_verify: Size to check in bytes
        :param segment_len: Length of segments to calculate checksum for
        :return: checksum as bytes
        """
        return super().calculate_checksum(
            checksum_type, self.__make_local(file_path), size_to_verify, segment_len
        )

    def verify_checksum(
        self,
        checksum: bytes,
        checksum_type: ChecksumType,
        file_path: Path,
        size_to_verify: int,
        segment_len: int = 4096,
    ) -> bool:
        """Verify checksum of file."""
        return super().verify_checksum(
            checksum=checksum,
            checksum_type=checksum_type,
            file_path=self.__make_local(file_path),
            size_to_verify=size_to_verify,
            segment_len=segment_len,
        )
