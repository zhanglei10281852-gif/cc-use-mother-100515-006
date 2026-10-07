# 公共服务数字体验回访

面向峰会展示的公共服务应用回访归因后端：接收多城市体验反馈，支持相似意见归并、
证据补充、责任分派与有期限的处理承诺，帮助产品团队区分真实问题、重复意见与
地区政策差异。纯 Python 标准库实现，运行时不依赖外部服务。

## 领域规则

| 规则 | 实现 |
| --- | --- |
| 反馈接收 | 每条反馈带来源（实名/匿名）、场景、政策版本、影响范围（地区 + 等级）与立场（问题/反例/一般） |
| 相似意见归并 | 同场景、地区有交集且文本相似度（中文 bigram containment）≥ 0.4 自动归并；审核人员可 `suggest`/`merge` 人工归并 |
| 合并保留归因 | 归并只挂接不删除：原始提交者与提交时间永久保留，归并全程留痕 |
| 匿名不可逆 | 匿名来源仅持久化 HMAC 伪名（`anon:...`），原始身份从不落盘，无法反向识别 |
| 政策版本门禁 | 政策版本更新只影响未结论事项；结论冻结作出时的政策版本 |
| 逾期升级 | 承诺逾期按 L1(0h)/L2(24h)/L3(72h) 阶梯幂等推进；服务启动（含重启）自动补扫 |
| 审核解释 | `explain` 输出结论依据的支撑反馈与反例（含提交者、时间）及完整事件链 |
| 地区报告 | `region` 命令 / `GET /regions/{region}/report` 输出当前状态与历史决定 |

数据以 JSON 原子写入存储文件（默认 `data/store.json`），重启后完整恢复。

## 运行测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 编译检查

```bash
python3 -m compileall -q src tests run_cli.py
```

## 命令行

```bash
# 提交反馈（匿名加 --anonymous，原始身份不落盘）
python3 run_cli.py submit --text "公交扫码经常失败" --scenario 移动端 \
    --policy-version P-2026.1 --region 杭州 --reporter citizen-01

# 归并：推荐候选 + 人工归并
python3 run_cli.py suggest <feedback_id>
python3 run_cli.py merge <feedback_id> <issue_id> --actor reviewer

# 证据 / 分派 / 限期承诺
python3 run_cli.py evidence <issue_id> --kind log --ref ticket-42
python3 run_cli.py assign <issue_id> --owner 李工 --team 出行服务组
python3 run_cli.py commit <issue_id> --deadline 2026-10-20T18:00:00+08:00 --note "两周内修复"

# 结论（必须引用支撑反馈或反例）与审核解释
python3 run_cli.py conclude <issue_id> --decision confirmed --rationale "..." \
    --support <fb_id> --counterexample <fb_id>
python3 run_cli.py explain <issue_id>

# 政策版本
python3 run_cli.py policy-register P-2026.2 --note "十月修订"
python3 run_cli.py policy-apply <issue_id> P-2026.2   # 已结论事项会被拒绝

# 逾期升级（幂等；每次调用即一次“重启补扫”）
python3 run_cli.py sweep

# 某地区问题的当前状态与历史决定
python3 run_cli.py region 杭州

# 启动 HTTP 接口
python3 run_cli.py serve --port 8080
```

存储路径用 `--store` 指定（默认 `data/store.json`，可用环境变量 `FEEDBACK_STORE` 覆盖）。

## HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/feedback` | 提交反馈 |
| GET | `/issues?region=&status=` | 列出事项 |
| GET | `/issues/{id}` | 事项详情（含原始提交者与时间） |
| GET | `/issues/{id}/explanation` | 审核解释 |
| POST | `/issues/{id}/merge` `/evidence` `/assign` `/commit` `/conclude` `/policy` | 事项操作 |
| POST | `/policies` | 登记政策版本 |
| POST | `/sweep` | 推进逾期升级 |
| GET | `/regions/{region}/report` | 地区当前状态与历史决定 |

领域规则冲突返回 409，对象不存在返回 404。

## 代码结构

```
src/task_domain_006/
  core.py             # 初始契约（不可变记录、稳定摘要、冲突检测）
  feedback_models.py  # 反馈/事项/证据/承诺/结论/政策版本/事件模型
  similarity.py       # 中文文本归一化与相似度
  store.py            # JSON 原子持久化
  service.py          # 全部用例与领域规则
  api.py              # HTTP 接口（stdlib http.server）
  cli.py              # 命令行
tests/
  test_contract.py          # 初始契约测试
  test_feedback_backend.py  # 领域规则测试（归并/匿名/政策门禁/逾期升级/解释/报告/接口）
```
