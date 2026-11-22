# 有状态 Linux 环境设计

> 状态：设计方案，尚未实现。当前代码中 `./cage up` / `./cage down` 仍使用 `--rm` 临时容器，`./cage down` 会丢弃容器内的系统修改；只有 `config/.claude` 和 workspace 保留。本文替代此前“保留容器可写层 + 具名卷”的方案。

## 1. 目标

用户应当感觉 Claude 容器是一台**有状态、可随意修改的 Linux**：装软件、改配置、写文件，下次进入还在。具体要求：

1. **启停仍由 Docker 控制**，但状态**不依赖容器常驻**：容器可以随时被删除、重建。
2. **状态以宿主文件保存**，Docker 本身消失（重装、清理镜像和卷、换 Docker 发行版）也能恢复。
3. **实时落盘**：用户随时可能退出，进程、网络、宿主都可能意外中断，不能依赖“退出时保存”或定时快照。
4. **默认保存用户内容，必要时保存整个系统**：用户主要在 HOME 下工作，但对 `/etc`、apt 安装等系统修改也要保留，并尽量轻量。
5. 重建环境、定时快照等命令可以提供，但属于次要能力。

## 2. 参考：Claude-Cage 如何保存环境

参考 [Lee-xeo/Claude-Cage](https://github.com/Lee-xeo/Claude-Cage)（阅读版本 `469ea36`，2026-11-22）。它的两种运行模式用了两种持久化方式。

### 本地模式：整台 Debian 虚拟机 + 持久虚拟磁盘

- 不是容器。用内嵌的 [Lima](https://lima-vm.io/) 运行一台完整的 Debian 13 虚拟机，后端分别是 macOS 的 Virtualization.framework、Intel Mac/Linux 的 QEMU（KVM），以及 Windows 的 WSL2/HCS（`internal/vm/driver_*.go`）。
- 首次安装下载并校验 Debian 13 基础镜像（约 320–325 MiB），再创建虚拟磁盘。磁盘容量上限 24 GiB，**按实际写入增长**（`internal/vm/lima.go` 中的 `.disk` 设置）。
- **保存位置**：
  - 数据根默认是 `~/.claude-cage`，可用 `CLAUDE_CAGE_CONFIG` 修改；账户、设置、代理配置也放在这里。
  - 虚拟机实例目录是 `~/.claude-cage/state/lima/<实例>/`（`internal/cli/app.go` 的 `LimaHome`，可用 `AGENT_VM_LIMA_HOME` 覆盖），其中是 Lima 的基础磁盘和差异磁盘。
  - 环境存储可以迁移到其他磁盘。迁移后用存储标记文件确认磁盘仍是原来那块，磁盘未连接时提示“环境存储需要恢复”，**不会新建环境覆盖原状态**（`internal/cage/storage.go`）。
- **如何维持状态**：整个 Linux 根文件系统就在这块虚拟磁盘上。客机内的每次写入都直接写入宿主上的磁盘文件，掉电后由客机文件系统的日志恢复一致性。更新或重装 Cage、重启电脑都不会更换环境或清空磁盘。
- 旧环境不再使用时，把它的磁盘整体**移动**（不复制、不删除）到 `state/guests-removed/<系统>-<版本>-<时间>`，并附 `manifest.json`。任一文件移动失败就全部放回原处。

### 云端模式：常驻容器 + 私有容器引擎

- 在服务器上运行私有的 rootless Docker 引擎，`--data-root=~/.claude-cage/container-engine/data`（`deploy/cloud/cage-container-engine.service`）。这样容器的可写层就落在自己的数据目录下，不依赖系统 Docker。
- 工作环境是**常驻容器**，容器内运行 systemd（需要用户命名空间内的 `SYS_ADMIN` 和 `/dev/fuse`、`/dev/net/tun`）。文档明确说明：“已有容器的整个可写文件系统都属于持久数据”。
- 容器**禁止**任何 bind mount 和 volume（`internal/containerenv/engine.go`），状态完全依靠可写层。
- 重建或换机时把停止的旧容器改名为 `<名称>-archive-<id>` 归档，保留回滚能力（`internal/containerenv/reset.go`）。

### 对本项目的启发

| Claude-Cage 的做法 | 是否适合本项目 | 原因 |
| --- | --- | --- |
| 整机虚拟机 + 增量虚拟磁盘 | 不采用 | 本项目定位是 Docker 内运行；内嵌虚拟机需要逐平台维护虚拟化后端 |
| 私有引擎 + 常驻容器可写层 | 不采用 | 状态仍依赖容器常驻；私有引擎在 macOS 上还需要额外的 Linux 虚拟机；systemd 需要额外权限 |
| 状态目录有标记，不覆盖缺失的存储 | **采用** | 外接盘未挂载、路径写错时，宁可报错，也不能静默新建空环境 |
| 重置时移动归档，而不是删除 | **采用** | 重置可以回滚，且不需要复制大文件 |
| 固定 digest 的 Debian 基础镜像 | **采用** | 初始化结果可复现 |

## 3. 方案对比

| 方案 | 实时性 | Docker 消失后 | 空间 | 隔离代价 | 结论 |
| --- | --- | --- | --- | --- | --- |
| A. 保留容器可写层（原方案） | 实时 | 丢失 | 只存差异 | 无 | 依赖容器常驻，不满足目标 1、2 |
| B. 具名卷 | 实时 | 丢失 | 按目录 | 无 | 卷在 Docker 存储里，不满足目标 2 |
| C. 定时快照或同步到宿主 | 有窗口期 | 可恢复 | 快照大小 | 无 | 不满足目标 3 |
| D. 容器内 overlayfs，upper 层放宿主 | 实时 | 可恢复 | 只存差异 | 需要 `CAP_SYS_ADMIN` 或 FUSE | 削弱隔离，不采用 |
| E. ext4 镜像文件挂到 loop 设备 | 实时 | 可恢复 | 稀疏文件 | 需要特权挂载 loop 设备 | 同上 |
| **F. 可变目录 bind mount 到宿主状态目录** | **实时** | **可恢复** | 完整复制一次（约 1 GB） | **无新增权限** | **采用** |

方案 F 用一次性的空间开销，换取实时、跨 Docker 生命周期、不增加容器权限这三点。

## 4. 方案：可变目录全部放在宿主状态目录

### 4.1 目录布局

每个实例一个状态目录，默认是项目下的 `state/<实例>/`（加入 `.gitignore`），可用 `state.dir` / `--state-dir` 改到其他磁盘：

```text
state/<实例>/
├── .caged-state          # 标记：实例 ID、布局版本、初始化所用镜像 digest、创建时间
├── lifecycle.lock        # 启停、初始化、重置、快照互斥
├── system/               # 系统层，重置时整体归档
│   ├── etc/   → /etc
│   ├── usr/   → /usr     （merged-usr：/bin /sbin /lib 都是指向 /usr 的符号链接，一并覆盖）
│   ├── var/   → /var     （dpkg/apt 数据库，必须与 /usr 一同保存）
│   ├── opt/   → /opt
│   ├── srv/   → /srv
│   └── root/  → /root
├── home/      → /home    # 用户层：claude-user 的 HOME 是 /home/claude，包含 .claude
├── archive/<时间>/        # reset 移入的旧 system/，附 manifest.json
└── snapshots/<时间>.tar.zst
```

不持久化的目录和真实 Linux 重启后的表现一致：`/proc`、`/sys`、`/dev`、`/run`、`/tmp` 由 Docker 或 tmpfs 提供。Docker 管理的 `/etc/hosts`、`/etc/hostname`、`/etc/resolv.conf` 会叠加在 `/etc` 的挂载之上，所以 DNS 仍由网关控制。

`home` 和 `system` 分开存放：用户内容和系统层可以分别重置、备份。

### 4.2 首次初始化（seed）

`./cage up` 发现实例从未初始化时：

1. 取得 `lifecycle.lock`。
2. **能力检测**：在状态目录里用临时容器检查属主（任意 UID/GID）、`setuid`、符号链接、硬链接、权限位能否被如实保存。任一项不支持就停止并说明原因，**不会悄悄降级**成只保存 HOME（见 §8）。
3. 用 Claude 镜像运行一次初始化容器（root、无网络），执行 `tar --numeric-owner --xattrs -cpf - etc usr var opt srv root home`，解包到 `state/<实例>/.seed-<id>/`。
4. 校验完成后，原子改名为 `system/` 和 `home/`，**最后**写入 `.caged-state`。中断后只会留下 `.seed-*`，下次清理后重来，不会出现初始化到一半的环境。

### 4.3 每次启动

容器仍然是可丢弃的外壳（继续使用 `--rm`），每次 `./cage up` 都重新创建：

```text
--mount type=bind,src=state/<实例>/system/etc,dst=/etc     （usr、var、opt、srv、root 同理）
--mount type=bind,src=state/<实例>/home,dst=/home
--mount type=bind,src=<项目>/caged,dst=/caged-runtime/caged,readonly
--mount type=bind,src=<项目>/containers/claude/entrypoint.sh,dst=/caged-runtime/entrypoint,readonly
```

- **标记必须存在**：状态目录存在但 `.caged-state` 缺失、实例 ID 不符，或配置里记录过这个实例但目录不见了，都拒绝启动，提示“环境存储不可用”，绝不自动重新初始化（借鉴 Claude-Cage 的存储标记）。
- **身份**：`claude-user` 的 UID/GID 跟随宿主（PUID/PGID）。entrypoint 用 `usermod` 修改的 `/etc/passwd` 位于持久化的 `/etc` 中，所以会保留。PUID 变化时，只在属主不一致的情况下 chown `/home/claude`。
- **启动器自己的代码不进入状态目录**：entrypoint、环境探针、检查程序从项目目录只读挂载到 `/caged-runtime`，用 `PYTHONPATH=/caged-runtime` 运行。这样用户（包括容器内 root）无法修改它们，项目更新后下次启动就生效。探针依赖状态里的 `python3`；用户如果弄坏了 `python3`，探针会失败，网关随即断网（失败即断网），不会放行。
- 网关和浏览器不变：网关的凭证和密钥仍只放在 tmpfs，不持久化；浏览器资料仍是临时的（是否持久化见 §10）。

### 4.4 实时性与一致性

- 容器内的写入就是对宿主文件的写入，不需要“保存”这一步，也不存在窗口期。可靠程度和一台普通 Linux 机器相同：宿主突然断电时，可能丢失最后几秒尚在页缓存里的数据。
- `./cage down` 停止容器后，再对状态目录执行一次 `sync`，缩短这个窗口。
- 容器被 kill、进程消失、网络断开都不影响已经写入的文件。下次 `./cage up` 重新挂载同一份状态即可。

## 5. 命令语义

| 命令 | 行为 |
| --- | --- |
| `./cage up` | 未初始化则 seed；然后挂载状态启动，网络检查流程不变 |
| `./cage down` | 停止并删除容器，状态目录保留，并执行 `sync` |
| `./cage reset [--home]` | 停止后把 `system/` **移动**到 `archive/<时间>/`（同盘上是改名，瞬间完成），再从当前镜像重新 seed 系统层。HOME 默认保留，`--home` 时一并归档 |
| `./cage archive list\|restore <时间>\|prune <时间>` | 查看、回滚、删除归档；删除需要输入实例名确认 |
| `./cage snapshot` | `docker pause` 应用容器，打包 `system/` 和 `home/` 为 `snapshots/<时间>.tar.zst`，再 `unpause`。可用宿主 cron 或 systemd timer 定时调用 |
| `./cage restore <快照>` | 停止后把当前状态归档，再从快照恢复 |
| `./cage destroy` | 删除整个实例状态。文件属主有 root，所以通过容器执行；需要输入实例名确认 |

升级方式和真实机器一样：在容器里执行 `apt upgrade`、`npm i -g @anthropic-ai/claude-code@<版本>`，或者用 `./cage upgrade claude` 代为执行。**修改 Dockerfile 只影响新实例和 `reset`**，已有实例以状态目录为准。

## 6. 各类故障如何恢复

| 情况 | 恢复方式 | 数据 |
| --- | --- | --- |
| 用户退出 Shell 或 Claude | 无需操作 | 全部保留 |
| 进程崩溃、容器被删除或 kill | `./cage up` | 全部保留 |
| 网络断开、节点失效 | 网关断网，恢复后 `./cage resume` | 全部保留 |
| 宿主重启 | `./cage up` | 全部保留（掉电时可能丢最后几秒） |
| Docker 重装，镜像、卷、缓存全部丢失 | `./cage up`：重建镜像，挂载原状态 | 全部保留 |
| 外接盘未挂载、状态路径写错 | 拒绝启动并提示，接回磁盘后再 `up` | 不会被覆盖 |
| 系统被折腾坏 | `./cage reset`，必要时 `archive restore` | HOME 保留，旧系统层可回滚 |
| 换一台机器 | 复制 `state/<实例>/`（同为 Linux、同一 CPU 架构） | 全部保留 |

## 7. 基础镜像

- 使用固定 digest 的 **Debian 13（trixie）slim**，和 Claude-Cage 以及官方 node 镜像所用的发行版一致。如果更习惯 Ubuntu，可以换成 `ubuntu:24.04`，方案的其余部分不变（见 §10）。
- Node.js 通过多阶段构建，从固定版本的官方 `node:24-trixie-slim` 复制 `/usr/local` 下的 node、npm、corepack，不添加第三方 apt 源。Claude Code 继续使用固定版本。
- 镜像只用于 seed 和提供容器外壳，内容被挂载覆盖，因此不再把运行时代码 COPY 进镜像。
- 预计每个实例的初始占用约 0.8–1.2 GB，主要是 `/usr`。

## 8. 风险与限制

1. **macOS 的文件共享能否保真**：Linux（包括 WSL 的 ext4）上 bind mount 能完整保留属主、setuid、硬链接。OrbStack 和 Docker Desktop 的 macOS 文件共享未必能，所以由 §4.2 的能力检测把关。检测不通过时，先只支持 Linux/WSL；macOS 的替代方案（例如在 Docker 虚拟机里放 ext4 镜像文件）作为后续议题。
2. **宿主上出现 root 属主的文件**：`system/` 下的文件属主是 root，宿主的普通用户不能直接删除或修改，所以由 `./cage destroy` 和 `archive prune` 通过容器处理。备份时需要 `sudo tar --numeric-owner`，或使用 `./cage snapshot`。
3. **空间**：每个实例完整复制一份系统，且不是增量存储。宿主文件系统支持 reflink（btrfs、XFS、APFS）时，snapshot 和 archive 可以改用 `cp --reflink` 优化。
4. **持久化的系统也会持久化用户的破坏**：例如修改 `/etc/profile`、替换 `python3`。网络边界由网关的命名空间和防火墙保证，与容器内系统无关；探针代码只读挂载，失败时断网。因此安全边界不变，最坏情况只是环境不可用，可以 `reset`。
5. **不提供 init/systemd**：服务的文件和配置会保留，但不会自动运行。Claude-Cage 用 systemd 需要额外权限，本方案不采用（见 §10）。

## 9. 迁移与实现阶段

### 迁移

现有实例是 `--rm` 容器，系统层本来就不会保留，所以只需要迁移 Claude 登录状态：

- 首次 seed 时，把 `config/.claude` **复制**到 `state/<实例>/home/claude/.claude`；
- 原目录保留到用户确认后再删除；
- `CLAUDE_CONFIG_DIR` 改为 `/home/claude/.claude`，`HOME` 从 `/config` 改为 `/home/claude`。

### 阶段 1：持久化主体

- 状态目录布局、标记、锁、能力检测、原子 seed；
- 挂载方案、`/caged-runtime` 只读运行时、HOME 迁移；
- Debian 基础镜像；
- `down` 时 `sync`。

### 阶段 2：维护命令

`reset`、`archive`、`snapshot`、`restore`、`destroy`、`upgrade claude`。

### 阶段 3：按需扩展

浏览器资料持久化、用户级自启动脚本、macOS 替代存储。

### 测试（集成测试使用独立实例，不连接真实节点）

- 修改 `/etc`、`/usr/local/bin`，用本地 `.deb` fixture 执行 `dpkg -i`，在 HOME 写入文件后：`docker kill` → `up`，`down` → `up`，`docker rmi` 镜像 → `up`，三种情况都验证内容完整；
- 写入过程中 `docker kill`，已完成的写入应全部存在；
- 标记缺失、实例 ID 不符、状态目录不存在时拒绝启动；seed 中断后可以重来；
- 能力检测：模拟不支持属主保存的目录时拒绝启动；
- `reset` 保留 HOME 并可回滚；`snapshot`/`restore` 往返一致；
- 网络边界回归：现有 verify/audit 全部通过，探针运行时不可写。

## 10. 待确认

1. 基础镜像用 **Debian 13**（推荐，与参考项目和官方 node 镜像一致）还是 **Ubuntu 24.04**？
2. 状态目录默认放在**项目内 `state/`**（推荐，与 `config/` 并列），还是用户目录下的 `~/.caged-claude/`？
3. 容器内是否给 `claude-user` 提供 `sudo`？不提供时，需要 root 可以用 `./cage shell --root`。
4. 浏览器资料（登录、Cookie）是否也要持久化？目前的设计是临时的。
5. macOS 能力检测不通过时，是先只支持 Linux/WSL，还是把 macOS 替代存储提前到阶段 1？
