import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import tarfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("stage_packages", ROOT / "runner/stage_packages.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


@pytest.fixture(scope="module")
def chain_template(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("git-stage-chain")
    for number, stage in enumerate(module.STAGES, 1):
        directory = tmp_path / "scenarios/repo_api" / stage
        code = directory / "code"
        (code / "api").mkdir(parents=True)
        (code / "api/public.py").write_text(f"STAGE = {number}\n")
        (code / "api/stable.py").write_text("def respond(value): return value\n")
        (directory / "task.md").write_text(f"public task {number}\n")
        manifest = {"task_id": f"repo_api_t{number}", "repository_id": "repo_api",
                    "stage": stage.split("_", 1)[1],
                    "code_dir": code.relative_to(tmp_path).as_posix()}
        (directory / "manifest.json").write_text(json.dumps(manifest))
    private = tmp_path / "private/references"
    private.mkdir(parents=True)
    (private / "secret.py").write_text("ANSWER_DO_NOT_LEAK = True\n")
    return tmp_path, module.build_stage_packages(tmp_path)


@pytest.fixture
def chain(chain_template, tmp_path):
    template, manifests = chain_template
    root = tmp_path / "benchmark"
    shutil.copytree(template, root)
    return root, copy.deepcopy(manifests)


def repack(root, manifest, repository):
    modified = copy.deepcopy(manifest)
    package = root / manifest["git_stage_package"]
    module._write_archive(repository, package)
    modified["git_stage_package_sha256"] = hashlib.sha256(package.read_bytes()).hexdigest()
    return modified


def test_stage_history_is_reproducible_traceable_and_future_free(chain):
    root, manifests = chain
    first_hashes = [item["git_stage_package_sha256"] for item in manifests]
    rebuilt = module.build_stage_packages(root)
    assert first_hashes == [item["git_stage_package_sha256"] for item in rebuilt]
    for index, manifest in enumerate(manifests):
        destination = root / f"unpacked-{index}"
        result = module.materialize_stage_package(root, manifest, destination)
        assert result["valid"]
        assert manifest["allowed_ancestor_shas"] == [item["visible_head_sha"] for item in manifests[:index]]
        for older in manifests[:index + 1]:
            historical = module._git(destination, "show", f"{older['visible_head_sha']}:api/public.py")
            assert historical == f"STAGE = {manifests.index(older) + 1}"
        for future in manifests[index + 1:]:
            with pytest.raises(module.StagePackageError):
                module._git(destination, "cat-file", "-e", future["visible_head_sha"])
        assert not (destination / "private").exists()
        assert not (destination / "task.md").exists()
        assert not (destination / ".git/logs").exists()
        assert "remote" not in (destination / ".git/config").read_text()


def test_hash_tampering_is_rejected(chain):
    root, manifests = chain
    package = root / manifests[0]["git_stage_package"]
    package.write_bytes(package.read_bytes() + b"tampered")
    with pytest.raises(module.StagePackageError, match="SHA256"):
        module.validate_stage_package(root, manifests[0])


def test_unreachable_future_object_rejected_even_if_manifest_whitelists_it(chain):
    root, manifests = chain
    first = root / "first"
    future = root / "future"
    module.materialize_stage_package(root, manifests[0], first)
    module.materialize_stage_package(root, manifests[-1], future)
    sha = manifests[-1]["visible_head_sha"]
    relative = Path(".git/objects") / sha[:2] / sha[2:]
    (first / relative).parent.mkdir(exist_ok=True)
    shutil.copyfile(future / relative, first / relative)
    modified = repack(root, manifests[0], first)
    modified["git_object_shas"] = sorted([*modified["git_object_shas"], sha])
    with pytest.raises(module.StagePackageError, match="future"):
        module.validate_stage_package(root, modified)


@pytest.mark.parametrize("metadata", ["logs/HEAD", "objects/info/alternates", "refs/remotes/origin/main", "hooks/post-checkout", "hidden_future.txt"])
def test_git_side_channels_are_rejected(chain, metadata):
    root, manifests = chain
    repo = root / "modified"
    module.materialize_stage_package(root, manifests[0], repo)
    target = repo / ".git" / metadata
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("future-private-data\n")
    modified = repack(root, manifests[0], repo)
    with pytest.raises(module.StagePackageError, match="forbidden Git"):
        module.validate_stage_package(root, modified)


def test_worktree_tampering_does_not_hide_behind_updated_package_hash(chain):
    root, manifests = chain
    repo = root / "modified"
    module.materialize_stage_package(root, manifests[0], repo)
    (repo / "api/public.py").write_text("STAGE = 'secret'\n")
    modified = repack(root, manifests[0], repo)
    with pytest.raises(module.StagePackageError, match="tree/worktree"):
        module.validate_stage_package(root, modified)


@pytest.mark.parametrize("name,kind", [("../escape", "file"), ("/absolute", "file"), ("a/../../escape", "file"), ("a//b", "file"), ("symlink", "symlink"), ("hardlink", "hardlink")])
def test_archive_paths_and_links_are_rejected_before_extraction(chain, name, kind):
    root, manifests = chain
    manifest = copy.deepcopy(manifests[0])
    package = root / manifest["git_stage_package"]
    with tarfile.open(package, "w:gz") as archive:
        info = tarfile.TarInfo(name)
        if kind in ("symlink", "hardlink"):
            info.type = tarfile.SYMTYPE if kind == "symlink" else tarfile.LNKTYPE
            info.linkname = "../escape"
            archive.addfile(info)
        else:
            payload = b"forbidden"
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    manifest["git_stage_package_sha256"] = hashlib.sha256(package.read_bytes()).hexdigest()
    destination = root / "unsafe-copy"
    with pytest.raises(module.StagePackageError, match="unsafe archive"):
        module.materialize_stage_package(root, manifest, destination)
    assert not destination.exists()
    assert not (root / "escape").exists()


def test_source_and_package_root_path_escape_is_rejected(chain):
    root, manifests = chain
    manifest = {**manifests[0], "git_stage_package": "../outside.tar.gz"}
    with pytest.raises(module.StagePackageError, match="unsafe path"):
        module.validate_stage_package(root, manifest)
    source = root / manifests[0]["code_dir"]
    (source / "escape").symlink_to(root / "private/references/secret.py")
    with pytest.raises(module.StagePackageError, match="nonregular code"):
        module.build_stage_packages(root)


@pytest.mark.parametrize("location", ["index", "object"])
def test_hidden_trailing_git_data_is_rejected(chain, location):
    root, manifests = chain
    repo = root / "modified"
    module.materialize_stage_package(root, manifests[0], repo)
    if location == "index":
        target = repo / ".git/index"
    else:
        sha = manifests[0]["visible_head_sha"]
        target = repo / ".git/objects" / sha[:2] / sha[2:]
    target.write_bytes(target.read_bytes() + b"FUTURE_PRIVATE_DATA")
    modified = repack(root, manifests[0], repo)
    with pytest.raises(module.StagePackageError, match="canonical|trailing|corrupt"):
        module.validate_stage_package(root, modified)


def test_remote_configuration_is_rejected(chain):
    root, manifests = chain
    repo = root / "modified"
    module.materialize_stage_package(root, manifests[0], repo)
    config = repo / ".git/config"
    config.write_text(config.read_text() + '[remote "origin"]\n\turl = https://example.invalid/future\n')
    modified = repack(root, manifests[0], repo)
    with pytest.raises(module.StagePackageError, match="configuration"):
        module.validate_stage_package(root, modified)
