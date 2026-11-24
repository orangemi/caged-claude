# 宿主桥设计：从笼子里回到宿主机

> 状态：阶段 1 已实现（§9）。审批与审计不在本文范围内，以后与笼子内命令的审计一起单独设计（§8）。

## 1. 场景

Claude 在笼子里写代码，但有些事必须回到宿主机上做：

- 在宿主上执行构建，或使用只有宿主才有的工具；
- 从宿主连接云服务器（宿主才有对应的网络和密钥）；
- 读取宿主上的文件和密钥，包括 Mac 上的文件；
- 既有交互式操作（shell、ssh 到云服务器），也有一次性命令。

以前的做法是在宿主上开一个 sshd，再在网络白名单里放行一个 `10.x` 内网地址，让笼子连回来。这个做法在本项目里行不通：网关把所有 TCP 都送进 TUN，以 GLOBAL 模式转发到远处的代理节点。即使防火墙放行了 `10.x`，流量也会被绕到节点去，连不回宿主。

## 2. 原则

1. **尽量不开网络口子。** Linux/WSL 上，笼子和宿主之间通过一个 Unix socket 文件通信，不经过网卡、TUN 或代理节点，网关防火墙一条规则都不改，延迟可以忽略。Mac 上 socket 无法跨越 Docker 的虚拟机，所以宿主作为唯一的特例，只放行一个回环端口，并且直连、不经过节点（§6.2）。
2. **宿主命令遵守宿主自己的环境。** 通过桥执行的命令，网络、代理、PATH、凭证都按宿主本来的配置：宿主有代理就走代理，没有就直连。桥不强加笼子的出口规则。笼子里的流量仍然只走节点，两者互不影响。

   例如宿主已经连在公司的 AWS 内网（VPN 或专线）里，而笼子没有这个网络。通过桥回到宿主后，`ssh -J host aws-box` 由宿主直接连到 AWS 内网，不经过代理节点，也不需要给笼子开任何内网口子。
3. **用成熟的协议，不自己造。** 交互式 shell、一次性命令、文件传输、端口转发都要支持，所以在 socket 上跑标准 SSH 协议。
4. **默认开启，可以关闭。** 这是常用功能，所以默认开启；`host_bridge.enabled: false` 时完全不启动，不挂载 socket，也不生成密钥。开启时，笼子里的任何进程（包括 root，以及被提示注入的 Claude）都能以你的身份在宿主上执行命令。这正是这个功能的用途，README 中会醒目说明，并在 `./cage status` 中显示。

## 3. 架构

```text
Claude 容器                                   宿主机
───────────                                   ──────
ssh host / scp / rsync
  └ ProxyCommand: hostpipe.py ──┐
                                │  state/<实例>/bridge/host.sock
/run/caged-host/host.sock ──────┘  （只读挂载进容器）
                                         │
                                   宿主桥监听进程（./cage up 启动）
                                         │ 每个连接
                                         ▼
                                   sshd -i -f state/<实例>/bridge-host/sshd_config
                                   （以你的用户身份运行，不监听任何端口）
                                         │
                                   你的 shell，使用宿主的网络、代理和文件
```

### 3.1 socket 与挂载

- socket 位于 `state/<实例>/bridge/host.sock`，权限 `600`，属于你的用户。
- `state/<实例>/bridge/` **以只读方式**挂载到 Claude 容器的 `/run/caged-host/`。连接 socket 不需要写权限；只读挂载使容器无法把 socket 换成自己的，也无法在这个目录里放符号链接。
- 容器里的 `claude-user` 与宿主用户 UID 相同，因此能连接这个 socket。

### 3.2 宿主桥监听进程

- 由 `./cage up` 在**宿主上**启动（不在 launcher 容器里），以后台方式运行。pid 文件为 `state/<实例>/bridge/listener.pid`；`./cage down` 停止它，并删除 socket。
- 监听 `host.sock`，每接受一个连接，就启动一次 `sshd -i`，把连接作为它的标准输入输出。连接结束，sshd 随之退出。
- 拒绝以 root 身份运行。启动时清理残留的 socket 和 pid 文件。
- 监听程序使用宿主的 `python3`（只用标准库）。这是开启这个功能时宿主需要的额外依赖；不开启时，宿主仍然只需要 Docker 和 `sh`。
- 监听进程意外退出后，`ssh host` 会失败。`./cage status` 会显示这个状态，再次执行 `./cage up` 即可恢复。

### 3.3 sshd：项目专用、非 root、按需启动

宿主只需要安装 `sshd`。专用配置、主机密钥和日志保存在 `<state.dir>/<实例>/bridge-host/`（`700`）。启动器会从旧版的 `config/host-bridge/<实例>/` 迁移这些文件。

