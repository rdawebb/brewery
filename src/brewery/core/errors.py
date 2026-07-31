"""Module defining custom exceptions for the Brewery application."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Self

from brewery.core.logging import BreweryLogger, get_logger

log: BreweryLogger = get_logger(name=__name__)

# Exit Codes
EXIT_SUCCESS = 0
EXIT_USER_ERROR = 1
EXIT_SYSTEM_ERROR = 2
EXIT_TRANSIENT_ERROR = 3


class BrewError(Exception):
    """Base exception class with context propagation.

    All exceptions in the Brewery should inherit from this class.
    Context is a dictionary that accumulates relevant information
    as the exception propagates up the call stack.

    Example:
        raise BrewError("An error occurred", context={"package": "foo"})

        # Or with context propagation
        try:
            ...
        except BrewError as e:
            raise e.with_context(operation="install", user="admin")
    """

    def __init__(self, message: str, context: dict[str, Any] | None = None) -> None:
        self.message: str = message
        self.context: dict[str, Any] = context or {}
        super().__init__(message)

    def with_context(self, **new_context: Any) -> Self:
        """Returns a new exception instance with updated context.

        Args:
            **new_context: Additional context to add to the exception.

        Returns:
            A new instance of the exception with merged context.
        """
        self.context.update(new_context)
        return self

    def __str__(self) -> str:
        """String representation of the exception including context."""
        if self.context:
            context_str: str = ", ".join(f"{k}={v}" for k, v in self.context.items())
            return f"{self.message} [{context_str}]"
        return self.message


class SysError(BrewError):
    """Errors due to system-level issues.

    These errors indicate problems with the system environment, such as
    file system errors, permission issues, or other unexpected conditions.

    These errors may require user intervention or system fixes, and
    CLI should display diagnostic information for troubleshooting.
    """


class TransientError(BrewError):
    """Errors that should be retried immediately.

    These errors are typically due to temporary conditions such as
    network issues or resource unavailability.

    Operations raising this exception should be idempotent.
    """


class UserError(BrewError):
    """Errors caused by user actions or inputs.

    These errors indicate that the user has made a mistake or provided
    invalid input, and should not be retried without correction.

    CLI should display helpful messages to guide the user.
    """


## Specific Exceptions ##


class CacheError(SysError):
    """Errors related to cache access or corruption.

    Typically indicates:
        - File system permission issues
        - Disk space exhaustion
        - Corrupted cache files
        - Read-only file system

    This is a SysError - may require user/system intervention.
    """

    def __init__(
        self,
        message: str | None = None,
        key: str | None = None,
        namespace: str | None = None,
        path: str | None = None,
        operation: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        """Initialise CacheError with detailed context.

        Args:
            message: Optional custom error message.
            key: The cache key involved in the error.
            namespace: The cache namespace or directory.
            path: The file path involved in the error.
            operation: The cache operation being performed.
            context: Additional context information.
        """
        ctx: dict[str, Any] = context or {}
        if key:
            ctx["key"] = key
        if namespace:
            ctx["namespace"] = namespace
        if path:
            ctx["path"] = path
        if operation:
            ctx["operation"] = operation

        if message is None:
            op_str = f"{operation}" if operation else ""
            message = f"Cache {op_str} operation failed"

        super().__init__(message, context=ctx)


class CellarError(SysError):
    """Installing a keg into the Cellar failed; per-formula fallback signal.

    Typically indicates:
        - Disk space exhaustion
        - File system permission issues
        - Cross-device move failure during staging
        - Corrupted or incomplete staging directory
    """

    def __init__(
        self,
        message: str | None = None,
        *,
        name: str | None = None,
        version: str | None = None,
        path: Path | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        """Initialise CellarError with detailed context.

        Args:
            message: Optional custom error message.
            name: The formula name being installed.
            version: The formula version being installed.
            path: The target keg path in the Cellar.
            context: Additional context information.
        """
        ctx: dict[str, Any] = context or {}
        if name:
            ctx["name"] = name
        if version:
            ctx["version"] = version
        if path is not None:
            ctx["path"] = path
        if message is None:
            ver_str = f" {version}" if version else ""
            message = f"Failed to install {name or 'unknown'}{ver_str} into Cellar"
        super().__init__(message, context=ctx)


class DownloadError(SysError):
    """A bottle could not be downloaded or failed verification.

    The pipeline should treat this as a per-formula fallback signal.

    Typically indicates:
        - Network connectivity issues
        - HTTP errors from the package registry
        - SHA-256 checksum mismatch (corrupted or truncated download)
        - All retry attempts exhausted after transient failures
    """

    def __init__(
        self,
        message: str | None = None,
        *,
        name: str | None = None,
        url: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        """Initialise DownloadError with detailed context.

        Args:
            message: Optional custom error message.
            name: The formula name whose bottle failed to download.
            url: The bottle URL that was being fetched.
            context: Additional context information.
        """
        ctx: dict[str, Any] = context or {}
        if name:
            ctx["name"] = name
        if url:
            ctx["url"] = url
        if message is None:
            message = f"Failed to download {name or 'unknown'}"
        super().__init__(message, context=ctx)


class ExtractionError(SysError):
    """A bottle could not be extracted; per-formula fallback signal.

    Typically indicates:
        - Unrecognised or corrupt bottle archive format
        - Unsafe tar members blocked by the security filter
        - Unexpected keg directory layout inside the archive
    """

    def __init__(
        self,
        message: str,
        *,
        archive: Path | None = None,
        dest: Path | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        """Initialise ExtractionError with detailed context.

        Args:
            message: Description of the extraction failure.
            archive: The archive file that failed to extract.
            dest: The staging directory the archive was being extracted into.
            context: Additional context information.
        """
        ctx: dict[str, Any] = context or {}
        if archive is not None:
            ctx["archive"] = archive
        if dest is not None:
            ctx["dest"] = dest
        super().__init__(message, context=ctx)


class ManifestError(SysError):
    """Raised when the manifest can't yield a usable tab.

    Callers treat this as 'fall back to scanning the keg + assembling the
    receipt without tab fields', not as a hard install failure.
    """


class ManifestFetchError(ManifestError):
    """Manifest index could not be retrieved from GHCR (network or HTTP error).

    Typically indicates:
        - Network connectivity issues
        - Transient GHCR service errors (429, 5xx)
        - All retry attempts exhausted
    """

    def __init__(
        self,
        message: str | None = None,
        *,
        name: str | None = None,
        tag: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        """Initialise ManifestFetchError with detailed context.

        Args:
            message: Optional custom error message.
            name: The formula name whose manifest could not be fetched.
            tag: The OCI image tag that was being fetched.
            context: Additional context information.
        """
        ctx: dict[str, Any] = context or {}
        if name:
            ctx["name"] = name
        if tag:
            ctx["tag"] = tag
        if message is None:
            message = f"manifest fetch failed for {name or 'unknown'} {tag or ''}"
        super().__init__(message, context=ctx)


class ManifestParseError(ManifestError):
    """Manifest was fetched but could not be parsed or is missing required fields.

    Typically indicates:
        - No bottle in the manifest matching the host's digest
        - Missing or malformed `sh.brew.tab` annotation
        - Incomplete required tab fields (homebrew_version, compiler, etc.)
        - Malformed JSON in the manifest index or tab annotation
    """

    def __init__(
        self,
        message: str | None = None,
        *,
        name: str | None = None,
        tag: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        """Initialise ManifestParseError with detailed context.

        Args:
            message: Optional custom error message.
            name: The formula name whose manifest could not be parsed.
            tag: The OCI image tag that was being parsed.
            context: Additional context information.
        """
        ctx: dict[str, Any] = context or {}
        if name:
            ctx["name"] = name
        if tag:
            ctx["tag"] = tag
        if message is None:
            message = f"manifest parse error for {name or 'unknown'} {tag or ''}"
        super().__init__(message, context=ctx)


class OperationInProgressError(SysError):
    """Another process holds the lock for a path we need to mutate.

    Raised when `flock` on `<prefix>/var/homebrew/locks/<rack>.formula.lock` is contended.
    Retrying is pointless while the holder lives, so callers report it per-formula rather
    than falling back to `brew`, which would contend on the same lock.
    """

    def __init__(
        self,
        subject: str,
        *,
        path: Path | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        """Initialise OperationInProgressError.

        Args:
            subject: What is locked, in user-facing terms.
            path: The lock file itself, for diagnostics.
            context: Additional context information.
        """
        ctx: dict[str, Any] = context or {}
        if path is not None:
            ctx["lock"] = path

        super().__init__(
            f"another brew or brewery process has already locked {subject} - "
            "wait for it to finish or terminate it to continue",
            context=ctx,
        )


class RelocationError(SysError):
    """Raised when a keg cannot be relocated natively; per-formula fallback signal.

    Typically indicates:
        - install_name_tool failure (e.g. Mach-O header pad exhausted)
        - codesign failure after relocation (macOS)
        - patchelf failure rewriting ELF rpath/interpreter (Linux)
        - Static archive containing an unrewritable placeholder path
        - A placeholder that survived substitution (unresolved token)
    """

    def __init__(self, path: Path, reason: str) -> None:
        """Initialise RelocationError.

        Args:
            path: The file within the keg that could not be relocated.
            reason: A description of why relocation failed.
        """
        self.path = path
        self.reason = reason
        super().__init__(f"{path}: {reason}")


class BrewCommandError(SysError):
    """Brew command returned a non-zero exit code.

    Typically indicates a deterministic failure that will not change on a
    retry:
        - Formula/cask not found or ambiguous
        - Link/install conflicts
        - Missing dependencies or an unsupported flag
        - A corrupted local Brew installation

    Genuinely transient causes (network, service outages, rate limiting) are
    possible but indistinguishable from the deterministic majority via the
    exit code, so this is NOT retried automatically. It instead drives the
    native fallback path where callers catch it explicitly.
    """

    def __init__(
        self,
        message: str | None = None,
        command: str | None = None,
        returncode: int | None = None,
        error: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        """Initialise BrewCommandError with detailed context.

        Args:
            message: Optional custom error message.
            command: The brew command that was executed.
            returncode: The exit code returned by the command.
            error: The error output from the command.
            context: Additional context information.
        """
        ctx: dict[str, Any] = context or {}
        if command:
            ctx["command"] = command
        if returncode:
            ctx["returncode"] = returncode
        if error:
            ctx["error"] = error

        if message is None:
            message = f"Brew command failed with exit code {returncode or 'unknown'}"

        super().__init__(message, context=ctx)


class BrewTimeoutError(TransientError):
    """Brew command timed out.

    Typically indicates:
        - Slow network conditions
        - Brew server overload
        - Very large package downloads
        - System resource constraints

    This will be retried automatically by the caller.
    """

    def __init__(
        self,
        message: str | None = None,
        command: str | None = None,
        timeout: float | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        """Initialise BrewTimeoutError with detailed context.

        Args:
            message: Optional custom error message.
            command: The brew command that was executed.
            timeout: The timeout threshold in seconds.
            context: Additional context information.
        """
        ctx: dict[str, Any] = context or {}
        if command:
            ctx["command"] = command
        if timeout is not None:
            ctx["timeout"] = timeout

        if message is None:
            message = f"Brew command timed out after {timeout or 'unknown'}s"

        super().__init__(message, context=ctx)


class CatalogFetchError(TransientError):
    """A catalog feed could not be fetched.

    Typically indicates:
        - Network connectivity issues
        - CDN or GitHub API outage
        - Rate limiting (HTTP 429)
        - Unexpected or malformed HTTP response
    """

    def __init__(
        self,
        message: str | None = None,
        url: str | None = None,
        status_code: int | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        """Initialise CatalogFetchError with an optional message and context.

        Args:
            message: Optional error message.
            url: Optional URL that was being fetched.
            status_code: Optional HTTP status code.
            context: Optional context dictionary.
        """
        ctx: dict[str, Any] = context or {}
        if url:
            ctx["url"] = url
        if status_code:
            ctx["status"] = status_code

        if message is None:
            message = f"Failed to fetch catalog from {url or 'unknown URL'}"

        super().__init__(message, context=ctx)


class AlreadyInstalledWarning(UserError):
    """Package already installed - no action taken.

    Typically indicates:
        - Re-running an install command for a package already present
        - Dependency resolution requesting an already-satisfied package

    CLI should handle this gracefully by informing the user.
    """

    def __init__(
        self, package: str | None = None, context: dict[str, Any] | None = None
    ) -> None:
        """Initialise AlreadyInstalledWarning with detailed context.

        Args:
            package: That name of the package that is already installed.
            context: Additional context information.
        """
        ctx: dict[str, Any] = context or {}
        if package:
            ctx["package"] = package

        super().__init__(
            message=f"'{package or 'unknown'}' is already installed",
            context=ctx,
        )


class LinkError(UserError):
    """Linking the keg conflicts with existing files; per-formula fallback signal.

    Typically indicates:
        - A manually placed file occupies a path Homebrew needs to own
        - A previous partial install left stale files in the prefix
        - Another formula already owns the conflicting path
    """

    def __init__(self, conflicts: list[tuple[str, str]]) -> None:
        """Initialise LinkError with the list of conflicting paths.

        Args:
            conflicts: List of (destination, existing_target) pairs that conflict.
        """
        self.conflicts = conflicts
        listing = "\n".join(
            f"  {dst} -> already {existing}" for dst, existing in conflicts
        )
        super().__init__(f"link conflicts:\n{listing}")


class PackageNotFoundError(UserError):
    """Requested package was not found in the repository.

    Typically indicates:
        - Misspelled formula or cask name
        - Formula removed or renamed in the catalog
        - Catalog not yet refreshed after a recent rename

    Do not retry without changing the package name.
    """

    def __init__(
        self,
        message: str | None = None,
        package: str | None = None,
        kind: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        """Initialise PackageNotFoundError with detailed context.

        Args:
            message: Optional custom error message.
            package: The name of the package that was not found.
            kind: The kind of package (e.g., formula, cask).
            context: Additional context information.
        """
        ctx: dict[str, Any] = context or {}
        if package:
            ctx["package"] = package
        if kind:
            ctx["kind"] = kind

        if message is None:
            kind_str = f" {kind}" if kind else ""
            message = f"Package{kind_str} '{package or 'unknown'}' not found"

        super().__init__(message, context=ctx)


class PinnedPackageWarning(UserError):
    """Package is pinned - upgrade skipped.

    Typically indicates:
        - Running upgrade on a package the user has pinned with `brew pin`

    CLI should inform the user that the package is pinned and cannot be upgraded.
    """

    def __init__(self, package: str | None = None) -> None:
        """Initialise PinnedPackageWarning with detailed context.

        Args:
            package: That name of the package that is pinned.
        """
        ctx: dict[str, Any] = {}
        if package:
            ctx["package"] = package

        super().__init__(
            message=f"'{package or 'unknown'}' is pinned and cannot be upgraded",
            context=ctx,
        )


class SettingsError(UserError):
    """Invalid config key, invalid value, or an unreadable file on write.

    Typically indicates:
        - An unknown or misspelled config key
        - A value that fails validation for its key
        - A corrupt config file (not valid JSON or not a JSON object)
        - An existing config file that cannot be read before a write

    Do not retry without correcting the key, value, or config file.
    """

    def __init__(
        self,
        message: str,
        *,
        key: str | None = None,
        value: str | None = None,
        path: Path | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        """Initialise SettingsError with detailed context.

        Args:
            message: Description of the settings failure.
            key: The dotted config key involved in the error.
            value: The raw value that failed validation.
            path: The config file involved in the error.
            context: Additional context information.
        """
        ctx: dict[str, Any] = context or {}
        if key:
            ctx["key"] = key
        if value:
            ctx["value"] = value
        if path is not None:
            ctx["path"] = path
        super().__init__(message, context=ctx)
