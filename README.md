# Caged Claude

把 Claude Code 关进 Docker 里运行，网络只能走你指定的一个代理节点。

- **网络锁死**：所有出站流量只经过选定的节点，DNS 也一样。节点不可用或检查不通过就直接断网，不会回退直连。
- **有状态的 Linux**：装的软件、改的配置、HOME 里的文件都实时保存在宿主上，容器重建、宿主重启甚至重装 Docker 后都能恢复。
- **随时回到宿主**：在笼子里执行 `ssh host`，就能用宿主的网络、文件和工具。
- **配套浏览器**：同一出口的 Chromium，访问 http://localhost:3002 。保留标签栏和地址栏，没有额外的系统标题栏及窗口关闭/最小化/最大化按钮；关闭最后一个标签或窗口后，会自动重新打开新标签页。界面不提供桌面菜单或终端入口。

## 准备

- Docker：OrbStack、Docker Desktop 或 Linux Docker Engine。宿主上不需要 Python 或 Go。宿主开着 Clash 等 TUN 时用到的拨号器，会在首次需要时用 Docker 编译。
- 一个 mihomo/Clash 格式的节点文件，其中包含独立的 AnyTLS 节点。
- 项目放在 Linux 文件系统上（Linux 或 WSL）。环境状态需要保留文件属主和 setuid。
- 可选：宿主上有 `python3` 和 `sshd` 程序时，才能使用 `ssh host`；不需要开启 SSH 服务。

## 快速开始

```bash
./cage init --mihomo /path/to/mihomo.yaml   # 生成 config/，并放入你的节点文件
./cage nodes                                # 列出节点，复制一个 anytls 节点名
```

编辑 `config/app.yaml`，至少填写这几项：

```yaml
proxy:
  node: "🇸🇬 Singapore 01"       # ./cage nodes 列出的名字
  expected_country: SG          # 出口国家；不限制就写 any
workspace:                      # 要交给 Claude 的项目目录，按原路径挂载
  - ~/Projects/my-project
```

时区不用填：启动时按节点出口 IP 所在地自动选择，浏览器和 shell 都使用它。要固定时区就写 `browser.timezone`，但它必须属于出口国家，否则拒绝启动。

然后启动：

```bash
./cage up        # 后台启动，全部检查通过后返回；首次需要几分钟构建镜像和初始化环境
./cage claude    # 进入 Claude Code，首次按提示登录
```

## 日常使用

| 命令 | 作用 |
| --- | --- |
| `./cage up` / `./cage down` | 启动 / 停止。停止不会丢失任何状态 |
| `./cage status` | 查看容器、网络许可和宿主桥的状态 |
| `./cage claude [参数...]` | 运行 Claude Code，参数原样传给 Claude，例如 `./cage claude -p '问题'` |
| `./cage shell [-- 命令]` | 进入 Claude 容器的 shell，或执行一条命令；`--root` 以 root 身份进入 |
| `./cage logs [-f]` | 查看网关日志，包括持续检查和断网原因 |
| `./cage pause` / `./cage resume` | 立即断开公网 / 重新检查后恢复 |
| `./cage help` | 查看全部命令；`./cage <命令> --help` 查看某个命令的选项 |

关闭终端、退出 `./cage claude` 都不会停止容器，需要停止时执行 `./cage down`。

## 常见操作

**在环境里装软件**：装上的软件会一直保留。第一次使用 apt 前要先更新索引：

```bash
./cage shell -- sh -c 'sudo apt-get update && sudo apt-get install -y jq'
```

**回到宿主**：在笼子里用 `ssh host`。宿主上的命令使用宿主自己的网络和凭证，所以宿主能连上的内网机器，这里也能通过宿主连上：

```bash
./cage shell
ssh host                          # 宿主上的交互式 shell
ssh host 'cd ~/proj && make'      # 一次性命令
scp host:~/notes.txt .            # 复制宿主上的文件
ssh -J host aws-box               # 经宿主连接只有宿主能访问的机器
```

宿主桥默认开启。**开启时，笼子里的任何进程都能以你的身份操作宿主。** 不需要时在 `config/app.yaml` 里写 `host_bridge: {enabled: false}`。

**换节点或出口国家**：`./cage down && ./cage up --node '🇯🇵 Japan 01' --expected-country JP`。

**同时运行多个实例**：每个实例用不同的名字和 UI 端口，环境互不影响，例如 `./cage up --instance test --port 3003`。之后的命令都加上 `--instance test`。

**升级 Claude Code**：像真实机器一样在环境里升级：`./cage shell -- sudo npm i -g @anthropic-ai/claude-code@latest`。

