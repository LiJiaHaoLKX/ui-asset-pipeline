# 项目架构

## 模块职责

1. `app.py`：仅监听 localhost 的浏览器后端，提供多页面目录、固定的流水线 API、后台任务状态和受限文件预览。
2. `web/`：完整操作工作台，覆盖配置、规范、参考图、标注、裁剪、提取、审核、源码与对比。
3. `gpt_image_api.py`：适配外部 gpt-image API。页面生成走 `/images/generations`；素材提取按官方 multipart 格式走 `/images/edits`
4. `region-marker.html`：保留的独立标注工具。完整项目使用 `web/` 内置的两级标注器，并直接保存坐标。
5. `crop_regions.py`：按照人工坐标和统一安全边距进行确定性裁剪。
6. `extract_uniform_background.py`：执行完整裁剪和本地透明化，采用 RGB 色差阈值并只删除与边缘连通的背景，同时建立原图与处理版本。
7. `extract_regions.py`：只处理已人工确认的 AI 透明化目标，按裁剪图片数量自动并发调用图片编辑 API。
8. `repair_background_targets.py`：只处理背景补全目标，调用图片编辑 API 并保留非透明结果及原始裁剪。
9. `split_transparent_objects.py`：按照 Alpha 连通区域拆分未知数量的对象。
10. `split_extractions.py`：批量拆分并合并本地、AI、完整裁剪、背景补全和代码元素清单。
11. `compare_images.py`：生成 `overlay.png`、`diff.png`、遮罩和数值指标，供下一轮视觉校准。
12. `pipeline_tasks.py`：以 SQLite 持久化浏览器创建的 Codex 任务、领取状态、事件记录和源码版本，并生成每页冻结的任务上下文。
13. `mcp_server.py`：stdio MCP Server。人工新建的 Codex 会话通过它领取第 6、7 阶段任务、读取上下文、写回源码并创建对比迭代。

## 多页面存储

`workspace/project.json` 是项目级页面目录，只保存页面 ID、名称、时间和当前页面 ID。`workspace/design-spec.json` 是所有页面共用的设计规范，`workspace/settings.json` 保存共用的画布尺寸、质量、裁剪边距和差异阈值。旧游戏首页注册为 `game-home` 和 `legacy` 存储，继续直接使用项目根目录，不迁移历史文件。新页面全部写入独立目录：

```text
pages/<page-id>/
  state.json
  prompts/reference-page.txt
  prompts/reference-page.notes.txt
  reference/
  regions/
  assets/
  src/
  artifacts/iterations/
  runs/gpt-text/
  runs/gpt-image/
```

每个 HTTP 请求在开始时读取当前页面；每个后台任务在入队时捕获页面 ID，并在线程本地上下文中执行。用户在任务执行期间切换页面，不会改变任务的输入、输出目录或阶段状态。

设计规范不是页面数据。浏览器左下角的“项目设置”统一管理项目设计规范、图片与文本模型配置以及公共处理参数。页面工作流中的“页面需求”只保存本页结构、文案、内容要求和生成后的图片提示词。

## 浏览器后端边界

- 服务固定绑定 `127.0.0.1`，并校验 Host 和写请求 Origin。
- `/api/state` 只返回 `apiKeyConfigured`，永不返回密钥正文。
- `POST /api/pages` 创建独立页面，`POST /api/pages/select` 切换页面，`POST /api/pages/rename` 只修改显示名称。
- `POST /api/prompt/generate` 根据项目规范和当前页需求调用文本模型，结果写入当前页提示词文件。
- 浏览器不能提交任意命令或任意磁盘路径；子进程参数由后端固定组装。
- 预览文件只允许访问项目内的参考图、区域、素材、产物、源码、提示词和运行记录目录。
- 源码写入只允许 `src/index.html`、`src/styles.css` 和 `src/app.js`。
- 浏览器和 MCP 写源码都使用内容 SHA 版本；Codex 执行期间禁止浏览器覆盖源码。
- 参考图生成和素材提取在浏览器中执行前必须再次确认费用。
- 后台长任务使用内存 Job 状态，最终结果和阶段状态持久化到 `workspace/state.json`。
- 旧页状态保存在 `workspace/state.json`；新页状态保存在各自的 `pages/<page-id>/state.json`。

## 数据原则

- 参考图坐标是唯一权威坐标系。
- 人工框是 API 上下文范围，不是最终素材边界。
- 图片 API 输出可能缩放或居中，所以输出图中的坐标不能直接映射回页面。
- Codex 根据参考图、来源人工框和拆分素材确定名称、父级、层级及最终位置。
- 每次 API 调用和页面对比都建立不可变目录，失败只重试对应任务。
- API 密钥只放在 `.env`，不写进配置、日志或代码。
- AI 识别只是建议，`needs-review` 目标必须经过人工确认才进入任一处理队列。
- 图片素材、代码元素、完整复合图片是业务分类；透明化、完整裁剪和背景补全是独立动作，不混用概念。
- 原始裁剪和每次处理结果都是不可变版本；审核只改变活动版本与元数据，不覆盖处理来源。

## Codex MCP 任务交换

浏览器不启动或唤醒 Codex。用户在第 6 或第 7 阶段创建持久化任务，复制任务指令，再手动新建 Codex 会话。新会话主动调用 `ui_asset_pipeline` MCP 并领取指定任务 ID。

```text
浏览器 -> app.py -> workspace/tasks.db
                         ^
                         |
Codex 会话 -> mcp_server.py
```

任务绑定固定 `pageId`，不读取浏览器随后切换到的活动页面。同一页面只能存在一个未结束写任务。状态为：

```text
queued -> running -> awaiting-human-approval -> completed
                  \-> failed
queued/running/awaiting-human-approval -> cancelled
```

任务上下文保存在当前页面的 `artifacts/codex/tasks/<task-id>/context.json`，其中包含设计规范快照、页面需求、参考图路径、审核素材、源码版本和验收条件，不包含 `.env` 或密钥。SQLite 中保存任务状态和事件，图片及源码继续使用现有页面目录。

## 状态流转

```text
draft-reference
  -> reference-approved
  -> regions-marked
  -> crops-created
  -> extraction-running
  -> extraction-review
  -> assets-approved
  -> page-generated
  -> comparison-ready
  -> code-adjusted
  -> comparison-ready ...
  -> completed
```

## 图片编辑请求契约

- URL：`http://154.12.91.166:3000/v1/images/edits`
- 请求：`multipart/form-data`
- 图片字段：`image[]`
- 透明输出：`background=transparent` 与 `output_format=png`
- 返回：优先读取 `data[0].b64_json`，同时兼容网关返回 URL 或原始图片响应
- `gpt-image-2` 不发送 `input_fidelity`
- 完整 UI 参考图生成固定为并发 1
- 素材提取默认并发数等于已批准裁剪图片数量：切割出 N 张图，就同时启动 N 个提取任务
- 必要时仍可通过 `--workers N` 人工限制并发

## 文本提示词请求契约

- 默认 URL：`http://154.12.91.166:3000/v1/chat/completions`
- 请求：OpenAI 兼容 JSON，包含 `model`、`messages`、`temperature` 和 `max_tokens`
- 输入：当前页面名称、项目级完整设计规范 JSON 和当前页需求
- 输出：只接受非空的最终图片提示词，写入当前页 `prompts/reference-page.txt`
- 记录：请求正文、响应正文和非敏感响应元数据写入当前页 `runs/gpt-text/`
- 密钥：只从 `.env` 读取，不进入请求记录、状态响应或浏览器回显