| 文件 | 用途 |
| --- | --- |
| `sshd_config` | 专用配置（见下） |
| `ssh_host_ed25519_key` | 桥专用的 host key，首次启用时生成，`600` |
| `authorized_keys` | 只包含容器的客户端公钥 |
| `sshd.log` | sshd 输出（`-E`），记录登录和会话 |

`sshd_config` 的要点：

```text
HostKey <state.dir>/<实例>/bridge-host/ssh_host_ed25519_key
AuthorizedKeysFile <state.dir>/<实例>/bridge-host/authorized_keys
AllowUsers <宿主用户名>
PubkeyAuthentication yes
PasswordAuthentication no
KbdInteractiveAuthentication no
UsePAM no                 # 非 root 的 sshd 不能使用 PAM
PermitRootLogin no
PermitTTY yes             # 支持交互式 shell
AllowTcpForwarding yes    # 支持 ssh -L 访问宿主本机的服务（见 §7 第 2 条）
X11Forwarding no
LogLevel VERBOSE
PidFile none
```

非 root 的 sshd 只能登录运行它的那个用户，这正好符合需要：容器只能以你的身份回到宿主。

### 3.4 容器侧客户端

- **客户端密钥**：首次启用时，由宿主侧生成 `ed25519` 密钥对。私钥放在 `state/<实例>/bridge/client_ed25519`（`600`，你的用户所有），随这个目录以只读方式出现在容器的 `/run/caged-host/` 中。容器里的 `claude-user` 与你的 UID 相同，所以能读取它，但无法替换它。公钥写入同一实例的 `bridge-host/authorized_keys`。
- **固定 host key**：宿主桥的 host key 公钥写入 `/run/caged-host/known_hosts`，容器在连接时校验，不会接受其他 host key。
- **客户端配置**：entrypoint 每次启动时重写 `/etc/ssh/ssh_config.d/caged-host.conf`。不修改用户自己的 `~/.ssh/config`：

  ```text
  Host host
      HostName caged-host
      HostKeyAlias caged-host
      User <宿主用户名>
      IdentityFile /run/caged-host/client_ed25519
      IdentitiesOnly yes
      UserKnownHostsFile /run/caged-host/known_hosts
      StrictHostKeyChecking yes
      ProxyCommand /usr/bin/python3 /caged-runtime/caged/runtime/hostpipe.py /run/caged-host/host.sock
  ```

- `hostpipe.py` 位于只读的 `/caged-runtime` 中，只做一件事：在 socket 和标准输入输出之间双向转发字节。容器里不需要安装 `socat` 或 `nc`。
- **需要 `openssh-client` 和 `rsync`**：新镜像会安装它们。已经初始化好的状态（例如当前环境）里没有它们，需要执行一次 `./cage shell -- sh -c 'sudo apt-get update && sudo apt-get install -y openssh-client rsync'`。`./cage up` 检测到桥已开启但容器里缺少它们时，会打印这条命令作为提示。

### 3.5 谁在什么时候做什么

宿主监听进程必须运行在宿主上，而 launcher 在容器里，所以分工如下：

1. `./cage up` 在 `state/.plans/` 下创建一个临时计划文件，通过 `CAGED_BRIDGE_PLAN` 传给 launcher；
2. launcher 照常启动三个容器。启动成功后，它把桥的计划写进这个文件，包括动作（`start`，或者关闭时的 `stop`）、实例、用户和路径；
3. launcher 退出后，`./cage` 用**宿主的** `python3` 执行 `caged/host/bridge.py <动作> <计划>`。这个程序只用标准库，不依赖 PyYAML，也不依赖 launcher 的其他模块。它负责生成密钥和配置，用 `sshd -t` 校验配置，然后转为后台进程，并在 socket 就绪后才返回；
4. `./cage down` 停止容器后，同样执行 `bridge.py stop`，即使停止容器失败也照样执行；
5. 桥启动失败只打印警告，不影响 `./cage up` 的结果。

## 4. 使用方式

| 需求 | 在笼子里执行 |
| --- | --- |
| 打开宿主的交互式 shell | `ssh host` |
| 在宿主上执行一次性命令 | `ssh host 'cd ~/proj && make build'` |
| 从宿主连接云服务器（交互式） | `ssh -J host cloud-server`，或先 `ssh host` 再 `ssh cloud-server` |
| 连接只有宿主能到达的内网机器（例如公司 AWS 内网） | `ssh -J host 10.0.1.23`；宿主上 `~/.ssh/config` 里的别名和密钥同样可用，例如 `ssh -J host aws-box` |
| 访问内网里的 HTTP 服务 | `ssh -L 9000:internal.example:443 host`，之后在笼子里访问 `localhost:9000` |
| 读取或复制宿主文件 | `scp host:~/notes.txt .`、`rsync -a host:~/data/ ./data/` |
| 访问宿主本机的服务 | `ssh -L 8080:localhost:8080 host` |

