# APT Bench Cascade Pipeline

从原文按顺序构建 passage、paragraph、sentence 三类目标。当前规则为 **v2（2026-10-28）**：保留所有通过基准一的篇幅，每个未通过的篇幅最多选择一个段落，再从剩余位置选择每位作者约200条句子。

## 选取规则

| 阶段 | 来源与方法 | 输出 |
|---|---|---|
| Passage | 原有章级预处理；完整章优先，长章使用本地 E5 语义向量和动态规划，形成1200–3000 tokens的完整非重叠切分；agent审核基准一 | **所有keep**，取消旧版一半上限 |
| Paragraph | 所有未通过基准一的篇幅，包括drop和未解决的need_more；agent阅读完整父篇幅，从既有原文选项中选择 | **每个父篇幅0或1条**；100–300 tokens，偏好100–200；完整自然段优先，必要时同一自然段内连续完整句子 |
| Sentence | 全部可用原文，扣除已选passage与paragraph的目标区间；agent以完整自然段为背景判断句子是否合格 | 合格候选去重后沿原文顺序均匀选择，默认**每位作者200条**；不足则全部保留并报告缺口 |

Paragraph只要求语义相对自足、表意完整，不要求完整解决一个问题。背景不能补救关键对象或指代留在目标外的问题。Sentence沿用原定义，可借助完整自然段理解指代。目标正文不能改写，三类目标内部及彼此之间不重叠；背景允许复用和重叠。正常文献引用、法条、短引语及论述中的少量诗句允许，独立诗歌目标和大篇幅借入他文排除。

篇幅仍只审核基准一，不能据此声称完成全文连贯或自足审核。段落有一轮agent选择和自检，未设置独立第二轮审核；减少数量不等于已经保证语义质量。

## 环境

Python 3.10+；使用已有环境，不自动安装或升级任何包。原工作环境为conda **MSAP**，始终加`-B`，不使用`-O`。依赖清单见[requirements.txt](requirements.txt)，该文件用于说明依赖，不要求在已有环境重新安装。

切分需要本地 `intfloat/multilingual-e5-small` 模型目录，默认查找固定修订 `614241f622f53c4eeff9890bdc4f31cfecc418b3` 的本机缓存。也可用`--model-path`指定。运行强制离线，不下载模型。模型权重、原始书籍和真实语料均不随本项目分发。

本项目是**程序校验与agent审核协作的流程**：Python准备固定审核任务，外部agent执行任务，Python校验真实返回结果。没有内置API密钥，也不会仅运行脚本就伪造模型审核。支持原生子agent的宿主可依[AGENTS.md](AGENTS.md)协调；其他宿主也可将任务JSON交给模型，按相同格式保存响应。

## 输入

每行一个原文unit，UTF-8 JSONL。必需字段为：

```json
{"scholar_id":"author_001","document_id":"book_001","document_title":"Book title","unit_id":"book_001_unit_001","unit_title":"Chapter title","text":"Original text with original paragraph breaks."}
```

`unit_id`全文件唯一。同一`document_id`的作者与标题必须一致。可加`language`等元数据。保留原文字符和空行；不可预先压缩空白。格式示例为[examples/source_units.jsonl](examples/source_units.jsonl)，它是人工编写的小示例，不是足长的真实语料或已完成审核的结果。

已有APT `exports_json`包可转换，无需安装依赖：

```powershell
conda activate MSAP
$python = (Get-Command python).Source
& $python -B -X utf8 pipeline.py import-bundle --source 'D:/path/to/processed_bundle' --out data/keynes.jsonl --author scholar_005
```

保留既有作者与文档ID，可启用原工程的文档结构适配；新增作者使用通用章标题识别。中文标点支持句子候选，但大陆法学的定制章级预处理目前适配原作者ID `scholar_031`、`scholar_032`。其他文档格式需先抽查分章和清理结果。

## 运行

每位作者单独一个新运行目录。下面示例假设已转换好本地输入文件：

```powershell
conda activate MSAP
$python = (Get-Command python).Source
& $python -B -X utf8 pipeline.py prepare --source data/keynes.jsonl --author scholar_005 --run runs/keynes_v2
& $python -B -X utf8 pipeline.py next --run runs/keynes_v2
```

