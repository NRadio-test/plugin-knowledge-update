# NRadio 知识库自动同步插件

这个插件负责把 `NRadio-test/nradio-web-platform` 的 `main` 分支中已经审核发布的结构化知识，同步到指定的 AstrBot 知识库。它不会读取审核分支，也不会自动合并 Pull Request。

## 工作方式

插件每次读取 `knowledge-base/import/knowledge.jsonl`，将每一行知识转换成一个适合检索的独立文本块。同步时会先上传带 GitHub 文件版本号的新文档；只有新文档完成向量化以后，才删除插件自己创建的旧版本。它只会清理名称以 `NRadio-Knowledge-` 开头的文档，不会修改人工导入的其他资料。

## 安装与配置

1. 在 AstrBot 插件管理中通过仓库地址安装本插件。
2. 先在 AstrBot 的知识库页面创建目标知识库，并配置 Embedding 模型。知识库建立后不要随意更换 Embedding 模型或向量维度。
3. 打开插件配置，在“要接收 NRadio 内容的 AstrBot 知识库”中选择目标知识库。
4. 为私有仓库创建 Fine-grained GitHub Token，只授予 `NRadio-test/nradio-web-platform` 的 `Contents: Read` 权限。
5. 可以把 Token 填入插件配置；更推荐在 AstrBot 容器中设置 `NRADIO_GITHUB_TOKEN` 环境变量，这样 Token 不会写入插件配置文件。
6. 保存并重载插件。默认在启动 15 秒后同步一次，之后每 30 分钟检查一次；GitHub 文件版本未变化时不会重复向量化。

插件兼容 AstrBot 知识库选择器返回的名称或 UUID。更新插件后请完整重启一次 AstrBot 实例，使新增指令进入 Core 的指令注册表。

AstrBot 管理员可以在聊天中使用 `/ku-up` 立即同步，使用 `/ku-info` 查看最近检查结果、上次成功更新时间、知识条数和 GitHub 版本。旧指令 `/nradio_kb_sync` 与 `/nradio_kb_status` 继续作为兼容别名保留。这些指令受 AstrBot 管理员 SID 权限控制，普通用户无法执行。

## 权限边界

插件对 GitHub 只需要读取权限，对 AstrBot 只操作配置中选中的知识库。GitHub 审核、PR 合并和知识内容修改仍在 `NRadio-test/nradio-web-platform` 中完成，插件本身不会生成、审核或改写知识。
