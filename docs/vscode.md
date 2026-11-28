# 用 VS Code 连接笼子里的 Claude

VS Code 可以直接连到正在运行的 Claude 容器，在笼子里使用 Claude Code 扩展：会话列表、思考过程、执行的命令都按扩展原有的方式展示，文件就是你 Mac 上的 workspace。

不需要 SSH，也不需要开端口：VS Code 的 Dev Containers 扩展通过 Docker 进入容器，和 `./cage shell` 一样用的是 `docker exec`。

## 工作方式

```text
Mac
 └─ VS Code（只显示界面）
      │  Dev Containers 扩展，经 Docker 进入容器
      ▼
笼子：<实例>-claude 容器
 ├─ VS Code Server、Claude Code 扩展、VS Code 里的终端
 ├─ Claude 的登录与会话：/home/claude/.claude（与 ./cage claude 共用）
 └─ workspace：与 Mac 相同的绝对路径
      │  所有出站流量
      ▼
   网关 ──▶ 节点 ──▶ Internet
```

- Claude Code 扩展装在容器那一侧，Claude 的请求、登录和用量查询都从笼子里经节点发出，时区、语言也是笼子里的设置。
- 在 VS Code 里改文件就是改 Mac 上的 workspace，Mac 和笼子看到的是同一份。
- `./cage claude` 和 VS Code 共用同一个登录和同一份会话历史。

## 前提

