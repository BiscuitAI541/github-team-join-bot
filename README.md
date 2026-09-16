# github-team-join-bot

自动化「申请加入 Team A」流程：用户在本仓库提 Issue 填写订单号，
GitHub Actions 会校验飞书多维表格中「订单号 + GitHub昵称」是否匹配，
匹配成功后自动把发起 Issue 的用户加入组织的 `A` team，并自动评论、关闭 Issue。

## 目录结构

```
.github/
  ISSUE_TEMPLATE/
    join-team-a.yml     # 申请表单，用户只需填订单号
  workflows/
    join-team-a.yml     # 校验 + 自动加人的 workflow
```
## 使用方式

1. 用户在本仓库新建 Issue，选择「申请加入 Team A」表单，填写订单号提交
2. Actions 自动触发：
   - 解析 Issue 表单里的订单号
   - 用 Issue 发起人的 GitHub 用户名 + 订单号去查询飞书多维表格
   - 命中记录 -> 调用 GitHub API 把该用户加入 `A` team，评论通过并关闭 Issue
   - 未命中 -> 评论拒绝原因并关闭 Issue
