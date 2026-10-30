"""Merge per-model compatibility results into one markdown report.

    python tools/model_compat_report.py --results results --out report.md

Every result file is one model x one platform.  Rows are grouped by model so a
reader can see, per model, whether it exported, loaded, ran on Vulkan, and
matched CPU output.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

STAGES = ("export", "load", "vulkan", "inference")


def load_results(results_dir: Path) -> list[dict]:
    rows = []
    for path in sorted(results_dir.rglob("*.json")):
        try:
            rows.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError) as exc:
            print(f"skipping unreadable {path}: {exc}")
    return rows


def platform_label(row: dict) -> str:
    env = row.get("env", {})
    os_name = env.get("runner_os") or env.get("platform", "?").split("-")[0]
    arch = env.get("runner_arch") or env.get("machine", "?")
    return f"{os_name}/{arch}"


def cell(value: bool | None) -> str:
    if value is True:
        return "pass"
    if value is False:
        return "fail"
    return "-"


def stage_cell(row: dict, stage: str) -> str:
    return cell(row.get("stages", {}).get(stage, {}).get("ok"))


def short_error(row: dict) -> str:
    err = row.get("error")
    if not err:
        return ""
    err = " ".join(str(err).split())
    # keep the table readable; the json keeps the full traceback
    return err if len(err) <= 90 else err[:87] + "..."


def escape(text: str) -> str:
    return str(text).replace("|", "\\|")


def render(rows: list[dict]) -> str:
    out: list[str] = []
    ok_rows = [r for r in rows if r.get("ok")]
    models = sorted({r["model"] for r in rows})

    out.append("# ultralytics → ncnn (pyncnn + Vulkan) 兼容性报告")
    out.append("")
    out.append("本报告由 GitHub Actions 自动生成：每个模型在真实的 pyncnn + Vulkan 环境下导出为 ncnn、")
    out.append("加载、推理，并与 CPU 结果逐元素比对。所有模型均取 **n** 尺寸。")
    out.append("")
    out.append(f"- 受测模型数：**{len(models)}**")
    out.append(f"- 测试组合数（模型 x 平台）：**{len(rows)}**")
    out.append(f"- 全部通过的组合数：**{len(ok_rows)}**")
    out.append("")

    # ---- versions / environment --------------------------------------
    env_keys = ("runner_os", "runner_arch", "python", "vulkan_icd", "ncnn_vulkan_driver")
    seen_env: dict[str, dict] = {}
    for row in rows:
        label = platform_label(row)
        if label not in seen_env:
            seen_env[label] = {"env": row.get("env", {}), "versions": row.get("versions", {}),
                               "gpus": row.get("gpus", [])}

    out.append("## 测试环境")
    out.append("")
    out.append("| 平台 | Python | ultralytics | ncnn | pnnx | torch | Vulkan 设备 |")
    out.append("| --- | --- | --- | --- | --- | --- | --- |")
    for label, info in sorted(seen_env.items()):
        v = info["versions"]
        gpus = info["gpus"] or ["<none>"]
        gpu_text = escape("<br>".join(gpus))
        out.append(
            f"| {escape(label)} | {escape(info['env'].get('python', '?'))} | "
            f"{escape(v.get('ultralytics', '?'))} | {escape(v.get('ncnn', '?'))} | "
            f"{escape(v.get('pnnx', '?'))} | {escape(v.get('torch', '?'))} | {gpu_text} |"
        )
    out.append("")

    icd = sorted({r.get("env", {}).get("vulkan_icd", "") for r in rows} - {""})
    if icd:
        out.append("Vulkan ICD：" + "、".join(f"`{escape(i)}`" for i in icd))
        out.append("")

    # ---- per-model matrix --------------------------------------------
    out.append("## 模型兼容性矩阵")
    out.append("")
    out.append("`export` = ultralytics 导出 ncnn，`load` = pyncnn 加载 param/bin，")
    out.append("`vulkan` = 网络真正绑定到 Vulkan 设备（未回退 CPU），`inference` = 推理且与 CPU 结果一致。")
    out.append("")
    out.append("| 模型 | 平台 | 导出 | 加载 | Vulkan | 推理 | param (MB) | Vulkan ms | CPU ms | 失败阶段 | 备注 |")
    out.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for row in sorted(rows, key=lambda r: (r["model"], platform_label(r))):
        timing = row.get("timing") or {}
        out.append(
            f"| `{escape(row['model'])}` | {escape(platform_label(row))} "
            f"| {stage_cell(row, 'export')} | {stage_cell(row, 'load')} "
            f"| {stage_cell(row, 'vulkan')} | {stage_cell(row, 'inference')} "
            f"| {row.get('params_mb') if row.get('params_mb') is not None else '-'} "
            f"| {timing.get('vulkan_ms_median', '-')} | {timing.get('cpu_ms_median', '-')} "
            f"| {escape(row.get('failed_stage') or '-')} | {escape(short_error(row))} |"
        )
    out.append("")

    # ---- per-model verdict -------------------------------------------
    out.append("## 按模型汇总")
    out.append("")
    out.append("| 模型 | 通过平台 | 失败平台 | 结论 |")
    out.append("| --- | --- | --- | --- |")
    by_model: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_model[row["model"]].append(row)
    for model in sorted(by_model):
        group = by_model[model]
        passed = [platform_label(r) for r in group if r.get("ok")]
        failed = [f"{platform_label(r)}({r.get('failed_stage') or '?'})" for r in group if not r.get("ok")]
        verdict = "**全部通过**" if not failed else ("部分通过" if passed else "**失败**")
        out.append(
            f"| `{escape(model)}` | {escape(', '.join(passed) or '-')} "
            f"| {escape(', '.join(failed) or '-')} | {verdict} |"
        )
    out.append("")

    # ---- failure detail ----------------------------------------------
    failures = [r for r in rows if not r.get("ok")]
    if failures:
        out.append("## 失败详情")
        out.append("")
        for row in sorted(failures, key=lambda r: (r["model"], platform_label(r))):
            out.append(f"### `{escape(row['model'])}` @ {escape(platform_label(row))}")
            out.append("")
            out.append(f"- 失败阶段：**{row.get('failed_stage') or '?'}**")
            out.append(f"- 错误：`{escape(row.get('error') or '?')}`")
            stage_info = row.get("stages", {}).get(row.get("failed_stage") or "", {})
            if stage_info.get("seconds") is not None:
                out.append(f"- 耗时：{stage_info['seconds']}s")
            log = row.get("log") or []
            if log:
                out.append("")
                out.append("<details><summary>日志</summary>")
                out.append("")
                out.append("```")
                out.extend(log[-25:])
                out.append("```")
                out.append("")
                out.append("</details>")
            out.append("")

    out.append("---")
    out.append("")
    out.append("生成方式：`.github/workflows/model-compat.yml`。")
    return "\n".join(out) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", default="results", help="directory of per-model json results")
    ap.add_argument("--out", default="report.md", help="markdown file to write")
    args = ap.parse_args()

    rows = load_results(Path(args.results))
    if not rows:
        print("no results found")
        return 1
    Path(args.out).write_text(render(rows), encoding="utf-8")
    print(f"wrote {args.out} from {len(rows)} result(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
