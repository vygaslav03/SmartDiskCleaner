# Changelog

## [1.0.0] — 2026-10-04

First public release.

### Added
- Junk cleaner with 20+ categories (Windows, temp, browser and app caches, logs, Recycle Bin) and Smart Clean.
- Whitelist-based safety model: path-traversal and link-escape protection, protected user/system folders, change-after-scan detection.
- Per-file exclusion in the *Details* dialog.
- Disk analyzer (folder tree by size, read-only).
- Large files finder and duplicate finder (size → partial hash → BLAKE2b), deletion to Recycle Bin by default.
- `Windows.old` cleanup via Disk Cleanup and component store cleanup via DISM (admin only).
- Running-application check before cleaning (no process is ever killed).
- Optional Winapp2.ini import with the same safety checks; registry rules ignored.
- Clean-up history (no file paths stored).
- Dark / light / system themes; Russian, Ukrainian and English UI.
- 126 unit tests, read-only GUI self-test, GitHub Actions CI with automatic `.exe` build and releases.

### Fixed during testing
- Group checkbox no longer selects heavy Windows operations (DISM / Windows.old).
- Imported rules can no longer bypass the fresh-temp-file protection.
