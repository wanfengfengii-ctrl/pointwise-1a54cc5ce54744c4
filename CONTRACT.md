# 文件入仓内存优化契约

现有业务、有限 XML 格式、Python 对象接口与异常类别见 README.md；examples/ 和原 tests/ 均保留。本轮只改善已有 import 的完整文件导入内存，不增加业务功能。分类标准规模固定且小，实例文件为本地 UTF-8，一次调用内不变，允许重复读取。单条顶层元素大小有界，不要求处理无限深嵌套或完整 XBRL 标准。

## 兼容结果

仍使用现有命令：

    python3.11 -B -m xbrl_local --db facts.sqlite import --dataset load --batch batch --taxonomy examples/taxonomy.xml --instance input.xml

成功输出 JSON 回执并退出 0；输入、资格、身份及文件或 SQLite 操作失败沿用退出 2 和 error JSON，资格失败另有 issues。error 的文字可改变，异常类别与 issues 内容及顺序不可改变。旧 Python 对象接口继续兼容；不要求其全量对象返回值满足文件入口的内存指标。

事实可先于上下文和单位；QName 按所在元素的命名空间解析，全实例顶层 XML id 唯一，未使用的上下文与单位仍须校验但不参与批次指纹。支持格式与原实现相同，不扩大 id 检查到原来不识别的嵌套属性。事实的原始数值和 decimals 文本不作数值改写。

成功后的规范事实载荷、数据集绑定、回执及数据库内批次指纹须与原对象路径一致；事实换序重放返回原回执，资格 issues 保持事实输入顺序。已有 SQLite user_version 和三张业务表继续可用，旧数据库和对象查询接口仍能读取结果。可以新增内部辅助表示，不要求采用指定存储或解析算法。

只有完整输入及业务校验通过才可发布事实和回执；尾部畸形、未解析引用、资格失败、身份冲突及读写失败不得留下部分业务事实、数据集绑定或回执。允许创建空数据库和原有 schema。自建临时资源在成功或失败退出时释放；不要求进程被强杀后的恢复、并发导入或断点续传。

## 可重复负载与独立期望

import_workload.py 逐条生成两类负载，每类分别为 25000 和 100000 个事实，分类标准固定使用 examples/taxonomy.xml：

* late：全部事实在前，随后才定义各自唯一的上下文及单位。
* scoped：逐组交错事实、上下文和单位；相邻 explicitMember 与 measure 将同一 q 前缀局部绑定到不同 URI，根的 q 指向无关 URI。

每个 f000000 等事实应保存 +001.2500、decimals=+0、对应 c000000、实体机构000000、2026-10-31、Region=North 和展开的 USD 单位。两个负载的规范行相同。独立 row() 与 expected_fingerprint() 不调用生产解析、资格或规范化逻辑，check_database() 逐条核对全部规范载荷、绑定、回执和指纹。小样例及文件错误由 import_checks.py 核验；原完整回归不作删改。

reference_import.py 固定调用原有 Taxonomy.from_file、Instance.from_file、Warehouse.ingest 对象路径，作为未优化基线入口，不包含另一套母项目源码。它和 import_workload.py、import_bench.py 的现有负载、期望及计量断言不得更改，也不得将参照入口接到优化后的文件入口。允许兼容改善对象实现；因此初始 reference 的实测是优化前证据，最终正确性还必须满足独立载荷期望，不能仅靠两条路径互比。

## RSS 与失败边界

各类完整导入三轮子进程峰值 RSS 中位数均须不超过 96MiB，同类四倍规模的峰值增量不超过 12MiB。import_bench.py target 从真实 CLI 启动到退出计量，包含解析、暂存、资格判定、指纹和提交；负载生成、数据库核对由父进程完成，不计入被测 RSS。Linux 的 ru_maxrss 按 KiB、macOS 按字节换算为 MiB。不得将业务负载转移给未计量进程或预存测试答案。

    python3.11 -B import_bench.py reference
    python3.11 -B import_bench.py target

reference 只报告未优化实测，不套用目标阈值；target 执行相同业务核对并断言目标。每次使用全新数据库，完整输出逐轮 RSS、中位数及指纹。600 秒子进程看门狗用于避免验证挂起；触发时报告未完成，不冒充资源达标或业务反例。若平台不能完整运行参照或目标，必须先退回核验，不把缺测判通过。

import_checks.py 核对换序重放、未用定义、局部作用域、尾部损坏、重复身份、引用缺失、资格顺序、事实冲突、分类标准绑定、提交失败及中途 SQL 写入失败后的原库状态。import_faults.py 提供一次性 commit/temp-read/temp-write 注入，且写出 activated 证据；它拦截常见 Python 文件访问及 SQLite 连接调用，不能覆盖所有原生 I/O。优化交付须补实际暂存读写失败及释放验证；若采用文件描述符、其他暂存位置或自定义 SQLite 工厂，应提供与实际设计匹配的注入适配。未激活不能算通过，不能用未捕获退出替代异常与回滚核对。无需为没有使用的暂存类型虚构故障证据。

## 核验与演示交付

    python3.11 -B verify_import.py baseline
    python3.11 -B verify_import.py target
    python3.11 -B import_bench.py demo

baseline 包含全部原检查、文件兼容检查和未优化 RSS 报告；target 以目标 CLI 的 RSS 检查替代参照计量。demo 实际导入含晚到定义和局部 QName 的文件，再重放同批，读取真实数据库核对规范载荷与指纹。

.pointwise-runtime.json 已保留 version=1、kind=cli、setup 以及 argv 数组形式的 verify/demo；初始 verify 选择 baseline。完成优化后将 verify 的最后参数切换为 target，并将自己补充的必要暂存异常检查纳入真实核验。demo 继续展示文件路径的真实结果。交付兼容实现、独立结果对照、必要失败边界核验、资源实测及简短交接不变量说明。不得修改原测试、削弱现有断言或更改负载达标。
