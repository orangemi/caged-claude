# 有状态 Linux 环境设计

> 状态：阶段 1（§9）已实现，包括 macOS 使用的 image 后端（§4.6）；阶段 2 的维护命令（reset、archive、snapshot、restore、destroy、upgrade）尚未实现。本文替代此前“保留容器可写层 + 具名卷”的方案。

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

每个实例一个状态目录，放在项目下的 `state/<实例>/`（加入 `.gitignore`），可用 `state.dir` / `--state-dir` 改到其他磁盘：

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
├── home/      → /home/claude   # 用户层：claude-user 的 HOME
├── browser/   → 浏览器容器的 /config   # Chromium 资料：登录、Cookie、历史，700 权限
├── archive/<时间>/        # reset 移入的旧 system/（或 home/、browser/），附 manifest.json
└── snapshots/<时间>.tar.zst
```

不持久化的目录和真实 Linux 重启后的表现一致：`/proc`、`/sys`、`/dev`、`/run`、`/tmp` 由 Docker 或 tmpfs 提供。Docker 管理的 `/etc/hosts`、`/etc/hostname`、`/etc/resolv.conf` 会叠加在 `/etc` 的挂载之上，所以 DNS 仍由网关控制。

`system`、`home`、`browser` 分开存放，可以分别重置和备份。

只持久化 `/home/claude`，不持久化整个 `/home`。原因是 Linux 宿主上的 workspace 通常在 `/home/<用户>/…`，并按原路径挂载进容器：如果 `/home` 整体是持久化挂载，Docker 会在 `state/<实例>/home` 里为这些 workspace 建出 root 属主的空挂载点目录。同理，workspace 也不能位于 `/var`、`/opt`、`/srv`、`/root` 之下，配置校验会拒绝。

Claude 的登录凭证**单独存放**，不放在 `state/` 里：

```text
config/claude/<实例>/  → /home/claude/.claude   （叠加在 home 的挂载之上，700 权限）
```

- 凭证最敏感，放在本来就是 `700`、专门存私有配置的 `config/` 中，不和约 1 GB 的状态一起打包或复制。
- 体积小，备份和迁移可以只拷这一个目录。
- `reset --home`，甚至 `destroy` 整个环境之后，仍然可以保持登录，不必重新授权。
- `CLAUDE_CONFIG_DIR=/home/claude/.claude`，原本在 HOME 外层的 `.claude.json` 也放在这里。
- 按实例分目录，两个实例不会同时写同一份凭证。`claude.config_dir` 仍然可以覆盖这个位置。

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
--mount type=bind,src=state/<实例>/home,dst=/home/claude
--mount type=bind,src=config/claude/<实例>,dst=/home/claude/.claude
--mount type=bind,src=<项目>/caged,dst=/caged-runtime/caged,readonly
--mount type=bind,src=<项目>/containers/claude/entrypoint.sh,dst=/caged-runtime/entrypoint,readonly
```

- **标记必须存在**：状态目录存在但 `.caged-state` 缺失、实例 ID 不符，或配置里记录过这个实例但目录不见了，都拒绝启动，提示“环境存储不可用”，绝不自动重新初始化（借鉴 Claude-Cage 的存储标记）。
- **身份**：`claude-user` 的 UID/GID 跟随宿主（PUID/PGID）。entrypoint 用 `usermod` 修改的 `/etc/passwd` 位于持久化的 `/etc` 中，所以会保留。PUID 变化时，只在属主不一致的情况下 chown `/home/claude`。
- **启动器自己的代码不进入状态目录**：entrypoint、环境探针、检查程序从项目目录只读挂载到 `/caged-runtime`，用 `PYTHONPATH=/caged-runtime` 运行。这样用户（包括容器内 root）无法修改它们，项目更新后下次启动就生效。探针依赖状态里的 `python3`；用户如果弄坏了 `python3`，探针会失败，网关随即断网（失败即断网），不会放行。
- 网关不变：节点凭证和控制密钥仍只放在 tmpfs，不持久化。
- 浏览器只持久化资料目录：`state/<实例>/browser` 挂载到浏览器容器的 `/config`；浏览器镜像本身的系统仍是临时的。这里有一个坑：Chromium 的 `SingletonLock` 记录了主机名，而现在网关容器每次重建主机名都会变，新主机名会让 Chromium 判定资料“正被另一台电脑使用”而拒绝打开。所以要做两件事：网关容器使用固定主机名 `caged-<实例>`（两个应用共享它的网络命名空间和主机名）；浏览器启动前清理残留的 `Singleton*` 锁文件。

### 4.4 实时性与一致性