## 数据保存在哪里

| 内容 | 位置 | 说明 |
| --- | --- | --- |
| 应用配置、节点文件 | `config/app.yaml`、`config/mihomo.yaml` | Git 忽略；节点文件权限 `600` |
| Claude 登录、设置和会话 | `state/<实例>/claude/` | `700` 权限，独立于系统和 HOME 挂载 |
| Linux 系统和 HOME | `state/<实例>/system/`、`state/<实例>/home/`；macOS 上在 `state/<实例>/disk.img` 里 | 实时写入；`/tmp` 不保留 |
| 浏览器登录和 Cookie | `state/<实例>/browser/`；macOS 上在 `state/<实例>/disk.img` 里 | 实时写入 |
| 宿主桥和实例登记 | `state/<实例>/bridge/`、`state/<实例>/bridge-host/`、`state/.instances/<实例>.json` | 实例登记固定在项目下，用于识别丢失的状态盘 |

升级前先 `./cage down`。下一次 `./cage up` 会迁移旧版默认位置里的 Claude 状态、已启用宿主桥的密钥与日志，以及实例登记。显式配置的 `claude.config_dir` 继续使用原位置。迁移规则见 [配置说明](docs/configuration.md#workspace-与-claude-授权持久化)。
| 项目文件 | 宿主上的原目录 | workspace 按原路径挂载，两边看到的是同一份文件 |

`state/` 下有 root 属主的文件。删除实例前先 `./cage down`，再删除实例目录和 `state/.instances/<实例>.json`。实例目录可能需要 sudo，删除后会丢失其中的登录状态。

如果实例还未完成迁移，登记可能仍在 `config/instances/<实例>.json`，删除实例时也要清理这份登记。

macOS 上，整个环境是一个 ext4 镜像文件 `state/<实例>/disk.img`。它是稀疏文件，按实际用量占用空间，容器里删除文件后空间会还给 Mac（详见[设计文档 §4.6](docs/stateful-service.md)）。

- **搬家或备份**：先 `./cage down`，再复制 `state/<实例>/`，其中包含登录凭证和宿主桥密钥。显式设置的 Claude 目录如果在实例目录外，需要另外复制。Mac 上可用 `cp -c` 克隆稀疏镜像。
- **删除实例**：先 `./cage down` 释放 loop 设备，再删除实例目录和 `state/.instances/<实例>.json`。
- **Time Machine**：它按整个文件备份，`disk.img` 一直在变，每次备份都可能复制好几 GB。不需要的话可以排除：`tmutil addexclusion -p state`。
- **Mac 磁盘写满**：环境里的写入会失败，文件系统可能转为只读。腾出空间后执行 `./cage down`、`./cage up`；ext4 会在挂载时重放日志。

## 出问题时

- 先看 `./cage status` 和 `./cage logs`。网络断开时，日志里会写明原因。
- 终端只显示每一步的结论（✓ 通过、✗ 失败、! 需要注意）。镜像构建和前台检查时容器自身的输出写在 `state/.logs/`，失败时会提示具体文件。给脚本用时加 `--json`，例如 `./cage status --json`、`./cage check audit --json`。
- `permission denied: ./cage`：执行 `chmod +x cage`。
- 启动时报 `Unable to locate package`：先执行 `sudo apt-get update`。
- 检查网络隔离是否生效：`./cage check verify`。完整的 11 项审计：实例运行中时 `./cage check audit` 直接检查当前实例（不中断使用）；要用真实删除网关的方式演练断网，换一个新实例：`./cage check audit --instance caged-audit --port 3003`。结果保存在 `state/<实例>/last-audit.json`。
- 宿主开着 Clash Verge 等 TUN：不需要关掉，也不需要加规则。`./cage` 会自动改用宿主拨号器，经物理网卡连接节点，启动时会打印 `✓ Host dialer  node connection bound to en0 …`。`./cage status` 的 `host dialer` 一行显示它的状态，日志在 `state/.dialer/dialer.log`。
- 网络断开且原因提到 “node itself may be unreachable”：节点不通，或者节点域名换了 IP。先看 `./cage status` 中的 `node address`，需要时 `./cage down && ./cage up` 重新解析。

## 文档

- [配置参考](docs/configuration.md)：全部配置项、优先级、端口、workspace、launcher 的环境变量
- [架构](docs/architecture.md)：网络边界、持续检查、数据生命周期、验证与审计、目录结构
- [有状态环境设计](docs/stateful-service.md)、[宿主桥设计](docs/host-bridge.md)、[实现原理](docs/implementation.md)
