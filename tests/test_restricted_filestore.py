import tempfile
from pathlib import Path
from unittest import TestCase

from crcmod.predefined import PredefinedCrc
from spacepackets.cfdp import ChecksumType, FilestoreResponseStatusCode

from cfdppy import RestrictedFilestore
from cfdppy.crc import calc_modular_checksum


class TestFileSystem(TestCase):
    def test_handle_files(self):
        with tempfile.TemporaryDirectory() as tempdir:
            filestore = RestrictedFilestore(restricted_path=Path(tempdir))
            file_path = Path(tempdir) / "test_file.txt"
            file_path.write_text("test")
            self.assertTrue(filestore.file_exists(file_path))
            self.assertEqual(4, filestore.file_size(file_path))
            self.assertEqual("test_file.txt", filestore.filename_from_full_path(file_path))
            self.assertFalse(filestore.is_directory(file_path))
            self.assertEqual(b"test", filestore.read_data(file_path, offset=None))
            filestore.truncate_file(file_path)
            filestore.write_data(file_path, data=b"new", offset=None)
            self.assertEqual("new", file_path.read_text())
            filestore.truncate_file(file_path)
            self.assertEqual("", file_path.read_text())

            # Test create new file
            result = filestore.create_file(Path("new_file.txt"))
            self.assertEqual(result, FilestoreResponseStatusCode.SUCCESS)
            self.assertTrue(Path(tempdir).joinpath("new_file.txt").exists())
            self.assertEqual(0, filestore.file_size(Path("new_file.txt")))
            # Rename
            result = filestore.rename_file(Path("new_file.txt"), Path("renamed_file.txt"))
            self.assertEqual(result, FilestoreResponseStatusCode.RENAME_SUCCESS)
            self.assertTrue(Path(tempdir).joinpath("renamed_file.txt").exists())
            # Replace
            result = filestore.replace_file(Path("renamed_file.txt"), Path("test_file.txt"))
            self.assertEqual(FilestoreResponseStatusCode.REPLACE_SUCCESS, result)
            # Delete
            result = filestore.delete_file(Path("renamed_file.txt"))
            self.assertEqual(result, FilestoreResponseStatusCode.DELETE_SUCCESS)
            self.assertFalse(Path(tempdir).joinpath("renamed_file.txt").exists())

    def test_create_folder_with_file(self):
        with tempfile.TemporaryDirectory() as tempdir:
            filestore = RestrictedFilestore(restricted_path=Path(tempdir))
            new_dir = Path(tempdir).joinpath("new_dir")
            self.assertFalse(new_dir.exists())
            result = filestore.create_file(new_dir.joinpath("a_file.txt"))
            self.assertEqual(FilestoreResponseStatusCode.CREATE_SUCCESS, result)
            self.assertTrue(new_dir.exists())
            self.assertTrue(new_dir.joinpath("a_file.txt").exists())

            # Create more than one folder
            first_folder = Path(tempdir).joinpath("first_folder")
            second_folder = first_folder.joinpath("second_folder")
            self.assertFalse(first_folder.exists())
            self.assertFalse(second_folder.exists())
            result = filestore.create_file(second_folder.joinpath("a_file.txt"))
            self.assertEqual(FilestoreResponseStatusCode.CREATE_SUCCESS, result)
            self.assertTrue(first_folder.exists())
            self.assertTrue(second_folder.exists())
            self.assertTrue(second_folder.joinpath("a_file.txt").exists())

    def test_handle_directories(self):
        with tempfile.TemporaryDirectory() as tempdir:
            filestore = RestrictedFilestore(restricted_path=Path(tempdir))
            dir_path = Path(tempdir) / "test_dir"
            dir_path.mkdir()
            self.assertTrue(filestore.is_directory(Path("test_dir")))
            self.assertEqual(
                FilestoreResponseStatusCode.REMOVE_DIR_SUCCESS,
                filestore.remove_directory(Path("test_dir"), recursive=False),
            )

            self.assertEqual(
                FilestoreResponseStatusCode.CREATE_DIR_SUCCESS,
                filestore.create_directory(Path("new_dir")),
            )
            self.assertTrue(Path(tempdir).joinpath("new_dir").exists())
            self.assertEqual(
                FilestoreResponseStatusCode.REMOVE_DIR_SUCCESS,
                filestore.remove_directory(Path("new_dir")),
            )
            self.assertFalse(Path(tempdir).joinpath("new_dir").exists())
            # Test list directory

            filestore.create_directory(Path("test_dir"))
            file_path = Path(tempdir).joinpath("test_dir").joinpath("should_be_in_list.txt")
            file_path.write_text(data="test")
            self.assertEqual(
                FilestoreResponseStatusCode.SUCCESS,
                filestore.list_directory(Path("test_dir"), Path("test_list.txt")),
            )
            data = filestore.read_data(Path("test_list.txt"), offset=None)
            self.assertIn("should_be_in_list.txt", data.decode())

    def test_absolute(self):
        with tempfile.TemporaryDirectory() as tempdir:
            filestore = RestrictedFilestore(restricted_path=Path(tempdir))
            with self.assertRaises(FileNotFoundError):
                filestore.read_data(Path(__file__), offset=None)

    def test_checksum(self):
        with tempfile.TemporaryDirectory() as tempdir:
            filestore = RestrictedFilestore(restricted_path=Path(tempdir))
            filestore.create_file(Path("test_file.txt"))
            filestore.write_data(Path("test_file.txt"), data=b"test", offset=None)

            crc = PredefinedCrc(crc_name="crc32")
            checksum = crc.new(b"test").digest()

            self.assertTrue(
                filestore.verify_checksum(
                    checksum=checksum,
                    checksum_type=ChecksumType.CRC_32,
                    file_path=Path("test_file.txt"),
                    size_to_verify=10,
                )
            )
            self.assertTrue(
                filestore.verify_checksum(
                    checksum=checksum,
                    checksum_type=ChecksumType.CRC_32,
                    file_path=Path(tempdir).joinpath("test_file.txt"),
                    size_to_verify=10,
                )
            )
            self.assertEqual(
                checksum,
                filestore.calculate_checksum(
                    checksum_type=ChecksumType.CRC_32,
                    file_path=Path("test_file.txt"),
                    size_to_verify=10,
                ),
            )
            self.assertFalse(
                filestore.verify_checksum(
                    checksum=b"NotRight",
                    checksum_type=ChecksumType.CRC_32,
                    file_path=Path("test_file.txt"),
                    size_to_verify=10,
                )
            )

            # No existing file
            with self.assertRaises(FileNotFoundError):
                filestore.calculate_checksum(
                    checksum_type=ChecksumType.CRC_32,
                    file_path=Path("no_file.txt"),
                    size_to_verify=10,
                )

            # No checksum
            self.assertEqual(
                bytes([0x0] * 4),
                filestore.calculate_checksum(
                    checksum_type=ChecksumType.NULL_CHECKSUM,
                    file_path=Path("test_file.txt"),
                    size_to_verify=10,
                ),
            )
            modular_checksum = calc_modular_checksum(Path(tempdir).joinpath("test_file.txt"))

            self.assertTrue(
                filestore.verify_checksum(
                    checksum=modular_checksum,
                    checksum_type=ChecksumType.MODULAR,
                    file_path=Path("test_file.txt"),
                    size_to_verify=10,
                )
            )
