# 机票低价监控（AstrBot 插件）

基于本地 AstrBot 4.29.1 和 fli / flights 0.10.0 实现。支持 QQ 指令与大语言模型自然语言函数调用（LLM Function Calling / Tool Use），提供真实票价查询、SQLite 任务持久化、37% 法则价格基准线计算、后台防风控轮询及主动低价推送。Python 3.12+。

## 功能与交互方式

插件同时支持 **斜杠指令交互** 与 **大模型自然语言对话交互（LLM Tools）**：

### 1. 指令交互
- `/机票监控 出发地 到达地 YYYY-MM-DD [时间段] [航司] [目标价]`：开启监控任务。
- `/我的监控`：查看当前任务详情、所处观察阶段、记录的低价与最新状态。
- `/取消监控`：取消并停止监控任务。

### 2. 大语言模型工具 (LLM Function Calling / Agent Tools)
插件向 AstrBot 注册了 4 个原生函数工具，当用户在私聊或群聊中自然对话（无需输入斜杠指令）时，大模型可自动识别意图并调用：
- `search_flights`：实时查询指定航线与日期的机票价格与直飞航班时刻（如用户问：“帮我查下后天北京去上海的机票”）。
- `set_flight_monitor`：通过自然语言开启机票监控（如用户说：“帮我盯一下下个月1号北京去上海的国航机票，低于600告诉我”）。
- `get_flight_monitor`：自然语言查看我的监控进度（如用户问：“我现在监控了什么航班？”）。
- `cancel_flight_monitor`：自然语言取消监控（如用户说：“帮我把机票监控取消了吧”）。
- 同一 AstrBot 平台实例中，每个用户标识只有一项任务；重新设置会覆盖旧任务并重置观察价格。
- 设置时记录消息来源，通知发送到最后一次设置所在的私聊或群聊，不会自动把群聊任务转为私聊。
- 单程、1 名成人、经济舱、直飞、人民币；北京时间出发日零点停止监控。起降时刻采用机场当地时间。
- 支持常见中文城市、机场三字码、同城多机场、航司别名或代码、起飞时段与目标价。
- 达到目标价后通知；只在价格进一步降低时再次通知。未设置目标价时使用文末的 37% 观察策略。
- 数据源 `fli` 与 `mock` 使用独立数据库。模拟模式在指令回复和低价提醒中标注，不会在真实查询失败时自动切换为模拟报价。
- 查询失败、没有匹配报价、发送失败分别记录在 `/我的监控` 中。

## 在当前工作目录安装

插件源码目录是 `D:\Program file\work\机票监控\astrbot_plugin_jipiao`。
发布包是这个目录中的 `dist\astrbot_plugin_jipiao.zip`，不包含开发虚拟环境。

以下命令在 PowerShell 执行。会初始化 AstrBot 自己的运行环境；开发测试环境 `.venv` 不需要复制到插件安装目录。

```powershell
Set-Location 'D:\Program file\work\机票监控\AstrBot'
uv sync
uv pip install --python '.venv\Scripts\python.exe' -e '..\fli'
New-Item -ItemType Directory -Path '.\data\plugins' -Force
Expand-Archive -LiteralPath '..\astrbot_plugin_jipiao\dist\astrbot_plugin_jipiao.zip' -DestinationPath '.\data\plugins'
$env:FLI_TIMEOUT = '20'
uv run --no-sync main.py
```

安装后入口应位于 `AstrBot\data\plugins\astrbot_plugin_jipiao\main.py`。如已存在同名插件，请先停止/卸载旧版本再替换，不要把开发目录中的 `.venv` 复制过去。

这里优先安装已检查的相邻 `fli` 源码；Python 发行包的名称是 `flights`。`requirements.txt` 固定为 `flights==0.10.0`。只安装同版本 PyPI 包不能证明代码与当前本地 checkout 一致。

### 自动读取代理环境变量（v0.1.1）

插件在每次 HTTP 请求前自动读取 **AstrBot 进程的环境变量**，并显式传给 fli 的网络客户端。无需修改插件配置或写死代理地址。

| 环境变量 | 用途 |
| --- | --- |
| `HTTPS_PROXY` / `https_proxy` | HTTPS 请求的代理，Google Flights 使用这一项 |
| `HTTP_PROXY` / `http_proxy` | HTTP 请求的代理；单独设置它不会代理 HTTPS |
| `ALL_PROXY` / `all_proxy` | 未配置对应协议代理时的后备代理 |
| `NO_PROXY` / `no_proxy` | 直接连接的主机或域名，逗号分隔；`*` 表示全部直连 |

