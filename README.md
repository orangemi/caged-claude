# Caged Claude

把 Claude Code 关进 Docker 运行，并把它的网络锁死：所有出站流量只能经过你指定的一个代理节点，节点不可用或检查不通过时直接断网，不会回退直连。

- **隔离运行**：Claude Code 跑在独立容器里，只能看到显式挂载的 workspace 和自己的授权目录（`config/.claude`），看不到宿主的其他文件、Keychain 或 Docker socket。
- **网络受限**：Claude 不拥有自己的网络，只共享 mihomo 网关容器的网络栈。网关默认拒绝一切流量，只放行到选定节点的连接；DNS 也经节点解析，IPv6、私网、元数据地址和公共 DNS 一律阻断。
- **持续检查，失败即断网**：网关持续核对代理、防火墙、DNS 和出口国家，每次通过只发放一段有限期的内核放行许可（默认 90 秒），检查失败或超时，许可到期自动断网。
- **配套浏览器**：同一网络里还有一个 Chromium，通过 `https://localhost:3002` 访问，用于登录授权或手动查看网页，出口和 Claude 完全一致。

网络架构、DNS 路径、防火墙和生命周期的详细说明见 [实现原理](docs/implementation.md)。

```
Claude Code / Chromium（各自独立容器，共享网关的网络命名空间）
  → 网关容器：mihomo TUN + DNS + 默认拒绝防火墙 + 持续检查
  → 配置选择的 AnyTLS 节点
  → Internet
```

> 项目最初叫 caged-browser，为了兼容已有实例和配置，代码内部的名称暂未改：默认实例名仍是 `caged-browser`，环境变量前缀仍是 `CAGED_BROWSER_`，容器标签仍是 `io.caged-browser.*`。

## 启动

依赖：Docker（OrbStack、Docker Desktop 或 Linux Docker Engine）和 POSIX `sh`。宿主机不需要 Python 或 Ruby。

```bash
cd ~/Projects/caged-claude
./start
./status
./claude
# 进入 Claude 开发容器的 Bash：
./shell
# 需要停止整个实例时：
./stop
```

所有入口脚本经 [containers/launcher/run.sh](containers/launcher/run.sh) 在一个 launcher 容器中运行 Python 启动器：挂载 Docker socket，项目目录按原路径读写挂载，`$HOME` 与当前目录只读挂载，用于读取配置和校验 workspace。首次运行会自动构建 launcher 镜像（只含依赖，代码直接挂载，改代码无须重建）。

| 环境变量 | 作用 |
| --- | --- |
| `CAGED_MOUNTS` | 冒号分隔的额外只读目录，用于 `$HOME` 之外的 workspace 或配置文件 |
| `CAGED_DOCKER_SOCK` | 指定 Docker socket；默认 `/var/run/docker.sock`，Linux 上会使用 `unix://` 形式的 `DOCKER_HOST` |
| `CAGED_NATIVE=1` | 不用容器，直接用宿主机的 Python 3.9+ 运行（需要 PyYAML） |

访问地址使用配置中的 UI 端口；示例为 https://localhost:3002。首次自签名证书提示由使用者在浏览器中确认。

`./start` 后台启动 network、browser、claude 三个容器，检查通过后返回终端。`./claude` 进入已运行的 Claude 容器执行 CLI；退出 Claude 对话不会停止容器。`./stop` 停止并移除这组容器，`./status` 查看状态。首次在 Claude 中登录后，配置和授权保存在项目 `config/.claude`，下次启动继续使用。

`./shell` 是 Bash 脚本入口，默认以容器实际的 `claude-user` UID/GID 进入 Claude 容器。也可以选择应用或执行单条命令：

```bash
./shell browser                 # 进入浏览器容器的 Bash
./shell network                 # 网络镜像为 Alpine，进入 root sh
./shell --root                  # 在 Claude 容器内使用 root；不调用 Mac sudo
./shell -- id                   # 非交互执行命令，保留原始参数
./shell --instance my-instance   # 选择其他实例
```

退出 Shell 不会停止容器。它复用现有网络与挂载，并校验容器所属项目和网关 ID。**当前 `./stop` 仍会删除容器可写层；安装软件和完整开发环境的持久化方案见 [有状态服务设计](docs/stateful-service.md)。**

