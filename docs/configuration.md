# 配置参考

日常使用只需要 [README](../README.md) 里的几项配置。本文是完整参考：配置文件、优先级、所有配置项、端口、workspace、launcher 的环境变量，以及从旧名称 caged-browser 升级的说明。

## 配置文件

```
config/app.yaml      # 启动配置：节点选择、时区、语言、端口和出口策略
config/mihomo.yaml   # mihomo 节点和凭证
```

整个 `config/` 目录已加入 Git 忽略。仓库只保留 [config.example/app.yaml](../config.example/app.yaml) 和不含真实节点信息的 [config.example/mihomo.yaml](../config.example/mihomo.yaml)，`./cage init` 从这两份模板生成配置；模板中的占位值不能直接用于连接。

`config/app.yaml` 的 `proxy.node` 指定从 `config/mihomo.yaml` 选用的节点，命令行可以临时切换：`./cage up --node '🇸🇬 Singapore 02'`。启动器按项目位置寻找 `config/app.yaml`，从其他目录执行 `./cage` 也读取同一份配置。`--settings` 指定其他应用 YAML；`proxy.file_path` 或 `--proxy-file-path` 指定节点 YAML，省略时读取应用 YAML 同目录的 `mihomo.yaml`。

节点文件可以自己填写，也可以从 Clash Verge 导入完整快照，例如 macOS 上：

```bash
./cage init --mihomo "$HOME/Library/Application Support/io.github.clash-verge-rev.clash-verge-rev/clash-verge.yaml"
```

不会自动读取或同步宿主上的代理配置。

实际 `config/mihomo.yaml` 包含节点凭证，权限设为 `600`，随整个 `config/` 目录一起被 Git 忽略。它在会话结束后保留。配置更新后重启会话读取，不会自动同步 Mac 的后续修改。

第一版支持独立 AnyTLS 节点；有 `dialer-proxy` 的链式节点会拒绝启动，而不是遗漏链路参数。复制的文件保留原始配置内容；容器运行时仍只提取所选节点，重新生成全局代理配置。

## 配置优先级

所有启动选项按 **命令行参数 > 环境变量 > `app.yaml` > 内置默认值** 解析，并且只校验最终生效的值。`--help` 列出完整选项。`config.example/app.yaml` 仅启用必选配置，可选项统一注释并注明默认值；需要覆盖默认值时取消对应注释。无默认值的选项缺失、键名拼错或生效的时区/端口非法时，会在启动容器前报错。

可省略的默认项包括节点文件 `mihomo.yaml`、实例名 `caged-claude`、节点引导 DNS（`https://1.1.1.1/dns-query` 与 `https://223.5.5.5/dns-query`）、运行时 DoH 上游 `https://1.1.1.1/dns-query`、宿主 TUN 检查 `warn`、locale `en_US.UTF-8`、语言 `en-US`、跟随启动者的 UID/主 GID、共享内存 `1g`，以及整个 `ports` 块。节点和期望国家仍由配置或覆盖项明确提供；时区省略时跟随节点（见下文）。

