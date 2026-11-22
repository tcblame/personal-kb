"""Short-lived Git discovery and conflict-index reads for a single retrieval."""
from __future__ import annotations

import os
import re
import subprocess
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from kb_timing import count, git_span


_READS: ContextVar[dict | None] = ContextVar("kb_query_git_reads", default=None)


@contextmanager
def read_scope():
    token = _READS.set({})
    try:
        yield
    finally:
        _READS.reset(token)


def _ordinary_environment() -> bool:
    # Explicit repositories, indexes, discovery rules and config overrides are
    # resolved by Git itself. Do not apply the ordinary-worktree shortcuts.
    return not any(k.startswith(("GIT_CONFIG", "GIT_DIR", "GIT_COMMON_DIR", "GIT_WORK_TREE",
                                 "GIT_INDEX", "GIT_CEILING", "GIT_DISCOVERY", "GIT_NAMESPACE",
                                 "GIT_OBJECT", "GIT_ALTERNATE", "GIT_REPLACE", "GIT_SHALLOW",
                                 "GIT_LITERAL", "GIT_GLOB", "GIT_NOGLOB", "GIT_ICASE"))
                   for k in os.environ)


def _stamp(path: Path):
    try:
        st = path.stat()
    except FileNotFoundError:
        return None
    return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns


def _gitdir(root: Path) -> Path | None:
    marker = root / ".git"
    if marker.is_dir():
        return marker
    if marker.is_file():
        locator = marker.read_text(encoding="utf-8").strip()
        if locator.startswith("gitdir: "):
            return (root / locator[8:]).resolve()
    return None


def repository_root(cwd: Path, run) -> Path | None:
    def native():
        status, output, error = run()
        return (Path(output).resolve() if status == 0 and output else None), error

    cache = _READS.get()
    if cache is None or not _ordinary_environment() or ".git" in cwd.parts:
        return native()[0]
    # Git confirms the first discovery. A new nested checkout, a gitfile
    # rewrite or a different mount must not inherit an earlier answer.
    try:
        start = cwd.resolve()
        device = start.stat().st_dev
        root = gitdir = None
        for directory in (start, *start.parents):
            if directory.stat().st_dev != device:
                break
            gitdir = _gitdir(directory)
            if gitdir is not None:
                root = directory
                break
            if (directory / "HEAD").is_file() or (directory / ".git").exists():
                return native()[0]  # bare repository or unusual marker
        if root and gitdir != root / ".git":
            # Linked worktrees, submodules and external gitdirs may get their
            # worktree/discovery settings from a separate common directory.
            return native()[0]
        key = ("root", root or start, gitdir, _stamp(gitdir / "config") if gitdir else None)
    except (OSError, ValueError, UnicodeError):
        return native()[0]
    if key in cache:
        count("git_read.root_hits")
        return cache[key]
    result, error = native()
    if (result is not None and result == root) or (root is None and "not a git repository" in error):
        cache[key] = result
    return result


def branch_name(root: Path, read):
    cache = _READS.get()
    if cache is None or not _ordinary_environment():
        return read()
    try:
        directory = _gitdir(root)
        if directory is None:
            return read()
        head = (directory / "HEAD").read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return read()
    if not head.startswith("ref: refs/heads/"):
        return read()
    key = ("branch", root, directory, head)
    if key in cache:
        count("git_read.branch_hits")
        return cache[key]
    value = read()
    if value == head.removeprefix("ref: refs/heads/"):
        cache[key] = value
    return value


def _layout(root: Path):
    directory = _gitdir(root)
    if directory is None:
        return None
    link = directory / "commondir"
    common = (directory / link.read_text().strip()).resolve() if link.is_file() else directory
    return directory, common


