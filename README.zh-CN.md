# Provendoc

[English](README.md) | [简体中文](README.zh-CN.md)

杂乱文档进，检索就绪的分块出——并附带完整收据。

把它指向真实世界的文件（PDF、Word、PowerPoint、表格、代码、日志、
配置、电子书），拿回适配 RAG 的语义分块，并且确切知道每个分块
来自哪里、管线在哪一步不确定。

为什么这很重要：大多数摄取工具会悄悄损坏文档——表格在行中间被
切断、标题被吞掉、扫描页退化成垃圾分块。Provendoc 要么正确解析，
要么带着可审计的诊断信息响亮失败；每一处净化动作都被披露，每个
分块都携带出处（源偏移、解析器、事实码），检索阶段的错误可以一直
追溯到源头。

每个文件由唯一的领域注册表
（`src/provendoc/core/domain/domain_registry.py`）路由到五个摄取
领域之一：`narrative`（叙事）、`structured_config`（结构化配置）、
`spreadsheet`（表格）、`code`（代码）和 `record_stream`（记录流）。
每个领域定义自己的解析、表征、评估与诊断行为。

## 30 秒上手

```python
from provendoc import Provendoc

res = Provendoc().chunk("anything.pdf")   # pdf / docx / xlsx / py / log / yaml ...

for text in res.texts:        # 只要文本 -> list[str]
    print(text)

for unit in res.chunks:       # 偏移与出处 -> 恒为 list
    print(unit.text[:40], unit.metadata["source_coordinates"])
```

三句话说清：

1. `chunk()` 返回语义分块，`parse()` 返回整篇文档，`evaluate()`
   额外返回质量反馈，`auto_pipeline()` 额外自动调参。
2. `res.chunks` 恒为 list——对全部五个领域、全部 142 个受支持后缀
   一视同仁。
3. 缺少可选后端时诚实降级（可审计的兜底），绝不崩溃。

诚实降级不是口号，是可复现的行为——在 core-only 安装（未装
`provendoc[pdf]`）下运行：

```python
from provendoc import Provendoc

res = Provendoc().parse("report.pdf")
print(res.document.metadata["parse_failed"])        # True
print(res.document.metadata["missing_backend"])
# {'missing_module': 'fitz', 'pip_extra': 'pdf',
#  'install_hint': 'pip install "provendoc[pdf]" (or provendoc[all])'}
```

没有异常、没有垃圾分块——"为什么没解析出内容"作为文档事实被
记录下来，并附上确切的修复方法。对*谎报自身格式*的文件（例如
伪造的 `.pdf`），格式校验闸门会拦截它，并把"声明格式 vs 实际内容"
的证据一并返回。

## 安装

Provendoc 尚未发布到 PyPI；从源码安装：

```bash
git clone <this-repo> provendoc && cd provendoc
pip install -e .                  # 核心：txt / md / 配置 / 代码 / 日志，无重型依赖
pip install -e ".[pdf,docx]"      # 只加装你需要的后端
pip install -e ".[all]"           # 全部可选后端
```

可选依赖按需安装：

