# 日报与周训练迁移：实现及验收边界

已实现并合成测试存储、编排和日志隔离代码。**尚未创建 App、配置凭据/Environment，也未运行真实私有数据端到端验收。** 公开工作流仍只有代码 CI 和手动合成验证；没有新增 cron，没有停用旧任务，没有部署本机程序。绿色合成测试不代表真实日报、训练或收益验证完成。

## 已实现的链路

| 环节 | 实现与证据边界 |
| --- | --- |
| 私有目标 | `ASHARE_STORAGE_REPO` 明确指定原私库；初始化及写前核验仓库身份与私有状态。Actions 仅读专用 `ASHARE_STORAGE_TOKEN`，不回退公开仓令牌或本机登录 |
| 报告/模型 | 私有 `data` 分支不可变文件与哈希清单；写入后按实际 commit 回读内容、长度、哈希和 schema；同名变化拒绝覆盖，重复仍需校验 |
| 检查点 | 确定性压缩包、内容哈希版本；先建私有草稿 Release，上传检查点和清单，下载验证后才发布；不删除或覆盖既有文件 |
| 输入恢复 | 优先新格式私有 Release，最近两版验证后恢复，降级显式记录。没有新格式时才读取原私有 bootstrap Release，先全包验证再导入。陈旧数据不能自动成为合格信号 |
| 重复与故障 | 上传/更新响应不确定先查实际状态；完整回读后写私有 `migration_runs` 完成回执。重复日/模式跳过，不作为新的验收。相同周模型可恢复后续存储，但标记 `reused_training`，不冒充重新训练 |
| 手动编排 | 真实函数连接恢复、日历、研究/训练、复盘、发布、检查点和完成回执；部分/空/陈旧覆盖、样本不足、回读失败都不会通过。过时盘前任务拒绝生成准时信号 |
| 日志隔离 | 独立主进程将 worker 的 Python、native 和后代进程 stdout/stderr 直接丢弃；公开只输出固定枚举状态和有上限的整数计数；不保留原始错误/详细日志 |
| 临时数据 | worker 使用新建临时目录、最小环境，不能指定用户数据目录；不传入 GitHub workflow command 文件或无关凭据。超时结束进程组并清理目录 |
| 本地读取 | 代码已能读取新 Release；`ASHARE_COMPUTE_REPO` 匿名读取公开任务状态，不发送私库 token。共享工作流状态注明未区分日报/训练；本机运行副本尚未更新 |

检查点哈希与日期验证证明传输完整性，不证明行情、复权、公司行动或模型经济含义正确。全市场有效覆盖仍由报告评估；当前股票池、历史数据及既有模型研究偏差仍在，本批不调策略、不启用模型、不修改模拟本金或风险参数。

## 权限与待操作时确认的配置