`config/app.yaml` 的 `proxy.node` 指定要从 `config/mihomo.yaml` 选择的节点。`nodes` 列出所有地区的节点；命令行可以临时切换：

```bash
./start nodes
./start --node '🇸🇬 Singapore 02'
```

配置分为两个文件：

```
config/app.yaml      # 启动配置：节点选择、时区、语言、端口和出口策略
config/mihomo.yaml   # mihomo 节点和凭证
```

启动器按项目位置寻找 `config/app.yaml`，从其他目录执行 `start` 也读取同一份应用配置。`--settings` 指定其他应用 YAML；`proxy.file_path` 或 `--proxy-file-path` 指定节点 YAML，省略时读取应用 YAML 同目录的 `mihomo.yaml`。节点文件可以从 Mac Clash Verge 复制，也可以自己填写，不会自动读取或同步 Mac 配置。

整个 `config/` 目录均已加入 Git 忽略。仓库保留 [config.example/app.yaml](config.example/app.yaml) 和不含真实节点信息的 [config.example/mihomo.yaml](config.example/mihomo.yaml)；新环境先复制两份模板，再填写节点和启动选项：

```bash
mkdir -p config
chmod 700 config
cp -n config.example/mihomo.yaml config/mihomo.yaml
cp -n config.example/app.yaml config/app.yaml
chmod 600 config/*.yaml
```

`cp -n` 保留已有配置；模板中的占位值不能直接用于连接。

也可以从 Mac Clash Verge 导入或更新完整快照，在项目目录执行：

```bash
mkdir -p config
chmod 700 config
cp "$HOME/Library/Application Support/io.github.clash-verge-rev.clash-verge-rev/clash-verge.yaml" config/mihomo.yaml
chmod 600 config/mihomo.yaml
```

实际 `config/mihomo.yaml` 包含节点凭证，权限设为 `600`，随整个 `config/` 目录一起被 Git 忽略。它在会话结束后保留；浏览器数据仍是临时的。配置更新后重启会话读取，不会自动同步 Mac 的后续修改。

第一版支持独立 AnyTLS 节点；有 `dialer-proxy` 的链式节点会拒绝启动，而不是遗漏链路参数。复制的文件保留原始配置内容；容器运行时仍只提取所选节点，重新生成全局代理配置。

## 配置优先级

所有启动选项按 **命令行参数 > 环境变量 > `app.yaml` > 内置默认值** 解析，并且只校验最终生效的值。`--help` 列出完整选项。`config.example/app.yaml` 仅启用必选配置，可选项统一注释并注明默认值；需要覆盖默认值时取消对应注释。无默认值的选项缺失、键名拼错或生效的时区/端口非法时，会在启动容器前报错。

可省略的默认项包括节点文件 `mihomo.yaml`、实例名 `caged-browser`、DoH 上游 `https://1.1.1.1/dns-query`、locale `en_US.UTF-8`、语言 `en-US`、跟随启动者的 UID/主 GID、共享内存 `1g`，以及整个 `ports` 块。节点、期望国家和时区仍由配置或覆盖项明确提供。