- 容器内的写入就是对宿主文件的写入，不需要“保存”这一步，也不存在窗口期。可靠程度和一台普通 Linux 机器相同：宿主突然断电时，可能丢失最后几秒尚在页缓存里的数据。
- `./cage down` 停止容器后，再对状态目录执行一次 `sync`，缩短这个窗口。
- 容器被 kill、进程消失、网络断开都不影响已经写入的文件。下次 `./cage up` 重新挂载同一份状态即可。

### 4.5 各类内容保存在哪里

二进制和软件：

| 安装方式 | 实际写入位置 | 状态目录 |
| --- | --- | --- |
| `apt install` | 程序在 `/usr/bin`、`/usr/lib`，包数据库在 `/var/lib/dpkg` | `system/usr` + `system/var` |
| root 执行 `npm i -g`（包括 Claude Code 升级） | `/usr/local/lib/node_modules`、`/usr/local/bin` | `system/usr` |
| `pip install --user`、`pipx`、`cargo install` | `~/.local`、`~/.cargo` | `home` |
| nvm、mise、Linux Homebrew 等用户级工具 | `~/.nvm`、`~/.local/share/mise` 等 | `home` |
| 手动下载的二进制 | `/usr/local/bin`、`/opt/<软件>`、`~/bin` | `system/usr`、`system/opt`、`home` |

两点约束：

- **`/usr` 与 `/var` 必须作为一个整体。** 文件在 `/usr`，“装了什么”的记录在 `/var/lib/dpkg`。只保存其中一个，apt 就会认为系统损坏。所以两者同属 `system/`，备份、归档、回滚都整体进行，不拆开。
- **merged-usr 布局**下 `/bin`、`/sbin`、`/lib` 都是指向 `/usr` 的符号链接，保存 `/usr` 就覆盖了全部系统二进制。

下载与数据：

| 内容 | 位置 | 是否保留 |
| --- | --- | --- |
| HOME 下的一切：下载的文件、模型、数据集、`~/.cache`、`~/.npm`、pip 缓存 | `/home/claude/…` | 保留 |
| workspace 项目文件 | 宿主原路径（直接挂载） | 保留 |
| apt 缓存、`/var/tmp` | `/var` | 保留 |
| 浏览器登录、Cookie、历史 | 浏览器 `/config` | 保留 |
| `/tmp` | tmpfs | **不保留**，和真实 Linux 重启一样（Debian 13 的 `/tmp` 默认就是 tmpfs）。需要在文档和 `./cage claude` 的提示中写明 |

### 4.6 image 后端：整个状态放在一个 ext4 镜像文件里（macOS）

macOS 上，宿主目录要经过 OrbStack 的文件共享（FUSE）才能进入 Docker 的 Linux 虚拟机。实测（§4.7）这层共享接受 `chown`，但不保存属主，所以 §4.1 的目录布局在 Mac 上无法通过能力检测。image 后端把**同一套布局**放进一个 ext4 镜像文件，文件本身仍在宿主上：

```text
state/<实例>/
├── .caged-state      # 宿主上的标记：多记录 backend: image 和文件系统 UUID
├── lifecycle.lock
├── disk.img          # 稀疏的 ext4 镜像，上限 state.size（默认 64g），按实际写入占用空间
│                     #   里面是 system/、home/、browser/，与 §4.1 相同
└── bridge/           # 宿主桥，仍是宿主目录
```

