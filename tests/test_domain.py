"""领域测试：墓碑保留序列模型、双支 OT 裁定、合并与拒绝。"""

import unittest

from app import domain


def mk(baseline, left=(), right=(), lname="left", rname="right"):
    return {
        "baseline": [{"id": i, "text": f"步骤{i}"} if isinstance(i, str) else i
                     for i in baseline],
        "branches": [
            {"name": lname, "ops": list(left)},
            {"name": rname, "ops": list(right)},
        ],
    }


def ins(op_id, new_id, anchor, text=None):
    # anchor 为 None 时省略（默认首位）；字符串走裸锚点（"FIRST" 即首位）；
    # dict（如 {"id": "FIRST"}）原样透传，用于锚定名为 FIRST 的普通步骤。
    if anchor is None:
        d = {"op_id": op_id, "kind": "INSERT", "new_id": new_id,
             "text": text or f"文本{new_id}"}
    else:
        d = {"op_id": op_id, "kind": "INSERT", "new_id": new_id,
             "anchor": anchor, "text": text or f"文本{new_id}"}
    return d


FIRST_STEP = {"id": "FIRST"}  # 锚定标识恰为 FIRST 的普通步骤（区别于首位锚点）


def dele(op_id, target):
    return {"op_id": op_id, "kind": "DELETE", "target": target}


def rep(op_id, target, text):
    return {"op_id": op_id, "kind": "REPLACE", "target": target, "text": text}


def live_ids(result):
    return [r["id"] for r in result["merged"] if r["step_no"] is not None]


def all_ids(result):
    return [r["id"] for r in result["merged"]]


def outcome_map(result):
    return {(o["branch"], o["op_id"]): o for o in result["outcomes"]}


class TestBasics(unittest.TestCase):
    def test_no_ops_keeps_baseline(self):
        r = domain.merge(mk(["A", "B", "C"]))
        self.assertTrue(r["ok"])
        self.assertEqual(live_ids(r), ["A", "B", "C"])
        self.assertEqual([row["step_no"] for row in r["merged"]], [1, 2, 3])

    def test_single_branch_full_lifecycle(self):
        r = domain.merge(mk(
            ["A", "B", "C"],
            left=[ins("L1", "x", "A"), dele("L2", "B"),
                  ins("L3", "y", "B"), rep("L4", "C", "新C")]))
        self.assertTrue(r["ok"], r)
        # A, x, B(墓碑), y(紧随墓碑), C(已替换文本)
        self.assertEqual(all_ids(r), ["A", "x", "B", "y", "C"])
        self.assertEqual(live_ids(r), ["A", "x", "y", "C"])
        b = next(row for row in r["merged"] if row["id"] == "B")
        self.assertEqual(b["status"], "tombstone")
        self.assertEqual(b["deleted_by"], "L2")
        c = next(row for row in r["merged"] if row["id"] == "C")
        self.assertEqual(c["text"], "新C")

    def test_insert_can_anchor_first(self):
        r = domain.merge(mk(["A"], left=[ins("L1", "x", None)]))
        self.assertTrue(r["ok"])
        self.assertEqual(all_ids(r), ["x", "A"])

    def test_insert_can_anchor_prior_insert(self):
        r = domain.merge(mk(["A"], left=[ins("L1", "x", "A"), ins("L2", "y", "x")]))
        self.assertTrue(r["ok"], r)
        self.assertEqual(all_ids(r), ["A", "x", "y"])

    def test_replace_does_not_drift_position(self):
        r = domain.merge(mk(["A", "B", "C"], left=[rep("L1", "B", "新B")]))
        b = next(row for row in r["merged"] if row["id"] == "B")
        self.assertEqual(b["position"], 1)
        self.assertEqual(b["step_no"], 2)


