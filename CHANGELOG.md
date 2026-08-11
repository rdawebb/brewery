# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- Installing a large dependency tree is around five times faster: relocation reads each file directly rather than memory-mapping it, which on macOS stalled every other formula's extraction and linking
- Bottles extract in about half the time, by deciding that a tar member stays inside the staging directory from its name rather than by resolving every member against the filesystem
- Formulae that ship thousands of individual files link around four times faster due to reduced object allocations
- Mach-O install names are rewritten natively instead of by spawning `install_name_tool` subprocesses, cutting the relocation step time of an install on binary-heavy formulae; `install_name_tool` is still used where a rewritten path no longer fits its load command, and `BREWERY_NO_NATIVE_MACHO=1` forces every binary through it
- An install that collides with a concurrent `brew` or brewery process now reports the conflict after a single lock timeout instead of one timeout per formula waiting behind it
- A binary that already lists the relocated search path no longer fails to install; the duplicate `LC_RPATH` entry is harmless to the dynamic loader, where `install_name_tool` rejected it outright
- Install timestamps in the table view are shown as `YYYY-MM-DD HH:MM` local time instead of a full ISO-8601 string
- Upgrading a formula by name when it is already up to date now reports it as up to date instead of reinstalling it, matching the way a bulk upgrade already treated it

### Fixed

- The link plan is now built under the same lock that applies it, so a peer can no longer replace a directory while the plan is still reading it
- A formula shipping the same file under two names no longer intermittently fails to install with a permission error, and both names keep the permissions the bottle shipped instead of one being left writable
- The keg size cache is merged rather than rebuilt, so partial/failed commands no longer empty it and leave the next command measuring every keg again; it is also written atomically, so a concurrent refresh cannot catch it half-written
- Install, upgrade, uninstall, link/unlink and cleanup now take Homebrew's own per-formula lock, so brewery and a concurrent `brew` no longer modify the same formula at the same time; a formula another process is holding is reported rather than waited on
- Changes to shared prefix directories are serialised across brewery processes, so a background cleanup or a second brewery run can no longer interleave with linking
- Two formulae installing at once that both provide the same file are now reported as a conflict instead of one silently overwriting the other's link; the formula that loses the race leaves the prefix untouched and falls back to `brew link`
- A bottle containing a hard link to a file it does not ship is now reported as a failed extraction instead of aborting the install with a traceback
- A corrupt or older-schema installed-records cache now rebuilds itself instead of failing the command
- An unexpected error installing one formula no longer discards the whole install report, and no longer lets worker threads keep writing to the Cellar after the command has returned
- A formula that fell back to `brew` successfully is no longer reported as an error
- The bottle cache directory is created before it is probed, so a first run with no existing Homebrew cache works
- A bottle download that pauses for a few seconds on a slow or congested connection is no longer abandoned and handed to `brew` to start over

### Security

- Bottle downloads are capped, so a response that overruns its advertised length is aborted rather than filling the disk before the checksum is verified
- Bottle extraction is capped on total size and member count, rejecting a decompression bomb before anything is written
- A bottle can no longer write outside the keg through a symlink it ships itself, and a bottle whose top-level entry is a symlink is rejected rather than followed into the Cellar

## [0.4.0] - 2026-07-21

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

[Unreleased]: https://github.com/rdawebb/brewery/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/rdawebb/brewery/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/rdawebb/brewery/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/rdawebb/brewery/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/rdawebb/brewery/releases/tag/v0.1.0