优先判断 `NO_PROXY`；然后采用协议对应的代理，最后回退到 `ALL_PROXY`，均未配置时直连。区分大小写的系统中同名小写变量优先。支持 HTTP/HTTPS 代理，以及 `socks5://`、`socks5h://` SOCKS 代理；`socks5h` 由代理端解析目标域名。不读取 Windows 注册表代理，不自动启动代理程序。

**当前电脑最简配置**：在启动 AstrBot 的同一 PowerShell 终端中设置 `HTTPS_PROXY` 即可。已确认的本机代理端口为 `7897`，换电脑时请改成实际地址。

```powershell
$env:HTTPS_PROXY = 'http://127.0.0.1:7897'
$env:NO_PROXY = 'localhost,127.0.0.1,::1'
uv run --no-sync main.py
```

变量名 `HTTPS_PROXY` 表示代理的目标请求是 HTTPS；代理地址的 `http://` 表示连接到本地代理所用的协议，两者不冲突。要为 HTTP 和其他请求同时配置代理，可用：

```powershell
$env:HTTP_PROXY = 'http://127.0.0.1:7897'
$env:HTTPS_PROXY = $env:HTTP_PROXY
$env:ALL_PROXY = $env:HTTP_PROXY
$env:NO_PROXY = 'localhost,127.0.0.1,::1'
uv run --no-sync main.py
```

只需设置一次用户环境变量、供以后启动的程序继承，也可以执行：

```powershell
[Environment]::SetEnvironmentVariable('HTTPS_PROXY', 'http://127.0.0.1:7897', 'User')
[Environment]::SetEnvironmentVariable('NO_PROXY', 'localhost,127.0.0.1,::1', 'User')
```

设置用户环境变量后，关闭并重新打开启动器/终端，再重启 AstrBot。已经运行的进程不会自动收到外部环境变量修改；仅重载插件不等于重新继承环境。插件会逐请求重新读取进程内环境；`FLI_TIMEOUT` 则仍需在导入 fli 前设置。

Docker 部署需通过 Compose 的 `environment` 或 `docker run -e` 将变量传入容器；容器里的 `127.0.0.1` 指容器自身，不是 Windows 宿主机，应使用容器可达的代理地址。

本插件的显式代理配置只包装本插件创建的 fli 查询，不修改 fli 全局客户端或系统代理；同一进程中的其他库仍可能自行读取这些通用环境变量。

## 接入 QQ 与试用

在 AstrBot WebUI 中配置 QQ 官方 WebSockets 机器人，或者配置已有的 OneBot v11 / NapCat 连接。具体流程见相邻 AstrBot 项目的 `docs/zh/platform/qqofficial/websockets.md` 与 `docs/zh/platform/aiocqhttp.md`。

群聊主动提醒需要在 QQ 中开启机器人主动发言。当前本地 QQ 官方适配器重启后不保留群场景缓存：重启后，请在原群发送一次 `/我的监控` 恢复会话。插件检测到群场景未就绪时会保留提醒机会，并记录失败原因；不会绕过 QQ 权限。

先在私聊尝试：

```text
/机票监控 北京 上海 2026-12-01 08:00-18:00 国航,东航 500
/我的监控
/取消监控
```

只设置目标价，时间与航司不限：

```text
/机票监控 PEK SHA 2026-12-01 - - 500
```

不设置目标价，使用观察策略：

```text
/机票监控 北京 上海 2026-12-01 12:00-20:00 - -
```

日期示例应换成实际未来日期。若要验证提醒流程，可在 WebUI 将数据源改为 `mock` 并重载插件，再设置目标价为 `1000` 的任务；模拟报价范围为 350–950 元。模拟测试完成后切回 `fli` 并重新创建真实任务。两种模式下保存的任务独立，切换后仅当前模式的任务运行。

后台监控和这些指令不调用大模型，无需为机票业务配置 LLM API。

## 配置与数据

| 配置 | 默认值 | 说明 |
| --- | --- | --- |
| `data_source` | `fli` | `fli` 真实查询，`mock` 显式模拟 |
| `round_interval_seconds` | `1800` | 每轮结束后等待秒数，最少 60 |
| `task_delay_seconds` | `30` | 每任务间隔，增加 -1～+2 秒抖动，最少配置 1 |

实际轮询周期是“全轮查询耗时＋任务间隔＋30 分钟”，不是每人严格每 30 分钟一次。新任务会唤醒空闲轮询，但最近检查过的旧任务不会立即重复查询。该实现适合少量用户；尚未对相同航线合并查询。

数据库位于 AstrBot 数据目录的 `plugin_data/astrbot_plugin_jipiao/`：

- `flights.sqlite3`：真实模式。
- `mock.sqlite3`：模拟模式。

数据库在原清单字段之外增加 `revision`、`last_checked_at`、`last_status`、`last_best_json`。修订标识与用户锁阻止取消/覆盖后的旧查询发送提醒；数据库重启后保留观察价格和上次推送价格。推送异常会保留价格提醒机会，下轮重试。

