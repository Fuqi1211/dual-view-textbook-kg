# 双视角教材知识图谱

本项目提供两类图谱的构建实现与统一调用接口：

1. **教材结构图**：复用发布指针指定的 v6 图谱；核验来源与哈希后原样导出。另封装原来的层级视图构建器，供已有物理段落图重新导出层级使用。
2. **概念语义图**：直接使用 `IncrementalEngine`，执行 G0、教材抽取、语义身份对齐、证据约束更新、版本提交与恢复。

两者通过 `corpus.json` 中证据的 `source_id`、`book_id`、层级路径和字符范围衔接。接口不会另造结构节点或替换已发布图谱。此包没有实现论文中另行规划的主张支持组和问答控制器。

## 先运行一个完整示例

需要 Python 3.10+。在仓库目录打开终端：

```powershell
python -m pip install -r requirements.txt
python -X utf8 -m dual_kg demo --output ./demo_output
python -X utf8 -m dual_kg status --output ./demo_output/concepts
python -X utf8 -m dual_kg process-export --output ./demo_output/concepts --destination ./demo_process
```

演示目录必须尚不存在。示例完全合成、固定模型返回、不访问网络，验证两类接口和原引擎的衔接。示例生成 2 个概念及带来源的关系，不能作为真实模型效果或论文实验结果。

也可以 `python -m pip install .`，随后用 `dual-kg` 替代 `python -m dual_kg`。

## 已有教材结构图在哪里

项目根目录的 `研究方案_20261002/24本文本图_当前版本.json` 是入口，当前指向：

```text
研究方案_20261002/24本文本图_版面归属修复v6_20261003/
  manifest.json
  层级数据清单.json
  <book_id>/text_graph.json
  <book_id>/hierarchy_graph.json
```

应以发布指针为准，部分旧交付说明仍描述 v2/v3。`hierarchy_graph.json` 是书—章—节—小节—段落层级；`text_graph.json` 保留物理来源及附属内容。接口读取发布版本，不改动历史产物。

```powershell
python -X utf8 -m dual_kg structure-export --project-root "原项目根目录" --book-id book_bef9d155e6d8 --output ./outputs/csapp_structure
python -X utf8 -m dual_kg concepts-prepare --project-root "原项目根目录" --book-id book_bef9d155e6d8 --output ./outputs/csapp_prepared
```

`--project-root` 是包含 `研究方案_20261002` 的目录。准备时不传 `--book-id` 会读取全部已发布教材。来源核验失败会报错；导出目录已存在会拒绝覆盖。准备接口可复用相同输入。

## Python 接口

```python
from dual_kg import export_structure, prepare_concepts, build_concepts, inspect_run, export_process

export_structure(project_root, "book_bef9d155e6d8", "outputs/structure")
bundle = prepare_concepts(project_root, "outputs/prepared", book_ids=["book_bef9d155e6d8"])
result = build_concepts("outputs/prepared/corpus.json", "outputs/concepts",
                        config="config/incremental.example.yaml", allow_api=True)
print(inspect_run("outputs/concepts"))
export_process("outputs/concepts", "outputs/process_copy")
```

`build_concepts` 的默认配置使用真实服务，必须显式 `allow_api=True` 并设置配置指定的密钥环境变量。运行前按需要修改服务地址、模型名和并发量。配置不内嵌密钥。示例默认 16 并发；完整教材调用量取决于数据，不在示例中自动执行。

已有物理段落图可调用原层级构建阶段：

```python
from dual_kg import build_structure
build_structure("existing/text_graph.json", "new_hierarchy",
                book_id="book_example", book_title="教材名称")
```

这一接口的输入是已有物理段落图的 `headings`、`nodes` 等字段，不能直接传 PDF 或 MinerU 原树。它只执行原 `build_hierarchy_view.build`；完整的历史 OCR 对齐、版面修复链见 `docs/构建过程与源码对应.md`。当前已有 v6 无需重跑。

## 命令行与 HTTP

```powershell
python -X utf8 -m dual_kg concepts-build --corpus ./outputs/prepared/corpus.json --output ./outputs/concepts --config ./config/incremental.example.yaml --allow-api --max-chunks 2
python -X utf8 -m dual_kg concepts-build --corpus ./outputs/prepared/corpus.json --output ./outputs/concepts --config ./config/incremental.example.yaml --allow-api
python -X utf8 -m dual_kg serve --port 8765
```

第二条使用相同目录继续，已完成片段复用。`--max-chunks` 是本轮选择前 N 个片段的上限，不是新增 N 个；省略后执行全部，并完成先验核查及收尾。

服务只监听 `127.0.0.1`，接口清单、请求示例和返回状态见 [接口说明](接口说明.md)。构建请求同步执行；另一请求可轮询状态。长时间任务优先使用 Python/CLI，HTTP 客户端应设足够超时。不要将本机文件接口直接暴露到公网。

## 中间处理过程

| 文件 | 内容 |
|---|---|
| `corpus.json` | 原文证据、字符范围、教材路径及切片 |
| `g0.json` | 未使用教材证据的初始先验 |
| `analysis/*.json` | 每片段的分析计划和图版本 |
| `decisions/*.json` | 实际提交的变更、身份与合并记录 |
| `graph.sqlite` | 工作图、断言、来源、提交及修订历史 |
| `runtime/model_cache.sqlite` | 模型返回、失败尝试、嵌入缓存 |
| `run_state.json` | 进度、失败片段计数、数据库状态 |
| `run_manifest.json` | 输入、模型和代码的恢复指纹 |
| `graph.json` | 构建完成或本轮结束时导出的工作图 |

`process-export` 会复制分析与决策 JSON，并将模型尝试记录转换成 `model_attempts.jsonl`，无需打开数据库即可阅读。合成演示使用注入的离线 runtime，没有真实模型缓存，模型尝试导出为空属于预期。

`partial`、`complete_with_skips`、`failed` 应分别处理，只有 `complete` 表示该输入范围的自动构建结束；所有状态都不代表人工验收。无 Neo4j 投影时 `database_written=false`。Python 可传入原 `Neo4jProjection`；需额外安装 `pip install .[neo4j]`，使用后由调用者关闭投影。CLI/HTTP 默认只写本地工作图。

## 源码与检查

- `dual_kg/`：新增公共接口、命令行、本机 HTTP 服务。
- `domain_ckg_workspace/ckg_incremental/`：当前原增量引擎，保持文件内容不变。
- `domain_ckg_workspace/ckg_build/`：原发布来源适配器及其同包依赖，保留以满足原导入结构。
- `legacy_text_graph_tools/`：历史结构图构建、修复、发布、验收及测试脚本；有些脚本需要原始教材与中间版本。
- `prompts/incremental_v5/`：真实阶段提示词。
- `config/`：原配置快照及领域模式。
- `SOURCE_MANIFEST.json`：核心源码和资源的逐文件 SHA-256。
- `tests/`、`upstream_tests/`：接口测试及选取的原引擎回归测试。

```powershell
python -X utf8 -m unittest discover -s tests
python -X utf8 -m unittest discover -s upstream_tests
```

验证范围与复验命令见 [验证说明](VALIDATION.md)。代码包排除整本教材、运行数据库、模型缓存、真实凭据和服务器运维脚本。

## 开源许可证

本项目以 [MIT License](../LICENSE) 开源。仅随代码分发合成示例，不包含真实教材、私有模型缓存或生产数据库。
