# 架构

Caged Claude 由三个容器组成，外加宿主上的一个可选组件：

```text
宿主机
├── ./cage（POSIX sh）─▶ launcher 容器（Python 启动器，驱动宿主 Docker）
├── 宿主桥监听进程（python3 + 按需 sshd -i，可选）◀───────────────┐
│                                                                   │ host.sock（Mac 上为回环 TCP）
Docker                                                              │
├── network：mihomo TUN + DNS + 默认拒绝防火墙 + 持续检查 ──▶ 选定的 AnyTLS 节点 ──▶ Internet
├── claude：Debian 13 + Claude Code，共享 network 的网络命名空间 ───┘
└── browser：Chromium，共享 network 的网络命名空间，UI 发布到 127.0.0.1:3002
```

- **网络**：claude 和 browser 没有自己的网络，只共享网关的网络栈；所有出口只经过选定节点，失败即断网。宿主桥是唯一的例外：Linux/WSL 上走文件 socket，不经过网络；Mac 上只放行宿主的一个端口。
- **状态**：Claude 的 Linux 系统、HOME 和浏览器资料实时保存在宿主 `state/<实例>/`，容器随时可以丢弃。见 [有状态环境设计](stateful-service.md)。
- **宿主桥**：笼子里 `ssh host` 回到宿主。见 [宿主桥设计](host-bridge.md)。
- **实现细节**：DNS 路径、防火墙规则、许可与监控、源码入口，见 [实现原理](implementation.md)。

## 出口验证

启动器只管理容器内网络，不检查或更改宿主的 TUN、系统代理及路由，不调用 sudo。容器内 mihomo 使用所选节点的配置自行拨号，不把 Mac Clash 的监听端口作为上游。

启动浏览器前，会实际请求出口检测服务并按 `proxy.expected_country` 核对国家。节点不可用或出口不符合生效策略时，停止本次启动并清理容器。

宿主网络可能影响连接节点的路径、延迟和可用性；出口是否合规始终以容器内的实际出口结果判断。另外，`./cage` 会查看宿主到公网的路由：经过 `utun*` 等非物理网卡时，说明宿主 TUN/VPN 会接管到节点的连接，按 `proxy.host_tunnel` 警告或拒绝启动。节点域名在启动时经 `proxy.bootstrap_dns` 等多个 DoH 服务解析，固定全部公网 IPv4。

## 网络边界

- 浏览器与 Claude 共享网络容器的网络栈，没有其他 Docker 网络。
- 浏览器启动前，网络容器先安装默认拒绝的 IPv4/IPv6 防火墙，再启动 mihomo TUN、DNS 并固定 GLOBAL 到选定节点。
- 外层 IPv4 TCP 连接仅允许选定节点 IP 和端口；容器内回环、TUN 流量、已建立连接的回包及本机浏览器控制入口按需允许。
- DNS 指向网络容器的 `127.0.0.1:53`，`proxy.dns_upstream` 显式经选定代理节点访问。Chromium 受管策略设为 `DnsOverHttpsMode=off`、`BuiltInDnsClientEnabled=false`，交给系统解析器，再由 mihomo 完成 DoH。公共 UDP/TCP 53 和 853 在进入 TUN 前拒绝，不作为备用解析器。启动时同时检测本地 UDP/TCP 查询、需要真实上游响应的新鲜 TXT 查询和公共 DNS 绕过。
- 节点服务器先固定为真实 IPv4，保留原配置的 SNI，避免代理自身解析产生引导循环。节点域名先用节点文件的 `dns.proxy-server-nameserver`，再用 `proxy.bootstrap_dns` 解析，全部校验 HTTPS 证书；最多固定 4 个地址，防火墙逐个放行，mihomo 在其间故障切换。浏览器域名经网关和所选节点查询。网页自己发起的 HTTPS/DoH 请求仍受全局代理约束，但不会被内容识别后强制改写成指定 DNS 服务。
- 新发起的私网、链路本地元数据和 Docker 网桥宿主管理连接在进入 TUN 前拒绝；浏览器 UI 已建立连接的回包允许返回。
- IPv6 外网流量直接阻断。AnyTLS 支持的 UDP 经代理转发；无法代理的流量不回退直连。
- mihomo 的控制接口与 Chromium DevTools 仅监听共享网络空间的回环地址，不发布给宿主或局域网。mihomo 控制接口另有每次运行随机生成的鉴权密钥。