- `./cage up` 已经启动实例。Claude 容器名是 `<实例名>-claude`，例如实例 `caged-claude` 的容器是 `caged-claude-claude`；`./cage status` 可以查看。
- 要打开的项目在 `workspace` 配置的目录下（见[配置参考](configuration.md#workspace-与-claude-授权持久化)）。不在 workspace 下的目录，容器里没有。
- Mac 上装了 VS Code，以及 Microsoft 的 **Dev Containers** 扩展（`ms-vscode-remote.remote-containers`）。

## 一次性配置

### 1. 让 VS Code 找到 docker

从程序坞启动的 VS Code 不一定带有终端里的 `PATH`。如果后面附加容器时找不到 docker，在 VS Code 的用户设置（`settings.json`）里写上 docker 的完整路径，例如 OrbStack：

```json
"dev.containers.dockerPath": "/usr/local/bin/docker"
```

### 2. 为容器写一份连接配置

在命令面板（`⇧⌘P`）执行 **Dev Containers: Open Named Container Configuration File**，选择 Claude 容器（例如 `caged-claude-claude`），写入：

```json
{
  "remoteUser": "claude-user",
  "workspaceFolder": "/Users/yourname/Projects/your-project",
  "extensions": ["anthropic.claude-code"],
  "settings": {
    "remote.autoForwardPorts": false
  }
}
```

| 字段 | 作用 |
| --- | --- |
| `remoteUser` | **必须是 `claude-user`**。容器的默认用户是 root；以 root 连接时，Claude 会在 `/home/claude/.claude` 写出 root 所有的文件，之后 `./cage claude` 就写不进去了。`claude-user` 会在容器启动时改成你在 Mac 上的 UID/GID。 |
| `workspaceFolder` | 打开时进入的目录，填 workspace 下的项目路径，和 Mac 上的路径完全相同。 |
| `extensions` | 自动在容器里安装 Claude Code 扩展。 |
| `remote.autoForwardPorts` | 关闭自动端口转发。VS Code 默认会把容器里监听的端口转发到 Mac 本机，关掉它，笼子里的服务就不会被意外开到 Mac 上。 |

这份配置按容器名保存，`./cage down` 后再 `./cage up`，容器名不变，配置继续有效。

### 3. 第一次连接

1. 命令面板执行 **Dev Containers: Attach to Running Container…**，选择 Claude 容器。
2. VS Code 打开一个新窗口，左下角显示容器名。第一次会把 VS Code Server 装进容器，需要一点时间。它装在持久化的 `/home/claude` 下，之后重启实例不用重装。
3. 打开 Claude Code 扩展（侧栏的 Claude 图标，或命令面板执行 **Claude Code: Open in New Tab**）。如果之前已经用 `./cage claude` 登录过，这里直接可用；否则按提示登录一次，登录也发生在笼子里。

## 日常使用

### 打开

1. `./cage up`。
2. 在 VS Code 的“最近打开”里选带容器名的那一项，或再执行一次 **Attach to Running Container…**。

### 会话

- 在 Claude Code 面板里新建会话、查看历史会话、继续之前的会话，用法和本机的 Claude Code 扩展一样。
- 思考过程、工具调用和执行的命令在对话里默认折叠，点开可以看到详情。
- 会话历史保存在 `/home/claude/.claude`，宿主上对应 `<state.dir>/<实例>/claude`；`./cage claude --resume` 也能看到在 VS Code 里开的会话。

### 终端

VS Code 里新开的终端就在容器里，身份是 `claude-user`：

- 直接运行 `claude` 就是笼子里的 Claude Code；
- 装软件用 `sudo apt-get …`（`claude.sudo: false` 时不可用，改在宿主执行 `./cage shell --root`）；
- `ssh host` 经宿主桥回到 Mac（见[宿主桥设计](host-bridge.md)）。

### 重启实例后

`./cage down` 会让 VS Code 窗口断开。`./cage up` 之后，在断开的窗口里点 **Reload Window**（或命令面板执行 **Developer: Reload Window**），即可重新连上。

### 结束

关掉 VS Code 窗口不会停止容器，也不会中断后台的 Claude 任务；需要停止时执行 `./cage down`。

## 隔离相关的注意事项

- **Claude Code 扩展只装在容器里。** 在 Mac 本机的 VS Code 里不要登录 Claude Code 扩展，否则同一个账号会经 Mac 本机网络访问 Anthropic，和笼子里的出口不一致。连接容器的窗口里，扩展面板会分成“本地”和“容器”两组，确认 Claude Code 在容器一组。
- **VS Code 本身的流量。** VS Code 界面、Mac 一侧的扩展和设置同步仍走 Mac 本机网络，与 Claude 账号无关；容器一侧的扩展（包括 Claude Code）和终端里的命令都经节点。
- **不经过 `./cage` 的检查。** VS Code 直接进入容器，不像 `./cage claude` 那样先确认容器仍接在网关上。网关停掉时防火墙照样断开外网，不会泄漏，只是 VS Code 里看到的是连接失败；用 `./cage status` 查看原因。
- **出站被暂停时。** `./cage pause` 或持续检查失败时，Claude 会连不上 Anthropic，这是预期行为；`./cage resume` 或恢复后重试即可。

## 常见问题

**附加容器时列表里没有 Claude 容器，或提示找不到 docker**：先 `./cage status` 确认容器在运行；仍然不行，按“让 VS Code 找到 docker”设置 `dev.containers.dockerPath`。

**Claude Code 要求重新登录，或看不到 `./cage claude` 的会话**：多半是以 root 连接的。打开连接配置，确认 `"remoteUser": "claude-user"`，然后 **Developer: Reload Window**。

**`./cage claude` 报无法写入 `/home/claude/.claude`**：之前以 root 连接过，留下了 root 所有的文件。改好 `remoteUser` 后，修正一次属主：

```bash
./cage shell --root -- chown -R claude-user:claude-user /home/claude
```

**打开的目录是空的，或提示路径不存在**：这个目录不在 workspace 配置里。把它加到 `workspace`，`./cage down && ./cage up` 后重新连接。

**扩展或终端里网络不通**：执行 `./cage status` 看 Outbound 一行，`./cage logs` 看断网原因。

## 与理想客户端的差距

VS Code 满足会话管理、完整展示 agent 工作内容和输入方便这几项，但没有手机端，也没有定时执行；会话列表的置顶、Emoji 标记、归档能做到什么程度，以 Claude Code 扩展当前版本为准。完整需求和其他候选方案见 [issue #17](https://github.com/orangemi/caged-claude/issues/17)。
