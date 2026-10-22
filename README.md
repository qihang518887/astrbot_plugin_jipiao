# 机票低价监控（AstrBot 插件）

v0.6.1 使用携程国内机票 H5 查价，沿用 `ticket-monitoring` 的持久化 Chromium 会话、手机模式、首页预加载与 `flightListSearchForH5` 响应解析流程。支持原有指令、LLM Tools、SQLite 任务、37% 观察策略和 QQ 低价提醒。Python 3.12+。

## 升级与已有监控

代码目录与运行数据目录分开。标准 AstrBot 部署中：

| 目录 | 升级操作 |
| --- | --- |
| `data/plugins/astrbot_plugin_jipiao/` | 替换为新插件代码 |
| `data/plugin_data/astrbot_plugin_jipiao/` | 原样保留整个目录 |
| `data/plugin_data/astrbot_plugin_jipiao/flights.sqlite3` | 原样保留，不删除、不重新创建 |

上面的 `data/` 是 AstrBot 的数据根目录；自定义数据根目录或 Docker 挂载时，以实际部署路径为准。插件使用 `StarTools.get_data_dir("astrbot_plugin_jipiao")` 获取数据目录。

1. 暂停插件或停止 AstrBot，备份整个插件数据目录。SQLite 在停止后备份，避免只复制主文件而遗漏 WAL 中的记录。
2. 把旧配置中的 `data_source` 明确改为 `ctrip`，删除已不用的 `tuniu_cookies` 配置项。两项携程路径可以留空；已有自定义会话路径可以继续使用。
3. 更新插件代码并安装下面的依赖。手动覆盖文件时删除旧代码目录中的 `tuniu.py`、`tuniu_cookies.py`、`tuniu_login.py`，或直接用新包替换整个代码目录。数据目录保持原位。不要使用会清理插件数据的卸载操作。
4. 启动 AstrBot，管理员私聊发送 `/携程登录`，使用携程 App 扫码并确认。看到“携程登录成功”后再查询机票验证。
5. 发送 `/我的监控` 检查原条件、目标价和观察记录。已有监控无需重新设置；重新设置反而会重置观察。

`data_source` 只接受 `ctrip` 和 `mock`。旧 `fli`、`tuniu` 值会直接报配置错误，不会自动映射。插件已移除途牛查询、Cookie、登录命令及其测试；运行时没有旧数据源适配分支。内置 `fli.models` 仅用于机场/航司名称校验，不参与 Google 查价。

数据库结构和业务算法未因这次登录改造而改变。任务条件、经停/中转开关、创建时间、观察记录、上次提醒价、通知会话与 revision 全部保留。旧观察基准继续使用，不同来源的报价口径和覆盖可能不同。`mock` 仍使用独立的 `mock.sqlite3`。

## 在机器人会话中登录携程

先在 **AstrBot 实际运行的 Python 环境** 安装依赖和 Chromium：

```bash
python -m pip install -r /实际插件路径/astrbot_plugin_jipiao/requirements.txt
python -m playwright install --with-deps chromium
```

Docker 部署在 AstrBot 容器或镜像内执行，并持久化插件数据目录。服务器无须图形桌面；登录与查询都使用无头 Chromium。Linux ARM64 需具备 Playwright Chromium 的系统依赖，当前尚未在用户的 ARM64 实机验证。

管理员私聊机器人发送：

```text
/携程登录
```

插件在服务器打开携程官方登录页，点击“扫码登录”，截取官网实际生成的二维码发回当前私聊。使用手机携程 App 扫描图片并确认。插件最多等待 120 秒，以官方页面跳转回登录指定的首页作为登录完成信号，再保存 Cookie 和 localStorage。二维码失效或等待超时后重新发送命令。

登录浏览器使用独立的临时目录，关闭后清理。只有确认成功才原子替换会话文件；超时、取消或浏览器错误不改原会话，卸载插件会结束尚在等待的登录。登录异常直接报错，不增加轮换身份、自动重试或其他登录路径。共享携程账户沿用原登录命令的管理员、私聊权限。

默认保存至插件数据目录 `cookies/ctrip_state.json`，后台查询自动使用，不需要在其他电脑登录或复制会话。设置了 `ctrip_storage_state` 时，登录保存和查询读取使用同一个自定义路径。会话凭证独立于 `flights.sqlite3`，登录不会重建监控。

v0.6.1 不导出或恢复 IndexedDB 查询缓存，避免 Playwright 恢复该缓存时出现 `IDBObjectStore` 键错误。已有会话文件即使包含 IndexedDB，读取时也仅取 Cookie 和 localStorage；成功查询后写回相同格式。升级不要求删除会话或浏览器目录。所有查询使用明确指定的会话路径，缺少会话文件直接要求 `/携程登录`，不转用浏览器缓存或另一个本地文件。