class TestConcurrentInsertArbitration(unittest.TestCase):
    def test_same_anchor_concurrent_insert_branch_order(self):
        payload = mk(["A", "B", "C"],
                     left=[ins("Lx", "x", "B")],
                     right=[ins("Rp", "p", "B"), ins("Rq", "q", "B")])
        r = domain.merge(payload)
        self.assertTrue(r["ok"], r)
        # 分支名 left<right：left 整体贴近 B；right 内保持重放序（q 后插更贴近）
        self.assertEqual(all_ids(r), ["A", "B", "x", "q", "p", "C"])
        om = outcome_map(r)
        self.assertEqual(om[("left", "Lx")]["result"], domain.R_KEPT)  # 贴近锚点一方
        self.assertEqual(om[("right", "Rp")]["result"], domain.R_TRANSFORMED)
        self.assertEqual(om[("right", "Rq")]["result"], domain.R_TRANSFORMED)
        # 仲裁依据必须包含稳定裁定说明
        self.assertIn("(分支名, 操作标识)", om[("right", "Rp")]["basis"])

    def test_arbitration_uses_names_not_positions(self):
        payload = mk(["A", "B"],
                     left=[ins("Z1", "z", "B")],
                     right=[ins("A1", "a", "B")],
                     lname="zulu", rname="alpha")
        r = domain.merge(payload)
        self.assertTrue(r["ok"], r)
        # alpha 字典序小，贴近 B
        self.assertEqual(all_ids(r), ["A", "B", "a", "z"])

    def test_concurrent_insert_at_FIRST(self):
        r = domain.merge(mk(["A"],
                            left=[ins("L1", "l", None)],
                            right=[ins("R1", "r0", None), ins("R2", "r1", None)]))
        self.assertTrue(r["ok"], r)
        self.assertEqual(all_ids(r), ["l", "r1", "r0", "A"])

    def test_position_shift_from_unrelated_concurrent_insert_is_transformed(self):
        # right 在 A 后插入；left 在 C 后插入 —— 锚点不同，但 left 的合并位整体后移
        r = domain.merge(mk(["A", "B", "C"],
                            left=[ins("L1", "l", "C")],
                            right=[ins("R1", "r", "A")]))
        self.assertTrue(r["ok"], r)
        self.assertEqual(all_ids(r), ["A", "r", "B", "C", "l"])
        om = outcome_map(r)
        self.assertEqual(om[("left", "L1")]["result"], domain.R_TRANSFORMED)
        self.assertEqual(om[("left", "L1")]["position_before"], 3)
        self.assertEqual(om[("left", "L1")]["position_after"], 4)


