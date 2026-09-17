# UI 素材工作流 V3

## 目标

把“框出一块图片然后选择抠图方式”升级为可审计的页面拆解流程。每个标注目标同时记录它是什么、怎样处理、在哪里、属于谁，以及处理前后的文件版本。

## 核心对象

### 元素类型 `elementType`

| 值 | 含义 | 默认处理 |
| --- | --- | --- |
| `image-asset` | 插画、图标、头像、商品等独立图片素材 | `local-transparent` 或 `ai-transparent` |
| `code-element` | 普通文字、按钮、卡片、分隔线和规则图形 | `none` |
| `complete-composite` | 文字、背景、徽标、按钮等不可拆分的完整位图 | `complete-crop` |

### 处理动作 `processingMode`

| 值 | 行为 | 是否调用图片 API |
| --- | --- | --- |
| `none` | 只记录为代码元素，不生成图片 | 否 |
| `complete-crop` | 按红框原样裁剪，保留全部像素 | 否 |
| `local-transparent` | 估算边缘主色，只移除与图片边缘连通的相近背景 | 否 |
| `ai-transparent` | 将绿色上下文裁剪和红框说明发送给图片编辑 API | 是 |
| `background-repair` | 将单个目标发送给图片编辑 API，移除遮挡并补齐完整背景 | 是 |

旧数据中的 `ai` 和 `background-script` 分别兼容映射为 `ai-transparent` 和 `local-transparent`。

## 人工审核闸门

AI 建议只产生候选标注，写入 `suggestion.source = "ai"` 和 `reviewStatus = "needs-review"`。用户在标注面板确认或修改后，目标才变为 `reviewStatus = "confirmed"`。未确认的 `image-asset` 或 `complete-composite` 不能参与原始裁剪或任何后续图片处理。处理中心执行前自动保存当前标注，并同时在浏览器和后端执行确认校验；未确认的代码元素不阻塞图片处理。

人工新画的目标默认来源为 `manual`，仍需在弹窗中确认类型与处理动作。

## 版本规则

任何图片处理都不得覆盖输入文件。资产清单包含：

```json
{
  "versions": [
    {"id": "original", "kind": "source-crop", "file": "originals/region-001-target-001.png"},
    {"id": "processed", "kind": "local-transparent", "file": "local/region-001-target-001.png"}
  ],
  "activeVersion": "processed"
}
```

审核页面必须能够同时查看原图与处理结果。重新处理会生成新的不可变版本 ID，不删除历史文件。

背景补全 API 可能返回与红框不同宽高比的画布。结果必须先按来源红框比例居中裁剪，再等比缩放到来源尺寸，禁止直接改变宽高造成角色或物体变形。处理历史记录 API 返回尺寸、最终尺寸和 `center-crop-no-stretch` 策略。

## 坐标与层级

每个目标和最终资产记录：

- `sourceRegion`：所属绿色上下文区域。
- `sourceTarget`：红框目标 ID。
- `sourceMarkedBox`：绿色区域的页面坐标。
- `pageBox`：红框在参考图中的页面坐标。
- `placement.bbox`：人工确认后的最终页面坐标。
- `name`：稳定的 kebab-case 语义名称。
- `parent`：父级语义名称，可为空。
- `zIndex`：页面层级整数。
- `processingMode`：实际处理动作。
- `processingHistory`：每次处理的时间、参数、输入和输出版本。

## 浏览器流程

1. 页面需求：根据项目设计规范生成图片提示词。
2. 参考图审核：人工批准当前页面参考图。
3. 区域标注：AI 给出候选，人工调整绿色上下文和红色目标，确认类型、动作、名称、父级和层级。
4. 处理中心：先生成原始裁剪；本地透明化、完整裁剪、AI 透明化和背景补全分别执行。
5. 素材审核：左右对比原图与处理结果，补齐元数据并逐项批准；代码元素在同一阶段审核但不产生 PNG。
6. 页面实现：Codex MCP 只接收已批准清单，并按类型决定使用位图或代码。
7. 对比校准：保留每轮渲染、重叠图、差异图和调整记录。

## 验收条件

- 三类元素在标注、处理、审核和 MCP 上下文中语义一致。
- AI 建议不会未经人工确认直接进入处理。
- 四种图片动作互相独立，代码元素不会误调用图片 API。
- 原始裁剪始终可访问，处理结果不覆盖原图。
- 审核结果包含名称、坐标、父级、层级、类型、动作和版本记录。
- 旧页面标注无需人工迁移即可继续打开和处理。
