import os.path
from pathlib import Path
import tempfile

from pyfakefs.fake_filesystem_unittest import TestCase
from cfdppy.filestore import HostFilestore, FilestoreResult


class TestCfdpHostFilestore(TestCase):
    def setUp(self):
        self.setUpPyfakefs()
        self.temp_dir = tempfile.gettempdir()
        self.test_file_name_0 = Path(f"{self.temp_dir}/cfdp_unittest0.txt")
        self.test_file_name_1 = Path(f"{self.temp_dir}/cfdp_unittest1.txt")
        self.test_dir_name_0 = Path(f"{self.temp_dir}/cfdp_test_folder0")
        self.test_dir_name_1 = Path(f"{self.temp_dir}/cfdp_test_folder1")
        self.test_list_dir_name = Path(f"{self.temp_dir}/list-dir-test.txt")
        self.filestore = HostFilestore()

    def test_creation(self):
        res = self.filestore.create_file(self.test_file_name_0)
        self.assertTrue(res == FilestoreResult.CREATE_SUCCESS)
        self.assertTrue(self.test_file_name_0.exists())
        res = self.filestore.create_file(self.test_file_name_0)
        self.assertEqual(res, FilestoreResult.CREATE_NOT_ALLOWED)

        res = self.filestore.delete_file(self.test_file_name_0)
        self.assertEqual(res, FilestoreResult.DELETE_SUCCESS)
        self.assertFalse(os.path.exists(self.test_file_name_0))
        res = self.filestore.delete_file(self.test_file_name_0)
        self.assertTrue(res == FilestoreResult.DELETE_FILE_DOES_NOT_EXIST)

    def test_rename(self):
        self.filestore.create_file(self.test_file_name_0)
        res = self.filestore.rename_file(self.test_file_name_0, self.test_file_name_1)
        self.assertTrue(res == FilestoreResult.RENAME_SUCCESS)
        self.assertTrue(os.path.exists(self.test_file_name_1))
        self.assertFalse(os.path.exists(self.test_file_name_0))
        res = self.filestore.delete_file(self.test_file_name_1)
        self.assertTrue(res == FilestoreResult.DELETE_SUCCESS)

    def test_create_dir(self):
        res = self.filestore.create_directory(self.test_file_name_0)
        self.assertTrue(res == FilestoreResult.CREATE_DIR_SUCCESS)
        self.assertTrue(os.path.isdir(self.test_file_name_0))
        res = self.filestore.create_directory(self.test_file_name_0)
        self.assertTrue(res == FilestoreResult.CREATE_DIR_CAN_NOT_BE_CREATED)

        res = self.filestore.delete_file(self.test_file_name_0)
        self.assertTrue(res == FilestoreResult.DELETE_NOT_ALLOWED)
        res = self.filestore.remove_directory(self.test_file_name_0)
        self.assertTrue(res == FilestoreResult.REMOVE_DIR_SUCCESS)

    def test_read_file(self):
        file_data = "Hello World".encode()
        with open(self.test_file_name_0, "wb") as of:
            of.write(file_data)
        data = self.filestore.read_data(self.test_file_name_0, 0)
        self.assertEqual(data, file_data)

    def test_read_opened_file(self):
        file_data = "Hello World".encode()
        with open(self.test_file_name_0, "wb") as of:
            of.write(file_data)
        with open(self.test_file_name_0, "rb") as rf:
            data = self.filestore.read_from_opened_file(rf, 0, len(file_data))
            self.assertEqual(data, file_data)

    def test_write_file(self):
        file_data = "Hello World".encode()
        self.filestore.create_file(self.test_file_name_0)
        self.filestore.write_data(self.test_file_name_0, file_data, 0)
        with open(self.test_file_name_0, "rb") as rf:
            self.assertEqual(rf.read(), file_data)

    def test_replace_file(self):
        file_data = "Hello World".encode()
        self.filestore.create_file(self.test_file_name_0)
        self.filestore.write_data(self.test_file_name_0, file_data, 0)
        with open(self.test_file_name_1, "w"):
            pass
        self.filestore.replace_file(self.test_file_name_1, self.test_file_name_0)
        self.assertEqual(self.filestore.read_data(self.test_file_name_1, 0), file_data)

    def test_list_dir(self):
        filestore = HostFilestore()
        tempdir = Path(tempfile.gettempdir())
        if os.path.exists(self.test_list_dir_name):
            os.remove(self.test_list_dir_name)
        # Do not delete, user can check file content now
        res = filestore.list_directory(
            dir_name=tempdir, target_file=self.test_list_dir_name
        )
        self.assertTrue(res == FilestoreResult.SUCCESS)

    def tearDown(self):
        pass
