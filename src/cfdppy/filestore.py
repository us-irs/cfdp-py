"""Contains the Filestore Interface and a Native Filestore implementation."""

from __future__ import annotations  # Python 3.9 compatibility for | syntax

import abc
import logging
import os
import platform
import shutil
import subprocess
from typing import TYPE_CHECKING, BinaryIO

from crcmod.predefined import PredefinedCrc
from spacepackets.cfdp.defs import NULL_CHECKSUM_U32, ChecksumType
from spacepackets.cfdp.tlv import FilestoreResponseStatusCode

from cfdppy.crc import calc_modular_checksum
from cfdppy.exceptions import ChecksumNotImplemented

if TYPE_CHECKING:
    from pathlib import Path

_LOGGER = logging.getLogger(__name__)

FilestoreResult = FilestoreResponseStatusCode


class VirtualFilestore(abc.ABC):
    """Interface for a virtual filestore implementation."""

    @abc.abstractmethod
    def read_data(self, file: Path, offset: int | None, read_len: int) -> bytes:
        """Read data from a provided Path at a specific offset.

        This is not used as part of a filestore request.
        It is used to read a file, for example to send it.

        :param file: File to read
        :param offset: Offset to read from
        :param read_len: Number of bytes to read
        :return: The read data
        :raises PermissionError: In case the file is not readable
        :raises FileNotFoundError: In case the file does not exist.
        """

    @abc.abstractmethod
    def read_from_opened_file(self, bytes_io: BinaryIO, offset: int, read_len: int) -> bytes:
        """Read data from an already opened file object.

        :param bytes_io: File object
        :param offset: Offset to read from
        :param read_len: Number of bytes to read
        :return: The read data
        """

    @abc.abstractmethod
    def is_directory(self, path: Path) -> bool:
        """Check if a given path is a directory.

        :param path: Path to check
        :return: True if the path is a directory
        """

    @abc.abstractmethod
    def filename_from_full_path(self, path: Path) -> str | None:
        """Get the filename from a full path.

        :param path: Full path
        :return: Filename
        """

    @abc.abstractmethod
    def file_exists(self, path: Path) -> bool:
        """Check if a file exists at the given path.

        :param path: Path to check
        :return: True if the file exists
        """

    @abc.abstractmethod
    def truncate_file(self, file: Path) -> None:
        """Truncate a file to zero bytes.

        :param file: File to truncate
        :raises FileNotFoundError: In case the file does not exist
        """

    @abc.abstractmethod
    def file_size(self, file: Path) -> int:
        """Get the size of a file.

        :param file: File to get the size of as number of bytes
        :return: Size of the file in bytes
        :raises FileNotFoundError: In case the file does not exist
        """

    @abc.abstractmethod
    def write_data(self, file: Path, data: bytes, offset: int | None) -> None:
        """This is not used as part of a filestore request, it is used to build up the received
        file.

        The file needs to exist before writing to it.

        :param file: File to write to
        :param data: Data to write
        :param offset: Offset to write, may be None for no offset
        :raises PermissionError: In case the file is not writable
        :raises FileNotFoundError: In case the file does not exist
        """

    @abc.abstractmethod
    def create_file(self, file: Path) -> FilestoreResponseStatusCode:
        _LOGGER.warning("Creating file not implemented in virtual filestore")
        return FilestoreResponseStatusCode.NOT_PERFORMED

    @abc.abstractmethod
    def delete_file(self, file: Path) -> FilestoreResponseStatusCode:
        _LOGGER.warning("Deleting file not implemented in virtual filestore")
        return FilestoreResponseStatusCode.NOT_PERFORMED

    @abc.abstractmethod
    def rename_file(self, _old_file: Path, _new_file: Path) -> FilestoreResponseStatusCode:
        _LOGGER.warning("Renaming file not implemented in virtual filestore")
        return FilestoreResponseStatusCode.NOT_PERFORMED

    @abc.abstractmethod
    def replace_file(self, _replaced_file: Path, _source_file: Path) -> FilestoreResponseStatusCode:
        _LOGGER.warning("Replacing file not implemented in virtual filestore")
        return FilestoreResponseStatusCode.NOT_PERFORMED

    @abc.abstractmethod
    def create_directory(self, _dir_name: Path) -> FilestoreResponseStatusCode:
        _LOGGER.warning("Creating directory not implemented in virtual filestore")
        return FilestoreResponseStatusCode.NOT_PERFORMED

    @abc.abstractmethod
    def remove_directory(self, _dir_name: Path, recursive: bool) -> FilestoreResponseStatusCode:
        _LOGGER.warning("Removing directory not implemented in virtual filestore")
        return FilestoreResponseStatusCode.NOT_PERFORMED

    @abc.abstractmethod
    def list_directory(
        self, _dir_name: Path, _file_name: Path, _recursive: bool = False
    ) -> FilestoreResponseStatusCode:
        _LOGGER.warning("Listing directory not implemented in virtual filestore")
        return FilestoreResponseStatusCode.NOT_PERFORMED

    @abc.abstractmethod
    def calculate_checksum(
        self,
        checksum_type: ChecksumType,
        file_path: Path,
        size_to_verify: int,
        segment_len: int = 4096,
    ) -> bytes:
        """Calculate the checksum for a given file.

        Raises
        -------

        ValueError
            Invalid input parameters
        FileNotFoundError
            File for checksum calculation does not exist
        """

    def verify_checksum(
        self,
        checksum: bytes,
        checksum_type: ChecksumType,
        file_path: Path,
        size_to_verify: int,
        segment_len: int = 4096,
    ) -> bool:
        return (
            self.calculate_checksum(checksum_type, file_path, size_to_verify, segment_len)
            == checksum
        )


