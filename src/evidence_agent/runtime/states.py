"""运行时的三种终止状态。

单独放一个模块，是为了让 `disposition` 能用它们判断证据强度，又不必反过来
导入 `agent`——那会形成循环依赖。
"""

from __future__ import annotations

JUDGED = "JUDGED"
ESCALATED = "ESCALATED"
TOOL_FAILED = "TOOL_FAILED"