## 持续检查与暂停恢复

日常仍是三个容器、一个入口。network 内默认每轮结束后等待 5 秒，核对代理模式/节点、IPv4/IPv6 防火墙、两个应用的实际时区、语言、系统 DNS 和浏览器 DNS 策略；上次成功完成 30 秒后，再取得新鲜 DNS 与 HTTPS 出口证据。实际间隔包含探针执行时间。每次成功给予最长 90 秒的内核放行许可；检测超时不延长旧许可，到期自动断网，即使监控进程被挂起也一样。

后台全部使用容器内命令行和只读环境探针。浏览器 DNS 策略检查读取受管配置文件，不调用浏览器 CDP，不新建、切换或关闭标签页。`Intl`、`navigator` 和浏览器实际策略是否生效，只在启动及显式 `smoke` / `audit` 时通过专用诊断页核对；后台不持续读取网页 JavaScript 环境。

```bash
./cage status   # 显示网络状态、剩余许可时间和失败原因
./cage pause   # 立即暂停公网，保留浏览器控制页面与容器
./cage resume  # 明确请求重新检查；通过后才恢复
```

临时故障关闭后，按 15、30、60、120、240、300 秒退避重检，之后最多每次等待 300 秒。已确认的配置违规会锁定关闭，需要修复后明确 `./cage resume`；手动暂停不会自动恢复。暂停不停止 network，因此恢复沿用原来的网络空间，工作区和 Claude 授权目录保留。

可选 `monitor` 配置的全部默认值见 [配置示例](../config.example/app.yaml)，实际 `config/app.yaml` 可以省略整个块。所有字段仍遵循 CLI > ENV > YAML > 默认值。许可必须覆盖 `interval + probe_timeout + 2*tick`，防止正常检测频率无法续期。默认内部环境探针端口为 19981、19982，可在 `ports` 中覆盖，不发布到宿主。

环境探针只返回固定的只读信息，通过每实例私有密钥、随机挑战与配置摘要验证；没有转发或任意命令入口。断网后的出口/DNS诊断仅由持有网络权限的网关运行，应用没有设置诊断标记所需的网络能力。mixed 代理另有鉴权，避免成为回环绕过入口。现有连接的放行检查排在通用 `ESTABLISHED` 放行之前。

运行策略以启动快照为准；修改宿主 `app.yaml` 后使用 `./cage down`、`./cage up` 应用，后台不读取 Mac 代理配置或网络设置。当前出口约束沿用 `proxy.expected_country`，未额外绑定固定公网 IP。后台不重复故障演练；完整 11 项审计可以在运行中的实例上执行，见下文。

## 数据生命周期

容器都以 `--rm` 运行。Claude 的系统与 HOME、浏览器资料、Claude 授权和宿主桥状态默认保存在 `state/<实例>/`。workspace 是宿主原目录。项目下的 `state/.instances/` 保存登记，在配置的状态磁盘缺失时仍可校验实例身份。

项目的 `config/mihomo.yaml` 保存配置快照及其中的节点凭证。启动器读取选定节点后，通过标准输入传给网络容器，不把凭证放进容器环境变量或镜像。生成的运行时配置、控制密钥和核心日志只保存在容器内 `/run/caged` 的 tmpfs，网关退出后销毁。项目配置快照、Docker 镜像和构建缓存保留。

## 验证

```bash
# 纯单元测试：不启动 Docker，不需要节点或私有配置；宿主直接运行需要 PyYAML 和 dnspython。
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests/unit -t . -v

# 容器内隔离测试：本地 TCP/UDP/DNS 端口正向对照、代理失效和删除后的断网。
# 不需要真实节点凭证，不修改 Mac 网络，可以保持 Mac TUN 开启。
# 同时验证后台启动、部分失败回滚、停止与状态；应用服务使用无外网请求的替身。
# 另用真实 Claude CLI 验证安装、workspace 写入与状态目录跨重建保留，不进行账号或模型请求。
./cage check test

# 网络验证；会在测试结束时真正停止并移除代理容器，再验证剩下的网络命名空间无法直连。
./cage check verify

# 启动实际 Chromium 和 Claude 服务，检查环境、DNS、出口及网页控制入口，然后清理。
./cage check smoke

# 11 项完整检查。实例运行中时检查当前实例，不中断使用；
./cage check audit
# 实例未运行时新建临时实例，真实删除其网关做故障演练，结束后自动清理。
./cage check audit --instance caged-audit --port 3003

# 只读状态 / 停止此项目创建的容器
./cage status
./cage down
```