| `app.yaml` 键 | 环境变量 | 命令行参数 |
| --- | --- | --- |
| 应用文件路径 | `CAGED_BROWSER_SETTINGS` | `--settings` |
| `proxy.file_path` | `CAGED_BROWSER_PROXY_FILE_PATH` | `--proxy-file-path` |
| `proxy.node` | `CAGED_BROWSER_NODE` | `--node` |
| `proxy.expected_country` | `CAGED_BROWSER_EXPECTED_COUNTRY` | `--expected-country` |
| `proxy.dns_upstream` | `CAGED_BROWSER_DNS_UPSTREAM` | `--dns-upstream` |
| `browser.timezone` | `CAGED_BROWSER_TIMEZONE` | `--timezone` |
| `browser.locale` | `CAGED_BROWSER_LOCALE` | `--locale` |
| `browser.language` | `CAGED_BROWSER_LANGUAGE` | `--language` |
| `browser.puid` / `browser.pgid` | `CAGED_BROWSER_PUID` / `CAGED_BROWSER_PGID` | `--puid` / `--pgid` |
| `browser.shm_size` | `CAGED_BROWSER_SHM_SIZE` | `--shm-size` |
| `ports.ui` | `CAGED_BROWSER_PORT` | `--port` |
| `ports.controller` | `CAGED_BROWSER_CONTROLLER_PORT` | `--controller-port` |
| `ports.mixed` | `CAGED_BROWSER_MIXED_PORT` | `--mixed-port` |
| `ports.debug` | `CAGED_BROWSER_DEBUG_PORT` | `--debug-port` |
| `instance` | `CAGED_BROWSER_INSTANCE` | `--instance` |
| `workspace` | `CAGED_BROWSER_WORKSPACE` | `--workspace`，可重复 |
| `claude.config_dir` | `CAGED_BROWSER_CLAUDE_CONFIG_DIR` | `--claude-config-dir` |
| `ports.browser_probe` / `ports.claude_probe` | `CAGED_BROWSER_BROWSER_PROBE_PORT` / `CAGED_BROWSER_CLAUDE_PROBE_PORT` | `--browser-probe-port` / `--claude-probe-port` |
| `monitor.tick` / `monitor.interval` | `CAGED_BROWSER_MONITOR_TICK` / `CAGED_BROWSER_MONITOR_INTERVAL` | `--monitor-tick` / `--monitor-interval` |
| `monitor.lease` / `monitor.probe_timeout` | `CAGED_BROWSER_MONITOR_LEASE` / `CAGED_BROWSER_MONITOR_PROBE_TIMEOUT` | `--monitor-lease` / `--monitor-probe-timeout` |
| `monitor.retry_initial` / `monitor.retry_max` | `CAGED_BROWSER_MONITOR_RETRY_INITIAL` / `CAGED_BROWSER_MONITOR_RETRY_MAX` | `--monitor-retry-initial` / `--monitor-retry-max` |

例如，用环境变量改变时区，并用命令行覆盖环境变量中的端口：

```bash
CAGED_BROWSER_TIMEZONE=Asia/Tokyo CAGED_BROWSER_PORT=3102 ./start --port 3103
```

切换地区时同时指定节点和期望国家，例如 `--node '🇯🇵 Japan 01' --expected-country JP`。将 `proxy.expected_country` 设为 YAML `null` 或 `any`（命令行/环境变量使用 `any`）可以取消国家限制，仍检查连通性及审计中的出口一致性。

`browser.language` 使用 `ja-JP` 等语言标签，自动生成 `ja-JP,ja` 偏好；`browser.locale` 控制系统 locale。两个应用的用户名固定为 `claude-user`，HOME 为 `/config`。`puid`、`pgid` 默认都是 `host`，每次启动读取当前进程的 UID/主 GID；通常等同于运行终端的 `id -u` 和 `id -g`。可显式填写整数；环境变量和 CLI 仍按优先级分别覆盖。使用普通用户执行 `./start` 即可，使用 sudo 启动会读到 root 身份。时区文件在容器启动时设置，不再烘焙进镜像。

`proxy.file_path` 省略时使用应用 YAML 同目录的 `mihomo.yaml`；在 YAML 中填写相对路径也基于该 YAML 所在目录。命令行和环境变量传入的相对路径基于调用目录。

旧的 `proxy.config`、`CAGED_BROWSER_PROXY_CONFIG`、`--config` 和 `--proxy-config` 仍作为兼容别名。相同来源同时提供新旧名称时，YAML 和环境变量使用新名称的值；命令行别名按出现顺序取最后一个值。CLI > ENV > YAML > 默认值的优先级不变。

DoH 上游目前要求带路径的 HTTPS 公网 IPv4 URL，例如 `https://1.1.1.1/dns-query`，由程序追加固定节点标识，避免额外的域名引导解析。

UI 只绑定宿主回环。DNS 的 53 端口和上游桌面镜像的 HTTPS 3001 是内部协议端口，不作为用户偏好覆盖；其他三个内部服务端口可配置，需互不冲突。全局代理、IPv6 阻断和默认拒绝防火墙保持固定的隔离约束。

