"""Pack skills_library content into compressed content-addressed blobs.

This is the bridge between today's on-disk skills_library and a future R2
content store. It does not upload anything; it creates a deterministic local
directory that can be synced to object storage:

    python pack_content_blobs.py
    aws s3 sync content_blobs s3://$R2_BUCKET/skills-content/

The blob key is the normalized SKILL.md content hash, so duplicate skill files
only store one compressed copy.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import tempfile
from pathlib import Path

from quality import content_hash


DEFAULT_LIBRARY_DIR = Path(__file__).parent / "skills_library"
DEFAULT_OUTPUT_DIR = Path(__file__).parent / "content_blobs"


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _blob_relpath(hash_value: str) -> str:
    return f"{hash_value[:2]}/{hash_value}.md.gz"


def pack_content_blobs(library_dir: Path = DEFAULT_LIBRARY_DIR, output_dir: Path = DEFAULT_OUTPUT_DIR) -> dict:
    index_path = library_dir / "index.json"
    files_dir = library_dir / "files"
    if not index_path.exists():
        raise FileNotFoundError(f"{index_path} does not exist")
    raw_index = json.loads(index_path.read_text(encoding="utf-8-sig", errors="replace"))

    manifest: dict[str, dict] = {}
    missing_files = 0
    entries = 0
    for url, entry in raw_index.items():
        filename = entry.get("file") if isinstance(entry, dict) else ""
        if not filename:
            continue
        source_path = files_dir / filename
        if not source_path.exists():
            missing_files += 1
            continue

        text = source_path.read_text(encoding="utf-8", errors="replace")
        hash_value = entry.get("content_hash") or content_hash(text)
        if not hash_value:
            continue
        relpath = _blob_relpath(hash_value)
        out_path = output_dir / relpath
        encoded = text.encode("utf-8")
        if hash_value not in manifest:
            compressed = gzip.compress(encoded, compresslevel=9, mtime=0)
            _write_atomic(out_path, compressed)
            manifest[hash_value] = {
                "path": relpath,
                "bytes": len(encoded),
                "compressed_bytes": len(compressed),
                "urls": [],
                "count": 0,
            }
        manifest[hash_value]["urls"].append(url)
        manifest[hash_value]["count"] += 1
        entries += 1

    manifest_path = output_dir / "manifest.json"
    manifest_payload = {
        "source_library": str(library_dir),
        "entries": entries,
        "unique_blobs": len(manifest),
        "missing_files": missing_files,
        "blobs": dict(sorted(manifest.items())),
    }
    _write_atomic(
        manifest_path,
        json.dumps(manifest_payload, indent=2, sort_keys=True).encode("utf-8"),
    )
    return manifest_payload


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Pack skills_library into gzip content-hash blobs.")
    parser.add_argument("--library-dir", type=Path, default=DEFAULT_LIBRARY_DIR)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    manifest = pack_content_blobs(args.library_dir, args.output_dir)
    print(
        "content blobs: "
        f"entries={manifest['entries']}, "
        f"unique_blobs={manifest['unique_blobs']}, "
        f"missing_files={manifest['missing_files']}, "
        f"output={args.output_dir}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
