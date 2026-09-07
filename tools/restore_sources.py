"""One-time transport bootstrap for a text-only GitHub connection.

A successful build commits the normal source tree and removes the transport
bundle. Subsequent builds simply use the checked-in sources. No runtime use.
"""
import base64
import hashlib
import json
import lzma
from pathlib import Path, PurePosixPath

root = Path.cwd()
parts = sorted((root / ".bootstrap").glob("source-*.b64"))
if parts:
    encoded = b"".join(b"".join(part.read_bytes().split()) for part in parts)
    if len(encoded) > 1_000_000:
        raise SystemExit("Source transport is too large")
    decompressor = lzma.LZMADecompressor()
    raw = decompressor.decompress(base64.b64decode(encoded, validate=True), max_length=4_000_000)
    if not decompressor.eof:
        raise SystemExit("Source transport exceeds the decoded size limit")
    expected = (root / ".bootstrap" / "source.sha256").read_text().strip()
    if hashlib.sha256(raw).hexdigest() != expected:
        raise SystemExit("Source transport checksum mismatch")
    files = json.loads(raw)
    if not isinstance(files, dict) or len(files) > 80:
        raise SystemExit("Invalid source manifest")
    for name, content in files.items():
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts or path.parts[0] in (".git", ".github", ".bootstrap") or not isinstance(content, str):
            raise SystemExit("Unsafe path in source transport")
        target = root.joinpath(*path.parts)
        if not target.resolve().is_relative_to(root.resolve()):
            raise SystemExit("Source path escapes repository")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="")
    print("Restored", len(files), "verified source files")
else:
    print("Using the normal checked-in source tree")
