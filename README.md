# NRadio 知识库自动同步插件

这个插件负责把 `NRadio-test/nradio-web-platform` 的 `main` 分支中已经审核发布的结构化知识，同步到指定的 AstrBot 知识库，并提供一个按 InfoID 管理单条知识的 AstrBot 插件页面。它不会读取审核分支，也不会自动合并 Pull Request。

## 工作方式

插件每次读取 `knowledge-base/import/knowledge.jsonl`，将每一行知识转换成一个适合检索的独立文本块。同步时会先上传带 GitHub 文件版本号的新文档；只有新文档完成向量化以后，才删除插件自己创建的旧版本。它只会清理名称以 `NRadio-Knowledge-` 开头的文档，不会修改人工导入的其他资料。

每一行 JSONL 的 `id` 都是稳定且唯一的 InfoID。网页内容检索、AstrBot 检索块和插件管理器使用同一个 InfoID，因此管理员可以准确定位某一条知识，而不是根据可能重复的标题猜测。

## 知识条目管理器

AstrBot 4.24.2 及以上会识别插件的 `pages/knowledge-manager/` 页面。更新插件并重载后，从插件详情或左侧“插件页面”进入“NRadio 知识条目管理器”，即可分页查看并搜索 InfoID、标题、正文、标签、来源、上传者、核验日期和可信度。

点击“编辑”会在新窗口打开受六位口令保护的 NRadio 网页编辑器。编辑器允许修改标题、正文、标签、来源和可信度，并保留 InfoID、原始上传者、原核验日期与修改审计记录；保存后的正式数据仍由 GitHub 工作流验证并更新，插件不需要 GitHub 写权限。

点击“删除”会把该 InfoID 移入 AstrBot 本地回收站，并立即重建插件管理的知识文档，使它不再参与 AstrBot 内容检索。这个操作不会改写 GitHub 正式 JSONL；后续自动同步也会继续排除该 InfoID，因此不会自动恢复。需要撤销时进入“回收站”点击“恢复”。如果同步目标暂时不可用，删除状态仍会保存，修复目标后点击“立即同步”即可收敛。

## 安装与配置

1. 在 AstrBot 插件管理中通过仓库地址安装本插件。
2. 先在 AstrBot 的知识库页面创建目标知识库，并配置 Embedding 模型。知识库建立后不要随意更换 Embedding 模型或向量维度。
3. 打开插件配置，在“要接收 NRadio 内容的 AstrBot 知识库”中选择目标知识库。
4. 设置“Embedding 单批文本数”：百炼 `qwen3.7-text-embedding` 填 `20`，百炼 `text-embedding-v4` 填 `10`，Google Gemini Embedding 填 `32`。这个值必须不超过模型单次允许的最大文本数量；Gemini 的批大小设置不会提高或绕过 Google 项目的免费额度。
5. 为私有仓库创建 Fine-grained GitHub Token，只授予 `NRadio-test/nradio-web-platform` 的 `Contents: Read` 权限。
6. 可以把 Token 填入插件配置；更推荐在 AstrBot 容器中设置 `NRADIO_GITHUB_TOKEN` 环境变量，这样 Token 不会写入插件配置文件。
7. 保存并重载插件。默认在启动 15 秒后同步一次，之后每 30 分钟检查一次；GitHub 文件版本未变化时不会重复向量化。
8. 打开插件详情中的“NRadio 知识条目管理器”，检查源条数、启用条数和目标知识库是否正确。

“知识条目网页编辑器地址”默认使用 `https://nradio.fallaxaura.dpdns.org/knowledge/manage/edit/`。只有域名迁移时才需要修改；它不保存六位口令，也不会扩大插件的 GitHub 权限。

插件兼容 AstrBot 知识库选择器返回的名称或 UUID。更新插件后请完整重启一次 AstrBot 实例，使新增指令进入 Core 的指令注册表。

AstrBot 管理员可以在聊天中使用 `/ku-up` 立即同步，使用 `/ku-info` 查看最近检查结果、上次成功更新时间、知识条数和 GitHub 版本。旧指令 `/nradio_kb_sync` 与 `/nradio_kb_status` 继续作为兼容别名保留。这些指令受 AstrBot 管理员 SID 权限控制，普通用户无法执行。

## 权限边界

插件对 GitHub 只需要读取权限，对 AstrBot 只操作配置中选中的知识库。管理器中的删除是 AstrBot 本地停用并可恢复；若要从网页和 GitHub 正式源中永久删除，仍应在 `NRadio-test/nradio-web-platform` 中提交并审核变更。插件本身不会生成、审核或改写 GitHub 知识。