`ssh -J host cloud-server` 时，到云服务器的那一跳由宿主发起，使用的是宿主的网络和宿主的 `~/.ssh` 配置，与笼子的代理出口无关。

### 4.1 交互式与一次性命令

- **人在终端里使用：完全支持交互。** 在 `./cage shell` 里执行 `ssh host`，得到宿主上一个完整的交互式 shell：PTY、颜色、窗口大小变化，以及 `vim`、`htop`，都可以正常使用；也能在里面继续 `ssh` 到云服务器。单个交互程序可以用 `ssh -t host htop`。
- **Claude 自己执行：使用一次性命令。** Claude Code 执行命令时没有终端，无法应答密码提示，也无法操作全屏程序。所以 Claude 应该使用 `ssh host '<命令>'`、`scp`、`rsync` 这类非交互形式。这是 Claude Code 执行命令方式的限制，与桥无关。宿主上的命令如果需要输入密码，应该事先在宿主上配置好，比如使用 ssh-agent 或免密码 sudo 规则。

## 5. 配置与命令

```yaml
host_bridge:
  enabled: true           # 默认开启；false 时完全不启动
  # user: alice           # 宿主用户名，默认取运行 ./cage 的用户
  # sshd: /usr/sbin/sshd  # 默认自动查找
```

宿主依赖：开启时需要宿主上有 `python3`（只用标准库）和 `sshd` 程序，不需要开启系统的 SSH 服务。因为桥默认开启，缺少依赖时 `./cage up` **不会失败**：会打印警告，例如 `host bridge unavailable: sshd not found; install openssh-server or set host_bridge.enabled: false`，然后照常启动笼子。`./cage status` 会显示 `host bridge: unavailable (<原因>)`。

| 命令 | 行为 |
| --- | --- |
| `./cage up` | 开启时：首次生成 host key 和客户端密钥；启动宿主监听进程；挂载 `/run/caged-host` |
| `./cage down` | 停止监听进程，删除 socket |
| `./cage status` | 显示 `host bridge: listening` / `stopped` / `unavailable (<原因>)` / `disabled` |
| `./cage bridge rotate` | 重新生成客户端密钥，替换 `authorized_keys`（怀疑密钥泄露时使用） |

## 6. 宿主平台

| 宿主 | 传输方式 | 说明 |
| --- | --- | --- |
| Linux | Unix socket（`transport: unix`） | 同一内核，可靠 |
| WSL | Unix socket（`transport: unix`） | 项目和状态需要放在 WSL 的 Linux 文件系统内，状态持久化本来也要求这一点 |
| Mac | **回环 TCP（`transport: tcp`）** | Unix socket 无法跨越 macOS 与 Docker 的 Linux 虚拟机，见下方实测 |

`transport: auto`（默认）按真实宿主选择：`./cage` 把 `uname -s` 通过 `CAGED_HOST_OS` 传给 launcher，因为 launcher 容器里看到的永远是 Linux。

### 6.1 Mac 实测（维护者，2026-11）

环境：macOS 26.5.2、OrbStack 2.2.5、Docker 29.8.2、Apple Silicon。

| 实验 | 结果 |
| --- | --- |
| Mac 监听 socket，容器挂载后连接（单文件和整个目录都试过） | 失败，`ECONNREFUSED` |
| 容器在挂载目录里创建 socket，Mac 连接 | 失败 |
| 容器在 Linux 卷里创建 socket，Mac 通过 `~/OrbStack` 连接 | 失败 |
| 两个容器通过共享 socket 通信 | 成功 |
| Mac 与容器之间走 TCP | 双向成功 |
| 容器 Unix socket → TCP 中转 → Mac Unix socket | 成功 |

结论：共享路径不会把 macOS 和 Linux 两个内核里的 socket 端点连起来。OrbStack 只为 Docker 自己的 socket 和 ssh-agent 做了专门转发；通用 socket 挂载的需求（OrbStack issue #62）目前仍未解决。

### 6.2 Mac 的做法：宿主作为网关唯一的直连特例

宿主是一个特例：除了代理节点，它是网关**唯一**允许直接连接的目标，而且只放行一个端口。

- **宿主侧**：`bridge.py` 监听 Mac 的 `127.0.0.1:<端口>`。只绑定回环地址，局域网访问不到；容器通过 Docker 的 `host.docker.internal` 转发进来。端口默认按实例名推算，落在 41000–41999 之间，同一实例每次相同；也可以用 `host_bridge.port` 指定。
- **launcher**：启动时把 `host.docker.internal` 解析成 IPv4 地址（OrbStack 上是 `0.250.250.254`），可以用 `host_bridge.address` 指定。如果解析结果是公网地址，就拒绝启动。地址和端口写进运行策略 `host_bridge_tcp`，交给网关。
- **网关防火墙**：只增加两条规则：

  ```text
  -A INPUT  -i eth0 -s <宿主>/32 -p tcp --sport <端口> -m conntrack --ctstate ESTABLISHED -j ACCEPT
  -A OUTPUT -o eth0 -d <宿主>/32 -p tcp --dport <端口> -j ACCEPT
  ```

  这两条位于网络许可开关和私网丢弃规则之前。所以 `./cage pause` 断开公网时，`ssh host` 照常可用，因为它连的是宿主，不是互联网。宿主上的其他端口、其他私网地址仍然被丢弃。这两条规则和其他规则一起安装、一起计入快照，持续检查会核对它们没有被改动。
