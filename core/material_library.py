"""Durable course uploads and PDF packets for scheduled study sessions."""

import hashlib
from html import escape
import io
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import threading

from paths import get_app_data_dir

SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".txt", ".jpg", ".jpeg", ".png"}
_LOCK = threading.RLock()


class MaterialError(Exception):
    pass


def parse_chapters(text):
    text = text.strip()
    if not text:
        return None
    match = re.fullmatch(r"(\d+)(?:\s*[-–]\s*(\d+))?", text)
    if not match:
        raise MaterialError("Enter a chapter number or range, such as 2 or 2-4.")
    start, end = int(match[1]), int(match[2] or match[1])
    if start < 1 or end < start:
        raise MaterialError("Chapter numbers must start at 1 and the range must go forwards.")
    return [start, end]


class MaterialLibrary:
    def __init__(self, root=None):
        self.root = Path(root) if root is not None else get_app_data_dir() / "materials"

    def _read(self):
        manifest = self.root / "library.json"
        if not manifest.exists():
            return {"version": 1, "courses": {}}
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            if not isinstance(data.get("courses"), dict):
                raise ValueError("Invalid course list")
            return data
        except (OSError, ValueError, TypeError) as error:
            raise MaterialError(f"Couldn't read the material library: {error}") from error

    def _write(self, data):
        self.root.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=self.root, suffix=".json.tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(data, stream, ensure_ascii=True, indent=2)
            os.replace(temporary, self.root / "library.json")
        finally:
            Path(temporary).unlink(missing_ok=True)

    def courses(self):
        with _LOCK:
            return sorted(self._read()["courses"], key=str.casefold)

    def materials(self, course, include_removed=False):
        with _LOCK:
            return [dict(item) for item in self._read()["courses"].get(course.strip(), [])
                    if include_removed or not item.get("removed")]

    def import_files(self, course, paths, *, restore_removed=True):
        """Copy uploads, append to this course, and return their managed paths."""
        course = course.strip()
        if not course:
            raise MaterialError("Choose or enter a course before uploading material.")
        sources = [Path(path) for path in paths]
        for source in sources:
            if not source.is_file():
                raise MaterialError(f"File not found: {source.name}")
            if source.suffix.lower() not in SUPPORTED_EXTENSIONS:
                raise MaterialError(f"Unsupported material type: {source.suffix}")
        imported = []
        with _LOCK:
            data = self._read()
            records = data["courses"].setdefault(course, [])
            course_id = hashlib.sha256(course.encode("utf-8")).hexdigest()[:20]
            for source in sources:
                with source.open("rb") as stream:
                    content_id = hashlib.file_digest(stream, "sha256").hexdigest()
                existing = next((item for item in records if item["id"] == content_id), None)
                if existing is not None and existing.get("removed") and not restore_removed:
                    continue
                if existing is not None and Path(existing["path"]).is_file():
                    existing.pop("removed", None)
                    imported.append(existing["path"])
                    continue
                if existing is None:
                    existing = next((item for item in records
                                     if item["name"].casefold() == source.name.casefold()), None)
                if existing is not None and existing.get("removed") and not restore_removed:
                    continue
                destination = self.root / "files" / course_id / content_id[:20] / source.name
                destination.parent.mkdir(parents=True, exist_ok=True)
                fd, temporary = tempfile.mkstemp(dir=destination.parent, suffix=".upload")
                os.close(fd)
                try:
                    shutil.copyfile(source, temporary)
                    os.replace(temporary, destination)
                finally:
                    Path(temporary).unlink(missing_ok=True)
                record = {"id": content_id, "name": source.name, "path": str(destination),
                          "chapters": existing.get("chapters") if existing else None}
                if existing is None:
                    records.append(record)
                else:
                    records[records.index(existing)] = record
                imported.append(str(destination))
            self._write(data)
        return list(dict.fromkeys(imported))

    def remove_files(self, course, paths):
        """Exclude selected uploads, retaining their removal across legacy imports."""
        selected = {str(path) for path in paths}
        if not selected:
            return 0
        with _LOCK:
            data = self._read()
            records = data["courses"].get(course.strip(), [])
            if selected - {item["path"] for item in records}:
                raise MaterialError("A selected file is no longer in this course's library.")
            removed = 0
            for item in records:
                if item["path"] in selected and not item.get("removed"):
                    item["removed"] = True
                    removed += 1
            if removed:
                self._write(data)
        return removed

    def set_chapters(self, course, path, text):
        chapters = parse_chapters(text)
        with _LOCK:
            data = self._read()
            record = next((item for item in data["courses"].get(course, []) if item["path"] == path), None)
            if record is None:
                raise MaterialError("This file is no longer in the course library.")
            record["chapters"] = chapters
            self._write(data)

    def resolve(self, course, legacy_paths=(), chapter_range=None):
        """Adopt old uploads and resolve files within the scheduled course only."""
        records = self.materials(course, include_removed=True)
        removed_names = {item["name"].casefold() for item in records if item.get("removed")}
        records = [item for item in records if not item.get("removed")]
        known_names = {item["name"].casefold() for item in records if Path(item["path"]).is_file()}
        existing = [str(path) for path in legacy_paths if path and Path(path).is_file()
                    and Path(path).name.casefold() not in known_names | removed_names]
        if existing:
            self.import_files(course, existing, restore_removed=False)
            records = self.materials(course)
        names = {item["name"].casefold() for item in records if Path(item["path"]).is_file()}
        if chapter_range:
            start, end = chapter_range
            records = [item for item in records if not item.get("chapters") or
                       (item["chapters"][0] <= end and item["chapters"][1] >= start)]
        paths = [item["path"] for item in records if Path(item["path"]).is_file()]
        missing = [Path(path).name for path in legacy_paths
                   if path and not Path(path).is_file()
                   and Path(path).name.casefold() not in names | removed_names]
        return paths, missing

    def prepare_pdf(self, course, paths):
        """Include every selected document, keeping file boundaries as bookmarks."""
        if not paths:
            return None
        if len(paths) == 1 and Path(paths[0]).suffix.lower() == ".pdf":
            return str(paths[0])
        import fitz
        signature = [(str(path), Path(path).stat().st_size, Path(path).stat().st_mtime_ns) for path in paths]
        packet_id = hashlib.sha256(json.dumps([course, signature]).encode("utf-8")).hexdigest()
        cache = self.root / "packets"
        cache.mkdir(parents=True, exist_ok=True)
        destination = cache / f"{packet_id}.pdf"
        if destination.is_file():
            return str(destination)
        packet = fitz.open()
        toc = []
        try:
            for path in map(Path, paths):
                toc.append([1, path.name, len(packet) + 1])
                if path.suffix.lower() == ".pdf":
                    with fitz.open(str(path)) as document:
                        packet.insert_pdf(document)
                elif path.suffix.lower() in {".jpg", ".jpeg", ".png"}:
                    with fitz.open(str(path)) as image:
                        with fitz.open("pdf", image.convert_to_pdf()) as document:
                            packet.insert_pdf(document)
                else:
                    if path.suffix.lower() == ".docx":
                        from docx import Document
                        word = Document(str(path))
                        parts = [paragraph.text for paragraph in word.paragraphs]
                        parts += [" | ".join(cell.text for cell in row.cells)
                                  for table in word.tables for row in table.rows]
                        text = "\n".join(parts)
                    else:
                        text = path.read_text(encoding="utf-8", errors="replace")
                    story = fitz.Story(html=f"<h1>{escape(path.name)}</h1><div style='white-space:pre-wrap'>{escape(text)}</div>")
                    buffer = io.BytesIO()
                    writer = fitz.DocumentWriter(buffer)
                    box = fitz.paper_rect("a4")
                    story.write(writer, lambda _number, _filled: (box, box + (40, 40, -40, -40), None))
                    writer.close()
                    with fitz.open("pdf", buffer.getvalue()) as document:
                        packet.insert_pdf(document)
            if not len(packet):
                raise MaterialError("The uploaded documents have no pages to display.")
            packet.set_toc(toc)
            packet.save(str(destination), garbage=4, deflate=True)
        except Exception as error:
            destination.unlink(missing_ok=True)
            if isinstance(error, MaterialError):
                raise
            raise MaterialError(f"Couldn't prepare the course material: {error}") from error
        finally:
            packet.close()
        return str(destination)
