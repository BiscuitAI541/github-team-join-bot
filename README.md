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
