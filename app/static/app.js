"use strict";

const SAMPLES = {
  concurrent: {
    baseline: [
      { id: "A", text: "起飞前绕机检查" },
      { id: "B", text: "确认襟翼处于起飞位" },
      { id: "C", text: "核对起飞简令" }
    ],
    branches: [
      {
        name: "left",
        ops: [
          { op_id: "L1", kind: "INSERT", new_id: "x-LTANK", anchor: "B", text: "左翼油箱油量复查" },
          { op_id: "L2", kind: "DELETE", target: "B" },
          { op_id: "L3", kind: "INSERT", new_id: "y-AFTER-TOMB", anchor: "B", text: "B 已删除仍锚定其后：检查单留存归档" },
          { op_id: "L4", kind: "INSERT", new_id: "z-WX", anchor: "FIRST", text: "最前位：气象雷达最后扫描" }
        ]
      },
      {
        name: "right",
        ops: [
          { op_id: "R1", kind: "INSERT", new_id: "p-RTANK", anchor: "B", text: "右翼油箱油量复查" },
          { op_id: "R2", kind: "INSERT", new_id: "q-AIL", anchor: "B", text: "右翼副翼行程复查" }
        ]
      }
    ]
  },
  merge: {
    baseline: [
      { id: "A", text: "记录起飞构型" },
      { id: "B", text: "设定起飞推力" },
      { id: "C", text: "喊话 V1" }
    ],
    branches: [
      {
        name: "left",
        ops: [
          { op_id: "L1", kind: "REPLACE", target: "A", text: "记录起飞构型（双签）" },
          { op_id: "L2", kind: "DELETE", target: "B" },
          { op_id: "L3", kind: "DELETE", target: "B" },
          { op_id: "L4", kind: "REPLACE", target: "A", text: "记录起飞构型（双签）" }
        ]
      },
      {
        name: "right",
        ops: [
          { op_id: "R1", kind: "REPLACE", target: "A", text: "记录起飞构型（双签）" },
          { op_id: "R2", kind: "DELETE", target: "B" },
          { op_id: "R3", kind: "REPLACE", target: "C", text: "喊话 V1 并计时" }
        ]
      }
    ]
  },
  conflict: {
    baseline: [
      { id: "A", text: "开车前许可确认" },
      { id: "B", text: "滑行路线复核" },
      { id: "C", text: "进入跑道前停顿检查" }
    ],
    branches: [
      {
        name: "left",
        ops: [
          { op_id: "L1", kind: "REPLACE", target: "A", text: "开车前许可确认（塔台频率）" },
          { op_id: "L2", kind: "DELETE", target: "C" }
        ]
      },
      {
        name: "right",
        ops: [
          { op_id: "R1", kind: "REPLACE", target: "A", text: "开车前许可确认（地面频率）" },
          { op_id: "R2", kind: "REPLACE", target: "C", text: "进入跑道前停顿检查并开灯" }
        ]
      }
    ]
  }
};

const $ = (id) => document.getElementById(id);
const inputEl = $("input");
const stateEl = $("input-state");

function esc(v) {
  return String(v ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[c]));
}

function markDirty(msg) {
  stateEl.textContent = msg;
  stateEl.className = "input-state dirty";
  $("result-area").classList.add("hidden");
  $("conflict-box").classList.add("hidden");
  $("error-box").classList.add("hidden");
}

inputEl.addEventListener("input", () => {
  markDirty("输入已改变：旧结论已作废，需重新执行复核");
});

document.querySelectorAll("button[data-sample]").forEach((btn) => {
  btn.addEventListener("click", () => {
    inputEl.value = JSON.stringify(SAMPLES[btn.dataset.sample], null, 2);
    markDirty("已载入示例，尚未复核");
  });
});

$("verify-btn").addEventListener("click", async () => {
  let payload;
  try {
    payload = JSON.parse(inputEl.value);
  } catch (err) {
    markDirty("JSON 解析失败，未产生任何结论");
    const box = $("error-box");
    box.classList.remove("hidden");
    box.innerHTML = `<h3>输入无法解析</h3><p>${esc(err.message)}</p>`;
    return;
  }
  let res;
  try {
    const resp = await fetch("/api/merge", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    });
    res = await resp.json();
    if (!resp.ok) {
      throw new Error(res.error || `HTTP ${resp.status}`);
    }
  } catch (err) {
    markDirty("请求失败，未产生任何结论");
    const box = $("error-box");
    box.classList.remove("hidden");
    box.innerHTML = `<h3>服务拒绝了请求</h3><p>${esc(err.message)}</p>`;
    return;
  }

  if (!res.ok) {
    stateEl.textContent = "复核结论：拒绝合并（存在必须裁定的冲突）";
    stateEl.className = "input-state dirty";
    $("result-area").classList.add("hidden");
    renderConflict(res.conflict);
    return;
  }

  stateEl.textContent = "复核结论：通过，可执行步骤表已生成";
  stateEl.className = "input-state fresh";
  $("conflict-box").classList.add("hidden");
  $("error-box").classList.add("hidden");
  renderMerged(res);
  renderOutcomes(res);
  $("result-area").classList.remove("hidden");
});