class TestFirstIdDisambiguation(unittest.TestCase):
    """首位锚点 FIRST 与名为 FIRST 的普通步骤标识必须可区分、不混淆。"""

    def test_reported_two_step_path_insert_first_step_then_anchor_it(self):
        # 验收路径：仅含 A 的基线；left 先在 A 后插入标识为 FIRST 的步骤，
        # 再插入 X 并锚定本支先前新增的 FIRST（{"id": "FIRST"}）；right 无操作。
        r = domain.merge(mk(
            ["A"],
            left=[ins("L1", "FIRST", "A"), ins("L2", "X", FIRST_STEP)]))
        self.assertTrue(r["ok"], r)
        self.assertEqual(all_ids(r), ["A", "FIRST", "X"])
        self.assertEqual(live_ids(r), ["A", "FIRST", "X"])
        om = outcome_map(r)
        self.assertEqual(om[("left", "L1")]["result"], domain.R_KEPT)
        self.assertEqual(om[("left", "L2")]["result"], domain.R_KEPT)
        # 两条操作的锚点必须按各自含义原样呈现
        self.assertEqual(om[("left", "L1")]["anchor"], "A")
        self.assertEqual(om[("left", "L1")]["anchor_kind"], "step")
        self.assertEqual(om[("left", "L2")]["anchor"], {"id": "FIRST"})
        self.assertEqual(om[("left", "L2")]["anchor_kind"], "step")
        # 定位依据须明确这是普通步骤而非序列首位
        self.assertIn("普通步骤", om[("left", "L2")]["basis"])
        self.assertIn("非序列首位锚点", om[("left", "L2")]["basis"])

    def test_head_anchor_and_named_first_step_coexist_and_distinguish(self):
        # 基线含标识恰为 FIRST 的普通步骤：left 用裸 "FIRST" 锚点插到序列首位，
        # right 用 {"id": "FIRST"} 锚到该普通步骤。两类定位互不混淆。
        r = domain.merge(mk(
            ["FIRST", "A"],
            left=[ins("L1", "H", "FIRST")],
            right=[ins("R1", "Y", FIRST_STEP)]))
        self.assertTrue(r["ok"], r)
        # H 在序列最前；Y 紧随名为 FIRST 的步骤之后
        self.assertEqual(all_ids(r), ["H", "FIRST", "Y", "A"])
        om = outcome_map(r)
        self.assertEqual(om[("left", "L1")]["anchor"], "FIRST")
        self.assertEqual(om[("left", "L1")]["anchor_kind"], "head")
        self.assertEqual(om[("right", "R1")]["anchor"], {"id": "FIRST"})
        self.assertEqual(om[("right", "R1")]["anchor_kind"], "step")
        self.assertIn("首位锚点", om[("left", "L1")]["anchor_label"])
        self.assertIn("普通步骤", om[("right", "R1")]["anchor_label"])

    def test_inserted_first_step_then_head_anchor_remains_head(self):
        # 同支先插入名为 FIRST 的步骤，再用裸 "FIRST" 锚点 —— 仍指序列首位，
        # 不会落到刚插入的 FIRST 步骤上。
        r = domain.merge(mk(
            ["A"],
            left=[ins("L1", "FIRST", "A"), ins("L2", "H", "FIRST")]))
        self.assertTrue(r["ok"], r)
        self.assertEqual(all_ids(r), ["H", "A", "FIRST"])
        om = outcome_map(r)
        self.assertEqual(om[("left", "L2")]["anchor"], "FIRST")
        self.assertEqual(om[("left", "L2")]["anchor_kind"], "head")

    def test_concurrent_arbitration_applies_to_named_first_step_anchor(self):
        # 同锚点（名为 FIRST 的普通步骤）并发插入，(分支名, 操作标识) 裁定照常
        r = domain.merge(mk(
            ["FIRST", "A"],
            left=[ins("L1", "X", FIRST_STEP)],
            right=[ins("Rp", "p", FIRST_STEP), ins("Rq", "q", FIRST_STEP)]))
        self.assertTrue(r["ok"], r)
        # left 贴近 FIRST 步骤；right 内保持重放序（q 更贴近）
        self.assertEqual(all_ids(r), ["FIRST", "X", "q", "p", "A"])
        om = outcome_map(r)
        self.assertEqual(om[("left", "L1")]["result"], domain.R_KEPT)
        self.assertEqual(om[("right", "Rp")]["result"], domain.R_TRANSFORMED)

    def test_head_concurrent_inserts_separate_from_named_first_step(self):
        # 锚定首位 与 锚定 FIRST 步骤 是两个不同锚点，不构成"同锚点并发"
        r = domain.merge(mk(
            ["FIRST", "A"],
            left=[ins("L1", "H", "FIRST")],
            right=[ins("R1", "Y", FIRST_STEP)]))
        self.assertTrue(r["ok"], r)
        om = outcome_map(r)
        # 双方都不应看到彼此作为"同锚点并发"对手
        self.assertNotIn("并发锚定同一锚点", om[("left", "L1")]["basis"])
        self.assertNotIn("并发锚定同一锚点", om[("right", "R1")]["basis"])

    def test_op_as_json_distinguishes_two_anchor_kinds(self):
        # 操作序列化：首位为裸字符串，名为 FIRST 的步骤为对象形式
        head_op = domain.Op(branch="b", seq=1, op_id="H", kind=domain.KIND_INSERT,
                            anchor=None, new_id="H")
        step_op = domain.Op(branch="b", seq=2, op_id="S", kind=domain.KIND_INSERT,
                            anchor="FIRST", new_id="S")
        self.assertEqual(head_op.as_json()["anchor"], "FIRST")
        self.assertEqual(step_op.as_json()["anchor"], {"id": "FIRST"})


