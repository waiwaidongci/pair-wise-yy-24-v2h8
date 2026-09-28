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
- `POST /api/breaking`：按地区和起止时刻登记新闻插播单（`air_date`、`region`、`start_time`、`end_time`、`title`）
- `POST /api/breaking/{id}/withdraw`：撤回插播，受影响节目在原时段复核版权、禁播和赞助间隔后恢复
- `POST /api/breaking/{id}/end`：结束插播（`actual_end_time`）；提前结束时复核恢复，正常结束则保留在顺延区

准备排期时填写 `air_date`、`start_time`、`program_id`、`region`。页面会直接显示校验错误，不会保存失败的排期。

## 新闻插播占位

值班员不再需要逐条删排期：

1. 登记插播单后，同地区同日期、与插播窗口时间重叠的**未播**节目自动转入顺延区（排期状态 `deferred`），存档表保留原时段、原节目和原状态；已有实播记录的排期留在当天不动。
2. **撤回**或**提前结束**插播后，只拿受影响的节目在其原时段复核：版权窗口与地区授权、禁播时段、赞助商间隔（含当天已播的同赞助节目）。复核通过即恢复原状态；不通过则进入 `pending_reschedule` 待重排，并在存档中写明原因。
3. 正常播完的插播单不自动恢复，受影响节目留在顺延区等待人工处理。
4. 顺延中/待重排的排期不参与常规时间重叠校验和当天对账，页面“新闻插播”区按插播单展示覆盖节目和每个节目的恢复结果。

判定（`breaking.py`，纯函数、不碰数据库与 HTTP）、存档（`database.py` 中的插播单、`slot_deferrals` 存档和状态迁移）、页面/接口（`static/index.html`、`app.py`）三层分开承担。
