# 文件入仓内存优化交接

本轮只替换 CLI `import` 的整条文件链，业务语义、有限 XML 格式、Python 对象接口、
异常类别、规范化载荷、数据集绑定、回执与批次指纹均未改变。原 `checks.py`、
`tests/`、`scenarios/`、`examples/`、`import_workload.py`、`import_bench.py`、
`reference_import.py` 与既有断言未改动。

## 改动位置

| 文件 | 作用 |
| --- | --- |
| `xbrl_local/streamio.py`（新增） | 以文件流 + `XMLPullParser` 逐段读取，一次只持有一个顶层元素；保留 `xmlio.parse` 的元素级命名空间作用域语义、根校验、DTD/ENTITY 拒绝和 UTF-8 契约。 |
| `xbrl_local/stream_import.py`（新增） | 两遍扫描的暂存与发布：pass 1 校验并暂存 context/unit 与全局 id 唯一性；pass 2 校验事实、解析引用、暂存规范载荷；随后按输入序求资格、按 id 序折叠指纹，一个 `BEGIN IMMEDIATE` 事务发布。 |
| `xbrl_local/instance.py` | 抽出 `parse_context` / `parse_unit` / `parse_fact` 作为唯一校验实现；`Instance.from_xml` 行为不变，文件入口与之共用同一套规则。 |
| `xbrl_local/warehouse.py` | `ingest` 改为构造行后调用新的 `publish_stream`（惰性遍历 + 同一事务逻辑）；对象路径结果与旧实现逐字节一致。 |
| `xbrl_local/__main__.py` | `import` 改走 `prepare_file` + `prepared.publish`；错误类别、退出码与 JSON 结构不变。 |
| `import_faults.py` | 仅一处设计匹配适配：`commit` 注入跳过暂存连接，只命中持久仓提交。 |
| `staging_checks.py`（新增） | 暂存读写/创建失败、回滚、重试与释放核验。 |
| `compare_import.py`（新增） | 参照对象路径与优化文件路径的独立逐库对照。 |
| `verify_import.py` | 原有步骤全保留，追加 `staging_checks.py` 与 `compare_import.py`。 |
| `.pointwise-runtime.json` | verify 末参数由 `baseline` 切为 `target`。 |

## 处理流程

1. `prepare_file` 先扫描实例：每个顶层元素都要求非空 `id` 并写入暂存 `ids` 表，
   因此"全实例 XML id 唯一"与"未用定义仍须校验"都保留，且不依赖 Python 集合。
2. pass 1 末尾若没有事实则 `InputError('empty fact batch')`，与对象路径一致。
3. pass 2 再次扫描，仅对事实做语法校验与引用解析；缺引用仍报
   `unresolved context or unit`。解析顺序允许事实先于上下文和单位。
4. `PreparedImport.publish` 先校验 dataset/batch 标识，再按输入序求资格（`issues`
   顺序不变），再按 `fact_id` 序增量折叠指纹，最后单事务发布。
5. 暂存是 `tempfile.mkstemp` 于 `TMPDIR` 的自建 SQLite 文件，`close()` 关闭并
   `unlink`，成功、失败、异常路径都释放。

## 不变量（与参照一致，已在核验中逐项断言）

- 规范事实载荷字段、键序与紧凑 JSON 编码；数据集绑定为分类标准文本 SHA-256。
- 批次指纹 = `sha256(json([taxonomy_digest, rows_sorted_by_id]))`，按 `id` 排序后折叠，
  与 `import_workload.expected_fingerprint` 的独立字面量口径吻合。
- 同批同内容返回原回执（含原 `inserted`）；同批不同内容 `ConflictError`。
- 事实换序重放返回原回执；未用上下文/单位不进入指纹。
- 局部 QName 作用域按元素解析；事实先于定义可用。
- 只有完整输入与业务校验通过才发布；尾部畸形、未解析引用、资格失败、身份冲突、
  提交失败均不留下业务事实、数据集绑定或回执。
- 失败批次修正后可用原批次号重试。

## 实测（本机 Linux，`import_bench.py target`，2026-11-16）

在 25k/100k 两种负载各三轮的完整子进程 `ru_maxrss` 中位数如下（上限 96 MiB，
同类 4 倍增量上限 12 MiB）：

| 负载 | 25k 中位 | 100k 中位 | 增量 |
| --- | --- | --- | --- |
| late | 23.27 MiB | 25.96 MiB | 2.69 MiB |
| scoped | 32.33 MiB | 38.83 MiB | 6.50 MiB |

对照未优化参照（同一 25k late 负载）：217.6 MiB，约 9 倍差距。

补充观测：在可执行文件内直接用 `resource.getrusage` 计量本进程时，25000/100000/
400000 事实（scoped）峰值分别约 22.8 / 23.0 / 25.8 MiB，增量约 0.2 / 3.0 MiB，
远小于 16 倍规模的线性预期。基准脚本报出的逐轮上升主要来自父进程在 `fork` 前的
堆状态被 `ru_maxrss` 继承（已单独复现：父进程先分配 200 MiB 再 `execve`，子进程
峰值即报约 208 MiB）。因此上表是偏保守的上界，优化实现本身接近常数。

## 核验覆盖

- `checks.py`：原 12 项核心/文件回归，未改断言。
- `import_checks.py`：16 项文件边界（换序重放、未用定义、局部作用域、尾部损坏、
  跨类 id 冲突、引用缺失、资格顺序、事实冲突、分类标准绑定、SQL 写入失败、
  commit 一次性失败、编码错误、失败批次重试）。
- `staging_checks.py`（新增 6 项）：真实导入后暂存文件被删除；`temp-read`/
  `temp-write` 注入实际激活、异常退出 2、库不变、暂存不残留；`mkstemp` 创建失败
  路径；分类标准绑定失败后释放；`commit` 注入命中持久仓提交并回滚，随后同批次号
  重试成功。
- `compare_import.py`（新增 6 项）：late/scoped 各两种规模与换序重放，逐库比对
  `datasets`/`facts`/`batches` 全表，另含局部前缀与未用定义。
- `import_bench.py demo`：真实文件导入 + 同批重放 + 独立指纹核对。
- 额外本地对照（不入库）：182 个变异实例的参照/目标状态与错误类别逐一比对；
  1/2/3/5/7/…/65536 字节分块下的流式解析一致性；BOM、XML 声明、注释、PI、
  自闭合根、跨分块 DTD 声明；非 ASCII 事实 id 的排序与指纹。

## 复现

```bash
python3.11 -B verify_import.py target     # 原检查 + 暂存/对照 + 目标 RSS 断言
python3.11 -B import_bench.py reference   # 未优化基线实测（不套阈值）
python3.11 -B import_bench.py demo        # 真实文件导入 + 同批重放 + 指纹
```

`reference_import.py` 仍固定调用 `Taxonomy.from_file` / `Instance.from_file` /
`Warehouse.ingest`，未接入优化后的文件入口，因此其计量仍是优化前证据。

## 边界与限制

- 实例文件在一次调用内被读取两遍；契约允许重复读取，文件不可变即可。
- 暂存位于 `TMPDIR`，需要该目录可创建/写入；不可用时导入以 `OSError` 退出 2，
  不发布任何业务结果（`staging_checks.py` 覆盖）。
- 不提供进程被强杀后的恢复、并发导入或断点续传（契约明确不要求）。
- 单条顶层元素仍需能一次装入内存；契约已声明单条大小有界。
