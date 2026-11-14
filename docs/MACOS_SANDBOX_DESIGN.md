# macOS 执行边界：阶段四候选

这是 Issue #45 第四阶段的实现与验收记录，按时间保留各轮结果。当前候选采用
macOS 原生低权限任务身份和独立特权辅助服务；虚拟机仅用于测试。
操作员显式配置项目授权和固定提交后，已验证的工作树、线性发布与 Dashboard
单次审批路径可以运行。默认生产开关仍关闭，缺少授权或快照时明确拒绝。
阶段二的安全读取不承担此项验收。

截至 2026-11-13，干净本地快照的真实受管初装、公开 Dashboard 审批与重复发布、
退出后的运行锁释放及再次启动均通过。辅助服务由管理员从独立解包并校验的发行包管理；用户受管
安装目录不提供 root 入口。[打包及复验步骤](../packaging/README.md#macos-stage-four-managed-install-acceptance)。
后文早期的未完成描述为当时状态，后续通过记录只覆盖各自注明的包和环境。
本地同协议 App/helper 跨版本升级、回滚和启动失败恢复已通过，详情见文末；
正式发行信任、任意协议版本兼容、对齐接纳基线后的多平台 CI 仍未验收。

## 原生路线：后台进程实验

阶段四优先评估直接在宿主 macOS 上执行，虚拟化实验暂留备选。用宿主解释器运行 `scripts/probe_macos_sandbox.py --native-only --output <临时报告路径>` 可以跳过 VM 分配探针。该检查仍使用 Seatbelt 作为实验工具，生产能力保持关闭。

- macOS 26.5.1 ARM64 实测：允许创建子进程时，子进程用 `setsid` 离开原进程组，原组被强制终止后，它仍能写入存活标记。系统 `launchd.plist(5)` 说明的默认清理也以进程组为范围。
- 同一环境下，撤去 `process-fork` 权限后，`fork`、`posix_spawn` 和创建新会话的 `posix_spawn` 都返回 `EPERM`。允许派生的对照组能执行相同操作，限制在 `exec` 替换程序后仍有效。这验证了阻止派生的候选边界，依赖子进程的构建和测试任务仍需其他机制。
- 允许 `fork`、仅禁止直接调用 `setsid` 和 `setpgid` 时，`posix_spawn` 仍能通过启动属性创建新会话或进程组。再禁止整个 `posix_spawn` 调用后，`fork` 加 `exec` 仍可执行，实测后台子进程留在原组并随原组被终止。普通 `posix_spawn` 也同时被拦，本机 Homebrew Python 启动器就因此启动失败，换成解释器本体才完成实验。该方案尚无通用工具兼容性或完整回收证据。
- 本机额外验证了 `kqueue`。自动跟踪子进程的 `NOTE_TRACK` 注册返回 `ENOTSUP`，普通 `NOTE_FORK` 只通知父进程发生派生，未返回新进程的身份。扫描 PID/PPID 也会遇到中间进程先退出、子进程已被重新收养的情况。两者都不能作为可靠的整树回收机制。

通用命令仍需要独立于 PID、父进程和进程组的任务归属。候选机制须先验证后台进程能否脱离该归属，再验证取消、超时、监督器崩溃后的完整回收及资源上限。当前的单进程实验不覆盖整树资源、受保护 Git 或最终发行包验收。

### 常用工具与实际监督器

在 macOS 26.5.1 ARM64 上，现有 `spawn_managed` 允许 Node、npm、Bun、Python 和 Git 派生普通子进程。这组实验只测启动和生命周期。通过 `scripts/probe_macos_scope_cleanup.py <本地 Node 可执行文件>` 可复现普通和脱离进程组的后台任务在四种退出场景下的结果：

| 场景 | 同组子进程继续运行 | 脱离进程组的子进程继续运行 |
| --- | --- | --- |
| 显式取消 | 否 | 是 |
| 实际输出收集超时 | 否 | 是 |
| 调用方被 SIGKILL | 否 | 是 |
| 监督器本身被 SIGKILL | 是 | 是 |

取消和超时明确报告清理未完成。调用方崩溃时，监督器仍可通过控制连接关闭清理原组。监督器被强杀时，调用方报告缺少清理结果。探针只运行限时进程，结果基于终止请求之后的存活标记。

额外的临时用户 `launchd` 任务允许 Node 正常派生，`SessionCreate` 也确实分配了独立的审计会话，脱离进程组的子进程继承了该会话。但 `bootout` 后该子进程仍继续运行，普通用户跨进程查询审计会话返回权限错误，且该会话没有对应的 `launchd` 登录域。因此该会话目前只能作为身份线索。

下一候选是独立的低权限任务用户身份，由独立于任务进程的受信任辅助程序管理。须验证身份分配与隔离、命令在该身份下使用工具链、跨会话后仍保有身份、回收期间阻止新进程产生、权限和进程竞态，以及恢复后才允许重用身份。该候选需要管理员权限进行原型验证，本轮没有创建系统用户或安装辅助程序。

### 独立低权限身份原型（2026-11-08）

`scripts/probe_macos_task_uid.py` 是需要操作者主动运行的固定测试原型。它不接收任意命令，不启用生产 `macos_execution`，不创建系统账户、不安装 daemon、不修改 Gitgo 安装。`--preflight` 只查询工具位置和进程列表；`--run` 需要管理员权限，缺少权限即退出。

```sh
/usr/bin/python3 -I scripts/probe_macos_task_uid.py --preflight
sudo /usr/bin/python3 -I scripts/probe_macos_task_uid.py --run
```

原型的任务归属和验收方式：

- 为工具测试与每组生命周期测试各分配一个未登记、当前进程列表未使用的数字 UID/GID。清除附加组，以该身份执行固定工具；工作区和 HOME 使用 `/private/tmp` 下的独立目录。UID 在同次实验中不分配给其他场景。跨运行通过 root 拥有的 `/private/tmp/gitgo-task-uid-probe-v1.lock` 串行化，但这不是正式的系统身份分配服务。
- Node、npm、Bun、Python、Git 各执行本地固定任务并派生子进程。npm 只运行本地脚本，Git 在私有临时仓库内执行提交 hook，不安装依赖、不访问远端、不读取用户的 Git 配置。
- 固定监督进程启动同组子进程、独立会话子进程、双重 fork 后被收养的后台进程。开始故障注入前，检查三者的实际 UID、真实 UID、保存 UID 以及进程组。另检查无法恢复 root、无法读取 root 私有测试文件。
- 测试正常结束、取消、真实等待超时、监督进程被 SIGKILL，以及独立清理器被 SIGKILL 后由外部观察者恢复清理。另一个 UID 的对照进程须持续存活。
- root 观察者只向仍持有父子关系的直接子进程注入故障。按 UID 扫描后，由先降权到对应身份的短命辅助进程发送信号；不以 root 身份向扫描得来的 PID 或 PGID 发信号，不使用 `kill(-1)`。这利用内核投递信号时再次执行的身份检查，避免跨身份 PID 重用误杀。参见 [Apple XNU 的 cansignal/kill 实现](https://github.com/apple-oss-distributions/xnu/blob/main/bsd/kern/kern_sig.c)。
- 清理至多等待五秒，要求连续三次观察不到该 UID 的进程，僵尸也计入剩余项。进程查询失败、信号辅助进程失败、剩余进程和启动失败均不能报告通过。所有已分配身份还会进行最终回收检查。固定后台任务自身有 25 秒生存上限，验收必须在其自然退出前完成。

实验会留下少量 root 私有临时产物和锁文件，位置写入 stderr；最终目录收紧为 `0700`，不以 root 递归删除任务可写目录。JSON 报告输出到 stdout，可由普通用户的 shell 重定向保存。脚本不需要也不读取管理员密码，密码由系统 `sudo` 处理。

**证据状态**：本机只读预检通过；当前登录用户下五种工具的固定脚本、三类子进程拓扑和原型拒绝条件已验证。首次实测修正后，系统 Python 3.9 的独立测试为 13 项通过。修正前使用仓库 Python 环境，在宿主权限下运行本探针测试及 `test_unix_process_scope.py`、`test_macos_forkless_scope.py` 得到 32 passed、6 skipped、9 subtests passed；跳过项为 Linux 专属机制和未提供新 frozen Host 的测试。执行沙箱内曾因 `sandbox-exec: sandbox_apply: Operation not permitted` 导致旧 forkless 测试失败，宿主运行已排除该限制。当前用户测试本身不能计作低权限验收；操作者授权后的第二次实测结果见下表。

**尚未覆盖**：数字身份没有账户名，依赖 `getpwuid`、钥匙串、图形会话或用户目录的工具可能不兼容；需要由真实实验报告结果。这里的文件权限仅隔离私有测试目录，不限制全部世界可读/可写宿主路径，也没有网络隔离、setuid 程序限制或完整资源边界。有限次数进程扫描不能证明恶意持续派生期间的原子封锁。外部观察者恢复清理不等于持久服务自动恢复，整个 harness 被强杀后只依靠固定测试任务的生存上限退出。正式身份租约、禁止身份逃逸/跨任务通信、阻止清理时继续派生、崩溃恢复和打包验收仍需独立完成。

**首次管理员实测与修正**：操作者第一次执行 `--run` 失败，清理助手在 `os.getgroups()` 处返回 `EINVAL`。Apple 系统 Python 链接 `getgroups$DARWIN_EXTSN`，该扩展先用 `getpwuid` 查询账户，未登记数字 UID 会失败；参见 [Apple Libc 实现](https://github.com/apple-oss-distributions/Libc/blob/main/sys/getgroups.c)。原型现改用 libSystem 的非扩展 `getgroups` 符号查询内核凭据，并保留查询失败即拒绝的条件。没有将异常当成空组列表。新增账户查询无关性、内核查询失败拒绝和部分工具结果保留的回归检查。改进了失败时的直接子进程收尾及完整报告输出。首次实验退出后，额外进程查询确认实验 UID 范围无剩余进程；原 JSON 留在私有开发产物 `report.first-failed.json`。

**第二次管理员实测（2026-11-08）**：操作者在 Mac 终端执行修正后的 `--run`，报告 `passed=true`。五种工具的固定子进程任务全部返回 0，Git 初始化和提交 hook 通过。以下每种生命周期场景都先确认同组、`setsid` 和双重 fork 三类后台进程已运行，再注入结束或故障条件。

| 场景 | 结果 | 首次确认身份下无进程耗时 |
| --- | --- | --- |
| 正常结束 | 无剩余进程 | 0.260 秒 |
| 显式取消 | 无剩余进程 | 0.261 秒 |
| 实际等待超时 | 确认超时发生，随后无剩余进程 | 0.297 秒 |
| 监督进程被 SIGKILL | 退出码 -9，随后无剩余进程 | 0.260 秒 |
| 清理器被 SIGKILL | 外部观察者接管后无剩余进程 | 0.274 秒 |

这些耗时包含连续空列表确认的等待，是本次有限样本观测值，不是延迟保证。不同 UID 的对照进程始终存活；最终七个实验身份全部观察为空，之后单独执行的进程查询也未发现实验 UID 范围内有进程。后台测试进程的自然生存上限为 25 秒，本次清理在其之前完成。

原报告保存在忽略目录 `dev-artifacts/macos-task-uid/report.second-passed.json`，SHA-256 为 `bb3085ec2542891b4ed203674ce9012f5e43bcde36bb9bc9a6a7594c0c49191a`。该结果支持独立身份原型的固定工具兼容性和有限后台任务回收；仍未覆盖恶意持续派生、整个受信任服务崩溃后的自动恢复、资源/网络完整隔离或真实发行包。`production_backend_enabled=false` 保持不变，阶段四整体尚未完成。

### 持续派生与 launchd 自动恢复原型

`scripts/probe_macos_uid_recovery.py` 在上述 UID 实验之上增加持续派生和真实服务重启。仍是固定负载开发探针，不是生产执行后端。

```sh
# 普通登录用户：只测试 launchd 在 SIGKILL 后自动重启程序，不测试独立 UID。
/usr/bin/python3 -I scripts/probe_macos_uid_recovery.py --launchd-smoke
# 管理员实测：七种场景，报告输出 stdout，进度输出 stderr。
sudo /usr/bin/python3 -I scripts/probe_macos_uid_recovery.py --run
```

每个场景在 `/private/tmp/gitgo-uid-recovery-*` 中保留 root 私有控制目录和代码快照。通过 `launchctl bootstrap system` 临时注册独立作业，结束后 `bootout` 卸载，不写入 `/Library/LaunchDaemons`、不创建账户、不修改 Gitgo 安装。作业以 root 监督，固定测试负载及信号助手仍先降到专用 UID。`launchd` 在这里负责重启监督程序，后台进程回收仍由 UID 机制完成，不把 `bootout` 清理原进程组误作整树回收。

临时作业使用 `KeepAlive.SuccessfulExit=false`，成功清理后退出 0；异常退出或被 SIGKILL 后由系统重新启动。该语义参考 [Apple launchd.plist](https://github.com/apple-oss-distributions/launchd/blob/main/man/launchd.plist.5)。已在本机通过普通用户作业实测 SIGKILL 后出现新 PID，随后确认作业已卸载，证据位于忽略目录 `dev-artifacts/macos-task-uid/launchd-smoke.json`。这项证据不等于 system 域专用 UID 的完整恢复验收。

持续派生负载由两个双重 fork 后的后台生产者组成，每个最多保有八个短命子进程，子进程进入独立会话；没有指数级派生。整组共享 25 秒截止时间，服务就绪及故障注入前分别检查出生记录有增长，同时从内核进程列表核对实际身份。单凭静态进程树或自然到期不能通过测试。

清理先以任务 UID 发送 SIGSTOP，重复扫描新出现的进程，观察身份下的集合稳定且均已暂停或退出后，再发送 SIGKILL。即使暂停/查询失败，也尝试强杀回收并保留错误。确认三次无进程才报告清空；暂停阶段至多两秒，整个清理循环至多六秒（单次系统调用另有上限）。这只是有界观测，不是内核原子阻止 fork 的机制。被冻结的进程无法依靠自身计时器退出，因此清理失败必须继续保留恢复责任。

自动恢复规则：

- root 控制记录在启动任务前先原子写入并同步 `starting` 状态及 `launch_count=1`。任何已开始阶段的服务重启都只清理，绝不重放命令；记录写入后、exec 之前崩溃也不重跑。
- 状态及程序快照不在任务可写目录中。控制记录须为 root 拥有的私有普通文件；拒绝链接、异常大小、错误格式和重复启动计数。单个服务的文件锁避免两个恢复者同时清理同一身份。
- 记录绑定本次 macOS 启动标识，跨启动记录不能触发任务或 UID 信号。同次启动留下的本探针记录中的 UID 会被后续本探针分配排除，包括观察程序崩溃、服务尚未派生任务的窗口。这仍不是正式的全系统身份租约分配器。
- 普通取消、超时、客户端控制连接 EOF 和任务监督进程退出均进入清理。清理器在运行或暂停任务后被强杀时，由 `launchd` 自动重新启动并处理遗留身份。故障注入计数先写入，允许连续两次 SIGKILL 而不形成无限故障循环。
- 采集程序只负责注入故障和读取证据。必须在采集程序进行应急收尾之前观察到服务自行清空；如果需要采集程序接管清理，自动恢复验收直接失败。报告同时检查不同身份的对照进程存活、服务实际代数和 PID、父进程为 launchd、任务只启动一次，以及卸载结果。

| 场景 | 预期服务代数 | 预期处理 |
| --- | --- | --- |
| 持续派生时取消 | 1 | 暂停并清理 |
| 持续派生时超时 | 1 | 自动进入清理 |
| 客户端被 SIGKILL | 1 | 控制连接 EOF 触发清理 |
| 任务监督进程被 SIGKILL | 1 | 后台生产者仍在时回收整个 UID |
| 清理器运行时被 SIGKILL | 2 | launchd 重启后只清理 |
| 暂停任务后清理器被 SIGKILL | 2 | 恢复后回收已暂停进程 |
| 清理器连续两次被 SIGKILL | 3 | 继续恢复，任务不重跑 |

管理员实测已由操作者完成。首次报告的清理和恢复行为通过复核，但跨进程计时存在下述缺陷；修正后第二次实测的七种场景同时通过行为与计时门槛，详见后文。实测前系统 Python 3.9 的恢复测试为 16 项通过；仓库 Python 环境执行两个 UID 探针测试以及 Unix/forkless 进程回归为 51 passed、6 skipped、25 subtests passed，跳过项仍是 Linux 专属机制和没有提供新 frozen Host 的用例。若应急清理也未完成，报告给出针对该 root 私有目录的 `--recover` 命令；它只能清理，不会启动 prepared 任务。

**首次系统域实测复核（2026-11-08）**：每种场景在故障注入前都有新增进程出生记录，最后均观察到身份集合暂停并清空。清理器运行时崩溃、暂停后崩溃分别出现两代服务，连续两次崩溃出现三代服务，PID 各异、父进程均为 1；七种场景的任务启动次数均为 1。每次都是服务先完成清理，采集程序未接管清理；不同身份的对照进程存活。临时 system 作业均卸载，额外进程/作业查询确认无残留。取消与超时场景在开始清理后仍分别增加两个出生记录，第二轮暂停扫描覆盖这些新进程。

最终一代服务内部测得的清理耗时依次为：取消 0.871 秒、超时 0.803 秒、客户端崩溃 0.851 秒、任务监督进程崩溃 0.919 秒、清理器运行时崩溃 0.821 秒、暂停后崩溃 0.618 秒、连续两次崩溃 0.702 秒。这些是同一进程内的清理耗时，不包含服务重新启动的等待，不是性能保证。

**计时缺陷与修正**：报告中重启后的 `incarnations.time` 回到接近零，说明系统 Python 3.9 的 `time.monotonic()` 起点并不跨进程共享。Python 文档明确 [macOS 从 Python 3.10 才改为所有进程共享此时钟](https://docs.python.org/3/library/time.html#time.monotonic)。旧的 `finished-started<20` 比较跨服务代数，不能作为总耗时或自然到期排除门槛。本轮行为证据独立保留，跨进程耗时验收标记为不接受。

已将持久化时间戳及工作负载出生时间改为 `clock_gettime_ns(CLOCK_MONOTONIC_RAW)`，新记录使用 schema 2 和显式时钟标识，并要求 `0 <= elapsed < 20`。旧 schema 1 记录仍用于身份保留和恢复清理，但不能重新启动任务，也不能通过新计时门槛。补充跨真实 exec 的系统 Python 时钟一致性、旧记录只清理、计时回退/过期/非法值拒绝测试；系统 Python 的恢复测试现为 19 项通过。修正后的整套管理员计时报告已重新采集并独立复核，结果如下。

原始报告保留为 `dev-artifacts/macos-task-uid/recovery-report.first-run.json`，SHA-256 为 `a3487b0096c0cee5fa5fd129417153fb06115c793e4e07e162bc062645971a00`；独立复核结果在同目录 `recovery-report.first-run-review.json`，其中分别记录 `behavior_evidence_verified=true` 与 `cross_process_elapsed_gate_accepted=false`。

**修正后第二次管理员实测（2026-11-08）**：七种场景全部通过。记录均为 schema 2、`CLOCK_MONOTONIC_RAW`，服务重启时间递增；任务从启动到清理完成的总耗时均在 `[0, 20)` 秒内，早于固定负载的 25 秒自然截止时间。

| 场景 | 任务启动至清理完成（含运行和服务重启等待） | 最后一代服务清理耗时 | 服务代数 |
| --- | --- | --- | --- |
| 取消 | 1.327 秒 | 0.711 秒 | 1 |
| 超时 | 2.416 秒 | 0.871 秒 | 1 |
| 客户端崩溃 | 1.477 秒 | 0.845 秒 | 1 |
| 任务监督进程崩溃 | 1.552 秒 | 0.919 秒 | 1 |
| 清理器运行时崩溃 | 2.587 秒 | 0.877 秒 | 2 |
| 暂停后清理器崩溃 | 1.693 秒 | 0.656 秒 | 2 |
| 连续两次清理器崩溃 | 3.492 秒 | 0.681 秒 | 3 |

上述数据是本次有限实验的观测值，不是性能保证。每项均有故障前持续派生证据、服务观察到暂停并清空、无清理错误、任务只启动一次；重启服务的 PID 各异且父进程为 1。取消场景的出生记录在清理开始后由 12 增至 14，超时场景由 31 增至 33，新增进程也被回收。采集程序没有接管应急清理，对照身份进程始终存活。七个临时作业均卸载；随后独立执行宿主 `ps` 和 `launchctl print system`，确认实验 UID 范围无剩余进程、无本探针系统作业。

第二次原报告保存在忽略目录 `dev-artifacts/macos-task-uid/recovery-report.second-passed.json`，SHA-256 为 `7225798b7ec9f097af6607d11058073c2917fa729dc226b6b2f89969eb73c8d1`。逐项断言和宿主检查结果保存在同目录 `recovery-report.second-run-review.json`，`behavior_evidence_verified` 与 `cross_process_elapsed_gate_accepted` 均为 `true`。本轮原型验收已完成，无须为同一组场景再次运行管理员命令；这不等于阶段四生产后端已完成。

自动恢复范围目前是本次启动期间的进程崩溃。整个采集程序被强杀时，临时服务可通过控制连接断开处理任务，但已完成的 launchd 作业可能留在系统域直到卸载或重启；本轮未做整机掉电恢复或永久服务安装验收。系统重启后临时作业不会重新注册。恶意持续派生的原子封锁、setuid/跨任务逃逸、总资源预算、网络/文件隔离以及接入现有授权和恢复记录仍未完成，生产能力继续关闭。

## 正式入口的受限接入候选（2026-11-08，六项管理员联合测试通过）

本轮把生命周期机制从脚本移入 `backend/core/macos_identity.py`，增加 `sandbox_macos.py` 和独立的 `macos_execution_service.py`。正式 `ProcessToolRunner → sandbox_popen` 已有 Darwin 分支，但默认仍拒绝执行。仅显式 root 测试程序可用进程内 `execution_trial` 开启一个临时空工作区的一次调用；没有环境变量/工具 JSON 开关，不要求把 Dashboard 当 root 启动。这是接入候选，不是已经交付的普通用户 broker。

- 沿用现有 native invocation journal：启动前提交调用意图，传递调用编号，不修改已授权的 `SandboxPolicy` 快照。服务回执必须匹配 UID、启动标识和调用编号；退出码未知保留失败，清理未知保留恢复债务。业务效果不会因清理成功而自动变为已验证。
- 身份分配与旧原型共用 root 私有全局锁，新租约存在 `/private/tmp/gitgo-macos-execution-v1`。在改变临时工作区归属前同步租约。已用 UID 暂不回收复用；旧探针保留的 UID 也排除。发现未结清或损坏租约即阻止下次试验，不凭超时或 PID 不存在释放归属。
- 每次调用生成 root 私有服务快照和命令记录，通过临时 system launchd 作业启动。只传递三条 stdin/stdout/stderr 管道，不传继承的 Host 文件/安装锁。root 私有控制 socket 只接受启动和取消，不提供一般提权命令接口。低 UID 和空附加组在执行 `sandbox-exec` 前设置；清理信号辅助程序也先降到任务 UID，root 不向扫描得到的 PID 发信号。
- 服务在 exec 前同步 `starting/launch_count=1`。第一代之外即使看到 prepared 也只清理；异常退出由 launchd 重启，清理错误保持 `cleanup_pending`。正常退出、取消、超时和控制连接断开都清理整个 UID。确认清空后 Host 才卸载作业。捕获启动异常不会卸载尚可能承担回收责任的服务。
- Seatbelt 候选默认拒绝；允许工作区内容读写、明确运行时代码只读、正常 fork/exec，未开放网络/Mach。Homebrew 依赖只允许已加载 Host 映像的具体文件，不开放整个 Homebrew 或用户目录。目录元数据为解释器查找而可见。外部状态库不属于工作区或运行时读白名单。
- 测试使用独立 root 拥有的临时运行副本，包括现有源码和约 156 MiB 的本地 venv，不下载依赖，也不修改用户主目录的 750 权限、不改现有项目/安装、不写永久 LaunchDaemon。测试目录与恢复记录保留，不能用删除记录代替清理。

普通登录 UID 实测已通过：工作区读写与 shell 子进程可用，外部文件读写、软链接越界、硬链接、回环 TCP 连接和直接 `setuid(0)` 被拒绝；外部文件与监听器有隔离外正向对照。报告在 `dev-artifacts/macos-task-uid/execution-chain-isolation.json`，明确 `dedicated_uid_tested=false`。正式 tool-runner 在同一策略下执行 `/usr/bin/printf native-chain-ok` 成功；嵌套 shell 命令仍被现有授权规则拒绝，未为试验放宽。

回归：执行授权、候选契约、旧 UID/恢复原型、Unix/forkless 监督器共 111 passed、6 skipped、30 subtests passed。随后补充新核心清理器的派生竞态和查询失败检查，候选测试单独 21 passed。跳过项不计作支持证据。Rust ABI 已从本工作树锁定依赖构建，未复用其他分支二进制。

已执行的操作者命令（在工作树根目录）：

```sh
sudo ../issue45-tools/macos-build-venv/bin/python -B -I scripts/check_macos_execution_chain.py --run > dev-artifacts/macos-task-uid/execution-chain-report.json
```

该脚本验证六项：专用 UID 加 Seatbelt 的隔离、持续派生期间取消、超时、控制连接断开、清理器 SIGKILL 后自动恢复，以及真实 `ProcessToolRunner`、管道和 journal 的成功路径。持续派生负载有 15 秒上限，必须在 14 秒内完成清理，失败即停止后续用例并保留诊断。控制连接断开不等于整台 Host 被 SIGKILL；永久服务、真实 Host 崩溃及完整打包仍需进一步验收。工具会话的 `sudo -n` 返回需要密码；操作者随后在终端执行了这套测试，六项联合测试结果已复核，见下文。

**管理员联合测试复核**：`execution-chain-report.json` 六项均通过，逐项检查调用编号、UID、启动次数、服务代数、退出原因、清理证据及共享时钟后确认结果一致。

| 场景 | 任务启动至清理完成 | 清理耗时 | 复核结果 |
| --- | --- | --- | --- |
| 独立 UID + Seatbelt 隔离 | 0.414 秒 | 0.307 秒 | 工作区写入和子进程可用；外部读写、链接越界、回环网络和直接切换 root 被拒绝 |
| 持续派生时取消 | 1.404 秒 | 0.926 秒 | 取消触发，清空 UID |
| 持续派生时超时 | 2.838 秒 | 0.758 秒 | 2 秒期限触发，清空 UID |
| 控制连接断开 | 1.338 秒 | 0.861 秒 | owner_eof 触发，清空 UID |
| 清理器被 SIGKILL | 2.218 秒 | 0.691 秒 | 出现第二代服务，自动清理，任务仍只启动一次 |
| 正式 ProcessToolRunner | 0.928 秒 | 0.355 秒 | 命令输出 native-chain-ok，退出码 0，Host 回执与租约匹配，evidence_state=committed |

四项后台生命周期测试的出生计数在注入前均由至少 6 增至 8 或 9，清理时确认暂停状态；总耗时小于 14 秒，早于负载 15 秒自然结束。六个不同 UID 和调用编号均为 `launch_count=1`、最终 `clean`、剩余进程为空且清理无错误。之后独立查询宿主进程表及 system launchd 域，未发现实验 UID 进程或临时测试服务。这些耗时仅代表本次实验，不是性能保证。

报告内 `tool_runner.runner.data.cleanup.complete=false` 是嵌套旧监督器的 `owned-session-only` 结果，其不能证明脱离进程组的后代已回收；不要把它改写成成功。外层 Host 的 `runner.cleanup` 则来自独立 UID 服务，`mode=macos_uid`、`complete=true`，并与租约 UID 和 invocation_id 一致。现有 Pipeline 代码采用 Host 的 `result.cleanup` 作为运行时清理事实，而非子进程 JSON 中的同名字段。本轮实测到 `ProcessToolRunner` 和 journal 的成功路径，不等于完整 ToolPipeline 授权/恢复、普通用户 broker 或 Host 强杀测试已覆盖。

原始报告另存为忽略目录 `dev-artifacts/macos-task-uid/execution-chain-report.first-passed.json`，SHA-256 为 `797dcf1f1cdc9df431f95859c779989d1c5cbe1cf872a5ba0375b6285a05bc23`。逐项复核和宿主无残留检查记录于同目录 `execution-chain-report.first-review.json`。这轮无需重复相同管理员命令；后续工作仍须完成以下生产边界。

仍未解决：普通用户与特权 broker 的认证/安装、真实工作区访问授权与租约回收、setuid 可执行文件和跨任务逃逸、并发调用、总资源限制、恶意无限派生的原子封锁及发行包。候选资源限制沿用固定实验的每进程 CPU/文件/进程数限制，并未落实完整 `SandboxPolicy.memory_bytes/process_limit` 的总量契约。不能删除测试门槛后直接上线；生产 `macos_execution=false` 保持不变。

## 普通用户 broker 与项目授权候选（六场景阶段验收通过）

在上述 root-only 执行链之上，新增 `macos_broker_protocol.py`、`macos_broker_client.py`、`macos_broker_service.py`。普通 Host 由操作者显式绑定临时 `broker_session`；`WorkspaceCandidateRuntime` 和 `ProcessToolRunner` 使用这条通道。没有模型参数或环境变量可开启受管权限，也没有把 Dashboard 改成 root 运行。生产能力 `macos_execution=false` 保持不变。

- 连接身份由 Darwin `getpeereid` 的内核结果取得；服务核验登录 UID，客户端核验 root 对端。root 私有授权固定 UID、目录设备号/inode/属主、授权编号、启动标识、有效期及随机能力凭证摘要。凭证文件只对该登录用户可读，socket 的父目录由 root 拥有且用户不可替换。这证明 OS 用户身份，不证明客户端的签名或应用身份；同一登录用户的其他进程属于该授权的信任范围。
- 用户通过 SCM_RIGHTS 传递已经打开的项目目录描述符。服务核对该对象与操作者选定目录，并在执行期间检查目录替换、授权撤销和过期。根路径逐级 `O_NOFOLLOW` 打开。请求不能指定 root 命令、运行环境、Seatbelt 策略或修改目录权限。
- 沿用现有执行副本和文件服务：普通 Host 读取真实项目，发送有界文件快照；root 仅在自己创建的空临时工作区落盘，再以独立 UID 执行固定 tool-runner。真实项目的 UID/GID、ACL 和权限不交给辅助服务修改。当前只支持 `exec_command`，最多 128 文件、4 MiB 内容和 60 秒原生调用期限；超界拒绝，不静默截断。
- 清理确认后，固定导出程序再次降到任务 UID 读取产物，拒绝软链接、FIFO、硬链接、设备及受保护元数据。root 不沿工具创建的路径读取文件。Host 仅替换私有执行副本，再通过原有 `FileMutationBatch` 检查/提交；任务输出不能制造文件提交或清理事实。项目授权身份进入候选绑定，执行完成及现有文件提交守卫重查目录对象。沿用文件服务的合作写入模型；未声称普通用户同 UID 外部写入者的所有竞态已经原子封锁。
- 帧长、内容、字段和文件模式有界，重复 JSON 字段拒绝；FD 被标记为不继承，错误路径关闭收到的描述符。调用编号在进入可能启动的窗口前持久化，重放拒绝。窗口之后连接丢失属于未知结果，不能伪装成未启动。取消、断线、超时及撤销转到现有 UID 清理；broker 被杀后，独立 launchd 清理器仍持有身份租约。

本地验证：新增 broker 测试与候选工作区、macOS 执行候选相关测试 **77 passed、3 skipped**。包含真实内核 peer UID/目录 FD 传输、错误能力凭证和目录对象、授权目录被替换、路径穿越、特殊文件导出拒绝，以及模拟 broker 对端下的完整 Host Pipeline/FileService 写回与拒绝路径。模拟对端测试明确不算特权隔离实测。其他现有授权/UID/Unix 监督器回归通过；原工具沙箱阻止 `sandbox_apply` 的 10 项在宿主重新验证，forkless 文件最终 **13 passed、2 skipped**，没有放宽产品隔离策略。

新增 `scripts/check_macos_user_broker.py`，待操作者执行：

```sh
sudo ../issue45-tools/macos-build-venv/bin/python -B -I scripts/check_macos_user_broker.py --run > dev-artifacts/macos-task-uid/user-broker-report.json
```

脚本将后端及现有 venv 暂存到 root 拥有、任务可读取的临时目录；建立普通登录用户拥有的 700 项目夹和隔离的 Host 状态，临时启动 root broker，再真正降为该登录用户运行完整 ToolPipeline。它不读取用户现有业务项目、不修改安装或主目录权限。运行六条路径：正常执行和文件服务写回、持续派生时取消、超时、普通 Host 强杀、broker 强杀、授权撤销。正常路径还验证错 UID、错凭证、错目录 FD、目录替换、客户端自定策略及调用重放均被拒绝。所有异常路径要求 UID 清空早于 15 秒负载自然结束，并核查正式项目无部分产物；结束后卸载临时服务，未知清理则保留恢复服务与证据并停止后续测试。

本轮 `sudo -n true` 返回需要密码，**尚未执行这套管理员联合测试，不能声称六条路径已经通过**。临时 grant/服务的代码和模拟 Host 写回通过，不等于永久服务安装、签名身份、发行包、并发调用、整树资源预算或未知 journal 自动对账已经完成。原有生产门槛与未验证项目继续保留。

首次管理员联合测试已执行，但在第一项普通 Host 的目录身份核验处停止：对 root 拥有的 `0711` 测试上级目录逐级使用 `O_RDONLY`，错误地要求了列目录权限，返回 `EACCES`。wrong-OS-UID 拒绝通过；尚未产生 `active.json` 或启动任务，因此取消/超时/崩溃等后续项目未运行，不能记为通过。临时 broker 已卸载。原报告保存在 `user-broker-report.first-failed.json`，SHA-256 `2b792c75c8eb282304edc18a31b5bb3244cd084c7ca56a4a3c9ccae8b3f7c18f`。

已修正 `open_directory`：Darwin 祖先目录逐级使用 `O_SEARCH`，保持 `O_NOFOLLOW` 和目录对象固定；最终从已固定对象的 `.` 打开可传递普通目录 FD，避免 Darwin 搜索专用 FD 的 SCM_RIGHTS/F_SETFD 限制。没有放宽目录权限。普通用户在原失败的 root-owned 0711 目录中已实测核验和 FD 传递成功；新增 0111 祖先权限用例同时证明目录仍不可列举。相关测试 **78 passed、3 skipped**。修复后的完整管理员六场景测试仍待重跑。

第二次管理员测试完成六场景，原脚本报告 `passed=true`，人工复核仍判定**未完全通过**。普通 Host UID 501 调用、独立任务 UID、完整 Pipeline/FileService 写回两文件及内容哈希已核对；六种伪造/错目录/重放请求均拒绝。六个不同 UID 均只启动一次，最后 clean、空进程且无清理错误，临时服务卸载；另行查询宿主 ps/system launchd 也无残留。五项持续派生测试均在出生计数继续增长时注入故障，任务开始至清理分别约 1.529 秒（取消）、4.228 秒（超时）、1.645 秒（Host 强杀）、1.473 秒（broker 强杀）、1.532 秒（撤销），早于 15 秒自然结束。

复核发现超时虽然发生并最终清理，但旧策略 `(allow signal (target self))` 阻止内部监督器终止子进程，`PermissionError` 覆盖 `COMMAND_TIMEOUT`，错误被归为 `TOOL_CRASH`。原验收只断言失败与清理，漏检错误分类。已改为 Apple 本机系统配置 `application.sb` 使用的 `(target same-sandbox)`，仍受不同沙箱边界和独立 UID 的内核权限限制；验收增加明确超时错误和无 PermissionError 的条件。新增真实 Seatbelt 测试确认沙箱内子进程可终止、沙箱外同 UID 进程的 signal 0 被拒绝，真实 tool-runner 返回 COMMAND_TIMEOUT。当前相关测试 **79 passed、3 skipped**；改变策略后的管理员六场景验收仍待执行。

broker 强杀时独立清理器已清空 UID，但普通 Host 因失去回执正确保留 `SANDBOX_CLEANUP_PENDING`，没有伪造确认或发布文件。此为进程清理成功，不代表未知 journal 已自动对账；授权撤销当前仍统一呈现执行失败。原始第二次报告另存 `user-broker-report.second-raw.json`，逐项复核与宿主复查保存在 `user-broker-report.second-review.json`、`user-broker-host-recheck.json`。保留原报告，不能以改写 passed 字段代替新策略实测。

第三次管理员测试：正常执行与取消通过，超时触发后独立 UID 清理成功（任务开始至清理约 5.408 秒），且已无 PermissionError。新的严格验收正确挡住另一处上层问题：`WorkspaceCandidateRuntime` 把执行器的 `COMMAND_TIMEOUT` 直接当作错误目录名称，而正式目录只定义 `TOOL_TIMEOUT`，因此报 `WORKSPACE_CANDIDATE_UNAVAILABLE` 并停止后续场景。三个任务 UID 和临时服务经独立宿主查询均无残留；第三次原报告与复核分别留在 `user-broker-report.third-raw.json` 和 `user-broker-report.third-review.json`。

已修复候选工作区边界的超时转换，保留 timed_out 而不是把已返回的结果记录标成 completed；同时保留 `ToolExecutionCancelled` 的 TOOL_CANCELLED 分类，避免丢成 TOOL_CRASH。新增完整候选 Pipeline 测试验证两种失败都不发布部分产物、保留确认的清理事实；模拟 broker 的真实 IPC/Host Pipeline 路径也覆盖 COMMAND_TIMEOUT 回执。验收脚本同时严格检查取消分类。这些是业务结果转换变更，不放宽 OS 权限或清理要求；管理员完整验收仍待重跑。

**最新结果：第四次管理员联合验收六项通过，逐项复核通过。** 以下结论替代此前三次测试的待复测状态，不扩大生产支持范围。

| 场景 | 任务启动至清理完成 | 核验结果 |
| --- | --- | --- |
| 普通用户执行、隔离与文件写回 | 1.001 秒 | 登录 UID 501 调用，任务使用独立 UID；两文件由原有文件服务提交，哈希与属主核对一致 |
| 持续派生期间取消 | 1.544 秒 | TOOL_CANCELLED / cancelled，清理确认，不发布部分文件 |
| 命令超时 | 5.468 秒 | TOOL_TIMEOUT / timed_out，无 PermissionError，清理确认，不发布部分文件 |
| 普通 Host 强杀 | 1.581 秒 | Host 退出码 -9，独立服务清空 UID，正式项目无部分产物 |
| broker 强杀 | 1.425 秒 | 独立服务通过 owner_eof 清空 UID；普通 Host 保留未知回执及恢复记录 |
| 授权撤销 | 1.564 秒 | 终止并清空 UID，不发布部分文件 |

六个 UID / invocation_id 各不相同，launch_count 均为 1，最终 clean、observed_empty=true、issues=[]。五个异常场景注入时均观测到持续派生，全部早于 15 秒负载自然结束；上述时间是任务启动至清理完成，非配置期限或性能保证。错误 OS UID、错误凭证、自报 UID、自定策略、其他目录、替换目录、重放请求均被拒绝；工作区访问和子进程正常，外部读写、链接越界、回环网络与直接 setuid(0) 拒绝。项目目录元数据、原文件与 canary 保持不变。另行读取宿主进程表与 system launchd 域，确认本轮 UID 无残留进程、临时试验服务无残留。

原始通过报告归档为 `dev-artifacts/macos-task-uid/user-broker-report.fourth-passed.json`，SHA-256 `96f80e91848d83b28efef5c3782ec9ab1c4b2bb37aa5554947d2f493a335c293`，逐项复核保存于 `user-broker-report.fourth-review.json`。最近代码回归（含错误恢复）133 passed、3 skipped。

阶段验收完成，不再要求重复同一脚本。仍需区分：broker 崩溃的进程已清理，但 Host 的 `SANDBOX_CLEANUP_PENDING` 尚需可信恢复对账，不能直接删记录或重放；撤销目前呈现通用执行失败。永久签名辅助服务的安装、发行包、并发与完整资源预算继续未验收，生产能力保持关闭。

身份 API 参考：[Apple getpeereid(3)](https://developer.apple.com/library/archive/documentation/System/Conceptual/ManPages_iPhoneOS/man3/getpeereid.3.html)。

## 崩溃记录自动核对与冻结辅助程序（2026-11-08，第二次管理员验收通过）

上述六场景通过记录属于上一版源码候选。本轮新增行为尚不能沿用其管理员通过结论。

- Host 在可能远程启动前，将 grant ID、项目目录身份写入既有 `native_invocations.v1`，并随请求发送原执行摘要。root 在启动前持久化逐调用绑定（调用 ID、摘要、普通用户 UID、项目身份、启动周期、独立 UID 租约），不依赖会被下一次调用覆盖的 active 指针。
- 新增只读 `status` 操作，仍核验内核对端 UID、能力凭证和目录 FD。撤销/过期授权可查询原清理事实，但不得启动新任务。跨 boot、错调用/摘要/租约、未清理或 UID 仍有进程都不能返回清理成功；查询不启动、停止、导出或重放任务。
- Host 在恢复检查和原生工具准入前自动核对。经 root 对端确认后，以 CAS 在原 SQLite/CAS 权威中保存回执及原记录引用，保留 `recovered_outcome=unknown` 和 pending 头。重复核对幂等；并发修改、连接失败或证据缺失保留原状态。`NATIVE_OUTCOME_UNCONFIRMED`（E3818）区分“进程已清理，业务结果未知”，仍阻止自动恢复/重放；不发布中断任务的文件。
- 补充冻结 Host 内部角色 `macos-broker-service`、`macos-execution-service`。冻结版恢复监督器直接调用已安装的固定 Host 角色，不再假定源码 `.py` 可从包内复制。特权角色必须是 root 从固定安装目录启动，并重新核对 root 所有权、目录权限和完整包内容摘要。
- `scripts/install_macos_sandbox_helper.py` 提供独立辅助程序安装器；根目录为 `/Library/PrivilegedHelperTools/org.gitgo.sandbox-v1/<bundle-id>/`。先核对本地代码签名，再复制、重查源/目标清单和内部链接、固定 root 所有权并原子发布；已有相同版本校验后复用，不覆盖运行中的版本。普通用户的受管 Gitgo、PATH、真实项目 ACL 不变。
- 当前只接受操作者显式指定的本地 ad-hoc 构建，**没有宣称发行者身份认证/公证或永久服务上线**。受测服务仍使用临时 launchd 作业和有时限、同启动周期的项目授权。正式发行信任、持久服务注册/升级/卸载、重启后授权恢复以及产品默认启用仍需后续工作。生产 `macos_execution` 能力保持关闭。固定系统 Python 小助手仍是依赖，不能宣称所有环境均已覆盖。

本轮验证：277 passed、8 skipped、30 subtests passed（恢复、broker、候选文件服务、真实 Seatbelt、UID 恢复、forkless 及 Native Host）。完整 `packaging/build_macos.sh` 构建、ad-hoc 签名核验、frozen Host/Daemon/tool-runner smoke 通过；普通 UID 直接调用两个特权角色均被拒绝。冻结包真实导入恢复模块，未借用源码后端。日志/本地检查保存在 `dev-artifacts/macos-helper/{regression.log,build.log,local-preflight.json}`。

准备好的安装包：`dev-artifacts/macos-helper/acceptance-v1`。辅助程序内容清单 591 项，bundle ID：`134285375de9c85e33462125df174abcf2f2f494991e0b8ae67e157ac84ddfc9`。这些是本地打包证据，不是管理员运行证据。

操作者运行：

```sh
sudo ../issue45-tools/macos-build-venv/bin/python -B -I scripts/check_macos_packaged_broker.py --run --allow-local-adhoc --bundle dev-artifacts/macos-helper/acceptance-v1/internal/gitgo-host > dev-artifacts/macos-helper/packaged-broker-report.json
```

联合脚本安装上述独立辅助程序，暂存 root 控制的测试驱动与普通用户假项目，用冻结 Host/特权 broker/恢复监督器执行原六场景。Host 强杀和 broker 强杀后重新启动 broker，由新普通用户 Host 自动核对原日志两次：必须清理已确认、结果仍未知、原记录可读、CAS 历史已保存、无重放、无文件发布。脚本核对真实冻结执行入口，异常路径不得仅因自然超时退出而通过。确认 UID 清理后卸载临时作业；未确认则保留恢复服务与证据。已安装的辅助程序及诊断记录保留，不设置开机常驻服务。

首次冻结包管理员验收（2026-11-08 23:00）未通过：辅助程序安装完成，但第一项在 8 秒启动等待后报告 `Broker socket did not appear`，未进入任务执行，临时 broker 已卸载。原始报告归档为 `dev-artifacts/macos-helper/packaged-broker-report.first-startup-failed.json`，SHA-256 `08e71a94adbed19e0b0862623bf54549ea53fcbe7ba5265d66c169e1955c03b1`。

系统日志显示 23:00:10.179 起首次加载本地 ad-hoc 包及多个 Python 扩展，23:00:16.901 仍在加载 `_lzma`，23:00:18.202 测试驱动卸载服务。新复制包以普通 UID 完成 broker/监督器模块冷加载约 7.413 秒；已安装包 591 项文件的内容摘要和 root 目录/权限核验通过。这支持“8 秒启动等待余量不足”的判断，但不能代替 root broker 实际就绪和六场景通过的证据。

验收驱动已改为独立的 40 秒服务启动上限：必须收到 root 对端对未授权 root 探测者的拒绝回执才算就绪，不把 socket 路径存在当成成功；发现服务已退出则提前报告；失败时记录卸载前 launchd 状态及 stdout/stderr。未放宽任务、取消或 UID 清理时限。新增慢启动、提前退出、假对端、错误响应和残留 socket 测试；相关 38 项通过。仅改变测试驱动，冻结包和 bundle ID 不变，下一次管理员运行复用原安装。联合验收仍待重跑。

**最新结果：第二次冻结包管理员联合验收六项通过，独立复核通过。** 此结论替代本节前面的待执行/待重跑状态，限于当前本地签名包、临时授权及临时服务范围。

| 场景 | 独立任务 UID | 启动至确认清理（秒） | 结果 |
| --- | --- | --- | --- |
| 正常执行 | 204776915 | 0.973 | 两文件经 FileService 提交，实际文件哈希及 UID 501 所有权复核通过 |
| 取消 | 202633647 | 1.626 | TOOL_CANCELLED，后台清空，无部分文件发布 |
| 超时 | 200861679 | 5.528 | TOOL_TIMEOUT，后台清空，无部分文件发布 |
| Host 强杀 | 209517139 | 1.578 | 自动核对清理回执，原结果保持未知，不重放 |
| broker 强杀 | 202443051 | 1.463 | 独立监督器清理，恢复后自动核对，不重放 |
| 授权撤销 | 207852372 | 1.644 | 中止并清理，无部分文件发布；当前业务呈现仍为 TOOL_CRASH |

六项均为独立调用和 UID，launch_count=1、最终 clean、observed_empty=true、无 cleanup issues。五项异常测试注入时后台出生计数仍增加，清理均早于 15 秒负载自然结束；表内为完整启动到清理时间，不将配置的 2 秒命令超时误称为 2 秒完成清理。真实冻结普通 Host、tool-runner 和 root 恢复监督器入口已核对；越界文件读写、链接、网络、提权及伪造授权/项目对象/重放请求均拒绝。内层 owned-session-only 信息仍如实保留，最终判断使用外层独立 UID 清理事实。

独立只读 SQLite/CAS 复核确认：两条崩溃路径的 pending/recovery 指向同一新记录，原记录和摘要可核验，新回执准确绑定原 invocation ID、execution basis、项目 dev/inode/owner、boot 和独立 UID；runtime_cleanup 已确认，recovered_outcome 仍为 unknown，退出结果未伪造，重复核对幂等且继续阻止自动恢复。其余场景终态 history 已保存且没有残留 pending。实际项目 canary、元数据及异常路径无产物均复核通过。另行查询宿主 ps 和 system launchd，六个 UID 无残留进程、临时 broker/恢复作业均已卸载。

报告归档 `dev-artifacts/macos-helper/packaged-broker-report.second-passed.json`，SHA-256 `0011041be64257345b7bd3b1fa9396f10a3a8764ce24bbc5e27cc421462c6e60`；逐项复核 `packaged-broker-report.second-review.json`，宿主检查 `packaged-broker-system-audit.json`。已安装辅助程序保留在原 bundle ID 的独立 root 目录；未启用永久 launchd 服务、生产默认 capability 或发行信任链，不能把本轮通过外推为第四阶段所有边界已完成。

## 常驻辅助服务生命周期（2026-11-08，等待管理员实机验收）

`macos_helper_lifecycle.py` 将安装、常驻、升级、回退和卸载接到独立的管理员入口。正式 macOS 打包现在包含 `sandbox-helper.sh` 与管理脚本，通过包内 frozen Host 运行；普通 Gitgo 安装器不会自动开启该候选能力。

- 不可变辅助程序仍安装到 `/Library/PrivilegedHelperTools/org.gitgo.sandbox-v1/<bundle-id>/runtime/gitgo-host`。
- 每个显式项目授权有命名注册，记录位于 `/private/var/db/gitgo-sandbox-v1/<name>/`，root 私有状态与普通用户凭据分开。
- `/Library/LaunchDaemons/org.gitgo.sandbox.<name>.plist` 使用固定角色和绝对路径、`RunAtLoad`、`KeepAlive` 与重启节流；broker 崩溃由 launchd 拉起。
- 管理锁串行化生命周期操作；admission 锁覆盖真实执行与输出处理。忙碌时拒绝升级/卸载。broker 已崩溃时，仍须逐项核对其 UID lease 为 clean、清理证据完整、当前 UID 进程集合为空；不能把服务死亡当作清理成功。
- 升级先持久化维护状态，停止旧作业并等它释放套接字所有权，然后启动新包。新进程必须经过固定安装路径验证，并回应真实 root peer 就绪探测。就绪后才恢复任务准入。失败恢复旧版本；回退也失败则保留维护状态、拒绝执行，由管理员 `recover` 重试。
- 每次状态转换保留 root 私有诊断快照，任务恢复权威仍是既有调用记录与 UID lease。恢复操作不重放任务、不发布未知结果。
- `uninstall` 移除 launchd 作业、plist、socket 和用户凭据，支持重复执行。保留不可变共享版本及 root 恢复证据，不删除项目或普通 Gitgo 安装；版本垃圾回收不在本轮范围。

包内管理员入口（所有变更均须显式调用，不通过模型/工具 JSON）：

```text
sudo ./sandbox-helper.sh install --name my-project --bundle ./internal/gitgo-host --project /absolute/project --lifetime 180 --allow-local-adhoc
sudo ./sandbox-helper.sh status --name my-project
sudo ./sandbox-helper.sh upgrade --name my-project --bundle /absolute/new/internal/gitgo-host --allow-local-adhoc
sudo ./sandbox-helper.sh recover --name my-project
sudo ./sandbox-helper.sh uninstall --name my-project
```

`--uid` 默认取 `SUDO_UID`；安装时验证普通用户和项目目录对象。常驻生命周期不延长授权：现有授权仍限制为 10–600 秒且绑定 boot。过期/跨 boot 后服务可驻留，但不准入新任务。持续授权产品接口、发行者签名信任、跨启动执行恢复及资源上限仍是后续边界，不能把本轮称为默认生产可用。

本地验证：生命周期/安装/broker 定向测试 **102 passed**；既有执行链、恢复、Seatbelt、UID 和 frozen 入口回归 **207 passed, 8 skipped, 30 subtests passed**。测试中的 launchd 替身只证明状态机分支，不能替代管理员实测。新增真实 `flock` 排他性测试，验证独立锁句柄不能同时进入。

管理员验收脚本 `scripts/check_macos_helper_lifecycle.py` 使用唯一命名服务、临时项目和真实 frozen 普通用户 Host，按以下顺序执行：安装 A 并隔离执行；SIGKILL 空闲 broker 后由 launchd 自动恢复；后台持续派生时拒绝升级/卸载并取消清理；切换到不同 bundle ID 的 B 并实际执行；故意安装签名有效但立即退出的惰性候选包，确认启动失败恢复 B 且仍可执行；注入停止后未完成切换的维护记录，确认 `recover` 幂等且不重放；过期授权拒绝执行；卸载并重复卸载、核对作业和凭据消失。

A/B 使用同一代码、不同的测试资源标记，以证明实际安装路径切换；不宣称验证了任意未来版本兼容性。维护中断采用明确的状态注入，broker 自动重启使用真实 SIGKILL。脚本结束仅在 UID 清理已证明时卸载测试服务，保留报告、版本和恢复证据。当前这组管理员验收尚未运行。

最终本地包为 `dev-artifacts/macos-helper/lifecycle-v2`，辅助程序 bundle ID `4a5d11b6ac21f564b162eee6f6489bc3c54d2dbac4aaa3ddfe1697c54e09d222`。完整构建及 Host/Daemon/tool-runner frozen smoke 通过；包内管理入口 `--help` 可运行，frozen 模块包含套接字所有权等待和 admission 锁。打包回归另有 **4 passed**，合计 **313 passed, 8 skipped, 30 subtests passed**。日志与预检记录在 `dev-artifacts/macos-helper/lifecycle-{build,unit,regression,packaging-tests}.log`、`lifecycle-preflight.json`、`lifecycle-bundle.json`。

### 首轮常驻生命周期验收：准备阶段失败，尚未执行服务测试

本次 `lifecycle-report.json` 为 0 字节，脚本已退出，服务注册目录尚不存在。临时工件停在复制 `/usr/bin/false` 后、创建 `intent.txt` 前。本地复现 `shutil.copy2` 的 `copystat/chflags` 尝试复制系统保护标记而触发 `PermissionError: Operation not permitted`。这是验收夹具准备错误，不能记为辅助服务安装/升级失败，也不能记为通过。

修复夹具为只复制数据并设置可执行权限，对临时副本显式 ad-hoc 签名（不修改系统原文件）；提前验证严格签名检查通过且执行正常返回 1。直接保留 Apple 平台签名的搬移副本在本机不受信任，因此不再依赖该信任路径。另捕获准备阶段异常并输出失败 JSON，避免留下空报告。新增真实 macOS 夹具测试；夹具、生命周期和就绪回归 **33 passed**。辅助服务代码与 `lifecycle-v2` frozen 包未变，只需重新运行修正后的验收脚本，并同时保存 stderr。复核记录 `dev-artifacts/macos-helper/lifecycle-first-attempt-review.json`，测试日志 `lifecycle-fixture-regression.log`。管理员实机服务验收仍待重跑。

### 第二轮常驻生命周期验收：前半程通过，版本切换失败后恢复旧服务

报告已存档 `dev-artifacts/macos-helper/lifecycle-second-failed.json`（SHA-256 `b26f8242b728aaf454ef1de7eaf91779755cd352be249d36b54cf897778aba7b`）。真实管理员运行确认：常驻安装与 frozen 普通用户隔离执行通过；SIGKILL 后 launchd 自动更换 PID（76278 → 76335）；持续派生后台任务期间升级、卸载均被排他锁拒绝；取消后 UID 208371856 在清理中经过两轮 STOP，最终无进程、无清理问题。正常执行 UID 201064398 同样已 clean。

后续升级 B 未完成，异常只报告“prior service restored”，原始 cause 未被验收脚本保存。系统日志显示旧服务退出后重新启用的仍是 A，未看到 B 注册；已安装 A/B 清单独立重新计算均匹配 bundle ID。报告中的 `Wrong peer user` 是就绪探测刻意用 root 连接普通用户授权端点产生的预期拒绝，不能据此认定升级原因。

代码复核发现 `bootout` 返回成功后立即 `print`，若作业还存在就报错。用真实、唯一命名的临时用户级 launchd 作业复现：bootout 返回 0，但随后约 0.3 秒内 print 仍返回 0，之后才返回 service-not-found 113。这是可触发上述回退的实际时序缺陷；由于本次原始异常链缺失，不能声称已从报告直接证明唯一根因。

修复为限时等待作业注销（10 秒），仅把 launchctl 的 113 当作明确不存在；观察出错仍拒绝继续。注销后继续等待 broker 生命周期锁释放，避免连接旧套接字误判候选健康。新增持久化 `lifecycle-failure.json` 保存切换阶段、候选与原始异常，验收报告同时保存完整异常链。真实用户级作业调用修正后的 `unload` 已通过，耗时约 0.365 秒，观察到 `0 → 113`；该试验不替代 root 服务升级验收。证据 `lifecycle-stop-real-check.json`。相关单元/回归 **108 passed**，完整包重新构建为 `lifecycle-v3`，管理员全链仍需重跑。

独立只读系统审计 `lifecycle-second-system-audit.json` 确认上述两个 UID 无残留进程，broker 与两个执行服务均返回 113；测试 plist、socket、用户凭据均已移除。不可变版本和私有恢复记录按设计保留。未执行到的成功升级、故意坏版本回退后执行、维护中断恢复、授权过期拒绝及独立卸载验收仍不能计为通过。

### 第三轮常驻生命周期验收通过（2026-11-09）

`lifecycle-v3` 已完成本机管理员实测，报告 `dev-artifacts/macos-helper/lifecycle-third-passed.json` 为 `passed=true`，SHA-256 `9dbfb1fa13f509425bb0b864066e69942704d829d5574cef3b71eb34064c0db6`。按当前验收范围，第一组辅助服务安装/常驻/升级/卸载可标记完成；前面的两次失败记录继续保留。

- A 版常驻安装与普通 UID 501 的真实 frozen Host 隔离执行通过；SIGKILL 后由 launchd 自动拉起，PID 78756 → 78849。
- 后台持续派生时，升级与卸载被 busy 锁拒绝；取消完成，UID 209591630 的清理经过两轮 STOP，清理阶段约 0.778 秒，最终无进程、无问题。
- 从 A `5be77c901cd402eec895ac7fbbe378fe9b1f007ae6c157b74287342e4c520c2e` 切换到 B `dd918c1b794ee119a885ccb1c4462d2da221158f8e3f158f58c43b58b6ff58b7` 后，普通用户通过新路径实际执行并提交输出。
- 故意退出码 1 的坏包在就绪期限内无法启动，回退 B 成功；回退后再次真实执行成功。报告中的这一条 TimeoutError 是预期故障注入，不能误读为验收失败。
- 注入维护中断状态后 `recover` 两次幂等，调用 ledger 未增加，没有任务重放；恢复后再次真实执行成功。
- 授权过期时服务仍驻留，但新任务被拒绝；卸载与重复卸载通过，plist、socket、用户凭据移除，版本与恢复记录按设计保留。

独立复核 `lifecycle-third-review.json`：5 次调用使用不同 UID，`launch_count=1`，全部 clean；普通客户端确为 UID 501 与相应 frozen A/B 路径。4 次成功调用的隔离结果、摘要和调用标识逐项匹配，取消调用为 TOOL_CANCELLED 且未提交；最终项目文件归属 501、实际 SHA-256 与提交回执一致，取消任务未发布后台派生输出，外部 canary 未改动。A/B 安装清单分别 591/592 项，重新计算均与各自 bundle ID 匹配。

独立系统检查 `lifecycle-third-system-audit.json`：5 个任务 UID 当前进程集合为空，broker 和 5 个执行服务的 launchctl 查询均返回 113，测试 plist、socket、凭据均不存在。测试没有留下常驻服务；不可变安装镜像和 root 私有证据保留供审计。

范围限制继续适用：A/B 是同一代码加不同测试资源标记，证明实际路径切换而非任意版本兼容；维护中断为状态注入；没有重启整台 Mac 来验证跨 boot 执行恢复。当前采用显式本地 ad-hoc 信任与最长 600 秒/boot 绑定授权，生产默认能力仍关闭。发行签名信任、持续项目授权、跨启动恢复及更广工具/资源边界仍属于第四阶段后续工作。

### 项目授权续期、撤销和历史调用恢复（本地验证通过，管理员验收待运行）

新增显式管理员 `renew` / `revoke`，不通过模型请求或普通用户套接字开放。常驻服务不自动延长授权。持久注册可指定 10–86400 秒授权，续期默认 3600 秒；临时探针仍限制 600 秒。授权仍绑定同一次系统启动、同一个普通用户和同一项目目录对象。跨 boot 续期明确拒绝，不能将本轮当作跨启动恢复已完成。

```text
sudo ./sandbox-helper.sh renew --name my-project --lifetime 3600
sudo ./sandbox-helper.sh revoke --name my-project
```

- 续期在管理锁和执行准入锁内进行，先确认所有已登记任务 UID 已清理，再重新核对目录身份；活跃执行、未知清理、目录替换、旧版本 helper 均拒绝续期。
- 每次续期同时更换 grant UUID 和随机 token，保持原调用与重放账本。先持久化维护标记并撤销旧凭据，再发布归属用户的凭据文件和新授权，最后移除维护标记。任一步中断均不能靠新旧凭据绕过关闭的执行准入；显式重试产生另一套新凭据。已打开的 Host 会话需要重新加载凭据。
- broker 取得执行锁后再次认证，防止请求在锁外通过旧凭据检查、锁内却消费新授权。维护标记存在时版本切换也被拒绝，避免旧 helper 不理解新标记。
- 撤销先落盘，再等待执行锁和完整 UID 清理证据。正在运行的 broker 观察到撤销后终止任务及脱离会话的后代；无法确认清理时仍保持撤销状态并报错，不声称完成。
- 当前凭据可只读查询同一用户、项目和 boot 下旧 grant 的调用清理。请求、root 映射及回执均校验原 grant、调用 ID、执行依据、lease 和 UID；业务结果继续为 unknown，保留旧记录、禁止自动重放或提交。状态查询不消耗或清空重放账本；持久注册账本上限 4096 次，达到上限即拒绝，不通过续期重置。
- 到期时钟继续使用 Darwin `CLOCK_MONOTONIC_RAW`。Apple 将其与包含睡眠时间的连续时钟等价描述，见 [mach_continuous_time](https://developer.apple.com/documentation/kernel/1646199-mach_continuous_time)。本轮验收测真实短时到期，不宣称已经实际睡眠或重启验收。

本地定向测试 **123 passed**；既有执行链、恢复、Seatbelt、UID 与 frozen 入口回归 **207 passed, 8 skipped, 30 subtests passed**。包含凭据轮换与执行锁竞态、分阶段发布中断、清理未知时拒绝、同范围历史授权查询，以及客户端和 broker 通过真实 socketpair 协议验证旧任务回执。它们不能替代 root 常驻服务实测。

完整构建 `dev-artifacts/macos-helper/authority-v1` 及 frozen Host/Daemon/tool-runner smoke 通过。helper bundle ID `1586051d8a9cc2baa371b451292c3a6ead8009b2875ae104cdf60bdc9877ccf2`，591 个清单项；实际 frozen 模块输出 `authority_api=1`、`max_lifetime=86400`，包内管理入口已包含 `renew` / `revoke`。证据分别为 `authority-{unit,regression,build}.log`、`authority-{bundle,preflight}.json` 和管理入口帮助文件。

管理员脚本 `scripts/check_macos_project_authority.py` 使用独立临时项目及唯一命名常驻服务，验证：真实普通用户执行；续期后拒绝旧凭据；真实到期；到期后显式续期；旧调用禁止重放；活跃任务续期被拒；撤销后持续派生后代清理且无输出发布；重复撤销和重新授权；发布凭据后故意中断时新旧凭据均拒绝、显式续期恢复；broker 被 SIGKILL 后自动恢复、更换凭据并核对旧调用清理，仍禁止自动恢复任务。最终仅在 UID 清理已确认时卸载测试服务，并保留审计证据。此脚本尚待用户执行，当前不能将这些管理员路径记为通过。生产默认能力和发行者签名信任继续关闭。

### 项目授权管理员验收通过（2026-11-09）

用户运行 `check_macos_project_authority.py` 的首轮报告为 `passed=true`，已存档 `dev-artifacts/macos-helper/authority-first-passed.json`，SHA-256 `6aa6aad38c51e96113118b267e8714eb1ba2c9b730fac62d7c1cde84f37a1702`。stderr 仅有阶段进度，无异常。测试服务 `auth-a868bf111df740e68d38`，使用上述 `authority-v1` 实际安装的 frozen helper。

- 普通 UID 501 的 5 次成功执行覆盖初始授权、续期、到期后续期、撤销后重新授权，以及中断续期后的恢复；每次任务独占不同低权限 UID，shell 子进程正常运行，输出经原 FileService 提交。
- 旧凭据、过期凭据、撤销凭据均被拒绝；跨项目、目录替换、伪造用户、自选策略和旧调用重放也被拒绝。活跃执行时续期被拒，没有悄悄延长正在运行任务的授权。
- 撤销期间持续派生后台进程的任务 UID 201764627 经两轮 STOP 清理，清理阶段约 0.848 秒；未知输出未提交。撤销和再次显式授权均通过。
- 在凭据发布后注入中断，新旧凭据均不能启动任务；再次显式续期恢复，并成功执行。
- SIGKILL broker 后自动拉起；原任务 UID 209041953 由监督器观察 owner EOF，经两轮 STOP 清理，清理阶段约 0.855 秒。更换凭据后核对到原 grant 的清理回执，Host 从 `NATIVE_CLEANUP_UNCONFIRMED` 转为 `NATIVE_OUTCOME_UNCONFIRMED`。原调用证据保留，两次核对幂等，结果仍 unknown，禁止自动恢复任务，没有重放或发布后台输出。

独立复核 `authority-first-review.json`：7 次调用全部 clean、`launch_count=1`、无清理问题，客户端均为普通 UID 501 与实际 sealed helper 路径；5 次成功输出中的 UID、子进程结果及摘要对应各自调用。最终文件归属 501，SHA-256 与最后提交回执一致；项目 Git 元数据 canary 未变，两个中止任务的后台文件未发布。重新计算安装镜像的 591 项清单与 bundle ID 一致。

独立系统检查 `authority-first-system-audit.json`：7 个任务 UID 均无当前进程；broker 与 7 个执行服务的 launchctl 查询均返回 service-not-found 113；测试 plist、socket、用户凭据已移除。不可变 helper 镜像和 root 私有恢复证据按设计保留。

因此本轮“同 boot 的显式项目授权续期/撤销和旧授权调用恢复核对”验收完成。它不代表整套第四阶段完成：跨系统启动恢复、发行者签名信任、更多开发工具及资源边界验收仍待推进，生产默认能力仍关闭。

### 跨系统启动恢复候选（本地验证通过，真实重启验收待运行）

常驻 broker 新调用的 UID lease 改存 root 私有 `/private/var/db/gitgo-execution-v1`，映射显式标记 `lease_store=persistent-v1`；临时探针仍使用原 `/private/tmp` 存储。分配器在同一排他锁下检查两类记录并保留 UID，不通过续期、服务卸载或重启回收身份。旧临时记录不被伪装成已迁移的持久记录；缺失或未知旧证据仍保留恢复阻塞。

常驻 broker 启动后、打开监听端点前，核对持久 lease。当前 boot 的任务仍交给现有监督器；早于当前 boot 的 lease 要求合法启动标识、有效原调用记录、三次连续空 UID 观察及稳定 boot。任何 UID 再出现、观察失败、时间倒退或记录无效均拒绝完成，不向重启后观察到的 PID 发信号，也不启动旧命令。

恢复先保存原 `state.json` 的完整归档与 SHA-256，再持久化 clean 状态及当前观察 boot。原 UID、原 boot、调用 ID、启动次数和 generation 保持不变；恢复证明绑定原归档并在每次核对时重新检查 UID 是否为空。归档或状态写入中断可以显式重试，不能提前声称清理完成。业务结果保持 unknown。

只读回执包含当前 `boot` 与原 `origin_boot`。普通 Host 在发起调用前把原 boot 写入既有 `native_invocations.v1` 记录；恢复时核对原调用、原 grant、原 boot、当前 boot、项目和用户。旧授权仍不能启动新任务；普通 `renew` 跨 boot 拒绝，管理员必须显式使用 `renew --after-reboot`，且所有旧调用清理已经证明。重新授权不会清空 Host 的未知结果记录或重放账本。

本地定向 **129 passed**，执行链/恢复/安装/frozen 入口回归 **221 passed, 8 skipped, 30 subtests passed**。跨 boot 单元测试使用模拟启动标识，覆盖原证据保留、重复核对、第二次重启、活跃 UID、查询失败、时间倒退、绑定篡改、写入中断，以及客户端/服务端的回执校验；不等于真实重启验收。

完整包 `dev-artifacts/macos-helper/boot-v1` 构建与 Host/Daemon/tool-runner frozen smoke 通过。bundle ID `99ef3f8b9d1328b790da4367b55c21604442270acc6974c274d446e3bf7e003a`，591 项；frozen 预检确认 `authority_api=2`、持久 lease 目录和新的管理选项存在。证据 `boot-{unit,regression,build}.log`、`boot-{bundle,preflight}.json`。

真实验收脚本 `scripts/check_macos_boot_recovery.py` 分 `--prepare` / `--resume --name NAME`，均需 `sudo --run`：

1. 准备阶段新建独立的持久测试项目和常驻服务，先用普通用户 frozen Host 成功执行。再让任务持续派生，实际卸载执行监督器与 broker，使原 lease 留在未确认状态；等待有明确时限的所有任务进程退出。保留 LaunchDaemon plist 和 root 私有原证据，broker 当前停止，不要求留下运行中的后台任务等待重启。
2. 用户真正重启 macOS 后，核验 launchd 已自行拉起 broker，不通过脚本手动启动掩盖自动恢复问题。核对启动标识确实变化、原 lease 已被持久恢复、原始归档与准备时摘要完全一致、启动次数仍为 1。
3. 确认旧权限不能执行、普通续期被拒，显式重新授权后，普通 Host 只核对旧调用的清理，继续保留结果未知和禁止自动重跑。确认旧任务输出未发布，再卸载测试服务，保留审计记录。

若准备完成后暂不做重启，可以运行同脚本 `--abort --name NAME --run` 显式清理并卸载。此路径由当前 boot 的既有专用 UID 监督器执行 recover-only，不复用跨 boot 的空集合证明。跨启动验收尚未执行，不能将本节标记通过；本轮也不声称覆盖突然断电、任意开发工具、发行者信任或整树资源上限。生产默认能力仍关闭。

#### 重启验收准备完成（仍待实际重启）

`boot-prepare-first-passed.json` 为 `passed=true, phase=awaiting_reboot`，SHA-256 `aef544a0c5f8381d0b53802612c3167dbc59945976400297a5f29129c54d99f5`。测试服务 `boot-f07f0034dc67455a90db`，持久 lease `5cb393f3b5174cc58da2f7a3a2638d8a`，任务 UID 207207668，准备时 boot `[1794032512,278949]`。stderr 无异常，执行到中断监督器和 broker、等待有界后代退出的准备终点。

独立只读检查 `boot-prepare-review.json` 确认：真实/有效/保存 UID 均无该任务残留进程；broker 与执行服务的 launchctl 查询均为 113；保留的 LaunchDaemon plist 为 RunAtLoad/KeepAlive，固定指向已验收构建 `99ef3f8b9d1328b790da4367b55c21604442270acc6974c274d446e3bf7e003a`。此时没有手动重启或重新启动测试 broker。尚不能声称跨系统启动恢复通过；下一步需要用户实际重启 macOS 后运行 `--resume --name boot-f07f0034dc67455a90db`。

#### 首次真实重启：自动恢复通过前段，重新授权因设备号变化失败

用户实际重启后的 `boot-resume-first-failed.json` 为 `passed=false`，SHA-256 `9d1f3c42db011a2a86db757f2388569ffcaf22c3da432484dabdefd6a9f3b121`。脚本已通过 launchd 自动启动、原 lease 的归档摘要及启动次数核对、旧权限拒绝、普通跨 boot 续期拒绝；在显式重新授权时抛出目录身份变化异常。后续 Host 回执核对和卸载未执行，整体验收不能计为通过。

只读核查 `boot-resume-first-review.json` 确认授权时目录身份 `[16777231,32167371,501]`，重启后 `[16777233,32167371,501]`：inode 和 owner 不变，仅设备号变化。原实现把同一次挂载有效的 dev/inode 元组当成跨启动恒定身份，导致错误拒绝。独立系统审计 `boot-resume-first-system-audit.json` 确认任务 UID 207207668 无进程，执行作业已不存在；broker 由 launchd 自动启动并仍驻留，PID 668。当前 boot 为 `[1794195896,546569]`，测试服务尚待显式卸载。

修复候选 `boot-v2` 在持久授权创建时通过同一已固定目录 FD 的 Darwin `fgetattrlist` 保存卷 UUID、文件 ID、精确创建时间和 owner，校验所有请求属性确实返回；不支持或缺失属性时拒绝，不退化成路径或 inode 单独匹配。常规执行继续严格检查当前 dev/inode/owner；显式跨 boot 续期只在预存持久对象完全匹配时刷新设备号。卷、目录、创建时间、owner 改变以及旧授权缺少预存对象时均拒绝。API 结构及属性含义参考 [Apple getattrlist 文档](https://github.com/apple-oss-distributions/xnu/blob/main/bsd/man/man2/getattrlist.2)，常量同时核对本机 SDK。

持久对象同时进入 root 调用映射和 Host 原调用记录。跨 boot 的清理回执保留原设备号元组，并用预存持久对象连接新授权；客户端核对原调用与当前固定目录，不能用新目录替代旧项目。没有修改旧 grant 或旧调用来伪造成功。旧测试缺少重启前卷身份，须保留失败证据、显式清理，再创建新的验收记录。

本地定向 **146 passed**；执行链/恢复/安装回归 **221 passed, 8 skipped, 30 subtests passed**。包含真实目录 FD 在改名后的稳定性和目录替换拒绝、模拟设备号变化与显式授权条件、卷/文件 ID/owner/创建时间变化拒绝，以及真实 socketpair 下跨设备号历史调用回执核对。`boot-v2` 完整构建及 frozen smoke 通过，frozen 实际属性读取通过，`authority_api=3`；bundle ID `06165ea7ec85b8616ebef105ad6e2314af9ab2b3302bc829c0f4fb9a622ab04e`，591 项。日志为 `boot-identity-{unit,regression}.log`、`boot-v2-build.log`，预检与清单为 `boot-v2-{preflight,bundle}.json`。

验收脚本增加显式 `--replace-test NAME`，仅用于准备新测试前卸载指定旧测试；不修改旧项目授权来让失败用例通过。下一轮要先运行带 `--replace-test boot-f07f0034dc67455a90db` 的新包准备，再实际重启核验。当前修复尚未获得真实重启通过证据。

#### boot-v2 真实重启验收通过（2026-11-09）

新准备报告 `boot-v2-prepare-first-passed.json` 为 `awaiting_reboot`，SHA-256 `6d027274fd7f9552ed92f6caa684869bd734bec0546e5c75cb9068b256ad6151`。旧测试已显式卸载，新服务为 `boot-a3bb15b7d08b496b869a`；准备阶段独立检查无任务进程，保留新服务 RunAtLoad/KeepAlive plist，并验证预存持久项目身份。

用户实际重启后的 `boot-v2-resume-first-passed.json` 为 `passed=true, phase=complete`，SHA-256 `3d817d9a07e960b8a79f028694ea8d5c5aab36be6a332713e1ec1c451313d275`。boot 从 `[1794195896,546569]` 变为 `[1794196892,197143]`。脚本在未手动启动 broker 的情况下核验 launchd 自动启动，并核对原状态归档摘要、原调用及 `launch_count=1`。

项目的运行期身份由 `[16777233,32259347,501]` 变为 `[16777230,32259347,501]`，再次真实覆盖设备号变化。重启前保存的卷 UUID、文件 ID、创建时间、owner 全部匹配；旧授权拒绝、普通跨 boot 续期拒绝，显式重新授权成功。普通用户 UID 501 的 frozen Host 核对原调用两次，清理确认、幂等、保留原记录及结果 unknown 均通过；未自动重跑、未发布中断任务输出，重放账本保持不变。

独立检查 `boot-v2-resume-review.json` 确认当前 boot 与报告相符，任务 UID 209037949 的真实/有效/保存 UID 均无残留进程，broker 与中断执行服务查询均为 service-not-found 113，测试 plist/socket/client.json 已移除。重新打开项目 FD 所读当前身份及持久属性与报告一致，成功输出和 Git 元数据 canary 未变，中断任务文件未发布。不可变 helper 镜像和审计记录按设计保留。

本次完成该候选包的真实重启恢复验收，上次设备号变化导致重新授权失败的问题已获得真机通过证据。它不等于突然断电验收，也不证明任意开发工具兼容、整树资源限制或发行者签名信任；第四阶段仍是候选能力，`production_enabled=false`。

### 开发工具与资源边界审计（2026-11-09，专用 UID 验收待运行）

`scripts/check_macos_developer_tools.py` 复用普通用户 frozen Host → broker → 专用 UID 执行器 → FileService 提交的现有验收链。每个用例独立临时项目、独立授权/调用/UID；执行后沿用凭据攻击拒绝、原项目 canary、清理及作业卸载检查。只新增验收工作负载，不放宽 Seatbelt、不修改资源策略或启用生产能力，也不需重新打包 helper。

离线工具样例包括 shell 管道、Python 子进程及不安装 pip 的 venv、临时仓库 Git init/add/commit/show、Clang/Make 编译运行、Node 子进程/npm offline 脚本、Bun 子进程。当前登录 UID 的无沙箱/Seatbelt 对照记录为 `dev-artifacts/macos-helper/developer-preflight.json`：六类基线均成功；Seatbelt 下五类成功，Node/npm 因 `libnode.137.dylib` 的读取被拒而失败。Bun 样例成功，不能仅凭 Homebrew 路径未在内容白名单内推断可执行文件必定启动失败。该对照未使用专用身份、frozen supervisor 或资源限制，不能替代正式链验收。既有 broker 验收脚本回归 **5 passed**。

资源样例有严格上限：最多尝试 160 个 `/dev/null` 描述符、单文件最多写入 1.25 MiB、最多 70 个不递归派生且各自最多存活 3 秒的子进程、触碰 64 MiB 内存、单个降低优先级的 CPU 子进程最多消耗 10 CPU 秒且另有 10.5 秒等待上限。内存样例不尝试耗尽宿主内存，也不以成功分配或 RLIMIT_AS/RSS 数值证明总内存限制有效。

静态核对发现当前候选仍为固定实验 `RLIMIT_CPU=8`、`RLIMIT_NPROC=64`、`RLIMIT_NOFILE=128`、`RLIMIT_FSIZE=1 MiB`。`SandboxPolicy.process_limit=32` 与 `memory_bytes=512 MiB` 未完整传入执行监督器，CPU 限制为每进程而非整树累计量。因此报告分别记录 `audit_complete`、各工具 `compatible` 与 `resource_contract_satisfied=false`，不能将测试收集/清理通过说成资源合同满足。正式打包路径的实际计数、限额触发和清理结果等待管理员运行后核对。

#### 首轮正式链审计完成，兼容性与资源缺口确认

`developer-first-report.json`（SHA-256 `4a45b79888e46ca18775fa26a29c49948f4ba6c25cfc1343e5b4e5bdb17c8fbb`）的 10 项收集均完成，`audit_complete=true`，但 `resource_contract_satisfied=false`。普通 UID 501 的 frozen Host、frozen supervisor 和 tool-runner 路径均确认；Shell、Python/venv、Git、Clang/Make、Bun 的固定离线样例成功，Node/npm 因 `libnode.137.dylib` 内容读取被 Seatbelt 拒绝而失败。

文件样例恰好写入 1,048,576 字节后得到 EFBIG；额外打开 124 个描述符后得到 EMFILE（进程原先已有描述符，限额 128）。进程样例成功派生 61 个子进程后得到 EAGAIN，已超过配置中的 32；实际继承的 NPROC 为 64。内存只触碰 64 MiB，AS/RSS 为无限，不能证明 512 MiB 总量限制。以上不是资源合同全通过。

首次 CPU 样例 returncode=1，仅消耗约 0.025 CPU 秒，不能作为 CPU 限额触发证据。已复现其 `os.nice(10)` 被 Seatbelt 拒绝，删除该非必要操作，并保留子进程 stdout/stderr、启动标记及异常退出检查。新脚本支持 `--case resource_cpu` 单项复测。当前登录 UID 的有界预检 `developer-cpu-preflight.json` 中，子进程虽继承 CPU 限额 `[8,8]`，仍正常完成约 10.13 CPU 秒，未观察到限额终止；因此不能根据 getrlimit 数值断言 CPU 限制有效。专用 UID/frozen 路径的修正版 CPU 复测仍待运行，不更改首次报告。

独立检查 `developer-first-review.json` 确认 10 个任务 UID 均无真实/有效/保存身份对应进程，10 个临时 broker 作业均不存在；报告逐项记录执行一次、clean、无清理问题及执行作业卸载。证据保留，生产能力继续关闭。

#### 修正版 CPU 正式链复测：确认未观察到预期限制

`developer-cpu-first-report.json`（SHA-256 `7a66ca3ccb6824b8a71e0fb819cb40b2e5af624571a7975f3c33c45fca6fc974`）使用同一 sealed `boot-v2` helper、普通用户 frozen Host 和专用任务 UID 207463776。启动标记存在、stderr 为空，CPU 子进程消耗约 **10.0255 CPU 秒**后正常返回 0；负载父进程记录继承的 `RLIMIT_CPU=[8,8]`。固定实验限额未达到预期的子进程约束，不能据此宣称每进程或整树 CPU 已受控，也不能将尚未定位的内核/继承机制原因当作结论。

`audit_complete=true` 仅指测试收集与清理完成；`per_process_cpu_limit_observed=false`、`aggregate_cpu_enforcement_verified=false` 与 `resource_contract_satisfied=false` 才是限额结论。本次不是原 `os.nice` 测试缺陷，真实 CPU 负载已经执行。任务启动一次，约 0.354 秒完成末尾空 UID 核对。独立 `developer-cpu-first-review.json` 确认无该 UID 的真实/有效/保存身份残留进程，临时 broker 作业不存在；原始报告和 stderr 均已归档。后续须修复资源约束并重新验收，保持生产能力关闭。

### 工具包与资源保护修复候选 resources-v2（管理员验收待运行）

构建脚本新增显式 `--node-runtime`，由普通构建用户运行 `scripts/stage_macos_node.py`。仅复制选定 Node 的 Mach-O 依赖闭包与匹配 npm 包，重写动态库引用为包内 loader-relative 路径并重新 ad-hoc 签名；不让 root broker 执行用户 Homebrew 程序来发现依赖，也不增加整个 Homebrew 或用户 home 的内容授权。工具链进入原不可变 helper 的清单/安装校验。本机闭包为 20 个镜像，npm 2381 项；这是本地验收工具包，尚未完成正式第三方分发/发行者信任验收。

沙箱监督器仅在 frozen helper 内存在该工具包时提供固定 PATH，支持 `node`、`npm` 及其子进程；`OPENSSL_CONF=/dev/null` 避免读取 Homebrew 配置。不会将硬编码 `/opt/homebrew/bin/node` 偷换成另一个程序，也没有放宽任意用户安装目录。最终 frozen 产物中的 Node 子进程和 npm 离线脚本均在 Seatbelt 下返回 0，证据 `resources-v2-preflight.json`。

管理员项目授权保存明确资源策略，模型 RPC 不能选择这些字段；broker 将策略完整传给专用 UID 监督器。`RLIMIT_NPROC` 使用配置的 32，CPU rlimit 使用指定预算；CPU 预算与 elapsed-time 超时分离。固定单文件/描述符限制保留。`authority_api=4` 阻止新授权管理脚本将新语义错误应用到旧 helper。

独立监督器每约 100ms 观察专用 UID 的进程，使用 BSD 真实/有效/保存身份与开始时间验证 PID，再读取 libproc 内存和 CPU 计数。CPU 按 Mach timebase 换成纳秒（本机 125/3，真实 CPU 负载校准测试覆盖）；按进程 incarnation 累积已经观察到的 CPU，已退出样本不从累计数删除。内存对各进程 resident/physical footprint 的较大值求和。观测失败、停顿、计数倒退或容量耗尽都终止任务并走既有 UID 清理，不把观察错误算成零用量。资源超限强制失败返回，禁止导出工作区；原调用证据保持。

**这一保护是超限检测后清理，不是严格的内核整树配额。** 内存可能在采样间隙超限，共享页求和可能保守多计；短命进程可在两次采样之间结束，其未观测 CPU 尚不能完整追账。报告固定 `strict_aggregate=false`，不得声称达到严格总内存/累计 CPU 合同或启用生产能力。进程数的内核限制仍需专用 UID 实测确认，不能用 mock 代替。

本地真实两子进程测试观察到约 1.041 CPU 秒触发 1 秒 CPU 门槛、约 111.5 MB 合计内存触发 64 MiB 门槛；测试用户主动回收自己创建的子进程，尚不等于 root UID 清理验收。证据 `resource-guard-live-preflight.json`。完整回归 **277 passed, 5 skipped, 30 subtests passed**；最终计时/策略修改的定向回归 **111 passed**。`resources-v2` 完整构建、Host/Daemon/tool-runner frozen smoke 通过，bundle ID `c1cbe3a859998ac953ee4b443950c24a783dd9dacc03791a7480a857ca8f7fd7`、3002 项。

管理员验收脚本 `scripts/check_macos_resource_fixes.py` 安装此确切本地包，在独立项目运行 16 项：普通工具与资源样例、包内 Node/npm、低 CPU 预算下闲置超过 2 秒仍可完成、单进程/多子进程 CPU 超限、脱离进程组的多子进程内存超限，以及取消/超时/broker 崩溃恢复。超限必须分类正确、未提交文件不可发布、任务启动一次且最终 UID 为空；每项清理后卸载临时作业，保留 root 审计证据及不可变镜像。尚待用户运行，不能提前标为通过。

### resources-v2 首次管理员验收：未通过，待恢复服务诊断

2026-11-09 的 `resources-fixes-report.json` 顶层 `passed=false`。Shell、Python 两项通过；第三项 Git 返回 `SANDBOX_CLEANUP_PENDING`，验收等待 UID 退休超时，后续 13 项未运行。因此包内 Node/npm 和资源超限的完整生产路径仍未得到此次验收证明。

失败原始报告归档为 `resources-fixes-report-failed-d4ee916b2257.json`，SHA-256 `d4ee916b22574765af87776a079ed978313e5b7e6836ebcb12f3f0c9a91c41d7`。只读查询显示该执行恢复服务仍被 launchd 重试，退出码 75；没有卸载该服务，也没有修改租约或宣告 UID 清理完成。broker 日志中的授权过期不能单独说明最初失败原因。新增 `scripts/inspect_macos_execution_failure.py` 供管理员只读导出执行状态白名单、服务日志首尾、launchd 状态和对应 UID 的进程记录；不输出任务 argv/env/profile，不执行任务或清理操作。

### resources-v3：修复启动身份误判，完整管理员验收待运行

后续管理员只读诊断确认失败租约仍为 `phase=prepared`、`generation=0`、`launch_count=0`，UID `200970322` 无进程。服务错误从日志开头即为 `Untrusted or stale execution lease`。记录中的 `kern.boottime` 为 `[1794196892,197143]`，当前读取却为 `[1794196892,136146]`；微秒字段变化导致先于任务启动的身份拒绝，不能归因为 Git 不兼容。原先以日历启动时间标识会话的实现有误。

`macos_identity.boot_id()` 改为严格校验、标准化的 `kern.bootsessionuuid`，读取失败时拒绝，不缓存或降级回日历时间。Apple 的 [XNU sysctl 实现](https://github.com/apple-oss-distributions/xnu/blob/main/bsd/kern/kern_sysctl.c) 分别暴露只读启动会话 UUID 与日历启动时间。恢复逻辑不再按标识大小排序；不同 UUID、可信旧记录和重复空 UID 观察仍须同时成立。旧格式未完成记录不能被推断成跨重启完成，旧格式已完成记录仅保留为历史证据；辅助服务授权协议升至 API 5。

仅用于操作员验收的 `retire_macos_unstarted_trial.py` 在分配锁、服务锁下核验明确指定的旧临时租约，要求 generation/launch_count 均为 0、没有 pid/started，检查原服务与不可变镜像绑定、重复观察 UID 为空后才卸载作业。原始记录按摘要归档，原 boot/UID 不改、不复用，退役原因明确为未启动的管理员退役。它不会启动或发送任务信号，也不作为普通工具 RPC。

新增启动身份回归（含观测到的微秒修正）、真实会话变化拒绝、旧格式拒绝、未启动退役及拒绝不明确/有进程记录的测试。受影响的 macOS 套件通过 **250 tests + 30 subtests**；真实 Seatbelt Node 子进程与离线 npm 在新冻结包中通过，冻结程序连续 5 次读到相同合法会话 UUID。上述普通用户预检不等同于独立低权限 UID 的完整验收。

新包 `dev-artifacts/macos-helper/resources-v3/internal/gitgo-host`：bundle ID `905e7a55e81b1f4aa8e1d39d047366679da5da59be1d3b9b12f57010ceeae6c9`，3002 entries；完整构建及 frozen Host/Daemon/tool-runner smoke 通过。下一次 `check_macos_resource_fixes.py` 可显式传入 `--retire-unstarted-lease` 核验并退役本次失败记录，再执行 16 项验收；尚未运行，production 仍关闭。失败报告现在会附带执行服务状态、错误和 UID 观察，减少额外管理员诊断轮次。

### resources-v3 管理员结果与 resources-v4 Node 启动候选

本轮报告整体仍未通过：前 8 项通过，第 9 项 `node_npm_managed` 的 Node 子进程示例和离线 npm 均在 4 秒截止时超时，后 7 项未运行。Shell、Python/venv、Git、Clang/Make、Bun 正常；进程限制实测为 32（创建 29 个子进程，加父级达到 32 后内核返回 EAGAIN），文件描述符限制 128、文件大小限制 1MiB 实测生效。`resource_memory` 仅是 64MiB 基础触碰/测量，不能算作内存超限保护通过；后续 CPU/内存超限案例未运行。

上轮 generation=0 的旧租约已完成显式未启动退役、原始记录归档与服务卸载。本轮全部 9 项报告均为 clean、无残留、broker 与恢复作业已卸载；另行只读 census 确认 9 个 UID 均为空。原报告归档为 `resources-v3-first-failed-8cc556a352a9.json`，SHA-256 `8cc556a352a9c9fe7be63e1dc3f936788be76361eff7d5a8227e47adef2e5deb`。

普通用户运行实际安装后的同一辅助包，Node/npm 在同一 Seatbelt 配置内仍通过，说明不能简单归因为安装路径或包缺少依赖。普通用户预检的系统拒绝日志出现 CoreFoundation 默认编码初始化、用户查询与 notification_center Mach lookup 路径；尚未取得失败专用 UID 的栈，因此这是待实测的原因假设，不作为已确认根因。Apple 自身 [WebDAV 启动实现](https://github.com/apple-oss-distributions/webdavfs/blob/main/WebDAVPlugin/WebDAV_Mount.c) 按实际 UID 设置 `__CF_USER_TEXT_ENCODING=0xUID:0:0` 以避免 CoreFoundation 查询用户主目录。

resources-v4 在 `low_spawn` 中为实际专用 UID 生成该编码提示，覆盖控制进程可能继承的 UID，保持调用者 env 不变；不放开 Mach、网络或目录访问，不延长验收的 4 秒截止时间。授权 API 升为 6。新增 6 项环境身份测试，相关 macOS 回归合计 **256 tests + 30 subtests** 通过；新冻结包普通用户下的真实 Seatbelt Node/npm 预检通过。验收工作负载增加编码 UID 匹配检查、Node 版本调用、JavaScript 入口标记；超时保留部分 stdout/stderr 和耗时。

新包 `dev-artifacts/macos-helper/resources-v4/internal/gitgo-host`，bundle ID `b4385580e221c7d13c561a2171e3a34ecb1930725a6e92304b7874c78cf74eb7`，3002 entries；构建和 frozen smoke 通过。独立低权限 UID 的 16 项管理员验收待运行，不能宣告 Node 问题已解决或开启 production。

### resources-v4 首次运行：版本探针超时，功能验收未开始

管理员运行在安装后、首项任务之前的冻结 helper 版本探针超出原 15 秒截止，`resources-v4-report.json` 为 0 字节，全部 16 项尚未开始；不能认定 Node 修复成功或失败。stderr 已归档为 `resources-v4-stderr-startup-failed-335b884e3363.log`，SHA-256 `335b884e33637cb44c313d86ab748459e6d59d254874875b53b3ada516b5fc52`。相同安装路径的普通用户探针约 0.4 秒通过，故管理员启动超时原因仍未确认。

仅修改验收脚本，不重建包：在安装之前建立结果对象，安装/探针失败也输出阶段及错误；探针不再继承 sudo 环境和终端输入，显式提供当前运行 UID 的 CF 编码提示。信任辅助程序的启动预算单独设为 40 秒，与现有 broker 冷启动等待一致，任务的 4 秒示例截止和 CPU/内存/进程策略不变。5 秒尚未退出时只采样这个受控、无秘密环境的直接子进程，记录分段 stderr、返回码、耗时；到期终止并回收探针。不会把超时当通过，也不自动跳过协议匹配。

新增真实子进程测试覆盖环境剥离、慢启动继续、截止终止并保留部分输出、协议不匹配拒绝和安装失败仍有 JSON；连同既有 broker 就绪测试共 **10 passed**。修改后的脚本对实际安装包的普通用户检查约 0.1 秒通过，证据 `resources-v4-startup-local.json`；管理员重跑及 16 项功能验收仍待完成。

### resources-v4 重跑：npm 通过，直接 Node 启动仍超时

`resources-v4-retry-report.json` 整体未通过，已运行 9/16 项。冻结 helper 启动探针约 0.116 秒通过，前 8 项通过；第 9 项的离线 npm 成功运行测试脚本并通过 Node 输出 `12345`，但直接 `node --version` 和带 JavaScript 入口标记的 Node 调用均在 4 秒超时，没有输出入口标记。编码提示与实际专用 UID 匹配。因此 v4 尚未解决直接启动问题，也不能据此断言原先启动探针超时的根因。原报告归档 `resources-v4-retry-report-failed-16ac48089696.json`，SHA-256 `16ac480896968dc7c3c47f96b7c405ef29ff766ed32928b4e0263496e7f1b5d4`。

已执行 9 项均只启动一次，最终 clean、UID 为空、无清理问题，临时 broker 和恢复作业均已卸载；另行只读 census 确认这 9 个 UID 无残留。剩余 CPU/子进程内存超限、取消、超时和 broker 崩溃用例尚未运行。基础内存用例仍不构成内存超限保护证明。

下一轮仅更新验收脚本，沿用同一 resources-v4 冻结包：在 root 验收进程中对本次租约专用 UID 的确切包内 Node 采集有界调用栈。采样前后通过 libproc 核对真实/有效/保存 UID、可执行路径和出生身份，变化则丢弃结果；不读取任务提供的 PID，不授予任务额外权限。Node 兼容性失败只有在原执行、隔离、发布和完整清理检查均通过后，才允许继续其余独立用例；失败结果保留，整体通过仍要求全部 16 项成功。诊断及启动/broker 验收脚本回归 **22 passed**。管理员诊断轮次待运行，production 继续关闭，严格内核级聚合资源上限仍未宣称实现。

### resources-v4 诊断轮次：16/16 通过，间歇启动原因仍未确认

2026-11-09 管理员报告 `resources-v4-diagnostic-report.json` 为 `passed=true`、`suite_complete=true`，16 项均通过。报告按 SHA-256 `86101e31615648b739a7f1256676bccd7d2f42ced3e011ee4386516731052fab` 归档为 `resources-v4-diagnostic-report-passed-86101e316156.json`；对应 stderr 归档 `resources-v4-diagnostic-stderr-passed-32ec2402f066.log`。helper 启动探针约 0.110 秒通过，仍为同一 resources-v4 包，没有新的产品代码修复。

直接 Node 版本调用约 0.023 秒、JavaScript 主程序及 shell 子进程约 0.039 秒、离线 npm 测试约 0.206 秒，均成功。采样观察结果为没有持续存活的包内 Node 进程，因此没有取得卡住调用栈；本轮证明这些路径可以成功，不能证明此前间歇超时已经根治，也不能将成功归因于诊断脚本或缓存。

低 CPU 预算闲置 3 秒正常完成；单进程与脱离进程组的子进程 CPU 用例分别在观测到约 2.089、2.269 CPU 秒时触发 2 秒预算；多子进程内存用例在观测到 183779328 字节时触发 160MiB 预算。三项均正确分类、禁止发布未提交输出并清理身份。取消、超时分类及 broker 崩溃后恢复核对通过，无重复执行。全部 16 个租约最终 clean、启动计数为 1、无清理问题、临时 broker 与恢复作业已卸载；验收后独立只读 census 再次确认全部 16 个 UID 无残留。

当前结论为这一轮正式冻结路径的兼容性、资源监测和清理验收通过；Node 间歇启动仍待不带采样的重复/冷启动稳定性验证。资源机制仍是周期采样并终止，存在观测超量与短命子进程计量边界，`strict_aggregate_verified=false`；production 保持关闭。不能据此宣告第四阶段整体完成。

### stability-v1：首次启动诊断、并发准入和资源边界验收候选

普通登录 UID 在新冻结包上复现了直接 Node 的 4 秒超时，而随后 npm 成功。将同一工具包复制到新的临时路径可再次观察首次启动延迟。`stability-v1-node-stack.txt` 采样到 Node 尚在 dyld `Loader::mapSegments → fcntl`，没有进入 JavaScript；本机 amfid/syspolicyd 同期日志显示逐个处理本地 ad-hoc 动态库。Apple 的 [dyld Loader 实现](https://github.com/apple-oss-distributions/dyld/blob/main/dyld/Loader.cpp) 在映射路径执行签名注册和 `F_CHECK_LV`。这支持本次延迟位于系统动态库验证路径，不把具体系统内部超时或全部历史失败都推断成同一个根因。

对照 `stability-v1-node-cold-measure.json`：两次新路径 Seatbelt 内首次启动约 4.079/3.587 秒，第二次约 0.110/0.105 秒；沙箱外新路径首次约 3.499 秒、再次约 0.024 秒。由此确认原统一 4 秒验收门槛会误判部分正常首次启动。新的稳定性脚本只为每个新任务的首个 `node --version` 提供 10 秒启动预算，后续五个命令仍为 4 秒；任一失败立即停止该任务，不重试、不预先在 root 下运行 Node、不调整产品请求截止或资源策略。新路径不等同于真实系统重启或清空系统缓存。

`stability-v1-fresh-image-preflight.json` 三轮均使用新的临时工具包副本，真实 Seatbelt + frozen Host 共 18 次 Node/npm 调用通过；首个 Node 分别约 9.077/3.712/3.710 秒，后续均小于一秒。此为普通登录 UID 的证据，不代替专用任务 UID 验收；原 4 秒失败报告保留为 `stability-v1-local-preflight.json`。

并发检查发现旧实现将身份分配全局锁持有到整次执行结束，另一个项目无法并发准入。现将分配/UID 保留继续置于全局锁内，分配完成后释放；每条租约持有独立、root 私有且不跨 exec 继承的 `admission.lock`。只有同 boot、状态明确且所有者锁仍被持有的活跃记录允许并行准入。遗留无锁记录、失去所有者、跨 boot、cleanup_pending 仍拒绝；锁本身必须为单链接、root 私有普通文件。所有 UID 历史保留且不复用，同一 broker 的准入/维护排他性不变。全局锁争用最多等待 3 秒。API 升为 7，旧辅助包不能被误当成新实现。

新增真实独立进程 flock/所有者强杀测试、双租约分配锁释放测试、错误属主/权限/链接/状态拒绝测试。相关回归 244 项在应用沙箱内通过，唯一受应用限制的真实 Seatbelt 测试在普通用户非嵌套环境重跑通过（合计 245 项）；后续验收脚本及资源监测定向测试 55 项通过。完整 `stability-v1` 构建和 frozen Host/Daemon/tool-runner smoke 通过，3002 entries，bundle ID `d54720d7cebffcb764e4af8263d4206f4aaebaab6a33da9baeefded5a972579e`。

管理员入口 `scripts/check_macos_stability_concurrency.py` 待运行：默认 6 次新服务/新 UID，每次 6 个 Node/npm 调用，无采样干预；两个独立项目同时执行，双方必须拒绝对方文件读写和 SIGUSR1，UID 不同。A 在独立会话子进程耗尽 2 CPU 秒预算后，B 必须继续更新心跳、保持文件不变并正常提交。另以最多 60 个顺序短命子进程（每个约 0.015 CPU 秒、8MiB 内存）对照父进程 wait 回收的 CPU 与采样总量；采样总量若更低则记录可证明的最小遗漏，若更高也不能宣称完整计量。`bounded_suite_passed` 仅代表这组有界用例与审计完成，`strict_aggregate_verified`、`ready_for_production` 保持 false。真实重启后的复核可使用 `--after-reboot-report`，必须核对不同 boot 和相同包，当前未执行。

### stability-v1 真机验收及 stability-v2 CPU 计量补强

用户提交的 `stability-v1-report.json` 已归档为 `stability-v1-report-reviewed-d30c1cf1dbee.json`，SHA-256 为 `d30c1cf1dbee0b3ec836a0b228ee9237596765e36be4b456e54c85fa9e09e38d`；错误输出归档为 `stability-v1-stderr-reviewed-0a73cb9902ef.log`。六轮共 36 次 Node/npm 调用全部成功、无重试；首轮 Node 首次启动约 8.572 秒，其余轮首次约 0.017–0.018 秒。独立 helper 启动探针耗时约 16.041 秒，完成初始化，不计作业务任务执行预算。此轮没有验证新的系统启动周期，`reboot_verified=false`。

两个真正重叠的任务使用不同 UID，互读、互写和互发信号均被拒绝。限额任务在观测 CPU 约 2.041 秒时触发 2 秒限制并清理；另一个任务在对方清理后心跳仍从 26 增至 29，正常结束并发布结果，canary 完整。全部九个租约最终 clean、无清理异常、UID 无残留，broker 与恢复服务卸载；另行只读进程核对亦未发现这些 UID 的残留进程。

资源审计同时确认了漏计：60 个已回收短命子进程实际消耗约 0.953564 CPU 秒，原监测器整个任务仅记录 0.379321956 秒，至少漏计 0.574242044 秒。因此 v1 的 `bounded_suite_passed=true` 只表示当时定义的有界用例完成，不能证明 CPU 完整计量或生产可用。

v2 保留原每个进程自身 CPU 的累计高水位，同时读取 `proc_pid_rusage` 的已回收子进程 CPU 计数。Apple [XNU kern_resource.c](https://github.com/apple-oss-distributions/xnu/blob/main/bsd/kern/kern_resource.c) 的 `update_rusage_info_child` 会将被回收子进程自身和其后代计数传给父进程。监测器取“所有已观察进程自身 CPU 累计”与“单一进程自身加已回收后代 CPU 的最大高水位”两者的最大值；不相加，也不累加多个后代计数，避免采样过的子进程被回收后重复计费。重复进程身份、非法计数和计数倒退会拒绝继续。资源 API 升至 2、authority API 升至 8。

这一实现仍是保守下界：没有观察到最终父进程、迅速退出并被重新托管的分支，以及内存峰值仍可能漏采。`strict_aggregate=false` 和 `ready_for_production=false` 保留。v2 验收要求短命子进程审计覆盖已知 CPU 下界，并额外验证 120 个有界顺序子进程在 1 秒 CPU 限制下被终止且不发布输出；旧的审计漏计结果不再能令整套有界测试通过。

相关回归 253 项通过，包含真实 Darwin fork/reap 子进程以及父子计数不重复累加的测试。正式 `stability-v2` 构建、frozen Host/Daemon/tool-runner smoke 通过；3002 entries，bundle ID `c05a65306c0eeca52cb8a266977fdd082894a5d68999028b443a27a5425cfc0d`。打包后普通用户预检 `stability-v2-frozen-check.json` 中，只观察父进程就记录约 1.296 秒 CPU，其中自身约 0.085 秒、子进程实际约 1.211 秒，正确触发 `resource_cpu`；此证据没有使用专用任务 UID。完整 v2 管理员稳定性/并发/资源验收及后续真实系统重启检查仍待用户执行。

### stability-v2 真机稳定性与并发隔离验收

用户运行的 v2 报告已归档为 `stability-v2-report-reviewed-52eff9503d12.json`（SHA-256 `52eff9503d12ac13ca1adb53182b24c4619903ca114c2489295aa253b33b512a`），stderr 为 `stability-v2-stderr-reviewed-9dc6acc50d1a.log`。确认运行上述 `c05a6530…` frozen helper，资源 API 2、authority API 8；`bounded_suite_passed=true`。六轮 36 次 Node/npm 调用全部通过、无重试，首轮 Node 首次启动约 9.088 秒，后续调用均小于一秒；helper 探针约 15.096 秒完成。这仍表明首次加载有明显延迟，未声称启动性能问题全部消失。

并发任务实际执行时间重叠，两个方向的文件读取、写入及信号访问均被拒绝。受限任务观测 CPU 约 2.020 秒时触发 2 秒限制，清理约 0.472 秒；另一任务在其清理后心跳从 24 增至 27，正常结束并发布结果。60 个短命子进程实际 CPU 约 0.957450 秒，修复后整个任务观测约 1.163934 秒，覆盖该已知下界；旧的仅自身采样计数仍只有约 0.394210 秒，说明新增的已回收后代计量确实参与了限额判断。独立 1 秒 CPU 限额用例在约 1.008649 秒时触发 `resource_cpu`，清理约 0.896 秒，无结果发布。

全部 10 个租约 launch_count=1、最终 clean、清理无异常、UID 无残留，broker 与恢复服务卸载。报告复核时再次进行只读系统进程核对，这 10 个 UID 亦无进程残留。本轮有界启动、并发隔离及短命子进程 CPU 回归验收通过；`ready_for_production=false`、`strict_aggregate_verified=false` 仍保留。boot UUID 与 v1 相同，`reboot_verified=false`，真实重启后的此包启动及严格聚合资源边界尚未验证。

### stability-v2 重启与资源边界补充验收（待管理员运行）

沿用已验收的 `c05a6530…` 包，不修改生产执行代码或重打包，以便比较同一个 sealed helper 在不同 boot UUID 下的行为。`--after-reboot-report` 现在必须引用一次有成功 helper 探针及 `bounded_suite_passed=true` 的报告，并验证 helper 路径一致、系统启动标识有效且不同，记录引用报告的 SHA-256。它证明启动周期发生变化，不保证操作系统文件缓存完全冷启动，也不代替安装器常驻恢复服务的独立验收。

新增 `--resource-boundaries`，在原有六轮 Node、并发及短命子进程 CPU 验收后追加六项：64 MiB 正常用量；双子进程持续内存超过 160 MiB 时终止且不发布；8 进程策略下内核 fork 拒绝；文件大小与文件描述符上限；12 个已确认重新托管的短命后台进程 CPU 计量；12 次各 192 MiB 的短时内存峰值采样。后台进程仅顺序生成 12 个，每个最多约 0.12 CPU 秒并有 2 秒循环墙钟截止；内存峰值仅保留一个映射，释放后才进入下一次；进程数量用例最多尝试 12 个子进程，均在 2 秒后退出。另有正式链既有的任务总截止和 UID 清理约束。

两项采样审计用 512 MiB / 120 CPU 秒策略保留输出，独立记录工作负载已知用量下界，不能视为低于该用量的限额已被强制执行。`execution_and_cleanup_passed` 表示执行、发布和清理及预期限制用例成立；`coverage_gaps` 与 `resource_fixture_coverage_passed` 单独记录已证实的漏采。启用边界验收时，任一已知下界未覆盖还会令顶层 `bounded_suite_passed=false` 并以非零状态退出；诊断采集完成不等于整套验收通过。即使一次观察覆盖了已知下界，也始终保留 `strict_aggregate_verified=false` 和 `ready_for_production=false`。任一执行、计量证据或清理失败会停止后续用例。

56 项定向测试通过，覆盖重复启动周期/不同包/失败基准拒绝、后台 CPU 和内存漏采不能升级为严格资源保证、清理失败停止等。`stability-v2-boundary-preflight.json` 为现有 frozen helper 在普通登录 UID 下的预检：12 个后台进程均确认已重新托管，CPU 合计约 1.440279 秒；12 次 192 MiB 内存峰值持续约 14–19 毫秒，全部生成有效记录。它仅验证用例能运行，不是专用 UID 隔离及限制验收。包库存仍为 3002 entries，身份与已验收 v2 完全一致。

用户重启 Mac 后执行（输出另存，不覆盖重启前基准）：

```sh
cd "/path/to/Gitgo-issue45-stage4"
sudo ../issue45-tools/macos-build-venv/bin/python -B -I \
  scripts/check_macos_stability_concurrency.py \
  --run --allow-local-adhoc --resource-boundaries \
  --bundle "$PWD/dev-artifacts/macos-helper/stability-v2/internal/gitgo-host" \
  --after-reboot-report "$PWD/dev-artifacts/macos-helper/stability-v2-report-reviewed-52eff9503d12.json" \
  > dev-artifacts/macos-helper/stability-v2-reboot-boundaries-report.json \
  2> dev-artifacts/macos-helper/stability-v2-reboot-boundaries-stderr.log
```

### 首次重启边界验收提交：启动周期未变化，未进入业务用例

14:28 提交的报告归档为 `stability-v2-reboot-boundaries-report-reviewed-ce436c270567.json`（SHA-256 `ce436c270567faf528e53a3cb8e04a4abcb0739fa5771404394f78f120419a63`）。相同 sealed helper 的启动探针约 0.109 秒成功，API 为 2/8；但读取的 boot UUID 仍为 `1712b6c7-f85c-42cc-9780-851da992c246`，与成功基准完全相同。验收按预期在启动周期检查处拒绝继续，`node_rounds=[]`，未启动本轮并发或资源边界用例；不能将此结果归因于资源限制失效。

14:29 在应用沙箱外只读调用系统 `sysctl kern.boottime kern.bootsessionuuid` 和 `uptime` 复核：开机时间仍为本机显示的 2026-11-09 12:01:32，启动 UUID 与报告相同，uptime 约 2:28。尚无基准之后整机重启的证据，重启后启动及资源边界验收继续待运行。未替换基准或绕过不同启动周期校验。

### 重启后资源边界实测：启动通过，孤儿进程 CPU 下界未覆盖

新报告已归档为 `stability-v2-reboot-boundaries-report-reviewed-cfcee769bc38.json`（SHA-256 `cfcee769bc3801424cf93aca636206bb4f8b3d32e5c647de4fec8f2087645095`），stderr 为 `stability-v2-reboot-boundaries-stderr-reviewed-ee19a2f1f353.log`。相同 `c05a6530…` sealed helper、相同成功基准哈希，boot UUID 已变为 `6a185e6c-447b-4115-9887-27b3de774c05`，`reboot_verified=true`。helper 启动探针约 0.344 秒，首个 Node 约 0.098 秒，六轮 36 次 Node/npm 调用全部通过；这证明此包重启后的启动链正常，不保证所有安装后的首次加载都同样快。

并发互读、互写、信号隔离均通过；受限任务终止后另一任务心跳从 27 增至 29，正常结束并发布。普通被父进程回收的短命子进程 CPU 用例继续通过，1 秒限额在约 1.040 秒观测用量时触发。新增六项边界用例全部完成、执行与清理通过：64 MiB 正常用量；160 MiB 持续内存限制在观测 192020480 字节时触发 `resource_memory`；8 进程策略下创建 5 个工作子进程后内核拒绝继续 fork（任务全体最多观测 8 个进程）；文件描述符与 1 MiB 文件限制均产生预期拒绝。

12 个确认已重新托管的后台进程 CPU 实际合计 `1.440238` 秒，整个任务仅观测 `1.144130496` 秒，至少漏计 `0.296107504` 秒。此审计没有证明特定低 CPU 限额已被绕过，但直接证明当前采样计量不能完整覆盖这类后台进程；仍观察不到的最终 CPU 不能靠原父进程的已回收计数补回。`coverage_gaps=["orphan_cpu_audit"]`、`resource_fixture_coverage_passed=false`、`bounded_suite_passed=false` 正确保留，不能以单个用例的诊断执行 `passed=true` 代替资源覆盖通过。

短时内存用例的 12 次 192 MiB 峰值持续约 22–47 毫秒，此次任务峰值观测 303939584 字节，覆盖已知下界；这是 512 MiB 策略下的采样证据，仍不证明所有瞬时峰值都能捕获或较低预算能被强制执行。全部 16 个租约 launch_count=1、最终 clean、清理无异常，相关服务已卸载；报告后再次独立核对真实/有效/保存 UID，均无残留进程。重启启动验收已完成，CPU 孤儿进程漏计与严格聚合资源保证仍未完成，不需为核对此份报告再次重启。

### stability-v3：用独立 launchd coalition 账本补齐退出进程 CPU

v2 的孤儿进程 CPU 漏计不能靠提高采样频率消除。新的执行监督器在工作负载启动前读取其 launchd resource coalition 的累计 CPU 基准，随后用当前值减基准。XNU [coalition_resource_usage_internal](https://github.com/apple-oss-distributions/xnu/blob/main/osfmk/kern/coalition.c) 汇总活跃与已退出任务的 ledger；后代 fork/exec 继承 coalition，setsid 或重新托管不会使已退出的任务从累计值中丢失。读取 ABI 使用 [proc_info_private.h](https://github.com/apple-oss-distributions/xnu/blob/main/bsd/sys/proc_info_private.h) 的 PROC_PIDCOALITIONINFO 和 [coalition.h](https://github.com/apple-oss-distributions/xnu/blob/main/osfmk/mach/coalition.h) 资源计数前缀，CPU Mach ticks 转换为纳秒。

新增 `macos_coalition.py`，只读取系统已创建的资源账本，不自行创建/加入 coalition，不申请 Apple 私有 entitlement。要求监督器直接由 launchd 启动、账本不同于 launchd、初始化时仅监督器一个存活任务。监测到的任务进程必须属于同一账本；监督器身份变化、账本不可读、计数倒退或无效立即失败关闭，不退回漏计的采样 CPU。每个任务独立账本，CPU 预算明确包含启动基准之后的该任务专用监督器和观测子进程开销，停止采样后执行的清理开销不计入本次执行证据。任务正常退出时补读最终计数，再决定是否允许成功发布。

资源 API 为 3、authority API 为 9。旧的采样 CPU 计数保留为诊断字段，实际执行链的限额用 `launchd_resource_coalition` 判断。内存仍采样，CPU 超限仍依赖轮询后终止，不声称即时内核硬上限，`strict_aggregate=false`、生产门禁不变。该接口属于 Darwin 私有 ABI，当前平台已实测；其他系统版本需要验收，接口不适配时拒绝执行。

相关回归 291 项通过；补充验收检查后，76 项定向回归通过（与前者重叠，不相加）。覆盖已退出后代在零 PID 样本时仍计费、启动前基准排除、避免重复相加、共享账本拒绝、跨账本进程拒绝、计数失败不降级、管理员验收必须校验独立账本等。`stability-v3` 完整打包、frozen Host/Daemon/tool-runner smoke 通过；3002 entries，bundle ID `e3c2c1f617ad0926bd7676bd80670b03594796e9808d4c6cbd22689f775fc13b`。

打包程序通过两个临时用户级 launchd 任务并发预检（`coalition-probe/frozen-result.json`）：忙任务的 12 个已重新托管子进程实际 CPU 合计 1.440431 秒，完全不采样其 PID 时，退出后账本仍记录 1.466808625 秒，触发 1 秒 CPU 限额判断。同期空闲任务账本仅记录 0.000747250 秒，账本 ID 分别为 1532/1530；两个测试任务均已卸载。该证据证明冻结实现能保留最终 CPU 且并发不串账，不代替专用低权限 UID 下的实际强制终止验收。

管理员验收在原有边界审计之外新增 `orphan_cpu_limit`：1 秒预算下必须得到 `resource_cpu` 且禁止发布输出。`--require-coalition` 还要求每个任务都记录 authoritative CPU ledger，并且所有调用使用不同的 coalition ID。本轮无须重启（v2 的重启证据仍只适用于 v2）；v3 专用 UID 完整验收待运行：

```sh
cd "/path/to/Gitgo-issue45-stage4"
sudo ../issue45-tools/macos-build-venv/bin/python -B -I \
  scripts/check_macos_stability_concurrency.py \
  --run --allow-local-adhoc --resource-boundaries --require-coalition \
  --bundle "$PWD/dev-artifacts/macos-helper/stability-v3/internal/gitgo-host" \
  > dev-artifacts/macos-helper/stability-v3-report.json \
  2> dev-artifacts/macos-helper/stability-v3-stderr.log
```

### stability-v3 真机验收：CPU 漏计回归与并发账本隔离通过

用户提交的 `stability-v3-report.json` 归档为 `stability-v3-report-reviewed-47ec40310dc4.json`（SHA-256 `47ec40310dc47c294f0393be327dc32434a28d0bc96004bea2a0a47f3cd516e4`），stderr 为 `stability-v3-stderr-reviewed-c3bd9c2a99ec.log`。实际运行 sealed helper `e3c2c1f…`，API 3/9；`bounded_suite_passed=true`、`resource_fixture_coverage_passed=true`，所有边界用例完成，`coverage_gaps=[]`。17 次调用使用 17 个不同的资源 coalition，均记录权威 CPU 账本。

12 个已重新托管的后台进程 CPU 实际合计 1.440319 秒，整个执行账本记录 2.065912416 秒，覆盖已知下界；该总量包含任务启动、专用监督器及其观测子进程，不能把差额全部归于后台工作负载。对应的旧采样自身计数仅为 0.900379747 秒，说明本次通过确实依赖新账本。单独 1 秒预算用例在账本约 1.093831416 秒时触发 `resource_cpu`，约 0.870 秒完成清理，无结果发布；计量完整性改进不等于即时无超调硬上限。

并发任务互读、互写和信号拒绝通过，受限任务 CPU 约 2.086 秒时终止，其任务 coalition 为 1568，存活任务为 1566。清理后另一任务心跳从 26 增至 29，正常发布结果。36 次 Node/npm 调用全部成功、无重试；新包首个 Node 启动约 8.515 秒，helper 首次探针约 14.759 秒，首次加载延迟仍存在。普通短命子进程、持续内存超限、进程数和文件/描述符限制均通过；192 MiB 短时内存峰值此次采样覆盖，不推导为所有瞬时峰值均可靠受限。

17 个租约全部 launch_count=1、最终 clean、无清理异常、无 UID 残留，相关 broker 与恢复服务已卸载；报告后另行核对真实/有效/保存 UID，同样无残留。本轮 CPU 计量与并发账本隔离验收通过。v3 的 `reboot_verified=false`（沿用此前 boot），v2 的重启证据不自动替代 v3；`ready_for_production=false`、`strict_aggregate_verified=false` 继续保留。

### v3 重启基准与内存峰值探索

重启验收继续使用原封不动的 v3 包 `e3c2c1f…`（库存 3002 entries 再核对一致）和 `stability-v3-report-reviewed-47ec40310dc4.json` 成功基准。当前还未将内存峰值改动接入执行链，以免改变本轮待验证的包身份。用户通过苹果菜单重启整台 Mac 后执行：

```sh
cd "/path/to/Gitgo-issue45-stage4"
sudo ../issue45-tools/macos-build-venv/bin/python -B -I \
  scripts/check_macos_stability_concurrency.py \
  --run --allow-local-adhoc --resource-boundaries --require-coalition \
  --bundle "$PWD/dev-artifacts/macos-helper/stability-v3/internal/gitgo-host" \
  --after-reboot-report "$PWD/dev-artifacts/macos-helper/stability-v3-report-reviewed-47ec40310dc4.json" \
  > dev-artifacts/macos-helper/stability-v3-reboot-report.json \
  2> dev-artifacts/macos-helper/stability-v3-reboot-stderr.log
```

内存探索依据本机 SDK `rusage_info_v4` 布局和 Apple [ri_lifetime_max_phys_footprint](https://developer.apple.com/documentation/kernel/rusage_info_v4/2870521-ri_lifetime_max_phys_footprint) 字段。普通用户对自己创建的两个独立子进程进行有界实验（一次仅分配 192 MiB，逐页触碰并立即释放，等待父进程读取，然后正常退出并回收）。`memory-peak-probe/result.json` 记录：MAP_SHARED 峰值持续约 12.016 毫秒，释放后当前 footprint 约 8.389 MB，内核 lifetime peak 仍约 209.814 MB；MAP_PRIVATE 约 12.984 毫秒，释放后当前约 8.422 MB，peak 仍约 209.863 MB。外部父进程在释放后读取的峰值一致；两个子进程被回收后，读取均返回 ESRCH。

因此可补强“已释放但进程仍存活”的峰值检测；此实验不证明提前阻止分配，也不覆盖两次观测间已经退出并被回收的进程。单个进程 lifetime peak 可以作为该任务曾使用内存的下界，但多个进程各自历史峰值不能直接相加作为同时发生的聚合峰值。XNU coalition 当前暴露的 phys_footprint 也是瞬时聚合值，不能从该字段推导累计峰值。后续实现需保留这些限制、独立验证峰值超限后的拒绝发布，并在 v3 重启验收完成后另立包身份；当前仍未宣称严格聚合内存硬上限。

### stability-v3 重启验收通过（2026-11-09）

报告归档为 `stability-v3-reboot-report-reviewed-0bf28b2afbbb.json`，SHA-256 `0bf28b2afbbbb117b0c8291fa3b2aa0074b6c101b1077542037c703511e66ca4`；stderr 为 `stability-v3-reboot-stderr-reviewed-398a4dc409a0.log`，SHA-256 `398a4dc409a04da2c71d0af1e4af08dd2d21493885a0444cad082a6ef6c607db`。重启前 boot 为 `6a185e6c-447b-4115-9887-27b3de774c05`，本轮为 `43864191-41ea-429c-94fa-76a4bddff286`，独立读取当前系统 boot UUID 一致；helper 仍为同一 sealed v3 `e3c2c1f617ad0926bd7676bd80670b03594796e9808d4c6cbd22689f775fc13b`，参考报告 SHA-256 与上轮成功基准一致。`reboot_verified=true`、`bounded_suite_passed=true`、`resource_fixture_coverage_passed=true`。

6 轮共 36 次 Node/npm 调用全部通过，首个 Node 约 0.101 秒，helper 探针约 0.331 秒。17 个任务使用 17 个不同的 coalition；普通 60 个短命子进程已知 CPU 约 0.953 秒，任务账本约 1.366 秒；12 个重新托管后台进程实际 CPU 合计 1.440291 秒，账本 2.084661041 秒（包含监督开销，不把差额归于工作负载）。后台进程 1 秒预算在账本 1.0090015 秒触发终止，约 0.688 秒完成清理，没有发布任务结果。

并发任务双向读、写、信号拒绝通过，受限任务清理后另一任务心跳从 25 增至 27，仍正常完成。持续内存、进程数、文件大小、描述符限制及短时内存审计均通过本轮用例。全部 17 个租约仅启动一次、最终 clean、清理 issues 为空、无残留 UID 进程，broker 和恢复服务均卸载；另外用系统进程表的有效/真实/保存 UID 核对，亦无测试进程残留。stderr 仅有进度记录，无异常栈。

本结论是同一 v3 包在重启后的有界回归通过，不替代其他平台或发行安装的验收。短时内存审计仍是采样覆盖而非瞬时硬上限保证；`ready_for_production=false`、`strict_aggregate_verified=false` 保持不变。下一步可另立包身份接入上节的单进程 lifetime peak 检测，保留已退出进程和非同时发生的多进程峰值限制。

### stability-v4：释放后的内存峰值检测，等待管理员验收

资源 API 升至 4（authority 仍为 9），`proc_pid_rusage` 改用完整的 `rusage_info_v4` 结构读取 `ri_lifetime_max_phys_footprint`。PID/启动时间/UID 前后核验及 coalition 归属检查保留；v4 查询失败不回退到只有瞬时内存的 v2。已读到的最大单进程历史 footprint 和瞬时进程内存总量分别保留，任一超过策略触发原有 `resource_memory` 清理与拒绝发布路径。多个进程的历史峰值绝不相加，也不把历史峰值加到当前其他进程内存上；进程消失、exec 或 PID 重用不会抹掉已经记录的峰值。证据新增 `peak_single_process_memory`、`memory_peak_lower_bound`、`memory_limit_basis` 和 `unobserved_exited_memory_covered=false`，原有 `peak_observed_memory` 仍只代表瞬时采样总量。

这是补强检测，不是分配前拦截；观察前已退出并被回收的进程、多个进程同时出现但未被采样的聚合峰值仍可能遗漏。瞬时 resident/footprint 按进程累加也可能对共享页保守多计。`strict_aggregate=false` 和生产能力关闭不变。

回归覆盖释放后才首次读取、峰值不能跨进程相加、已记录峰值不能因 PID 重用丢失、非法/缺失计数拒绝继续。完整相关回归初次为 311 passed / 1 failed，唯一失败是 Codex 外层沙箱禁止 `sandbox_apply`；在普通用户环境重跑该 Seatbelt 用例以及更新后的边界验收测试，38 passed（重叠测试不累加）。日志为 `stability-v4-tests.log`、`stability-v4-host-tests.log`。正式构建和 frozen Host/Daemon/tool-runner smoke 通过，包位于 `dev-artifacts/macos-helper/stability-v4`，helper 库存 3002 entries，身份 `12c81a4f16d9fe706c62e66488f5b752612d4102e2a7a97ea8a18ab8f1aa4a84`。

`memory-peak-v4/frozen-result.json` 用实际冻结 helper 做独立有界探针：自己的直接子进程逐页触碰 96 MiB，约 7.724 毫秒后释放，随后等待管道输入，确保第一次观察发生在释放后。当前内存 11,993,088 字节，历史峰值 108,921,192 字节；256 MiB 策略正常，64 MiB 策略返回 `resource_memory`，依据为 `single_process_lifetime_peak`。子进程已回收。此探针只限定自己创建的子进程以隔离同 UID 其他程序，不代替专用 UID/launchd/正式发布路径验收。

管理员套件新增两项：一个 192 MiB 短暂映射释放后保持进程存活一秒，在 512 MiB 策略下正常收集峰值；同一负载在 160 MiB 策略下触发终止、清理，不能发布 `result.json` / `measurements.json`。正式监督器不会为了测试而暂停采样，因此超限用例也可能在逐页写入期间被瞬时总量提前终止；报告记录实际触发依据，不把这种提前终止冒充释放后检测证据。释放后检测由上述独立探针确定性覆盖。新套件共 19 个任务，仍验证 CPU 账本互不混用及已有 Node/npm、并发隔离和资源边界。无需重启：

```sh
cd "/path/to/Gitgo-issue45-stage4"
sudo ../issue45-tools/macos-build-venv/bin/python -B -I \
  scripts/check_macos_stability_concurrency.py \
  --run --allow-local-adhoc --resource-boundaries --require-coalition \
  --bundle "$PWD/dev-artifacts/macos-helper/stability-v4/internal/gitgo-host" \
  > dev-artifacts/macos-helper/stability-v4-report.json \
  2> dev-artifacts/macos-helper/stability-v4-stderr.log
```

本节截至打包探针完成；管理员验收尚未运行，不声称 v4 低权限执行链或重启验证已通过。

### stability-v4 真机验收通过（2026-11-09）

报告归档 `stability-v4-report-reviewed-5701aaa03318.json`，SHA-256 `5701aaa03318be4b106f8df52bd553a23c7827ab9b95811cd45c3f9af79285b2`；stderr 归档 `stability-v4-stderr-reviewed-2f8f8d58afde.log`，SHA-256 `2f8f8d58afdeeee35f3d9439634ad91a636585a3ed4d9715c1e2a379005b2779`。实际 helper 身份为上一节 v4 `12c81a4f…`，资源/authority API 4/9。`bounded_suite_passed=true`、`resource_fixture_coverage_passed=true`，所有边界用例完成，`coverage_gaps=[]`。

新增正常用例逐页触碰 192 MiB，约 12.001 毫秒后释放：瞬时总量最高仅 103,120,896 字节，单进程历史峰值仍记录 215,237,040 字节，在 512 MiB 预算下正常完成。新增超限用例的瞬时总量最高仅 102,694,912 字节（约 97.94 MiB），未达到 160 MiB 限额；单进程历史峰值 215,220,656 字节（约 205.25 MiB），实际终止依据明确为 `single_process_lifetime_peak`，而非瞬时采样。任务以 `resource_memory` 结束，约 0.591 秒完成 UID 清理，客户端结果 `is_error=true`、`effect_state=not_committed`，验收确认项目没有发布 result/measurements。这轮正式执行链证据直接覆盖了此前可能漏掉的已释放短峰值。

36 次 Node/npm 调用全部成功，无重试；新包首个 Node 启动约 8.600 秒，helper 初次探针约 6.879 秒（记录 slow_start，仍在既定预算内），首次加载延迟未被本次改动解决。19 个任务使用 19 个不同 coalition，CPU 账本隔离通过；并发双向读写及信号拒绝通过，受限任务清理后另一任务心跳从 25 增至 28。普通及重新托管后台进程 CPU、持续内存、进程数、文件/描述符限制均通过。

19 个租约均仅启动一次、最终 clean、清理 issues 为空、无残留 UID 进程，broker 与恢复服务已卸载；报告后独立核对系统进程表的有效/真实/保存 UID，同样没有残留。stderr 仅有进度记录。本轮内存峰值补强验收完成；v4 未做新的整机重启，`reboot_verified=false` 如实保留，不继承 v3 的重启证明。观察前已退出并回收的进程及未采样到的跨进程聚合峰值仍不受保证，`ready_for_production=false`、`strict_aggregate_verified=false` 保持不变。

### 受保护 Git 对象只读原型（2026-11-09，待管理员验收）

新增 `backend/core/macos_git_read.py`：调用方持有已授权的 Rust `ReadRoot`，有界枚举只提供对象文件名，实际内容全部通过该原生根读取；拒绝符号链接、硬链接、特殊文件、跨卷内容、外部 alternates、promisor 和超预算对象。捕获结果最多 2048 个文件、32 MiB，总枚举最多 4096 项，单文件沿用原生 16 MiB 上限。生成带内容摘要的私有裸仓库，只搬运 loose/pack 对象及普通 pack 索引，不携带源配置、hooks、refs、index、工作区或凭据。捕获后源目录替换不会改变已复制的字节。

这一步仅支持完整 SHA-1 OID 的 `cat-file -t/-s` 和两个提交的 `merge-base`，并非任意仓库的原子快照，也不是正式 Git 读写入口。批量/blob 输出、历史服务接入、worktree、提交写入、网络传输、SHA-256、真实项目授权绑定和 frozen 打包均未宣称完成；原 `protected_git` macOS 拒绝分支、`macos_execution=false` 保持不变。此模块不能自行授予原仓库读取权，也不另建恢复账本；后续正式接入必须沿用原 Host 权威与 native invocation 记录。

本地新增 29 项测试通过；连同 broker、恢复与项目授权回归共 108 项通过。真实 Rust 读取覆盖对象链接拒绝、目录身份替换、捕获后替换、大小/数量上限、loose/pack 读取、提交共同祖先、严格参数限制及未清理时禁止结果接纳。本机普通 UID 的 Seatbelt 验证已运行：三个对象查询成功，目录外 canary 读写被拒绝，恶意配置/hooks 的标记未产生。这只证明该原型的 Seatbelt 路径，不能当作低权限 UID、管理员辅助服务或 frozen 包验收。

`scripts/check_macos_git_read.py` 复用现有 `execution_trial`、launchd UID 监督器与资源守卫，使用一次性测试仓库。正常路径须查询成功且 UID 清理完成；取消、超时和监督器崩溃路径先验证查询与脱离进程组的后台进程已启动，再注入故障，要求原 UID 无残留、launch_count=1、原因匹配、结果未接纳。管理员验收尚未运行，不计为通过。命令如下（无永久安装、无需重启，不接触真实项目；失败保留恢复证据）：

```sh
cd "/path/to/Gitgo-issue45-stage4"
sudo ../issue45-tools/macos-build-venv/bin/python -B -I \
  scripts/check_macos_git_read.py --run \
  > dev-artifacts/macos-helper/git-read-v1-report.json \
  2> dev-artifacts/macos-helper/git-read-v1-stderr.log
```

### Git 只读首轮管理员验收失败及源码服务修复（2026-11-09）

`git-read-v1-report.json` 的 `passed=false`：normal 用例未启动 Git，其他三项未运行。服务在导入时缺少 `macos_coalition`；租约为 prepared、generation=0、launch_count=0。源码版 `DarwinTrialProcess` 仍只复制 identity、execution_service 和 resource_guard 三个模块，遗漏后来加入的 coalition 依赖。此前 frozen 包携带该模块的证据不覆盖这条独立源码入口。独立进程表核对未发现该专用 UID 的真实、有效或保存 UID 残留；launchd 临时服务仍处于重复启动失败状态，不能将其算作已卸载。

现已统一用 `stage_source_service` 搬运四个模块，并以真实系统 Python 的 `-I` 模式作导入预检；预检发生在写入 prepared 状态和注册 launchd 之前。新增测试实际运行复制后的独立模块，删除 coalition 的旧布局可复现同一导入失败。关联原型、coalition、恢复与未启动租约收尾组合共 91 项通过。

管理员重跑入口增加显式 `--retire-unstarted-source-lease <lease-id>`，仅接纳同一 boot、从未开始、generation/launch_count 均为零且无 PID/started 的临时源码租约。先取得分配器、Host owner、service 锁，核验服务脚本内容、root 文件属性、实际 job 绑定和 UID 空集，卸载指定 job 并再次核对；归档原记录、保留 UID 占用与未执行证明，不修补或重放旧 job，不删除恢复记录。活跃、未知或不匹配状态拒绝收尾。旧 frozen/legacy 入口保持独立检查。

v2 管理员验收待运行，使用同一 Git 只读脚本加上述参数，输出 `git-read-v2-report.json` 和 `git-read-v2-stderr.log`。尚未证明低权限 Git 查询及四条生命周期路径通过，不开启正式能力。

### Git 对象只读 v2 管理员验收通过（2026-11-09）

报告归档 `git-read-v2-report-reviewed-7f2c02ff3f57.json`，SHA-256 `7f2c02ff3f572b72303879b97d1c681ab0584e12560c46595f2190073b9fc405`；stderr 归档 `git-read-v2-stderr-reviewed-014d043c56ee.log`。实际 staging 中的对象读取、源码启动、监督器、coalition、验收及旧租约收尾脚本与当前源码逐字节一致。四个用例均通过，Git 子任务 stderr 均为空，报告的 `passed=true`。

正常路径在独立低权限 UID 下完成 SHA-1 对象类型、大小及两个提交的共同祖先查询，同时拒绝工作区外 canary 读写；仅在退出成功且 UID 清理确认后接纳结果。源仓库配置/hooks 标记未执行，捕获后源元数据替换没有改变私有对象视图。取消、超时、监督服务 SIGKILL 三项在查询完成且脱离进程组的后台进程确实存活后注入故障，均拒绝结果接纳、完成 UID 清理。崩溃用例 generation=2、reason=automatic_recovery，但 launch_count 仍为 1，证明恢复没有重放任务；其他用例 generation=1。四个租约最终均为 clean、cleanup.complete=true、issues 为空、残留进程为零。

v1 漏模块留下的租约也已完成显式收尾：保留原始记录摘要与 never_started 证明，launch_count=0，job 已卸载，UID 继续保留占用。报告后另行查询系统进程表及 launchctl，四个新 UID 加旧 UID 的真实/有效/保存 UID 均无残留，五个临时服务均确认不存在；独立核对归档 `git-read-v2-independent-check-reviewed-e44634e91583.json`。

本轮完成的是有界对象只读原型的源码执行与生命周期验收。`production_backend_enabled=false`、`frozen_package_verified=false` 保留；不扩展为完整仓库历史、worktree/提交/推送、原子快照、真实项目正式授权绑定或发行包验收。正式 Host Git 接入及相应打包验证仍待完成。

## 暂缓的虚拟化实验

此前评估的路线是在 Apple silicon Mac 上运行独立 macOS 来宾，保留 macOS 命令与开发工具的系统兼容性。来宾工具链需独立准备和校验。Linux 来宾会改变工具执行环境，Intel Mac 也不在这条路线的支持范围内。

宿主需支持 Apple Virtualization，并为宿主辅助程序赋予相应 entitlement。开发探针使用临时的本地 ad-hoc 签名；发行包签名、辅助程序身份和真实加载对象仍需单独实现与验收。Objective-C 探针调用公开框架，不依赖开发机 Swift 与 SDK 模块版本恰好一致。

Apple 当前支持的映像可能要求至少 2 个虚拟 CPU、4GiB 来宾内存，以及体积很大的恢复映像。本地元数据查询给出实际要求，不能把框架通用内存下限当作 macOS 可启动下限。现有工具默认 512MiB 预算不足以容纳此类来宾；不得暗中调高用户任务预算。宿主开销、来宾基础开销与任务额度的产品约定和强制实施尚未完成。

## 已提供的开发探针

以下命令在源码 checkout 中使用可信 Python 运行。所有输出和 VM 资源只能留在被忽略的私有开发目录；不要提交系统映像、硬件模型、机器标识或原始诊断。

```text
python -B scripts/run_macos_guest_probe.py requirements
python -B scripts/run_macos_guest_probe.py configuration --metadata METADATA_JSON --output-directory PRIVATE_DIRECTORY
python -B scripts/run_macos_guest_probe.py install --restore-image OFFICIAL_LOCAL_IPSW --new-vm-directory NEW_PRIVATE_VM_DIRECTORY
python -B scripts/run_macos_guest_probe.py boot --vm-directory PRIVATE_VM_DIRECTORY
```

- `requirements` 仅查询 Apple 支持的恢复元数据，不下载或安装。
- `configuration` 创建稀疏测试磁盘，分别验证基础、通信设备和空只读共享目录的配置；不启动来宾。只有全部变体有效才返回成功。配置接受不证明来宾设备可用或共享目录能抵抗路径竞态。
- `install` 使用 Apple 恢复映像加载和安装 API，仅向新建的私有虚拟磁盘安装。已有目录不覆盖，失败安装不自动重试或当成可启动映像。它不安装到宿主系统。
- `boot` 仅接受安装完成的本地实验目录；框架启动后 45 秒强制停止，并检查返回状态。正常停止成功不证明宿主或辅助程序强制退出后同样能清理。

生命周期探针没有网络设备、宿主目录共享或命令执行入口。框架启动事件仍带 `guest_command_execution_tested=false`。安装完成也不意味着 Setup Assistant、可信代理或开发工具已配置完成，更不意味着文件、网络、资源或恢复攻击验收通过。非零退出和未执行的测试不能计为通过。

## 接入现有机制前必须解决的边界

| 边界 | 所需实现与验收 |
| --- | --- |
| 权威与对象 | Python 原权限、工具合同和 `native_invocations.v1` 保持唯一业务权威。宿主与来宾的文件对象身份不同，必须通过已核验的映射或代理连接，不能伪造相同 inode/device。 |
| 工作区与 Git | 不共享宿主 home 或宽泛目录。私有数据搬运、实际文件对象固定、并发替换和写入提交语义需实现；受保护 Git 的读写、凭据、仓库元数据仍走原授权与恢复链路。 |
| 通信与来宾权限 | 可信来宾代理需与普通工具用户分离；控制通道认证、帧大小、重放、断线和输出上限均需检查。设备配置通过不能代替真实来宾通信测试。 |
| 整树资源 | 来宾内脱离会话、派生子孙和嵌套工具均在约定预算内。vCPU 数不等于累计 CPU 秒；宿主控制进程用量不等于来宾整树用量。需要实际耗尽和观测失效时拒绝的证据。 |
| 取消与恢复 | 原生所有者负责 VM 生命周期；宿主退出、辅助程序被杀、启动中断和断电后仍能核对原调用。未知执行不重放，停止未确认不释放恢复债务。单次 stop 成功不能外推为完整恢复证明。 |
| 身份与打包 | Rust 与 Apple 框架辅助程序、来宾基础映像、代理和工具版本均需固定；旧授权不能消费替换后的实现。最后由真实 frozen 包执行同一路径，不能只跑源码探针。 |

不为赶进度增加无隔离回退，不以 `AUTO` 关闭边界，也不另建审批或持久恢复账本。真实 macOS 来宾验收需有支持虚拟化的宿主；GitHub 托管 macOS ARM runner 不支持嵌套虚拟化，普通云端构建只能作为部分证据。

公开报告应逐项列出正常执行、越界读写、网络、资源、超时、取消、子孙树和恢复结果。缺少任一适用证据时保持候选状态，Issue #45 与 #17 的完整目标继续开放。

参考：[Apple macOS 虚拟化](https://developer.apple.com/documentation/virtualization/virtualize-macos-on-a-mac)、[Virtualization entitlement](https://developer.apple.com/documentation/virtualization/adding-the-virtualization-entitlement-to-your-project)、[GitHub 托管 runner 限制](https://docs.github.com/en/actions/reference/runners/github-hosted-runners)。

### Git 对象读取接入项目辅助服务（2026-11-09，待管理员验收）

新增 Host 内部 `macos_broker_client.read_git_objects`：必须已有 operator
project grant、已注册项目身份、匹配该项目 `.git/objects` 的 Rust `ReadRoot`。
它不是工具 JSON 入口，不自行获取读取批准；不支持链接工作树、外部对象库、
任意仓库路径。原始项目身份不能因这次读取而被创建，已有 Pipeline 上下文
若属于另一项目则拒绝。对象目录枚举与 Rust 读取的边界沿用 v2 原型。

`git_read` 使用同一 kernel peer UID、项目目录 FD、grant、调用去重账本、
`NativeInvocationRecord`、UID/Seatbelt/launchd 生命周期以及恢复状态查询。
执行依据包含精确查询和对象副本摘要，远端回执必须匹配原调用 ID 和副本摘要。
取消、断线、清理不完整、输出格式异常或最终调用记录提交失败时，不返回成功结果；
断线后保留原调用记录，不自动重放。候选工作区命令与 Git 读取共享传输/清理
回执处理；Git 分支不调用 workspace exporter，不把任何候选输出写回原项目。

Root 仅将通过严格对象文件名、摘要和既有 broker 配额检查的字节写入新建私有目录，
执行固定 `/usr/bin/git` 查询；不打开请求指定的项目内容路径。传输仍限制为
128 文件、4 MiB（小于独立原型 2048 文件、32 MiB），不放宽已有协议上限。
Git stdout/stderr 各限 4096 字节，成功输出另按对象类型、十进制大小或 40 位
merge-base OID 验证。配置、refs、hooks、index、alternates、promisor、网络、
Git 写入和生产 protected_git 后端均未开放。

本地验证：Git 原型/新 broker 入口、既有 broker/recovery/project authority、
native invocation recovery、workspace candidate service 相关测试共
187 passed / 3 skipped；语法检查和 `git diff --check` 通过。
其中 transport 的 root peer 使用测试替身，不能算真实低权限执行验收。

新管理员脚本 `scripts/check_macos_git_broker.py --run` 使用暂存源码运行时、
一次性项目和临时 launchd broker。普通登录 UID 执行三次真实查询，并验证：
原项目内容和权限不变、恶意源配置/钩子未执行、原始 Host journal 与 broker
调用账本一致、重放/错误项目/扩展命令拒绝、每个 UID 只启动一次且已清理、
临时服务退出。失败保留恢复证据，不擅自清除未知 UID 租约。
此脚本尚待用户运行；新入口的真实故障注入、冻结包验证与正式 Git 消费者接入
仍需后续验证。之前 v2 的取消/超时/guardian 崩溃验收仅覆盖独立原型，不外推
为本次 broker 入口也已完成同等真机验收。`macos_execution` 仍为 false。

### Git 只读辅助服务 v1 管理员验收通过（2026-11-09）

用户执行 `check_macos_git_broker.py --run` 后，报告顶层及 case/client
均为 `passed: true`。实际客户端为普通登录 UID 501，经 root broker 分配
三个不同任务 UID；对象类型、对象大小、两提交 merge-base 三次查询匹配。
重放、错误项目 FD、未开放命令均拒绝，原项目内容/权限未变，源配置和钩子
未执行。三条 Host 原始 invocation journal 与 broker 账本一致，全部退出码 0、
cleanup complete、generation 1；每个租约 launch_count 1、phase clean、
observed_empty true、issues/remaining 为空。报告与客户端服务 stderr 无异常；
last-error 中 `GIT_OBJECT_VIEW_UNSUPPORTED` 来自预期的未开放命令拒绝测试。

独立只读主机复核：UID 202981190、208213183、202632001 未出现在进程的
有效/真实/saved UID 字段；三个 execution-trial 和一个 user-broker-trial
的 launchctl 查询均明确返回服务不存在。实际暂存运行时的五个实现/脚本文件
与工作区及验收前源码摘要一致（测试文件不纳入暂存运行时）。

原报告已保留不可覆盖的摘要命名副本，SHA-256：
`9de0bfa66b302f24859b5263636a29fd681bfdd5f21c4ba8e06d45ab2f5e6a04`。
报告、stderr、源码摘要和独立主机复核记录位于 ignored 的
`dev-artifacts/macos-helper/git-broker-v1-*-reviewed-*`，不提交本机验收私有路径。

这次证据确认普通用户到辅助服务的固定对象查询正常链路及拒绝边界。
它尚不覆盖新 broker Git 入口的取消/超时/崩溃真机故障注入，也不覆盖冻结包。
正式 Git 消费者、写操作与生产 capability 仍未启用。

### Git broker 故障链验收准备（待管理员执行）

新增 `scripts/check_macos_git_broker_faults.py`，覆盖 cancel、timeout、
owner_disconnect、broker_crash、guardian_crash 五条路径。为避免短查询无法
稳定命中故障窗口，仅在暂存测试 broker 的内存中包装原来的 sandbox_popen：
先执行 broker 生成的真实固定 Git 查询、输出 `blob`，再运行有 45 秒上限的
测试 leader 和 setsid/double-fork 后台进程。正式 broker 协议、配置、环境变量
均未增加测试开关；该包装器拒绝持久安装路径及非原始固定 Git 命令。

故障前必须观察到真实 Git 结果、目录外读写被 Seatbelt 拒绝、任务 UID 下
leader 与后台 PID 同时存活且属于不同 session/process group。标记原子发布；
缺少就绪证据不能把一次快速退出当作故障验收成功。故障后核对 UID 空、
launch_count 仍为 1、原项目和目录外 canary 未变、结果未交给 Host 调用者。
监督进程崩溃必须 generation 2 / automatic_recovery；调用端或 broker 崩溃后，
恢复客户端以普通 UID 查询同一原始 invocation 的清理回执，验证旧证据保留、
核对幂等、业务结果仍 unknown、新请求被 pending evidence 阻止且不重放。
未知 UID 状态保留恢复服务。所有测试仅使用一次性项目及临时 launchd job。

准备过程中补强了最终结果交付时的取消边界：在收到 terminal frame 后再次检查
取消/截止时间，Git adapter 在原始 journal 提交后再次检查取消，避免取消恰在
回执接收或记录提交期间发生时仍交付成功。对应两个竞态回归测试已加入。

本地 Git/broker/recovery/故障脚本相关 152 项通过。另用普通用户直接执行
真实 Seatbelt + 同一故障 fixture 冒烟：Git 实际返回 blob，目录外 canary
读写被拒绝，后台进程确已脱离 session/process group，stderr 为空；仅清理
该测试创建的两个私有进程组。此冒烟不证明专用 UID 或 root broker 故障恢复。
验收前源码摘要及普通 UID 冒烟记录保存在 ignored 的
`dev-artifacts/macos-helper/git-broker-faults-v1-*`。五项管理员实测及冻结包
验收仍待完成；正式生产 capability 保持关闭。

### Git broker 五项故障管理员验收通过（2026-11-09）

`git-broker-faults-v1-report.json` 顶层及五项 case 均通过：cancel、timeout、
owner_disconnect、broker_crash、guardian_crash。每项注入前均已取得真实 Git
`blob` 输出、目录外读写拒绝证据，以及同任务 UID 的 leader 与已脱离 session/
process group 的后台进程存活证据；没有把未启动或提前退出当作故障成功。

取消和超时均向调用者报告相应失败，未交付已经产生的 Git 输出。
超时由 Host 截止时间触发取消，因此底层 reason 为 cancel，Host 分类仍为
`tool timed out`，这是本次实测的具体路径，不是对独立 supervisor 超时触发的证明。
调用端 SIGKILL 和 broker SIGKILL 后，普通 UID 恢复客户端核对原调用的清理
回执成功，旧证据保留、重复核对幂等、结果仍 unknown、后续执行受 pending
记录阻止，未伪造完成历史。guardian SIGKILL 后 generation 为 2、reason 为
`automatic_recovery`。五项 launch_count 均为 1，seen 均仅有原始调用 ID。

五项最终 phase clean、observed_empty/quiescence_observed true、issues 和
remaining 为空，原项目及目录外 canary 未变；客户端/恢复客户端/服务 stderr
均无异常。last-error 中 Wrong peer user 是就绪探针故意以未获授权的 root
身份连接后被拒绝的预期证据。独立主机 ps 对 effective/real/saved UID 的复核
未发现五个任务 UID 的任何进程；十个临时 launchd job 的查询均返回 113，且
明确表示服务不存在。14 个暂存实现及脚本文件与验收前摘要、当前工作区一致。

原报告 SHA-256：
`af096bb971ee1cbf767007a60ff713ab2e5d339188d9f5a050c162e341c3feb8`。
报告、stderr、源码摘要及独立复核已在 ignored 的 dev-artifacts 中按摘要归档。
两个崩溃案例的 unknown journal 保留在一次性验收项目状态里，不删除或当作
成功退休；UID 清理和业务结果确认是不同结论。

本轮使用暂存源码和显式测试 launch wrapper，证明真实 Git 输出后的故障与
后台进程生命周期接入；不声称未包装的自然慢 Git 负载或冻结安装包已通过。
下一步仍需冻结包验证、正式 Git 消费者接入等，生产 capability 保持关闭。

### Git broker 冻结包验收准备（2026-11-09，管理员实测待执行）

新增 `scripts/check_macos_packaged_git.py`，通过现有 helper installer 安装显式
允许的本地 ad-hoc 包到独立的 root-owned 内容寻址版本，然后顺序执行正常
三查询与五项故障验收。普通 UID 客户端及恢复客户端使用该 frozen Host 的
Python role；正常 broker 使用正式 `macos-broker-service` role；任务监督器
必须使用同一个已安装 Host 的 `macos-execution-service` role。故障测试只在
独立临时 broker 的内存中注入既有 launch wrapper，不新增生产测试入口。

验收报告记录客户端和故障 broker 的后端模块加载位置，拒绝混入源码目录；
核对每个任务 job 的精确 ProgramArguments，并在测试前后重新验证安装包
库存身份。现有 Git 查询、权限拒绝、journal、后台进程清理和 unknown
恢复断言全部保留。失败停止后续案例并保留原租约/恢复证据。

本地准备已完成：相关原有测试 55 项及新增冻结证据/错误入口拒绝测试
15 项通过。从当前源码摘要副本构建新产物，避开不可覆盖的旧 Rust 暂存库，
使用独立 PyInstaller 缓存；没有替换旧安装。正式打包路径、ad-hoc codesign
校验、冻结 Rust object reads、Host/Daemon/tool-runner smoke 均通过。
另在源码路径之外以冻结 Host 加载验收脚本，三个固定 Git 查询通过，所有
受检后端模块位于包内。以上普通用户检查不等于 root helper 验收。

产物及预检摘要保存在 ignored 的 `dev-artifacts/macos-helper/git-broker-packaged-v3*`。
待用户运行管理员命令后再核对八个任务 UID、临时服务退出和原始回执。
本次安装仅添加隔离 helper 版本；不切换现有常驻服务、用户 Gitgo 或 PATH，
无需重启。正式 Git 消费者、写协议、发布者信任策略及生产 capability 未启用。

### Git broker 冻结安装包管理员验收通过（2026-11-10 复核）

用户执行 `check_macos_packaged_git.py --run --allow-local-adhoc` 后，正常与
故障子报告退出码均为 0、passed/frozen_package_verified 均为 true。
已安装 helper 的内容寻址身份与预检一致，测试前后库存不变；独立复核再次
确认 root 控制的安装目录、完整 sealed inventory、Host SHA-256 和严格
codesign 校验均通过。七个暂存脚本和 742 个后端输入文件与验收前摘要一致。

普通登录 UID 501 的真实冻结客户端完成对象类型、对象大小、merge-base
三次查询，重放/错误项目/未开放命令均拒绝，原始 journal 与 broker 账本一一
对应。正常 broker 使用正式 frozen service role，八个任务的 supervisor 均
使用同一安装包的 frozen execution-service role；12 份客户端、故障 broker
及恢复客户端运行时记录均显示受检后端模块来自包内。

cancel、timeout、owner_disconnect、broker_crash、guardian_crash 五项通过。
每项故障前已产生真实 Git 输出、证实目录外访问被拒绝，并观察到同任务 UID
下脱离 session/process group 的存活后台进程；故障后输出未被作为成功交付。
guardian 自动恢复为 generation 2，其余 generation 1，全部 launch_count 1。
调用端/broker 崩溃的两项恢复均保留原证据，清理回执确认、核对幂等、结果
仍 unknown、后续 admission 被阻止且未伪造完成历史。未知业务结果记录保留。

全部八个 UID 最终 clean、observed_empty true、issues/remaining 为空。
正常短查询没有剩余进程可冻结，因此 stop_rounds 0 / quiescence_observed
false 是预期值；五项后台故障均 stop_rounds 1 / quiescence_observed true。
独立主机 ps 对 effective/real/saved UID 复核无残留；八个任务 job 和六个
临时 broker job 的 launchctl 查询均返回 113 并明确表示服务不存在。
源码项目和 canary 均未变化，客户端/服务错误输出为空。last-error 仅包含
预期拒绝探针；首个正常 broker 冷启动就绪约 15 秒，未将其当作请求执行时间。

报告 SHA-256：`788c468973718307e816b0ec303b0b6263c9193e9aae8b65856d4db054bdcf35`。
报告、stderr、预检摘要、源码/安装包核对及独立主机检查均已按摘要归档在
ignored 的 `dev-artifacts/macos-helper/git-broker-packaged-v3-*-reviewed-*`。

本轮确认本地 ad-hoc 冻结安装包的固定 Git 只读入口及五项生命周期故障链；
故障仍使用显式测试 launch wrapper，超时仍是 Host deadline 触发底层 cancel。
不扩展为发布者签名正式发行、常驻服务升级后全量验收或正式 Git 消费者接入
已经完成。正式 Git 消费者、写协议与生产 capability 继续保持未启用。


### 正式 Git 消费者的有限接入及集中验收准备（2026-11-10）

第四阶段工作区已按内容对齐第二、三阶段最新 head：第二阶段
`d667c1f9995f774b9ee2115384a9f8a38666abb6`、第三阶段
`fabd06c8faf2f712b4c560cfd10841d9c2cae8da`。从共同基线的 97 个文件以
三方内容合并接入，未发生冲突；保留当前第四阶段未提交改动，并留存前后摘要。
这是工作区内容对齐，不表示已完成 Git 历史合并、提交或 PR。

新增 `loop.git_macos`，在显式 operator broker 上下文内将
ProtectedLocalGitRunner、LocalGitRunner 和 AgentWorktreeManager 的固定对象
查询接到低 UID broker。传递原 task cancellation，禁止 stdin、身份覆盖、
任意 revision 表达式和未支持命令；绑定上下文中不回退到裸 subprocess。
未绑定的旧 LocalGitRunner 路径仍保持原行为，不能视为全局启用了 macOS 隔离。

对象视图新增精确 `merge-base --is-ancestor <40-hex> <40-hex>`，仅允许无输出的
0/1 返回码；1 表示否定关系，不能吞掉超时、取消、清理失败或其他非零返回。
调用失败无法确认是否执行时保留 command_started=null，以原 journal 为准。
第二阶段新增的 read_roots/metadata_roots 在当前 macOS 执行策略里明确拒绝，
不忽略权限声明或隐式扩大可读范围。

当前快照本地回归：615 passed、22 skipped、30 subtests passed；Rust 单元测试
11 passed。构建中的 Dashboard 定向测试、冻结 Rust 读取与 Host/Daemon/
tool-runner smoke 通过。从源码目录之外由新冻结 Host 加载预检，五种真实
对象查询（含祖先真/假）通过，十个受检后端模块均来自冻结包；库存摘要和
严格 codesign 校验通过。这些结果不替代管理员身份隔离验收。

安装包位于 ignored 的 `dev-artifacts/macos-helper/git-consumers-v1`；构建使用
独立源码快照及新 Rust 暂存库，避免覆盖现有已映射的 native 文件。包身份为
`ed47efcc983767e11e90014cc034e21d39fdcec9804ca52380da92fefa0b3add`。
源码、native、包库存、构建日志、回归日志及冻结预检均保存在同名前缀下。

明日集中入口 `dev-artifacts/macos-helper/run-git-consumers-v1.sh` 先核对摘要，
再请求 sudo。使用与安装包一致的源码/native 快照，依次执行：原三查询加
七个真实消费者调用、五项故障清理/恢复、独立常驻服务安装/KeepAlive 重启/
升级/失败候选回退/卸载。第一项失败便停止后续套件；报告每次使用新文件，
保留恢复记录。无需重启 Mac，不替换当前 Gitgo 或现有常驻服务。
`--check-only` 只做普通用户摘要预检。尚未执行本批管理员验收。

仍未完成：完整 Git 工作树/发布/写操作协议及其权限验收、发布者信任配置与
正式发行验收。当前仍是有上限的 SHA-1 固定对象读取；linked worktree、SHA-256、
外部对象存储和通用仓库读取未开放。生产 macos_execution capability 保持关闭；
瞬时总内存硬上限也不因本批测试而获得保证。


### 正式 Git 有限消费者及常驻服务集中管理员验收通过（2026-11-10）

用户完成 `run-git-consumers-v1.sh`，报告 `git-consumers-v1-report-Iwt0ju`
三套件均退出 0、passed=true，consumers/with_lifecycle=true，安装包未变化。
报告 SHA-256：`8189456273ea53694cc5ec02ea983567048e639f47b8ffaf9fdf9cad9e383717`。

正常组十次查询全部匹配，其中七次经真实消费者；原 journal 与 broker 账本
一一对应，九次返回 0，祖先关系否定返回 1。三种越权/重放探针拒绝，源项目
不变，hooks/config 扩展未执行。正常 broker 和任务 supervisor 均使用已安装
冻结 Host；12 份 Git 客户端/故障 broker/恢复客户端证据的十个受检模块来自包内。

五种故障 cancel、timeout、owner_disconnect、broker_crash、guardian_crash
全部清理成功，脱离进程组的子进程实际存在后才触发故障。输出没有被误接纳；
调用端/broker 崩溃后的 unknown 记录保留，恢复幂等、无重放、未伪造完成历史。
guardian 恢复为 generation 2，其他为 1，所有 launch_count 仍为 1。
故障测试使用显式 launch wrapper，timeout 是 Host deadline 触发底层 cancel。

常驻服务组验证独立服务安装、SIGKILL 后 launchd 自动拉起、运行中拒绝升级和
卸载、取消并清理后台进程、切换至内容不同的版本 B、坏候选启动失败恢复 B、
维护中断恢复不重放、授权过期拒绝、卸载幂等和证据保留。五次任务均成功清理。
B 是同一实现增加资源标记的测试版本；不是任意跨版本兼容性声明。失败候选的
TimeoutError 是预期故障注入，随后旧版本执行通过，并非本轮验收失败。

独立普通用户只读复核：20 个任务 UID 的 effective/real/saved UID 进程均无
残留，27 个任务/broker/常驻 launchd job 均明确不存在。A/B 两版 root 控制的
完整 sealed inventory 与内容寻址目录匹配，严格 codesign 通过；A 的 Host
摘要匹配构建预检。753 个暂存后端/验收输入文件匹配源码摘要。root 私有安装
回执未由普通用户重新读取，安装器回执验证采用本次管理员报告的结果。

报告、stderr 和独立检查按摘要归档为 ignored 的
`git-consumers-v1-*-8189456273ea.*`。未知业务结果和恢复证据未删除。
本次通过有限固定 Git 对象消费者及独立服务生命周期验收；完整 Git 工作树/
发布/写协议、发布者信任与生产 capability 仍未完成，不据此开放生产能力。


### 受保护 Git 写入基础：仅追加不可变对象（2026-11-10，待管理员验收）

新增 `macos_git_write` 和 `git_objects` broker 操作，经真实
ProtectedLocalGitRunner / git_macos 进入原 native invocation journal。首批仅允许
精确 `hash-object -w --stdin`、`mktree -z`、带显式作者与时间的 `commit-tree`。
输入最大 1 MiB，延用有限 SHA-1 对象视图；树条目拒绝符号链接、gitlink、
路径穿越、受保护名字和大小写冲突。不能执行任意 Git 参数、hooks、filters、
外部配置、网络、refs/index/工作文件修改。

特权 broker 只在自己新建的私有目录中暂存已校验对象，将该副本的对象目录
交给独立低权限 UID。Git 在原 Seatbelt/资源限制/UID 清理链内生成对象。
只有原调用绑定、退出状态、清理确认、视图摘要、原对象未变及新 loose 对象
解压边界/类型/长度/内容哈希全部通过，普通 Host 才用固定对象目录句柄写回。
写回先 fsync 临时文件，再用 Darwin exclusive rename 发布，已存在的对象只
允许完全一致，不覆盖。异常时可能遗留不可达的已验证对象；不代表分支更新，
不重放未知结果，不自动删除恢复证据。尚未实现对象回收事务。

补正 DarwinTrialProcess 的二进制管道：text=False 保留原始字节，避免把 blob
当 UTF-8 文本；显式 encoding/errors 按文本模式处理。BoundedCommunication
没有输入时只关闭 stdin，空 bytes 不被转换成 str。真实管道测试覆盖空输入、
NUL、非 UTF-8、CRLF、UTF-8 文本和显式编码；模拟 launchd 不能证明身份隔离。

验收脚本新增 --objects：普通用户经冻结 Host 生产 blob/tree/commit 并校验写回；
源项目原有内容/模式/所有者必须不变，仅允许新增内容寻址对象。五项故障使用
已经实际生成对象并派生后台进程的显式测试 wrapper，验证取消、超时、调用端
断开、broker 崩溃和 guardian 崩溃后不接纳输出、源项目未变、后台进程清空，
保留 unknown 记录。仍需管理员在真实辅助服务上执行，普通用户测试不能替代。

完整工作树的物化/授权、index 与工作文件写回、带预期旧值的 refs 更新、
CandidateGit 发布链仍未接好；当前只是这些写流程的基础，不是第四阶段完成。
生产 capability 继续关闭，现有正式安装和用户项目不因这批测试而改变。

本轮普通用户定向回归 58 passed（随后 stdin 无输入关闭逻辑纳入扩大回归）。
扩大回归 587 passed、52 skipped、30 subtests passed、15 failed。失败全部位于
`test_agent_dag_worktree.py`：完整工作树消费者尚无已授权的 macOS 执行链，
不允许 unrestricted fallback；先前 git-consumers-v1 快照也复现首项相同失败。
这些失败是明确的未完成工作，不跳过后宣称全套通过。

最终验收包 `git-objects-v2` 使用独立源码/native 快照，bundle identity 为
`69d6bef52789f898a7c65f97d03646f026ff6cd982b2c275f49cba0c9889b2d0`。
正式打包、14 项 Dashboard 定向检查、冻结 Rust 读取及 Host/Daemon/tool-runner
smoke 通过；新冻结 Host 的五次固定对象查询、blob/tree/commit 生成与 Host
追加校验、真实二进制管道适配器预检通过。管道预检模拟 launchd，不能替代
真实低 UID 服务验收。严格 codesign 和源码/包摘要预检通过。

集中验收入口为 `dev-artifacts/macos-helper/run-git-objects-v2.sh`，先核对
2027 个快照输入、Rust 库和包库存，然后请求 sudo。正常组含原十查询及三种
对象生成，之后执行五种写入故障清理；第一套失败便停止，不重复生命周期
验收，不需要重启 Mac，不替换当前 Gitgo。安装独立内容寻址的 ad-hoc helper
版本，任务使用临时项目和临时服务。报告每次新建，保留恢复证据。当前尚未
运行这批管理员验收。旧 git-objects-v1 包未包含最终二进制适配器修复，不用于
此次验收。

上批 git-consumers-v1 普通用户独立复核脚本修正五个故障 broker 的 job 标签
前缀后重新执行通过，实际 27 个 job 均不存在；管理员原始报告未变。


#### 写入验收包修订 v3：保留固定提交身份（2026-11-10）

v2 尚未进行管理员验收。进一步启动链核对发现通用 sandbox_environment 会过滤
Git 作者/提交者变量，导致低 UID commit-tree 失败；因此 v1/v2 都不作为可运行
的管理员验收入口，后续使用 git-objects-v3。Darwin 执行适配器新增精确验证：
只有固定 /usr/bin/git、当前私有 object-view、完整安全参数和已校验 commit-tree
请求可携带六个作者/提交者字段，两个身份必须一致。其他 Git 环境变量仍清除，
不会给普通命令扩大环境白名单。测试通过实际执行环境生成提交并核对作者与
提交者内容，另覆盖错误程序/路径/选项、缺失/不一致身份等拒绝情形。管理员
验收同时核对树和提交的预期内容哈希，不以返回了 40 位字符串作为成功依据。

修订后定向测试 87 passed；共用 macOS 执行链、Git 消费者与恢复记录回归
591 passed、52 skipped、30 subtests passed。此回归不含完整工作树 DAG 套件；
上文 15 项未完成测试仍未解决，保留 v2 日志，不据此宣称全套通过。
v3 正式打包和冻结 smoke 通过；冻结 Host 五次对象查询、三种对象生成/追加、
提交作者/提交者内容、二进制管道及实际待启动状态中的身份字段均通过预检。
这些普通用户预检仍不证明真实低 UID 隔离，管理员验收待运行。

v3 bundle identity：
`69cf219e0580b1a3b03d03d0d37019e94e461183dede637ead5a6427f35f2c20`。
源码/native/库存摘要与严格 codesign 通过。入口
`dev-artifacts/macos-helper/run-git-objects-v3.sh`，同样先做完整性核对、再请求
sudo，正常 13 次调用后执行五种写入故障；无需重启、不替换当前 Gitgo。
v2 运行入口已阻止执行并标注 superseded，旧证据保留。


### Git 对象写入及故障清理管理员验收通过（2026-11-10）

用户完成 `run-git-objects-v3.sh`，报告 `git-objects-v3-report-irnDbH`：正常与
故障两组 exit_code=0、passed=true；objects/consumers=true，with_lifecycle=false，
production_enabled=false。报告 SHA-256：`e065ddbc8af21c99fe6c3c424d497a30b76e729ee2a4905a220926f14d992f94`。
包身份仍为 `69cf219e0580b1a3b03d03d0d37019e94e461183dede637ead5a6427f35f2c20`，
Host 摘要与预检一致。运行的是 root 控制目录内实际安装的冻结辅助服务。

正常组 13 次调用全部匹配：原十次对象读取/消费者调用，加二进制 blob、tree、
commit 三类生产及普通 Host 校验追加。12 次返回 0，祖先关系否定返回 1；
原 journal 与 broker 账本一一对应。重放、错误项目、未开放命令均拒绝。
source_unchanged=false 是预期结果：仅新增已验证的不可变对象，原有项目内容/
权限/所有者不变，未执行 hooks/config 扩展，也未创建 data.bin 或更新引用。
普通用户独立重新读取三份落盘对象，解压后逐字节核对输入、树结构、提交作者/
提交者及时间，并重新计算 SHA-1；所有对象由普通用户持有。

五种故障 cancel、timeout、owner_disconnect、broker_crash、guardian_crash
均在实际生成私有对象并派生脱离进程组的后台进程后触发：源仓库和外部 canary
不变，失败对象未写回，所有 UID 清理确认。调用端断开和 broker 崩溃后的原
unknown 记录保留，恢复幂等、后续业务准入仍阻止、无伪造完成历史；guardian
自动恢复为 generation=2，launch_count 仍为 1，未重放命令。故障测试使用显式
launch wrapper；timeout 是 Host deadline 触发底层 cancel，不把 8.23 秒等待
超时计作纯清理时长。日志中的未开放查询/Wrong peer user 是预期拒绝探针。

普通用户独立只读复核：18 个任务 UID 的 real/effective/saved UID 进程均无残留，
24 个任务/临时 broker launchd job 均明确不存在。sealed inventory 与包身份匹配、
严格 codesign 通过；12 份运行证据的 11 个后端模块来自冻结包，754 个
暂存后端/验收输入与构建源码摘要一致。root 私有安装回执采用管理员 harness
结果，普通用户没有绕过权限读取。原始报告、stderr、独立 audit 均按摘要归档，
恢复记录保留，未清除未知业务结果。

本轮通过的是 Git 对象写入基础链路及五项故障清理。完整工作树物化/授权、
index/工作文件回写、带预期旧值的分支引用更新与 CandidateGit 发布尚未完成；
先前完整工作树 DAG 的 15 项失败不因本次验收而算通过。生产 capability
继续关闭，尚不声明第四阶段或完整 Git 工作流完成。

### 工作树内容准备：git-checkout-v1（管理员验收通过）

固定的 `git_checkout` broker 操作接受已有项目 grant、Rust 有界对象快照和
一个完整 SHA-1 commit；不接受目的路径、自由 Git 参数或环境变量。固定
Python producer 与 `/usr/bin/git` 子进程共同位于原有低权限 UID/Seatbelt lease，
通过 `cat-file commit`、`ls-tree -r -z`、`cat-file blob` 导出原始对象内容。
不复制源 config/hooks/index，也不运行 attributes 指定的 filter 或编码转换。
普通文件、可执行文件、嵌套目录和空提交在当前有界范围内支持；符号链接、
submodule、保留元数据名称、大小写/Unicode 别名、过深目录及超限输入明确拒绝。
支持范围仍为单次最多 128 个文件/4 MiB，commit 原文最多 64 KiB；不声明
任意仓库大小或所有 Git checkout 语义。带显式空子树的非常规提交会因完整树
校验不一致拒绝，不会悄悄丢弃内容后接纳。

root 仅在启动前写入自己新建的目录；任务结束后仍由降权 exporter 读取
任务产物。Host 先等待绑定的 UID 清理回执、核对视图/commit、校验 commit
内容哈希，再从全部文件字节和模式递归重建 Git tree 哈希，与 commit 的 tree
匹配。Host 自行生成无 stat cache/扩展的 v2 index，不接纳任务提供的 index。
取消、未知结果、清理不完整、journal 写入失败都不能交付内容，未知调用不重放。

本轮只交付经过校验的内存镜像，**尚不向真实项目或正式工作树写入**。
linked-worktree 登记、目录对象与 WorktreeLease 绑定、原子发布/清理恢复、
完整 index/patch/commit/ref 流程仍属于既定第一组后续工作，不算新增阶段。
必须先确认新的冻结 Python producer → Git 子进程链在真实 helper 中可启动、
能隔离和清理，再将此镜像接入持久工作树状态转换。

本地验证覆盖实际 Git 的松散/pack 对象、二进制/执行位、原始属性语义、Host
生成 index 的实际 `write-tree` 核对、篡改/别名/超限拒绝与原 journal 故障。
打包验收选项 `--checkout` 在 normal 中增加空/非空镜像两次调用；五项故障
改为 checkout producer，注入前要求真实文件已生成且脱离会话的后代仍存活。
故障是显式 harness 注入，不冒充自然发生的慢 Git；之前对象写入故障的 v3
已通过证据独立保留。此批不重复常驻服务 lifecycle，不替换正式 Gitgo 安装。

2026-11-10 管理员运行与普通用户独立复核完成。原始报告
`dev-artifacts/macos-helper/git-checkout-v1-report-V44djU`，SHA-256
`d11967251701ff14cfd95939b6bbee2a6504565f3e5dd99134f26319f8c64c5b`。
包身份 `74b7821c0d67aa3ab707df3068e8d611ed463bb4e1cdad91dc3436cf78c9a0ad`，
实际运行 `/Library/PrivilegedHelperTools/org.gitgo.sandbox-v1/<包身份>/runtime/gitgo-host`。
normal/faults 均 exit=0、passed=true、frozen_package_verified=true；安装包未变化。

正常 15 次调用（10 次对象查询、3 次对象生产、2 次 checkout）结果及原 journal
对应一致；ancestry=false 的一次 exit=1 为预期答案。空提交与含二进制文件的
提交均导出正确，两个 Host index 摘要独立重算匹配。正常组只增加三份已核对
内容/哈希的 Git 对象；原文件未被覆盖，未创建正式 worktree 或 data.bin。
重放、错误项目、未开放命令均被拒绝。故障组在真实 object.txt 已生成且
detached 后代仍存活后注入 cancel/timeout/owner_disconnect/broker_crash/
guardian_crash，五项均清理成功、源项目及外部 canary 不变、结果未交付。
两项断连/崩溃恢复保留原 unknown 证据、幂等核对、阻止继续准入，未重放或
伪造成功历史；guardian generation=2 且 launch_count=1。timeout 为 Host
deadline 触发底层 cancel，等待 deadline 的时长不算纯清理耗时。

独立只读审计确认 20 个 UID 的 real/effective/saved UID 无残留进程，26 个
正确命名的临时 launchd job 均不存在；sealed inventory、Host 哈希、严格
codesign 与预检包一致。12 份运行证据均含 12 个冻结后端模块；755 个暂存
backend/scripts 输入与构建摘要匹配。root 私有安装回执仍以管理员 harness
校验为依据，普通用户复核没有绕过权限。按报告摘要归档 report/stderr/audit，
审计脚本 `dev-artifacts/macos-helper/audit-git-checkout-v1.py`。

此结果仅关闭「工作树内容准备」验收点，生产 capability 仍关闭；完整工作树
登记、权限绑定、发布与清理恢复仍未验收，先前完整工作树 DAG 的 15 项失败
不因此变为通过。下一步接入已有 WorktreeLease 生命周期，不新增授权账本。

### 正式工作树登记与恢复：git-worktree-v1（待管理员验收）

在已有 WorktreeLease/项目 SQLite 与原 native_invocations journal 中接入
macOS 生命周期，不建立第二套授权账本。显式项目 grant 下的不可变 commit
先走上一批已验收的 checkout producer；普通 Host 核对完整镜像后，在项目外
私有 staging 目录写入文件和自己生成的 index，再以排他 rename 发布。
`.git/worktrees/gitgo-<随机值>` 登记固定 HEAD、commondir、gitdir、locked 与
index；WorktreeLease 持久化项目、父目录、工作树与元数据目录的 dev/ino/uid。
登记与后续转换使用同一个 Host flock，禁止并发覆盖相同 process 的登记。

受保护 Git consumer 的固定 HEAD/common-dir 查询和对象查询/生产可从已经
登记、身份和元数据核对一致的工作树发起。任务 scope 必须拥有该 process
的 lease；伪造 `.git` 指针不建立权限。对象 producer 仍只获得原项目的私有
对象视图，保留原项目 journal 与 execution/call/session/process/task 关联。
不同项目的 storage 或任务身份不能借用该工作树。这个绑定没有开放任意命令、
任意写路径或修改用户分支；普通工具执行视图仍需既定后续接入验证。

登记后的状态依次 reserved/building/prepared/published。创建中断时仅清理
已记录的目录对象，不重新发起 Git 调用或恢复任务执行；删除先记录意图，
再把同一目录对象排他改名到 retirement 目录，断点恢复保持原删除意图。
删除不跟随目录中的符号链接；已完成删除再次恢复不改写状态。路径对象被
替换、身份缺失、原 native journal 有未确认债务时保留记录并阻止清理/使用。
特别是 mkdir 成功但目录身份尚未落盘的窗口会明确保留现场，不能仅凭随机
名字认领或删除。此情况需要人工核对，不能宣称所有中断均自动清理成功。

本批支持从显式 snapshot_commit/review_commit 创建有界工作树，复制固定
运行元数据；未支持脏根目录 snapshot、upstream patch、resume、受保护 index
更新、seal/promote/ref 发布。它们仍属原第一组 Git 工作流剩余项。此前完整
工作树 DAG 的失败不因此计为通过，生产 capability 保持关闭。

管理员 harness 新选项 --worktrees：正常 23 次真实 broker 调用（原 15 次加
1 次工作树创建、1 次已绑定任务的对象查询、6 次 Host 崩溃夹具创建），核对
原 journal、真实冻结模块及全部 UID/临时 job 清理。Host 崩溃由独立普通用户
进程 os._exit(86) 注入，分别覆盖四个创建边界、删除改名后、身份未落盘窗口；
最后一项必须保留并拒绝自动恢复，其余可确认对象须清理且不重放。另保留五项
checkout cancel/timeout/断连/broker crash/guardian crash 的真实服务故障验收。
本轮只操作隔离测试项目和内容寻址 helper 版本，不替换正式 Gitgo 安装。

2026-11-10 git-worktree-v1 管理员报告 `git-worktree-v1-report-M3XsOZ`：
正常 23 次调用与 7 个工作树场景均通过；包括登记、发布、跨任务/伪造指针拒绝、
四个创建边界与删除中断的幂等恢复、身份未落盘时保留现场。真实 frozen helper
模块、原 journal 和工作树权限绑定均核对通过。但整批 **未通过**：第一个
cancel 故障中 UID 已清理，broker 读取控制证据报 `not a bounded regular
evidence file` 并断连。Host 正确保留 unknown cleanup 记录且不交付结果；后续
四项故障未执行，不能用正常组结果替代整批通过。独立只读复核 24 个 UID 无
残留、26 个临时 job 均已卸载，包 inventory/签名/Host 哈希一致；保留原报告
与未确认调用，不重放或手工删除未知 ledger。

排查复现了同错误的确定性竞态：`bounded_read` 在 open 与 fstat 之间遇到
`atomic_json` 的 rename，新版占据路径，已打开旧 inode 的 nlink 变成 0，旧
检查错误地按非法证据拒绝。这与报告的读取失败吻合，但旧报告没有记录出错
inode 的 nlink，故不把历史根因断言为完全确认。v2 仅对 nlink=0 的已打开普通
文件做最多三次重新读取；owner/private/type/size/多硬链接拒绝仍适用，不重试
任何任务调用。持续替换超过上限仍保留不确定状态。新增确定性真实 rename
测试、poll 取得终态清理测试和非法文件拒绝测试；v2 尚待真实服务复测。

2026-11-10 git-worktree-v2 管理员验收与独立复核通过。报告
`git-worktree-v2-report-Zw9Sjm`，SHA-256
`e2368c98f8ce5cc1a3d96e3c9fdc7e3c8e3dc3529c89fb7b6ea4aeb89a4a349c`；
包身份 `d4b99d76f8311bcbe7963a005fcf6af05d418eb88e5154cd89ea6f33b151e94d`。
正常 23 次调用、7 个工作树场景和五项故障全部通过；cancel/timeout 收到完整
清理回执，断连与 broker crash 仍保留未知结果并禁止重放，guardian 恢复仅
启动一次任务。独立复核 28 个 UID 无 real/effective/saved UID 残留、34 个
临时 job 不存在，13 个冻结模块、暂存源码与签名/inventory 均匹配。身份未
落盘夹具按预期保留目录，未冒认自动清理。v1 的失败证据独立保留。

这关闭本批工作树登记/绑定/发布/恢复验收点；完整 Git 工作流、普通工具执行
接入和最终发布范围仍未完成。下一步增加显式文件选择的受保护 index 暂存，
保持用户分支、主工作树 index 与工作文件不变；不提前开放 seal/promote。

### 显式路径 index 暂存：git-index-v1（待管理员验收）

`AgentWorktreeManager.stage_index` 仅允许 Host 对已有 write/leased 工作树
选择 1–16 个字面路径，明确处理新增/修改/删除；没有自动扩大为全目录扫描。
未选择的 index 条目保持不变，不隐式执行忽略规则、属性过滤器、hook 或配置。
Rust 固定对象读取选中文件，Host 核对相同 inode 的普通文件属性/执行位；
符号链接、硬链接、FIFO、目录、元数据路径及别名拒绝。每个文件最多 1 MiB，
本次选中内容总量最多 4 MiB，最终 index 最多 128 文件/16 目录，超限明确拒绝。
这些仍是当前有界支持范围，不能说任意大型项目或全部 Git add 语义已兼容。

沿用已验收低 UID 的 hash-object/mktree producer，逐条等待清理和原 journal
终态，再比对 Host 重算的 blob/tree OID。所有命令使用原项目对象私有视图；
Root 不读取工作树内容或写用户 index。普通 Host 最后生成无扩展 v2 index，
经排他 `index.lock`、fsync 与 rename 只更新该工作树的私有 index，HEAD/refs
和主工作树 index 不变。已有 index.lock 在任何 producer 启动前拒绝且不删除。
当前没有把此 Host 生命周期操作开放为嵌套工具命令；运行工具/自动选路仍待
既定第二组集成，不借此绕过原有 admission/cancellation 归属。

开始对象生产前持久化 index_update 意图。中断期间工作树 verify/consumer
拒绝继续使用；已知清理完成时恢复退休这个临时工作树，保留 failed 记录，
不会重跑对象生产或自动继续任务。若 native journal 仍有 unknown 债务，工作树
及意图均保留，不能仅依据目录路径或超时删除。已提交 index 哈希/staged_tree
但 Host 未返回的情况可核对保留，不意味着任务已经完成或自动继续执行。
恢复退休会删除临时工作树中的未发布编辑；主项目不受影响，当前不提供失败
执行内容的续跑恢复。seal、privacy scan 后 commit/ref、上游 patch 与 promote
仍未开放。不得把本批 index 暂存当成完整 Git 发布流程完成。

管理员 `--index` 会验证真实新增/修改/删除、执行位、原 HEAD 不变，并用独立
普通用户进程在 producing/prepared/rename 后/最终记录后四处 os._exit(86)。
正常组共 40 次 broker 调用；底层五项故障回归选用已有对象写入 producer。
Host stage 的取消与未知结果控制在本地测试中覆盖，本批五项 OS 故障测试是
对象 producer 本身，不冒充完整 stage/发布在每个 OS 故障下都已端到端验收。

2026-11-10 git-index-v1 管理员验收及独立复核通过：报告
`git-index-v1-report-kOYNLt`，SHA-256
`f6d3ba9336a76f5e1b558677eff3d2fbce3a2e7c4c8acbc35f7c07e52bf68058`。
40 次正常调用、7 个工作树场景、5 个 index 场景、5 项底层 producer 故障
通过；独立复核 45 个任务 UID 无残留、51 个临时 job 已卸载，14 个冻结模块
及安装包身份/签名匹配。报告、stderr、独立 audit 按摘要归档。

### 显式暂存结果的隐私检查与内部引用（git-result-v1，待管理员验收）

新增 Host 生命周期接口 `AgentWorktreeManager.publish_index`。它要求已有
显式 index 暂存；再次通过 Rust 读取全部 staged 文件并匹配 blob OID/执行位，
工作字节若已变化则拒绝，不能把检查过的旧字节和另一批新字节混用。新增/修改
路径在 stage 的任何 producer 调用前也执行现有隐私规则，完整 staged 文件在
发布前再次扫描。扫描采用文件名及逐行内容构造的有界输入，保留现有非敏感
finding（规则/路径/行号/指纹），文件以 `++` 起始不能冒充 diff 头。二进制中
可见的文本规则也检查；这是现有模式扫描能力，非任意编码秘密检测保证。

Host 固定 author、单 parent、tree 和消息，独立重算 commit OID，仍由原低 UID
对象 producer 创建。随后再用已验证 checkout 协议读取该 commit 的完整镜像，
核对与 canonical index 一致并再次扫描。只有清理和原 invocation journal 已
确认的结果可进入引用发布。Host 通过固定目录 FD 和新 UUID 名，在
`refs/gitgo/results/<uuid>` 排他发布一个 immutable loose ref；不覆盖已有
任务引用或用户分支，不改变任一 HEAD 或主 index。持有 `packed-refs.lock`
避免发布时与 pack-refs 竞争，已有锁直接拒绝，不接管或删除。锁身份先落盘，
再执行 ref 排他 rename；引用目录身份在操作前后重新核对。

本接口只发布明确的暂存结果，不代替现有 `seal()` 自动暂存语义，不把任务
置为 sealed，不自动合并运行时输出或启动 promote。调用方取得 commit/tree/ref
之后，后续正式工作流仍需完成 runtime 输出处理、任务封存及推广接入。已经
发布的工作树禁止继续 restage/re-publish，dispose 保留其不可变结果引用。
普通 Git pack-refs 若移动了已记录 loose ref，当前验证会明确拒绝，不静默
认领另一份文件；支持 packed 结果引用与保留策略仍属于后续集成范围。

producing/verified/packed-locked/prepared 与 ref rename 后中断，原 journal
若有未知清理债务即停止恢复。债务已确认后只处理记录过 inode 的锁；未记录
或被替换的锁原样保留并拒绝恢复。已发布 ref 保留为中断证据，退休临时工作树
并记 failed，不宣称任务成功、不重跑 commit、不自动继续执行。最终 result
记录已落盘的情况可核对引用并保留，仍不等价于产品任务完成。正常结果和
中断结果的内部引用均保留；引用回收/用户分支推广不在本批开放。

本地覆盖真实 Git 对象及 ref、隐私拒绝、工作文件漂移、六处持久化/rename
中断、未知债务、不认领锁及引用篡改。管理员批次计划 56 次正常组 broker
调用（含 7 个结果发布场景），另回归五项对象 producer OS 故障；这些故障
不冒充整条产品任务发布的端到端验收。生产能力开关保持关闭。

2026-11-10 git-result-v1 管理员验收及独立复核通过。报告
`git-result-v1-report-QjwCb3`，SHA-256
`b710f7fb667b383ca2aeb34890a865b0b59a5f6baef5b994dc8c7d8fd762dfa7`。
56 次正常调用、正常结果发布及六处崩溃、五项底层 producer 故障通过；
独立核对了三个保留引用的真实 commit/tree/parent 对象，61 个任务 UID 无残留、
67 个临时 job 已卸载，15 个冻结模块及包身份/签名匹配。审批记录仍位于原
root-private 目录，由管理员 harness 校验；普通用户复核没有宣称能读取它们。

### 不可变结果读取与 packed 引用兼容（git-artifact-v1，待管理员验收）

`AgentWorktreeManager.read_artifact` 的 macOS 路径现在使用现有受保护 commit
镜像读取协议，不再尝试不受支持的 `show commit:path`。完整 SHA-1 commit、
字面相对路径和项目 grant 是必要输入；绝对路径、反斜杠、跳转和元数据路径
不会被规范化成另一个有效名字。只有低 UID 退出清理、journal 完成和 Host
重算 commit/tree/index 验证后才返回指定文件，保留既有 digest/分页/二进制
拒绝语义。工作树删除后仍按 commit 读取对象；输出明确是 Git blob 字节，
不伪装成当前工作文件或属性过滤后的字节。上层 owned-child 与 sealed 状态
授权检查未放宽；任务封存/推广流程仍未开放。

沿用镜像的 128 文件/4 MiB 总量限制，因此不能称任意大型 commit 或仅因单文件
小于 10 MiB 就可读取；超出整个镜像预算时明确拒绝。本批没有扩大 root helper
的操作集合，也没有新增任意 Git 命令或直接读取仓库配置的路径。

已经记录的 loose result ref 被 Git pack-refs 收入 packed-refs 后，验证可通过
Rust 读取固定表并比对原记录的完整 ref 名与 commit。重复、不同值、缺失、
不安全权限、多硬链接及符号链接拒绝；存在 loose 文件时仍验证原 inode，不能
利用 packed 记录掩盖被替换的 loose ref。只有原 Git 目录对象和精确记录可作为
依据，不提供任意 revision 解析。前节对“所有 packed 结果一概拒绝”的限制在
此被替换；其他公开封存、推广和引用回收边界保持不变。

新增恢复边界：ref 已发布且 pack 锁已释放，但最终 result 记录尚未保存时，
若随后发生 pack-refs，恢复核对原意图与 packed 内容后仍保存该 ref 为中断
证据，退休临时工作树、标记 failed，不重放或推断任务成功。没有原 ref lock
身份的未知记录仍拒绝认领。

普通用户单测使用真实 `git pack-refs --all --prune` 验证兼容和该恢复窗口。
管理员批次的源仓库故意含恶意配置，禁止为测试而对它直接执行 Git：harness
只在自己的临时夹具中构造精确 packed 记录并移除对应 loose 文件，验证冻结
Host 的读取与恢复，不把这一夹具说成运行了真实 Git pack。预计正常组 63 次
broker 调用，含两次 artifact 读取、正常发布和七处崩溃恢复；另外沿用五项
producer OS 故障。生产能力开关仍关闭。

2026-11-10 git-artifact-v1 本轮未通过，保留失败报告
`git-artifact-v1-report-aHPAG3`，SHA-256
`581a4a84285b0dad3353eeb8484f23d65f3d05f66df2d611ef5b93400eafab88`。
63 个任务 lease 均 clean；独立只读复核 63 个任务 UID 无残留、64 个临时 job
已卸载、安装包身份及签名匹配。但客户端未返回完整结果，末尾拒绝检查没有
完成，后续 OS 故障批次没有运行，不能将本轮列为验收通过。

原因是临时 broker 的 64 次连接预算包含就绪探测及拒绝请求：63 次任务加
一次就绪连接已耗尽预算，之后三次拒绝探测无法连接。普通用户真实 Unix
socket 回归复现了第 64 次连接后退出、第 65 次连接失败；原 Host 外层将
OSError 显示为通用安装拒绝，harness 现额外记录原始 traceback 供定位。
产品连接预算及 Host 安全边界均未修改。

git-artifact-v2 仅调整验收分组并复用同一冻结产品包：独立工作树生命周期
批次 23 次任务调用（保留全部六处原工作树故障）；结果读取/发布批次 57 次
任务调用（保留正常工作树及全部七处结果故障）。各批另计一次就绪和三次
拒绝连接，最大 61 次；之后仍回归五项底层 producer OS 故障。所有检查
保留，且任一批失败仍中止总验收。完整产品任务封存、推广及生产开关均未
因此开放，v2 仍待管理员实测。

2026-11-10 git-artifact-v2 管理员验收和独立复核通过。报告
`git-artifact-v2-report-D7L0hQ`，SHA-256
`56efee3154ca89bf158f66bdda74a836410aa7c94730d28545a3a1a985d9d124`。
两批 80 次正常链调用、五项底层故障完成；85 个任务 UID 无残留、92 个临时
job 已卸载，原 journal 与 broker 对应。独立检查了四个保留引用（两个 loose、
两个 packed）及实际 commit/tree/blob，artifact 在工作树删除后仍按已验证
commit 读取。原 v1 失败证据保留，未改写为成功；生产开关保持关闭。

### 提交目录与差异读取接入（git-compare-v1，待管理员验收）

macOS Git consumer 新增两个严格形式：`ls-tree -rz <完整 SHA-1> -- [字面路径…]`
以及 `diff --no-renames --name-only -z <旧 SHA-1> <新 SHA-1> --`（亦接受两个
完整 SHA-1 的 `old..new` 形式）。用于现有 promotion `_tree` 和后续封存的
changed-files 计算；只返回不可变 Git blob 的模式/OID 或变更路径，不读取
当前工作文件，不执行用户仓库的 diff/filters/hooks。目录选择只接受字面文件
或目录前缀；拒绝通配符、pathspec、选项式路径、HEAD、三点范围、额外参数。

实现复用受保护 checkout 协议和原 native invocation journal：每个不同提交
取一个已完成清理且重算 commit/tree 的镜像；Host 比较 blob OID 和执行模式。
相同提交只读取一次，仍验证对象及授权，不能直接推断为空。全部镜像共享
调用方时限，逐次前后核对项目授权和登记工作树，取消、超时、未知清理、
工作树被替换时不返回部分结果。沿用单镜像 128 文件/4 MiB 等限制；选择单个
路径也不能绕过完整镜像预算。不解析分支、不放宽 helper 操作集合或生产开关。

管理员 harness 将比较放在独立工作树生命周期批次：29 次任务调用，另加
一次就绪及三次拒绝连接，共 33 次。结果发布批次仍 57 次调用（总连接 61），
并保留五项底层 producer 故障。普通用户测试与干净仓库真实 Git 输出逐字节
对照了增删改、二进制、执行模式、Unicode/空格目录、同提交读取和失效路径。
完整任务自动暂存、运行时证据合并、封存和推广尚未宣称完成。

2026-11-10 git-compare-v1 管理员验收及独立复核通过。报告
`git-compare-v1-report-bQAon6`，SHA-256
`b526dc3f23537262d8d1c648776074b1fa8f0314f485bae67870a426903de9ec`。
86 次正常链调用、五项底层故障完成；91 个任务 UID 无残留、98 个临时 job
已卸载。已核对六次新增镜像读取及登记工作树的原 invocation scope、16 个
冻结模块、四个实际结果引用/对象；生产开关保持关闭。

### 显式发布结果的封存确认（git-seal-v1，待管理员验收）

正式 `AgentWorktreeManager.seal()` 新增 macOS 分支：只接纳已经通过
`stage_index` 和 `publish_index` 明确准备好的结果。Host 在原 lifecycle 锁下
核对整个工作树的普通文件、模式、canonical index、结果引用和项目身份，
并通过原低 UID checkout 通道重新验证父提交和结果提交，生成 changed_files。
sealed/result_commit/own_commit 和绑定证据在原 worktree SQLite 事务中一起
写入；不另建账本、不写用户分支、不移动工作树 HEAD（它继续表示基线）。
封存结果由不可变 result ref 标识，读取、verify 与恢复使用这份证据。

本分支没有偷偷将原自动暂存语义改成“仅处理部分文件”：未显式发布、存在
未暂存的新增/删除/内容/模式变化时明确拒绝，包括尚未支持自动排除的 ignored
输出。未知 symlink/hardlink/FIFO、目录替换、别名和挂载跨越均拒绝。全量盘点
限定 512 项、64 层、单文件 1 MiB、业务数据 4 MiB、业务加运行时数据 8 MiB；
沿用 canonical index 的文件/目录限制。该盘点不宣称是阻止同 UID 外部写入的
内核快照；两次盘点一致且无 native 清理债务才可确认，最终结果仍是验证过的
不可变对象，不是可继续变化的工作目录。

新租约在复制 Host 运行时输入后记录内容摘要。封存要求运行时文件与这份
基线一致；新增或修改的测试/依赖证据被保留并明确拒绝封存，待 canonical
runtime-output merge 接入，绝不静默丢弃。缺少摘要的旧租约不被认领为新版
可封存租约。自动选择/暂存、运行时证据合并、上游补丁和用户工作区推广仍是
既定第一组剩余工作，不能因本接口而宣称完整任务链已经可用。

封存确认唯一外部效果是已有原子 worktree 状态写入。写入前中断，重载为 leased
且不能伪称任务成功；写入后、返回前中断，重载为 sealed 并验证原引用/证据。
两者恢复都不重放生产操作、不自动继续工作。普通单测覆盖这两个窗口、取消、
未知 cleanup 债务、盘点漂移、未暂存输入、运行时输出和零业务差异。

管理员脚本增加独立 sealing 批次 40 次调用（总连接 44），包含正常封存和
两处真实 Host 进程退出；同时保留工作树/比较批次 29 次、结果批次 57 次及
五项底层故障。每批独立临时服务，连接上限不变。生产开关仍关闭。

2026-11-10 git-seal-v1 管理员验收及独立复核通过。报告
`git-seal-v1-report-e0NIuM`，SHA-256
`77479a950ad8bc59b6128c7a63445c0f42f1e8cdf16c2e154112fb79211a245b`。
126 次正常链调用、五项底层故障完成；131 个任务 UID 无残留、139 个临时 job
已卸载。核对了普通发布的四个引用、封存批次的三个实际引用及对象，确认
两处封存中断分别保持未确认/已确认状态。17 个冻结模块、761 个暂存源码
文件与原快照一致，生产开关保持关闭。

### 运行时证据的显式合并（git-runtime-v1）

`AgentWorktreeManager.merge_runtime_outputs` 仅允许 Host 在已登记且没有 native
清理债务的写租约上调用。固定处理 test_manifest、dependency_feedback、
dependency_observations 三种文件；使用原测试 seed/validation 合并规则、
依赖边覆盖规则及追加观察记录规则。解析失败、重复键、未知输出、只读输入
修改或删除、JSONL 原前缀改变均拒绝并保留证据；每文件与合并输出上限 1 MiB。
原有 metadata-locks 中这三种文件对应的空锁文件可保留并纳入输入摘要，其它
未知文件不会被默认忽略。该步骤不改变业务提交，也不授予普通工具 metadata
写权限。原输入摘要仍保留，另存本次已合并输入的摘要；封存必须匹配它。

合并意图与确认使用原 worktree SQLite 行，记录固定目标、前后摘要和有界
候选字节。复用 Host metadata_files 的文件锁及 expected_sha256 冲突检测；
当前原生 Host 的 TestManifest 写入也加入相同的 CAS 通道。项目、目录身份、
工作树绑定及输出摘要在写入前后核对。各文件替换是原子的，三个文件整体
不宣称为文件系统事务，也不宣称能阻止同 UID 外部程序任意修改目录。

恢复仅核对：全部后像已存在则确认；尚有原前像则保留 pending，不执行任何
写入；遇未知像则拒绝。pending 时不能运行该工作树、封存、再次暂存或清理。
明确再次调用 merge 才会写仍匹配原前像的成员，已匹配后像的成员直接跳过，
因此崩溃后不会再次追加 JSONL。部分成功时保留原工作树及意图，不回滚覆盖
其它写入。管理员验收新增独立 30 调用批次，验证正常合并后封存、四处真实
Host 退出、恢复零写入和显式重试只追加一次。

本接口尚不等于整个任务完成链已接通；自动暂存/合并调度、上游补丁及推广
仍属于既定第一组后续范围。生产开关继续关闭。

2026-11-10 git-runtime-v1 管理员验收及独立复核通过。报告
`git-runtime-v1-report-JbFutF`，SHA-256
`8994939b2d7ee33d6db53989b4dfad21f9bc9ad95f34c769f335d4282caa6f09`。
156 次正常链调用、五项底层故障完成；161 个任务 UID 无残留，170 个临时
job 已卸载。核对实际运行记录后，正常案例和四个中断案例的观察记录均只
追加一次，测试清单及反馈各保留五个案例。恢复本身没有写入，部分成功仍需
显式重试。18 个冻结模块和 762 个暂存源码文件与原快照一致。

### 封存前归并运行时证据（git-finalize-v1，待管理员验收）

公开 `seal()` 在业务文件、canonical index、隐私规则及固定结果提交全部
验证后调用同一运行时合并实现。合并后再次核对目录和内容摘要，最后写入
原 SQLite 行的 sealed 确认。未暂存业务变更、取消或 immutable checkout
失败不会先发布运行记录；合并后出现漂移则保留合并确认，但不标记任务完成。
原生调用债务和部分合并意图仍阻止封存；恢复只核对，显式重试才继续写入。

允许现有依赖工具产生的 `dependency_graph.v2.json` 派生缓存和对应空锁。
新缓存必须匹配当前 schema，纳入工作树输入摘要及派生缓存确认；不把缓存
边或缓存文件复制到主项目。主项目原有加载器根据业务文件和辅助数据摘要
决定重建。允许删除派生缓存，其它只读输入删除、未知文件、非空锁、链接
及元数据别名仍拒绝。这里只兼容既有派生缓存，不放宽业务或持久证据权限。

管理员验收使用两个独立批次（53 和 42 次调用）覆盖正常公开 seal，以及
合并意图、首文件、末文件、合并确认、sealed 确认五处真实 Host 中断；保留
29 次工作树、57 次结果发布和五项底层故障。每批原连接预算保持不变。
本轮普通用户回归 845 passed、7 skipped、30 subtests passed；真实低权限
辅助服务验收尚未运行，生产开关仍关闭。自动选择/暂存、上游补丁恢复和
推广尚未接通，不能据此宣称整个任务完成链已完成。

2026-11-10 git-finalize-v1 最新报告 `git-finalize-v1-report-zC1tNM` 通过独立
复核，SHA-256 `01f158d760d8af0485ca7de77212d1d8ccbd60892b14f05a7890882ff089bd4a`。
181 次正常链调用、五项底层故障通过；本次 186 个 UID 无残留、195 个 job
已卸载，五处封存中断恢复均不重复追加记录。实际 Git 对象、引用和运行记录
均已核对。另有较早空报告 `git-finalize-v1-report-YrFV22` 停在 finalize_pending，
发现其临时 broker `org.gitgo.user-broker-trial.3d8e9nd5` 仍运行；该次中断不在
最新报告的清理证明范围，待管理员只读诊断，不能宣称全部历史试验均已清理。

随后该次历史试验的专用清理通过：20 个租约均为 clean，临时 broker 和
20 个任务 job 均已独立查询确认不存在，恢复证据保留。清理报告摘要
`678f26b06f222f6033d1f36487b2e733d1b6d668bcfafc84c282ed969c83e8b6`。
首次清理在 bootout 成功后立即查询时断言失败；后续查询确认服务已消失。
Git 验收脚本现改为有界等待卸载，查询权限错误不会视为卸载成功，等待失败
仍写入报告。相关卸载与打包编排回归 37 项通过。此改动不宣称解决 SIGKILL
或关闭终端导致整个验收控制器被终止时的自动报告保存问题。

### 有界自动完成（git-auto-v1，待管理员验收）

公开 `AgentWorktreeManager.seal()` 对尚未发布结果的 macOS 租约先执行 Host
盘点、运行记录格式检查和隐私检查，比较 canonical index，收集新增、修改、
删除及模式变化，依次复用 stage、publish、seal 原事务。各步骤恢复不自动
继续下一步；已有固定结果时维持原封存行为，不能自动重暂存覆盖它。
暂存后再次盘点，若业务或运行证据漂移则不发布。原项目 HEAD/index 不变。

当前自动收集拒绝 `.gitignore` 和包含有效规则的 `.git/info/exclude`，不自创
忽略规则解释器；每批最多 16 个变更路径，保留原文件和字节预算。空且未
暂存的树也明确拒绝。此阶段不承诺一般 Git 忽略配置兼容性，不代表上游补丁、
推广或完整产品入口已完成。生产开关仍关闭。

本轮回归 857 passed、7 skipped、30 subtests passed；新冻结 Host 的 36 项
普通用户预检通过，包含从公开 seal 自动开始的五个真实退出点。该预检替代
了特权准入，仍需运行 `run-git-auto-v1.sh` 验证真实辅助服务。该包沿用两个
分批封存验收的调用预算。此节记录于打包后，冻结源码快照保留构建时版本。

git-auto-v1 管理员报告 `git-auto-v1-report-CQKjGJ` 已复核通过，摘要
`d75154e1b90d669690d481a35f2c0c5c719aad363f2e1e2fcab88d4ba58f87f0`。
181 次正常链调用、五项底层故障通过；186 个任务 UID 无残留、195 个 job
已卸载。核对实际对象、引用、运行记录及 19 个冻结模块，五处自动完成中断
均不重复追加、不自动继续任务。

git-auto-v2 继续处理既定自动完成边界：每批仍最多 16 条路径，自动完成按
先删除、后新增/修改的顺序分批，保留原 128 文件、目录和字节上限。所有
业务文件先做隐私检查；最后再次盘点，出现漂移不发布。批间中断保留已有
index 状态，恢复不执行后续批次。空树使用内部显式参数进入原 mktree 生产
与 index 日志，普通空路径调用仍拒绝，内部入口也拒绝非空 index。

普通回归 861 passed、7 skipped、30 subtests passed；38 项冻结程序预检
通过（仍替代特权准入）。管理员验收新增 57 调用边界批次，覆盖 18 文件
分批及空结果封存后的真实 Host 退出。其余批次保留；等待运行
`run-git-auto-v2.sh`。忽略规则、上游补丁及推广未在本轮放开，生产仍关闭。
本段记录于打包后，不修改已有冻结源码快照。

git-auto-v2 管理员报告 `git-auto-v2-report-jjWNzD` 已复核通过，SHA-256
`747e5a602064546bede05b1f8c323d08d42916247bcd576f8ecc3ed180849bb2`。
238 次正常链调用及五项底层故障通过，243 个 UID 无残留，253 个 job
已卸载。独立读取实际 Git 对象确认多批案例有完整 18 个文件，空结果 tree
字节确实为空；七处退出恢复不重复追加，19 个冻结模块与签名身份一致。

git-auto-v3 对自动收集增加保守的忽略规则允许列表：空行、注释以及根级
`.gitgo` 元数据排除规则可通过；业务模式、否定、转义、不明编码仍拒绝。
这不是 Git wildmatch 的部分实现，也不宣称支持任意 ignore 配置。规则文件
本身仍作为业务文件参与摘要、暂存和固定结果验证，不能静默丢弃。
管理员边界批次改为 58 次调用，包含 `.gitignore` 与其它 18 个业务文件的
自动完成。普通回归 873 passed、7 skipped、30 subtests passed，38 项冻结
预检通过；真实特权准入仍待 `run-git-auto-v3.sh`。生产开关继续关闭。
本段记录于构建后，不修改已固定的源码快照。

git-auto-v3 管理员报告 `git-auto-v3-report-RmOqhR` 已复核通过，摘要
`6bed517553e9d4cd9f29204e287e59598eeab0caa60124c3bc0558349eb15787`。
239 次正常链调用、五项底层故障通过，244 个 UID 无残留，254 个 job 已
卸载；实际多批提交含 `.gitignore` 和其余 18 个业务文件，空树及原有恢复
证据完整，19 个冻结模块身份与暂存源码一致。

### 线性上游提交接入（git-upstream-v1，待管理员验收）

macOS 工作树创建接受最多八个不同的完整提交 ID。使用现有低 UID 私有
对象读取链逐个执行固定的 cat-file 类型检查及 merge-base --is-ancestor
谓词；全链成立后从末端固定提交创建工作树。保留原 snapshot_commit 和
显式 upstream_commits，工作树绑定使用实际末端提交；不移动用户 HEAD。
分叉、逆序、非提交对象、超量/重复输入、审核时夹带上游均拒绝。检查失败
不创建工作树记录，未确认的原生调用仍由原 journal 保留恢复债务。

此支持是线性继承，不是 cherry-pick、文本冲突合并或未提交工作恢复。
普通回归 881 passed、7 skipped、30 subtests passed；39 项冻结程序预检
通过，含真实子进程中祖先查询、接入及封存后的退出恢复。特权准入仍由
预检替代，需 `run-git-upstream-v1.sh` 的新增 35 调用批次确认。生产关闭。
本段记录于构建后，已有源码快照保持不变。

### 线性上游验收数据修正（git-upstream-v2，待管理员复验）

v1 两次管理员运行（hGRFsW、mgQ3X0）均通过前五批，在线性上游批
28 次调用后停止。原因是脚本把无父提交的 protected-object 提交作为
snapshot，却把独立 fixture 的 second 提交当上游。两者不是祖先关系，
`MACOS_WORKTREE_UNCONFIRMED: nonlinear upstream requires protected patch merging`
是正确拒绝；外层空 stdout 的 JSONDecodeError 只是次生错误。
两次共 534 个任务身份、546 个临时 launchd job 已只读核对无残留，
证据 `git-upstream-v1-failure-audit.json`；不能把清理通过写成验收通过。

v2 只修正验收脚本及测试：非空对象提交显式以 first 为父；上游子场景
从 first 创建，以非空提交为 upstream，保留产品的非线性拒绝。
新增测试调用同一 fixture producer，用真实 Git 检查对象 ID、唯一父提交、
祖先关系和二进制文件内容。定向回归 48 passed；新包 frozen smoke 通过；
39 项普通用户冻结预检通过，其中 fixture producer 也经冻结 Host 的
Seatbelt 写入路径执行。特权准入在预检中仍为替代，不算管理员验收。
`run-git-upstream-v2.sh --check-only` 已通过。下一步运行该脚本的完整管理员
验收，核对新 35 调用批及后续五种故障。生产仍关闭。
本段记录于构建后，v2 源码快照及清单保持不变。

### 线性上游实际验收通过与拒绝边界补测

v2 管理员报告 `git-upstream-v2-report-mpLQFU` 七组全部通过；独立复核
274 次正常协议调用及五种故障，共 279 个任务 UID、290 个临时服务无残留。
检查包身份、签名、763 个 staged 源文件、17 份冻结运行证据，并解析真实
Git 对象确认 first（空树）→ 非空上游 → 封存结果，根 HEAD 未移动。
完整复核在 `git-upstream-v2-audit.json`。这证明线性路径，不代表分叉合并支持。

下一批 v3 只补此前普通回归覆盖的拒绝路径：分叉、倒序、blob、重复、
超过八个上游、review 夹带上游；每项都要求没有新增工作树记录、目录或
根 HEAD 改动。实际需新增五次受保护查询（其中两次祖先谓词返回 1）；
上游整批共 40 次调用。新增 `--only-stage finalize_upstream` 可只复验此批，
报告明确标记 focused_acceptance 和 selected_stages，不冒充完整七组验收。
产品授权、生产开关保持不变。

v3 已完成 54 项定向回归及 45 项冻结 Host/真实 Seatbelt 预检（含上述
六种拒绝），正式打包 smoke 和 wrapper --check-only 通过。特权身份
准入仍须管理员实测；执行 `dev-artifacts/macos-helper/run-git-upstream-v3.sh`
只跑 40 调用的上游批次，不重跑已确认的其它六组。结果保存在唯一命名
的报告中。下一轮可用 `audit-git-upstream-v3.py` 只读检查其证据和清理。
本补充位于构建后，不改动已封存的 v3 源码快照。未提交或推送。

### 上游拒绝实际验收通过；显式部分工作续跑

v3 管理员报告 `git-upstream-v3-report-aHqZxw` 通过。只读复核 40 调用、
六种拒绝、40 个任务身份及 41 个临时服务，均无残留；真实对象链和签名
核验通过，证据 `git-upstream-v3-audit.json`，SHA256
`5773485eee0eb98419d398cf3c4c2c2ab7db6acaacf9105edf30b54f38128143`。

新增显式 resume_from：只接受同项目、同 snapshot、同 upstream 列表、
相同实际基线的、未完成的已登记 write 租约；拒绝旧/伪造对象、同进程 ID、
review、已推广源和未完成的 Git/runtime 事务。先通过原自动暂存/隐私扫描/
不可变结果 ref 保存检查点，再从受保护 checkout 返回的精确文件建立新的
登记工作树。新工作树 HEAD 仍是原基线，index/文件为检查点，因此最终
封存包含旧执行的全部编辑（新增、删除、模式和二进制内容）。

旧 lease 的 continuation_commit 在现有工作树行确认；新 lease 保留来源 ID
和检查点 ID。没有新增授权账本，检查点不等于完成任务；旧 lease 仍为 leased，
没有 result_commit/推广资格。新工作树重新复制根项目 runtime 输入，不复制
旧 .gitgo 输出或进程身份。检查点后旧文件再变动会拒绝，不静默丢弃编辑；
确认丢失必须 reload/recover，恢复只核对/清理，不自动创建或执行新任务。
原忽略规则/大小/路径限制继续适用；非线性 patch 合并和正式推广仍未接入。

本轮宿主回归 911 passed、7 skipped、30 subtests passed；初次受工具沙箱
限制的 11 项 socket/Seatbelt 失败已在普通用户宿主复测通过。随后补充
目的租约占用、runtime 事务拒绝以及确认丢失的定向测试。管理员批准备
`--only-stage finalize_resume`，预计 48 调用，覆盖私有检查点、新工作树
prepared 时退出、sealed 确认后退出，保留原工作并验证无自动重放。
生产开关仍关闭，需冻结包预检及管理员验收后才能标记实际通过。

续跑候选 `git-resume-v1` 的打包 smoke、47 项冻结 Host/真实 Seatbelt 普通用户
预检、wrapper 摘要预检均通过；最终补强后的续跑/上游定向回归 27 passed。
冻结预检实际运行 `resume-prepared` 与 `resume-sealed` 子进程退出路径，旧快照
可读、新文件和模式保留，恢复未自动重放。特权准入仍被普通预检替代，
下一步由用户运行 `dev-artifacts/macos-helper/run-git-resume-v1.sh` 做单批管理员
验收，再用 `audit-git-resume-v1.py` 独立核验。未宣称管理员实测已完成。
此记录在构建后追加，已封存快照不变；无提交、推送或生产开关变更。

### 续跑管理员验收通过及入口拒绝补测

`git-resume-v1-report-ZuIKF8` 通过，独立复核见 `git-resume-v1-audit.json`：
48 调用，48 个任务身份、49 个临时服务无残留；prepared 中断清理新目录，
旧检查点保留；sealed 中断保留完成结果且不重放。真实对象包含 binary bytes
及 100755 模式，最终提交仍以原任务基线为父；根 runtime 只有 runtime-normal，
没有旧执行输出。生产仍关闭。

v2 只补管理员入口拒绝覆盖，产品实现未修改。冻结子进程在真实续跑前依次
测试伪造租约、review、复用源 process_id、含私钥的源文件。每例要求工作树
权威行、目录集合及 native history 均不变化；secret fixture 独占创建后删除。
拒绝事实由子进程输出、父验收器核对，并写入报告。仍为 48 调用单批验收。

v2 的 71 项定向回归、47 项冻结 Host/真实 Seatbelt 预检和摘要预检通过。
冻结子进程两个续跑场景均实际执行四种拒绝，并由预检父进程读取 JSON
拒绝证据。普通预检仍替代特权准入，因此等待用户执行
`dev-artifacts/macos-helper/run-git-resume-v2.sh`；随后用
`audit-git-resume-v2.py` 复核拒绝事实、私有引用和清理状态。
本记录追加于打包后，不修改封存快照；产品未启用、未提交或推送。

### 续跑入口拒绝验收通过；正式发布前的只读比较

`git-resume-v2-report-c7JX9e` 实测通过，`git-resume-v2-audit.json` 独立核验
两条恢复路径各自四种拒绝、检查点及根 runtime 未混入旧执行结果，48 个
任务身份与 49 个临时服务无残留。续跑正常/拒绝路径至此已有真实辅助服务证据。

开始接正式 promotion 的前置比较：macOS 的 `_matches` 改用固定提交的
受保护 checkout 图像及 Rust ReadRoot + 已有 descriptor-bound selected_file
读取授权项目，逐项比较原始字节、执行权限和存在性；不调用项目 Git、
attributes/filter/hook，不把 dangling symlink 或目录当成不存在。路径、
文件/总字节预算和根目录身份均检查。比较只提供前置条件，不能代替现有
Candidate/审批/文件锁/写入/CAS；完整 promotion 尚未接通，生产仍关闭。

新增管理员 `--only-stage finalize_promotion_read`，预计 33 次调用；在一次性
测试项目中独占创建 data.bin fixture，检查缺失、正确二进制、模式漂移、内容
漂移、预期不存在、dangling symlink、目录七种情况，最后恢复 fixture 不存在。
产品函数只读；原用户项目和正式安装不变。普通回归覆盖同一个验收 helper。

promotion-read-v1 的 70 项定向回归、54 项冻结 Host/Seatbelt 预检、打包 smoke
和封存摘要检查通过。普通用户预检替代特权准入，不能代替管理员验收；下一步
运行 `dev-artifacts/macos-helper/run-git-promotion-read-v1.sh`，再独立核对
33 次调用的比较证据及身份/服务清理。无需重启。本文记录追加于封存后，
未修改已打包源码；完整发布写入仍未接通，生产仍关闭，未提交或推送。

### 发布前比较实测通过；恢复记录走同一次受保护读取

`git-promotion-read-v1-audit.json` 核验通过：33 次调用、33 个任务身份、34 个
临时服务无残留；七种内容/模式/存在性比较符合预期，报告摘要
`a79f390b22432974e2f50f54e73f71d105ba792f226ff7b674c85be14421b353`。

macOS `promotion_candidate.matches_record` 接入同一次受保护 checkout 和
描述符绑定读取，同时核对固定提交的字节、模式与记录的 raw SHA-256，避免
随后另走通用 `_tree/_bytes`。显式 null、缺项、重复/额外路径、错误摘要或
字段被拒绝；缺少整个 file_versions 字段才保留历史 commit-only 语义。
恢复记录必须完整覆盖 guarded paths，before/after 摘要均先验证格式。

83 项定向测试通过，包括真实 SQLite prepared 状态的 before/after/foreign
三种恢复决策，确认只核对和推进记录，不重放文件写入。管理员验收增加八次
有效格式的恢复比较和六种无原生调用的坏记录拒绝，连同原七例共 21 个场景，
整批 41 次调用。完整 promotion 的整合、候选和发布写入仍待接入，生产关闭。

promotion-record-v1 已完成正式打包、frozen smoke、68 项冻结预检及封存摘要
检查。冻结预检已执行全部 21 项比较/坏记录场景，仍替代特权准入，管理员
路径尚待 `run-git-promotion-record-v1.sh`；随后由
`audit-git-promotion-record-v1.py` 核对 41 次调用和清理。无需重启。
本记录追加于封存后；未变更已打包快照，未启用生产、未提交或推送。

### 恢复摘要验收通过；受保护候选进入 Host 文件写入

`git-promotion-record-v1-audit.json` 通过：41 次调用、41 个任务身份、42 个
临时服务无残留，报告摘要
`b9a6b68083f46e4a66f825829ad1c74cbe51bb899c67ffa12ec805c170818ff3`。

macOS `commit_promotion_patch` 接入固定提交图像的原始内容/模式写入，保留
共同候选检查、建议/必选检查、跨进程文件锁、取消和过期运行检查。整个 delta
必须与 changed 集合相同，并再次扫描隐私；不执行项目 Git/filter/hook。
仅支持现有安全父目录内的普通文件新增、替换、删除和执行位变化；新父目录、
链接、元数据、超预算或不完整 delta 均在写入前拒绝。新增使用排他 rename，
不覆盖并发出现的目标；替换共享原有 cooperating-writer 语义，不声称可对
任意外部写入实现原子 CAS 或跨文件事务。

临时文件名称先进入原 agent_promotion prepared 记录，创建后先保存身份，
再写入/同步/替换；临时身份由 native_write 字段绑定项目与父目录。恢复仅
删除身份一致的临时文件，未知创建/替换保留债务；不会重放、回滚或删除目标。
部分效果返回 partial/ambiguous。prepare 回调支持原记录追加该意图；prepared
恢复先清理这些已确认临时文件，再比较原 before/after 图像。
增量发布新路径仍按提交核对，已有 file_versions 必须完整覆盖先前记录 paths。

定向验证覆盖真实字节/模式、增删、候选拒绝、部分替换、取消和四个中断位置。
管理员探针新增正常创建/删除，以及真实普通 Host os._exit(86) 后的临时文件
回收与目标保留；共 51 次固定辅助调用，25 个比较/写入场景。写入都在一次性
测试项目，完整上层 integration/ref 路径仍待接通，生产仍关闭。

promotion-write-v1 的 103 项定向回归、72 项冻结预检、正式打包 smoke 与封存
摘要核验通过。普通预检使用真实冻结写入代码，特权准入被替代且中断为
BaseException；不能代替管理员批次的真实 os._exit(86)。下一步运行
`run-git-promotion-write-v1.sh`，再由 `audit-git-promotion-write-v1.py` 核对
51 次调用、两次真实中断与身份/服务/临时文件清理。无需重启。
本记录追加于封存后；生产未启用，未提交或推送。

### 写入层管理员验收通过；正式线性发布入口

`git-promotion-write-v1-audit.json` 通过：51 次调用、51 个任务身份与 52 个
临时服务无残留；真实 os._exit(86) 的 created/applied 两点分别保留旧目标/
新目标并仅清理已确认临时文件。报告摘要
`04f6eb81dfd9aa748bb59900602154486a1009b3ba0ad69cf3167d5c193254be`。

`AgentWorktreeManager.promote` 的 macOS 路径现在直接使用受保护固定提交图像、
原 agent_promotion CAS 记录和候选写入服务。每个提交必须具有同项目/同快照的
持久封存确认与可验证不可变结果引用；接受 disposed 后仍保留的已封存证明，
但不伪造 worktree/task 成功。每批最多八个结果，通过低权限固定 ancestry
查询确认线性顺序；分叉拒绝，不模拟三方合并。复用永久保留的 results 引用
作为 GC 锚点并记录 native_result_refs，不另写可变 promotions 引用，不移动
用户 HEAD/index，不使用临时 Git integration checkout 或项目 filter/hook。

原 prepared 记录由真实摘要/模式核对恢复。旧内容允许显式请求重新发布，新
内容确认效果后不重写；外部未知改动保留债务。临时身份的清理先于效果核对。
历史非 native_linear 记录明确拒绝并要求迁移，不能猜测以前的写入语义。
输入依赖和交付摘要均从有界固定图像/授权读取产生，保留必选候选检查、取消、
跨进程锁及最终 CAS；不增加并行成功账本。生产仍关闭。

本批将以正常发布/重复请求和正式入口 created/applied 两次真实中断验收，
在原写入批次上增加 31 次辅助调用，总计 82 次、29 个场景。原有新父目录、
非线性合并、完整 dirty-root 捕获等限制仍明确保留。

正式 Pipeline 调用允许原 native scope，先验证调用者项目与日志存储归属，
并将取消事件传给 ancestry/checkout；跨项目、已取消或遗留 native 债务拒绝。
121 项定向测试通过，含线性增量/packed ref、真实 SQLite CAS 失败、未知外部
改动、重复请求，以及 Pipeline scope 归属。完整工具审批链和生产启用仍需
后续产品链验收，不以 manager 入口测试替代。

promotion-flow-v1 的正式打包 smoke、76 项冻结预检及封存摘要核验通过。
冻结预检覆盖新增四个正式入口场景，但替代特权准入并使用软中断；管理员
真实中断待 `run-git-promotion-flow-v1.sh`，随后由
`audit-git-promotion-flow-v1.py` 独立核对 82 次调用及清理。无需重启。
本记录追加于封存后，未修改封存源码；未启用生产、未提交或推送。

### 线性发布的管理员验收通过

初版批次把已单独验收的比较与底层写入用例并入线性发布，共计划
82 次调用。试验辅助服务最多接受 64 个连接，其中启动探针占用一个。
前两次在同一个位置耗尽额度，`flow-created` 未进入故障注入点；
已有 63 个任务身份清理完成，辅助服务已卸载。诊断批次确认失败源为
辅助服务端连接拒绝。

`git-promotion-flow-v2` 单独运行线性发布及其必要的封存准备。
辅助服务二进制和签名保持原封存身份；本次改动限于验收脚本与诊断。
81 项定向回归、76 项冻结工作树预检与 2 项对象生产预检通过。
在 macOS 26.6.2 测试虚拟机内用真实辅助服务运行，审计核对
57 次调用、57 个任务身份及 58 个临时服务，无残留。
正常发布成功，重复请求未重写。创建临时文件后中断，显式重试只写一次；
替换目标后中断，重试确认已生效并补齐记录，实际写入为零。
两次中断均为真实退出码 86，已确认身份的临时文件均被回收。
报告 SHA-256 为 `e42ed7ff344937da4382d7f974a915283bad427d5790cb68f8d923062875aa07`。

此虚拟机关闭 SIP。本轮覆盖真实辅助服务下的发布入口、恢复和清理。
上层工具审批与正式产品启动链仍待验收，生产开关保持关闭。

### 产品工具的审查与受保护发布验收

`git-promotion-review-v3` 在 macOS 26.6.2 虚拟机的冻结 Host 中使用
正式工具注册表和 `ToolPipeline` 调用 `review_child_outcome` 与
`promote_agent_changes`。封存工作树和 Git 写入均走实际辅助服务。
测试用例预置了已完成子任务的写入回执，用于验证上层审查与发布的连接。

未审查与要求修改两种状态均拒绝发布，没有原生调用、发布记录或文件写入。
审查通过后，真实发布的六条原生调用与工具回执逐条相符，已发布状态持久化。
重复请求的两条原生调用只用于核对，文件未重写。已发布工作树随后清理，
审计确认共 34 次调用、34 个任务身份、35 个临时服务均无残留。
定向回归 89 项通过，原封存辅助服务二进制未改动。报告 SHA-256 为
`3d1981a3c9c55af3215e85bbd80142f7dd3678636ecc70cfed5221bb2e8c49f3`。

前两次分别因验收脚本过早清理工作树、将原始调用 ID 与带前缀的历史键直接
比较而失败，已修正测试顺序和审计。正式安装启动、常驻服务授权、
用户交互审批及开启 SIP 的验收仍待补齐，生产开关保持关闭。

### 已安装公开命令启动验收（2026-11-11）

在 macOS 26.6.2 ARM64 虚拟机使用 `git-promotion-flow-v1` 完整暂存包。以
普通用户执行包内 `install.sh`，指定独立安装目录和命令目录。通过安装产生的
公开命令查询到 `1.0.0-alpha.3.1.1`，并在真实 PTY 打开首次启动设置页。
Dashboard 连接包内 Native Host 并显示实际版本，随后通过 SIGTERM 清理路径
退出（143）。首次探针等待项目页提示，第二次用 Ctrl+C 退出设置页，均超时；
最终探针按首次启动的设置页和实际版本判断就绪。

同包另行尝试 `--managed`，返回 `UPDATE_INSTALLATION_BLOCKED`。确认包中
缺少 `build-manifest.json` 和 `update-data-contract.json`，拒绝后未发布受管命令。
受管初始安装需要按正常构建流程生成发行清单和冻结运行时证据，清单要求可追溯
的干净源码。受管安装、发布者信任和默认生产启用均待验收。

常驻服务可由管理员配置项目授权，但当前公开入口没有建立对应的
`broker_session`。产品入口调用受保护 Git 时仍要求既有会话并拒绝无授权执行。
当前启动实测只确认 Dashboard 与冻结 Host；通过用户交互审批将常驻服务授权
接入实际执行的整条路径、开启 SIP 的环境仍待验证。安装、辅助服务及 broker
定向回归为 99 通过、2 跳过。虚拟机原始记录位于
`gitgo-vm-lab/reports/installed-launch-20261111-151642`。

### 常驻项目授权与冻结 Host 联调（2026-11-11）

新增 Host 侧 `resident_project_session`。它以已登记项目和操作员指定的服务名
为输入，核对 root 管理的目录、固定 socket、当前 UID 的私有凭据及项目对象
身份。会话退出后清除上下文。每次请求仍由辅助服务验证授权是否有效。

macOS 26.6.2 虚拟机使用新构建的包和包内管理员入口安装唯一命名的服务。
普通用户通过已安装冻结 Host 的内部验收入口执行固定 Git 对象读取。正确
项目返回 `blob`，错项目及无会话请求被拒绝。管理员撤销后同一项目的读取
也被拒绝。最后确认 launchd 作业已卸载、客户端凭据已移除。首次源码探针
因虚拟机的 Python SQLite 版本低于 Gitgo 运行时要求退出，复测用冻结 Host。
虚拟机的 SIP 仍关闭，公开产品入口的会话接入和用户交互审批尚待验证。
原始记录在 `gitgo-vm-lab/reports/resident-session-frozen-20261111-152743`。

辅助服务、授权与 Git broker 定向回归 109 项通过。现有权限决策和 macOS
发布专项 28 项通过。通用发布文件服务测试另有 7 失败、11 个夹具错误，
均停在子工作树创建时缺少固定提交。用改动前的 broker 客户端复现了同一
夹具错误。这组测试仍需接入 macOS 的项目授权和快照流程后重跑。

另行生成了受管安装的构建清单和冻结运行时数据证据。本机执行真实受管
安装器确认当前未提交快照被拒绝、公开入口未创建。底层契约错误为
`UPDATE_PACKAGE_CONTRACT: Build identity differs from the signed release`。
正式受管安装通过仍需以可追溯的干净候选重新构建。生产开关保持关闭。

### 工作树准入诊断与 SIP 验收（2026-11-11）

macOS 工作树创建先校验项目授权，再检查是否具备封存的快照提交。两种
拒绝均在创建工作树记录或调用辅助服务前返回。已有工作树、线性上游和
broker 定向回归共 71 项通过。公开入口仍需要接入授权和快照流程。

在独立副本 `gitgo-tahoe-sip-lab` 的恢复模式中启用 SIP。恢复环境使用 NAT
完成联网验证，随后以仅主机网络重新启动。两次启动均确认 SIP 已完整开启。

在 macOS 26.6.2（25G83）上复用此前验证过的冻结包，包 SHA-256 为
`d563c03eb1a45122f6be64b11a7b09a84ec6d62497431c644e42d9070b4478c8`。
实际安装并启动常驻辅助服务，普通用户使用已安装的冻结 Host 通过内部
验收入口读取 Git 对象。错项目、无会话和撤销后的访问均拒绝。卸载后
launchd 作业与客户端凭据均已清除。记录位于
`gitgo-vm-lab/reports/resident-session-sip-20261111-213144`。

同一启动会话复跑已封存的 `git-promotion-review-v3`。未审查和要求修改
时禁止发布，批准后使用实际辅助服务发布，重复请求核对既有结果。共核对
34 次调用、34 个任务身份与 35 个已卸载作业，无残留。验收完成后再次
确认 SIP 已开启。此次使用验收程序设置审批状态，公开入口和用户交互
审批仍待接入并验证。原始报告位于
`gitgo-vm-lab/reports/git-promotion-review-v3-20261111-213204`，启动记录
和两份包的 SHA-256 位于 `gitgo-vm-lab/reports/sip-acceptance-20261111-213119`。

### 已安装 Host 的项目工作树入口（2026-11-11）

本地操作员可在已登记项目的配置项中选择管理员预先授权的常驻服务，
并指定用于首个子任务的完整提交 ID。配置只保存服务名和提交 ID。

```json
{
  "macos_worktree": {
    "service": "example-project",
    "snapshot_commit": "0123456789abcdef0123456789abcdef01234567"
  }
}
```

将示例值替换为已授权的服务名和该项目的实际提交 ID，然后重新启动项目守护进程。
守护进程验证项目注册和凭据，工作树创建仍经真实辅助服务核验固定提交，
子任务只继承本项目的会话。现有任务沿用已固定的快照。缺少配置、授权或
快照时拒绝创建受保护工作树。此模式暂不加载完整工作区和 Trial Git 历史，
状态会标明历史不可用。未提交的工作区文件不进入这个提交快照。

本地候选使用管理员显式授权的测试辅助服务。产品内的交互式审批和正式
签名受管安装另行验收。

新冻结包 SHA-256 为
`497c2f69b20b0b22dcbe27d7c967a192a0c47d989e4e93c607552d3e58023527`。
在开启 SIP 的 macOS 26.6.2 虚拟机上，普通安装的 Host 通过正常进程
启动项目守护进程。缺少快照时收到工作树不可用事件。随后内部验收入口使用
同一项目配置、真实常驻辅助服务及子任务线程，在指定提交创建并核对工作树，
退出后清理工作树、服务和凭据。验收日志位于
`gitgo-vm-lab/reports/project-entry-sip-20261111-214741`。

Python 定向回归 188 通过、3 跳过。单独运行的跨进程重启测试在当前和
改动前快照上均复现 watchdog FSEvents 关闭崩溃。Dashboard 定向回归 25 通过。

### Dashboard 单次发布授权（2026-11-11）

配置了受保护项目快照的守护进程将 `promote_agent_changes` 注册为需要用户
批准的单次调用。原有监督代理审阅、工作树封存和发布账本仍参与校验。
授权卡通过现有 Dashboard 决策流程提交，允许一次的授权绑定本次调用参数。

开启 SIP 的虚拟机安装冻结包，通过实际 Dashboard 和伪终端按键验收。
本地测试 Provider 只生成任务及工具调用，批准和拒绝由 Dashboard 按键提交。
等待批准和拒绝后的测试文件保持原样，批准后真实辅助服务发布该文件。
再次批准同一发布时，回执返回已有提交和空变更列表，内容、inode 和修改时间
均保持不变。辅助服务和凭据已清理。相关 Python 测试 72 项通过。

冻结包 SHA-256 为
`67607416c5237d51376914249cf6ffd917e5d394301edd8b64b303e7d2bb4c0d`，
记录位于 `gitgo-vm-lab/reports/dashboard-approval-sip-20261111-221032`。

### 干净候选的受管安装与 Dashboard 验收（2026-11-13）

从原开发工作树复制明确列出的源码文件，在独立副本保存本地提交；原开发
分支和上游未提交、未推送。最终本地候选为
`caa0b4e10d551bde2e7dd8d2298c5f64969ab164`，tree 为
`068259239031820bc32b997892545a939d047f66`。完整构建运行 `--update-evidence`，
保留真实冻结 Host 数据证据和 clean source 清单。最终归档 SHA-256 为
`0aa401e363cf8a89d583380c7cd9bcb145c87cc2ac8e9c07cfa3666d65a704b0`。

首轮在 SIP 开启的虚拟机完成真实受管初装，随后从 `app/active` 以 root
运行辅助服务管理脚本被普通用户安装所有权规则拒绝；未创建服务。没有放宽
用户 Host 的准入或运行锁检查。新增 `MACOS_HELPER_MANAGED_LOCATION` 明确
提示管理员使用独立解包且校验的发行包；辅助服务安装仍封存到 root 所有的
独立目录，用户 App 的升级不自动更换 helper。

最终验收在仅主机网络、SIP 完整开启的 macOS 26.6.2 ARM64 虚拟机运行。
`check_macos_managed_dashboard.py` 核对 source commit/tree、全部包清单与代码
签名，然后以普通用户真实执行 `install.sh --managed --no-path-update`。
先验证从已安装 active 目录调用管理员入口被拒绝且无服务注册，再从校验后
发行包管理同包 helper。实际公开 `bin/gitgo` 经稳定启动器打开 Dashboard，
测试 Provider 只提供任务和工具调用，拒绝/批准仍由真实 PTY 按键提交。

- 待审批及拒绝后项目内容、inode、修改时间均不变。
- 单次批准后发布成功；再次批准同一结果返回已有发布且不重写。
- Dashboard 退出后普通独立进程可以独占原 runtime.lock；原公开命令重新
  启动报告 `1.0.0-alpha.3.1.1`，再次退出后锁也释放。
- helper 卸载成功，launchctl print 返回明确未注册码 113，客户端凭据、socket
  和 LaunchDaemon 文件均不存在。恢复记录及不可变 helper 镜像按设计保留。

报告在 `gitgo-vm-lab/reports/managed-dashboard-sip-20261113-230107/reports/report.json`，
SHA-256 为 `d715c2f46ec4cd1c704e239c02e905bdb457094c27b9a49619e348c1d1e6dc74`。
包、候选 git bundle、逐文件快照和回归日志保存在本地忽略目录
`dev-artifacts/macos-helper/managed-dashboard-v1`。这段结果文档追加于候选封存后。

安装、权限、发布和运行时契约的定向回归 174 通过、2 跳过（需单独传入包
路径的测试）；真实包由本轮虚拟机验收覆盖。新增三项发布保护回归通过，
覆盖批准后新增不可用强制检查、文件写锁及两个实例争用同一发布。
构建中的 Dashboard 14 项和冻结 Host/daemon/tool-runner smoke 通过。

当前仅为操作员显式选择的本地 ad-hoc 包，`production_enabled=false`；
受管初装回执仍为 `updates_enabled=false`。正式发布者信任、跨版本 App/helper
组合与第三阶段接纳后的多平台 CI 均未完成。本轮不扩展工作树和资源能力范围。


### App 与辅助服务跨版本升级、回滚验收（2026-11-13）

本轮只补充验收工具和可追溯证据。第四阶段原开发分支仍未正式合入此前的
macOS 退出后更新修复。隔离副本以 `caa0b4e10d551bde2e7dd8d2298c5f64969ab164`
为输入，再组合独立更新分支的 `c5eb3de`、`214ec08`、`dabb574`、`e102971`。
补丁 SHA-256 为 `3acfdef2284265c40c3e5e115de4b7d427ece34aa544c760f756ca7f1f6412ec`。
组合补丁未提交到第四阶段原开发分支、未推送上游；隔离快照已有本地提交，
这些本地提交不是正式接纳提交。

| 包 | 本地版本 | 干净源码 commit | tree |
| --- | --- | --- | --- |
| a | `1.0.0-alpha.3.1.1.stage4lab.1` | `348f7d8e2484b2c724aa02697a7d356a783b793f` | `80f648afb06951500325926a7a4c7d805e140c1a` |
| b | `1.0.0-alpha.3.1.1.stage4lab.2` | `7cf0bc8bc551cb0745289f6ad87557a7727812db` | `89d5cc9af337443b40c359f9683735c139c9b7c4` |
| failure | `1.0.0-alpha.3.1.1.stage4lab.3` | `b1a17344d27abe60b0f7ec41f65417ca2a220ccc` | `1dba2d18ed6df636c1af0a3767c4f6074ed450fd` |

归档 SHA-256：

- a：`6162478fc262512f5496ceade05b595d2f639d6893926805d58546e662050c8e`。
- b：`f383f4eb0d97d35744bd1670456b1d7cfaa815a84ead75c253b9e6c789131258`。
- failure：`42751dd5e1d379916ba3a6eac21ed5ea425ed14c2b28a7b4d476888f25c389d9`。

A/B 使用相同辅助服务协议、独立完整构建和不同版本身份；没有通过改安装登记、
伪造构建清单或替换已构建的二进制制造版本。failure 是源码显式让 Dashboard
`--version` 返回 73 的故障构建。更新清单使用真实 Ed25519 签名，公钥在构建时
固定；私钥仅保留在宿主私有测试目录，未进包或报告。只把下载传输替换成本地
固定字节，正常清单、签名、哈希、包契约和稳定启动器检查均执行。

在 SIP 完整开启、仅主机网络的 macOS 26.6.2（25G83）ARM64 虚拟机中：

- 真实 `install.sh --managed` 初装 A，确认安装的是 API 12 的退出后更新启动器。
  API 11 的旧安装契约不在本轮修改；修复版需要先手动安装一次。
- 原公开命令打开真实 Dashboard，经 PTY 按键先做等待/拒绝不发布、批准发布
  一次和重复批准不写入。后续升级、显式回滚也经 Dashboard 确认后退出，
  再用同一个公开命令检查实际启动版本。
- App/helper 的 A/A、B/A、B/B、A/B 均通过真实冻结 Host 的受保护 Git 对象读取。
  各组合重试已批准的发布时返回已有结果，发布记录、文件内容、inode 和修改
  时间均不变；这些组合中的重复检查使用正式发布入口，不声称每格都重新进行
  一次用户交互审批。既有项目授权与客户端凭据也未变化、未续期。
- 另一个真实 App Host 持有运行锁时，Dashboard 更新返回 `UPDATE_BUSY`；当前
  版本、程序树和事务记录保持原样。该实例退出后，原操作正常更新 A→B。
- 显式回滚 B→A 后原入口打开 A。辅助服务 A→B、B→A 后读取正常；升级到无法
  启动的 helper 时明确报告已恢复先前 B，旧请求账本未变化，随后调用正常。
- 故障 App 的签名和包校验通过，确实在 `CHECK_NEW/dashboard-version` 因退出码
  73 失败，记为 `UPDATE_HEALTH_FAILED`；恢复后程序树摘要与之前 A 完全相同，
  原入口仍能打开 A，读取与已发布结果核对继续通过。
- 使用真实历史实验 helper `1.0.0-alpha.3.1` 验证协议差异：新版 checkout 操作
  被明确拒绝，正式调用记录为未启动，没有新增 helper 执行或修改项目。恢复
  当前 helper 后读取正常。历史包来自保留的未提交实验快照，不是正式历史发行包；
  其 executable SHA-256 为 `cd952e9dae8c0183a6e61ce9792096d044f5119a448e451de4c32511bb438bd6`。
- 矩阵执行期间 macOS 启动标识不变。最终 39 次 helper 调用均有已清理记录，
  未发现这些任务 UID 的剩余进程，未缺失调用记录，服务空闲核对通过。常驻
  helper 卸载返回 0，launchctl 查询返回 113，凭据、socket 和常驻启动项移除。
  不将此结果扩张为逐一核验所有历史临时 launchd 标签；恢复记录和不可变镜像保留。

最终报告：`gitgo-vm-lab/reports/cross-version-sip-20261113-234047/reports-v3/report.json`，SHA-256 为 `c44ca140ec322f06f399abe01a33ea4918622cf92eb6ae8fe50f692f3c3c9fd1`。
源码 git bundle、三个完整包、签名清单、脚本及最终报告副本保存在
`dev-artifacts/macos-helper/cross-version-v1`。复验输入与命令见
[打包说明](../packaging/README.md#macos-apphelper-cross-version-acceptance)。

首次完整矩阵通过；加强断言后的第二轮在故障 App 已成功回退之后因虚拟机
磁盘不足拒绝写入下一次调用记录，明确没有启动任务，服务仍安全卸载。保留
`cross-version-sip-20261113-232631` 的失败报告，没有将它改写成通过。之后先
保留前两轮安装控制证据、确认服务卸载并独占运行锁，再回收仅这两份测试
安装，未降低存储保护阈值；最终完整复跑通过。外层新增至少 3 GiB 空闲预检。
旧控制器使用 Tart stop 后观察到包含服务登记在内的较早磁盘状态重新出现；
其 plist、凭据与 helper previous 身份均退回较早内容，底层机制尚未确定；源码没有 removed 自动
变 active 的恢复路径。现改为 guest 内正常关机并等待退出，另在下一次启动
只读核验已卸载服务仍为 removed、launchctl 113、凭据和启动项仍不存在；
该后续检查保存为最终报告目录的 post-shutdown.json。未因此放宽产品生命周期规则。

组合快照的更新、运行锁、事务、helper 生命周期和发布定向回归 205 项通过；
原工作树相关定向回归此前 56 项通过，其中新 probe 测试由 3 项扩充为 7 项并通过。
三个包均通过完整构建和冻结 Host/daemon/tool-runner smoke。当前结果覆盖特定
同协议 A/B 和一项历史协议拒绝路径，不保证任意跨版本协议/状态格式兼容。
helper 就绪检查尚无能力协商：旧服务能启动但可能拒绝新功能，须按兼容组合
交付。正式发布者信任、公网更新链路、第三阶段接纳后的正式组合和多平台 CI
仍未验收，`production_enabled=false` 保持不变。


### 辅助服务协议协商与未知结果保护（2026-11-14）

当前源码新增逐调用能力核对。普通 Host 在同一条已经验证 root 对端的连接上，
先发送只含授权和原始调用身份的 `capabilities` 请求；root 服务在有效项目授权
和准入锁下返回协议及各操作修订号，绑定调用、执行依据、grant、用户、项目对象
和当前 boot。Host 仅在精确匹配后发送功能请求。服务仍在该锁内再次验证实时授权，
不允许核对后换调用身份、执行依据、grant 或令牌；后续功能请求须属于公布的操作集合，
并再次通过该操作原有的严格校验。核对本身不占用执行账本或创建任务 UID。
历史 `status` 仍保留到期授权与跨启动恢复的独立只读规则。

新 Host 遇到旧 helper 或不支持的协议/操作修订会返回 `HELPER_UPGRADE_REQUIRED`，
留下正式的 `not_started` 历史记录，不发送功能请求，也不降级重试旧协议。
新 helper 保留现有严格功能协议供已验证的旧 Host 使用，因此本轮新增 Host 的
部署顺序是先更新 helper，再更新 App；回退 helper 到不兼容版本后，新 Host 应拒绝执行。
这不承诺未来任意数据或协议变更都能兼容。握手头和正文共用绝对截止时间，
服务等待功能帧最多 5 秒；Host 取 5 秒与任务剩余时限的较小值，并能响应取消。

复查发现并修复一项既有错误：远程功能请求已经发出，但启动回执丢失时，
通用执行器没有本地进程对象，曾把远程未知状态覆盖成 `not_started`。
现在这类通信/协议异常保留 `ambiguous`，原生调用记录保持待恢复，后续同项目
执行被阻止。只读清理核对可以确认进程已清空，但不能把未知业务结果改写成成功，
也不能因此自动重放任务。回执前的能力核对失败则仍为明确未启动。

普通用户回归覆盖真实 socket/描述符传递、正式调用记录和完整 ToolPipeline，
管理员隔离结论仍需下述冻结包与真实服务验收；不能把传输替身测试当作 root 验收。



#### 冻结包与真实辅助服务验收

候选版本 `1.0.0-alpha.3.1.1.stage4lab.6`，本地干净源码 commit
`16f4593da40c7b62afb56279831c606cbeb4b376`，tree `0243958d5f5f3c092b216a01c3939338d7b85f40`，
完整包 SHA-256 `f0a48674aa6a04950104bbc34ea74438810546145bc468e3ccf86a348f91c101`。
该副本接续上一轮 B 组合快照 `7cf0bc8bc551cb0745289f6ad87557a7727812db`，
仅加入本批明确列出的源文件；逐文件摘要见 `new/identity.json`。仍是本地测试组合，
未提交到原第四阶段分支、未推送、未正式接纳；退出后更新修复的正式集成安排不变。
实际执行的验收脚本单独按 SHA-256 保留，不能与打包时的脚本快照混为一份。

SIP 完整开启的 macOS 26.6.2 ARM64 VM 中，以下七格均通过：

| 配对/转换 | 实际结果 |
| --- | --- |
| 新 App / 旧 helper | `HELPER_UPGRADE_REQUIRED`，未发送功能请求，root 执行账本不增 |
| 旧 App / 新 helper | 原冻结 Host 正常读取对象 |
| 新 App / 新 helper | 受保护读取成功 |
| 新 App / 新 helper，用户审批后 | 真实 Dashboard 等待和拒绝不写入，批准写入一次，重复不再写入 |
| helper 启动失败后 | 恢复原新 helper，随后操作通过 |
| helper 回退到旧版 | 新 App 再次明确拒绝，未启动任务 |
| helper 恢复新版 | 操作与已发布结果核对恢复正常 |

项目授权、凭据和已发布文件身份不变，原公开入口始终启动候选新 App。
本轮没有重跑上一轮完整 App 更新器矩阵；已有成功升级、回滚、失败恢复证据保留。

额外执行真实 `exec_command`：只在客户端已收到真实 helper 的 `started` 帧后
注入一次 EOF，原调用记录保留 unknown/ambiguous，实际只发出一个 `run`。
第一次重试通过原有只读恢复机制取得清理回执，但保留 `NATIVE_OUTCOME_UNCONFIRMED`。
随后新冻结 Host 核验已有回执并再尝试两次，均拒绝重放。此次新 Host 的 status 发送
列表为空，因为前一个 Host 的重试已完成清理核对；不声称新 Host 又进行了远程查询。
原始 CAS 证据仍在，没有伪造完成历史或删除未知结果债务。此债务保留在一次性测试
项目中是预期结果，不代表任务进程未清干净。

最终 root 审计 27 次调用、27 份对应记录，无缺失，均已清理，相关 UID 无剩余进程。
故障调用 launch_count=1。常驻服务卸载、凭据、socket、plist 清理通过，正常关机后
再次启动核验仍为 removed / launchctl 113，之后再次正常关机。
完整矩阵期间 boot 不变；仅后续清理持久性检查主动重启了测试 VM。

报告：`gitgo-vm-lab/reports/broker-compatibility-sip-20261114-001540/reports/report.json`，
SHA-256 `046609baf05d8ebf278eb9a9f716dbf58e54601d5282559ccc281d37b3313a30`。
后续启动检查：同目录上一级 `post-shutdown.json`，SHA-256 `c6e1d72d373f2be9708227ac559a5ee31cc8e40f73ed4ff8ae3488ee796171b2`。
包、源码 bundles、脚本和报告副本位于 `dev-artifacts/macos-helper/broker-compatibility-v1`。

本机最终 macOS 与发布回归 1,186 通过、2 跳过，30 子测试通过；随后把新冻结 Host
路径传给原跳过的两项 supervisor 测试，2 项通过。新增验收脚本专项 8 项通过。
通用发布文件服务原有 7 失败/11 夹具错误已修复，相关 98 项回归通过：保留真实
Git/SQLite/文件发布，只为普通用户单测替换 broker 传输；不能冒充管理员隔离证据。
初次受工具运行限制的检查有 12 项失败，允许系统接口后均通过，原失败日志保留；
没有降低断言或更改测试为无条件跳过。构建中的 Dashboard 14 项及冻结
Host/daemon/tool-runner smoke 通过。未进行当前组合的 Windows/Linux 或全仓 CI。

### 正式发布信任的未接线基础（2026-11-14）

`macos_helper_trust.py` 新增固定 root 策略读取、已认证发行摘要绑定、Developer ID
requirement、精确 TeamIdentifier/signing Identifier 判定及发布者一致性校验。
没有读取环境变量或工具参数作为信任来源，也没有安装策略文件或启用生产能力。
真实 macOS 测试覆盖 requirement 编译及未签名、ad-hoc、篡改二进制拒绝；
Developer ID 正向证书路径目前只测试了替身边界，不能声称实际发行者认证通过。

该模块尚未接入 installer/lifecycle。完整 onedir 清单与认证发行清单的只读绑定
现由下述 `macos_helper_release` 基础实现，尚未成为安装授权。正式发行仍需管理员
入口自身认证、安装回执中的信任绑定，以及启动、升级、回滚和恢复时重新核验。
并非只等待证书。Duo 还需确认获批的 TeamIdentifier、固定签名 Identifier、
发布密钥和版本组合；当前本地 ad-hoc 验收与 `production_enabled=false` 保持原范围。
`release_macos.sh` 已补齐更新证据、发布策略及 Node 工具链参数转发，未改变干净源码、
测试和发布检查门禁。给二进制签名本身不代表整个 helper 目录和管理员入口可信。

### 固定管理入口与停止服务前的完整性检查（2026-11-14）

管理命令现在进入冻结 Host 内的固定 `macos-helper-admin` 角色；相邻的
`manage_macos_sandbox_service.py` 不再由包入口以 root 执行。固定角色要求 macOS、
真实和有效 root 身份、冻结运行时，并在读取安装状态前拒绝用户受管的 active
位置；原 `installed_runtime` 准入仍保留。源码管理脚本仅作为显式开发调用的薄入口。
这减少了一个可执行输入，并不认证启动该 Host 前已经加载的 PyInstaller 依赖。
本地操作员选择包、`--allow-local-adhoc` 和 `production_enabled=false` 的边界不变。

helper 清单扫描改为目录描述符逐级打开、禁止跟随替换后的目录，限定文件字节数、
目录深度和总条目数，拒绝读取期间的替换、增长、截断和原地改写；不可读目录不再
被 `os.walk` 默认行为静默忽略。框架链接在已捕获清单内解析，拒绝逃逸、循环和
失效目标。旧完整包的清单及 bundle ID 与新算法一致。这不是并发可写目录的原子
快照；最终安全仍依赖 root 私有暂存及封存后的不可变安装。

`switch` 在写维护状态和停止旧服务前核验候选及当前回退镜像，`recover` 在停止
当前进程前核验恢复目标；实际 `launch` 时再次核验。损坏镜像不会先打断健康服务。
另外，辅助服务拒绝新增有限、绑定原调用的原因枚举：授权无效、维护中和协议不兼容。
真实 broker 到 ToolPipeline 保留准确原因；只有明确 `not_started` 可透传，已经
启动或结果未知的任务继续走原清理及禁止重放门禁。

本地干净候选 `1.0.0-alpha.3.1.1.stage4lab.8`：commit
`8973c7a03a386537a538b6122d46a10b987efed7`，tree
`24f80b5d315f25f91a9b6f4dbc59ac04a7ebf09f`，归档 SHA-256
`ef2a79f0aaa640eacfe042f583acda4dcdd25de2faf691edd16c82dacd3ced83`。
它从此前 `.6` 组合候选继续构建；原工作树未提交，未推送或接纳。首次 `.7` 构建
成功，但测试快照漏带验收脚本依赖，收集测试时失败；保留该证据后补齐依赖并重建
`.8`，没有将失败包用于虚拟机验收。

SIP 开启的 macOS 26.6.2 ARM64 VM 中，`check_macos_helper_entry.py` 验证：

| 场景 | 结果 |
| --- | --- |
| 普通用户调用 root 管理角色 | 拒绝，未注册服务 |
| 相邻管理 Python 被替换为写 canary/退出 91 | 未执行该脚本，固定角色实际安装成功 |
| 候选包包含外部链接 | 停服务前拒绝，原 PID、授权、账本与启动项不变 |
| 本轮独有回退镜像的非执行 marker 损坏 | 升级拒绝，原服务保持运行 |
| 维护恢复目标的同一 marker 损坏 | 恢复拒绝，当前新服务 PID 不变 |
| 修复测试 marker 后正常恢复/再升级 | 普通冻结客户端实际受保护读取通过 |

只修改本轮独有 fixture marker 和明确记录的维护边界，未修改安装回执或历史包。
6 次真实执行均只启动一次，最终 UID 为空、无清理问题，服务、凭据、socket 和
启动项均移除。正常关闭 VM 后再次启动，服务仍不存在，随后再次正常关机。
报告：`gitgo-vm-lab/reports/helper-entry-sip-20261114-011405/reports/report.json`，
SHA-256 `f407d88f3bc0c28863960337d7256df6e3bbd37c2b5b3ebefddcefece86d4f4f`。

该候选 macOS 与发布回归 1,278 通过、2 跳过、30 子测试通过；以实际冻结程序补跑
原跳过的 2 项均通过。Dashboard 构建测试、冻结 Host/Daemon/tool-runner smoke 和
包契约通过。完整证据保存在 `dev-artifacts/macos-helper/helper-entry-v1`。
没有重跑本组合的 Windows/Linux 或全仓 CI；正式发行信任与签名仍未启用。

### 完整发行包的只读认证基础（2026-11-14）

`macos_helper_release.inspect_release` 在显式本地管理员验收模式下，从固定的
root 策略读取发行公钥，验证原始清单的 Ed25519 签名、产品、通道、平台、架构、
有效期与下载地址范围。它按签名中给出的大小复制并校验归档，使用 root 私有暂存
目录安全解包，核验完整产品契约、文件清单，以及 helper 子目录与清单的精确对应。
策略及内容在返回前再次核对；正常和失败路径都删除本次私有暂存。

它不执行候选代码、不写安装回执或服务状态，没有 CLI 安装入口。返回值仅为本次
检查的诊断证据，不能作为另一目录或未来操作的安装许可。没有安装固定策略文件，
没有把候选自带的公钥、哈希、环境变量或参数当作信任来源。测试使用真实 Ed25519
签名、归档和产品契约；这不代表真实 Developer ID 发布链已经验收。

回放水位、授权升级、精确保留前驱回退、可信引导入口和 root 信任回执尚未接入
生命周期。清单时效检查也不是持久回放防护。上述工作与 Duo 待确认的发行者配置
分别记账，`production_enabled=false` 保持不变。

### 扩展权限与有界启动恢复（2026-11-14）

macOS 的扩展 ACL 可以在 POSIX mode 仍为 0700/0600 时授予其他身份写权限。
新增 `macos_file_authority.require_no_extended_acl`，直接核验已打开描述符上的
Darwin ACL，拒绝非空 ACL、读取失败及检查期间身份变化，不解释或修改已有 ACE。
安装目录逐层、封存 helper 的目录/文件、root 控制记录和分配/生命周期/恢复锁
均接入此检查，复制后发布前再次检查封存暂存包。普通用户检查 root 0711 目录时
使用 `O_SEARCH`，允许遍历而无需目录列表权限。旧原型恢复记录和临时 broker 根
也检查顶层与控制目录，不能因它们来自旧测试而跳过。

Host 对需独占写入的工作树父目录、控制元数据、发布目标父目录和锁使用同一检查，
保留已有所有权、模式、身份及内容校验。这是独占写权限判定补强，未证明存在审批
绕过。默认无 ACL 项目兼容；带非空 ACL 的这些对象保守拒绝，包括 deny-only 和
继承 ACL。程序不自动移除权限，普通项目源文件只读路径未增加该限制。

真实旧包启动暴露出全局身份恢复锁争用：原服务遇 `LOCK_NB` 忙即退出，管理器只等
40 秒，无法区分恢复繁忙与坏程序。当前新源码在同一进程等待分配锁最多 30 秒，
完整恢复预算 120 秒（包括等待）；仍逐条核验历史证据、观察空 UID、原子提交每条
恢复记录，不跳过历史或制造 clean。预算在既有有界内核观察返回边界检查，并非
能抢占内核调用的实时保证。已完成的单条恢复在后续超时后仍保留。

启动诊断由 root 写入 `private/startup.json`，严格绑定本次 nonce、boot、helper、
PID 和启动次数。管理器就绪等待总上限 180 秒；有效新进展只能刷新 40 秒无活动
窗口，旧记录、错误身份或未来时间戳不能续时。真正就绪仍要求原有 root socket
拒绝握手，诊断不能授权任务。恢复完整结束后才开放监听；超时或损坏记录明确拒绝，
保留原因，升级失败沿用原恢复流程。旧二进制不会因此获得修复：没有当前进展的
旧 helper 仍按 40 秒无活动截止处理。历史规模超过恢复预算仍会失败，需要另行
明确规模与归档策略，不能删除恢复证据来使验收通过。

### 权限与恢复组合候选的实际验收（2026-11-14）

本地候选 `1.0.0-alpha.3.1.1.stage4lab.10` 的干净组合快照为 commit
`5c90ce13a376beb3d178237e283161d92441407e`、tree
`b2e78a0ce2f16320711cf9d7e91fb5c778d47507`，完整包 SHA-256 为
`0751425978d4d8aaa03f66aa1a5c24380d7a5799ecc31ca31ef296109e3586fe`。
该快照继续保留先前独立组合的更新器修复；不是原开发分支或上游的正式合入。
原工作树 HEAD 仍为 `35ee79b28590f20584ae032cc6a691bd85254ee9`，本阶段产品输入
与本次清单一致；不能因此声称原工作树与组合快照整仓相同。

保留了两次失败及后续复验，未把失败记录覆盖为通过：

- `.9` 的真实旧 `.6` helper 在启动时反复争用全局恢复锁，尚未建立监听和执行
  账本，初装失败；确认从未启动任务后，通过正常卸载清理。该失败推动了上述有界
  恢复修复。同期检查修复了普通用户遍历 root 0711 目录时误用 `O_RDONLY` 的问题。
  原失败、诊断及后续独立清理证据保存在 `dev-artifacts/macos-helper/helper-authority-v1-failed`。
- `.10` 首轮真实升级已就绪，但验收程序把分行 JSON 按单行解码，报告解析失败；
  没有授予完整验收通过。修复只接受首条就绪消息及其后完整一个 JSON 对象的解析，
  拒绝截断、额外文档或尾随杂项；50 项脚本测试通过后，用同一包完整复跑。

最终 SIP 开启的 macOS 26.6.2 ARM64 VM 验收七项均通过：普通用户管理拒绝、固定
角色不执行外部管理脚本、危险候选拒绝、损坏当前回退镜像拒绝、测试 marker 的扩展
ACL 拒绝、新 helper 启动锁争用恢复、损坏恢复目标拒绝。之后恢复旧 helper、再升级
新 helper，各使用真实普通用户冻结客户端读取。新增 ACL 仅作用于本轮独有 marker，
恢复时只移除该条 ACE；没有重写回执、清空 ACL 或删除历史恢复证据。

锁争用用例中持锁约 0.755 秒，观测到新 helper 的 `recover_busy`，随后同一 PID、
启动 nonce 与 `starts=1` 进入 ready，`lock_waits=1`，扫描 186 条历史记录。7 次
真实调用均 `launch_count=1`、UID 为空且清理完成。启动前的全局恢复在固定预算内
用时约 50.77 秒、尝试两次；首次精确为 `RecoveryBusy`，不是忽略任意恢复错误。
最终调用返回的恢复条数仅属于该次调用，不作为两次尝试的合计。

卸载后正常关闭 VM，再次启动确认服务未加载、凭据/socket/启动项不存在（launchctl
返回 113），随后正常关机。完整报告位于
`gitgo-vm-lab/reports/helper-authority-v2-rerun-sip-20261114-021233/reports/report.json`，
SHA-256 `e39e719af9ab280c5afc25f9645f6b4a68e0d0d9ef092fdf8a70992ea4c0df0c`。
重启后检查 SHA-256 为
`9305a83ed2dd9358e965a0cfee68b53d11a1d4ef0b38212fb815aba53d2e5c4a`。

对应干净源码的定向回归为 1,497 通过、2 跳过、30 子测试通过；实际包构建后补跑
这 2 项 frozen Host 测试均通过。Dashboard 构建测试、冻结 Host/Daemon/tool-runner
smoke、原生读取及完整包契约通过。没有重跑 Windows/Linux 或全仓 CI。源 bundle、
包、日志、首轮失败和最终报告保存于 `dev-artifacts/macos-helper/helper-authority-v2`。

构建后只修正了外部验收脚本及其测试；产品包未改变。实际执行的
`check_macos_helper_entry.py` SHA-256 为
`899293a41de71c8b02917b2eea84009a76860fd0e90cf9fbcce632ccfe1441f5`，对应测试为
`cc22dedab7f189b591794850ccf758f80886091c52fef9b7eaddb897be252915`。
两者与原构建快照分别记账，可在归档 `executed-inputs` 中复核。

### 同一权限补强包的 Dashboard 发布复验（2026-11-14）

上述 `.10` 完整包又在 SIP 开启的 VM 中走过真实受管初装、公开命令和 Dashboard
审批发布，以覆盖本次 Host 工作树独占权限检查。包清单和干净源码 commit/tree
由实际冻结 Host 核验；没有另外替换脚本进入产品包。使用仅本机的测试模型接口，
通过真实伪终端选择审批按钮，并读取实际项目文件核对结果：

| 操作 | 结果 |
| --- | --- |
| 审批卡等待用户选择 | 文件内容、inode、修改时间不变 |
| 用户拒绝 | 收到权限拒绝结果，文件不变 |
| 用户仅允许一次 | 发布成功，文件替换为已批准内容 |
| 再次批准相同发布 | 返回既有发布结果，内容、inode、修改时间不再改变 |
| 退出并重新打开原公开命令 | 运行锁可独占，仍报告 `.10` 版本 |
| 服务卸载、正常关机及再次启动 | 服务/凭据/socket/启动项均不存在 |

用户受管 `active` 目录不能作为 root 管理入口的拒绝也再次通过；安装 helper
仍来自独立校验后的完整测试包。实际脚本与快照相同：外层 SHA-256 为
`e8292c3c78ffa52d8d086866217bbfc4d7053c69a11b44b579572cb34ceaa377`，Dashboard probe 为
`929fc030e24534a42b6267928869e388690d8d21fd5fdc4f28d8f6c17941ab3e`。
完整报告：`gitgo-vm-lab/reports/managed-dashboard-authority-sip-20261114-022815/reports/report.json`，
SHA-256 `bb45125904852aecdeedfe0fc5ec6ac68869ece8c25830a26c72f37a08ae0154`。
源码和包引用上述同一 `.10` 归档；实际执行、正常重启和准备阶段证据保存于
`dev-artifacts/macos-helper/managed-dashboard-authority-v1/result.json`。

准备阶段的四次中止单独保留：一次宿主隔离环境无法创建 vmnet，之后发现旧报告已
不在来宾原路径，使用宿主封存副本核对时又发现历史测试服务仍存在。旧报告仅证明
当时观察，不能证明当前仍然卸载。重新只读审计该服务，13 个历史调用全部 clean、
无 UID 进程及遗漏记录；通过当前固定管理入口正常卸载，随后只清理对应旧 App/bin，
保留项目和 root 恢复证据。旧服务重新出现的底层原因未由此确定，不能归因为当前
版本的卸载错误，也不把旧通过记录改写成持久状态证明。最终普通重启后独立核验
本轮与该历史服务均不存在。这些准备中止不计为 Dashboard 通过用例。

本轮不认证正式发布者，不启用生产能力；未补做可见终端人工观感或多平台 CI。
## 内部验收的完整包摘要固定

Issue #45 要求验证最终原生程序的身份、签名/摘要及实际加载对象，没有指定 Apple Developer ID。内部验收可使用管理员独立核对的完整 helper 库存摘要；这不认证公开发布者，也不启用公开发行能力。已有 Developer ID 校验模块保留为未来发行选项，自动更新的签名 manifest 校验不变。

首次管理入口必须来自管理员已核对的可信构建或源码。程序运行后自查不能证明首次运行它是安全的；不要用未知安装包自己打印的摘要作为来源证明。在可信源码中，用普通用户执行 `python -B scripts/macos_helper_digest.py --bundle <辅助服务目录>`，将结果与独立的构建/审查记录核对。该命令只读取文件，不运行候选。

管理员通过固定 `macos-helper-admin` 角色安装，使用 `--expected-bundle-sha256 <已核对摘要>`，不同时传 `--allow-local-adhoc`。摘要覆盖完整目录中的内容、文件执行位和内部链接。安装前、复制后及 root 封存后分别核对；不匹配时不启动候选。root 控制的 `install.json` 记录 `operator-pinned-sha256`，保留 `production_enabled=false`。安装后的固定管理员入口、服务启动、升级预检和恢复复用同一库存与回执检查。

已固定摘要的服务拒绝切换到只有旧式本地许可、没有摘要固定回执的候选；相同库存的旧安装可在管理员明确确认摘要后采用新回执，旧式安装请求不能降级已有回执。升级失败恢复已记录的确切前驱；恢复不是重新接受任意目录，也不清除调用记录。内部管理员可以明确固定较旧构建，这属于显式本地管理，不是自动更新的版本/序号许可。旧 helper 若不认识新回执会拒绝启动，不能据此声明任意历史版本可升级到此模式。

不支持自动接受新的发布者、公钥轮换或无人确认的远程 helper 升级；这些是后续发行范围。摘要固定不会修改项目授权的有效期、启动身份或权限，重启后仍需现有显式授权流程。