| `app.yaml` 键 | 环境变量 | 命令行参数 |
| --- | --- | --- |
| 应用文件路径 | `CAGED_CLAUDE_SETTINGS` | `--settings` |
| `proxy.file_path` | `CAGED_CLAUDE_PROXY_FILE_PATH` | `--proxy-file-path` |
| `proxy.node` | `CAGED_CLAUDE_NODE` | `--node` |
| `proxy.expected_country` | `CAGED_CLAUDE_EXPECTED_COUNTRY` | `--expected-country` |
| `proxy.bootstrap_dns` | `CAGED_CLAUDE_BOOTSTRAP_DNS` | `--bootstrap-dns` |
| `proxy.dns_upstream` | `CAGED_CLAUDE_DNS_UPSTREAM` | `--dns-upstream` |
| `proxy.host_tunnel` | `CAGED_CLAUDE_HOST_TUNNEL` | `--host-tunnel` |
| `browser.timezone` | `CAGED_CLAUDE_TIMEZONE` | `--timezone` |
| `browser.locale` | `CAGED_CLAUDE_LOCALE` | `--locale` |
| `browser.language` | `CAGED_CLAUDE_LANGUAGE` | `--language` |
| `browser.puid` / `browser.pgid` | `CAGED_CLAUDE_PUID` / `CAGED_CLAUDE_PGID` | `--puid` / `--pgid` |
| `browser.shm_size` | `CAGED_CLAUDE_SHM_SIZE` | `--shm-size` |
| `browser.ui_scheme` | `CAGED_CLAUDE_UI_SCHEME` | `--ui-scheme` |
| `ports.ui` | `CAGED_CLAUDE_PORT` | `--port` |
| `ports.controller` | `CAGED_CLAUDE_CONTROLLER_PORT` | `--controller-port` |
| `ports.mixed` | `CAGED_CLAUDE_MIXED_PORT` | `--mixed-port` |
| `ports.debug` | `CAGED_CLAUDE_DEBUG_PORT` | `--debug-port` |
| `instance` | `CAGED_CLAUDE_INSTANCE` | `--instance` |
| `workspace` | `CAGED_CLAUDE_WORKSPACE` | `--workspace`，可重复 |
| `claude.config_dir` | `CAGED_CLAUDE_CLAUDE_CONFIG_DIR` | `--claude-config-dir` |
| `claude.sudo` | `CAGED_CLAUDE_CLAUDE_SUDO` | `--claude-sudo` |
| `state.dir` | `CAGED_CLAUDE_STATE_DIR` | `--state-dir` |
| `state.backend` | `CAGED_CLAUDE_STATE_BACKEND` | `--state-backend` |
| `state.size` | `CAGED_CLAUDE_STATE_SIZE` | `--state-size` |
| `ports.browser_probe` / `ports.claude_probe` | `CAGED_CLAUDE_BROWSER_PROBE_PORT` / `CAGED_CLAUDE_CLAUDE_PROBE_PORT` | `--browser-probe-port` / `--claude-probe-port` |
| `ports.host_proxy` | `CAGED_CLAUDE_HOST_PROXY_PORTS` | `--host-proxy-ports` |
| `monitor.tick` / `monitor.interval` | `CAGED_CLAUDE_MONITOR_TICK` / `CAGED_CLAUDE_MONITOR_INTERVAL` | `--monitor-tick` / `--monitor-interval` |
| `monitor.lease` / `monitor.probe_timeout` | `CAGED_CLAUDE_MONITOR_LEASE` / `CAGED_CLAUDE_MONITOR_PROBE_TIMEOUT` | `--monitor-lease` / `--monitor-probe-timeout` |
| `monitor.retry_initial` / `monitor.retry_max` | `CAGED_CLAUDE_MONITOR_RETRY_INITIAL` / `CAGED_CLAUDE_MONITOR_RETRY_MAX` | `--monitor-retry-initial` / `--monitor-retry-max` |

例如，用环境变量固定时区，并用命令行覆盖环境变量中的端口：

```bash
CAGED_CLAUDE_TIMEZONE=Asia/Tokyo CAGED_CLAUDE_PORT=3102 ./cage up --port 3103
```

### 时区

`browser.timezone` 可省略（或写 `auto`）。网关测得出口 IP 后，启动器按出口所在地选择时区：

- 定位服务给出的时区属于出口国家时用它，例如美国出口可能是 `America/Chicago`；
- 否则用该国的默认时区：通常是 tzdata `zone.tab` 中的第一个，澳大利亚、巴西、加拿大、俄罗斯、乌克兰、乌兹别克斯坦改用人口最多的时区（如 `Australia/Sydney`、`America/Toronto`）。

填写时必须是某个国家的时区（`UTC`、`Etc/GMT+5` 这类不属于任何国家，直接报错），并且：

- 设置了 `proxy.expected_country` 时，启动容器前就检查时区属于该国家（只在启动时检查，`./cage nodes`、`status`、`down` 不受影响）；
- 网关测得出口后，时区不属于出口国家则拒绝启动，并提示该国可用的时区。

选定的时区同时写入两个容器的 `TZ`、`/etc/timezone` 和 `/etc/localtime`，因此浏览器的 `Intl` 和 shell 里的 `date` 一致。网关在收到这个时区之前不启动监控，也就不放行任何出站流量。运行中复检出口时，如果出口换到别的国家，监控判定违规并断网；`./cage status` 显示当前时区，审计的“出口符合已保存规则”一项也会核对它。附加到已运行实例的命令（`./cage claude`、审计等）沿用该实例的时区；显式指定不同的时区会报错，需要先 `./cage down`。

切换地区时同时指定节点和期望国家，例如 `--node '🇯🇵 Japan 01' --expected-country JP`；没有固定时区时，时区随之变成 `Asia/Tokyo`。将 `proxy.expected_country` 设为 YAML `null` 或 `any`（命令行/环境变量使用 `any`）可以取消国家限制，仍检查连通性及审计中的出口一致性。

