# TeyvatNotes

原神攻略问答工具，支持检索资料、核对引用和保存多轮会话。

- **提瓦特手记桌面版**：在 Windows 窗口中提问、补充玩家条件、核对引用和保存模型配置。
- **攻略 CLI**：导入自备资料、检索证据并生成带引用的回答，也可运行离线演示。

## 快速开始

安装 Python 3.12+ 和 uv，在仓库根目录运行：

```powershell
uv sync --locked
```

然后任选一个入口试用。以下命令以 PowerShell 为例。

### 试用知识问答 CLI

运行离线演示：

```powershell
uv run python -m teyvatnotes.knowledge.answer_cli samples/questions/character.json --demo
```

命令会输出一份含回答和引用的 JSON，并标记 `demo_only: true`。示例使用虚构资料，只用于体验流程。

### 使用 Windows 桌面版

运行 `dist/desktop/TeyvatNotes-Setup.exe`，安装后从开始菜单打开「提瓦特手记」。默认进入本地攻略，不需要另装 Python、Qt 或 PostgreSQL。入门资料覆盖薇斯纳 7.1 的一组配队建议，保留原攻略链接；其他角色或版本没有证据时会提示资料不足。右上角可主动切换到独立的虚构演示。

在「设置」填写 HTTPS Base URL、模型型号和 API Key，点「保存到 .env」，即可开始本地资料的联网模型问答。模型须支持 Responses 工具调用，实际调用可能计费。安装时在 `%LOCALAPPDATA%\TeyvatNotes\.env` 创建空白模型模板，已存在则保留；也可选择已有配置文件，不需要填写数据库连接或 UUID。

攻略库随安装包只读交付。会话和自动生成的用户标识保存在 `%LOCALAPPDATA%\TeyvatNotes\conversations.sqlite3`，升级或卸载不会移除这些用户文件。桌面版不再连接旧 PostgreSQL，会话不会自动导入；正式攻略 CLI 使用 PostgreSQL。详见[桌面本地数据说明](docs/desktop.md)。

开发时在仓库根目录运行：

```powershell
uv sync --locked --group desktop
uv run --no-sync python -m teyvatnotes.desktop
```

在 Windows x64 上构建安装包时，安装 [Inno Setup](https://jrsoftware.org/isdl.php)，然后运行：

```powershell
uv sync --locked --group desktop-build
.\scripts\build_desktop.ps1 -InnoCompiler "<ISCC.exe 的绝对路径>"
```

## 下一步

- **使用自己的资料和模型**：[资料入库](docs/guide-ingestion.md) · [模型配置与单轮问答](docs/model-answering.md) · [多轮会话](docs/conversations.md)
- **了解实现与验证**：[检索方式](docs/rag-retrieval.md) · [评测说明](docs/evaluation.md)
