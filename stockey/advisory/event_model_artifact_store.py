from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

import pandas as pd
from environs import Env

from advisory.event_meta_model import DEFAULT_ARTIFACT_DIR, DEFAULT_MODEL_BASENAME, _artifact_paths, load_model_metadata


env = Env()
env.read_env()

DEFAULT_S3_PREFIX = env.str("EVENT_MODEL_ARTIFACT_S3_PREFIX", "models/advisory_event_meta_model")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_default(value: Any) -> Any:
    if isinstance(value, pd.Timestamp):
        return None if pd.isna(value) else value.isoformat()
    if isinstance(value, Path):
        return str(value)
    return str(value)


def build_artifact_manifest(*, artifact_dir: Path, model_basename: str, s3_prefix: str) -> dict[str, Any]:
    model_path, meta_path = _artifact_paths(artifact_dir, model_basename)
    missing = [str(path) for path in [model_path, meta_path] if not path.exists()]
    if missing:
        raise FileNotFoundError(f"missing event-model artifact files: {', '.join(missing)}")

    metadata = load_model_metadata(artifact_dir, model_basename)
    model_version = str(metadata.get("model_version") or model_basename)
    uploaded_at = pd.Timestamp.utcnow().strftime("%Y%m%dT%H%M%SZ")
    base_prefix = s3_prefix.strip("/").rstrip("/")
    version_prefix = f"{base_prefix}/{model_basename}/{model_version}/{uploaded_at}"
    latest_prefix = f"{base_prefix}/{model_basename}/latest"

    files = [
        {
            "role": "model",
            "local_path": str(model_path),
            "filename": model_path.name,
            "sha256": _sha256_file(model_path),
            "bytes": model_path.stat().st_size,
            "versioned_key": f"{version_prefix}/{model_path.name}",
            "latest_key": f"{latest_prefix}/{model_path.name}",
        },
        {
            "role": "metadata",
            "local_path": str(meta_path),
            "filename": meta_path.name,
            "sha256": _sha256_file(meta_path),
            "bytes": meta_path.stat().st_size,
            "versioned_key": f"{version_prefix}/{meta_path.name}",
            "latest_key": f"{latest_prefix}/{meta_path.name}",
        },
    ]
    return {
        "status": "planned",
        "uploaded_at": uploaded_at,
        "artifact_dir": str(artifact_dir),
        "model_basename": model_basename,
        "model_name": metadata.get("model_name"),
        "model_version": model_version,
        "horizon_days": metadata.get("horizon_days"),
        "return_threshold": metadata.get("return_threshold"),
        "s3_prefix": base_prefix,
        "version_prefix": version_prefix,
        "latest_prefix": latest_prefix,
        "metadata": metadata,
        "files": files,
    }


def publish_event_model_artifacts(
    *,
    artifact_dir: Path = DEFAULT_ARTIFACT_DIR,
    model_basename: str = DEFAULT_MODEL_BASENAME,
    s3_prefix: str = DEFAULT_S3_PREFIX,
    dry_run: bool = False,
) -> dict[str, Any]:
    manifest = build_artifact_manifest(
        artifact_dir=Path(artifact_dir),
        model_basename=str(model_basename),
        s3_prefix=str(s3_prefix),
    )
    manifest_payload = json.dumps(manifest, indent=2, ensure_ascii=False, default=_json_default, sort_keys=True)
    manifest_file = {
        "role": "manifest",
        "local_path": None,
        "filename": "manifest.json",
        "sha256": hashlib.sha256(manifest_payload.encode("utf-8")).hexdigest(),
        "bytes": len(manifest_payload.encode("utf-8")),
        "versioned_key": f"{manifest['version_prefix']}/manifest.json",
        "latest_key": f"{manifest['latest_prefix']}/manifest.json",
    }
    manifest["files"].append(manifest_file)

    if dry_run:
        manifest["status"] = "dry_run"
        return manifest

    from utils.store import save_file, save_file_content

    uploaded: list[dict[str, Any]] = []
    for file_info in manifest["files"]:
        for key_name in ["versioned_key", "latest_key"]:
            key = file_info[key_name]
            if file_info["role"] == "manifest":
                save_file_content(key, manifest_payload)
            else:
                save_file(file_info["local_path"], key=key)
            uploaded.append({"role": file_info["role"], "key": key})
    manifest["status"] = "uploaded"
    manifest["uploaded"] = uploaded
    return manifest


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Upload trained advisory event-model artifacts to S3-compatible storage.")
    parser.add_argument("--artifact-dir", default=str(DEFAULT_ARTIFACT_DIR))
    parser.add_argument("--model-basename", default=DEFAULT_MODEL_BASENAME)
    parser.add_argument("--s3-prefix", default=DEFAULT_S3_PREFIX)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = publish_event_model_artifacts(
        artifact_dir=Path(args.artifact_dir),
        model_basename=args.model_basename,
        s3_prefix=args.s3_prefix,
        dry_run=bool(args.dry_run),
    )
    print(json.dumps(result, indent=2, ensure_ascii=False, default=_json_default))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
