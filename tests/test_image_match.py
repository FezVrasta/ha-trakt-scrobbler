"""Difference-hash matcher tests. Dependency-light (needs Pillow only)."""

from __future__ import annotations

import importlib.util
import os
import sys

_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "custom_components", "trakt_scrobbler", "image_match.py",
)
_spec = importlib.util.spec_from_file_location("trakt_image_match", _PATH)
im = importlib.util.module_from_spec(_spec)
sys.modules["trakt_image_match"] = im
_spec.loader.exec_module(im)


def _check(c, m):
    if not c:
        raise AssertionError(m)


def test_hamming() -> None:
    _check(im.hamming(0b1010, 0b1000) == 1, "hamming basic")
    _check(im.hamming(0xFF, 0x00) == 8, "hamming all bits")


def test_hash_identical_image_zero_distance() -> None:
    if not im.available():
        print("  (Pillow unavailable — skipping image tests)")
        return
    from PIL import Image
    import io

    buf = io.BytesIO()
    # a non-uniform gradient so the hash is meaningful
    img = Image.new("L", (64, 36))
    img.putdata([(x * 7 + y * 3) % 256 for y in range(36) for x in range(64)])
    img.save(buf, format="PNG")
    data = buf.getvalue()
    h1 = im.hash_bytes(data)
    h2 = im.hash_bytes(data)
    _check(h1 is not None and h1 == h2, "identical images must hash equal")
    _check(im.hamming(h1, h2) == 0, "identical distance is 0")


def test_hash_different_images_far_apart() -> None:
    if not im.available():
        return
    from PIL import Image
    import io

    def png(fn):
        b = io.BytesIO(); img = Image.new("L", (64, 36)); img.putdata([fn(x, y) for y in range(36) for x in range(64)]); img.save(b, format="PNG"); return b.getvalue()

    a = im.hash_bytes(png(lambda x, y: (x * 7) % 256))
    b = im.hash_bytes(png(lambda x, y: (y * 11) % 256))
    _check(im.hamming(a, b) > 30, "structurally different images should be far apart")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    fails = 0
    for t in tests:
        try:
            t(); print(f"ok    {t.__name__}")
        except AssertionError as e:
            fails += 1; print(f"FAIL  {t.__name__}: {e}")
    print(f"\n{len(tests)-fails}/{len(tests)} passed")
    sys.exit(1 if fails else 0)