class TestAnchorStructuralValidation(unittest.TestCase):
    def _reject(self, payload):
        with self.assertRaises(domain.OTReject):
            domain.merge(payload)

    def test_bare_first_string_is_head_not_step(self):
        # 裸 "FIRST" 永远是首位锚点；基线无 FIRST 步骤时也不悬空
        r = domain.merge(mk(["A"], left=[ins("L1", "z", "FIRST")]))
        self.assertTrue(r["ok"])
        self.assertEqual(all_ids(r), ["z", "A"])

    def test_object_anchor_other_than_first_step_rejected(self):
        self._reject(mk(["A"], left=[ins("L1", "x", {"id": "A"})]))
        self._reject(mk(["A"], left=[ins("L1", "x", {"id": "B"})]))

    def test_malformed_anchor_rejected(self):
        self._reject(mk(["A"], left=[ins("L1", "x", {"id": "FIRST", "extra": 1})]))
        self._reject(mk(["A"], left=[ins("L1", "x", 123)]))
        self._reject(mk(["A"], left=[ins("L1", "x", ["FIRST"])]))

    def test_dangling_named_first_step_anchor_when_no_such_step(self):
        # 基线没有 FIRST 步骤、本支也未先前插入时，{"id": "FIRST"} 是悬空锚点
        r = domain.merge(mk(["A"], left=[ins("L1", "x", FIRST_STEP)]))
        self.assertFalse(r["ok"])
        self.assertEqual(r["conflict"]["code"], "DANGLING_ANCHOR")
        self.assertEqual(r["conflict"]["ref"], "FIRST")

    def test_dangling_named_first_step_on_other_branch_insert(self):
        # FIRST 步骤由 left 插入时，right 不能锚定它（对侧标识不可引用）
        r = domain.merge(mk(["A"],
                            left=[ins("L1", "FIRST", "A")],
                            right=[ins("R1", "Y", FIRST_STEP)]))
        self.assertFalse(r["ok"])
        self.assertEqual(r["conflict"]["code"], "DANGLING_ANCHOR")


class TestTombstoneAnchor(unittest.TestCase):
    def test_insert_after_tombstone_on_same_branch(self):
        r = domain.merge(mk(["A", "B", "C"],
                            left=[dele("L1", "B"), ins("L2", "y", "B")]))
        self.assertTrue(r["ok"], r)
        self.assertEqual(all_ids(r), ["A", "B", "y", "C"])
        om = outcome_map(r)
        self.assertEqual(om[("left", "L2")]["result"], domain.R_KEPT)
        self.assertIn("墓碑", om[("left", "L2")]["basis"])

    def test_other_branch_anchor_deleted_tombstone_holds_position(self):
        r = domain.merge(mk(["A", "B", "C"],
                            left=[dele("L1", "B"), ins("L2", "y", "B")],
                            right=[ins("R1", "r", "B")]))
        self.assertTrue(r["ok"], r)
        # B 墓碑后：left 块（y）贴近，然后 right（r）
        self.assertEqual(all_ids(r), ["A", "B", "y", "r", "C"])

    def test_delete_outcome_kept_and_tombstone_count(self):
        r = domain.merge(mk(["A", "B"], left=[dele("L1", "A")]))
        om = outcome_map(r)
        self.assertEqual(om[("left", "L1")]["result"], domain.R_KEPT)
        self.assertEqual(r["stats"]["tombstones"], 1)
        self.assertEqual(r["stats"]["live"], 1)