| 端口设置 | 默认值 | 实际用途 |
| --- | --- | --- |
| `ports.ui` | `3002` | Mac 上访问网页桌面的 HTTPS 入口；唯一发布到宿主的端口 |
| `ports.controller` | `9090` | 容器内 mihomo 控制 API，启动器和审计用它核对模式与选定节点 |
| `ports.mixed` | `17890` | 受随机密码保护的 HTTP/SOCKS 入口；密码只在网关私有 tmpfs，应用不能借它绕过暂停 |
| `ports.debug` | `9222` | 容器内 Chromium DevTools，浏览器检查用它读取策略、时区、语言和出口 |

内部端口不发布到 Mac，不占用 Mac 上对应端口；日常使用都可以不填写。

`./start` 先启动 network 并核对出口，再启动 browser 和 claude。业务网络默认关闭，两种应用的环境与 DNS/出口证据通过后才放行。三个容器使用 `-d --rm`；任一步初始化或检查失败，本次启动创建的容器会回滚清理。启动成功后交给 Docker 运行，network 内继续监控，不保留 Mac 前台监督进程。关闭启动终端不会停止容器。

browser 和 claude 复用同一份时区、locale、语言、UID/GID、代理环境变量清理和 DNS 配置，并共享网关的网络命名空间。会检查两个容器内的实际设置及出口。网关意外退出后，其他容器可以仍显示运行，但独立防火墙阻止公网回退直连；`./status` 会标记网关不可用，`./claude` 拒绝进入这种异常实例。使用 `./stop` 后重新 `./start` 恢复。

并行实例使用另一组名称和 UI 端口，例如 `./start --instance caged-test --port 3003`。查看和停止对应实例使用 `./status --instance caged-test`、`./stop --instance caged-test`；进入使用 `./claude --instance caged-test`。`./claude --app-settings /path/to/app.yaml` 可切换应用配置，其他参数原样传给 Claude，例如 `./claude --model sonnet` 或 `./claude -p '问题'`。Claude 自身的 `--settings` 参数仍传给 Claude，不会当成项目应用配置。

## Workspace 与 Claude 授权持久化

在实际 `config/app.yaml` 中按需设置；不填写时不挂载 workspace：

```yaml
workspace:
  - ~/Projects/my-project
  - ~/Documents/notes
```

路径展开为宿主的绝对路径，并以相同路径读写挂载到 browser 和 claude，首个 workspace 是两个应用的工作目录。这样 Git 路径、文件引用和 Claude 项目标识不会因挂到 `/workspace` 而改变。相对路径在 YAML 中基于该 YAML 目录；CLI/环境变量的相对路径基于调用目录。目录必须已经存在。

`CAGED_BROWSER_WORKSPACE` 可以是单个路径或 JSON 路径数组，设为 `[]` 可清除配置中的列表；CLI 可重复 `--workspace /path`，覆盖整个列表。不会自动共享整个 Mac 主目录或 Docker socket，也不会修改 workspace 的文件属主。挂载后，其中的文件按普通权限可被两个应用读取和修改，包括目录中的隐藏文件。

Claude 的持久化映射独立于 workspace：

```text
宿主：<项目>/config/.claude  →  Claude 容器：/config/.claude（即 ~/.claude）
CLAUDE_CONFIG_DIR=/config/.claude
```

容器中的 HOME 与浏览器一样为 `/config`。显式设置 `CLAUDE_CONFIG_DIR` 使 `.credentials.json`、`settings.json`、会话/记忆以及原本在 HOME 外层的 `.claude.json` 一并保存在绑定目录中。默认不会复制 Mac 的 `.claude` 或 Keychain；首次通过 `./claude` 登录。容器中无法自动打开本机浏览器时，按 CLI 提示在 Mac 打开登录 URL 并粘贴返回代码。

`claude.config_dir` 是可选项，默认上述项目目录；如需隔离测试，可指定本项目 `config/` 下的其他子目录。状态目录权限为 `700`，授权文件由 Claude 管理。`./stop`、镜像重建和容器重建不会删除宿主目录；有效登录令牌可继续使用，令牌本身仍有有效期。`config/` 整体已被 Git 忽略，也被排除在 Claude 镜像的构建上下文之外。