`browser.language` 使用 `ja-JP` 等语言标签，自动生成 `ja-JP,ja` 偏好；`browser.locale` 控制系统 locale。两个应用的用户名固定为 `claude-user`；Claude 的 HOME 是 `/home/claude`，浏览器的是 `/config`。`puid`、`pgid` 默认都是 `host`，每次启动读取当前进程的 UID/主 GID；通常等同于运行终端的 `id -u` 和 `id -g`。可显式填写整数；环境变量和 CLI 仍按优先级分别覆盖。使用普通用户执行 `./cage up` 即可，使用 sudo 启动会读到 root 身份。时区文件在容器启动时设置，不再烘焙进镜像。

`proxy.file_path` 省略时使用应用 YAML 同目录的 `mihomo.yaml`；在 YAML 中填写相对路径也基于该 YAML 所在目录。命令行和环境变量传入的相对路径基于调用目录。

旧的 `proxy.config`、`CAGED_CLAUDE_PROXY_CONFIG`、`--config` 和 `--proxy-config` 仍作为兼容别名。相同来源同时提供新旧名称时，YAML 和环境变量使用新名称的值；命令行别名按出现顺序取最后一个值。CLI > ENV > YAML > 默认值的优先级不变。

两个 DNS 选项都是 1–4 个带路径的 HTTPS 公网 IPv4 URL。YAML 写列表或单个字符串；环境变量和命令行写逗号分隔的列表或 JSON 数组。

- `proxy.bootstrap_dns`（默认 `https://1.1.1.1/dns-query`、`https://223.5.5.5/dns-query`，一家国际、一家国内）：启动器经宿主网络查询所选节点域名的 A 记录。所有服务同时查询并校验证书，按列表顺序取第一个含公网 IPv4 的应答；fake-IP、内网地址不算。全部失败时停止启动，报错逐个列出每个服务的原因（只显示主机名，不显示路径）。节点服务器已经是公网 IPv4 时跳过查询。
- 节点文件里的 `dns.proxy-server-nameserver` 中的远程 HTTPS DoH（最多 4 个，可以是域名）排在 `bootstrap_dns` 之前，与 mihomo 自身的做法一致；域名形式的服务先经 `bootstrap_dns` 解析。非 HTTPS、本机或内网地址、带用户名密码或 `#` 参数（如 `#skip-cert-verify=true`）的条目会被忽略，不阻断启动。
- 应答中的全部公网 IPv4（最多 4 个）都会固定：防火墙逐个放行，mihomo 用 fallback 组在这些地址之间切换。节点域名改指其他 IP 后，`./cage status` 会提示，`./cage down && ./cage up` 重新解析。
- `proxy.dns_upstream`（默认 `https://1.1.1.1/dns-query`）：运行时网关经所选节点访问这些端点。mihomo 同时查询全部端点、采用第一个可用应答，单个端点返回 SERVFAIL 或不可达不会导致解析失败；代价是每个端点都会看到查询。DNS 检查失败时，报错附带每个端点经节点的直接测试结果。

`proxy.host_tunnel` 处理宿主自身的 TUN/VPN。`./cage` 在进入启动器容器前查看宿主到几个公网地址的路由（`CAGED_NATIVE=1` 时还会查看节点地址本身），宿主拨号器也会自行比较。出口不是物理网卡（例如 macOS 上的 `utun*`、Linux 上的 `Meta`、`tun*`、`wg*`）时，说明 Clash/mihomo TUN 或 VPN 正在接管公网流量。

| 取值 | 行为 |
| --- | --- |
| `auto`（默认） | 发现宿主 TUN 时经宿主拨号器连接节点（绑定物理网卡）；没有 TUN 时直连 |
| `dialer` | 总是经拨号器；拨号器不可用或物理网卡连不上节点时停止启动 |
| `direct` | 从不使用拨号器；发现 TUN 只警告 |
| `block` | 发现 TUN 即拒绝启动 |

拨号器由 `./cage` 用 Docker 中的 Go 工具链编译（首次约一分钟，需要拉取 `golang:1.23-alpine`），保存在 `state/.dialer/`，日志为 `state/.dialer/dialer.log`。设置 `CAGED_DIALER=0` 可以不编译、不启动它。每个实例在宿主上使用一个端口 43000–43999（按实例名推算，只监听 127.0.0.1）；Linux 上改用挂载进网关的 Unix socket。WSL 上，TUN 装在 Windows 一侧，WSL 的流量也要经过 Windows 路由，所以拨号器作为 Windows 程序运行（`./cage` 经 WSL 互操作启动它，并按 Windows 的路由自行判断 TUN）。网关如何连到它取决于 Docker：

