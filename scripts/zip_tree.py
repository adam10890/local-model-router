"""Write a directory as an atomic, ZIP64-capable stored archive."""
from __future__ import annotations

import sys
import zipfile
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 3:
        raise SystemExit("usage: zip_tree.py <source-directory> <archive.zip>")
    source = Path(sys.argv[1]).resolve(strict=True)
    archive = Path(sys.argv[2]).resolve(strict=False)
    if not source.is_dir():
        raise SystemExit(f"source directory does not exist: {source}")
    temporary = archive.with_name(f".{archive.name}.tmp")
    archive.parent.mkdir(parents=True, exist_ok=True)
    temporary.unlink(missing_ok=True)
    try:
        with zipfile.ZipFile(
            temporary,
            mode="w",
            compression=zipfile.ZIP_STORED,
            allowZip64=True,
        ) as bundle:
            for path in sorted(source.rglob("*")):
                if path.is_file():
                    bundle.write(path, path.relative_to(source).as_posix())
        temporary.replace(archive)
    finally:
        temporary.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