class TestIdempotentMerge(unittest.TestCase):
    def test_duplicate_delete_within_branch_merges(self):
        r = domain.merge(mk(["A"], left=[dele("L1", "A"), dele("L2", "A")]))
        self.assertTrue(r["ok"], r)
        om = outcome_map(r)
        self.assertEqual(om[("left", "L1")]["result"], domain.R_KEPT)
        self.assertEqual(om[("left", "L2")]["result"], domain.R_MERGED)
        self.assertEqual(om[("left", "L2")]["merged_into"]["op_id"], "L1")

    def test_identical_replace_within_branch_merges(self):
        r = domain.merge(mk(["A"], left=[rep("L1", "A", "同文"), rep("L2", "A", "同文")]))
        self.assertTrue(r["ok"], r)
        om = outcome_map(r)
        self.assertEqual(om[("left", "L2")]["result"], domain.R_MERGED)

    def test_identical_replace_cross_branch_merges(self):
        r = domain.merge(mk(["A"], left=[rep("L1", "A", "同文")],
                            right=[rep("R1", "A", "同文")]))
        self.assertTrue(r["ok"], r)
        self.assertEqual(next(row for row in r["merged"] if row["id"] == "A")["text"], "同文")
        om = outcome_map(r)
        # keeper 取 (分支名, op_id) 最小者：L1
        self.assertEqual(om[("right", "R1")]["result"], domain.R_MERGED)
        self.assertEqual(om[("right", "R1")]["merged_into"]["op_id"], "L1")
        self.assertEqual(om[("left", "L1")]["result"], domain.R_KEPT)

    def test_duplicate_delete_cross_branch_merges(self):
        r = domain.merge(mk(["A"], left=[dele("L9", "A")], right=[dele("R1", "A")]))
        self.assertTrue(r["ok"], r)
        om = outcome_map(r)
        self.assertEqual(om[("right", "R1")]["result"], domain.R_MERGED)
        self.assertEqual(om[("right", "R1")]["merged_into"]["op_id"], "L9")


class TestRejections(unittest.TestCase):
    def _conflict(self, payload, code):
        r = domain.merge(payload)
        self.assertFalse(r["ok"])
        c = r["conflict"]
        self.assertEqual(c["code"], code)
        self.assertTrue(c["basis"])
        self.assertIsNotNone(c["op_a"])
        return c

    def test_divergent_replace_within_branch(self):
        c = self._conflict(
            mk(["A"], left=[rep("L1", "A", "文本1"), rep("L2", "A", "文本2")]),
            "DIVERGENT_REPLACE")
        self.assertEqual(c["op_a"]["op_id"], "L1")
        self.assertEqual(c["op_b"]["op_id"], "L2")

    def test_divergent_replace_cross_branch(self):
        c = self._conflict(
            mk(["A"], left=[rep("L1", "A", "塔台频率")], right=[rep("R1", "A", "地面频率")]),
            "DIVERGENT_REPLACE")
        self.assertEqual(c["ref"], "A")

    def test_replace_then_delete_within_branch(self):
        c = self._conflict(
            mk(["A"], left=[rep("L1", "A", "新"), dele("L2", "A")]),
            "DELETE_REPLACE_CONFLICT")
        self.assertEqual(c["op_b"]["op_id"], "L2")

    def test_delete_then_replace_within_branch(self):
        self._conflict(
            mk(["A"], left=[dele("L1", "A"), rep("L2", "A", "新")]),
            "DELETE_REPLACE_CONFLICT")

    def test_delete_replace_cross_branch(self):
        c = self._conflict(
            mk(["A"], left=[dele("L1", "A")], right=[rep("R1", "A", "新")]),
            "DELETE_REPLACE_CONFLICT")
        self.assertEqual(c["op_a"]["op_id"], "L1")
        self.assertEqual(c["op_b"]["op_id"], "R1")

    def test_duplicate_new_id_within_branch(self):
        c = self._conflict(
            mk(["A"], left=[ins("L1", "dup", "A"), ins("L2", "dup", "A")]),
            "DUPLICATE_NEW_ID")
        self.assertEqual(c["ref"], "dup")

    def test_duplicate_new_id_cross_branch(self):
        self._conflict(
            mk(["A"], left=[ins("L1", "dup", "A")], right=[ins("R1", "dup", "A")]),
            "DUPLICATE_NEW_ID")

    def test_new_id_collides_baseline(self):
        self._conflict(mk(["A"], left=[ins("L1", "A", "FIRST")]), "DUPLICATE_NEW_ID")

    def test_dangling_anchor(self):
        c = self._conflict(mk(["A"], left=[ins("L1", "x", "GHOST")]), "DANGLING_ANCHOR")
        self.assertIsNone(c["op_b"])  # 单方操作即非法
        self.assertEqual(c["ref"], "GHOST")

    def test_dangling_target_to_other_branch_insert(self):
        # right 不能引用只有 left 插入的标识
        self._conflict(
            mk(["A"], left=[ins("L1", "x", "A")], right=[dele("R1", "x")]),
            "DANGLING_TARGET")

    def test_first_conflict_is_earliest_by_order(self):
        # 两处异替换：(left L1, A) 与 (left L3, B)；首个必须指向 A 那对
        payload = mk(
            ["A", "B"],
            left=[rep("L1", "A", "a1"), rep("L3", "B", "b1")],
            right=[rep("R9", "A", "a2"), rep("R8", "B", "b2")])
        r = domain.merge(payload)
        self.assertFalse(r["ok"])
        self.assertEqual(r["conflict"]["ref"], "A")
        self.assertEqual(r["conflict"]["issue_count"], 2)