`context.send_message()` 成功表示交给匹配的适配器，不是用户已读回执。发送已被平台接受但响应中断、或发送后进程突然退出等情况下，仍可能重复提醒；本版本不承诺消息恰好送达一次。到期任务先删除，再尝试一次结束通知；结束通知失败不会阻止清理任务。

## 37% 观察策略

这是按需求实现的经验规则，不保证找到机票最优买入时机：

1. 起飞前 60 天以外不查询、不记录。
2. 从 `max(创建时间, 起飞前60天)` 开始，到起飞前 7 天的剩余时间中，前 37% 用来观察；计算的观察时长至少 12 小时。
3. 观察期只保留最低 3 次记录；与这些已保留记录中 6 小时以内的同价记录去重。相隔 6 小时的同价可以重复入榜。
4. 决策期以最低 3 次记录中的最高价作为阈值；没有观察记录时等待前 7 天兜底。
5. 进入起飞前 7 天时优先兜底，即使不足 12 小时观察也会结束观察。
6. 每次通知后，仅更低价格再次提醒。等于观察最低价是“追平”，更低才是“刷新”。

## 数据边界

fli 当前读取 Google Flights 搜索结果，无法保证全量航班或覆盖国内所有销售渠道。提醒只称“本次匹配最低价”，不代表全网最低价。时间段筛选是本地筛选，过滤后为空不代表实际没有航班。

第一版不支持中转、往返、儿童价、行李额度、退改规则和自动购票。报价可能发生变化，税费、行李和支付总价以购票页面为准。缺失价格、非人民币报价、非直飞结果会跳过；小数报价向上取整，避免把高于整数目标价的报价误判为达标。

查询在线程中执行，不阻塞机器人事件循环。Python 无法强制终止已发出的线程内 HTTP 请求，因此卸载时会等待当前查询退出，避免重载后留下重复查询线程。请求超时和重试由 fli 管理，`FLI_TIMEOUT` 是单次请求超时，不是整个搜索的硬性总超时。

## 开发与验证

从工作目录运行：

```powershell
Set-Location 'D:\Program file\work\机票监控'
# 如需新建测试环境：
uv venv --python 3.12 astrbot_plugin_jipiao/.venv
uv pip install --python astrbot_plugin_jipiao/.venv/Scripts/python.exe -e ./AstrBot -e ./fli pytest pytest-asyncio ruff

astrbot_plugin_jipiao/.venv/Scripts/python.exe -m pytest -c astrbot_plugin_jipiao/pyproject.toml astrbot_plugin_jipiao/tests -q
astrbot_plugin_jipiao/.venv/Scripts/python.exe -m ruff check astrbot_plugin_jipiao

# 单次真实查询，不保存任务、不发送消息：
astrbot_plugin_jipiao/.venv/Scripts/python.exe -m astrbot_plugin_jipiao.smoke --dep PEK --arr SHA --date 2026-12-01

# 明确标注的模拟查询：
astrbot_plugin_jipiao/.venv/Scripts/python.exe -m astrbot_plugin_jipiao.smoke --mock

# 构建可安装压缩包：
astrbot_plugin_jipiao/.venv/Scripts/python.exe astrbot_plugin_jipiao/build_package.py
```

测试使用真正的 AstrBot 模块、指令参数解析器、消息链和 fli 模型；报价请求与发送端在离线测试中替换为可控对象。框架导入产生的配置放在临时目录，不接触已有机器人账号。

本次结果：67 项测试通过，Ruff 检查通过。代理测试包含环境变量优先级、小写名称、`NO_PROXY`、变量变更与删除，以及用真实 fli 网络客户端连接本地测试服务器验证代理转发和直连。测试输出保留两条弃用提示：AstrBot 引入的 `audioop`，以及本插件使用的传统 `register` 装饰器；在当前 AstrBot 4.29.1 / Python 3.12 中可用。升级框架时应复查插件注册接口。

真实查询记录见 `validation/live_query_2026-10-18.json`。2026-10-18 19:12（北京时间）查询 PEK → SHA、出发日 2026-12-01，返回 5 条有效报价；当次最低 HU7601、07:25–09:35、人民币 2510 元。这是单次接口结果，不是对国内平台价格的交叉核验。

v0.1.1 在 2026-10-18 22:25（北京时间）仅设置 `HTTPS_PROXY`，清空 `HTTP_PROXY` 与 `ALL_PROXY` 后再次查询成功，返回 5 条报价；记录见 `validation/live_proxy_query_2026-10-18.json`。

本次验证不包含真实 QQ 登录、消息实际送达、持续数日运行或购票成交验证。