- **选择**：`state.backend` 默认 `auto`，macOS 上是 `image`，Linux/WSL 上仍是 `dir`，行为不变。Linux/WSL 也可以显式写 `state.backend: image`。已有环境始终沿用标记里记录的后端；显式配置与之不符时拒绝启动，不会悄悄换后端。
- **挂载方式**（§4.7 的 L3）：一个短暂的特权辅助容器（`containers/storage`，只有 e2fsprogs，无网络）在 Docker 虚拟机里执行 `losetup`，把镜像接到 loop 设备上；随后定义 Docker local 卷 `<实例>-state`（`type=ext4,device=/dev/loopN`）。claude 和 browser 容器用 `volume-subpath` 挂载各个目录，例如 `system/usr → /usr`、`home → /home/claude`、`browser → /config`。这两个容器本身仍然没有特权。Claude 凭证仍是 `config/claude/<实例>/` 的 bind mount（普通文件放在 FUSE 上没有问题）。
- **生命周期**：Docker 在第一个使用卷的容器启动时挂载文件系统，在最后一个容器退出时卸载。`./cage down` 再删除卷的定义（不删数据），并断开 loop 设备，所以停止后的 `disk.img` 是干净、可以直接复制的文件。loop 设备号在 Docker 虚拟机重启后会变化，因此每次 `up` 都重新定义卷；只有没被任何容器使用的卷才会被替换。
- **挂载选项**：loop 设备开启 direct I/O，虚拟机不再把同一份数据额外缓存一遍；卷以 `discard` 挂载，容器里删除文件后，`disk.img` 在宿主上占用的空间随即缩小（§4.7）。
- **身份校验**：标记记录了文件系统 UUID；接入前用 `tune2fs` 比对，被替换的镜像会被拒绝。同一个文件已经接在多个 loop 设备上时也拒绝，避免同一文件系统被挂载两次。不开启 ext4 的多重挂载保护（MMP）：它防的是两台机器同时挂载同一个文件（例如放在网盘里），但实测在 OrbStack 上检查间隔会被拉长到 42 秒，每次异常停机后的第一次挂载要多等一分多钟。
- **首次初始化**：在 `disk.img.tmp` 上建文件系统、接入并 seed，完成后断开、改名为 `disk.img`，**最后**写宿主标记。中断只会留下 `disk.img.tmp`，下次启动清理后重来；没有标记的 `disk.img` 永远不会被删除或覆盖。
- **迁移**：停止实例后，复制整个 `state/<实例>/` 和 `config/claude/<实例>/` 即可，放到另一台 Mac 上，或者放到 Linux 上作为 image 后端使用。在 Mac 上用 `cp -c` 可以利用 APFS 克隆，不占额外空间。dir 与 image 两种后端之间的转换（`export`/`import`）放在阶段 2。
- **代价**：多一次特权容器调用（只做 losetup）；数据的持久性取决于 OrbStack 把 loop 设备的写入和 `fsync` 落到 Mac 文件上的方式。OrbStack 自己的虚拟机磁盘也是 Mac 上的镜像文件，风险属于同一类。

### 4.7 macOS 实测（维护者，2026-11）

环境：macOS 26.5.2、OrbStack 2.2.5、Docker 29.8.2、Apple Silicon。诊断脚本是一次性的，没有保留在仓库中。

| 项目 | 结果 |
| --- | --- |
| Mac 目录 bind mount（FUSE） | 属主不保存（`chown` 后仍是 `0:0`）；setuid、硬链接、符号链接正常 |
| 命名卷（虚拟机内 btrfs） | 能力检测通过，但数据不在宿主上 |
| Mac 上的稀疏 ext4 镜像 | 8 GB 镜像实际占用 6 MB；能力检测通过；卸载、重新挂载后数据仍在；`fsck` 干净 |
| L1：nsenter 到 Docker 主机挂载 | 失败（busybox mount 报 `Invalid argument`） |
| L2：特权容器挂载，经 rshared 传播 | 可用，但要自己管理挂载点 |
| **L3：losetup + local 卷 `device=/dev/loopN`** | **可用；`volume-subpath`、多个容器同时挂载都正常（采用）** |
| L4：local 卷直接 `o=loop` 指向文件 | 失败（内核 mount 不支持 loop 选项） |

性能（1 GB 顺序写 + fsync / 500 次 4 KB 同步写 / 复制 101 MB 的 `/usr`）：Mac 目录 1.18 s / 0.41 s / 0.65 s，命名卷 1.18 s / 0.84 s / 0.41 s，ext4 镜像 1.39 s / 1.01 s / 0.18 s。

第二轮，针对镜像方案的风险：

| 项目 | 结果 | 结论 |
| --- | --- | --- |
| 用真实 Claude 镜像 seed（约 1 GB、1.8 万个文件） | 4 s；`sudo` 的 setuid 保留 | 首次启动不会明显变慢 |
| loop direct I/O | 1 GB 写入 + fsync 从 1.51 s 降到 0.44 s；同步小块写 0.92 s / 1.14 s | 采用 |
| 删除后回收空间 | 以 `discard` 挂载：写入 1 GB 后删除，镜像从 1030 MB 回到 6 MB；不开时仍占 1031 MB，`fstrim` 后为 7 MB | 采用 `discard` |
| `docker kill` 中断写入 | 已 fsync 的 966 个文件全部存在，`fsck` 干净 | ext4 日志足够 |
| 多重挂载保护（MMP） | 能拒绝第二次挂载，但检查间隔被拉到 42 s | 不采用 |
| 在线扩容 | `resize2fs` 在辅助容器中看不到挂载点，按离线处理，打开设备时报 busy | 以后做停机扩容：`truncate` → `e2fsck` → `resize2fs` |