class NativeFilestore(VirtualFilestore):
    def __init__(self):
        pass

    def read_data(self, file: Path, offset: int | None, read_len: int | None = None) -> bytes:
        if not file.exists():
            raise FileNotFoundError(file)
        file_size = self.file_size(file)
        if read_len is None:
            read_len = file_size
        if offset is None:
            offset = 0
        with open(file, "rb") as rf:
            rf.seek(offset)
            return rf.read(read_len)

    def file_size(self, file: Path) -> int:
        if not file.exists():
            raise FileNotFoundError(file)
        return file.stat().st_size

    def read_from_opened_file(self, bytes_io: BinaryIO, offset: int, read_len: int) -> bytes:
        bytes_io.seek(offset)
        return bytes_io.read(read_len)

    def file_exists(self, path: Path) -> bool:
        return path.exists()

    def is_directory(self, path: Path) -> bool:
        return path.is_dir()

    def filename_from_full_path(self, path: Path) -> str | None:
        return path.name

    def truncate_file(self, file: Path) -> None:
        if not file.exists():
            raise FileNotFoundError(file)
        with open(file, "w"):
            pass

    def write_data(self, file: Path, data: bytes, offset: int | None) -> None:
        """Primary function used to perform the CFDP Copy Procedure. This will also create a new
        file as long as no other file with the same name exists

        :return:
        :raises FileNotFoundError: File not found
        """
        if not file.exists():
            raise FileNotFoundError(file)
        with open(file, "r+b") as of:
            if offset is not None:
                of.seek(offset)
            of.write(data)

    def create_file(self, file: Path) -> FilestoreResponseStatusCode:
        """Returns CREATE_NOT_ALLOWED if the file already exists"""
        if file.exists():
            _LOGGER.warning("File already exists")
            return FilestoreResponseStatusCode.CREATE_NOT_ALLOWED
        try:
            # Creates subfolders if they do not exist
            file.parent.mkdir(exist_ok=True, parents=True)
            with open(file, "x"):
                pass
            return FilestoreResponseStatusCode.CREATE_SUCCESS
        except OSError:
            _LOGGER.exception(f"Creating file {file} failed")
            return FilestoreResponseStatusCode.CREATE_NOT_ALLOWED

    def delete_file(self, file: Path) -> FilestoreResponseStatusCode:
        if not file.exists():
            return FilestoreResponseStatusCode.DELETE_FILE_DOES_NOT_EXIST
        if file.is_dir():
            return FilestoreResponseStatusCode.DELETE_NOT_ALLOWED
        os.remove(file)
        return FilestoreResponseStatusCode.DELETE_SUCCESS

    def rename_file(self, old_file: Path, new_file: Path) -> FilestoreResponseStatusCode:
        if old_file.is_dir() or new_file.is_dir():
            _LOGGER.exception(f"{old_file} or {new_file} is a directory")
            return FilestoreResponseStatusCode.RENAME_NOT_PERFORMED
        if not old_file.exists():
            return FilestoreResponseStatusCode.RENAME_OLD_FILE_DOES_NOT_EXIST
        if new_file.exists():
            return FilestoreResponseStatusCode.RENAME_NEW_FILE_DOES_EXIST
        old_file.rename(new_file)
        return FilestoreResponseStatusCode.RENAME_SUCCESS

    def replace_file(self, replaced_file: Path, source_file: Path) -> FilestoreResponseStatusCode:
        if replaced_file.is_dir() or source_file.is_dir():
            _LOGGER.warning(f"{replaced_file} is a directory")
            return FilestoreResponseStatusCode.REPLACE_NOT_ALLOWED
        if not replaced_file.exists():
            return FilestoreResponseStatusCode.REPLACE_FILE_NAME_ONE_TO_BE_REPLACED_DOES_NOT_EXIST
        if not source_file.exists():
            return FilestoreResponseStatusCode.REPLACE_FILE_NAME_TWO_REPLACE_SOURCE_NOT_EXIST
        source_file.replace(replaced_file)
        return FilestoreResponseStatusCode.REPLACE_SUCCESS

    def remove_directory(
        self, dir_name: Path, recursive: bool = False
    ) -> FilestoreResponseStatusCode:
        if not dir_name.exists():
            _LOGGER.warning(f"{dir_name} does not exist")
            return FilestoreResponseStatusCode.REMOVE_DIR_DOES_NOT_EXIST
        if not dir_name.is_dir():
            _LOGGER.warning(f"{dir_name} is not a directory")
            return FilestoreResponseStatusCode.REMOVE_DIR_NOT_ALLOWED
        if recursive:
            shutil.rmtree(dir_name)
            return FilestoreResponseStatusCode.REMOVE_DIR_SUCCESS
        try:
            os.rmdir(dir_name)
            return FilestoreResponseStatusCode.REMOVE_DIR_SUCCESS
        except OSError:
            _LOGGER.exception(f"Removing directory {dir_name} failed")
            return FilestoreResponseStatusCode.RENAME_NOT_PERFORMED

    def create_directory(self, dir_name: Path) -> FilestoreResponseStatusCode:
        if dir_name.exists():
            # It does not really matter if the existing structure is a file or a directory
            return FilestoreResponseStatusCode.CREATE_DIR_CAN_NOT_BE_CREATED
        os.mkdir(dir_name)
        return FilestoreResponseStatusCode.CREATE_DIR_SUCCESS

    def list_directory(
        self, dir_name: Path, target_file: Path, recursive: bool = False
    ) -> FilestoreResponseStatusCode:
        """List a directory

        :param dir_name: Name of directory to list
        :param target_file: The list will be written into this target file
        :param recursive:
        :return:
        """
        if not dir_name.exists() or not dir_name.is_dir():
            _LOGGER.warning(f"{dir_name} does not exist or is not a directory")
            return FilestoreResponseStatusCode.NOT_PERFORMED

        if platform.system() == "Linux" or platform.system() == "Darwin":
            cmd = ["ls", "-al"]
        elif platform.system() == "Windows":
            cmd = ["dir"]
        else:
            _LOGGER.warning(f"Unknown OS {platform.system()}, do not know how to list directory")
            return FilestoreResponseStatusCode.NOT_PERFORMED

        open_flag = "a" if target_file.exists() else "w"
        with open(target_file, open_flag) as of:
            of.write(f"Contents of directory {dir_name} generated with '{cmd}':\n")
            try:
                # cmd is not modified by user input and dir_name has been checked above
                result = subprocess.run(cmd, check=True, capture_output=True, cwd=dir_name)  # noqa S603
            except (subprocess.CalledProcessError, OSError) as e:
                _LOGGER.error(f"Failed to list directory {dir_name}: {e}")
                return FilestoreResponseStatusCode.NOT_PERFORMED
            of.write(result.stdout.decode())
        return FilestoreResponseStatusCode.SUCCESS

    def _verify_checksum(self, checksum_type: ChecksumType) -> None:
        if checksum_type not in [
            ChecksumType.CRC_32,
            ChecksumType.CRC_32C,
        ]:
            raise ChecksumNotImplemented(checksum_type)

    def checksum_type_to_crcmod_str(self, checksum_type: ChecksumType) -> str | None:
        if checksum_type == ChecksumType.CRC_32:
            return "crc32"
        if checksum_type == ChecksumType.CRC_32C:
            return "crc32c"
        raise ChecksumNotImplemented(checksum_type)

    def _generate_crc_calculator(self, checksum_type: ChecksumType) -> PredefinedCrc:
        self._verify_checksum(checksum_type)
        return PredefinedCrc(self.checksum_type_to_crcmod_str(checksum_type))

    def calculate_checksum(
        self,
        checksum_type: ChecksumType,
        file_path: Path,
        size_to_verify: int,
        segment_len: int = 4096,
    ) -> bytes:
        if checksum_type == ChecksumType.NULL_CHECKSUM:
            return NULL_CHECKSUM_U32
        if not file_path.exists():
            raise FileNotFoundError(file_path)
        if checksum_type == ChecksumType.MODULAR:
            return calc_modular_checksum(file_path)
        if segment_len == 0:
            raise ValueError("segment length can not be 0")
        crc_obj = self._generate_crc_calculator(checksum_type)
        current_offset = 0
        # Calculate the file CRC
        with open(file_path, "rb") as file:
            while current_offset < size_to_verify:
                read_len = min(segment_len, size_to_verify - current_offset)
                if read_len > 0:
                    crc_obj.update(self.read_from_opened_file(file, current_offset, read_len))
                current_offset += read_len
            return crc_obj.digest()


HostFilestore = NativeFilestore