`next`报告下一步程序动作或需要agent执行的任务文件，不会自行调用模型。完整顺序：

1. `prepare`：章级预处理、E5切分，保存来源坐标和切分状态。不足1200或切分失败的章不作为passage成品，但其可用正文仍进入句子候选。
2. `boundary-jobs`：生成`jobs/boundary/*.json`。交给agent完整阅读，结果写入任务内的`output_path`。
3. `select-passages`：核验全部边界结果，保留所有keep；未通过者保存为`remaining_passages.jsonl`。
4. `paragraph-jobs`：生成`jobs/paragraph/*.json`，交给agent每个父篇幅选0或1条。
5. `select-paragraphs`：核验数量、原文、长度、来源和去重。
6. `sentence-jobs`：准备全部剩余位置，生成`jobs/sentence/*.json`，交给agent审核。
7. `finalize`：合格句去重、均匀取200条、全量来源及冲突检查，导出`cascade_dataset.zip`。

每一步程序命令均为`& $python -B -X utf8 pipeline.py <阶段> --run runs/keynes_v2`。`status`查看已校验组数，`pending`列出缺少结果的任务。句子目标可在首次prepare时通过`--sentence-target 200`指定，随后固定。

可以断点续跑，但不能改动已经绑定的输入、代码、提示词、tokenizer或数量规则。修改这些内容须使用新目录，旧结果不得静默套用到新规则。已有真实结果继续使用；失败或缺失的批次单独重试。最终ZIP不静默覆盖。每条结果只有一轮选择理由，任务ID代表审核派发单，不能当作程序验证过的模型身份。

如果所有passage均通过，paragraph池为空是正常情况；如果句子合格不足200，`sentence_selection.json`记录缺口，不降低标准补数。通过的篇幅若存在原文重叠或规范化相同／包含文本，程序停止并要求解决来源重复，不为去重而悄悄丢弃通过者。段落去重淘汰后不自动用未审核备选补位。

## 输出及对应关系

| 文件 | 关键字段／关系 |
|---|---|
| `passages.jsonl` | `sample_id`、`text`、`token_count`、来源ID与`source_spans`、`boundary_review` |
| `remaining_passages.jsonl` | 未通过基准一的父篇幅，键为`candidate_id`；保留其边界判断 |
| `paragraphs.jsonl` | `text`为目标；`context`为**完整父篇幅**；`passage_id`连接remaining表的`candidate_id`，并非最终passage目标表 |
| `sentences.jsonl` | `text`为目标句，`paragraph`为包含该句的完整自然段；自然段背景可以与其他目标重叠 |
| `source_units.jsonl` | 本作者原始unit，供精确还原 |
| `audit/*.jsonl` | 分阶段审核选择、理由、输入与提示词hash |
| `*_selection.json`、`verification.json` | 各阶段数量、去重、句子缺口、来源还原与目标冲突检查 |

`source_spans`是原始unit.text的**0基、左闭右开字符区间**，按顺序拼接对应片段得到目标原文。`context_source_spans`恢复段落的篇幅背景，`paragraph_source_spans`恢复句子的自然段背景。`start/end`只表示外包区间，精确恢复应使用spans。所有ID以一次作者运行目录为命名空间，跨作者汇总需连同`scholar_id`使用。

完整候选、审核任务和原始响应保留在本地运行目录；最终ZIP含目标、背景父表、原文和审计汇总。本pipeline不生成Task，也不改动旧Task工程。

## 检查

```powershell
& $python -B -X utf8 -m unittest discover -s tests -v
```

测试覆盖取消一半上限、每父篇幅最多一条、未通过者来源限制、200句均匀取样与不足情况、原文与背景关系、错误审核响应的拒绝。测试中的响应是明确标注的合成fixture，不构成真实语料质量审核。

## 来源

切分方法继承[SPZ2025/segmentation_split_toll](https://github.com/SPZ2025/segmentation_split_toll)固定提交，派生版保持本项目1200–3000阈值，见[tools/SOURCE.md](tools/SOURCE.md)。v1 Keynes试跑的51/1015/1303条是旧规则成果，不是本v2重新执行的结果；本仓库发布代码与流程，不混入旧版成品。