没有测试停止 OrbStack 虚拟机的情况，因为那会中断宿主上的其他容器。

## 5. 命令语义

| 命令 | 行为 |
| --- | --- |
| `./cage up` | 未初始化则 seed；然后挂载状态启动，网络检查流程不变 |
| `./cage down` | 停止并删除容器，状态目录保留，并执行 `sync`；image 后端还会删除卷定义、断开 loop 设备 |
| `./cage reset [--home] [--browser]` | 停止后把 `system/` **移动**到 `archive/<时间>/`（同盘上是改名，瞬间完成），再从当前镜像重新 seed 系统层。HOME 和浏览器资料默认保留，`--home`、`--browser` 时分别一并归档。Claude 凭证在 `config/` 中，任何 reset 都不会触及 |
| `./cage archive list\|restore <时间>\|prune <时间>` | 查看、回滚、删除归档；删除需要输入实例名确认 |
| `./cage snapshot` | `docker pause` 应用容器，打包 `system/` 和 `home/` 为 `snapshots/<时间>.tar.zst`，再 `unpause`。可用宿主 cron 或 systemd timer 定时调用 |
| `./cage restore <快照>` | 停止后把当前状态归档，再从快照恢复 |
| `./cage destroy` | 删除整个实例状态。文件属主有 root，所以通过容器执行；需要输入实例名确认 |

升级方式和真实机器一样：在容器里执行 `apt-get update && apt-get upgrade`（新 seed 的环境没有软件包索引，第一次安装任何包之前也要先 `apt-get update`）、`npm i -g @anthropic-ai/claude-code@<版本>`，或者用 `./cage upgrade claude` 代为执行。**修改 Dockerfile 只影响新实例和 `reset`**，已有实例以状态目录为准。

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

- **已确认**：使用固定 digest 的 **Debian 13（trixie）slim**。当时与 Ubuntu 26.04 LTS 的比较：

  | 方面 | Debian 13 | Ubuntu 26.04 LTS |
  | --- | --- | --- |
  | snap | 没有 snap，`apt install chromium`、`firefox` 是真正的 deb 包 | 这几个包是指向 snap 的过渡包，Docker 里不能运行 snap，**直接装不上** |
  | Node.js | 官方 `node:24-trixie-slim` 就是 Debian，复制过来的二进制与系统库完全一致 | 可以用（glibc 向后兼容），但不完全一致 |
  | 生态 | 一般 | 更好，很多第三方软件只提供 Ubuntu 的 PPA 或安装说明 |
  | 支持周期 | 约 3 年，加 LTS 共 5 年 | 标准 5 年 |

  推荐 Debian，主要原因是 snap：这个环境会长期保留，在里面装浏览器（比如跑 Playwright 测试）是常见需求。
- Node.js 通过多阶段构建，从固定版本的官方 `node:24-trixie-slim` 复制 `/usr/local` 下的 node、npm、corepack，不添加第三方 apt 源。Claude Code 继续使用固定版本。
- 镜像只用于 seed 和提供容器外壳，内容被挂载覆盖，因此不再把运行时代码 COPY 进镜像。
- 预计每个实例的初始占用约 0.8–1.2 GB，主要是 `/usr`。

## 8. 风险与限制

1. **dir 后端只支持 Linux/WSL**：Linux（包括 WSL 的 ext4）上 bind mount 能完整保留属主、setuid、硬链接。macOS 默认使用 image 后端（§4.6）。dir 后端的能力检测仍然保留，检测不通过就报错，并说明哪一项没有保存、应改用哪个后端。
2. **宿主上出现 root 属主的文件**：`system/` 下的文件属主是 root，宿主的普通用户不能直接删除或修改，所以由 `./cage destroy` 和 `archive prune` 通过容器处理。备份时需要 `sudo tar --numeric-owner`，或使用 `./cage snapshot`。
3. **空间**：每个实例完整复制一份系统，且不是增量存储。宿主文件系统支持 reflink（btrfs、XFS、APFS）时，snapshot 和 archive 可以改用 `cp --reflink` 优化。
4. **持久化的系统也会持久化用户的破坏**：例如修改 `/etc/profile`、替换 `python3`。网络边界由网关的命名空间和防火墙保证，与容器内系统无关；探针代码只读挂载，失败时断网。因此安全边界不变，最坏情况只是环境不可用，可以 `reset`。
6. **sudo（已确认）**：默认给 `claude-user` 免密码 sudo，`claude.sudo: false` 可以关闭。
   - 理由：这是一台可以随意修改的长期环境，Claude 经常需要 `apt install`。
   - 网络边界不受影响：容器内 root 没有 `NET_ADMIN`，改不了网关防火墙；改 DNS 也会被公网 53/853 的拦截规则挡住。
   - 能改的只有 `system/`，弄坏了可以 `reset` 或从归档回滚。
   - **代价**：环境探针以 root 运行，用私钥给检查结果签名。给了 sudo，Claude 或用户就能读到私钥，也能停掉探针。时区、语言等环境一致性检查会因此从防篡改降级为尽力而为，网络断开约束不受影响。
   - 关闭 sudo 时，需要 root 就从宿主执行 `./cage shell --root`。
   - “只允许 sudo apt”不提供：apt 可以安装任意 deb 并以 root 执行包里的脚本，实际上等于完整 root。
