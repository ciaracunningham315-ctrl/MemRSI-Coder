# EvoCode Benchmark 运行与复现说明

本目录包含 EvoCode 的完整评测题集、阶段 Git 包构建脚本、隔离判分器与并发调度执行器。关于评测方法论与实测结果，请优先查阅主文档：

- [**Benchmark 评测设计思路 (`../docs/benchmark-design.md`)**](../docs/benchmark-design.md)
- [**Benchmark 评测结果与图表分析 (`../docs/benchmark-results.md`)**](../docs/benchmark-results.md)
- [正式运行脱敏汇总报表 (`reports/formal_summary/summary.md`)](reports/formal_summary/summary.md)

---

## 1. 题集规模与实验结构

评测覆盖三个独立 Python 仓库（[repo_api](scenarios/repo_api/README.md)、[repo_cli](scenarios/repo_cli/README.md)、[repo_data](scenarios/repo_data/README.md)），每个仓库包含 `T1 Seed`、`T2 Transfer`、`T3 Stale`、`T4 Held-out` 四个时序阶段，共 **12 道主任务**：

- **54 个主实验运行位**：实验 A（`A1` / `A2` 各 9 题，测公共历史下的 Raw-RAG 开关）+ 实验 B（`B2` / `B3` / `B4` 各 12 题，测静态技能、一阶任务技能演化与完整双环自进化）。
- **8 个独立门禁审计候选（NSRR）**：在独立审计工作区与外部标注集上单独检验 `Skill Gate` 对有害、过期与重复无增益技能的拦截能力，审计状态不回流主实验。

---

## 2. 离线题集构建与校验

在 `benchmark/` 目录下可通过 `uv` 重建各阶段只读 Git 包并执行离线契约与题集验收（无需调用在线模型）：

```bash
uv run --project .. --extra dev python scripts/build_stage_packages.py
uv run --project .. --extra dev python scripts/build_stage_packages.py --repository-id repo_cli
uv run --project .. --extra dev python scripts/build_stage_packages.py --repository-id repo_data

uv run --project .. --extra dev python scripts/validate_dataset.py --probe-python "$(uv run --project .. --extra dev python -c 'import sys; print(sys.executable)')"
uv run --project .. --extra dev python scripts/validate_dataset.py --repository-id repo_cli
uv run --project .. --extra dev python scripts/validate_dataset.py --repository-id repo_data

uv run --project .. --extra dev python scripts/qualify_repairs.py
uv run --project .. --extra dev python scripts/validate_gate_tasks.py
uv run --project .. --extra dev python -m pytest -q tests
```

---

## 3. 在线预检与全量评测执行

运行在线评测前，先根据 [`configs/chain_integration.example.yaml`](configs/chain_integration.example.yaml) 配置本地运行文件（本地配置与密钥均已纳入 `.gitignore` 忽略保护）：

```bash
# 本地静态检查（不发起网络请求）
uv run --project .. python scripts/run_formal.py --skip-service-checks

# 在线服务只读预检（核对推理、向量、精排与本地 Docker 镜像就绪状态）
uv run --project .. python scripts/run_formal.py --preflight-only \
  --config "/path/to/local-evocode-config.yaml" \
  --max-total-tokens 100000000

# 启动或断点续跑 54 主任务 + 8 候选门禁审计
uv run --project .. python scripts/run_formal.py --execute \
  --config "/path/to/local-evocode-config.yaml" \
  --batch-id formal_run --max-total-tokens 100000000
```

---

## 4. 目录结构说明

| 目录 | 说明 |
| :--- | :--- |
| `configs/` | 评测调度契约、阶段门禁映射与运行配置模板 |
| `scenarios/` | 三仓库公开任务描述、初始代码模板与公开测试 |
| `private/acceptance/` | 主任务独立隐藏验收测试（不对 Agent 容器挂载） |
| `private/regression/` | 固定历史回归重放题与测试 |
| `private/references/` | 各阶段离线参考修复补丁 |
| `private/gate_selection/` | `Task Skill` 与 `Meta-Skill` 沙箱门禁评测题 |
| `private/gate_audit/` | `NSRR` 独立门禁审计题与外部参考标签 |
| `git_stage_packages/` | 按阶段剥离未来提交的只读 Git 历史包 |
| `seeds/` | 公共历史轨迹与各组等价初始状态种子 |
| `gate_cases/` | 8 个独立审计候选技能定义与元信息 |
| `runner/`、`scripts/`、`tests/` | 阶段屏障并发调度器、命令行入口与测试套件 |
| `reports/` | 离线题集准入校验报告与汇总指标报表 |
| `results/`、`.runtime/` | 本地运行账本与临时容器工作区（默认 Git 忽略） |
