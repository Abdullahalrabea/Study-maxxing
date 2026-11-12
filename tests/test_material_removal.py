import json
from pathlib import Path
import unittest
from unittest.mock import patch

from PyQt6.QtCore import QItemSelectionModel
from PyQt6.QtWidgets import QApplication

import test_material_library as fixtures
from test_material_library import make_pdf
from material_library import MaterialLibrary, MaterialError
import material_library_dialog as dialog_module
import study_planner_panel as panel_module


class MaterialRemovalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(["test-material-removal"])

    def setUp(self):
        fixtures.MaterialLibraryTests.setUp(self)
        self.first = make_pdf(self.root / "first.pdf", "First PDF")
        self.second = make_pdf(self.root / "second.pdf", "Second PDF")
        self.paths = self.library.import_files("A", [self.first, self.second])

    def tearDown(self):
        fixtures.MaterialLibraryTests.tearDown(self)

    def test_remove_one_pdf_survives_restart_and_old_saved_references(self):
        old_packet = self.library.prepare_pdf("A", self.paths)
        self.assertEqual(self.library.remove_files("A", [self.paths[0]]), 1)
        restarted = MaterialLibrary(self.library.root)
        paths, missing = restarted.resolve("A", [self.first, self.second] + self.paths)
        self.assertEqual(paths, self.paths[1:])
        self.assertEqual(missing, [])
        self.assertEqual([item["name"] for item in restarted.materials("A")], ["second.pdf"])
        self.assertNotEqual(restarted.prepare_pdf("A", paths), old_packet)
        self.assertTrue(Path(self.first).is_file())
        Path(self.first).unlink()
        self.assertEqual(restarted.resolve("A", [self.first] + self.paths), (self.paths[1:], []))

    def test_remove_all_pdfs_does_not_restore_them_automatically(self):
        self.library.remove_files("A", self.paths)
        self.assertEqual(self.library.resolve("A", [self.first, self.second] + self.paths), ([], []))
        self.assertEqual(self.library.materials("A"), [])
        self.assertEqual(self.library.remove_files("A", self.paths), 0)

    def test_removal_is_limited_to_the_selected_course(self):
        other = self.library.import_files("B", [self.first])
        self.library.remove_files("A", [self.paths[0]])
        self.assertEqual(self.library.resolve("B")[0], other)
        with self.assertRaises(MaterialError):
            self.library.remove_files("A", other)
        self.assertEqual(self.library.resolve("B")[0], other)

    def test_old_reference_under_another_name_does_not_restore_removed_content(self):
        alias = self.root / "renamed.pdf"
        alias.write_bytes(Path(self.first).read_bytes())
        self.library.remove_files("A", [self.paths[0]])
        self.assertEqual(self.library.resolve("A", [str(alias)])[0], self.paths[1:])

    def test_explicit_upload_can_restore_a_removed_pdf(self):
        self.library.set_chapters("A", self.paths[0], "2-3")
        self.library.remove_files("A", [self.paths[0]])
        self.assertEqual(self.library.import_files("A", [self.first]), self.paths[:1])
        self.assertEqual(self.library.resolve("A")[0], self.paths)
        self.assertEqual(self.library.materials("A")[0]["chapters"], [2, 3])

    def test_daily_lookup_discards_topics_from_removed_material(self):
        self.settings.setValue(panel_module._config_key("A", "Quiz"), json.dumps({
            "material_paths": self.paths, "topics": [{"topic": "Removed PDF", "synopsis": "Old content"}],
        }))
        self.library.remove_files("A", [self.paths[0]])
        pdf, topics, session = panel_module.find_todays_material(
            None, session={"course": "A", "field": "Quiz"}, library=self.library
        )
        self.assertEqual(pdf, self.paths[1])
        self.assertEqual(topics, [])
        self.assertEqual(session["material_paths"], self.paths[1:])

    def test_remove_selected_button_handles_multiple_rows_and_refresh(self):
        third = make_pdf(self.root / "third.pdf", "Third PDF")
        self.library.import_files("A", [third])
        self.settings.setValue(panel_module._config_key("A", "Quiz"), json.dumps({"material_paths": self.paths}))
        with patch.object(dialog_module, "QSettings", return_value=self.settings):
            dialog = dialog_module.MaterialLibraryDialog(courses=["A", "B"], current_course="A", library=self.library)
        changed = []
        dialog.materials_changed.connect(changed.append)
        try:
            self.assertFalse(dialog.remove_button.isEnabled())
            selection = dialog.table.selectionModel()
            flags = QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows
            selection.select(dialog.table.model().index(0, 0), flags)
            selection.select(dialog.table.model().index(2, 0), flags)
            self.assertTrue(dialog.remove_button.isEnabled())
            dialog.remove_button.click()
            self.assertEqual(changed, ["A"])
            self.assertEqual(dialog.table.rowCount(), 1)
            self.assertEqual(dialog.table.item(0, 0).text(), "second.pdf")
            self.assertFalse(dialog.remove_button.isEnabled())
            dialog._load_course()
            self.assertEqual(dialog.table.rowCount(), 1)
        finally:
            dialog.close()
            dialog.deleteLater()
            self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
