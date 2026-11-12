"""Upload this repository to the Hugging Face Space named in deploy/hf_space_id.

The token is read from HF_TOKEN or HUGGING_FACE_HUB_TOKEN. It is not printed.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from huggingface_hub import HfApi


def space_id() -> str:
    override = os.environ.get("HF_SPACE_ID", "").strip()
    if override:
        return override
    path = Path("deploy/hf_space_id")
    for line in path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if text and not text.startswith("#"):
            return text
    raise SystemExit("deploy/hf_space_id has no Space id")


def main() -> None:
    token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")
    if not token:
        raise SystemExit("HF_TOKEN is not set. Add it as a repository secret. Nothing was uploaded.")
    repo_id = space_id()
    if repo_id.startswith("OWNER/") or "/" not in repo_id:
        raise SystemExit(
            f"Space id {repo_id!r} is still a placeholder. Set deploy/hf_space_id to owner/orbitguard."
        )
    api = HfApi(token=token)
    # The existing Space is private. Do not pass private=False: that asks
    # the Hub to create a public repo. exist_ok leaves an existing repo's
    # visibility alone.
    api.create_repo(
        repo_id=repo_id,
        repo_type="space",
        space_sdk="docker",
        exist_ok=True,
        private=True,
    )
    api.upload_folder(
        repo_id=repo_id,
        repo_type="space",
        folder_path=".",
        commit_message="Publish OrbitGuard monitoring Space",
        ignore_patterns=[
            ".git/**",
            ".git*",
            ".venv/**",
            "**/__pycache__/**",
            ".pytest_cache/**",
            ".env",
            "**/*.db",
            "**/.cache/**",
        ],
    )
    print(f"Uploaded Docker Space https://huggingface.co/spaces/{repo_id}")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - surface the hub error without the token
        print(f"Space upload failed: {exc.__class__.__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