## 出口验证

启动器只管理容器内网络，不检查或更改宿主的 TUN、系统代理及路由，不调用 sudo。容器内 mihomo 使用所选节点的配置自行拨号，不把 Mac Clash 的监听端口作为上游。

启动浏览器前，会实际请求出口检测服务并按 `proxy.expected_country` 核对国家。节点不可用或出口不符合生效策略时，停止本次启动并清理容器。

宿主网络可能影响连接节点的路径、延迟和可用性；启动器根据容器内的实际出口结果判断，不根据宿主接口名称推断。节点地址在启动时通过宿主解析一次；该引导解析不包含浏览器访问的域名。

## 网络边界

- 浏览器与 Claude 共享网络容器的网络栈，没有其他 Docker 网络。
- 浏览器启动前，网络容器先安装默认拒绝的 IPv4/IPv6 防火墙，再启动 mihomo TUN、DNS 并固定 GLOBAL 到选定节点。
- 外层 IPv4 TCP 连接仅允许选定节点 IP 和端口；容器内回环、TUN 流量、已建立连接的回包及本机浏览器控制入口按需允许。
- DNS 指向网络容器的 `127.0.0.1:53`，`proxy.dns_upstream` 显式经选定代理节点访问。Chromium 受管策略设为 `DnsOverHttpsMode=off`、`BuiltInDnsClientEnabled=false`，交给系统解析器，再由 mihomo 完成 DoH。公共 UDP/TCP 53 和 853 在进入 TUN 前拒绝，不作为备用解析器。启动时同时检测本地 UDP/TCP 查询、需要真实上游响应的新鲜 TXT 查询和公共 DNS 绕过。
- 节点服务器先固定为真实 IPv4，保留原配置的 SNI，避免代理自身解析产生引导循环。此节点地址的引导解析在宿主进行；浏览器域名不经宿主 DNS。网页自己发起的 HTTPS/DoH 请求仍受全局代理约束，但不会被内容识别后强制改写成指定 DNS 服务。
- 新发起的私网、链路本地元数据和 Docker 网桥宿主管理连接在进入 TUN 前拒绝；浏览器 UI 已建立连接的回包允许返回。
- IPv6 外网流量直接阻断。AnyTLS 支持的 UDP 经代理转发；无法代理的流量不回退直连。
- mihomo 的控制接口与 Chromium DevTools 仅监听共享网络空间的回环地址，不发布给宿主或局域网。mihomo 控制接口另有每次运行随机生成的鉴权密钥。

## 持续检查与暂停恢复

日常仍是三个容器、四个入口。network 内默认每轮结束后等待 5 秒，核对代理模式/节点、IPv4/IPv6 防火墙、两个应用的实际时区、语言、系统 DNS 和浏览器 DNS 策略；上次成功完成 30 秒后，再取得新鲜 DNS 与 HTTPS 出口证据。实际间隔包含探针执行时间。每次成功给予最长 90 秒的内核放行许可；检测超时不延长旧许可，到期自动断网，即使监控进程被挂起也一样。

后台全部使用容器内命令行和只读环境探针。浏览器 DNS 策略检查读取受管配置文件，不调用浏览器 CDP，不新建、切换或关闭标签页。`Intl`、`navigator` 和浏览器实际策略是否生效，只在启动及显式 `smoke` / `audit` 时通过专用诊断页核对；后台不持续读取网页 JavaScript 环境。

```bash
./status        # 显示网络状态、剩余许可时间和失败原因
./start pause   # 立即暂停公网，保留浏览器控制页面与容器
./start resume  # 明确请求重新检查；通过后才恢复
```

临时故障关闭后，按 15、30、60、120、240、300 秒退避重检，之后最多每次等待 300 秒。已确认的配置违规会锁定关闭，需要修复后明确 `./start resume`；手动暂停不会自动恢复。暂停不停止 network，因此恢复沿用原来的网络空间，工作区和 Claude 授权目录保留。

