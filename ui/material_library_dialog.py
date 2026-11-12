"""Upload and organise persistent material separately for each course."""

import json

from PyQt6.QtCore import Qt, QSettings, QSignalBlocker, pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QHeaderView, QLabel,
    QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from material_library import MaterialLibrary, MaterialError


class MaterialLibraryDialog(QDialog):
    materials_changed = pyqtSignal(str)

    def __init__(self, courses=(), current_course="", library=None, parent=None):
        super().__init__(parent)
        self.library = library or MaterialLibrary()
        self.settings = QSettings("StudyWarden", "StudyWarden")
        self.setWindowTitle("Course materials")
        self.resize(760, 500)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Choose a course, then upload its slides and notes."))
        names = set(courses) | set(self.library.courses())
        names.update(key[len("study_plan/"):].rsplit("|", 1)[0]
                     for key in self.settings.allKeys() if key.startswith("study_plan/"))
        if current_course:
            names.add(current_course)
        self.course_combo = QComboBox()
        self.course_combo.setEditable(True)
        self.course_combo.addItems(sorted(names, key=str.casefold))
        self.course_combo.setCurrentText(current_course or self.course_combo.currentText())
        self.course_combo.lineEdit().setPlaceholderText("Course name, e.g. CS320")
        layout.addWidget(self.course_combo)
        self.upload_button = QPushButton("Upload material...")
        self.upload_button.clicked.connect(self._upload)
        layout.addWidget(self.upload_button)
        info = QLabel(
            "Files are saved in the app, so moving the originals won't break your study sessions. "
            "Optionally assign chapters (e.g. 2 or 2-4) so a day's plan opens the matching files. "
            "Files without chapter numbers stay available for every session in this course."
        )
        info.setWordWrap(True)
        layout.addWidget(info)
        self.table = QTableWidget(0, 2)
        self._table_course = ""
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setHorizontalHeaderLabels(["Material", "Chapter(s), optional"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.table.itemChanged.connect(self._chapter_changed)
        layout.addWidget(self.table, 1)
        self.remove_button = QPushButton("Remove selected files")
        self.remove_button.setAutoDefault(False)
        self.remove_button.setToolTip("Remove the selected files from this course's study material")
        self.remove_button.setEnabled(False)
        self.remove_button.clicked.connect(self._remove_selected)
        self.table.itemSelectionChanged.connect(self._update_remove_button)
        layout.addWidget(self.remove_button)
        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.course_combo.currentTextChanged.connect(self._load_course)
        self._load_course()

    def _load_course(self, *_):
        course = self.course_combo.currentText().strip()
        missing = []
        try:
            for key in self.settings.allKeys():
                if key.startswith("study_plan/") and key[len("study_plan/"):].rsplit("|", 1)[0] == course:
                    try:
                        stored = json.loads(self.settings.value(key))
                        paths = stored.get("material_paths") or [stored.get("material_path")]
                        _, lost = self.library.resolve(course, [path for path in paths if path])
                        missing.extend(lost)
                    except (TypeError, ValueError, AttributeError):
                        continue
            records = self.library.materials(course)
        except (MaterialError, OSError) as error:
            self.table.setRowCount(0)
            self.status_label.setText(str(error))
            return
        self._table_course = course
        with QSignalBlocker(self.table):
            self.table.setRowCount(0)
            for record in records:
                row = self.table.rowCount()
                self.table.insertRow(row)
                name = QTableWidgetItem(record["name"])
                name.setFlags(name.flags() & ~Qt.ItemFlag.ItemIsEditable)
                name.setData(Qt.ItemDataRole.UserRole, record["path"])
                name.setToolTip(record["path"])
                self.table.setItem(row, 0, name)
                chapters = record.get("chapters")
                text = "" if not chapters else (str(chapters[0]) if chapters[0] == chapters[1]
                                                  else f"{chapters[0]}-{chapters[1]}")
                self.table.setItem(row, 1, QTableWidgetItem(text))
        self.upload_button.setEnabled(bool(course))
        self._update_remove_button()
        self.status_label.setText(
            "Earlier uploads could not be found. Upload these files again: " + ", ".join(dict.fromkeys(missing))
            if missing else f"{len(records)} file(s) saved for {course}." if course else "Enter a course name to get started."
        )

    def _upload(self):
        course = self.course_combo.currentText().strip()
        paths, _ = QFileDialog.getOpenFileNames(
            self, f"Upload material for {course}", "", "Documents (*.pdf *.docx *.txt *.jpg *.jpeg *.png)"
        )
        if not paths:
            return
        try:
            self.library.import_files(course, paths)
        except (MaterialError, OSError) as error:
            self.status_label.setText(str(error))
            return
        self._load_course()
        self.materials_changed.emit(course)

    def _update_remove_button(self):
        self.remove_button.setEnabled(
            self.course_combo.currentText().strip() == self._table_course
            and bool(self.table.selectionModel().selectedRows())
        )

    def _remove_selected(self):
        course = self.course_combo.currentText().strip()
        if course != self._table_course:
            return
        paths = [self.table.item(index.row(), 0).data(Qt.ItemDataRole.UserRole)
                 for index in self.table.selectionModel().selectedRows()]
        if not paths:
            return
        try:
            removed = self.library.remove_files(course, paths)
        except (MaterialError, OSError) as error:
            self.status_label.setText(str(error))
            return
        self._load_course()
        self.materials_changed.emit(course)
        self.status_label.setText(f"Removed {removed} file(s) from {course}.")

    def _chapter_changed(self, item):
        if item.column() != 1:
            return
        course = self.course_combo.currentText().strip()
        path = self.table.item(item.row(), 0).data(Qt.ItemDataRole.UserRole)
        try:
            self.library.set_chapters(course, path, item.text())
        except (MaterialError, OSError) as error:
            self.status_label.setText(str(error))
            return
        self.status_label.setText("Chapter assignment saved.")
        self.materials_changed.emit(course)