`verify` 按生效配置检查 HTTPS 出口、容器内 DNS、UDP/STUN、强制绑定物理接口的直连阻断、IPv6 阻断，以及代理容器移除后的断网行为。STUN 使用 Google/Cloudflare 的公开 STUN 服务，不申请摄像头或麦克风权限。

`test` 执行 `tests/unit` 与 `tests/integration`，包含配置优先级、校验与跨组件传递测试，并使用本地替身验证网络和进程清理；不代表真实节点已经验证。测试服务和启动替身集中在 `tests/fixtures`。`verify` 和 `smoke` 使用真实节点，但同样不会检查或改变宿主 TUN。

应用探针生命周期回归可以单独执行。将 `CAGED_BROWSER_TEST_IMAGE` 和 `CAGED_CLAUDE_TEST_IMAGE` 分别设为已构建的新镜像，再运行 `python3 -m unittest tests.integration.test_agent_lifecycle -v`。测试使用无外网、无用户状态和无发布端口的临时容器，验证等待私有注册、签名环境报告、探针被杀后的自动拉起，以及 `docker restart` 后复用原身份自动恢复；未指定对应镜像的测试会跳过。

`audit` 参考 Claude-Cage 的检查项目，但按本项目的容器结构取证。每一项独立判定：取得全部证据为“通过”，确认违规为“失败”，缺少证据为“未知”；一项缺证据不会掩盖其他项的结果。终端先打印逐项结论与处理建议，再输出完整 JSON，并写入 `state/<实例>/last-audit.json`；不是 11/11 时返回非零退出码。

两种模式只在“代理故障关闭”一项上不同：

- **当前运行实例**（实例运行中时的默认）：外网许可必须已开放。演练只给一个专用 UID（65533）加一条策略路由，使它的流量失去代理路由，与代理停止时相同；先做正向对照（同一 UID 平时能连通），再确认演练时 HTTPS、UDP、公共 DNS 均失败且内核拒绝计数增加。用户的连接和代理进程保持运行。浏览器与终端共享同一网络命名空间和内核规则，这一模式不单独让真实浏览器断网。
- **一次性实例**：真实删除这次审计自己的网关容器，再检查终端、Claude 与真实浏览器的断网行为。