推荐 App 仅安装到 `MatokiJZ/ashare-observer`，首版权限缩减为 **Contents 读写、Metadata 只读**。新检查点和原 bootstrap 均使用 Release，无需 Actions 权限。[Release API 权限](https://docs.github.com/en/rest/releases/releases)

旧 Actions artifact 兼容仅在显式 `ASHARE_LEGACY_ARTIFACTS=1` 时开启，才需要 Actions 读取；默认封装不传此变量，不要求这项授权。Contents 写权限无法限制到单个分支或 Release，并包含删除能力；代码不调用删除不等于权限层面没有删除能力。

待单独确认的具体动作：

1. 创建仅用于该项目的 GitHub App，建议名 `MatokiJZ-ashare-private-storage`（创建时再查可用性）；主页指向公开代码仓库；关闭 webhook，无用户 OAuth/账户权限，无额外事件订阅。
2. 只安装到上述私有仓库，授予 Contents 读写、Metadata 只读，不选全部仓库，不授予 Workflows、Actions 或 Administration。
3. 在公开仓库创建 `private-storage` Environment，分支策略只允许 `main`，首次真实验收要求用户手动批准。核实实际服务端规则，不能用环境变量标记代替保护；不默默取消审批来启用定时。
4. 经确认后生成 App 私钥，直接保存为该 Environment 的 `ASHARE_STORAGE_APP_PRIVATE_KEY` Secret；Client ID 保存为 `ASHARE_STORAGE_APP_CLIENT_ID` 变量。不得通过聊天、源码或日志传递私钥。安装令牌只按运行生成并以 `ASHARE_STORAGE_TOKEN` 传给 worker，通常最长一小时并由 Action 结束时撤销。
5. 手动运行前另确认本次模式、代码版本、时段和 `ASHARE_PRIVATE_RUN_APPROVED=1`。当前未设置批准变量，也未安装真实 workflow。

[GitHub App 工作流认证](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/making-authenticated-api-requests-with-a-github-app-in-a-github-actions-workflow)说明内置 GITHUB_TOKEN 仅能访问运行仓库；[Environment 保护](https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments/manage-environments)用于限制凭据可用范围。短期替代是同仓库、同权限且建议 30 天有效的 fine-grained PAT，仍须单独确认；本实现不自动回退到 PAT。

## 真实入口与公开输出

`docs/templates/private-research.yml.example` 只是**不可执行模板**：位于 docs，Actions 引用为 `NOT_READY`，只手动触发、main-only、标准 Ubuntu、`contents: read`，没有 schedule、artifact 或 cache。不能直接复制使用。必须先核验/固定所有 Actions 的完整 SHA、依赖完整性和 Environment 保护，再单独批准安装及手动验收。

模板先安装公开依赖，再生成仅限私库的安装令牌；令牌只给固定入口步骤。PR/fork 测试从不接触私有环境。封装拒绝仓库、分支、事件、模式、批准标记或凭据不符合的情况；这些标记只是额外校验，不能声称实际环境保护已配置。原 `scripts/cloud_task.py` 继续在 Actions 中无条件拒绝。

公开输出不包含原始数据、模型、旧提交信息、个人姓名/邮箱、路径或异常全文。私有原始输出直接丢弃，诊断粒度因此仅剩安全阶段/错误分类；不存在可下载的详细日志。受信任 worker 及依赖本身仍能访问处理所需私有数据，所以代码审查、依赖审核和最小权限不可由日志封装替代。公开仓库归属账号仍可见，不能承诺匿名。

## 真实端到端验收仍待完成

分别手动验证日报与周训练，记录证据但只保存在私库：

- 实际私有输入来源、版本、哈希、非空文件数、数据截止日和交易日历；bootstrap 若过旧，明确失败或继续修复，不宣称已可用。
- 实际采集和报告/复盘，以及合格时间切分、拟合、验证、测试及模型文件；`partial`、`insufficient`、休市、复用旧模型或已有回执跳过，均不能当完整新验收。
- 实际私有报告/模型/检查点发布与下载回读；读取端使用新格式；检查公开分支、运行元数据和日志没有私人内容，artifact/cache 没有私有数据。
- 权限失败、超时、损坏检查点、过时数据和并发冲突有明确失败/恢复证据。输入损坏不导入；旧有效版本不删除。损坏或不确定状态不能被忽略。
- 记录实际耗时、包大小/增长、数据源可用性和私有 Release 容量。每包上限 100 MiB、解包上限 800 MiB/7000 文件；没有自动清理旧数据，长期保留和容量策略须在启用 cron 前确定。

此后才提出切换建议，另行确认启用计划触发、停用被替代旧任务和本机读取部署。代码迁移不等于数据服务迁移，完整训练不等于可盈利或可交易。供应商批量数据/新闻/财务的再分发许可仍未核实，所有相关数据保持私有。

## 计划时刻（未启用）

| 工作 | 北京时间 | UTC cron |
| --- | --- | --- |
| initial | 每日 07:35 | `35 23 * * *` |
| update | 每日 08:35 | `35 0 * * *` |
| fallback | 每日 09:05 | `5 1 * * *` |
| close | 每日 16:10 | `10 8 * * *` |
| train | 周六 08:00 | `0 0 * * 6` |

GitHub cron 可能延迟或丢弃任务，公开仓库长期无活动还可能停用；它不是准点服务，也不是连续盘中模拟。[调度规则](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows) 原三个私有工作流继续保持现状，实际运行仍可能消耗私有 Actions 额度；公开标准 runner 的免费计算不代表私有存储或外部调用费用为零。[计费边界](https://docs.github.com/en/billing/concepts/product-billing/github-actions)
