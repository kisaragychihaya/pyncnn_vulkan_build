"""Check whether an ultralytics model can be exported to ncnn and run on pyncnn + Vulkan.

One model per invocation so a crash on one model cannot take down the rest of the matrix:

    python tools/model_compat.py --model yolov8n.pt --out results/yolov8n.json

The important part is the Vulkan assertion.  ncnn silently flips
``opt.use_vulkan_compute`` back to false when it cannot get a valid device
(``src/net.cpp``: "no valid vulkan device, fallback to cpu"), so an inference
that merely returns correct-looking numbers proves nothing.  Every stage below
records what actually happened instead of assuming success.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

STAGES = ("export", "load", "vulkan", "inference")


class Recorder:
    """Collect per-stage outcomes plus whatever environment facts we can scrape."""

    def __init__(self, model: str):
        self.data = {
            "model": model,
            "ok": False,
            "failed_stage": None,
            "error": None,
            "stages": {s: {"ok": False, "seconds": None, "error": None} for s in STAGES},
            "env": {
                "python": platform.python_version(),
                "platform": platform.platform(),
                "machine": platform.machine(),
                "runner_os": os.environ.get("RUNNER_OS", ""),
                "runner_arch": os.environ.get("RUNNER_ARCH", ""),
                "vulkan_icd": os.environ.get("VK_ICD_FILENAMES") or os.environ.get("VK_DRIVER_FILES") or "",
                "ncnn_vulkan_driver": os.environ.get("NCNN_VULKAN_DRIVER", ""),
            },
            "versions": {},
            "gpus": [],
            "gpu_used": None,
            "vulkan_active": False,
            "params_mb": None,
            "artifacts": {},
            "timing": {},
            "log": [],
        }

    def log(self, msg: str) -> None:
        print(msg, flush=True)
        self.data["log"].append(msg)

    def stage_ok(self, stage: str, seconds: float) -> None:
        self.data["stages"][stage] = {"ok": True, "seconds": round(seconds, 3), "error": None}

    def stage_fail(self, stage: str, exc: BaseException, seconds: float) -> None:
        detail = "".join(traceback.format_exception_only(type(exc), exc)).strip()
        self.data["stages"][stage] = {"ok": False, "seconds": round(seconds, 3), "error": detail}
        self.data["failed_stage"] = stage
        self.data["error"] = detail
        self.log(f"[FAIL] {stage}: {detail}")

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8")


def collect_versions(rec: Recorder) -> None:
    for name in ("ultralytics", "ncnn", "pnnx", "torch"):
        try:
            mod = __import__(name)
            rec.data["versions"][name] = str(getattr(mod, "__version__", "?"))
        except Exception as exc:  # a missing optional dependency is data, not a crash
            rec.data["versions"][name] = f"unavailable ({type(exc).__name__})"


def collect_gpus(rec: Recorder) -> list[str]:
    """Enumerate Vulkan devices as seen by pyncnn."""
    import ncnn

    count = ncnn.get_gpu_count()
    names = []
    for i in range(count):
        try:
            names.append(f"{i}: {ncnn.get_gpu_info(i).device_name()}")
        except Exception as exc:
            names.append(f"{i}: <error {type(exc).__name__}>")
    rec.data["gpus"] = names
    rec.log(f"vulkan devices: {count}")
    for n in names:
        rec.log(f"  {n}")
    return names


def build_net(ncnn, param: Path, model: Path, use_vulkan: bool, device_index: int | None):
    """Load a net, then verify Vulkan is genuinely active when it was requested."""
    net = ncnn.Net()
    net.opt.use_vulkan_compute = use_vulkan
    if use_vulkan and device_index is not None:
        net.set_vulkan_device(device_index)
    net.load_param(str(param))
    net.load_model(str(model))
    if use_vulkan:
        # load_param can silently downgrade the option on a dead driver
        if not net.opt.use_vulkan_compute:
            raise RuntimeError("ncnn reset opt.use_vulkan_compute to False (CPU fallback)")
        if net.vulkan_device() is None:
            raise RuntimeError("Net.vulkan_device() is None after load_param (CPU fallback)")
    return net


def forward(ncnn, param: Path, model: Path, device_index: int | None, size: int, use_vulkan: bool, runs: int):
    """Run a few forward passes and return outputs plus median latency in ms."""
    import numpy as np

    net = build_net(ncnn, param, model, use_vulkan, device_index)
    # ncnn Mat expects CHW.  Feeding HWC makes extract() segfault instead of
    # raising, so getting this shape right is load-bearing.
    data = np.random.RandomState(0).rand(3, size, size).astype(np.float32)
    names = sorted(net.output_names())
    mats, timings = None, []
    for _ in range(runs):
        start = time.perf_counter()
        with net.create_extractor() as ex:
            ex.input(net.input_names()[0], ncnn.Mat(data))
            mats = []
            for name in names:
                code, mat = ex.extract(name)
                if code != 0:
                    raise RuntimeError(f"extract('{name}') returned {code}")
                mats.append(np.array(mat).copy())
        timings.append((time.perf_counter() - start) * 1000)
    return mats, sorted(timings)[len(timings) // 2]


def run_inference_subprocess(param: Path, binary: Path, device_index: int | None, size: int) -> dict:
    """Run the Vulkan/CPU comparison in a child process and return its payload.

    A segfaulting kernel must be reported as a failure, not kill the run, so the
    child communicates through a temp json file and a non-zero exit is turned
    into a readable error.
    """
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "inference.json"
        cmd = [
            sys.executable,
            str(Path(__file__).resolve()),
            "--infer-child",
            "--param", str(param),
            "--bin", str(binary),
            "--imgsz", str(size),
            "--child-out", str(out),
        ]
        if device_index is not None:
            cmd += ["--device", str(device_index)]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)

        # The result file is the source of truth, not the exit code: after the
        # payload is written the child can still die in an ncnn worker thread,
        # and that says nothing about whether the inference succeeded.
        if out.is_file():
            try:
                payload = json.loads(out.read_text(encoding="utf-8"))
            except ValueError:
                payload = None
            if payload is not None:
                if proc.returncode != 0:
                    payload["child_exit_code"] = proc.returncode
                return payload

        tail = (proc.stderr or proc.stdout or "").strip().splitlines()
        detail = tail[-1] if tail else "no output"
        if proc.returncode < 0 or proc.returncode in (139, 3221225477):
            raise RuntimeError(f"inference crashed before producing a result (exit {proc.returncode}): {detail}")
        raise RuntimeError(f"inference child failed (exit {proc.returncode}): {detail}")


def inference_child(args: argparse.Namespace) -> int:
    """Body of the inference subprocess."""
    import numpy as np

    import ncnn

    param = Path(args.param)
    binary = Path(args.bin)
    vk_mats, vk_ms = forward(ncnn, param, binary, args.device, args.imgsz, True, 3)
    cpu_mats, cpu_ms = forward(ncnn, param, binary, args.device, args.imgsz, False, 3)

    payload = {
        "output_shapes": [list(m.shape) for m in vk_mats],
        "timing": {"vulkan_ms_median": round(vk_ms, 2), "cpu_ms_median": round(cpu_ms, 2)},
    }
    # Compare against the magnitude of the CPU output: Vulkan runs fp16 kernels,
    # so a raw absolute epsilon would flag every model.  NaN/inf are still fatal.
    max_diff = 0.0
    scale = 1e-9
    non_finite = 0
    for a, b in zip(vk_mats, cpu_mats):
        if a.shape != b.shape:
            raise RuntimeError(f"Vulkan output shape {a.shape} != CPU output shape {b.shape}")
        if not a.size:
            continue
        a64 = a.astype(np.float64)
        b64 = b.astype(np.float64)
        non_finite += int(np.count_nonzero(~np.isfinite(a64)))
        max_diff = max(max_diff, float(np.max(np.abs(a64 - b64))))
        scale = max(scale, float(np.max(np.abs(b64))))
    rel_diff = max_diff / scale
    payload["vulkan_cpu_max_abs_diff"] = round(max_diff, 6)
    payload["vulkan_cpu_rel_diff"] = round(rel_diff, 6)
    payload["vulkan_non_finite"] = non_finite
    # 1% of the output range: comfortably above fp16 noise, far below a broken kernel.
    payload["vulkan_matches_cpu"] = bool(non_finite == 0 and rel_diff < 0.01)

    Path(args.child_out).write_text(json.dumps(payload, indent=2), encoding="utf-8")
    # Flush and skip teardown: the payload is on disk and ncnn may segfault
    # while destroying the Vulkan device.
    _exit_hard(0)


def main() -> int:
    ap = argparse.ArgumentParser()
    # Child-process mode reuses this entry point so there is exactly one way to
    # load a net and run it.
    ap.add_argument("--infer-child", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--param", help=argparse.SUPPRESS)
    ap.add_argument("--bin", help=argparse.SUPPRESS)
    ap.add_argument("--child-out", help=argparse.SUPPRESS)
    ap.add_argument("--model", help="ultralytics weight name, e.g. yolov8n.pt")
    ap.add_argument("--out", help="path of the result json to write")
    ap.add_argument("--imgsz", type=int, default=640)
    ap.add_argument("--device", type=int, default=None, help="preferred Vulkan device index")
    ap.add_argument("--workdir", default="work", help="scratch dir for weights and exports")
    args = ap.parse_args()

    if args.infer_child:
        return inference_child(args)
    if not args.model or not args.out:
        ap.error("--model and --out are required")

    rec = Recorder(args.model)
    out_path = Path(args.out)
    workdir = Path(args.workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    collect_versions(rec)

    import ncnn

    gpus = collect_gpus(rec)
    if not gpus:
        # Without a device there is nothing to validate; say so instead of
        # producing a green row that was really CPU-only.
        rec.data["failed_stage"] = "vulkan"
        rec.data["error"] = "no Vulkan device available in the runner"
        rec.log("[FAIL] no Vulkan device available")
        rec.save(out_path)
        return 2

    device_index = args.device if args.device is not None else ncnn.get_default_gpu_index()
    if not 0 <= device_index < len(gpus):
        device_index = ncnn.get_default_gpu_index()
    rec.data["gpu_used"] = gpus[device_index]

    # ---- export -------------------------------------------------------
    start = time.time()
    try:
        from ultralytics import YOLO

        # Pass an explicit path so both the download and the export land in workdir.
        weight = workdir / args.model
        model = YOLO(str(weight))
        export_dir = None
        export_error = None
        # Try the real input size first; only shrink if pnnx chokes on it.
        for arg in (args.imgsz, 320, 128):
            try:
                export_dir = Path(str(model.export(format="ncnn", imgsz=arg, device="cpu")))
                rec.data["export_imgsz"] = arg
                break
            except Exception as exc:
                export_error = exc
                rec.log(f"export imgsz={arg} failed: {type(exc).__name__}: {exc}")
        if export_dir is None or not export_dir.is_dir():
            raise export_error or RuntimeError("export produced no *_ncnn_model directory")

        params = sorted(export_dir.glob("*.param"))
        bins = sorted(export_dir.glob("*.bin"))
        if not params or not bins:
            raise RuntimeError(f"{export_dir} has no .param/.bin pair")
        rec.data["artifacts"] = {
            "dir": export_dir.name,
            "param": params[0].name,
            "bin": bins[0].name,
            "files": sorted(p.name for p in export_dir.iterdir()),
        }
        rec.data["params_mb"] = round(bins[0].stat().st_size / 1e6, 2)
        rec.stage_ok("export", time.time() - start)
        rec.log(f"exported -> {export_dir.name} ({rec.data['params_mb']} MB)")
    except BaseException as exc:
        rec.stage_fail("export", exc, time.time() - start)
        rec.save(out_path)
        return 1

    param, binary = params[0], bins[0]

    # ---- load (CPU, parsing only) -------------------------------------
    start = time.time()
    try:
        net = build_net(ncnn, param, binary, False, None)
        rec.data["input_names"] = list(net.input_names())
        rec.data["output_names"] = list(net.output_names())
        rec.data["layer_count"] = len(net.layers())
        rec.stage_ok("load", time.time() - start)
        rec.log(
            f"loaded: {rec.data['layer_count']} layers, in={rec.data['input_names']}, "
            f"out={rec.data['output_names']}"
        )
    except BaseException as exc:
        rec.stage_fail("load", exc, time.time() - start)
        rec.save(out_path)
        return 1

    # ---- vulkan: does the net actually come up on the device? ----------
    start = time.time()
    try:
        net = build_net(ncnn, param, binary, True, device_index)
        rec.data["vulkan_active"] = True
        # VulkanDevice.info is a bound method, not a property
        rec.data["vulkan_device_name"] = net.vulkan_device().info().device_name()
        rec.stage_ok("vulkan", time.time() - start)
        rec.log(f"vulkan enabled on {rec.data['vulkan_device_name']}")
    except BaseException as exc:
        rec.stage_fail("vulkan", exc, time.time() - start)
        rec.save(out_path)
        return 1

    # ---- inference: Vulkan and CPU must agree -------------------------
    # Run in a subprocess: a broken ncnn kernel segfaults rather than raising,
    # and an in-process crash would take the whole result file down with it.
    start = time.time()
    try:
        size = rec.data.get("export_imgsz", args.imgsz)
        payload = run_inference_subprocess(param, binary, device_index, size)
        rec.data.update(payload)
        if not payload.get("vulkan_matches_cpu"):
            if payload.get("vulkan_non_finite"):
                raise RuntimeError(f"Vulkan output has {payload['vulkan_non_finite']} non-finite value(s)")
            raise RuntimeError(
                f"Vulkan output diverges from CPU "
                f"(rel {payload.get('vulkan_cpu_rel_diff')}, abs {payload.get('vulkan_cpu_max_abs_diff')})"
            )
        rec.stage_ok("inference", time.time() - start)
        rec.log(
            f"inference ok: shapes={payload['output_shapes']} "
            f"vk={payload['timing']['vulkan_ms_median']}ms cpu={payload['timing']['cpu_ms_median']}ms "
            f"rel_diff={payload['vulkan_cpu_rel_diff']}"
        )
    except BaseException as exc:
        rec.stage_fail("inference", exc, time.time() - start)
        rec.save(out_path)
        return 1

    rec.data["ok"] = True
    rec.save(out_path)
    return 0


def _exit_hard(code: int) -> None:
    """Exit without running ncnn/vulkan teardown.

    After a successful run the process can still segfault while destroying the
    Vulkan device, which would turn a clean pass into a CI failure.  The result
    is already on disk by this point, so skip destructors entirely.
    """
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)


if __name__ == "__main__":
    # os._exit on the way out: ncnn can segfault during interpreter teardown
    # after a fully successful run, which would fail the job for no reason.
    _exit_hard(main())
