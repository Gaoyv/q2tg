# EFB QQ NapCat Channel

这是一个直接连接 [NapCatQQ](https://github.com/NapNeko/NapCatQQ) OneBot 11 WebSocket 服务的
EH Forwarder Bot 从端，用来在 Telegram 与 QQ 之间双向转发消息。它替代旧的 CoolQ、go-cqhttp、
`aiocqhttp` 和 `efb-qq-plugin-go-cqhttp` 链路。

## 兼容性设计

- EFB 模块 ID 保持为 `milkice.qq`。
- 私聊 ID 保持为 `private_<QQ号>`，群聊 ID 保持为 `group_<群号>`。
- 消息 ID 保持为 `<会话数字ID>_<OneBot消息ID>[_附件序号]`。
- 因此旧 `efb-telegram-master` 数据库中的 `chatassoc`、`msglog` 和 `slavechatinfo` 可以直接恢复。
- 旧 `GoCQHttp` YAML 配置可被读取，但正式部署会生成新的 `NapCat` 配置段和随机 OneBot token。

## 已实现功能

- QQ → Telegram：私聊/群聊文本、回复、@、图片、语音、视频、文件、QQ 表情、合并转发摘要、JSON 卡片摘要、撤回通知。
- Telegram → QQ：文本、原生回复与群 @、图片、贴纸、GIF、语音、视频、文件、撤回/编辑后重发。
- 好友和群目录、QQ 头像、好友新增/群成员变化通知。
- 带 token 的 OneBot WebSocket、请求/响应关联、连接超时与自动重连。
- 媒体大小限制、下载超时、容器网络隔离；OneBot 3001 端口不发布到公网。

## 本地测试

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[test]" "efb-telegram-master==2.3.1"
.\.venv\Scripts\python.exe -m pytest -q
```

生成一致性的旧数据库恢复副本：

```powershell
.\.venv\Scripts\python.exe scripts\prepare_deployment.py
.\.venv\Scripts\python.exe scripts\validate_profile.py
```

`prepare_deployment.py` 使用 SQLite online backup API 合并原数据库与 WAL，原始 `default` 目录不会被修改。
运行时 profile、Telegram token、OneBot token、WebUI token 和 QQ 登录态都被 `.gitignore` 排除。

## Docker 部署

部署目录为 `/opt/q2tg`：

```bash
cd /opt/q2tg/deploy
docker compose config --quiet
docker compose build efb
docker compose up -d
docker compose ps
```

NapCat WebUI 只绑定 VPS 的 `127.0.0.1:6099`。在本机建立隧道后访问
`http://127.0.0.1:6099/webui`：

```powershell
ssh -i .\work\q2tg_deploy_ed25519 -L 6099:127.0.0.1:6099 root@<VPS_IP>
```

在 WebUI 中完成 QQ 扫码登录即可。OneBot WebSocket 配置已自动放置在
`deploy/napcat/config/onebot11.json`，不需要把 3001 端口暴露到公网。

## 运维

```bash
cd /opt/q2tg/deploy
docker compose ps
docker compose logs --tail=200 efb
docker compose logs --tail=200 napcat
docker compose restart efb
```

数据库持久化于 `/opt/q2tg/deploy/profile/blueset.telegram/tgdata.db`；QQ 登录态持久化于
`/opt/q2tg/deploy/ntqq`。升级前应停止服务并备份整个 `deploy/profile` 与 `deploy/ntqq`。
