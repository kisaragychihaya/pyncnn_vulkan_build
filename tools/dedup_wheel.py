"""Remove duplicated libncnn copies from manylinux wheels.

ncnn's cmake sets VERSION/SOVERSION on the shared library, producing the
real file libncnn.so.1.0.<date> plus the libncnn.so.1 and libncnn.so
symlinks. The wheel build dereferences symlinks, so every wheel would ship
three full copies of the same library. Keep only libncnn.so.1 (the SONAME
the extension module links against) and refresh the RECORD file.

Usage: python dedup_wheel.py <wheel> [<wheel>...]
"""

import base64
import csv
import hashlib
import io
import sys
import zipfile


def should_drop(name):
    if name == "ncnn/libncnn.so":
        return True
    # ncnn/libncnn.so.1.0.<date> (but keep ncnn/libncnn.so.1)
    if name.startswith("ncnn/libncnn.so.1."):
        return True
    return False


def record_hash(data):
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest())
    return "sha256=" + digest.rstrip(b"=").decode("ascii")


def dedup(path):
    with zipfile.ZipFile(path) as zin:
        data = {info.filename: zin.read(info.filename) for info in zin.infolist()}

    record_name = next(n for n in data if n.endswith(".dist-info/RECORD"))

    dropped = [n for n in data if should_drop(n)]
    if not dropped:
        print("nothing to drop in", path)
        return

    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    for name in sorted(data):
        if should_drop(name) or name == record_name:
            continue
        writer.writerow((name, record_hash(data[name]), len(data[name])))
    writer.writerow((record_name, "", ""))
    data[record_name] = buf.getvalue().encode("utf8")

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zout:
        for name, blob in data.items():
            if should_drop(name):
                continue
            zout.writestr(name, blob)

    print("dropped", ", ".join(dropped), "from", path)


if __name__ == "__main__":
    for path in sys.argv[1:]:
        dedup(path)