def _config_paths(cache: dict, repo: Path):
    key = ("config-paths", os.environ.get("HOME"), os.environ.get("XDG_CONFIG_HOME"), os.environ.get("PATH"))
    if key not in cache:
        paths = []
        for variable in ("GIT_CONFIG_SYSTEM", "GIT_CONFIG_GLOBAL"):
            args = ["var", variable]
            with git_span(args, cwd=repo):
                proc = subprocess.run(["git", *args], cwd=repo, capture_output=True)
            if proc.returncode:
                cache[key] = None
                return None
            paths.extend(Path(os.fsdecode(p)) for p in proc.stdout.splitlines() if p)
        cache[key] = paths
    return cache[key]


def _remote_key(repo: Path, cache: dict):
    layout = _layout(repo)
    if layout is None:
        return None
    directory, common = layout
    paths = _config_paths(cache, repo)
    if paths is None:
        return None
    configs = (*paths, common / "config", directory / "config.worktree")
    stamps = tuple(_stamp(path) for path in configs)
    key = ("remote", repo, directory, common, configs, stamps)
    # Legacy remotes and conditional includes may change independently.
    if any(folder.is_dir() and any(folder.iterdir()) for folder in (common / "remotes", common / "branches")):
        return None
    if key not in cache and any(stamp and b"include" in path.read_bytes().lower()
                                for path, stamp in zip(configs, stamps)):
        return None
    return key


def remote_identity(repo: Path, read):
    """Reuse a confirmed remote only while its ordinary Git configs are unchanged."""
    cache = _READS.get()
    if cache is None or not _ordinary_environment():
        return read()
    try:
        key = _remote_key(repo, cache)
    except (OSError, UnicodeError, ValueError):
        key = None
    if key in cache:
        count("git_read.remote_hits")
        return cache[key]
    value = read()
    if key is not None and value:
        try:
            if key == _remote_key(repo, cache):
                cache[key] = value
        except (OSError, UnicodeError, ValueError):
            pass
    return value


def _checkout_key(repo: Path):
    layout = _layout(repo)
    if layout is None:
        return None
    directory, common = layout
    head = (directory / "HEAD").read_text().strip()
    oid = head
    if head.startswith("ref: refs/heads/") and not (common / "reftable").exists():
        ref = common / head.removeprefix("ref: ")
        # Only directly resolved loose refs and detached HEADs are reusable.
        # Packed refs and symbolic ref chains use native Git.
        oid = ref.read_text().strip() if ref.is_file() else ""
    if re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", oid):
        return ("checkout", repo, directory, head, oid)
    return None


def checkout(repo: Path, read):
    cache = _READS.get()
    if cache is None or not _ordinary_environment():
        return read()
    try:
        key = _checkout_key(repo)
    except (OSError, UnicodeError, ValueError):
        key = None
    if key in cache:
        count("git_read.checkout_hits")
        return cache[key]
    value = read()
    if key:
        head, oid = key[-2:]
        branch = head.removeprefix("ref: refs/heads/") if head.startswith("ref: ") else "detached-" + oid[:12]
        if value == (branch, oid):
            cache[key] = value
    return value


def unmerged(root: Path, relative: Path) -> bool | None:
    """None asks the caller to use its ordinary path-specific Git check."""
    cache = _READS.get()
    if (cache is None or not _ordinary_environment() or relative.as_posix().startswith(":")
            or any(char in relative.as_posix() for char in "*?[")):
        return None
    try:
        directory = _gitdir(root)
        if directory is None:
            return None
        index = directory / "index"
        stamp = _stamp(index)
        key = ("unmerged", index, stamp)
        if key not in cache:
            args = ["ls-files", "--unmerged", "--full-name", "-z"]
            with git_span(args, cwd=root):
                proc = subprocess.run(["git", *args], cwd=root, capture_output=True)
            if proc.returncode or _stamp(index) != stamp:
                return None
            # NUL-delimited paths also preserve tabs, quotes and non-UTF8 names.
            cache[key] = {line.split(b"\t", 1)[1] for line in proc.stdout.split(b"\0") if line}
        else:
            count("git_read.index_hits")
        return os.fsencode(relative.as_posix()) in cache[key]
    except (OSError, ValueError, UnicodeError, IndexError):
        return None
