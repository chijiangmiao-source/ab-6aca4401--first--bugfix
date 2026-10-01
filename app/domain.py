"""飞行试验检查单 —— 双支离线修订的操作变换（OT）领域核心。

序列模型约定：
- 每个步骤（基线步骤或某支插入的步骤）在分支状态中只创建一次，占用一个
  永不回收的序列位置；删除只置墓碑标记（``deleted=True``），节点本身保留，
  因此以"已删步骤"为锚点的后续插入仍有确定位置（紧随墓碑之后），不会发生
  位置漂移。
- 插入位置完全由锚点标识决定（FIRST 或任一尚存 / 已删 / 本支先前插入的
  步骤），不由下标决定。
- 双支并发插入同一锚点时，按 (分支名, 操作标识) 稳定裁定：分支名字典序
  较小的一支整体贴近锚点，分支内部则严格保持本支顺序重放结果（后发生的
  同锚点插入更贴近锚点）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

# 可打印 ASCII（含标点），但不含空白，保证标识可在行格式中安全切分
ID_RE = re.compile(r"^[!-\x7e]+$")
MAX_OPS_PER_BRANCH = 80
HEAD = "\x00__FIRST__\x00"  # 首位虚拟锚点：含控制字符，不可能与任何合法 ASCII 标识撞键

KIND_INSERT = "INSERT"
KIND_DELETE = "DELETE"
KIND_REPLACE = "REPLACE"
KINDS = (KIND_INSERT, KIND_DELETE, KIND_REPLACE)

# 结果裁定
R_KEPT = "kept"          # 保留：原样生效
R_TRANSFORMED = "transformed"  # 转换：因并发操作发生位移
R_MERGED = "merged"      # 合并：幂等重复操作并入先前操作


class OTReject(Exception):
    """输入结构层面的拒绝（非领域冲突），映射为 HTTP 400。"""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


@dataclass
class Op:
    branch: str
    seq: int  # 从 1 开始
    op_id: str
    kind: str
    target: Optional[str] = None
    anchor: Optional[str] = None   # None 表示 FIRST
    new_id: Optional[str] = None
    text: Optional[str] = None

    def loc(self) -> tuple[str, int]:
        return (self.branch, self.seq)

    def as_json(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "branch": self.branch,
            "seq": self.seq,
            "op_id": self.op_id,
            "kind": self.kind,
        }
        if self.target is not None:
            d["target"] = self.target
        if self.kind == KIND_INSERT:
            d["new_id"] = self.new_id
            d["anchor"] = self.anchor if self.anchor is not None else "FIRST"
        if self.text is not None:
            d["text"] = self.text
        return d


@dataclass
class Node:
    id: str
    text: str
    origin: str  # "baseline" 或插入它的分支名
    insert_op: Optional[Op] = None
    deleted: bool = False
    first_del: Optional[Op] = None
    # (op, text) —— 存活期间发生过的替换；同文本重复替换在这里合并
    rep_ops: list[tuple[Op, str]] = field(default_factory=list)


@dataclass
class InsertEvent:
    op: Op
    new_id: str
    anchor: Optional[str]  # None == FIRST
    branch: str
    seq: int


@dataclass
class Issue:
    """一次必须拒绝的冲突。order 用于稳定挑出"首个冲突"。"""

    order: list[tuple[str, int]]
    code: str
    basis: str
    op_a: Optional[Op]
    op_b: Optional[Op] = None
    ref: Optional[str] = None  # 涉及但不属于任何操作的标识（如悬空锚点）


@dataclass
class BranchState:
    name: str
    nodes: list[Node] = field(default_factory=list)
    by_id: dict[str, Node] = field(default_factory=dict)
    inserts: list[InsertEvent] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    # op_id -> 被并入的操作（幂等：重复删除 / 相同替换）
    merged_into: dict[str, Op] = field(default_factory=dict)
    ops: list[Op] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# 输入校验
# --------------------------------------------------------------------------- #
def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise OTReject(msg)


def _check_id(value: Any, label: str) -> str:
    _require(isinstance(value, str) and bool(ID_RE.fullmatch(value)),
             f"{label} 必须是非空、不含空白的 ASCII 标识")
    return value


def parse_request(payload: Any) -> tuple[list[dict[str, str]], dict[str, list[dict[str, Any]]]]:
    """校验并规整请求体。返回 (baseline, {分支名: [op dict]})。"""
    _require(isinstance(payload, dict), "请求体必须是 JSON 对象")
    base_raw = payload.get("baseline")
    _require(isinstance(base_raw, list) and len(base_raw) > 0, "baseline 必须是非空数组")
    baseline: list[dict[str, str]] = []
    seen: set[str] = set()
    for i, item in enumerate(base_raw):
        _require(isinstance(item, dict), f"baseline[{i}] 必须是对象")
        sid = _check_id(item.get("id"), f"baseline[{i}].id")
        text = item.get("text")
        _require(isinstance(text, str) and text != "", f"baseline[{i}].text 必须是非空字符串")
        _require(sid not in seen, f"基线步骤标识重复：{sid}")
        seen.add(sid)
        baseline.append({"id": sid, "text": text})

    br_raw = payload.get("branches")
    _require(isinstance(br_raw, list) and len(br_raw) == 2,
             "branches 必须是恰好两支（left / right）的数组")
    branches: dict[str, list[dict[str, Any]]] = {}
    names: set[str] = set()
    for b in br_raw:
        _require(isinstance(b, dict), "branches 的每项必须是对象")
        name = b.get("name")
        _require(isinstance(name, str) and name.strip() != "", "分支名必须是非空字符串")
        _require(name not in names, f"分支名重复：{name}")
        names.add(name)
        ops = b.get("ops")
        _require(isinstance(ops, list), f"分支 {name} 的 ops 必须是数组")
        _require(len(ops) <= MAX_OPS_PER_BRANCH,
                 f"分支 {name} 至多 {MAX_OPS_PER_BRANCH} 条操作，实际 {len(ops)} 条")
        op_ids: set[str] = set()
        clean: list[dict[str, Any]] = []
        for seq, raw in enumerate(ops, start=1):
            _require(isinstance(raw, dict), f"{name}#%d 操作必须是对象" % seq)
            op_id = _check_id(raw.get("op_id"), f"{name}#%d.op_id" % seq)
            _require(op_id not in op_ids, f"分支 {name} 操作标识重复：{op_id}")
            op_ids.add(op_id)
            kind = raw.get("kind")
            _require(kind in KINDS, f"{name}#{seq}({op_id}) kind 必须是 INSERT/DELETE/REPLACE")
            item: dict[str, Any] = {"op_id": op_id, "kind": kind}
            if kind == KIND_INSERT:
                new_id = _check_id(raw.get("new_id"), f"{name}#{seq}.new_id")
                anchor = raw.get("anchor", "FIRST")
                if anchor == "FIRST":
                    anchor = None
                else:
                    anchor = _check_id(anchor, f"{name}#{seq}.anchor")
                text = raw.get("text")
                _require(isinstance(text, str) and text != "",
                         f"{name}#{seq}({op_id}) text 必须是非空字符串")
                item.update(new_id=new_id, anchor=anchor, text=text)
            elif kind == KIND_DELETE:
                item["target"] = _check_id(raw.get("target"), f"{name}#{seq}.target")
            else:  # REPLACE
                item["target"] = _check_id(raw.get("target"), f"{name}#{seq}.target")
                text = raw.get("text")
                _require(isinstance(text, str) and text != "",
                         f"{name}#{seq}({op_id}) text 必须是非空字符串")
                item["text"] = text
            clean.append(item)
        branches[name] = clean
    return baseline, branches


# --------------------------------------------------------------------------- #
# 单支重放（墓碑保留序列模型）
# --------------------------------------------------------------------------- #
def replay(name: str, ops_raw: list[dict[str, Any]], baseline: list[dict[str, str]]) -> BranchState:
    st = BranchState(name=name)
    for b in baseline:
        st.nodes.append(Node(id=b["id"], text=b["text"], origin="baseline"))
    st.by_id = {n.id: n for n in st.nodes}

    for seq, raw in enumerate(ops_raw, start=1):
        op = Op(branch=name, seq=seq, op_id=raw["op_id"], kind=raw["kind"],
                target=raw.get("target"), anchor=raw.get("anchor"),
                new_id=raw.get("new_id"), text=raw.get("text"))
        st.ops.append(op)

        if op.kind == KIND_INSERT:
            if op.new_id in st.by_id:
                holder = st.by_id[op.new_id]
                partner = holder.insert_op
                if partner is not None:
                    basis = (f"新标识 {op.new_id} 已被本支操作 {partner.op_id}（#{partner.seq}）"
                             f"先行插入；插入步骤的全局标识必须唯一，拒绝重复新标识。")
                    st.issues.append(Issue([partner.loc(), op.loc()], "DUPLICATE_NEW_ID",
                                           basis, partner, op, ref=op.new_id))
                else:
                    basis = (f"新标识 {op.new_id} 与基线步骤标识冲突；插入标识必须全局唯一。")
                    st.issues.append(Issue([op.loc()], "DUPLICATE_NEW_ID", basis, op,
                                           ref=op.new_id))
                continue
            if op.anchor is not None and op.anchor not in st.by_id:
                basis = (f"插入 {op.new_id} 的锚点 {op.anchor} 在本支重放到第 {seq} 条时"
                         f"不存在：既非尚存/已删的基线步骤，也非本支先前插入的步骤，"
                         f"属悬空引用，序列模型无法定位。")
                st.issues.append(Issue([op.loc()], "DANGLING_ANCHOR", basis, op,
                                       ref=op.anchor))
                continue
            node = Node(id=op.new_id, text=op.text, origin=name, insert_op=op)
            if op.anchor is None:
                st.nodes.insert(0, node)
            else:
                anchor_node = st.by_id[op.anchor]
                pos = st.nodes.index(anchor_node)
                # 紧随锚点之后；墓碑节点同样占位，故位置确定、不漂移
                st.nodes.insert(pos + 1, node)
            st.by_id[node.id] = node
            st.inserts.append(InsertEvent(op=op, new_id=node.id, anchor=op.anchor,
                                          branch=name, seq=seq))
            continue

        # DELETE / REPLACE 均针对已存在目标
        target = op.target
        if target not in st.by_id:
            code = "DANGLING_TARGET"
            basis = (f"{op.kind} 操作引用的 {target} 在本支重放到第 {seq} 条时不存在"
                     f"（对侧分支引入的标识不能作为本支引用，属悬空引用）。")
            st.issues.append(Issue([op.loc()], code, basis, op, ref=target))
            continue
        node = st.by_id[target]

        if op.kind == KIND_DELETE:
            if node.deleted:
                # 重复删除 —— 幂等，合并入首次删除
                st.merged_into[op.op_id] = node.first_del
                continue
            if node.rep_ops:
                prev = node.rep_ops[-1][0]
                basis = (f"目标 {target} 已被本支操作 {prev.op_id}（#{prev.seq}）替换文本，"
                         f"删除与替换在同一步骤上交叉，存活状态无法同时成立，拒绝。")
                st.issues.append(Issue([prev.loc(), op.loc()], "DELETE_REPLACE_CONFLICT",
                                       basis, prev, op, ref=target))
                continue
            node.deleted = True
            node.first_del = op
        else:  # REPLACE
            if node.deleted:
                prev = node.first_del
                basis = (f"目标 {target} 已被本支操作 {prev.op_id}（#{prev.seq}）删除，"
                         f"墓碑只保留定位作用、不能再被替换文本，删除与替换交叉，拒绝。")
                st.issues.append(Issue([prev.loc(), op.loc()], "DELETE_REPLACE_CONFLICT",
                                       basis, prev, op, ref=target))
                continue
            if node.rep_ops:
                prev_op, prev_text = node.rep_ops[-1]
                if prev_text == op.text:
                    # 相同替换 —— 幂等合并
                    st.merged_into[op.op_id] = prev_op
                    continue
                basis = (f"目标 {target} 被本支 {prev_op.op_id}（#{prev_op.seq}）与 "
                         f"{op.op_id}（#{seq}）替换为不同文本，无法裁定同一槽位的最终文本，拒绝。")
                st.issues.append(Issue([prev_op.loc(), op.loc()], "DIVERGENT_REPLACE",
                                       basis, prev_op, op, ref=target))
                continue
            node.text = op.text
            node.rep_ops.append((op, op.text))

    return st


# --------------------------------------------------------------------------- #
# 双支交叉分析与合并
# --------------------------------------------------------------------------- #
def _cross_issues(states: dict[str, BranchState], baseline: list[dict[str, str]]) -> list[Issue]:
    issues: list[Issue] = []
    names = sorted(states)

    # 1) 跨支重复新标识（插入标识必须全局唯一）
    seen_insert: dict[str, InsertEvent] = {}
    for name in names:
        for ev in states[name].inserts:
            if ev.new_id in seen_insert:
                prev = seen_insert[ev.new_id]
                basis = (f"标识 {ev.new_id} 被两支分别插入：{prev.branch} 支 {prev.op.op_id}"
                         f"（#{prev.seq}）与 {ev.branch} 支 {ev.op.op_id}（#{ev.seq}）；"
                         f"插入标识必须全局唯一，拒绝重复新标识。")
                issues.append(Issue([prev.op.loc(), ev.op.loc()], "DUPLICATE_NEW_ID",
                                    basis, prev.op, ev.op, ref=ev.new_id))
            else:
                seen_insert[ev.new_id] = ev

    # 2) 基线步骤上的跨支删除/替换效应
    for b in baseline:
        sid = b["id"]
        dels: list[Op] = []
        reps: list[tuple[Op, str]] = []
        for name in names:
            node = states[name].by_id.get(sid)
            if node is None:
                continue
            if node.first_del is not None:
                dels.append(node.first_del)
            reps.extend(node.rep_ops)
        if dels and reps:
            d = dels[0]
            r = reps[0][0]
            first, second = sorted([d, r], key=lambda o: (o.branch, o.seq))
            basis = (f"步骤 {sid} 在 {d.branch} 支 {d.op_id}（#{d.seq}）被删除、"
                     f"又在 {r.branch} 支 {r.op_id}（#{r.seq}）被替换文本；"
                     f"删除与替换跨支交叉于同一步骤，墓碑与文本更新不能共存，拒绝。")
            issues.append(Issue([first.loc(), second.loc()], "DELETE_REPLACE_CONFLICT",
                                basis, first, second, ref=sid))
        texts = {text for _, text in reps}
        if len(texts) > 1:
            # 找出头两个文本不同的操作
            o1, t1 = reps[0]
            o2 = next(o for o, t in reps[1:] if t != t1)
            first, second = sorted([o1, o2], key=lambda o: (o.branch, o.seq))
            basis = (f"步骤 {sid} 被两支替换为不同文本（{first.op_id}: "
                     f"{next(t for o,t in reps if o is first)!r} vs "
                     f"{second.op_id}: {next(t for o,t in reps if o is second)!r}），"
                     f"同一槽位最终文本无共识，拒绝异替换。")
            issues.append(Issue([first.loc(), second.loc()], "DIVERGENT_REPLACE",
                                basis, first, second, ref=sid))
    return issues


def merge(payload: Any) -> dict[str, Any]:
    """主入口：校验 -> 双支重放 -> 交叉裁定 -> 合并表 + 逐操作结论。"""
    baseline, branches_raw = parse_request(payload)
    names = sorted(branches_raw)
    states = {name: replay(name, branches_raw[name], baseline) for name in names}

    issues: list[Issue] = []
    for name in names:
        issues.extend(states[name].issues)
    issues.extend(_cross_issues(states, baseline))

    if issues:
        issues.sort(key=lambda x: (x.order, x.code))
        first = issues[0]
        return {
            "ok": False,
            "conflict": {
                "code": first.code,
                "basis": first.basis,
                "op_a": first.op_a.as_json() if first.op_a else None,
                "op_b": first.op_b.as_json() if first.op_b else None,
                "ref": first.ref,
                "issue_count": len(issues),
            },
        }

    # ---- 无冲突：构建统一序列（递归展开锚点树） ----
    children: dict[str, list[InsertEvent]] = {}
    for name in names:
        for ev in states[name].inserts:
            children.setdefault(ev.anchor if ev.anchor is not None else HEAD, []).append(ev)
    # 同锚点：(分支名) 分块，分支名小者贴近锚点；分支内部保持重放结果（seq 大的贴近锚点）
    for evs in children.values():
        evs.sort(key=lambda e: (e.branch, -e.seq, e.op.op_id))

    def unified_node(node_id: str) -> Node:
        if any(node_id == b["id"] for b in baseline):
            base_text = next(b["text"] for b in baseline if b["id"] == node_id)
            node = Node(id=node_id, text=base_text, origin="baseline")
            dels: list[Op] = []
            reps: list[tuple[Op, str]] = []
            for name in names:
                src = states[name].by_id[node_id]
                if src.first_del is not None:
                    dels.append(src.first_del)
                reps.extend(src.rep_ops)
            if dels:
                keeper = min(dels, key=lambda o: (o.branch, o.op_id))
                node.deleted = True
                node.first_del = keeper
            if reps:
                node.text = reps[0][1]
                node.rep_ops = reps
            return node
        # 插入节点只可能存在于一支
        for name in names:
            src = states[name].by_id.get(node_id)
            if src is not None:
                return src
        raise KeyError(node_id)

    ordered: list[Node] = []

    def emit(node_id: str) -> None:
        node = unified_node(node_id)
        ordered.append(node)
        for ev in children.get(node_id, []):
            emit(ev.new_id)

    for ev in children.get(HEAD, []):
        emit(ev.new_id)
    for b in baseline:
        emit(b["id"])

    merged_index = {n.id: i for i, n in enumerate(ordered)}

    # ---- 跨支幂等合并关系（重复删除 / 相同替换）并入 merged_into ----
    for b in baseline:
        sid = b["id"]
        dels = [states[n].by_id[sid].first_del for n in names
                if states[n].by_id[sid].first_del is not None]
        if len(dels) > 1:
            keeper = min(dels, key=lambda o: (o.branch, o.op_id))
            for op in dels:
                if op is not keeper:
                    states[op.branch].merged_into[op.op_id] = keeper
        reps_all = [(op, text) for n in names for op, text in states[n].by_id[sid].rep_ops
                    if op.op_id not in states[n].merged_into]
        by_text: dict[str, list[Op]] = {}
        for op, text in reps_all:
            by_text.setdefault(text, []).append(op)
        for text, ops in by_text.items():
            if len(ops) > 1:
                keeper = min(ops, key=lambda o: (o.branch, o.op_id))
                for op in ops:
                    if op is not keeper and op.op_id not in states[op.branch].merged_into:
                        states[op.branch].merged_into[op.op_id] = keeper

    # ---- 逐操作结论 ----
    outcomes: list[dict[str, Any]] = []
    for name in names:
        st = states[name]
        own_index = {n.id: i for i, n in enumerate(st.nodes)}
        concurrent_anchor: dict[Optional[str], list[InsertEvent]] = {}
        other = names[1] if names[0] == name else names[0]
        for ev in states[other].inserts:
            concurrent_anchor.setdefault(ev.anchor, []).append(ev)

        for op in st.ops:
            if op.op_id in st.merged_into:
                keeper = st.merged_into[op.op_id]
                if op.kind == KIND_DELETE:
                    basis = (f"对 {op.target} 的删除与 {keeper.branch} 支 {keeper.op_id}"
                             f"（#{keeper.seq}）效果完全相同；重复删除幂等，本操作合并入该操作，"
                             f"墓碑只立一次。")
                else:
                    basis = (f"对 {op.target} 的替换文本与 {keeper.branch} 支 {keeper.op_id}"
                             f"（#{keeper.seq}）逐字相同；相同替换幂等，本操作合并入该操作。")
                outcomes.append({"branch": name, "seq": op.seq, "op_id": op.op_id,
                                 "kind": op.kind, "result": R_MERGED,
                                 "merged_into": keeper.as_json(), "basis": basis,
                                 "target": op.target})
                continue

            if op.kind == KIND_INSERT:
                anchor_label = op.anchor if op.anchor is not None else "FIRST"
                before = own_index[op.new_id]
                after = merged_index[op.new_id]
                peers = concurrent_anchor.get(op.anchor, [])
                anchor_node = (ordered[merged_index[op.anchor]]
                               if op.anchor is not None else None)
                moved = before != after
                if peers:
                    basis = (
                        f"与 {','.join(e.branch + ' 支 ' + e.op.op_id for e in peers)} "
                        f"并发锚定同一锚点 {anchor_label}；按 (分支名, 操作标识) 稳定裁定，"
                        f"分支序 {names[0]} < {names[1]}，字典序小者整体贴近锚点、分支内保持"
                        f"顺序重放结果；本插入标识与锚点不变，序列位置 {before} → {after}"
                        f"{'（发生位移，故转换）' if moved else '（恰为贴近锚点一方，位置保留）'}。")
                    result = R_TRANSFORMED if moved else R_KEPT
                elif moved:
                    basis = (f"锚点 {anchor_label} 无同锚点并发，但对支在更早序列位置的插入"
                             f"使本插入整体后移：序列位置 {before} → {after}，标识与锚点不变，"
                             f"属于并发插入导致的位置转换。")
                    result = R_TRANSFORMED
                elif anchor_node is not None and anchor_node.deleted:
                    basis = (f"锚点 {anchor_label} 已是墓碑（被 {anchor_node.first_del.op_id} 删除）；"
                             f"墓碑保留序列位置，本插入紧随其后，位置 {after} 保留，不发生漂移。")
                    result = R_KEPT
                else:
                    basis = f"锚点 {anchor_label} 与新标识 {op.new_id} 均稳定，无并发位移，位置 {after} 保留。"
                    result = R_KEPT
                outcomes.append({"branch": name, "seq": op.seq, "op_id": op.op_id,
                                 "kind": op.kind, "result": result,
                                 "new_id": op.new_id, "anchor": anchor_label,
                                 "position_before": before, "position_after": after,
                                 "basis": basis})
            elif op.kind == KIND_DELETE:
                basis = (f"{op.target} 未被对侧异改：删除生效，节点保留为墓碑锚点，"
                         f"合并序列位置 {merged_index[op.target]}，后续锚定不漂移。")
                outcomes.append({"branch": name, "seq": op.seq, "op_id": op.op_id,
                                 "kind": op.kind, "result": R_KEPT, "target": op.target,
                                 "basis": basis})
            else:
                basis = (f"{op.target} 未被任何一支删除且替换文本一致：文本生效，标识与序列位置"
                         f"{merged_index[op.target]} 保留。")
                outcomes.append({"branch": name, "seq": op.seq, "op_id": op.op_id,
                                 "kind": op.kind, "result": R_KEPT, "target": op.target,
                                 "text": op.text, "basis": basis})

    merged_rows: list[dict[str, Any]] = []
    step_no = 0
    for i, node in enumerate(ordered):
        if not node.deleted:
            step_no += 1
        tags = []
        if node.origin != "baseline":
            tags.append(f"插入自 {node.origin} 支")
        if node.rep_ops:
            tags.append("文本已替换")
        status = "tombstone" if node.deleted else ("inserted" if node.origin != "baseline"
                                                    else "replaced" if node.rep_ops else "live")
        merged_rows.append({
            "position": i,
            "step_no": None if node.deleted else step_no,
            "id": node.id,
            "text": node.text,
            "status": status,
            "origin": node.origin,
            "tags": tags,
            "deleted_by": node.first_del.op_id if node.first_del else None,
        })

    return {
        "ok": True,
        "arbitration": {
            "rule": "同锚点并发插入按 (分支名, 操作标识) 字典序裁定；分支内保持顺序重放",
            "branch_order": names,
        },
        "merged": merged_rows,
        "outcomes": outcomes,
        "stats": {
            "baseline": len(baseline),
            "ops": {n: len(states[n].ops) for n in names},
            "live": step_no,
            "tombstones": len(ordered) - step_no,
        },
    }