| 检查 | 本项目的证据 |
| --- | --- |
| 环境外出站约束 | 默认拒绝的 IPv4/IPv6 防火墙未被改写，物理接口探针触发内核丢弃计数；浏览器只共享网关网络 |
| 终端检测通道 | 无代理环境变量的 HTTPS 请求从 6 个出口 IP 服务中取得公网 IPv4（上一个失败或 1 秒无结果就并发下一个），再由 5 个定位服务按顺序给出国家；全部失败时请求 generate_204，区分“节点不通”和“节点屏蔽了检测服务” |
| DNS 与解析约束 | 系统解析器仅为回环；本地 UDP/TCP 与新鲜递归 TXT 成功；显式节点 DoH；公共 UDP/53、TCP/53、DoT/853 被拒绝且丢弃计数增加 |
| 系统时区与语言 | 实际环境、locale、`/etc/timezone`、`/etc/localtime` 和 UTC 偏移一致 |
| 浏览器时区与语言 | 专用诊断标签读取 `Intl`、`navigator` 和 `chrome://policy` 中实际生效的 DNS 策略，随后关闭标签 |
| 共享文件与宿主私有目录 | 精确核对明示 workspace 与 Claude 状态目录的挂载白名单；无额外绑定、网关凭证或 Docker socket；匿名卷随容器清理；随机令牌经 Claude 状态目录往返读写，宿主 `config/` 中的探针文件在环境内按宿主路径打不开 |
| 宿主管理接口 | 元数据、网桥与宿主（macOS 上 host.docker.internal）的 22/2375/2376 及 `ports.host_proxy` 与宿主系统代理端口均不可达，私网丢弃计数增加；无网络管理/原始套接字权限；控制器匿名请求 401；仅发布 localhost UI；报告注明宿主桥是有意保留的通道 |
| 代理故障关闭 | 当前实例：专用 UID 失去代理路由后 HTTPS/UDP/公共 DNS 均失败且直连拒绝计数增加，正向对照先通过。一次性实例：删除网关后，检查容器、Claude 的 TCP/DNS/UDP 与浏览器新鲜字面 IP 导航均失败 |
| UDP 出站约束 | 保留经节点转发 UDP 的设计，以实际 STUN 出口和物理接口默认拒绝策略验证；与 Claude-Cage 默认阻断 UDP 的策略不同 |
| IPv6 出站约束 | 全局 IPv6 地址为 0；实际 IPv6 请求失败；存在 IPv6 默认路由时，IPv6 默认拒绝计数必须增加 |
| 出口符合已保存规则 | 出口 IP 不在内置的中国大陆网段（APNIC 分配给 CN 的地址），定位国家不是 CN，并符合期望国家；终端、UDP、浏览器、Claude 容器四种出口 IP 一致；不是永久固定 IP 保证 |

本地隔离测试有可连通的 TCP、UDP、UDP/53、TCP/53 和 TCP/853 服务作正向对照，再验证网关运行与移除后均无法绕过。生命周期测试使用独立随机实例，可在日常浏览器运行时执行。

默认不打印代理配置。上游 TLS 设置沿用所选节点的当前参数；不会自动更改订阅或 Mac Clash 的运行模式。

## 目录结构

```text
caged-claude/
├── cage                          # 唯一入口：POSIX sh，子命令见 ./cage help
├── caged/
│   ├── host/                     # 配置、Docker、实例编排、状态目录、宿主桥、CLI 与审计
│   ├── runtime/
│   │   ├── network/              # mihomo、防火墙、许可与持续监控
│   │   ├── agents/               # 应用容器中的签名环境探针
│   │   └── checks/               # 命令行及显式浏览器检查
│   └── shared/                   # 共享策略与消息编码
├── containers/
│   ├── network/                  # 各镜像的 Dockerfile 与初始化文件
│   ├── browser/
│   ├── claude/
│   └── launcher/                 # ./cage 使用的 launcher 镜像
├── tests/
│   ├── unit/                     # 无 Docker 的单元测试
│   ├── integration/              # 隔离的 Docker 集成测试
│   └── fixtures/                 # 测试服务与替身
├── config/                       # 本机应用和代理配置，Git 忽略
├── config.example/               # 可提交的配置模板
├── state/                        # 实例登记、Linux 环境、应用资料和宿主桥状态，Git 忽略
└── docs/
    ├── configuration.md          # 配置参考
    ├── architecture.md           # 本文
    ├── stateful-service.md       # 有状态环境设计
    ├── host-bridge.md            # 宿主桥设计
    └── implementation.md         # 实现原理与源码阅读入口
```

镜像统一从项目根目录构建，构建上下文只包含公开运行代码和镜像文件。宿主模块不进入容器；修改镜像实际复制的文件会自动触发相关镜像重建。配置键名保持兼容。

## 实现参考

- [Claude-Cage 的网络检查与证据逻辑](https://github.com/Lee-xeo/Claude-Cage/blob/b6a8cf8271ad29b05f08b3e07d306e4dd8c32bbf/internal/workbench/safety.go)
- [Chromium DNS-over-HTTPS 受管策略](https://chromeenterprise.google/intl/en_ca/policies/dns-over-https-mode/)
- [Docker 共享网络命名空间](https://docs.docker.com/engine/network/#container-networks)
- [mihomo TUN](https://wiki.metacubex.one/en/config/inbound/tun/)
- [mihomo DNS 指定代理](https://wiki.metacubex.one/en/config/dns/)
- [OrbStack 网络与宿主代理](https://docs.orbstack.dev/docker/network)
