# 日报与周训练迁移：实现及验收边界

已实现存储、编排、日志隔离及独立验收命名空间，增加手动工作流入口。**尚未创建 App、配置私钥，也未运行真实私有数据端到端验收。** `private-storage` Environment 已设置只允许 `main`、要求账号持有人审核，并关闭管理员绕过。App 创建仍需账号持有人完成 GitHub 身份验证。没有新增 cron，没有停用旧任务，没有部署本机程序。绿色合成测试不代表真实日报、训练或收益验证完成。

## 已实现的链路

| 环节 | 实现与证据边界 |
| --- | --- |
| 私有目标 | `ASHARE_STORAGE_REPO` 明确指定原私库；初始化及写前核验仓库身份与私有状态。Actions 仅读专用 `ASHARE_STORAGE_TOKEN`，不回退公开仓令牌或本机登录 |
| 报告/模型 | 验收写入私有 `migration-acceptance` 独立根分支，生产 `data` 仅作为输入；写后按实际 commit 回读内容、长度、哈希和 schema；同名变化拒绝覆盖，重复仍需校验 |
| 检查点 | 验收使用 `ashare-acceptance-checkpoint-v1-` 独立标签；生产恢复忽略这些标签。先建私有草稿 Release，上传检查点和清单，下载验证后才发布；不删除或覆盖既有文件 |
| 输入恢复 | 优先新格式私有 Release，最近两版验证后恢复，降级显式记录。没有新格式时才读取原私有 bootstrap Release，先全包验证再导入。陈旧数据不能自动成为合格信号 |
| 重复与故障 | 上传/更新响应不确定先查实际状态；完整回读后写私有 `migration_runs` 完成回执。重复日/模式跳过，不作为新的验收。独立验收训练强制执行原固定配方，并要求当前时间戳的新模型，不能复用生产本周模型；重复完成回执仍不会再训练 |
| 手动编排 | 真实函数连接恢复、日历、研究/训练、复盘、发布、检查点和完成回执；部分/空/陈旧覆盖、样本不足、回读失败都不会通过。过时盘前任务拒绝生成准时信号 |
| 日志隔离 | 独立主进程将 worker 的 Python、native 和后代进程 stdout/stderr 直接丢弃；公开只输出固定枚举状态和有上限的整数计数；不保留原始错误/详细日志 |
| 临时数据 | worker 使用新建临时目录、最小环境，不能指定用户数据目录；不传入 GitHub workflow command 文件或无关凭据。超时结束进程组并清理目录 |
| 本地读取 | 代码已能读取新 Release；`ASHARE_COMPUTE_REPO` 匿名读取公开任务状态，不发送私库 token。共享工作流状态注明未区分日报/训练；本机运行副本尚未更新 |

检查点哈希与日期验证证明传输完整性，不证明行情、复权、公司行动或模型经济含义正确。全市场有效覆盖仍由报告评估；当前股票池、历史数据及既有模型研究偏差仍在，本批不调策略、不启用模型、不修改模拟本金或风险参数。

## 权限与配置状态

推荐 App 仅安装到 `MatokiJZ/ashare-observer`，首版权限缩减为 **Contents 读写、Metadata 只读**。新检查点和原 bootstrap 均使用 Release，无需 Actions 权限。[Release API 权限](https://docs.github.com/en/rest/releases/releases)

旧 Actions artifact 兼容仅在显式 `ASHARE_LEGACY_ARTIFACTS=1` 时开启，才需要 Actions 读取；默认封装不传此变量，不要求这项授权。Contents 写权限无法限制到单个分支或 Release，并包含删除能力；代码不调用删除不等于权限层面没有删除能力。

账号持有人已批准以下具体配置。批准不等于操作完成，实际状态仍须核实：

1. 创建仅用于该项目的 GitHub App，建议名 `MatokiJZ-ashare-private-storage`（创建时再查可用性）；主页指向公开代码仓库；关闭 webhook，无用户 OAuth/账户权限，无额外事件订阅。
2. 只安装到上述私有仓库，授予 Contents 读写、Metadata 只读，不选全部仓库，不授予 Workflows、Actions 或 Administration。
3. 已创建 `private-storage` Environment，服务端分支策略只允许 `main`（不含 tag），要求 MatokiJZ 人工审核，管理员不能绕过。保留账号持有人审核自己触发任务的能力，助手不得代批；这与自动放行不同。环境变量不能代替保护，不默默取消审批来启用定时。
4. 经确认后生成 App 私钥，直接保存为该 Environment 的 `ASHARE_STORAGE_APP_PRIVATE_KEY` Secret；Client ID 保存为 `ASHARE_STORAGE_APP_CLIENT_ID` 变量。不得通过聊天、源码或日志传递私钥。安装令牌只按运行生成并以 `ASHARE_STORAGE_TOKEN` 传给 worker，通常最长一小时并由 Action 结束时撤销。
5. 手动验收入口固定可信代码提交，`ASHARE_ACCEPTANCE_COMMIT` 必须与 `GITHUB_SHA` 完全相同，并要求 `ASHARE_PRIVATE_RUN_APPROVED=1`。真实输入研究及训练已获范围授权，但每次环境审核仍由用户本人完成。当前尚未设置这些变量。

[GitHub App 工作流认证](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/making-authenticated-api-requests-with-a-github-app-in-a-github-actions-workflow)说明内置 GITHUB_TOKEN 仅能访问运行仓库；[Environment 保护](https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments/manage-environments)用于限制凭据可用范围。短期替代是同仓库、同权限且建议 30 天有效的 fine-grained PAT，仍须单独确认；本实现不自动回退到 PAT。

## 真实入口与公开输出

`.github/workflows/private-research.yml` 只支持手动触发，默认 `validate_only=true`：仅安装公开依赖并运行合成测试，不读取私有环境或生成 App token。只有显式选 `false` 才进入需要用户审核的真实任务；仓库、`main`、触发人和再次运行人都固定校验。工作流没有 schedule、artifact 或 cache，使用标准 Ubuntu 和 `contents: read`。

Actions 固定为已核实的完整提交 SHA；Linux Python 3.12 依赖固定版本和 SHA256，单独 venv 安装后核对清单，全部发生在生成私库令牌之前。哈希验证不能证明第三方代码安全；`jsonpath` 的已锁定源码包仍需构建，构建依赖也锁定，关闭隔离构建和自动依赖补装。

真实步骤要求审核后的完整提交匹配环境变量，令牌只交给固定入口。PR/fork 测试不接触私有环境。入口再次校验身份、分支、事件、模式、批准标记、提交和隔离写入目标；缺失时拒绝执行。原 `scripts/cloud_task.py` 继续在 Actions 中无条件拒绝。

生产输入报告和模型固定读取同一 `data` commit，检查点另记 Release/tag/hash；这两个来源并非原子快照，仍须检查日期与内容资格。验收报告、模型和完成回执只写 `migration-acceptance`，首次提交不继承生产历史；生产同名回执不能使验收跳过。验收 Release 不作为生产同步的默认输入。要将验收结果用于日常读取，需要另行确认切换方案；本批不自动迁移。

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
