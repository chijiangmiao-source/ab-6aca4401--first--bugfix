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
    d = {"op_id": op_id, "kind": "INSERT", "new_id": new_id,
         "anchor": anchor or "FIRST", "text": text or f"文本{new_id}"}
    return d


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
