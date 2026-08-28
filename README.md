# pyncnn_vulkan_build

基于 GitHub Actions 编译 [Tencent/ncnn](https://github.com/Tencent/ncnn) 的 python 绑定（pyncnn），
启用 **Vulkan** 并使用**动态库**（`NCNN_SHARED_LIB=ON`）。

## 产物

每个平台 5 个 CPython 版本的 wheel（cp310 / cp311 / cp312 / cp313 / cp314，不含 free-threaded 的
cp313t/cp314t），以及对应平台的 ncnn SDK 包（动态库 + 头文件 + cmake 配置）：

| 平台 | wheel artifact | SDK artifact |
| --- | --- | --- |
| windows-amd64 (`windows-2025`) | `wheels-windows-amd64` | `sdk-windows-amd64` (zip: `ncnn.dll` / `ncnn.lib` / `include/` / cmake) |
| windows-arm64 (`windows-11-arm`) | `wheels-windows-arm64` | `sdk-windows-arm64` (zip) |
| linux-amd64 (`ubuntu-24.04`, manylinux) | `wheels-linux-amd64` | `sdk-linux-amd64` (tar.gz: `libncnn.so*` / `include/` / cmake) |
| linux-arm64 (`ubuntu-24.04-arm`, manylinux aarch64) | `wheels-linux-arm64` | `sdk-linux-arm64` (tar.gz) |

打 tag 推送时，所有 wheel 和 SDK 包会自动附加到对应的 GitHub Release。

## 使用

- Actions 页面手动运行 `pyncnn-vulkan-build` workflow，可通过 `ncnn_ref` 输入指定 ncnn 的
  tag / branch / commit（默认 `master`）。
- 或推送一个 tag，构建完成后自动发布 Release。

## 实现说明

- Vulkan：ncnn 默认 `NCNN_SIMPLEVK=ON`（内置 vulkan loader，运行时 `dlopen` 系统的
  `vulkan-1.dll` / `libvulkan.so.1`，找不到则回退 CPU），因此编译期**无需安装 Vulkan SDK**。
- 动态库进 wheel：
  - `tools/patch_setup.py` 给 ncnn 的 `setup.py` 补上 `CMAKE_RUNTIME_OUTPUT_DIRECTORY`，
    让 Windows 下的 `ncnn.dll` 落到扩展模块旁边，随 wheel 一起打包，再由 delvewheel repair。
  - Linux 下 `libncnn.so.1.0.<date>` 及其符号链接会被 wheel 解引用成 3 份拷贝，
    `tools/dedup_wheel.py` 只保留扩展模块实际链接的 `libncnn.so.1` 并刷新 `RECORD`。
- 官方 ncnn CI（`.github/workflows/release-python.yml`）已验证 windows-11-arm 可构建
  cp310–cp314 的 win_arm64 wheel，本仓库矩阵与其保持一致。

## OpenCV win32 SDK

`opencv-win32-build` workflow（`.github/workflows/opencv.yml`）编译 OpenCV **32 位 Windows**
版（windows-2025 + MSVC x86），启用 opencv_contrib extra modules
（`OPENCV_EXTRA_MODULES_PATH`），关闭 apps / tests / perf-tests / docs，以及 Java(JNI) /
JS / Python 绑定，只保留 C/C++ API。

- Actions 页面手动运行，可通过 `opencv_ref` 输入指定 opencv 与 opencv_contrib 的
  tag / branch / commit（默认 `master`，两个仓库使用同一 ref 以保证版本一致）。
- `cmake --install` 的结果打包为 `opencv-contrib-win32.zip`，上传为
  `sdk-opencv-windows-win32` artifact（仅 Actions 存储，不随 Release 发布）。
