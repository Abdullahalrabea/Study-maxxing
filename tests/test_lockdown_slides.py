import contextlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import test_material_library as fixtures
import fitz
from PyQt6.QtCore import QSettings
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QWidget

import lockdown_window as viewer_module
from material_library import MaterialLibrary


class OfflinePanel(QWidget):
    def __init__(self, *args, parent=None, **kwargs):
        super().__init__(parent)
        self.shutdown = Mock()


class LockdownSlideTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(["test-lockdown-slides"])

    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.settings = QSettings(str(root / "settings.ini"), QSettings.Format.IniFormat)
        library = MaterialLibrary(root / "library")
        chapters = []
        for chapter in range(3):
            path = root / f"chapter-{chapter + 1}.pdf"
            with fitz.open() as document:
                for slide in range(50):
                    page = document.new_page(width=720, height=405)
                    page.insert_text((30, 40), f"Chapter {chapter + 1}, slide {slide + 1}")
                    page.draw_rect((20, 60, 200, 120), color=(0, 0, 1), fill=(0, 0, 1))
                document.save(path)
            chapters.append(str(path))
        packet = library.prepare_pdf("Course A", library.import_files("Course A", chapters))
        for name in ("AttentionVideoPlayer", "VoiceAssistantPanel", "SpotifyNowPlayingWidget"):
            self.stack.enter_context(patch.object(viewer_module, name, OfflinePanel))
        self.stack.enter_context(patch.object(viewer_module, "QSettings", return_value=self.settings))
        self.stack.enter_context(patch.object(viewer_module, "LLMClient"))
        self.stack.enter_context(patch.object(viewer_module.lockdown_os, "LockdownEnforcer"))
        self.stack.enter_context(patch.object(viewer_module.lockdown_os, "ExitHotkeyDetector"))
        self.window = viewer_module.LockdownWindow(packet, None)
        self.window.resize(1700, 1000)
        self.window.show()
        self.settle()

    def tearDown(self):
        self.window._pdf_layout_timer.stop()
        self.window._pdf_render_timer.stop()
        self.window._clock_timer.stop()
        self.window.stop_lockdown()
        self.window.close()
        self.window._pdf_doc.close()
        self.window.deleteLater()
        self.app.processEvents()

    def settle(self):
        QTest.qWait(150)
        self.app.processEvents()

    def displayed_pages(self):
        return {i for i, label in enumerate(self.window._page_labels) if not label.pixmap().isNull()}

    def test_all_150_slides_are_accessible_and_last_slide_renders(self):
        self.assertEqual(self.window._effective_total_slides, 150)
        self.assertEqual(len(self.window._page_labels), 150)
        self.window.pdf_scroll.verticalScrollBar().setValue(self.window.pdf_scroll.verticalScrollBar().maximum())
        self.settle()
        self.assertIn(149, self.displayed_pages())
        self.assertIn("Chapter 3, slide 50", self.window._current_slide_text())
        image = self.window._page_labels[149].pixmap().toImage()
        color = image.pixelColor(round(image.width() * 100 / 720), round(image.height() * 90 / 405))
        self.assertGreater(color.blue(), 240)
        self.assertLess(color.red(), 15)

    def test_chapter_picker_jumps_beyond_slide_80_and_renders_that_chapter(self):
        self.assertEqual(self.window.document_combo.count(), 3)
        self.window._jump_to_material(2)
        self.settle()
        self.assertGreaterEqual(self.window._current_visible_page(), 100)
        self.assertIn("Chapter 3", self.window._current_slide_text())
        self.assertIn(100, self.displayed_pages())

    def test_scrolling_keeps_memory_bounded_and_renders_evicted_pages_again(self):
        for page in (0, 80, 130, 40, 149, 0):
            self.window.pdf_scroll.verticalScrollBar().setValue(self.window._page_labels[page].y())
            self.settle()
            displayed = self.displayed_pages()
            self.assertIn(page, displayed)
            self.assertLess(len(displayed), 10)
        self.assertNotIn(149, self.displayed_pages())

    def test_zoom_and_resize_keep_later_slides_accessible_and_annotations_aligned(self):
        self.window.pdf_scroll.verticalScrollBar().setValue(self.window._page_labels[110].y())
        self.settle()
        self.window._on_zoom_in()
        self.settle()
        self.window.resize(1500, 900)
        self.settle()
        self.assertGreaterEqual(self.window._current_visible_page(), 110)
        self.assertLessEqual(self.window._current_visible_page(), 111)
        self.assertIn("Chapter 3", self.window._current_slide_text())
        self.assertEqual(self.window.pdf_annotation_canvas.size(), self.window.pdf_container.size())
        self.window.pdf_scroll.verticalScrollBar().setValue(self.window.pdf_scroll.verticalScrollBar().maximum())
        self.settle()
        self.assertIn(149, self.displayed_pages())

    def test_manual_completion_button_signals_and_closed_review_cannot_finish_a_new_session(self):
        completed = Mock()
        self.window.completion_requested.connect(completed)
        self.window.complete_session_button.click()
        completed.assert_called_once()
        self.window._timer_widget = Mock()
        with patch.object(viewer_module, "SessionReviewDialog") as review:
            self.window.stop_lockdown()
            self.window._on_review_ready({"recap": "Old session"})
            self.window._finish_material_completion()
            review.assert_not_called()
            self.window._timer_widget.finish_block_early.assert_not_called()


if __name__ == "__main__":
    unittest.main()