“登录成功”表示官网完成登录，不保证机票接口在服务器 IP 上一定可用，应再查一次机票。会话失效时再次发送 `/携程登录`。会话文件含登录凭证，不要提交 GitHub 或发送到聊天。Linux 新文件权限为 600，文件及目录应允许 AstrBot 用户读写。

`ctrip_session` 仅保留为可选的本地调试导出工具，机器人登录不依赖它。安装包不含 `.local`、浏览器资料、登录会话或数据库。

## 单次查价

当前工作区运行以下命令，不创建监控、不访问监控数据库、不发送 QQ 消息：

```powershell
rtk proxy astrbot_plugin_jipiao/.venv/Scripts/python.exe -m astrbot_plugin_jipiao.smoke --dep 大连 --arr 上海 --date 2026-11-05 --storage-state astrbot_plugin_jipiao/.local/ctrip_state.json
```

`--profile-dir` 指定浏览器资料目录，CLI 默认源码目录 `.local/ctrip_browser`；`--storage-state` 默认源码目录 `.local/ctrip_state.json`。CLI 默认路径与 AstrBot 运行时的数据目录不同，服务器测试应显式指定与插件配置一致的两个路径。

在服务器插件包的父目录中测试（路径、日期换成实际值）：

```bash
python -m astrbot_plugin_jipiao.smoke --dep 大连 --arr 上海 --date 2026-11-05 --storage-state /实际数据目录/cookies/ctrip_state.json --profile-dir /实际数据目录/browser/ctrip
```

`--allow-stopovers`、`--allow-connections` 分别允许经停和中转；`--allow-stops` 同时允许。`--time-window 08:00-18:00`、`--airlines CA,MU` 验证原筛选条件；`--mock` 是明确的模拟查询。

## 指令、LLM Tools 与范围

- `/携程登录`：管理员私聊扫码，直接保存服务器会话。
- `/机票监控 出发地 到达地 YYYY-MM-DD [时间段] [航司] [目标价] [直飞|接受经停|接受中转|接受经停与中转]`
- `/我的监控`：条件、观察阶段、记录的低价、上次提醒价与最近状态。
- `/取消监控`：停止当前平台账号的任务。
- `search_flights`：自然语言查价。默认先直飞，无匹配报价再依次查经停、中转备选。
- `set_flight_monitor`、`get_flight_monitor`、`cancel_flight_monitor`：自然语言设置、查看、取消监控。

```text
/机票监控 大连 上海 2026-11-05 - - 350
/机票监控 大连 上海 2026-11-05 08:00-18:00 国航,东航 500 接受经停
/机票监控 大连 上海 2026-11-05 - - 500 接受中转
```

一个平台账号一个任务，重新设置会覆盖并重置观察。通知发送到最后一次设置所在的会话。仅单程、1 成人、经济舱、人民币。中文城市“上海”包括 SHA/PVG；明确填写机场码 SHA 则只匹配虹桥，不能把浦东报价算进去。时间段约束首段起飞，航司白名单约束全部航段营销航司。

| 用户范围 | allow_stopovers | allow_connections |
| --- | --- | --- |
| 仅直飞 | false | false |
| 直飞、经停 | true | false |
| 直飞、中转 | false | true |
| 全部 | true | true |

航段内的 `fsitem` 是经停，多个 `mutilstn` 航段按完整航班号和换机场情况分类。同一行程有经停又有换乘，需要两个开关都开启。经停只返回城市与时长时不编造中间到离时刻；只在中转时间能确认时标注过夜。同航班号不保证不换机。备选查询不修改任务，后台没有结果时不会扩大已保存的范围。

## 提醒、算法与存储

数据仍放在 AstrBot 插件数据目录：真实 `flights.sqlite3`、模拟 `mock.sqlite3`。`tasks` 表每用户一行，保存查询条件、最低三次观察、最近报价、上次提醒价、通知会话和 revision；不保存每轮全部历史。取消或北京时间出发日零点到期时删除任务。重启保留观察记录与提醒状态。

任务串行查询，默认间隔 30 秒并有 -1 至 +2 秒抖动，每轮结束等待 1800 秒。实际周期包含全轮查询耗时。没有按临近起飞自动加快、固定推送时刻或夜间免打扰。

设置目标价时，首次不高于目标价即提醒，之后只有进一步降价才提醒。未设置目标价时沿用原 37% 策略：起飞前 60 天以外等待；从 `max(创建时间, 起飞前60天)` 到起飞前七天的剩余时间中，前 37% 观察、至少 12 小时；保留最低三次记录，六小时以内同价去重；决策期取这三次记录中的最高价作为基准；进入起飞前七天优先兜底，后续仅更低价再次提醒。经验规则不保证最低买入价。

