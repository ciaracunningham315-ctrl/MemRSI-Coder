#!/usr/bin/env python3
"""Build a reusable, network-prepared image for EvoCode's offline tool containers."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import tempfile
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE_PACKAGES = (
    "fastapi>=0.115,<1",
    "uvicorn>=0.30,<1",
    "httpx>=0.27,<1",
    "pytest>=8,<9",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build an image containing dependencies before tools run in isolated "
            "--network none containers."
        )
    )
    parser.add_argument("--tag", default="evocode-runtime:py312", help="Docker image tag")
    parser.add_argument("--base-image", default="python:3.12-slim", help="Python base image")
    parser.add_argument(
        "--requirements",
        action="append",
        type=Path,
        default=[],
        metavar="FILE",
        help="requirements file to install in the image (repeatable)",
    )
    parser.add_argument(
        "--project",
        type=Path,
        help="project directory; automatically include requirements.txt and requirements-dev.txt",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    requirements = [path.resolve() for path in args.requirements]
    if args.project:
        project = args.project.resolve()
        if not project.is_dir():
            raise SystemExit(f"Project directory does not exist: {project}")
        for name in ("requirements.txt", "requirements-dev.txt"):
            candidate = project / name
            if candidate.is_file() and candidate not in requirements:
                requirements.append(candidate)
    missing = [str(path) for path in requirements if not path.is_file()]
    if missing:
        raise SystemExit("Requirements file not found: " + ", ".join(missing))

    with tempfile.TemporaryDirectory(prefix="evocode-docker-env-") as tmp:
        context = Path(tmp)
        requirement_names = []
        for index, source in enumerate(requirements):
            target_name = f"requirements-{index}.txt"
            shutil.copyfile(source, context / target_name)
            requirement_names.append(target_name)

        if args.project and (args.project.resolve() / "pyproject.toml").is_file():
            project_config = tomllib.loads((args.project.resolve() / "pyproject.toml").read_text())
            project_table = project_config.get("project", {})
            dependencies = list(project_table.get("dependencies", []))
            dependencies.extend(project_table.get("optional-dependencies", {}).get("dev", []))
            if dependencies:
                target_name = "project-dependencies.txt"
                (context / target_name).write_text("\n".join(dependencies) + "\n")
                requirement_names.append(target_name)

        install = " ".join(f"-r /tmp/{name}" for name in requirement_names)
        dockerfile = [
            f"FROM {args.base_image}",
            "RUN " + json.dumps(["python", "-m", "pip", "install", "--no-cache-dir", *BASE_PACKAGES]),
        ]
        if requirement_names:
            dockerfile.extend(
                [
                    *[f"COPY {name} /tmp/{name}" for name in requirement_names],
                    "RUN python -m pip install --no-cache-dir " + install,
                ]
            )
        dockerfile.extend(
            [
                'RUN python -c "import fastapi, httpx, pytest, uvicorn"',
                "WORKDIR /workspace",
            ]
        )
        (context / "Dockerfile").write_text("\n".join(dockerfile) + "\n")
        command = ["docker", "build", "--tag", args.tag, str(context)]
        print(f"Building {args.tag} with network access for dependency preparation...", flush=True)
        result = subprocess.run(command, check=False)
        if result.returncode:
            return result.returncode

    print(f"Prepared image: {args.tag}")
    print("Set config.runtime.docker_image to this tag before running isolated tools.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
