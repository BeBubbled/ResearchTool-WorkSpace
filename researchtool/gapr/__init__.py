"""gapr：研究空白调研（docs/gap-research.md）的确定性部分。

这个包只做不需要判断力的事：访问文献 API、缓存、检索记录、日期防火墙、
属性矩阵的规则计算（R1/R2/R2′/R3）、规则文件冻结。需要判断力的步骤
由 Claude Code 的 /gap-research 技能完成，本包内部不调用任何 LLM。
"""

__version__ = "0.1.0"