查询失败不会消耗提醒机会，发送失败保留下一轮重试机会，状态见 `/我的监控`。QQ 官方群适配器重启后可能需要在原群发送 `/我的监控` 恢复群场景，并开启群主动发言权限。平台接受消息不等于已读，不保证恰好送达一次。

## 配置、代理与价格口径

| 配置 | 默认 | 作用 |
| --- | --- | --- |
| data_source | ctrip | 携程查询；mock 独立模拟库，旧名称不再接受 |
| ctrip_storage_state | 空 | 默认插件数据目录 cookies/ctrip_state.json |
| ctrip_profile_dir | 空 | 默认插件数据目录 browser/ctrip |
| round_interval_seconds | 1800 | 每轮结束等待秒数，最少 60 |
| task_delay_seconds | 30 | 任务间隔秒数，最少 1 |

浏览器启动读取 AstrBot 进程的 HTTPS_PROXY/https_proxy，未设置时用 ALL_PROXY/all_proxy；NO_PROXY/no_proxy 可让携程直连。不读取 Windows 系统代理注册表。登录和查价工具规则一致。容器内的 127.0.0.1 是容器自身；Chromium 代理能力以实际支持为准，未验证带认证的 SOCKS。

价格取有库存、成人经济舱政策的整程 `policyinfo.tprice`，取最低后向上取整。查询使用已登录的携程账户，不再仅凭 `LimitedCtripMemberShip` 标签排除报价；该标签本身不足以证明存在付费会员或等级要求。公务舱、学生/年龄/人数限制、其他 `Limited` 标记（如航司会员、指定卡）和高风险政策仍过滤。不合计分段价格，不拿日历价代替航班价，最终账户资格、税费、行李与实付以订票页为准。当前验证国内单程，不保证国际/港澳台、往返、儿童价、全网覆盖或全网最低价，没有自动购票。

查询在线程中执行，使用全局锁串行访问浏览器目录；卸载时等待当前查询退出。上游错误直接报错，不添加身份轮换、失败回退或重试。

## 构建与验证

```powershell
rtk proxy astrbot_plugin_jipiao/.venv/Scripts/python.exe -m pytest -c astrbot_plugin_jipiao/pyproject.toml astrbot_plugin_jipiao/tests -q
rtk proxy astrbot_plugin_jipiao/.venv/Scripts/ruff.exe check astrbot_plugin_jipiao
rtk proxy astrbot_plugin_jipiao/.venv/Scripts/python.exe astrbot_plugin_jipiao/build_package.py
```

安装包：`dist/astrbot_plugin_jipiao.zip`。requirements 移除当前不用的 curl-cffi 等 Google 抓取依赖，新增 Playwright >=1.59；保留内置 fli 枚举与 Pydantic 以维持原输入校验。来源和 Apache-2.0 许可见 `THIRD_PARTY_NOTICES.md`、`LICENSE.ctrip`。

2026-10-22 本机验证：携程会话文件载入全新浏览器目录后，插件 `fetch_raw_flights` 查到大连至上海、2026-11-05 的 47 条原始行程；普通经济舱规范化后 36 条（直飞 22、经停 2、中转 12），最低 CA8911 17:40–19:55、CNY 320。记录见 `validation/live_ctrip_plugin_2026-10-22.json`。回放样本只保留公开航班字段，不含 Cookie、PID 或 token。

离线测试覆盖原算法、旧任务完整保留、原会话继续提醒、机场/时段/全部航司筛选、经停/中转、跨日与失败不消耗提醒。未验证 Linux ARM64 实机、真实 QQ 送达或连续多日运行。

v0.6.0：113 项测试通过，Ruff 检查与格式检查通过。本机全新无头浏览器成功生成并截取官方扫码二维码；扫码确认后的保存流程使用隔离测试验证，尚未实际手机扫码。记录见 `validation/ctrip_qr_login_2026-10-22.md`。保留 AstrBot 原有的 audioop 与 register 两项弃用提示。

v0.6.1：120 项测试通过，Ruff 检查与格式检查通过。在原会话副本上，使用同一个持久化浏览器目录连续三次查询成功：北京→上海（2026-11-05）两次各 185 条原始结果、136 条报价；乌鲁木齐→大连（2026-11-08）67 条原始结果、46 条报价，其中经停 5、中转 41。CZ6800 经停天津 60 分钟、CNY 1170，CZ6966 经停西安 65 分钟、CNY 1230，均不会进入不经停直飞范围。测试会话写回均无 IndexedDB，未修改原会话、访问监控库或发送 QQ 消息。记录见 `validation/live_ctrip_repeat_2026-10-22.json` 和 `validation/ctrip_repeat_fix_2026-10-22.md`，价格仅为测试时的返回值。
