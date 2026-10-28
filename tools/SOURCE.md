# 篇幅切分工具

此目录的 `segmentation_split_toll` 是上游 Python API 所需文件的原样副本，供本项目直接导入，不安装到 conda 环境。

- 仓库：https://github.com/SPZ2025/segmentation_split_toll
- 固定提交：`7318947431d30add87122b95fa79bc7cd149b15d`
- 版本：1.0.0
- 文件：`__init__.py`、`algorithm.py`、`backend.py`、`segmenter.py`、`profile.json`
- 复用 MSAP 现有依赖和固定修订的本地 E5 缓存；运行时使用 `-B`，禁止下载模型。

`segmentation_project/` 是本项目采用的派生版：从上述提交复制 `algorithm.py` 和 `segmenter.py`，将长度约束及相关状态、异常说明统一接入根目录 `source_utils.py` 的1200–3000范围，复用上游的E5Backend，结果profile标明派生范围。原始DP目标、候选位置生成、原文切片和语义向量计算保持不变；上游原样副本未改动。

独立项目入口为根目录`pipeline.py`，来源工具为`source_utils.py`；清理后的目标按来源片段逐条还原验证。上游profile中的2000–8000为历史原始范围，实际使用的派生版阈值为1200–3000。