| Extra | 增加的能力 |
| --- | --- |
| `pdf` | PDF 解析（PyMuPDF；AGPL——见 [License](#license)） |
| `pdf-layout` | MinerU 版面分析，处理复杂 PDF |
| `docx` / `doc` | OOXML / 旧版二进制 Word |
| `spreadsheet` | pandas 引擎 + `.xlsb` / `.ods` 后端 |
| `parquet` | `.parquet`（pyarrow） |
| `code` | tree-sitter AST 代码分块 |
| `api` | FastAPI REST 服务（`provendoc-serve`） |
| `image-ocr` | 扫描页 OCR（需本地 Tesseract 数据） |
| `langchain` / `llama-index` | 检索框架适配器 |

缺少可选后端时诚实降级（可审计的兜底）而非崩溃——见上面的示例。

## 快速上手（SDK）

```python
from provendoc import Provendoc

ad = Provendoc()

result = ad.parse("file.pdf")                       # 纯解析
result = ad.chunk("file.md")                        # 语义分块

# res.chunks 恒为 List[RepresentationUnit]，无论来自五个领域中的哪一个。
for unit in result.chunks:
    print(unit.unit_type, unit.metadata.get("char_start"), unit.metadata.get("char_end"))

# 被合并或改写过的单元不携带 char_start/char_end；只保留坐标层，
# 并可能附带一条说明，解释为什么逐字定位不可行。
if result.chunks:
    coords = result.chunks[0].metadata["source_coordinates"]
    print(coords["coordinate_system"], coords["char_span"])

# 粗/细粒度层：显式选层；恒为 list，缺失时为 []。
coarse = result.chunks_at("coarse")
fine = result.chunks_at("fine")

# 零依赖输出
texts = ad.chunk_texts("file.md")                   # ["分块1", "分块2", ...]
items = ad.chunk_dicts("file.md")                   # [{"text": ..., "metadata": {...}}, ...]

# 质量反馈（RSE 三层信封）
result = ad.evaluate("file.txt", chunk_size=200, overlap=50)
score = result.metadata["benchmark"]["rse"]["quality_profile"]["quality_score"]

# 评估 + 调参
result = ad.auto_pipeline("file.txt")

# 代码领域：跨文件语义（可选启用，只读）
result = ad.chunk("main.py", project_root="./src")  # 分块获得 project_references
index = ad.index_project("./src")                   # 全项目跨文件索引
```

在 `benchmark["evaluation"]` 字典中读 `invariant_composite`；
旧的六维 `overall` 已退役，恒为 0.0。

## 支持的格式

- narrative（叙事）— txt, md, markdown, mdx, pdf, docx, doc, docm,
  dot, dotx, dotm, rtf, tex, rst, qmd, rmd, epub, pptx, pptm, ppsx,
  ppsm, potx, potm, ppt, pps, pot, odp, fodp, otp, sti, png, jpg,
  jpeg, webp, bmp, gif, tif, tiff
- structured_config（结构化配置）— json, yaml, yml, toml, ini, cnf,
  conf, cfg, xml, properties, env, envrc, plist, editorconfig /
  gitconfig / npmrc 家族, tf, tfvars, tf.json, tftest.hcl, cargo.lock
- spreadsheet（表格）— csv, tsv, psv, xlsx, xls, xlsm, xlsb, ods,
  parquet（需 parquet extra）
- code（代码）— py, js, ts, jsx, tsx, vue, html, css, java, go, php,
  c/cpp, cs, rs, lua, dart, r, perl, zig, less, hcl, bash/powershell,
  sql, proto, swift/kotlin/scala/ruby, cmake/makefile, ...
  （共 48 个后缀；单一事实源：`core/domain/domain_registry.py`）
- record_stream（记录流）— log, jsonl, ndjson

注意：`.html/.htm` 归属代码领域（容错解析 + AST/组件分块），不走
叙事管线。未知后缀回落到容错文本解析器，返回披露性的
`no_magic_expected` 判定而非直接报错；声明后缀与实际内容相矛盾的
文件（如伪造的 `.docx`）会诚实失败。

压缩包（zip / tar / 7z 家族的压缩成员）在采集阶段展开：每个成员
走各自领域的管线，出处保留虚拟路径，并有嵌套深度 / 成员数 / 体积
护栏和可审计的展开事实。

## 为什么选 Provendoc

- **证据优先。** 源文本原样保留；对工作文本的每一步净化动作都在
  登记簿加警告披露；每个分块携带出处（源偏移、解析器、事实码）。
- **诚实失败。** 不支持或损坏的文件带着可审计诊断响亮失败，绝不
  悄悄退化成垃圾分块。
- **领域感知分块。** 五个摄取领域各有语义表征——叙事散文、配置
  键值树、表格、代码（tree-sitter AST）、记录流（日志格式家族
  识别：CLF / W3C / syslog / logfmt；JSONL 信封指纹）。
- **不可见注入字符普查。** 解析层运行不可见字符普查（零宽字符、
  bidi 控制符、ANSI 转义、C0 控制符、tag 字符、异体选择符），
  纯观测。该层只检测并披露，不拦截也不清除；对被标记的字符如何
  处置是下游的策略选择。

数字一览：**142 个文件后缀**、5 大领域 · MIT · Python 3.10+ ·
解析期零网络调用。

## CLI 与 REST API

包安装两个控制台入口：

```bash
provendoc path/to/dataset --mode benchmark   # 批处理 CLI
provendoc-serve                               # REST API 服务（pip install "provendoc[api]"）
```

CLI 会对整个目录逐文件走各领域管线，并把本次运行的产物写入
`runs/{run_id}/`——`manifest.json`、每文件的表征/评估产物，以及
`reports/index.html` 总览。只有全部文件处理完退出码才是 `0`；
诚实失败文档（格式 mismatch、缺后端）计入已处理，其事实保留在
产物中。运行模式（`--list-modes`）在速度与诊断信息之间权衡：
`benchmark`、`development`、`batch`、`analysis`、`debug`。

REST 示例（curl）：

```bash
curl -s http://127.0.0.1:8000/health
curl -s -F file=@report.md http://127.0.0.1:8000/api/v1/chunk | jq '.metadata.chunk_count'
```

端点：`/api/v1/parse`、`/api/v1/chunk`、`/api/v1/evaluate`、
`/api/v1/auto-pipeline`、`/api/v1/domains`，外加异步的
`/api/v1/batch` 任务接口。OpenAPI 文档在 `/docs`。

随包附带注释齐全的配置模板 `provendoc.toml.example`。REST 应用
也以 `provendoc.api.app`（FastAPI 工厂）形式提供，可用 uvicorn
直接嵌入。REST 服务支持可选的 `X-API-Key` 认证（经
`PROVENDOC_API_KEY` 配置）。

## 版本与稳定性

Provendoc 处于 0.x：公开 SDK 面（`parse` / `chunk` / `evaluate` /
`auto_pipeline`、导出函数、`/api/v1/` 下的 REST 端点）在次版本间
仍可能演进。带版本的契约以**新增版本化键**承载破坏性变更，不做
就地改写；未版本化面（配置字段名、报告布局、内部键）可能在次版本
间变化。生产环境请固定精确版本，升级时查阅 `CHANGELOG.md`。

## 关于本发布

这是最初的**纯代码**发布（v0.1.0）：解析内核、五领域表征层、
SDK、CLI 与 REST 应用。测试套件、基准语料、预注册评估套件，以及
部分 docstring 引用的设计规格文档（`specs/*.md`）维护在开发树中，
计划在后续版本发布。相应地，本仓库中少数注释与工具配置段
（如 `pyproject.toml`）仍会提及 `tests/`、`tools/`、`ci.yml` 等
路径——它们指向开发树，在本快照中不起作用。

解析不可信文档本身就是一个攻击面：见上文的不可见字符普查与
诚实失败契约。

## 许可证

MIT——见 [LICENSE](LICENSE) 与 [NOTICE](NOTICE)。

核心包以 MIT 发布，只依赖宽松许可证的组件。限制性许可证的引擎
全部是可选 extra，**默认不安装**：

- `provendoc[pdf]` 引入 **PyMuPDF**（AGPL-3.0，或向 Artifex 购买
  商业许可）。商业用户要么遵守 AGPL，要么获取 Artifex 许可。
  不装它，PDF 路径诚实降级（可审计兜底，绝不崩溃）。
- `provendoc[pdf-layout]` 引入 **MinerU**（MinerU 开源许可证——
  Apache-2.0 基础上的附加条款；商用前请阅读）。
- 表格 extra 额外引入 **pyxlsb**（LGPLv3+）与 **odfpy**
  （GPL-2.0+ / Apache-2.0 双许可；按 Apache-2.0 消费）。

MIT 授权仅覆盖 Provendoc 自身代码，不延伸到你自己安装的可选后端。

## 命名

"Provendoc"（provenance + document）是一个独立的开源项目。
**与** `provendoc.com` 或任何使用类似名称的主体**无隶属、无背书、
无关联**；如有雷同纯属巧合。