可选 `monitor` 配置的全部默认值见 [配置示例](config.example/app.yaml)，实际 `config/app.yaml` 可以省略整个块。所有字段仍遵循 CLI > ENV > YAML > 默认值。许可必须覆盖 `interval + probe_timeout + 2*tick`，防止正常检测频率无法续期。默认内部环境探针端口为 19981、19982，可在 `ports` 中覆盖，不发布到宿主。

环境探针只返回固定的只读信息，通过每实例私有密钥、随机挑战与配置摘要验证；没有转发或任意命令入口。断网后的出口/DNS诊断仅由持有网络权限的网关运行，应用没有设置诊断标记所需的网络能力。mixed 代理另有鉴权，避免成为回环绕过入口。现有连接的放行检查排在通用 `ESTABLISHED` 放行之前。

运行策略以启动快照为准；修改宿主 `app.yaml` 后使用 `./stop`、`./start` 应用，后台不读取 Mac 代理配置或网络设置。当前出口约束沿用 `proxy.expected_country`，未额外绑定固定公网 IP。后台不重复删除网关的故障演练；完整 11 项审计仍使用独立测试实例。

## 数据生命周期

浏览器自己的配置、登录状态和缓存仍不持久化，容器删除时清理。显式挂载的 workspace 与 Claude 授权目录是保留在宿主的两类数据，`--rm` 不删除它们。

项目的 `config/mihomo.yaml` 保存配置快照及其中的节点凭证。启动器读取选定节点后，通过标准输入传给网络容器，不把凭证放进容器环境变量或镜像。生成的运行时配置、控制密钥和核心日志只保存在容器内 `/run/caged` 的 tmpfs，网关退出后销毁。项目配置快照、Docker 镜像和构建缓存保留。

## 验证

```bash
# 纯单元测试：不启动 Docker，不需要节点或私有配置；宿主直接运行需要 PyYAML。
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests/unit -t . -v

# 容器内隔离测试：本地 TCP/UDP/DNS 端口正向对照、代理失效和删除后的断网。
# 不需要真实节点凭证，不修改 Mac 网络，可以保持 Mac TUN 开启。
# 同时验证后台启动、部分失败回滚、停止与状态；应用服务使用无外网请求的替身。
# 另用真实 Claude CLI 验证安装、workspace 写入与状态目录跨重建保留，不进行账号或模型请求。
./start test

# 网络验证；会在测试结束时真正停止并移除代理容器，再验证剩下的网络命名空间无法直连。
./start verify

# 启动实际 Chromium 和 Claude 服务，检查环境、DNS、出口及网页控制入口，然后清理。
./start smoke

# 11 项完整检查，使用新的临时实例；结束后自动清理，不影响已有浏览器。
./start audit --instance caged-audit --port 3003

# 只读状态 / 停止此项目创建的容器
./status
./stop
```

`verify` 按生效配置检查 HTTPS 出口、容器内 DNS、UDP/STUN、强制绑定物理接口的直连阻断、IPv6 阻断，以及代理容器移除后的断网行为。STUN 使用 Google/Cloudflare 的公开 STUN 服务，不申请摄像头或麦克风权限。

`test` 执行 `tests/unit` 与 `tests/integration`，包含配置优先级、校验与跨组件传递测试，并使用本地替身验证网络和进程清理；不代表真实节点已经验证。测试服务和启动替身集中在 `tests/fixtures`。`verify` 和 `smoke` 使用真实节点，但同样不会检查或改变宿主 TUN。

`audit` 参考 Claude-Cage 的检查项目，但按本项目的容器结构取证。检查结果逐项输出 JSON，未完成的项保持 `unknown`；任何必要探针失败都会返回非零退出码。它只适用于新建的临时会话，会真正删除自己创建的网络容器进行故障演练，不能附加到已有浏览器。

