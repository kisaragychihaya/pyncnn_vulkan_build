"""Patch ncnn's setup.py so the ncnn shared library lands next to the
python extension module inside the wheel.

setup.py already points CMAKE_LIBRARY_OUTPUT_DIRECTORY at the wheel build
lib dir, which covers libncnn.so on linux and the .pyd module everywhere.
On Windows a SHARED library's .dll is a RUNTIME artifact though, so without
these extra flags ncnn.dll stays in the cmake build tree and delvewheel
cannot find it when repairing the wheel.
"""

import sys


def main():
    path = sys.argv[1]
    with open(path, encoding="utf8") as f:
        src = f.read()

    # single-config generators + the top-level flags
    anchor = '"-DCMAKE_LIBRARY_OUTPUT_DIRECTORY_RELEASE={}".format(extdir),'
    assert src.count(anchor) == 1, "anchor 1 not found, ncnn setup.py changed?"
    src = src.replace(
        anchor,
        anchor + "\n"
        '            "-DCMAKE_RUNTIME_OUTPUT_DIRECTORY={}".format(extdir),\n'
        '            "-DCMAKE_RUNTIME_OUTPUT_DIRECTORY_RELEASE={}".format(extdir),',
    )

    # multi-config (Visual Studio) generators
    anchor2 = '"-DCMAKE_LIBRARY_OUTPUT_DIRECTORY_{}={}".format(cfg.upper(), extdir)\n                ]'
    assert src.count(anchor2) == 1, "anchor 2 not found, ncnn setup.py changed?"
    src = src.replace(
        anchor2,
        '"-DCMAKE_LIBRARY_OUTPUT_DIRECTORY_{}={}".format(cfg.upper(), extdir),\n'
        '                    "-DCMAKE_RUNTIME_OUTPUT_DIRECTORY_{}={}".format(cfg.upper(), extdir)\n'
        "                ]",
    )

    with open(path, "w", encoding="utf8", newline="") as f:
        f.write(src)
    print("patched", path)


if __name__ == "__main__":
    main()
