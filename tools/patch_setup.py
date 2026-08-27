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

    # 3. After the CMake build finishes, copy ncnn.dll next to the extension
    # module on Windows.  CMAKE_RUNTIME_OUTPUT_DIRECTORY is not always honoured
    # (e.g. target-level properties or multi-config generators may override it),
    # so an explicit copy guarantees the DLL is inside the un-repaired wheel.
    build_anchor = '''        subprocess.check_call(
            ["cmake", "--build", "."] + build_args, cwd=self.build_temp
        )'''
    assert src.count(build_anchor) == 1, "build anchor not found, ncnn setup.py changed?"
    src = src.replace(
        build_anchor,
        build_anchor + '''\n
        # Package the ncnn shared library (RUNTIME artifact on Windows).
        if sys.platform == "win32":
            import glob
            dll_candidates = glob.glob(
                os.path.join(self.build_temp, "**", "ncnn.dll"), recursive=True
            )
            # CMake may already have placed ncnn.dll directly in extdir via
            # CMAKE_RUNTIME_OUTPUT_DIRECTORY; setuptools will then copy it into
            # the wheel.  Fall back to the build tree only if it is not there.
            ext_dll = os.path.join(extdir, "ncnn.dll")
            if not dll_candidates and os.path.exists(ext_dll):
                dll_candidates = [ext_dll]
            if dll_candidates:
                for dll in dll_candidates:
                    if os.path.abspath(dll) != os.path.abspath(ext_dll):
                        shutil.copy2(dll, extdir)
                        print("copied ncnn.dll to", extdir, "from", dll)
                    else:
                        print("ncnn.dll already at", extdir)
            else:
                print("WARNING: ncnn.dll not found under", self.build_temp)''',
    )

    with open(path, "w", encoding="utf8", newline="") as f:
        f.write(src)
    print("patched", path)


if __name__ == "__main__":
    main()