| WSL 上的 Docker | 网关到 Windows 拨号器 | 要求 |
| --- | --- | --- |
| WSL 里的 Docker Engine | 挂载的 Unix socket → WSL 里以 relay 模式运行的同一程序 → Windows `127.0.0.1:<端口>` | WSL 网络模式为 mirrored（`%UserProfile%\.wslconfig` 中 `networkingMode=mirrored`，然后 `wsl --shutdown`）；relay 日志在 `state/.dialer/relay.log` |
| Docker Desktop | `host.docker.internal:<端口>` | 无 |

WSL 为 NAT 模式且使用 Docker Engine 时，WSL 连不到 Windows 的回环地址：`auto` 会警告并直连，`dialer` 会停止启动并说明原因。Docker Desktop 这一路径目前未经实测。

`ports.host_proxy`（默认 `[7890, 7897, 1080]`）列出审计时笼子必须连不到的宿主代理端口；`./cage` 还会自动加上宿主当前的系统代理端口（macOS 读 `scutil --proxy`，Linux 读指向本机的 `http_proxy` 等变量）。

UI 只绑定宿主回环。DNS 的 53 端口，以及上游桌面镜像的 3000（HTTP）和 3001（HTTPS），都是内部协议端口，不作为用户偏好覆盖；其他内部服务端口可配置，需互不冲突，也不能占用这三个端口。全局代理、IPv6 阻断和默认拒绝防火墙保持固定的隔离约束。

`browser.ui_scheme` 选择网页桌面的协议，默认 `http`，访问地址是 `http://localhost:<ports.ui>`。网页桌面的剪贴板同步、视频解码（WebCodecs）、音频等功能需要浏览器的“安全上下文”，而浏览器把 `http://localhost` 和 `http://127.0.0.1` 也视为安全上下文。UI 又只绑定在回环地址上，所以 HTTP 不缺少这些功能，也不需要确认自签名证书。设为 `https` 时，改用镜像的 3001 端口和自签名证书，访问地址变成 `https://localhost:<ports.ui>`。两种方式下，网关都只发布、只放行所选的那一个端口。

| 端口设置 | 默认值 | 实际用途 |
| --- | --- | --- |
| `ports.ui` | `3002` | 宿主上访问网页桌面的入口（默认 HTTP）；唯一发布到宿主的端口 |
| `ports.controller` | `9090` | 容器内 mihomo 控制 API，启动器和审计用它核对模式与选定节点 |
| `ports.mixed` | `17890` | 受随机密码保护的 HTTP/SOCKS 入口；密码只在网关私有 tmpfs，应用不能借它绕过暂停 |
| `ports.debug` | `9222` | 容器内 Chromium DevTools，浏览器检查用它读取策略、时区、语言和出口 |

内部端口不发布到 Mac，不占用 Mac 上对应端口；日常使用都可以不填写。

`./cage up` 先启动 network 并核对出口，再启动 browser 和 claude。业务网络默认关闭，两种应用的环境与 DNS/出口证据通过后才放行。三个容器使用 `-d --rm`；任一步初始化或检查失败，本次启动创建的容器会回滚清理。启动成功后交给 Docker 运行，network 内继续监控，不保留 Mac 前台监督进程。关闭启动终端不会停止容器。

browser 和 claude 复用同一份时区、locale、语言、UID/GID、代理环境变量清理和 DNS 配置，并共享网关的网络命名空间。会检查两个容器内的实际设置及出口。网关意外退出后，其他容器可以仍显示运行，但独立防火墙阻止公网回退直连；`./cage status` 会标记网关不可用，`./cage claude` 拒绝进入这种异常实例。使用 `./cage down` 后重新 `./cage up` 恢复。

并行实例使用另一组名称和 UI 端口，例如 `./cage up --instance caged-test --port 3003`。查看和停止对应实例使用 `./cage status --instance caged-test`、`./cage down --instance caged-test`；进入使用 `./cage claude --instance caged-test`。`./cage claude --app-settings /path/to/app.yaml` 可切换应用配置，其他参数原样传给 Claude，例如 `./cage claude --model sonnet` 或 `./cage claude -p '问题'`。Claude 自身的 `--settings` 参数仍传给 Claude，不会当成项目应用配置。

