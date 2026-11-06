# macOS 执行边界：阶段四候选

这是 Issue #45 第四阶段的设计与开发探针，不是已交付的执行后端。生产 `macos_execution` 能力仍为 `false`，工具执行与受保护 Git 继续明确拒绝。阶段二的安全读取不承担此项验收。

## 当前路线与兼容范围

候选路线是在 Apple silicon Mac 上运行独立 macOS 来宾，保留 macOS 命令与开发工具的系统兼容性。它不等于直接使用宿主的所有已安装工具；来宾工具链需独立准备和校验。不能把 Linux 来宾作为透明替代，也不能把本路线说成已支持 Intel Mac。

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
