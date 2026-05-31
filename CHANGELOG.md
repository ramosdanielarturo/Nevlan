# Changelog

## [Unreleased]

### Added

- Universal identity confidence gate: weak target identity returns `NEEDS_REVIEW` instead of blind coords clicks; semantic plan blocks coords as primary.
- Mandatory post-step verification after action steps (`OUTCOME_NOT_CONFIRMED`, `TARGET_NOT_FOUND`, `SURFACE_NOT_READY`).
- Strategy pack `windows_chrome_youtube` for Search→Chrome→Profile→New tab→YouTube→Search (identity + verification first).

### Fixed

- P1: Notepad save dialog primary 5/5 (post-release); baseline histórico `fase7-runtime-v1` intacto.

## [fase7-runtime-v1] - 2026-05-24

### Added

- Fase7 Release Gate (certified 25/25): frozen live baseline, CI workflow, validator, and reproducibility checklist.