5. **不提供 init/systemd**：服务的文件和配置会保留，但不会自动运行。Claude-Cage 用 systemd 需要额外权限，本方案不采用（见 §10）。

## 9. 迁移与实现阶段

### 迁移

现有实例是 `--rm` 容器，系统层本来就不会保留，所以只需要迁移 Claude 登录状态：

- 默认实例首次启动时，如果 `config/claude/<实例>/` 不存在而 `config/.claude` 存在，就把后者**移动**过去，登录状态不变。同一块盘上只是改名；失败时保持原样；
- `CLAUDE_CONFIG_DIR` 改为 `/home/claude/.claude`，`HOME` 从 `/config` 改为 `/home/claude`；
- 浏览器资料目前是临时的，没有需要迁移的内容。

### 阶段 1：持久化主体

- 状态目录布局、标记、锁、能力检测、原子 seed；
- 挂载方案、`/caged-runtime` 只读运行时、HOME 迁移、Claude 凭证单独挂载；
- 浏览器资料持久化、网关固定主机名、清理 Chromium 锁文件；
- 新的基础镜像（按 §7 的确认结果）、sudo 开关；
- `down` 时 `sync`。

### 阶段 2：维护命令

`reset`、`archive`、`snapshot`、`restore`、`destroy`、`upgrade claude`。

### 阶段 3：按需扩展

用户级自启动脚本。

### 测试（集成测试使用独立实例，不连接真实节点）

- 修改 `/etc`、`/usr/local/bin`，用本地 `.deb` fixture 执行 `dpkg -i`，在 HOME 写入文件后：`docker kill` → `up`，`down` → `up`，`docker rmi` 镜像 → `up`，三种情况都验证内容完整；
- 写入过程中 `docker kill`，已完成的写入应全部存在；
- 标记缺失、实例 ID 不符、状态目录不存在时拒绝启动；seed 中断后可以重来；
- 能力检测：模拟不支持属主保存的目录时拒绝启动；
- `reset` 保留 HOME、浏览器资料和 Claude 凭证，并可回滚；`snapshot`/`restore` 往返一致；
- 浏览器登录后 `docker kill` 网关和浏览器，再 `up`，Cookie 仍在，Chromium 不报配置被占用；
- 网络边界回归：现有 verify/audit 全部通过，探针运行时不可写。
- image 后端（单元测试模拟 Docker 和 loop 设备）：首次 seed 只在 `disk.img.tmp` 上进行、标记最后写入；再次启动不重新 seed，并在新的 loop 设备上重建卷；被替换或缺失的镜像、被其他容器占用的卷都拒绝；没有标记的 `disk.img` 不会被覆盖。集成测试 `test_image_backend_survives_kill_detach_and_recreation` 在真实 Docker 上走一遍 seed → 写入 → kill → 断开 → 重新接入，Linux 和 macOS 上都可以用 `./cage check test` 运行。

## 10. 决定记录

| 问题 | 结论 |
| --- | --- |
| 状态目录位置 | 项目内 `state/<实例>/`（已确认） |
| Claude 凭证 | 单独存放在 `config/claude/<实例>/`，不随 `state/` 打包或重置（已确认） |
| 浏览器登录与 Cookie | 持久化到 `state/<实例>/browser/`，与 Claude 系统层分开（已确认） |
| macOS | 使用 image 后端：`state/<实例>/disk.img`，经 loop 设备和 Docker local 卷挂载（已确认，§4.6）；Linux/WSL 默认仍用目录，可选 image |
| 浏览器资料（image 后端） | 与系统、HOME 放在同一个镜像里，一个实例的全部状态就是 `disk.img` 加凭证目录（已确认） |
| 基础镜像 | Debian 13（trixie）slim，固定 digest（已确认，比较见 §7） |
| sudo | 默认免密码 sudo，`claude.sudo: false` 可关闭（已确认，代价见 §8 第 6 条） |
