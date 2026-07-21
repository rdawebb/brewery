# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Linux support: bottle selection and install at the default `/home/linuxbrew/.linuxbrew` prefix, ELF dynamic-linkage relocation via `patchelf` for non-default prefixes, and a systemd user-timer background daemon
- Cache and log directories honour `$XDG_CACHE_HOME`/`$XDG_STATE_HOME` on Linux
- Missing external tools (`patchelf`, `install_name_tool`, `codesign`, `systemctl`) now report an actionable error instead of a traceback

## [0.3.0] - 2026-07-19

### Added

- Native install pipeline, replacing shell-outs to `brew install`: SHA256-verified content-addressed bottle downloads, archive extraction with format detection and security filtering, Mach-O relocation engine brew-compatible `INSTALL_RECEIPT.json` generation, and clonefile-backed atomic Cellar placement
- Platform-aware GHCR OCI manifest selection for bottle downloads
- Brew-compatible keg linker with conflict detection pre-pass
- Native uninstall pipeline with manifest-based unlinking and blocking on dependent packages
- Native upgrade pipeline with old-keg swap, receipt inheritance, and automatic fallback to brew
- `link`/`unlink` and `pin`/`unpin` commands, with `--dry-run` support
- `cleanup` command for stale kegs, with age, count, and size-cap retention strategies; the daemon now runs a daily cleanup sweep
- `config` command and a user settings file (refresh interval, retention age)
- Live progress reporting for install and upgrade, with per-stage status glyphs and a fixed-column layout
- Compact brew-style multi-column output for `list` and `search`

### Changed

- Standardised exit codes on partial failure across multi-package commands
- Pinned packages are now honoured when upgrading by name
- Overhauled package-details rendering and pagination
- Codesigning is now batched across all rewritten Mach-O files in a keg, speeding up install relocation

### Fixed

- Aliases are resolved before install/uninstall/upgrade verification
- Catalog rows absent from a fresh full-feed download are now pruned
- Relocator resolves formula tokens and rejects unresolved placeholders; text and symlink substitution apply even when relocation is skipped
- Linker serialises shared-directory mutations under a lock, skips self-referential keg symlinks, and removes the `opt` symlink when unlinking the keg it points at
- Link/pin state changes now invalidate the package cache

## [0.2.0] - 2026-06-09

### Added

- SQLite catalog store for formula and cask metadata, populated from the Homebrew API feeds by an async fetcher
- Full-text search and catalog-only lookup for packages that are not installed
- Filesystem-derived installed-state scanner (Cellar/Caskroom), including linked/pinned detection and a reverse-dependency graph
- Keg size measurement with an mtime-keyed disk cache

### Changed

- Replaced the brew-JSON provider pipeline with a merge-on-read join of filesystem state and the catalog, removing `brew info --json` from the read path
- Converted the read pipeline from async to sync
- Daemon management switched to the modern `launchctl bootstrap`/`bootout` API

### Fixed

- `outdated` no longer misreports packages after a cache refresh

## [0.1.0] - 2026-05-31

Initial release.

### Added

- CLI for managing Homebrew packages: `list`, `search`, `info`, `install`, `uninstall`, `upgrade`, and `outdated`, with multi-package support and batch execution with concurrency control
- Auto-detection of formula vs cask in `info`
- Pagination for large package lists
- Background catalog-refresh daemon managed via launchd
- Passthrough of unknown commands to `brew`
- Package size calculation

[Unreleased]: https://github.com/rdawebb/brewery/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/rdawebb/brewery/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/rdawebb/brewery/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/rdawebb/brewery/releases/tag/v0.1.0
