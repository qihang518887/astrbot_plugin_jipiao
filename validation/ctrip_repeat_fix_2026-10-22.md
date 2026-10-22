# v0.6.1 连续查询与会员政策修复验证

在用户保存会话的副本上复现了 `BrowserContext.set_storage_state` 错误：

```text
Unable to restore IndexedDB: Failed to execute 'add' on 'IDBObjectStore':
The object store uses out-of-line keys and has no key generator
and the key parameter was not provided.
```

该会话包含 2 个 IndexedDB 数据库。仅移除 IndexedDB 后，原持久化浏览器加
`set_storage_state` 能查询成功，因此不能将失败笼统归因于两者互斥。
也不能从这个样本推断每次导出都立即失败；它能随查询写回产生下次无法恢复的缓存。

修复：登录与查询均使用 `indexed_db=False` 导出；读取与写入只保留携程 Cookie
和 localStorage，从不恢复 IndexedDB。已有含缓存的会话文件可直接使用，成功查询后
原子写回干净格式。fetcher 要求调用方明确给出会话和浏览器路径，不再将 None
隐式替换成源码 `.local` 中的文件，也不改用只有 profile 的另一套登录状态。

政策修复只放行准确的 `LimitedCtripMemberShip` 标记；库存、经济舱、资格说明、
其他 Limited 标记及 HighRisk 的过滤保持。未声称所有会员政策均无额外资格要求。
单航段的 `fsitem` 仍是经停依据，`segtpe=1` 不会将有经停点的行程当作不经停直飞。

在同一测试浏览器目录中，完整 `fetch_raw_flights` 连续三次真实查询成功：

| 航线 | 日期 | 原始行程 | 归一化报价 |
| --- | --- | --- | --- |
| 北京→上海，第一次 | 2026-11-05 | 185 | 136，直飞 115、中转 21 |
| 北京→上海，第二次 | 2026-11-05 | 185 | 136，直飞 115、中转 21 |
| 乌鲁木齐→大连 | 2026-11-08 | 67 | 46，经停 5、中转 41 |

CZ6800 09:45–16:10，经停天津 60 分钟，CNY 1170；CZ6966 17:25–次日 00:25，
经停西安 65 分钟，CNY 1230。价格为查询时返回值。二者的公开字段另存为最小回放样本，
不含 PID、账号、会话或令牌。测试会话每次写回均不含 IndexedDB。

原会话哈希保持不变，未访问用户监控数据库、发送 QQ 消息或进行购票操作。
机器端 Linux ARM64、实际 QQ 图片送达和连续多日运行仍未验证。

120 项测试通过；Ruff 检查与格式检查通过。回归覆盖重复恢复和写回、现有含 IndexedDB
会话的读取、扫码会话保存、会员政策与其他资格限制、真实经停航班回放和全部原业务测试。