function opCard(op, label) {
  if (!op) {
    return `<div class="op-card dead">${esc(label)}：不适用（该冲突由单方操作自身非法导致，无另一方操作）</div>`;
  }
  return `<div class="op-card"><strong>${esc(label)}</strong>\n${esc(JSON.stringify(op, null, 2))}</div>`;
}

function renderConflict(c) {
  const box = $("conflict-box");
  box.classList.remove("hidden");
  box.innerHTML = `
    <h3>⛔ 首个冲突：${esc(c.code)}（全表共 ${c.issue_count} 处，仅展示首个）</h3>
    <p><strong>变换依据：</strong>${esc(c.basis)}</p>
    <p><strong>涉及标识：</strong><code>${esc(c.ref ?? "—")}</code></p>
    <div class="pair">${opCard(c.op_a, "冲突方 A（首操作）")}${opCard(c.op_b, "冲突方 B")}</div>`;
}

function renderMerged(res) {
  const arb = res.arbitration;
  $("arbitration").textContent =
    `裁定规则：${arb.rule}；本次分支序：${arb.branch_order.join(" < ")}（字典序小者同锚点时贴近锚点）`;
  const tbody = $("merged-table").querySelector("tbody");
  tbody.innerHTML = res.merged.map((row) => {
    const stepCell = row.step_no == null
      ? '<span class="step-del">墓碑</span>'
      : `<span class="step-no">${row.step_no}</span>`;
    const tags = row.tags.length ? `<br><span class="basis-line">${row.tags.map(esc).join("；")}</span>` : "";
    const del = row.deleted_by ? `<br><span class="basis-line">删除操作：${esc(row.deleted_by)}</span>` : "";
    return `<tr class="${esc(row.status)}">
      <td>${stepCell}</td>
      <td>${row.position}</td>
      <td class="id-cell">${esc(row.id)}</td>
      <td>${esc(row.text)}</td>
      <td><span class="badge ${esc(row.status)}">${esc(row.status)}</span>${tags}${del}</td>
    </tr>`;
  }).join("");
  const s = res.stats;
  $("stats-line").textContent =
    `基线 ${s.baseline} 条；操作 left ${s.ops.left ?? 0} 条 / right ${s.ops.right ?? 0} 条；` +
    `可执行步骤 ${s.live} 条，墓碑保留 ${s.tombstones} 个。`;
}

const RESULT_LABEL = { kept: "保留", transformed: "转换", merged: "合并" };

function renderOutcomes(res) {
  const tbody = $("outcome-table").querySelector("tbody");
  tbody.innerHTML = res.outcomes.map((o) => {
    const detail = o.target ? `目标 <code>${esc(o.target)}</code>`
      : `新标识 <code>${esc(o.new_id)}</code> @ ${esc(o.anchor)}`;
    const shift = o.kind === "INSERT"
      ? `<br><span class="basis-line">序列位 ${o.position_before} → ${o.position_after}</span>` : "";
    const merged = o.merged_into
      ? `<br><span class="basis-line">并入 ${esc(o.merged_into.branch)} 支 ${esc(o.merged_into.op_id)} #${o.merged_into.seq}</span>`
      : "";
    return `<tr>
      <td>${esc(o.branch)}</td>
      <td>${o.seq}</td>
      <td class="id-cell">${esc(o.op_id)}</td>
      <td>${esc(o.kind)}<br><span class="basis-line">${detail}</span>${shift}${merged}</td>
      <td><span class="result-${esc(o.result)}">${esc(RESULT_LABEL[o.result] || o.result)}</span></td>
      <td>${esc(o.basis)}</td>
    </tr>`;
  }).join("");
}

inputEl.value = JSON.stringify(SAMPLES.concurrent, null, 2);
markDirty("已载入示例，尚未复核");
