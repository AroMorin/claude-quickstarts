#!/usr/bin/env python3
"""Require every commit to stage a SemVer version increase.

The hook prefers pyproject.toml versions ([project].version or
[tool.poetry].version). A root VERSION file is only a fallback for repos
without versioned project metadata.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tomllib
from dataclasses import dataclass

SEMVER_PATTERN = re.compile(
    r"^(?P<major>0|[1-9]\d*)\."
    r"(?P<minor>0|[1-9]\d*)\."
    r"(?P<patch>0|[1-9]\d*)"
    r"(?:-(?P<prerelease>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+(?P<build>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)


class InvalidSemVerError(ValueError):
    """Raised when a version string is not valid SemVer."""

    def __init__(self, value: str) -> None:
        """Build a human-readable SemVer validation error."""
        message = (
            f"{value!r} is not valid SemVer. Use MAJOR.MINOR.PATCH, "
            "optionally with prerelease/build metadata."
        )
        super().__init__(message)


class InvalidPyprojectError(ValueError):
    """Raised when pyproject.toml cannot be parsed."""

    def __init__(self, details: str) -> None:
        """Build a pyproject parsing error with TOML details."""
        super().__init__(f"pyproject.toml is invalid TOML: {details}")


class VersionValidationError(ValueError):
    """Raised when a version source contains an invalid version."""

    def __init__(self, source_name: str, reason: str) -> None:
        """Build a validation error tied to a specific version source."""
        super().__init__(f"{source_name}: {reason}")


@dataclass(frozen=True)
class VersionSource:
    """A discovered version value and the file it came from."""

    name: str
    version: str


@dataclass(frozen=True)
class CheckResult:
    """The hook decision and message to show to the committer."""

    exit_code: int
    message: str
    is_error: bool = False


@dataclass(frozen=True)
class SemVer:
    """Comparable SemVer value, ignoring build metadata precedence."""

    major: int
    minor: int
    patch: int
    prerelease: tuple[str, ...]

    @classmethod
    def parse(cls, value: str) -> SemVer:
        """Parse a SemVer string into a comparable value."""
        match = SEMVER_PATTERN.fullmatch(value.strip())
        if match is None:
            raise InvalidSemVerError(value)

        prerelease_text = match.group("prerelease")
        prerelease = tuple(prerelease_text.split(".")) if prerelease_text else ()

        return cls(
            major=int(match.group("major")),
            minor=int(match.group("minor")),
            patch=int(match.group("patch")),
            prerelease=prerelease,
        )

    def __lt__(self, other: SemVer) -> bool:
        """Return whether this SemVer has lower precedence than another."""
        release = (self.major, self.minor, self.patch)
        other_release = (other.major, other.minor, other.patch)
        if release != other_release:
            return release < other_release

        if not self.prerelease and other.prerelease:
            return False
        if self.prerelease and not other.prerelease:
            return True

        return _compare_prerelease(self.prerelease, other.prerelease) < 0


def _compare_prerelease(left: tuple[str, ...], right: tuple[str, ...]) -> int:
    comparison = 0
    for left_part, right_part in zip(left, right, strict=False):
        if left_part == right_part:
            continue

        comparison = _compare_prerelease_part(left_part, right_part)
        break

    if comparison != 0:
        return comparison

    return (len(left) > len(right)) - (len(left) < len(right))


def _compare_prerelease_part(left: str, right: str) -> int:
    left_numeric = left.isdigit()
    right_numeric = right.isdigit()
    if left_numeric and right_numeric:
        left_number = int(left)
        right_number = int(right)
        return (left_number > right_number) - (left_number < right_number)
    if left_numeric:
        return -1
    if right_numeric:
        return 1

    return (left > right) - (left < right)


def run_git(args: list[str]) -> subprocess.CompletedProcess[str]:
    """Run git with controlled arguments and captured output."""
    return subprocess.run(  # noqa: S603 - fixed git command with controlled args.
        ["git", *args],  # noqa: S607
        check=False,
        capture_output=True,
        text=True,
    )


def read_git_object(revision: str, path: str) -> str | None:
    """Read a file from HEAD or the staged index."""
    object_spec = f":{path}" if revision == ":" else f"{revision}:{path}"
    result = run_git(["show", object_spec])
    if result.returncode != 0:
        return None
    return result.stdout


def get_project_version(pyproject_text: str) -> str | None:
    """Extract the package version from pyproject.toml text."""
    try:
        data = tomllib.loads(pyproject_text)
    except tomllib.TOMLDecodeError as exc:
        raise InvalidPyprojectError(str(exc)) from exc

    project = data.get("project")
    if isinstance(project, dict):
        version = project.get("version")
        if isinstance(version, str) and version.strip():
            return version.strip()

    tool = data.get("tool")
    poetry = tool.get("poetry") if isinstance(tool, dict) else None
    if isinstance(poetry, dict):
        version = poetry.get("version")
        if isinstance(version, str) and version.strip():
            return version.strip()

    return None


def read_version_sources(revision: str) -> list[VersionSource]:
    """Read supported version sources from HEAD or the staged index."""
    sources: list[VersionSource] = []

    pyproject_text = read_git_object(revision, "pyproject.toml")
    if pyproject_text is not None:
        project_version = get_project_version(pyproject_text)
        if project_version is not None:
            sources.append(VersionSource("pyproject.toml", project_version))

    version_text = read_git_object(revision, "VERSION")
    if version_text is not None:
        version = version_text.strip()
        if version:
            sources.append(VersionSource("VERSION", version))

    return sources


def choose_version_source(sources: list[VersionSource]) -> VersionSource | None:
    """Choose pyproject.toml first, then VERSION as a fallback."""
    for source in sources:
        if source.name == "pyproject.toml":
            return source
    return sources[0] if sources else None


def validate_version(value: str, source_name: str) -> SemVer:
    """Parse and validate a version from a named source."""
    try:
        return SemVer.parse(value)
    except InvalidSemVerError as exc:
        raise VersionValidationError(source_name, str(exc)) from exc


def self_test() -> int:
    """Run a small SemVer precedence regression suite."""
    checks = [
        ("0.1.1", "0.1.0", True),
        ("1.0.0", "1.0.0-rc.1", True),
        ("1.0.0-rc.2", "1.0.0-rc.1", True),
        ("1.0.0", "1.0.0", False),
        ("1.0.0-alpha", "1.0.0", False),
    ]
    for new_text, old_text, expected in checks:
        new = SemVer.parse(new_text)
        old = SemVer.parse(old_text)
        actual = old < new
        if actual != expected:
            sys.stderr.write(
                f"self-test failed: expected {new_text} > {old_text} "
                f"to be {expected}, got {actual}\n"
            )
            return 1
    sys.stdout.write("version bump hook self-test passed\n")
    return 0


def check_version_bump() -> CheckResult:
    """Validate that the staged version exists and increases from HEAD."""
    result = CheckResult(
        exit_code=1,
        message=(
            "Missing staged version source. Add a root VERSION file or "
            "set [project].version in pyproject.toml."
        ),
        is_error=True,
    )

    try:
        staged_source = choose_version_source(read_version_sources(":"))
        if staged_source is not None:
            staged_version = validate_version(staged_source.version, staged_source.name)
            head_source = choose_version_source(read_version_sources("HEAD"))
            if head_source is None:
                result = CheckResult(
                    exit_code=0,
                    message=(
                        f"Initial version source accepted: {staged_source.name} "
                        f"{staged_source.version}"
                    ),
                )
            else:
                head_version = validate_version(head_source.version, head_source.name)
                result = compare_versions(head_source, head_version, staged_source, staged_version)
    except ValueError as exc:
        result = CheckResult(exit_code=1, message=str(exc), is_error=True)

    return result


def compare_versions(
    head_source: VersionSource,
    head_version: SemVer,
    staged_source: VersionSource,
    staged_version: SemVer,
) -> CheckResult:
    """Compare old and staged versions and return the hook decision."""
    if head_version < staged_version:
        return CheckResult(
            exit_code=0,
            message=(
                f"Version bump accepted: {head_source.version} -> "
                f"{staged_source.version} ({staged_source.name})"
            ),
        )

    return CheckResult(
        exit_code=1,
        message=(
            "Version must increase on every commit. "
            f"HEAD has {head_source.name}={head_source.version}; "
            f"staged {staged_source.name}={staged_source.version}."
        ),
        is_error=True,
    )


def main() -> int:
    """Run the hook or the built-in self-test."""
    if "--self-test" in sys.argv:
        return self_test()

    result = check_version_bump()
    stream = sys.stderr if result.is_error else sys.stdout
    stream.write(f"{result.message}\n")
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