| 检查 | 本项目的证据 |
| --- | --- |
| 环境外出站约束 | 默认拒绝的 IPv4/IPv6 防火墙未被改写，物理接口探针触发内核丢弃计数；浏览器只共享网关网络 |
| 终端检测通道 | 无代理环境变量的 HTTPS 请求返回符合配置策略的实际出口 |
| DNS 与解析约束 | 系统解析器仅为回环；本地 UDP/TCP 与新鲜递归 TXT 成功；显式节点 DoH；公共 UDP/53、TCP/53、DoT/853 被拒绝且丢弃计数增加 |
| 系统时区与语言 | 实际环境、locale、`/etc/timezone`、`/etc/localtime` 和 UTC 偏移一致 |
| 浏览器时区与语言 | 专用诊断标签读取 `Intl`、`navigator` 和 `chrome://policy` 中实际生效的 DNS 策略，随后关闭标签 |
| 共享文件与宿主私有目录 | 精确核对明示 workspace 与 Claude 状态目录的挂载白名单；无额外绑定、网关凭证或 Docker socket；匿名卷随容器清理 |
| 宿主管理接口 | 元数据/网桥管理端口不可达且私网丢弃计数增加；无网络管理/原始套接字权限；控制器匿名请求 401；仅发布 localhost UI |
| 代理故障关闭 | 删除测试网关后，检查容器、Claude 的 TCP/DNS/UDP 与浏览器新鲜字面 IP 导航均失败；浏览器绕过缓存和 Service Worker |
| UDP 出站约束 | 保留经节点转发 UDP 的设计，以实际 STUN 出口和物理接口默认拒绝策略验证；与 Claude-Cage 默认阻断 UDP 的策略不同 |
| IPv6 出站约束 | 实际 IPv6 请求失败且内核 IPv6 默认拒绝策略未变化 |
| 出口符合已保存规则 | 符合生效的期望国家策略，且当前终端、UDP、浏览器、Claude 容器四种出口 IP 一致；不是永久固定 IP 保证 |

本地隔离测试有可连通的 TCP、UDP、UDP/53、TCP/53 和 TCP/853 服务作正向对照，再验证网关运行与移除后均无法绕过。生命周期测试使用独立随机实例，可在日常浏览器运行时执行。

默认不打印代理配置。上游 TLS 设置沿用所选节点的当前参数；不会自动更改订阅或 Mac Clash 的运行模式。

## 目录结构

```text
caged-claude/
├── start / stop / status / claude  # 服务和 Claude 命令
├── shell                         # 进入容器或执行单条命令
├── caged/
│   ├── host/                     # 配置、Docker、实例编排、CLI 与审计
│   ├── runtime/
│   │   ├── network/              # mihomo、防火墙、许可与持续监控
│   │   ├── agents/               # 应用容器中的签名环境探针
│   │   └── checks/               # 命令行及显式浏览器检查
│   └── shared/                   # 共享策略与消息编码
├── containers/
│   ├── network/                  # 各镜像的 Dockerfile 与初始化文件
│   ├── browser/
│   ├── claude/
│   └── launcher/                 # 宿主入口脚本使用的 launcher 镜像与 run.sh
├── tests/
│   ├── unit/                     # 无 Docker 的单元测试
│   ├── integration/              # 隔离的 Docker 集成测试
│   └── fixtures/                 # 测试服务与替身
├── config/                       # 本机私有配置与 Claude 状态，Git 忽略
├── config.example/               # 可提交的配置模板
└── docs/implementation.md         # 实现原理与源码阅读入口
```

镜像统一从项目根目录构建，构建上下文只包含公开运行代码和镜像文件。宿主模块不进入容器；修改镜像实际复制的文件会自动触发相关镜像重建。配置键名和四个日常命令保持兼容。

## 实现参考

- [Claude-Cage 的网络检查与证据逻辑](https://github.com/Lee-xeo/Claude-Cage/blob/b6a8cf8271ad29b05f08b3e07d306e4dd8c32bbf/internal/workbench/safety.go)
- [Chromium DNS-over-HTTPS 受管策略](https://chromeenterprise.google/intl/en_ca/policies/dns-over-https-mode/)
- [Docker 共享网络命名空间](https://docs.docker.com/engine/network/#container-networks)
- [mihomo TUN](https://wiki.metacubex.one/en/config/inbound/tun/)
- [mihomo DNS 指定代理](https://wiki.metacubex.one/en/config/dns/)
- [OrbStack 网络与宿主代理](https://docs.orbstack.dev/docker/network)
