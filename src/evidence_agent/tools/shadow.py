"""联网影子模式的装配层：把"获取 → 快照 → 结果"串起来并与自动判定隔离。

**影子模式的含义**（本阶段硬约束，写在代码里防止无意越界）：

- 这一层的产物只出现在工作台的「联网参考材料」区，**不参与**任何自动判定；
- 它不注册进 Agent 的工具集（`build_tools` 没有它），所以评测与运行时
  不可能调用到它——隔离靠"不在工具集里"，不是靠调用方自觉；
- 影子材料与自动研判结果在数据结构上分开：一个是 `WebReference`，
  一个是 `ClaimOutcome`，报告里也分节。

新增测试 `tests/test_web_shadow.py` 里有一条锁死这件事：
同一个 Agent 在"有联网材料"与"没有联网材料"两种情况下，逐条主张的
状态／判定／标志位／档位必须完全一致。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .websearch import (
    FAILURE_STATUSES,
    STATUS_NO_PROVIDER,
    STATUS_OK,
    SearchHit,
    SearchProvider,
    SnapshotStore,
    WebFetcher,
    WebFetchResult,
    build_fetcher,
    build_search_provider,
    check_url,
    load_web_config,
    now_iso,
)

#: 材料在本阶段固定的用途标记。工作台必须原样显示这句话。
REFERENCE_DISCLAIMER = "联网参考材料仅供人工参考，不参与当前自动研判结论。"

#: 单次运行允许的最大联网请求数（防止一次点击打出几十个请求）。
MAX_REQUESTS_PER_RUN = 6


class ShadowRetrievalError(RuntimeError):
    pass


@dataclass
class WebReference:
    """一条联网参考材料的完整形状。

    `status` 会如实保留失败原因——**失败不是"未发现相关证据"**，
    界面必须能区分"查不到"与"没查成"。
    """

    query: str = ""
    mode: str = "url"  # url | search
    url: str = ""
    title: str = ""
    domain: str = ""
    snippet: str = ""
    status: str = ""
    http_status: int = 0
    fetched_at: str = ""
    text: str = ""
    text_chars: int = 0
    truncated: bool = False
    raw_sha256: str = ""
    text_sha256: str = ""
    snapshot: str = ""
    snapshot_meta: str = ""
    error: str = ""
    content_type: str = ""
    charset: str = ""
    redirects: list[str] = field(default_factory=list)
    reference_only: bool = True
    disclaimer: str = REFERENCE_DISCLAIMER

    @property
    def ok(self) -> bool:
        return self.status == STATUS_OK

    def to_dict(self, *, include_text: bool = True) -> dict[str, Any]:
        return {
            "query": self.query,
            "mode": self.mode,
            "url": self.url,
            "title": self.title,
            "domain": self.domain,
            "snippet": self.snippet,
            "status": self.status,
            "ok": self.ok,
            "http_status": self.http_status,
            "fetched_at": self.fetched_at,
            "text": self.text if include_text else self.text[:1000],
            "text_chars": self.text_chars,
            "truncated": self.truncated,
            "raw_sha256": self.raw_sha256,
            "text_sha256": self.text_sha256,
            "snapshot": self.snapshot,
            "snapshot_meta": self.snapshot_meta,
            "error": self.error,
            "content_type": self.content_type,
            "charset": self.charset,
            "redirects": list(self.redirects),
            # 这两条进接口，前端直接渲染，避免界面自己编措辞
            "reference_only": True,
            "disclaimer": REFERENCE_DISCLAIMER,
        }


def _domain_of(url: str) -> str:
    from urllib.parse import urlsplit

    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def _to_reference(
    result: WebFetchResult, *, query: str = "", mode: str = "url", snippet: str = ""
) -> WebReference:
    return WebReference(
        query=query,
        mode=mode,
        url=result.url,
        title=result.title or result.final_url or result.url,
        domain=_domain_of(result.final_url or result.url),
        snippet=snippet,
        status=result.status,
        http_status=result.http_status,
        fetched_at=result.fetched_at,
        text=result.text,
        text_chars=len(result.text),
        truncated=result.truncated,
        raw_sha256=result.raw_sha256,
        text_sha256=result.text_sha256,
        snapshot=result.snapshot_path,
        snapshot_meta=result.snapshot_meta_path,
        error=result.error,
        content_type=result.content_type,
        charset=result.charset,
        redirects=list(result.redirects),
    )


class ShadowRetrieval:
    """一次运行内的联网参考材料获取。

    用法（工作台）：

        shadow = ShadowRetrieval(root, run_id="content-20261015-120000")
        references = shadow.fetch_urls(["https://example.com/a"])
        references = shadow.discover("某产品 续航 60 天")

    任何一步失败都变成 `WebReference.status` 里的明确状态，不抛异常给界面。
    """

    def __init__(
        self,
        root: Path,
        *,
        run_id: str,
        fetcher: WebFetcher | None = None,
        provider: SearchProvider | None = None,
        store: SnapshotStore | None = None,
        config: Mapping[str, Any] | None = None,
        max_requests: int = MAX_REQUESTS_PER_RUN,
    ) -> None:
        self.root = Path(root)
        self.run_id = SnapshotStore.check_run_id(run_id)
        self.config = dict(config if config is not None else load_web_config(self.root))
        self.fetcher = fetcher or build_fetcher(self.config)
        self.provider = provider or build_search_provider(self.config)
        subdir = str(self.config.get("snapshot_dir") or "snapshots/web")
        self.store = store or SnapshotStore(self.root / "runs" / self.run_id, subdir=subdir)
        self.max_requests = int(max_requests)
        self.requests = 0

    # --- 对外 ---------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return bool(self.config.get("enabled", True))

    def fetch_urls(self, urls: Sequence[str]) -> list[WebReference]:
        """获取明确给出的 URL。**不需要任何密钥。**"""

        references: list[WebReference] = []
        for url in urls:
            if self.requests >= self.max_requests:
                references.append(
                    self._capped_reference(url, "url")
                )
                continue
            # 预检查：不安全的目标不必发起请求，也要留下记录
            check = check_url(
                str(url or "").strip(),
                resolver=self.fetcher.resolver,
                allow_hosts=self.fetcher.allow_hosts,
                deny_hosts=self.fetcher.deny_hosts,
            )
            if not check.ok:
                from .websearch import WebFetchResult as _R, STATUS_UNSAFE_URL, STATUS_BAD_URL

                status = STATUS_UNSAFE_URL if str(url or "").strip() else STATUS_BAD_URL
                references.append(
                    _to_reference(
                        _R(url=str(url or "").strip(), status=status, error=check.reason),
                        mode="url",
                    )
                )
                continue
            result = self.fetcher.fetch(url)
            self.requests += 1
            result = self.store.save(result, base=_domain_of(result.final_url or url) or "page")
            references.append(_to_reference(result, mode="url"))
        return references

    def discover(self, query: str) -> list[WebReference]:
        """按查询词发现候选 URL，再逐个取回。

        没有搜索密钥时返回**一条明确的失败记录**（`NO_PROVIDER`），
        不返回空列表——空列表会被界面误解成"没有相关材料"。
        """

        text = (query or "").strip()
        if not text:
            return [self._error_reference("", "search", "查询词为空")]
        if not self.enabled:
            return [self._error_reference(text, "search", "联网参考模式未启用")]
        if not self.provider.available:
            return [
                self._provider_reference(text)
            ]

        hits, error = self.provider.search(text)
        if error:
            return [self._error_reference(text, "search", error)]
        if not hits:
            from .websearch import STATUS_EMPTY

            return [self._empty_reference(text)]

        references: list[WebReference] = []
        for hit in hits:
            if self.requests >= self.max_requests:
                references.append(self._capped_reference(hit.url, "search"))
                continue
            result = self.fetcher.fetch(hit.url)
            self.requests += 1
            result = self.store.save(result, base=_domain_of(hit.url) or "page")
            references.append(
                _to_reference(result, query=text, mode="search", snippet=hit.snippet)
            )
        return references

    def references_payload(self, references: Sequence[WebReference]) -> dict[str, Any]:
        """给接口与报告的形状。刻意与自动研判结果平级、分开。"""

        failed = [r for r in references if r.status in FAILURE_STATUSES]
        ok = [r for r in references if r.status == STATUS_OK]
        return {
            "disclaimer": REFERENCE_DISCLAIMER,
            "reference_only": True,
            "run_id": self.run_id,
            "requests": self.requests,
            "max_requests": self.max_requests,
            "counts": {
                "total": len(references),
                "ok": len(ok),
                "failed": len(failed),
            },
            "items": [r.to_dict() for r in references],
        }

    # --- 内部：几种失败形状 --------------------------------------------

    def _error_reference(self, query: str, mode: str, message: str) -> WebReference:
        return WebReference(
            query=query,
            mode=mode,
            status=STATUS_NO_PROVIDER if "密钥" in message else "UPSTREAM_ERROR",
            fetched_at=now_iso(),
            error=message,
        )

    def _provider_reference(self, query: str) -> WebReference:
        return WebReference(
            query=query,
            mode="search",
            status=STATUS_NO_PROVIDER,
            fetched_at=now_iso(),
            error=(
                f"未配置搜索 API 密钥（环境变量 {self.provider.api_key_env}）："
                "发现模式不可用。可以先直接给出具体 URL 使用获取模式。"
            ),
        )

    def _empty_reference(self, query: str) -> WebReference:
        from .websearch import STATUS_EMPTY

        return WebReference(
            query=query,
            mode="search",
            status=STATUS_EMPTY,
            fetched_at=now_iso(),
            error="搜索接口没有返回任何结果（这不是获取失败）",
        )

    def _capped_reference(self, url: str, mode: str) -> WebReference:
        from .websearch import STATUS_BLOCKED

        return WebReference(
            query="",
            mode=mode,
            url=url,
            status=STATUS_BLOCKED,
            fetched_at=now_iso(),
            error=f"本次运行的联网请求数已达上限（{self.max_requests}），未继续获取",
        )


def load_reference_payload(root: Path, run_id: str) -> dict[str, Any] | None:
    """读回一次运行保存的联网参考材料。没有就是 None。"""

    from ..runio import RUN_ID_PATTERN

    if not RUN_ID_PATTERN.match(str(run_id or "")):
        raise ShadowRetrievalError(f"运行编号不合法：{run_id!r}")
    path = Path(root) / "runs" / run_id / "web_references.json"
    if not path.exists():
        return None
    import json

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def write_reference_payload(root: Path, run_id: str, payload: Mapping[str, Any]) -> Path:
    """把参考材料写进运行目录的索引文件（`web_references.json`）。

    合并语义，而不是覆盖或拒绝：

    - **证据快照本身绝不改**（`snapshots/web/*.raw`／`*.txt`／`*.json` 每个文件只写一次，
      同名自动加序号）；
    - 索引文件按 `snapshot` 去重后**累加**，因为一个运行里可以分多次取材料
      （先取一个地址、再发现一批、中间还有被拒的尝试）。若在这里拒绝写入，
      后取的材料与失败记录就丢了——那等于界面比落盘的内容更"全"，不可追溯。

    合并时不重算请求数：`requests` 累加，`counts` 按合并后的条目重算。
    """

    import json
    from datetime import datetime, timezone

    from ..runio import RUN_ID_PATTERN

    if not RUN_ID_PATTERN.match(str(run_id or "")):
        raise ShadowRetrievalError(f"运行编号不合法：{run_id!r}")
    run_dir = Path(root) / "runs" / run_id
    if not run_dir.exists():
        raise ShadowRetrievalError(f"运行目录不存在，不能写参考材料：{run_id}")
    path = run_dir / "web_references.json"

    incoming = dict(payload)
    existing: dict[str, Any] = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                existing = loaded
        except (OSError, json.JSONDecodeError):
            existing = {}

    def key_of(item: Mapping[str, Any]) -> str:
        return str(item.get("snapshot_meta") or item.get("text_sha256") or item.get("url") or "")

    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in list(existing.get("items") or []) + list(incoming.get("items") or []):
        if not isinstance(item, Mapping):
            continue
        key = key_of(item)
        if key and key in seen:
            # 同一份材料重复出现时保留信息更全的那条（例如失败→成功）
            continue
        if key:
            seen.add(key)
        merged.append(dict(item))

    ok = [item for item in merged if item.get("status") == "OK"]
    failed = [item for item in merged if item.get("status") != "OK"]
    final = {
        "disclaimer": incoming.get("disclaimer") or existing.get("disclaimer") or REFERENCE_DISCLAIMER,
        "reference_only": True,
        "run_id": run_id,
        "requests": int(existing.get("requests") or 0) + int(incoming.get("requests") or 0),
        "max_requests": incoming.get("max_requests") or existing.get("max_requests"),
        "counts": {"total": len(merged), "ok": len(ok), "failed": len(failed)},
        "items": merged,
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    if existing.get("created_at"):
        final["created_at"] = existing["created_at"]
    else:
        final["created_at"] = final["updated_at"]

    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(final, ensure_ascii=False, indent=2), encoding="utf-8")
    import os as _os

    _os.replace(tmp, path)
    return path


__all__ = [
    "MAX_REQUESTS_PER_RUN",
    "REFERENCE_DISCLAIMER",
    "ShadowRetrieval",
    "ShadowRetrievalError",
    "WebReference",
    "load_reference_payload",
    "write_reference_payload",
]