## Workspace 与 Claude 授权持久化

在实际 `config/app.yaml` 中按需设置；不填写时不挂载 workspace：

```yaml
workspace:
  - ~/Projects/my-project
  - ~/Documents/notes
```

路径展开为宿主的绝对路径，并以相同路径读写挂载到 browser 和 claude，首个 workspace 是两个应用的工作目录。这样 Git 路径、文件引用和 Claude 项目标识不会因挂到 `/workspace` 而改变。相对路径在 YAML 中基于该 YAML 目录；CLI/环境变量的相对路径基于调用目录。目录必须已经存在。

`CAGED_CLAUDE_WORKSPACE` 可以是单个路径或 JSON 路径数组，设为 `[]` 可清除配置中的列表；CLI 可重复 `--workspace /path`，覆盖整个列表。不会自动共享整个 Mac 主目录或 Docker socket，也不会修改 workspace 的文件属主。挂载后，其中的文件按普通权限可被两个应用读取和修改，包括目录中的隐藏文件。

Claude 的登录凭证单独保存，不和环境状态放在一起：

```text
宿主：<state.dir>/<实例>/claude  →  Claude 容器：/home/claude/.claude（即 ~/.claude）
CLAUDE_CONFIG_DIR=/home/claude/.claude
```

`.credentials.json`、`settings.json`、会话、记忆以及 `.claude.json` 都保存在这个目录中，目录权限为 `700`。新实例通过 `./cage claude` 登录。容器无法打开本机浏览器时，按 CLI 提示在宿主打开登录 URL 并粘贴返回代码。

`claude.config_dir` 可指定实例的 `claude/` 目录或其子目录。已有的显式 `config/` 路径继续有效，由该配置指定原有资料的位置。`config/` 和 `state/` 都被 Git 忽略，并排除在镜像构建上下文之外。

升级后的首次 `./cage up` 会把旧版默认目录 `config/claude/<实例>/` 迁到新位置。如果没有该目录，通过应用配置、环境变量或默认值选中的实例可以接收更早的 `config/.claude`。目标已存在时保留两边的目录。跨文件系统时，完整复制成功后才启用新目录，旧目录保留。启用的宿主桥也会迁移其主机身份和日志，实例登记则迁到项目下固定的 `state/.instances/`。

如果 `state.dir` 位于项目之外，使用 `CAGED_NATIVE=1` 执行 `./cage up`。默认容器启动器以只读方式共享项目之外的路径。

## launcher 运行方式

`./cage help` 和 `./cage init` 直接由 shell 完成。其他命令会在一个 launcher 容器里运行 Python 启动器（`caged/host`）：

- 挂载 Docker socket，由它驱动宿主的 Docker；
- 项目目录按原路径读写挂载，`$HOME` 和当前目录只读挂载，用于读取配置和校验 workspace；
- launcher 镜像只含依赖，代码直接挂载，改代码不需要重建镜像。

| 环境变量 | 作用 |
| --- | --- |
| `CAGED_MOUNTS` | 冒号分隔的额外只读目录，用于 `$HOME` 之外的 workspace 或配置文件 |
| `CAGED_DOCKER_SOCK` | 指定 Docker socket；默认 `/var/run/docker.sock`，Linux 上会使用 `unix://` 形式的 `DOCKER_HOST` |
| `CAGED_NATIVE=1` | 不用容器，直接用宿主机的 Python 3.9+ 运行（需要 PyYAML，节点使用域名时还需要 dnspython） |
| `CAGED_DIALER=0` | 不编译、不启动宿主拨号器（见 `proxy.host_tunnel`） |

## 从 caged-browser 升级

项目早期叫 caged-browser，现在所有名称都统一为 caged-claude：

- **入口**：原来的 `./start`、`./stop`、`./status`、`./claude`、`./shell` 合并为 `./cage`；
- **环境变量前缀**：`CAGED_BROWSER_*` 改为 `CAGED_CLAUDE_*`；
- **默认实例名**：改为 `caged-claude`，容器名为 `caged-claude-network`、`-browser`、`-claude`；
- **容器标签**：改为 `io.caged-claude.*`，镜像也会以新名字重建。

旧版启动的容器不再受管理，可能占用 UI 端口。`./cage up` 和 `./cage status` 发现旧容器时会提示删除命令。`config/` 和 Claude 登录状态不受影响。
