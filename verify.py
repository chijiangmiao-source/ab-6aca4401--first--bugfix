"""Compose verify 服务的一次性验收脚本。

依次执行：
  1. 领域测试（unittest，覆盖同锚点并发插入、墓碑后插入、冲突拒绝等）
  2. 构建检查（py_compile 全量语法编译）
  3. HTTP 冒烟：健康响应、页面可达、三组关键场景的 API 裁定
     （同锚点并发插入 / 墓碑后插入 / 冲突拒绝）

全部通过以退出码 0 报告验收成功；任一失败退出码非 0。
"""

from __future__ import annotations

import json
import os
import py_compile
import sys
import unittest
import urllib.error
import urllib.request
from pathlib import Path

WEB_URL = os.environ.get("WEB_URL", "http://web:8080")
ROOT = Path(__file__).resolve().parent

GREEN, RED, RESET = "\033[32m", "\033[31m", "\033[0m"


def hr(title: str) -> None:
    print(f"\n{'=' * 22} {title} {'=' * 22}", flush=True)


def http(method: str, path: str, body=None):
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(WEB_URL + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            raw = resp.read().decode("utf-8")
            ctype = resp.headers.get("Content-Type", "")
            parsed = json.loads(raw) if "application/json" in ctype and raw else None
            return resp.status, parsed if parsed is not None else raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8")
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, raw


def main() -> int:
    failures: list[str] = []

    # ---- 1. 领域测试 ------------------------------------------------------
    hr("1/3 领域测试")
    loader = unittest.TestLoader()
    suite = loader.discover(str(ROOT / "tests"))
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    if not result.wasSuccessful():
        failures.append("领域测试未全部通过")

    # ---- 2. 构建检查（语法编译） ------------------------------------------
    hr("2/3 构建检查 py_compile")
    compile_failed = []
    for py in list((ROOT / "app").rglob("*.py")) + list((ROOT / "tests").rglob("*.py")):
        try:
            py_compile.compile(str(py), doraise=True)
        except py_compile.PyCompileError as exc:
            compile_failed.append(f"{py}: {exc}")
    if compile_failed:
        failures.extend(compile_failed)
        print("\n".join(compile_failed))
    else:
        print(f"已编译 {len(list((ROOT / 'app').rglob('*.py'))) + len(list((ROOT / 'tests').rglob('*.py')))} 个 Python 文件，全部通过")

    # ---- 3. HTTP 冒烟 ------------------------------------------------------
    hr(f"3/3 HTTP 冒烟（目标 {WEB_URL}）")

    def check(label: str, ok: bool, detail: str = "") -> None:
        mark = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
        print(f"  [{mark}] {label}" + (f" —— {detail}" if detail else ""), flush=True)
        if not ok:
            failures.append(label)

    status, body = http("GET", "/healthz")
    check("健康响应 GET /healthz=200 且 status=ok",
          status == 200 and isinstance(body, dict) and body.get("status") == "ok",
          f"status={status} body={body}")

    req = urllib.request.Request(WEB_URL + "/")
    with urllib.request.urlopen(req, timeout=5) as resp:
        page = resp.read().decode("utf-8")
    check("页面 GET / 可达且包含复核工作台标记",
          resp.status == 200 and "双支离线修订" in page and "/api/merge" in page,
          f"status={resp.status}, bytes={len(page)}")

    for asset in ("/app.js", "/styles.css"):
        s, _ = http("GET", asset)
        check(f"静态资源 {asset} 可达", s == 200, f"status={s}")

    # 场景 A：同锚点并发插入（裁定 left 贴近锚点）+ 墓碑后插入
    concurrent = {
        "baseline": [
            {"id": "A", "text": "绕机检查"},
            {"id": "B", "text": "襟翼起飞位"},
            {"id": "C", "text": "核对简令"}
        ],
        "branches": [
            {"name": "left", "ops": [
                {"op_id": "L1", "kind": "INSERT", "new_id": "x-LTANK", "anchor": "B", "text": "左翼油箱复查"},
                {"op_id": "L2", "kind": "DELETE", "target": "B"},
                {"op_id": "L3", "kind": "INSERT", "new_id": "y-TOMB", "anchor": "B", "text": "墓碑后插入：留存归档"}
            ]},
            {"name": "right", "ops": [
                {"op_id": "R1", "kind": "INSERT", "new_id": "p-RTANK", "anchor": "B", "text": "右翼油箱复查"},
                {"op_id": "R2", "kind": "INSERT", "new_id": "q-AIL", "anchor": "B", "text": "副翼行程复查"}
            ]}
        ]
    }
    status, res = http("POST", "/api/merge", concurrent)
    if status == 200 and res.get("ok"):
        ids = [r["id"] for r in res["merged"]]
        om = {(o["branch"], o["op_id"]): o for o in res["outcomes"]}
        expected = ["A", "B", "y-TOMB", "x-LTANK", "q-AIL", "p-RTANK", "C"]
        check("场景A 同锚点并发插入按 (分支名,操作标识) 稳定裁定",
              ids == expected, f"合并序={ids} 期望={expected}")
        check("场景A 墓碑后插入紧随已删锚点且不漂移",
              ids.index("y-TOMB") == ids.index("B") + 1)
        check("场景A 位移操作裁定为 transformed 并给出依据",
              om[("right", "R1")]["result"] == "transformed"
              and "(分支名, 操作标识)" in om[("right", "R1")]["basis"])
        check("场景A 每条原操作都有保留/转换/合并结论",
              len(res["outcomes"]) == 5
              and all(o["result"] in ("kept", "transformed", "merged") for o in res["outcomes"]))
    else:
        check("场景A 同锚点并发插入合并通过", False, f"status={status} body={body}")

    # 场景 B：幂等合并（相同替换 + 重复删除，跨支）
    idem = {
        "baseline": [{"id": "A", "text": "记录构型"}, {"id": "B", "text": "设定推力"}],
        "branches": [
            {"name": "left", "ops": [
                {"op_id": "L1", "kind": "REPLACE", "target": "A", "text": "记录构型（双签）"},
                {"op_id": "L2", "kind": "DELETE", "target": "B"},
                {"op_id": "L3", "kind": "DELETE", "target": "B"}
            ]},
            {"name": "right", "ops": [
                {"op_id": "R1", "kind": "REPLACE", "target": "A", "text": "记录构型（双签）"},
                {"op_id": "R2", "kind": "DELETE", "target": "B"}
            ]}
        ]
    }
    status, res = http("POST", "/api/merge", idem)
    if status == 200 and res.get("ok"):
        om = {(o["branch"], o["op_id"]): o["result"] for o in res["outcomes"]}
        check("场景B 跨支相同替换合并、重复删除合并",
              om[("right", "R1")] == "merged"
              and om[("left", "L3")] == "merged"
              and om[("right", "R2")] == "merged",
              f"results={om}")
        check("场景B 首操作保留且合并表可执行",
              om[("left", "L1")] == "kept" and om[("left", "L2")] == "kept")
    else:
        check("场景B 幂等合并通过", False, f"status={status} body={body}")

    # 场景 C：冲突拒绝（对同一步骤的不同替换）
    diverge = {
        "baseline": [{"id": "A", "text": "许可确认"}],
        "branches": [
            {"name": "left", "ops": [{"op_id": "L1", "kind": "REPLACE", "target": "A", "text": "塔台频率"}]},
            {"name": "right", "ops": [{"op_id": "R1", "kind": "REPLACE", "target": "A", "text": "地面频率"}]}
        ]
    }
    status, res = http("POST", "/api/merge", diverge)
    if status == 200 and isinstance(res, dict) and res.get("ok") is False:
        c = res["conflict"]
        check("场景C 不同替换被拒绝并报首个冲突双方操作",
              c["code"] == "DIVERGENT_REPLACE"
              and c["op_a"]["op_id"] == "L1" and c["op_b"]["op_id"] == "R1"
              and c["basis"])
    else:
        check("场景C 冲突拒绝", False, f"status={status} body={body}")

    # 场景 C2：删除与替换跨支交叉
    cross = {
        "baseline": [{"id": "A", "text": "停顿检查"}],
        "branches": [
            {"name": "left", "ops": [{"op_id": "L1", "kind": "DELETE", "target": "A"}]},
            {"name": "right", "ops": [{"op_id": "R1", "kind": "REPLACE", "target": "A", "text": "停顿检查并开灯"}]}
        ]
    }
    status, res = http("POST", "/api/merge", cross)
    check("场景C2 删除/替换交叉被拒绝",
          status == 200 and res.get("ok") is False
          and res["conflict"]["code"] == "DELETE_REPLACE_CONFLICT",
          f"body={res}")

    # 场景 C3：悬空引用
    dangling = {
        "baseline": [{"id": "A", "text": "x"}],
        "branches": [
            {"name": "left", "ops": [{"op_id": "L1", "kind": "INSERT", "new_id": "x1", "anchor": "GHOST", "text": "悬锚"}]},
            {"name": "right", "ops": []}
        ]
    }
    status, res = http("POST", "/api/merge", dangling)
    check("场景C3 悬空锚点引用被拒绝",
          status == 200 and res.get("ok") is False
          and res["conflict"]["code"] == "DANGLING_ANCHOR")

    # ---- 汇总 --------------------------------------------------------------
    hr("验收汇总")
    if failures:
        print(f"{RED}验收失败：{len(failures)} 项未通过{RESET}")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"{GREEN}验收通过：领域测试、构建检查、页面/健康/API 冒烟全部成功{RESET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
