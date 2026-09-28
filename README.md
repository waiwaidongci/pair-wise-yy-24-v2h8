# 电台播出与版权窗口排程

一个不依赖第三方包、使用 SQLite 和标准库 HTTP 服务的电台排程项目。系统把“计划排期”和“实际播出”分开保存，支持地区授权、日期窗口、禁播时段、节目冷却、赞助商间隔、直播临时替换、实播对账与版权越界检查。

## 运行

需要 Python 3.11+。

```bash
python app.py
```

默认端口为 `8111`，页面地址是 <http://127.0.0.1:8111>。第一次启动会创建 `radio.db` 并写入三条演示排期。也可以设置端口和数据库位置：

```bash
PORT=9000 RADIO_DB=/tmp/radio.db python app.py
```

## 测试

```bash
python -m unittest discover -s tests -v
```

测试覆盖完整流程：排期、临时替换、播放日志、按日期对账；同时覆盖时间重叠、未授权地区和实播错节目等失败场景。

## 主要 API

- `GET /api/state`：节目、排期和最近对账异常
- `POST /api/programs`：创建节目并授权地区
- `POST /api/programs/{id}/regions`：追加地区授权
- `POST /api/schedule`：创建排期
- `POST /api/slots/{id}/replace`：替换计划节目并重新校验
- `POST /api/playout`：登记实播记录
- `POST /api/reconcile`：按日期生成漏播、错播、时长偏差和超授权异常
- `POST /api/interruptions`：按地区和起止时刻登记新闻插播占位单
- `POST /api/interruptions/{id}/release`：撤回或提前结束插播，复核受影响节目并恢复

准备排期时填写 `air_date`、`start_time`、`program_id`、`region`。页面会直接显示校验错误，不会保存失败的排期。

## 新闻插播

突发新闻不需要再逐条手动移除排期。登记插播单（`region`、`air_date`、`start_time`、`end_time`）后，系统自动找出同地区当天被覆盖的**未播**节目（没有实播记录的 planned/replaced 排期），整段转入顺延区，排期记录保留原时段；已播排期留在当天不动。插播占位期间，新排期不能落进插播窗口。

撤回或提前结束（可选 `actual_end_time`，须在开始与原定结束之间）后，系统只拿受影响节目在**原时段**复核版权（授权日期窗口、地区授权）、禁播时段和赞助商间隔：通过则恢复回当天排播单；有冲突则留在待重排区，`pending_reason` 写明具体原因，事件存档记录每条节目的转入/恢复/待重排结果。

判定逻辑放在 `interruptions.py`（纯函数，无状态），持久化和事件存档在 `database.py`，页面只负责展示覆盖节目与恢复结果。
