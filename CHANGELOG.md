# Changelog

## [1.0.0] — 2026-10-04

First public release.

### Added
- Junk cleaner with 20+ categories (Windows, temp, browser and app caches, logs, Recycle Bin) and Smart Clean.
- Whitelist-based safety model: path-traversal and link-escape protection, protected user/system folders, change-after-scan detection.
- Quarantine: cleaned junk is kept for 7 days (configurable) and can be restored; restoring never overwrites files.
- Per-file exclusion in the *Details* dialog.
- Disk analyzer: interactive squarified treemap with drill-down, synced with a folder tree by size (read-only); fast mode reads the NTFS MFT directly when run as administrator, with automatic fallback to the folder walk.
- Large files finder and duplicate finder (size → partial hash → BLAKE2b), deletion to Recycle Bin by default.
- `Windows.old` cleanup via Disk Cleanup and component store cleanup via DISM (admin only).
- Running-application check before cleaning (no process is ever killed).
- Optional Winapp2.ini import with the same safety checks; registry rules ignored.
- Clean-up history (no file paths stored).
- Dark / light / system themes; Russian, Ukrainian and English UI.
- 159 unit tests, read-only GUI self-test, GitHub Actions CI with automatic `.exe` build and releases.

### Fixed during testing
- Group checkbox no longer selects heavy Windows operations (DISM / Windows.old).
- Imported rules can no longer bypass the fresh-temp-file protection.
