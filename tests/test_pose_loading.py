import contextlib
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
for folder in ("core", "vision"):
    sys.path.insert(0, str(ROOT / folder))

from PIL import Image
import pose_engine


class PoseLoadingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.folder = Path(self.temporary.name) / "\u0645\u0631\u0627\u062c\u0639\U0001f4da"
        self.folder.mkdir()
        self.name = "Florks RenovaMix\U0001f4e2\u2728\U0001f92a.jpg"
        Image.new("RGB", (64, 32), (30, 60, 90)).save(self.folder / self.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_static_image_loads_with_unicode_folder_and_filename(self):
        frames, durations = pose_engine.load_image_frames(self.folder / self.name)
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].shape, (32, 64, 3))
        self.assertEqual(durations, [1000])

    def test_pose_bank_loads_with_strict_windows_output_encoding(self):
        buffer = io.BytesIO()
        with io.TextIOWrapper(buffer, encoding="cp1252", errors="strict") as output:
            spec = {"hand": "near_mouth"}
            with contextlib.redirect_stdout(output), patch.object(pose_engine, "GESTURES", {self.name: spec}):
                challenge = pose_engine.PoseChallenge("unused.task", self.folder).load_bank()
            output.flush()
            diagnostics = buffer.getvalue().decode("cp1252")
            self.assertIn("Loaded 1 reference pose", diagnostics)
            self.assertIn("\\U0001f4e2", diagnostics)
        self.assertEqual(challenge.bank[0].name, self.name)
        self.assertEqual(challenge.bank[0].spec, spec)

    def test_utf8_output_preserves_the_original_filename(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            challenge = pose_engine.PoseChallenge("unused.task", self.folder).load_bank()
        self.assertIn(self.name, output.getvalue())
        self.assertEqual(len(challenge.bank), 1)

    def test_unreadable_unicode_image_is_skipped_without_stopping_valid_poses(self):
        (self.folder / "broken\U0001f4e2.png").write_bytes(b"not an image")
        (self.folder / "empty\U0001f4e2.png").write_bytes(b"")
        with io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict") as output:
            with contextlib.redirect_stdout(output):
                challenge = pose_engine.PoseChallenge("unused.task", self.folder).load_bank()
        self.assertEqual([entry.name for entry in challenge.bank], [self.name])
        self.assertEqual(pose_engine.load_image_frames(self.folder / "missing\U0001f4e2.png"), ([], []))

    def test_unicode_gif_still_loads_every_frame(self):
        gif = self.folder / "animated\U0001f4e2.gif"
        Image.new("RGB", (20, 10), "red").save(
            gif, save_all=True, append_images=[Image.new("RGB", (20, 10), "blue")], duration=120, loop=0
        )
        frames, durations = pose_engine.load_image_frames(gif)
        self.assertEqual(len(frames), 2)
        self.assertEqual(durations, [120, 120])


if __name__ == "__main__":
    unittest.main()
