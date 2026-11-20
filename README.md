# 党建 AI 智能问答系统

面向学习读者、知识管理员和系统管理员。初始正式知识库收录共产党员网发布的《中国共产党章程》（2022 年 10 月 22 日通过，PDF 共 47 页）。读者可以提问、追问并按页打开所引版本。课程样稿保存在测试数据中，仅载入各轮独立测试库。

## Windows 启动

- **运行包**　完整解压到当前用户可写的文件夹，双击 `启动系统.cmd`。包内提供 64 位 Windows 所需的 Python、PostgreSQL 程序、应用依赖和党章原文。首次启动会初始化本机数据库并创建三个演示账号。保留解压后的目录可继续使用原有数据。
- **源代码**　准备 Windows x64、Python 3.11 或更新版本（本项目在 3.13 上验证），在项目根目录双击 `启动系统.cmd`。首次运行需要联网下载依赖、PostgreSQL 与党章原文。也可以使用已安装的 PostgreSQL 15 或更新版本，把二进制目录配置为环境变量 `PGBIN`。若 PowerShell 的本机策略限制脚本，可按下方命令手工启动。
- 浏览器打开 `http://127.0.0.1:8768`。首次启动后打开 `.local/演示账号.txt`，按角色登录。正式问答需要可用的 DeepSeek API 配额及出站 HTTPS。可在管理员的“模型配置”页录入密钥、测试并启用；已经使用脚本配置凭据的实例会在启动时测试并启用模型。
- 需要更换本机密钥时，在源代码根目录执行 `.venv\Scripts\python.exe -m scripts.credential`（运行包使用 `.runtime\python\python.exe`），按隐藏输入提示录入。初始化配置使用凭据名 `deepseek`。在界面新建的配置可通过“模型配置”新建版本并切换。
- `停止系统.cmd` 会关闭本安装目录的网页服务与任务进程。数据库继续保留学习记录。应用和数据库默认只监听回环地址。需要在同一台机器上并行演示另一份安装时，分别设置 `DANGJIAN_PORT` 和 `DANGJIAN_PGPORT` 后再首次启动。

若已有 PostgreSQL 二进制，在 PowerShell 中可直接运行以下命令。数据库与原文均由启动器初始化。

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m scripts.launch
```

## 系统组成

浏览器使用同源 HTML、CSS 和 JavaScript。FastAPI 提供 `/api/v1` 接口。SQLAlchemy 与 Alembic 管理 PostgreSQL 事务和数据迁移。独立任务进程解析原文并维护索引、备份与到期状态。

| 模块 | 主要行为 |
| --- | --- |
| 认证与会话 | Argon2 密码摘要、服务端会话、CSRF 校验。账号可有多个角色，访问按当前角色判断。 |
| 问答与引用 | 先过滤可见角色、发布版本、适用日期，再以中文分词和全文索引检索。DeepSeek 从候选证据中选句，服务端核验出处并保存引用快照。 |
| 知识维护 | 导入 PDF、DOCX 和 UTF-8 TXT（单文件上限 20 MiB）。后台解析、审核发布、撤回归档、分类、标签和反馈处理。 |
| 运行管理 | 模型测试与切换、安全规则及版本、运营查询、追加式审计、备份和恢复核对。 |

正式资料的来源、发布日期、下载地址与 SHA-256 见 `samples/constitution.json`。课程语料与预期答案见 `samples/course.json`。

## 运行维护

`.local` 保存原文、凭据、演示账号、备份、日志及测试证据。Windows 上模型凭据使用当前用户的 DPAPI 加密。Git 排除本机运行状态、虚拟环境及密钥文件。备份含账户数据、历史记录和加密凭据，应当只在本地受控保存。迁移至其他 Windows 用户时需重新配置模型凭据。

在源代码根目录用项目解释器执行以下维护命令。运行包将命令中的 Python 路径替换为 `.runtime\python\python.exe`。Windows 首次启动会把数据库工具复制到当前用户的本地应用数据目录。若 Windows 配置文件的路径包含非 ASCII 字符且初始化出现编码错误，可将 `DANGJIAN_PG_HOME` 设为当前用户可写的英文路径后重新启动。

```powershell
.venv\Scripts\python.exe -m scripts.maintenance backup
.venv\Scripts\python.exe -m scripts.maintenance restore ".local\backups\实际备份文件.zip"
.venv\Scripts\python.exe -m scripts.maintenance rebuild
```

恢复命令会创建独立的 `dq_restore_*` 数据库，核对数据库计数、当前版本、原文散列与索引，并打印恢复目录。保留最近 7 份备份，任务进程按北京时间日期检查每日备份。启动故障可依次查看 `.local/web.log` 和 `.local/worker.log`。

## 测试

先启动本机演示数据库。测试程序从其连接配置创建独立的 `dq_qa_*` 库。演示端的正式党章库不载入课程样稿。

```powershell
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.venv\Scripts\python.exe -m pytest -q tests
.venv\Scripts\python.exe -m scripts.quality
.venv\Scripts\python.exe -m scripts.browser_check
```

`pytest` 覆盖 TC01 至 TC43。50 题测评对可答与无依据场景使用真实检索及已配置的模型，对特定错误输出按测试设计注入受控候选。结果写入 `.local/verification`。浏览器检查使用本机安装的 Chrome 与 Edge 以及本项目的演示账号。模型调用可能产生服务商费用。