class TestStructuralValidation(unittest.TestCase):
    def test_reject_81_ops(self):
        ops = [dele(f"L{i:02d}", "A") for i in range(81)]  # 大量重复删除也只 81 条输入
        payload = mk(["A"], left=ops[:1])
        payload["branches"][0]["ops"] = ops
        with self.assertRaises(domain.OTReject):
            domain.merge(payload)

    def test_accept_80_ops(self):
        # 80 条：1 条删除 + 79 条对它的重复删除（合并）
        ops = [dele("L0", "A")] + [dele(f"L{i}", "A") for i in range(1, 80)]
        r = domain.merge(mk(["A", "B"], left=ops))
        self.assertTrue(r["ok"], r)
        self.assertEqual(len(r["outcomes"]), 80)

    def test_non_ascii_id_rejected(self):
        with self.assertRaises(domain.OTReject):
            domain.merge(mk(["A步骤"], left=[]))

    def test_whitespace_id_rejected(self):
        payload = {"baseline": [{"id": "A B", "text": "x"}],
                   "branches": [{"name": "left", "ops": []},
                                {"name": "right", "ops": []}]}
        with self.assertRaises(domain.OTReject):
            domain.merge(payload)

    def test_duplicate_op_id_rejected(self):
        with self.assertRaises(domain.OTReject):
            domain.merge(mk(["A"], left=[dele("DUP", "A"), dele("DUP", "A")]))
        # 注意：这是结构拒绝（操作标识必须唯一），区别于幂等合并的业务语义

    def test_must_have_two_branches(self):
        with self.assertRaises(domain.OTReject):
            domain.merge({"baseline": [{"id": "A", "text": "x"}], "branches": []})


class TestOutcomeCompleteness(unittest.TestCase):
    def test_every_original_op_classified(self):
        payload = mk(
            ["A", "B", "C"],
            left=[ins("L1", "x", "B"), dele("L2", "B"),
                  ins("L3", "y", "B"), dele("L4", "B"),
                  rep("L5", "C", "新C")],
            right=[ins("R1", "p", "B"), ins("R2", "q", "B"),
                   rep("R3", "C", "新C"), dele("R4", "B")])
        r = domain.merge(payload)
        self.assertTrue(r["ok"], r)
        self.assertEqual(len(r["outcomes"]), 9)
        results = {o["result"] for o in r["outcomes"]}
        self.assertEqual(results, {domain.R_KEPT, domain.R_TRANSFORMED, domain.R_MERGED})
        # 合并表中每个存活步骤序号连续且从 1 开始
        live = [row["step_no"] for row in r["merged"] if row["step_no"] is not None]
        self.assertEqual(live, list(range(1, len(live) + 1)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
