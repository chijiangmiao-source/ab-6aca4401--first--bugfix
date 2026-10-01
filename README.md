# 飞行试验检查单 · 双支离线修订 OT 复核

两支离线修订（left / right）同时回传后，复核员在浏览器中判断它们能否合为一份
可执行步骤表。系统采用**保留删除锚点（墓碑）的序列模型**完成双支操作变换，
防止位置漂移让修改落到错误步骤。

## 模型要点

- 基线步骤带唯一 ASCII 标识；每支至多 **80** 条按顺序发生的
  `INSERT` / `DELETE` / `REPLACE`。
- 插入带全局唯一新标识，可锚定 `FIRST` 或任一**尚存、已删（墓碑）或本支先前
  插入**的步骤；删除只置墓碑、节点永不回收，故锚点位置永不漂移。
- **同锚点并发插入**按 `(分支名, 操作标识)` 稳定裁定：分支名字典序小者整体
  贴近锚点；分支内部保持顺序重放结果（后发生的同锚点插入更贴近锚点）。
- 幂等合并：相同替换、重复删除（支内或跨支）合并入先生效的操作。
- 必须拒绝并展示**首个冲突**的双方操作与变换依据：
  - `DIVERGENT_REPLACE` 同一步骤的不同替换
  - `DELETE_REPLACE_CONFLICT` 删除与替换交叉
  - `DUPLICATE_NEW_ID` 插入新标识重复
  - `DANGLING_ANCHOR` / `DANGLING_TARGET` 悬空引用
- 通过时同时展示**规范合并步骤表**（墓碑灰显、执行序号连续）与**每条原操作的
  保留 / 转换 / 合并结论及依据**。输入改变或校验失败，页面旧结论立即作废。

零第三方依赖：Python 3.11 标准库 HTTP 服务 + 原生前端；测试用 `unittest`。

## 本地运行（无需 Docker）

```bash
python3 -m app.server            # 默认 0.0.0.0:8080，可用 PORT 覆盖
curl http://127.0.0.1:8080/healthz
python3 -m unittest discover -s tests -v
```

## Docker Compose

```bash
docker compose up -d web                 # 浏览器访问 http://localhost:8080
HOST_PORT=9090 docker compose up -d web  # 可配置宿主端口
```

验收服务 `verify`：构建镜像、等待 `web` 健康检查通过后，运行**领域测试 +
构建检查 + 页面 / 健康 HTTP 冒烟**（覆盖同锚点并发插入、墓碑后插入、幂等合并、
冲突拒绝），随后退出，退出码即验收结论：

```bash
docker compose build web verify
docker compose run verify; echo "退出码=$?"   # 0 = 验收通过
# 或一次性： docker compose up --build verify （web 会作为依赖一并启动）
```

## API

`POST /api/merge`

```json
{
  "baseline": [{"id": "A", "text": "起飞前绕机检查"}],
  "branches": [
    {"name": "left",  "ops": [
      {"op_id": "L1", "kind": "INSERT", "new_id": "x", "anchor": "A", "text": "左翼油量复查"},
      {"op_id": "L2", "kind": "DELETE", "target": "A"},
      {"op_id": "L3", "kind": "REPLACE", "target": "x", "text": "复查双发油量"}
    ]},
    {"name": "right", "ops": []}
  ]
}
```

- `200 {"ok": true, ...}`：含 `merged`（规范步骤表）、`outcomes`（逐操作结论）、
  `arbitration`（裁定规则与分支序）。
- `200 {"ok": false, "conflict": {...}}`：领域冲突，含 `code`、`basis`、
  `op_a` / `op_b`（首个冲突双方）、`ref`、`issue_count`。
- `400 {"error": ...}`：结构非法（JSON 错误、标识不合规、超过 80 条等）。