- **TUN**：mihomo 的 `route-exclude-address` 把 `<宿主>/32` 排除在 TUN 路由之外，所以这条连接直接从网关网卡出去，不会被送到代理节点。
- **容器侧**：仍然是 `ssh host`。entrypoint 把 ProxyCommand 的目标设为 `tcp:<宿主>:<端口>`；密钥、固定的 host key 和用户名都和 Linux 一样，从只读的 `/run/caged-host/` 读取。
- **Linux 和 WSL 完全不受影响**：不使用 TCP 时，网关的规则和 TUN 配置与之前逐字节相同。

Docker Desktop on Mac 尚未实测。它的 `host.docker.internal` 一般解析到 `192.168.65.254`，也能连到 Mac 回环地址上的服务，但还需要验证。

## 7. 安全边界（审批与审计除外）

1. **开启即授权，而且默认开启。** 笼子里任何能读到客户端私钥、能连上 socket 的进程，都能以你的身份在宿主上执行任意命令、读取你能读取的任何文件。这就是这个功能的用途。不需要时，用 `host_bridge.enabled: false` 关闭。是否需要审批、如何审计，留到 §8 统一设计。
2. **端口转发会让笼子访问宿主的网络。** `ssh -L` 能访问宿主能访问的任何地址，这符合原则 2（宿主命令按宿主的环境来）。这个设置只作用于桥专用的 sshd，系统的 SSH 服务不受影响。已确认保持开启。
3. **容器无法篡改通道。** socket 所在目录以只读方式挂载。宿主桥把固定的主机密钥、sshd 配置、`authorized_keys` 和日志保存在单独的 `bridge-host/` 目录，由宿主监听器访问。
4. **系统 SSH 服务保持不变。** 不开放任何网络端口，不修改宿主上已有的 sshd 配置和 `~/.ssh/authorized_keys`。
5. **密钥可以留在宿主上。** 推荐用 `ssh -J host …` 或在宿主 shell 里使用宿主的密钥。这样宿主上的私钥不必复制进容器，被复制进容器的只有桥专用的那把客户端密钥。

## 8. 以后再定

- **审批**：哪些命令需要确认、在哪里确认、如何防止伪造确认界面。SSH 提供了现成的扩展点（`authorized_keys` 的 `command=`、`restrict`，以及 ForceCommand 包装器）。
- **审计**：桥的会话日志，与笼子内命令的记录统一。Claude Code 的会话记录默认位于 `state/<实例>/claude/projects/`，容器可以写入，不能作为可信的审计依据。
- **Docker Desktop on Mac**：实测 §6.2 的方案。

## 决定记录

| 问题 | 结论 |
| --- | --- |
| 默认开关 | 默认开启，`host_bridge.enabled: false` 关闭；缺少依赖时警告并继续启动笼子 |
| 宿主依赖 | `python3` 和 `sshd` 程序 |
| 端口转发 | 桥专用的 sshd 中开启 `AllowTcpForwarding yes` |
| 交互式 | 支持（人在终端里使用）；Claude 自己执行时用一次性命令 |
| Mac | 回环 TCP，宿主是网关唯一的直连特例（方案 A，§6.2）；OrbStack 的前提条件已由维护者实测 |

## 9. 实现阶段与测试

1. 宿主监听进程、按需启动的 sshd、密钥生成、只读挂载、`/etc/ssh/ssh_config.d` 配置、`hostpipe.py`、镜像加入 `openssh-client`，以及 `status` 显示桥的状态。
2. `bridge rotate`，以及对已初始化环境缺少 `ssh` 的提示。
3. Mac 实测，按需加入 TCP 备选方案。

测试：

- 单元测试：sshd 配置生成、密钥与 `authorized_keys` 的生成与轮换、`hostpipe.py` 的双向转发、拒绝以 root 运行、残留 socket 的清理。
- 集成测试（独立实例，不连接真实节点）：
  - 容器内 `ssh host true` 成功，`ssh host id -un` 返回宿主用户；
  - 交互式 PTY 可用；`scp` 往返一致；
  - 关闭桥后 `ssh host` 失败；
  - 容器无法写入 `/run/caged-host`；
  - 网关防火墙规则与关闭桥时完全一致。
