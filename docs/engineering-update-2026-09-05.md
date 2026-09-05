# 2026-09-05 工程更新

本次同步五批已验收的工程改进及对应审查修复。公开版保留既有脱敏内容、默认直连设置和上游署名；不包含私人对话、生产数据库、凭据、原始研究记录或离线安装包。

## 改了什么

- 固定 Python 3.11.15 容器基础镜像、66 项运行依赖和 4 项额外测试依赖。新增源码快照、文件散列校验、数据库备份、新目录恢复和隔离测试入口。
- 图片续聊与重新生成能够携带窗口内的原图，覆盖两种既有图片协议。补充记忆检索改用全库缓存，较旧的相关记录不再被近期候选上限排除。
- 聊天阶段耗时、主轮结果和后台任务关联同一轮次标识；退出统一收尾。新增 `/healthz`，直接检查初始化状态和数据库可读性。
- 将基础动作处理从集中的聊天代码中拆出。认识更新同时接收对应原话与来源标识，不把主模型转述当作原文；人格、关系判断和互动原则不变。
- 增加独立视觉摘要槽、图片观察检索与 `memory.view_image` 按需重看，默认关闭。

审查修复包括：按数据库副本引用补齐附件；向量回写拒绝已编辑正文的旧结果；关闭图片功能时取消重试；摘要每次重试前重新检查来源；重看补充不抢占主轮结果；默认部署冒烟直接检查 `/healthz`。

数据库新增 `image_observations` 旁表及索引，不改写原消息。前四批不新增模型调用；第五批关闭时不会生成图片描述、召回图片观察或执行按需重看。普通聊天携带近期原图的能力与此开关独立。

## 安装与检查

依赖锁按 Linux amd64、Python 3.11 验收。其他平台请使用容器；不要为绕过平台或文件散列不匹配而直接取消校验。

在仓库根目录和对应虚拟环境中执行：

```sh
python -m pip install --only-binary=:all: -r aion-chat/requirements-dev.txt
python scripts/check_backend.py
```

本次公开版核心检查为 250 项通过、1 项跳过；跳过的只是未公开原始研究记录的来源冻结核对，其余断言保留。README 原有七组契约检查另有 93 项通过，包括默认直连和运行时关系称谓。检查使用临时数据库、合成图片与模拟接口，不调用真实模型，也不证明视觉摘要账号可用或描述质量。

需要离线安装或使用热更新脚本时，先在上述平台下载锁定安装包：

```sh
python -m pip download --only-binary=:all: --require-hashes \
  -r aion-chat/requirements-test.lock -d aion-chat/wheels
python -m pip install --no-index --find-links=aion-chat/wheels \
  -r aion-chat/requirements-dev.txt
```

`scripts/lock_backend.py --check` 用于核对已安装环境、安装包和锁定文件，不会自动升级依赖。

## 备份与恢复

以下命令均在仓库根目录执行，输出目录必须尚不存在。备份包含私人运行数据和指定的环境配置，应保存在受控位置，不上传公开仓库。

```sh
python scripts/engineering_baseline.py snapshot \
  --output .codex-backups/source-20260905
python scripts/engineering_baseline.py backup-data \
  --data-dir aion-chat/data --env-file .env \
  --output .codex-backups/data-20260905
python scripts/engineering_baseline.py verify .codex-backups/data-20260905
python scripts/engineering_baseline.py restore .codex-backups/data-20260905 \
  --target .codex-backups/restore-check-20260905 --check-startup
```

备份先冻结各数据库，再按副本中的消息引用补齐附件；缺失或损坏会使备份、校验或恢复失败。各数据库分别取得一致副本；如果需要跨数据库的同一业务时点，先停止写入。SQLite 日志模式可能需要访问辅助文件，遇到只读挂载导致超时应调整备份挂载权限或停写，不要将未完成目录当作恢复点。

恢复只写入新目录，不切换现有服务。环境文件恢复到目标的 `env/` 下，数据在 `data/` 下；确认校验和启动检查通过后，再单独决定是否切换。

## 部署与回退

普通自托管入口仍是 `aion-chat/docker-compose.yml`。`deploy/docker-compose.prod.yml` 是绑定本机地址的 Linux 服务器模板，使用前检查环境文件、数据目录权限和反向代理。

`scripts/deploy_server.sh` 用于已有容器的热更新。先配置 `SERVER_USER`、`SERVER_HOST`、`SERVER_PATH`，准备本地虚拟环境和离线安装包；服务器默认地址是占位符，不会指向维护者的实例。通过 `bash scripts/deploy_server.sh` 预览，明确加 `--apply` 才会修改服务器。

热更新前保存实际容器的恢复镜像；源码来自冻结快照，检查通过后才保存发布记录。脚本不会自动备份挂载的数据目录，应先执行上一节的数据备份。冒烟要求 `/healthz` 成功；旧容器的持续健康探针只有在应用新版容器配置重建时才会更新。

代码回退使用当次 `recovery-<发布编号>` 镜像及原有挂载、环境配置。恢复镜像不包含挂载数据库；若确需数据回退，先停写、保存当前数据，再使用已校验备份恢复到新目录后切换。新增图片旁表可以保留，旧代码不依赖它，无需为了回退主动删表。公开提交可用 `git revert` 撤销，不重写已有历史。

## 图片记忆保持默认关闭

在设置页为 `vision_summary` 选择支持图片输入的 `openai` 兼容端点和型号。端点独立于主聊天模型，不要求走某家官网；国内平台或兼容网关均可按实际接口能力配置。代码预填型号不代表账号已有额度，也不承诺任何渠道永久免费。

摘要只接收单张图片和描述指令，不发送完整聊天或人格。启用后只处理新图片；历史记录通过指定来源消息编号手动补做，不自动扫描全库。每张图通常增加一次摘要和一次向量调用；摘要最多尝试两次，向量化失败保留已成功的描述。重看每轮最多追加一次当轮主模型调用，主模型必须支持图片。

只停止生成时关闭摘要槽；完整停用时关闭图片记忆。两种停用都会取消已登记的描述与向量任务，已经发出的请求无法收回。完整停用还会停止图片观察注入和按需重看，原消息、原图与已有描述保留。
