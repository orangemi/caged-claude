# 有状态开发服务设计

本文是后续生命周期调整方案。当前代码已提供 `./cage shell`，但 `./cage up` / `./cage down` 仍使用原来的 `--rm` 临时容器模式；尚未执行本方案中的迁移或更改正在运行的实例。

## 为什么只挂载 `.claude` 不够

`.claude` 保存 Claude 的授权、设置、会话和记忆，不能覆盖整个 Linux 开发环境。其他文件可能分别落在：

| 内容 | 典型位置 |
| --- | --- |
| Shell、Git、SSH 与其他工具配置 | `$HOME`，本项目为 `/config` |
| 用户安装的 Python、Node、Rust 工具与缓存 | `/config/.local`、`.npm`、`.cargo` 等，具体取决于安装方式 |
| Linux Homebrew 与其安装的软件 | `/home/linuxbrew/.linuxbrew` |
| apt 安装的软件、包数据库与系统配置 | `/usr`、`/var/lib/dpkg`、`/etc` 等 |

Docker 容器的可写层在普通停止、再次启动之间保留。删除容器才会删除这层数据；`--rm` 会让进程退出自动触发删除。因此，日常操作应复用原容器，并把明确要跨重建保留的数据放到卷中。[Docker 容器运行与生命周期](https://docs.docker.com/reference/cli/docker/container/run/)

## 建议的服务语义

三个容器作为同一个实例保留，普通启停不再删除：

| 操作 | 目标行为 |
| --- | --- |
| `./cage up`，首次启动 | 创建三个不带 `--rm` 的容器，完成网络与环境检查 |
| `./cage up`，容器已停止 | 启动原 network，再启动原 browser、claude，重新取证后放行 |
| `./cage up`，容器都在运行 | 核对归属、配置和网络关系，返回已有状态 |
| `./cage down` | 先停两个应用，再停 network，保留容器、可写层和数据卷 |
| `./cage shell` / `./cage claude` | 进入已有容器，不影响服务生命周期 |
| 升级、改挂载或重建 | 独立的显式操作；先保存状态，再替换容器 |

保留整组容器是因为应用使用 `--network container:<network ID>`。不能删掉 network 后仅创建一个同名容器，就认为旧应用会加入新网络空间。普通恢复保留原容器 ID；需要替换网关时，必须协调重新创建两个应用的网络绑定。

网关停止后 tmpfs 中的节点参数和控制密钥会消失。恢复不能只执行三个裸 `docker start`：启动器仍需重新通过标准输入注入节点配置，初始化默认拒绝防火墙、监控密钥和限时许可，再重新注册两端环境探针。旧许可、旧就绪文件和旧探针响应都不能充当新一轮启动的证据。故障时应关闭业务出口并保留开发文件，不用删除容器来回滚。

## 两层持久化

第一层是保留容器本身。它能保住日常 Shell 修改、用户工具、apt 安装和其他可写层文件，满足停止后继续工作的需求。

第二层是给明确的开发数据使用具名卷或绑定目录，避免未来正常重建应用镜像时丢失：

| 挂载目标 | 建议保存方式 |
| --- | --- |
| Claude 完整 HOME `/config` | 每实例独立的具名卷，保留 Shell 配置、用户工具与缓存 |
| `/config/.claude` | 保留已有项目 `config/.claude` 绑定，覆盖 HOME 卷中的同名子目录 |
| `/home/linuxbrew/.linuxbrew` | 每实例独立的具名卷，保留 Linux Homebrew 与软件 |
| workspace | 沿用原宿主绝对路径的绑定挂载 |

浏览器现有 `/config` 匿名卷也应明确保留；如果希望其数据跨容器重建继续使用，再改成每实例独立具名卷。网关运行密钥继续放 tmpfs，不做持久化。

Homebrew 的 Linux 默认前缀是 `/home/linuxbrew/.linuxbrew`，与 Mac 的 `/opt/homebrew` 不同，应在 Linux 容器中安装 Linux 版本。初次准备前缀目录和系统依赖可以由容器 root 完成，之后由 `claude-user` 安装软件。Homebrew 本身没有在这次改动中预装。[Homebrew on Linux](https://docs.brew.sh/Homebrew-on-Linux)

apt 等分布在系统目录中的安装仅靠 HOME 和 Homebrew 卷无法跨重建保留。稳定的系统依赖应逐步写进 Claude Dockerfile；临时探索安装依靠原容器可写层延续。涉及替换原容器时，应先将尚未固化的系统修改保存文件系统快照或记录为镜像构建步骤，不能静默覆盖。

## 已有实例的迁移

当前运行的实例仍带 `AutoRemove=true`，不能假定编辑启动脚本能改变它。迁移需要先保存 Claude 的可写层、整个 HOME、Homebrew 前缀（如已安装）和浏览器匿名卷，再创建新的持久化实例。`docker commit` 的文件系统快照不包含绑定挂载或卷的数据，这些要分别保留。[Docker commit](https://docs.docker.com/reference/cli/docker/container/commit/)

迁移和升级应按以下顺序实现并测试：

1. 校验实例归属，记录容器、卷和镜像身份，准备恢复清单。
2. 在停止任何带 `--rm` 的旧容器之前，完成可写层快照与卷备份。
3. 创建新的持久化服务并恢复应用文件；沿用 workspace 与 `.claude` 数据。
4. 重新核对 UID/GID、时区、语言、DNS、代理出口和断网约束。
5. 验证普通 `stop → start` 保持三个容器 ID、安装的软件和文件内容。
6. 验证异常恢复与显式重建路径；备份不随一次成功启动自动删除。

配置中的环境变量、端口、UID/GID 和挂载发生变化时，Docker 不能通过普通 `start` 修改原容器的创建参数。启动器应明确提示需要迁移或重建，不应为了应用配置变化自动丢弃开发环境。
