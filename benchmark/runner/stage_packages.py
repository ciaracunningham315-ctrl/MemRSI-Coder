"""Deterministic, stage-local Git repositories for the frozen benchmark.

Archives contain only code and its allowed Git ancestry, never task/private
metadata. ``allowed_ancestor_shas`` means strict ancestors, oldest first; HEAD
is recorded separately. Snapshot hashes bind path, executable mode and bytes.
All manifests and packages are trusted Runner inputs; verification also derives
object reachability independently, so adding an object to a manifest whitelist
cannot legitimize a hidden future object. No model or network is used here.
"""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tarfile
import tempfile
import zlib
from typing import Any

STAGES = ("t1_seed", "t2_transfer", "t3_stale", "t4_heldout")
GIT_CONFIG = (
    "[core]\n\trepositoryformatversion = 0\n\tfilemode = true\n"
    "\tbare = false\n\tlogallrefupdates = false\n"
)
IDENTITY = "EvoCode Benchmark"
EMAIL = "benchmark@example.invalid"
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
SHA1 = re.compile(r"^[0-9a-f]{40}$")


class StagePackageError(ValueError):
    """A package, source snapshot or Git history violates the stage boundary."""


def _git(repo: Path, *args: str, date: str | None = None, binary: bool = False, index_file: Path | None = None):
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_TERMINAL_PROMPT": "0", "GIT_NO_REPLACE_OBJECTS": "1",
                "GIT_AUTHOR_NAME": IDENTITY, "GIT_COMMITTER_NAME": IDENTITY,
                "GIT_AUTHOR_EMAIL": EMAIL, "GIT_COMMITTER_EMAIL": EMAIL,
                "LC_ALL": "C", "TZ": "UTC"})
    if index_file is not None:
        env["GIT_INDEX_FILE"] = str(index_file)
    if date is not None:
        env.update({"GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date})
    command = ["git", "-c", "core.autocrlf=false", "-c", "core.hooksPath=" + os.devnull,
               "-c", "commit.gpgsign=false", "-c", "core.fsmonitor=false", "-C", str(repo), *args]
    result = subprocess.run(command, env=env, capture_output=True, timeout=30)
    if result.returncode:
        raise StagePackageError(f"git {' '.join(args[:2])} failed: {result.stderr.decode(errors='replace').strip()}")
    return result.stdout if binary else result.stdout.decode("utf-8").strip()


def _relative(root: Path, value: Any) -> Path:
    if not isinstance(value, str) or not value or "\\" in value:
        raise StagePackageError("expected a safe root-relative POSIX path")
    parts = PurePosixPath(value)
    if parts.is_absolute() or any(part in ("..", ".") for part in value.split("/")):
        raise StagePackageError(f"unsafe path: {value}")
    result = root.joinpath(*parts.parts)
    # Reject symlinks even when they point back inside the root.
    for parent in (result, *result.parents):
        if parent == root:
            break
        if parent.is_symlink():
            raise StagePackageError(f"symlink path: {value}")
    if not result.resolve().is_relative_to(root):
        raise StagePackageError(f"path escapes benchmark root: {value}")
    return result


def _code_files(directory: Path, *, allow_git: bool = False) -> list[Path]:
    if directory.is_symlink() or not directory.is_dir():
        raise StagePackageError(f"missing or unsafe code directory: {directory}")
    files = []
    for current, directories, names in os.walk(directory, followlinks=False):
        current_path = Path(current)
        for name in list(directories):
            path = current_path / name
            if path.is_symlink():
                raise StagePackageError(f"symlink in code snapshot: {path}")
            if current_path == directory and name == ".git" and allow_git:
                directories.remove(name)
            elif name in (".git", "__pycache__", ".pytest_cache"):
                raise StagePackageError(f"forbidden directory in code snapshot: {path}")
        for name in names:
            path = current_path / name
            if path.is_symlink() or not path.is_file():
                raise StagePackageError(f"nonregular code file: {path}")
            if name == ".git" or name.endswith((".pyc", ".pyo")):
                raise StagePackageError(f"forbidden code file: {path}")
            files.append(path)
    return sorted(files, key=lambda path: path.relative_to(directory).as_posix())


def snapshot_inventory(directory: str | Path, *, allow_git: bool = False) -> list[dict[str, str]]:
    directory = Path(directory)
    return [{"path": path.relative_to(directory).as_posix(),
             "mode": "100755" if path.stat().st_mode & 0o111 else "100644",
             "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
            for path in _code_files(directory, allow_git=allow_git)]


def _inventory_hash(inventory: list[dict[str, str]]) -> str:
    return hashlib.sha256(json.dumps(inventory, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def code_snapshot_sha256(directory: str | Path) -> str:
    return _inventory_hash(snapshot_inventory(directory))


def _tree_inventory(repo: Path) -> list[dict[str, str]]:
    result = []
    for entry in _git(repo, "ls-tree", "-r", "-z", "--full-tree", "HEAD", binary=True).split(b"\0"):
        if not entry:
            continue
        metadata, raw_path = entry.split(b"\t", 1)
        mode, kind, oid = metadata.decode("ascii").split()
        if mode not in ("100644", "100755") or kind != "blob":
            raise StagePackageError("Git tree contains a symlink, submodule or unsupported mode")
        result.append({"path": raw_path.decode("utf-8"), "mode": mode,
                       "sha256": hashlib.sha256(_git(repo, "cat-file", "blob", oid, binary=True)).hexdigest()})
    return sorted(result, key=lambda item: item["path"])


def _object_ids(repo: Path) -> list[str]:
    lines = _git(repo, "cat-file", "--batch-all-objects", "--batch-check=%(objectname)").splitlines()
    return sorted(lines)


def _reachable_ids(repo: Path) -> list[str]:
    return sorted({line.split(" ", 1)[0] for line in _git(repo, "rev-list", "--objects", "HEAD").splitlines()})


def _history(repo: Path) -> list[str]:
    return _git(repo, "rev-list", "--reverse", "HEAD").splitlines()


def _verify_repository(repo: Path, manifest: dict[str, Any], source: Path) -> dict[str, Any]:
    git_dir = repo / ".git"
    if git_dir.is_symlink() or not git_dir.is_dir():
        raise StagePackageError("package must contain an ordinary .git directory")
    expected_files = {"HEAD", "config", "index", "refs/heads/main"}
    for path in git_dir.rglob("*"):
        relative = path.relative_to(git_dir).as_posix()
        if path.is_symlink():
            raise StagePackageError(f"Git symlink is forbidden: {relative}")
        if path.is_dir():
            if relative not in {"objects", "objects/info", "objects/pack", "refs", "refs/heads", "refs/tags"} and not re.fullmatch(r"objects/[0-9a-f]{2}", relative):
                raise StagePackageError(f"forbidden Git directory: {relative}")
        elif relative not in expected_files and not re.fullmatch(r"objects/[0-9a-f]{2}/[0-9a-f]{38}", relative):
            raise StagePackageError(f"forbidden Git metadata: {relative}")
    if (git_dir / "config").read_text() != GIT_CONFIG:
        raise StagePackageError("Git configuration differs from the offline allowlist")
    if (git_dir / "HEAD").read_text() != "ref: refs/heads/main\n":
        raise StagePackageError("unexpected HEAD/ref")
    head = manifest.get("visible_head_sha")
    ancestors = manifest.get("allowed_ancestor_shas")
    if not isinstance(head, str) or not SHA1.fullmatch(head):
        raise StagePackageError("invalid visible_head_sha")
    if not isinstance(ancestors, list) or any(not isinstance(sha, str) or not SHA1.fullmatch(sha) for sha in ancestors):
        raise StagePackageError("invalid allowed_ancestor_shas")
    if (git_dir / "refs/heads/main").read_text() != head + "\n":
        raise StagePackageError("noncanonical branch ref")
    if _git(repo, "rev-parse", "HEAD") != head:
        raise StagePackageError("HEAD does not match the stage manifest")
    if _git(repo, "for-each-ref", "--format=%(refname)") != "refs/heads/main":
        raise StagePackageError("unexpected Git references")
    history = _history(repo)
    if history != [*ancestors, head] or len(history) != len(set(history)):
        raise StagePackageError("Git ancestry differs from allowed strict ancestors")
    for index, sha in enumerate(history):
        parents = _git(repo, "rev-list", "--parents", "-n", "1", sha).split()[1:]
        if parents != ([] if index == 0 else [history[index - 1]]):
            raise StagePackageError("stage history must be a complete linear chain")
    # Git's object reader may ignore trailing compressed bytes. Check the entire
    # loose file, including its object identity, before invoking Git's inventory.
    for object_file in (git_dir / "objects").glob("??/*"):
        if not object_file.is_file():
            continue
        payload = object_file.read_bytes()
        inflater = zlib.decompressobj()
        try:
            decoded = inflater.decompress(payload, MAX_ARCHIVE_BYTES + 1)
        except zlib.error as exc:
            raise StagePackageError("invalid compressed Git object") from exc
        if not inflater.eof or inflater.unused_data or inflater.unconsumed_tail:
            raise StagePackageError("Git object has invalid or trailing data")
        if hashlib.sha1(decoded).hexdigest() != object_file.parent.name + object_file.name:
            raise StagePackageError("Git object identity mismatch")
    with tempfile.TemporaryDirectory(prefix="verify-index-", dir=repo.parent) as temporary:
        index_file = Path(temporary) / "index"
        _git(repo, "read-tree", "HEAD", index_file=index_file)
        if (git_dir / "index").read_bytes() != index_file.read_bytes():
            raise StagePackageError("Git index is not the canonical current tree")
    objects = _object_ids(repo)
    if objects != _reachable_ids(repo):
        raise StagePackageError("unreachable/future Git objects detected")
    if manifest.get("git_object_shas") != objects:
        raise StagePackageError("Git object inventory differs from manifest")
    _git(repo, "fsck", "--full", "--strict", "--no-reflogs")
    tree = _tree_inventory(repo)
    worktree = snapshot_inventory(repo, allow_git=True)
    expected = snapshot_inventory(source)
    expected_directories = {parent.as_posix() for item in expected
                            for parent in PurePosixPath(item["path"]).parents if parent.as_posix() != "."}
    actual_directories = {path.relative_to(repo).as_posix() for path in repo.rglob("*")
                          if path.is_dir() and ".git" not in path.relative_to(repo).parts}
    if actual_directories != expected_directories:
        raise StagePackageError("unexpected code directories in package")
    if tree != expected or worktree != expected:
        raise StagePackageError("Git tree/worktree differs from current source snapshot")
    digest = _inventory_hash(expected)
    if manifest.get("code_snapshot_sha256") != digest:
        raise StagePackageError("code snapshot hash mismatch")
    return {"valid": True, "visible_head_sha": head, "allowed_ancestor_shas": ancestors,
            "git_object_count": len(objects), "code_snapshot_sha256": digest}


def _unpack(package: Path, destination: Path) -> None:
    if package.stat().st_size > MAX_ARCHIVE_BYTES:
        raise StagePackageError("package exceeds size limit")
    with tarfile.open(package, "r|gz") as archive:
        total = 0
        seen = set()
        for member in archive:
            raw = member.name
            parts = PurePosixPath(raw)
            if (not raw or raw.startswith("/") or "\\" in raw or
                    any(part in ("..", ".", "") for part in raw.split("/")) or
                    not (member.isfile() or member.isdir()) or raw in seen):
                raise StagePackageError(f"unsafe archive member: {raw}")
            seen.add(raw)
            total += member.size
            allowed_modes = (0o755,) if member.isdir() else (0o644, 0o755)
            if member.mode not in allowed_modes or member.size < 0 or total > MAX_ARCHIVE_BYTES:
                raise StagePackageError(f"unsafe archive mode or size: {raw}")
            target = destination.joinpath(*parts.parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                stream = archive.extractfile(member)
                if stream is None:
                    raise StagePackageError(f"archive member has no bytes: {raw}")
                with target.open("xb") as output:
                    shutil.copyfileobj(stream, output)
                target.chmod(0o755 if member.mode & 0o111 else 0o644)


def _package_path(root: Path, manifest: dict[str, Any]) -> Path:
    package = _relative(root, manifest.get("git_stage_package"))
    digest = hashlib.sha256(package.read_bytes()).hexdigest()
    if manifest.get("git_stage_package_sha256") != digest:
        raise StagePackageError("stage package SHA256 mismatch")
    return package


def materialize_stage_package(root: str | Path, manifest: dict[str, Any], destination: str | Path) -> dict[str, Any]:
    """Safely extract and verify before making a repository available to a caller.

    Destination must not exist. A failed verification removes the extracted copy.
    """
    root = Path(root).resolve()
    destination = Path(destination)
    if destination.exists() or destination.is_symlink():
        raise StagePackageError("materialization destination must not exist")
    package = _package_path(root, manifest)
    source = _relative(root, manifest.get("code_dir"))
    destination.mkdir(parents=True)
    try:
        _unpack(package, destination)
        return _verify_repository(destination, manifest, source)
    except (OSError, tarfile.TarError, UnicodeError, subprocess.TimeoutExpired, ValueError) as exc:
        shutil.rmtree(destination)
        if isinstance(exc, StagePackageError):
            raise
        raise StagePackageError(str(exc)) from exc


def validate_stage_package(root: str | Path, manifest: dict[str, Any]) -> dict[str, Any]:
    root = Path(root).resolve()
    runtime = root / ".runtime"
    runtime.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="validate-git-", dir=runtime) as temporary:
        return materialize_stage_package(root, manifest, Path(temporary) / "repository")


def _write_archive(repo: Path, package: Path) -> None:
    package.parent.mkdir(parents=True, exist_ok=True)
    with package.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0, compresslevel=9) as zipped:
            with tarfile.open(fileobj=zipped, mode="w", format=tarfile.PAX_FORMAT) as archive:
                for path in sorted(repo.rglob("*"), key=lambda item: item.relative_to(repo).as_posix()):
                    info = tarfile.TarInfo(path.relative_to(repo).as_posix())
                    info.mode = 0o755 if path.is_dir() or path.stat().st_mode & 0o111 else 0o644
                    info.mtime = 0
                    if path.is_dir():
                        info.type = tarfile.DIRTYPE
                        archive.addfile(info)
                    else:
                        payload = path.read_bytes()
                        info.size = len(payload)
                        archive.addfile(info, io.BytesIO(payload))


def build_stage_packages(root: str | Path, repository_id: str = "repo_api") -> list[dict[str, Any]]:
    """Build a linear history sequentially, archiving each stage before the next.

    Source manifests are updated only after all packages pass verification. Fixed
    UTC commit dates/identity, zero-stat index and normalized tar/gzip metadata
    make reconstruction byte-identical for identical snapshots and task IDs.
    """
    root = Path(root).resolve()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", repository_id):
        raise StagePackageError("unsafe repository_id")
    runtime = root / ".runtime"
    runtime.mkdir(exist_ok=True)
    records = []
    with tempfile.TemporaryDirectory(prefix="build-git-", dir=runtime) as temporary:
        repo = Path(temporary) / "repository"
        repo.mkdir()
        _git(repo, "init", "--initial-branch=main", "--object-format=sha1", "--template=")
        (repo / ".git" / "config").write_text(GIT_CONFIG)
        description = repo / ".git" / "description"
        description.unlink(missing_ok=True)
        for index, stage in enumerate(STAGES, 1):
            manifest_path = _relative(root, f"scenarios/{repository_id}/{stage}/manifest.json")
            manifest = json.loads(manifest_path.read_text())
            if manifest.get("repository_id") != repository_id:
                raise StagePackageError("manifest repository_id mismatch")
            source = _relative(root, manifest.get("code_dir"))
            files = _code_files(source)
            for path in repo.iterdir():
                if path.name != ".git":
                    shutil.rmtree(path) if path.is_dir() else path.unlink()
            for path in files:
                target = repo / path.relative_to(source)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(path.read_bytes())
                target.chmod(0o755 if path.stat().st_mode & 0o111 else 0o644)
            _git(repo, "add", "--all", "--force")
            _git(repo, "commit", "--allow-empty", "-m", f"{repository_id}: {manifest['task_id']}",
                 date=f"2026-01-{index:02d}T00:00:00+00:00")
            (repo / ".git" / "COMMIT_EDITMSG").unlink(missing_ok=True)
            (repo / ".git" / "index").unlink()
            _git(repo, "read-tree", "HEAD")
            package = _relative(root, f"git_stage_packages/{repository_id}_{stage}.tar.gz")
            history = _history(repo)
            manifest.update({"git_stage_package": package.relative_to(root).as_posix(),
                             "visible_head_sha": history[-1], "allowed_ancestor_shas": history[:-1],
                             "git_object_shas": _object_ids(repo),
                             "code_snapshot_sha256": code_snapshot_sha256(source)})
            _verify_repository(repo, manifest, source)
            _write_archive(repo, package)
            manifest["git_stage_package_sha256"] = hashlib.sha256(package.read_bytes()).hexdigest()
            validate_stage_package(root, manifest)
            records.append((manifest_path, manifest))
    for path, manifest in records:
        path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    return [manifest for _, manifest in records]
