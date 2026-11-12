# Changelog

## 1.1.1 - 2026-11-12

### Added

- Upload material buttons on the main screen and Focus panel, with a persistent per-course library that keeps earlier uploads available after restarts and on future study days.
- Chapter assignments for uploaded files, combined study packets, and document navigation in lockdown. PDF, Word, text, and image material can be prepared for the viewer; PowerPoint slides should be exported as PDF.
- Removal of selected library files, including multiple PDFs at once. Old plan references cannot silently restore removed material; explicitly uploading it again can restore it.
- A session chooser for days with multiple sessions, preserving separate chapters and start times and keeping the selected session through calendar refreshes.
- A Mark session completed button in Focus and lockdown. It ends all remaining blocks, stops monitoring, and archives the exact selected Notion calendar entry. Failed saves show a retry option.
- Regression coverage for startup, material persistence and removal, session selection, Unicode pose assets, distraction pauses, full-document viewing, and manual completion.

### Changed

- Pose and push-up challenges are temporarily disabled, including old penalty prompts. Distraction detection, slide dimming, countdown pauses, and automatic resume after refocusing remain active.
- The viewer renders pages near the viewport and releases distant images, keeping every slide accessible without loading an entire course packet into memory.
- Repeated uploads are deduplicated by content, and uploading a changed file with the same name replaces its previous library version.

### Fixed

- Startup crashes from duplicate planner work and background threads being destroyed while still running.
- The distraction monitor's Windows character-encoding error with Unicode or emoji filenames, and loading images from Unicode paths.
- The hardcoded 80-page display limit, including navigation to later documents and maintaining the reading position across zoom and resize.
- Only the latest upload being available, wrong-course material fallback, and stale material or topic previews after course changes or file removal.
- Distinct same-day sessions being combined, selected sessions changing after a refresh, and completion affecting the wrong Notion entry.
- Starting another session after completing the first without restarting the application.
- Timer pause and resume behavior when leaving lockdown while distracted; slide dimming no longer depends on a pose challenge being visible.
- Duplicate completion clicks and overlapping saves. Closing the app waits for an active completion save to finish.
- Manual completion recording unspent blocks as actual study time.
- Callbacks from a closed review affecting the next session, and pending PDF rendering continuing after lockdown stops.
- Stale planner previews and repeated generation when changing courses or deadlines.

### Validation

- All 66 automated tests pass.
- Native Windows checks cover later-chapter rendering, the completion button, and successful application startup.
