"""联网影子模式的检索层：真实网页获取、发现与原文快照。

**定位（本阶段硬约束）**：联网结果只作为工作台里的"人工参考材料"。

- 不参与主张真假判断；
- 不参与 `lookup_fact` 或 `compare_numeric` 的计算；
- 不参与来源门槛、权威性排序与 R1–R13 处置；
- 不改变既有评测任务的输入输出（评测工具集里没有这个工具）。

它存在的意义是让运营看到真实材料，并为将来的联网核查积累**可复现的快照**：
`文档库` 之外的取舍记录见 `docs/16-backlog.md` E4 条（评测基线不联网，
真实产品必须联网，"可复现"靠结果快照实现）。

## 两种能力必须分清

| 模式 | 输入 | 需不需要密钥 | 状态 |
|---|---|---|---|
| `url` | 明确的公开 URL | 不需要 | 已实现 |
| `search` | 查询词 | 需要搜索 API 密钥 | 已实现，未配密钥时明确报错 |

**不做**：把搜索引擎结果页当作 API 抓取。没有密钥时"发现模式"直接返回
`no_provider`，绝不伪造搜索结果。
"""

from __future__ import annotations

import hashlib
import html
import io
import ipaddress
import json
import os
import re
import socket
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

#: 获取状态。区分"没有结果""获取失败""没有正文""超时"——
#: 绝不把失败伪装成"未发现相关证据"。
STATUS_OK = "OK"
STATUS_EMPTY = "EMPTY"
STATUS_BLOCKED = "BLOCKED"
STATUS_TIMEOUT = "TIMEOUT"
STATUS_HTTP_ERROR = "HTTP_ERROR"
STATUS_TOO_LARGE = "TOO_LARGE"
STATUS_NO_TEXT = "NO_TEXT"
STATUS_UNSUPPORTED_TYPE = "UNSUPPORTED_TYPE"
STATUS_UNSAFE_URL = "UNSAFE_URL"
STATUS_BAD_URL = "BAD_URL"
STATUS_NO_PROVIDER = "NO_PROVIDER"
STATUS_UPSTREAM_ERROR = "UPSTREAM_ERROR"

#: 这些状态表示"这次获取没有可用的正文"，工作台要如实展示原因。
FAILURE_STATUSES = frozenset(
    {
        STATUS_BLOCKED,
        STATUS_TIMEOUT,
        STATUS_HTTP_ERROR,
        STATUS_TOO_LARGE,
        STATUS_NO_TEXT,
        STATUS_UNSUPPORTED_TYPE,
        STATUS_UNSAFE_URL,
        STATUS_BAD_URL,
        STATUS_NO_PROVIDER,
        STATUS_UPSTREAM_ERROR,
    }
)

ALLOWED_SCHEMES = ("http", "https")
TEXT_CONTENT_TYPES = ("text/html", "text/plain", "application/xhtml", "application/xml", "text/xml")

DEFAULT_TIMEOUT_S = 8.0
DEFAULT_MAX_REDIRECTS = 3
DEFAULT_MAX_BYTES = 512 * 1024
DEFAULT_MAX_TEXT_CHARS = 6000

#: 内网与保留地址段。SSRF 的第一道闸：**字面**判断，不用解析。
_BLOCKED_NETS = (
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),   # 运营商级 NAT
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),  # 链路本地，含云元数据 169.254.169.254
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.0.0.0/24"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("198.18.0.0/15"),
    ipaddress.ip_network("224.0.0.0/4"),
    ipaddress.ip_network("240.0.0.0/4"),
    ipaddress.ip_network("::/128"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("ff00::/8"),
)

#: 云厂商元数据主机名。DNS 解析成公网地址也不能放行——它们本来就是内网入口。
_BLOCKED_HOSTNAMES = frozenset(
    {
        "metadata.google.internal",
        "metadata.goog",
        "instance-data",
    }
)


class WebRetrievalError(RuntimeError):
    """联网检索的配置或使用错误。"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --- URL 安全 ---------------------------------------------------------


@dataclass(frozen=True)
class UrlCheck:
    ok: bool
    reason: str = ""
    resolved: tuple[str, ...] = ()


def _ip_is_blocked(ip: ipaddress._BaseAddress) -> bool:
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast:
        return True
    if getattr(ip, "is_site_local", False):
        return True
    return any(ip in net for net in _BLOCKED_NETS if net.version == ip.version)


def default_resolver(host: str) -> Sequence[str]:
    """把主机名解析成 IP 列表。测试注入用，生产走 `socket.getaddrinfo`。"""

    infos = socket.getaddrinfo(host, None)
    return sorted({str(info[4][0]) for info in infos})


def check_url(
    url: str,
    *,
    resolver: Callable[[str], Sequence[str]] = default_resolver,
    allow_hosts: Sequence[str] = (),
    deny_hosts: Sequence[str] = (),
) -> UrlCheck:
    """判断这个 URL 允不允许访问（SSRF 闸门）。

    三道检查，缺一不可：

    1. **协议**：只允许 http／https，`file:`、`ftp:`、`data:` 一律拒绝；
    2. **字面**：主机是 IP 时直接判它是不是私有／保留段；
    3. **解析后**：主机名要解析，解析出的每个地址都要过同一套判断。
       DNS rebinding 与"内网域名"都靠这一步拦住。

    重定向之后必须**重新调用本函数**——只校验初始 URL 等于没校验。
    """

    if not url or not isinstance(url, str):
        return UrlCheck(False, "URL 为空")

    parsed = urllib.parse.urlsplit(url.strip())
    scheme = (parsed.scheme or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        return UrlCheck(False, f"只允许 http／https，收到 {scheme or '无协议'}")
    if not parsed.netloc:
        return UrlCheck(False, "缺少主机名")

    try:
        host = (parsed.hostname or "").strip().strip(".")
    except ValueError:
        return UrlCheck(False, "主机名非法")
    if not host:
        return UrlCheck(False, "缺少主机名")
    if parsed.username or parsed.password:
        # 带凭据的 URL 会把口令写进快照与日志
        return UrlCheck(False, "URL 里不允许携带用户名或口令")

    lowered = host.lower()
    if lowered in _BLOCKED_HOSTNAMES:
        return UrlCheck(False, f"禁止访问元数据主机：{lowered}")
    if lowered in {h.lower() for h in deny_hosts if h}:
        return UrlCheck(False, f"该主机在拒绝名单里：{lowered}")
    allow = {h.lower() for h in allow_hosts if h}
    if allow and lowered not in allow:
        return UrlCheck(False, f"该主机不在允许名单里：{lowered}")

    literal: ipaddress._BaseAddress | None = None
    try:
        literal = ipaddress.ip_address(lowered)
    except ValueError:
        literal = None

    if literal is not None:
        if _ip_is_blocked(literal):
            return UrlCheck(False, f"禁止访问内网或保留地址：{literal}")
        return UrlCheck(True, "", (str(literal),))

    try:
        addresses = tuple(resolver(host))
    except Exception as exc:  # DNS 失败要当作"获取失败"，不是"没有证据"
        return UrlCheck(False, f"域名解析失败：{type(exc).__name__}")

    if not addresses:
        return UrlCheck(False, "域名解析没有返回地址")

    for text in addresses:
        try:
            ip = ipaddress.ip_address(str(text))
        except ValueError:
            return UrlCheck(False, f"解析结果不是合法地址：{text}")
        if _ip_is_blocked(ip):
            return UrlCheck(False, f"解析到内网或保留地址：{text}")
    return UrlCheck(True, "", addresses)


def resolve_redirect(current: str, location: str) -> str:
    """按 RFC 把 Location 解析成绝对 URL（相对路径、协议相对都要支持）。"""

    return urllib.parse.urljoin(current, location)


# --- 正文提取 ---------------------------------------------------------


_SCRIPT_BLOCKS = re.compile(
    r"<(script|style|noscript|template|svg|iframe)\b.*?</\1>", re.IGNORECASE | re.DOTALL
)
_BLOCK_LEVEL = re.compile(
    r"</?(p|div|br|li|tr|h[1-6]|section|article|header|footer|blockquote|table)\b[^>]*>",
    re.IGNORECASE,
)
_TAGS = re.compile(r"<[^>]+>")
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_META_CHARSET = re.compile(
    r"""<meta[^>]+charset\s*=\s*["']?\s*([A-Za-z0-9_\-]+)""", re.IGNORECASE
)
_WHITESPACE = re.compile(r"[ \t\u00a0]+")
_MULTI_NEWLINE = re.compile(r"\n{3,}")


def guess_charset(raw: bytes, content_type: str = "") -> str:
    """猜编码：HTTP 头 → HTML meta → UTF-8 → GBK → 兜底 Latin-1。"""

    header_match = re.search(r"charset=([A-Za-z0-9_\-]+)", content_type or "", re.IGNORECASE)
    if header_match:
        return header_match.group(1)
    head = raw[:4096].decode("ascii", errors="ignore")
    meta_match = _META_CHARSET.search(head)
    if meta_match:
        return meta_match.group(1)
    for candidate in ("utf-8", "gb18030"):
        try:
            raw.decode(candidate)
            return candidate
        except UnicodeDecodeError:
            continue
    return "latin-1"


def decode_body(raw: bytes, content_type: str = "") -> tuple[str, str]:
    """把原始字节解码成文本。返回 (text, 实际使用的编码)。

    解码失败不让异常冒出去：联网内容的编码不可控，取不到正文要如实标记
    （调用方按 `NO_TEXT` 处理），而不是抛异常掀翻整个请求。
    """

    charset = guess_charset(raw, content_type)
    try:
        return raw.decode(charset, errors="replace"), charset
    except LookupError:
        return raw.decode("utf-8", errors="replace"), "utf-8(replace)"


def extract_text(body: str) -> tuple[str, str]:
    """从 HTML 里抽正文，返回 (正文, 标题)。

    不做可读性算法，只做确定性清洗：去掉脚本样式、块级标签转换行、去标签、
    反转义实体、压空白。**不执行任何脚本**，也不解析 DOM。
    """

    title = ""
    title_match = _TITLE.search(body or "")
    if title_match:
        title = html.unescape(_TAGS.sub("", title_match.group(1))).strip()

    text = _SCRIPT_BLOCKS.sub(" ", body or "")
    text = _BLOCK_LEVEL.sub("\n", text)
    text = _TAGS.sub(" ", text)
    text = html.unescape(text)
    text = _WHITESPACE.sub(" ", text)
    lines = [line.strip() for line in text.splitlines()]
    text = "\n".join(line for line in lines if line)
    return _MULTI_NEWLINE.sub("\n\n", text).strip(), title


# --- 网页获取 ---------------------------------------------------------


@dataclass
class WebFetchResult:
    """一次获取的完整留痕。字段对齐"输出应为结构化结果"的要求。"""

    url: str
    final_url: str = ""
    status: str = STATUS_UPSTREAM_ERROR
    http_status: int = 0
    content_type: str = ""
    charset: str = ""
    title: str = ""
    text: str = ""
    fetched_at: str = field(default_factory=now_iso)
    raw_sha256: str = ""
    text_sha256: str = ""
    truncated: bool = False
    error: str = ""
    redirects: list[str] = field(default_factory=list)
    raw_bytes: bytes = b""
    snapshot_path: str = ""
    snapshot_meta_path: str = ""

    @property
    def ok(self) -> bool:
        return self.status == STATUS_OK

    def to_dict(self, *, include_text: bool = True) -> dict[str, Any]:
        """给接口与报告用的形状。原始字节不进 JSON（另有快照文件）。"""

        payload = {
            "url": self.url,
            "final_url": self.final_url,
            "status": self.status,
            "ok": self.ok,
            "http_status": self.http_status,
            "content_type": self.content_type,
            "charset": self.charset,
            "title": self.title,
            "fetched_at": self.fetched_at,
            "raw_sha256": self.raw_sha256,
            "text_sha256": self.text_sha256,
            "truncated": self.truncated,
            "error": self.error,
            "redirects": list(self.redirects),
            "raw_bytes": len(self.raw_bytes),
            "text_chars": len(self.text),
            "snapshot": self.snapshot_path,
            "snapshot_meta": self.snapshot_meta_path,
        }
        payload["text"] = self.text if include_text else self.text[:1200]
        return payload


def find_curl() -> str | None:
    """找一个可用的 curl 可执行文件。

    为什么需要它：部分国内官方站点（民航局、市场监管总局、药监局等）的 TLS 配置
    与 Python 标准库的协商方式不兼容——TCP 通、浏览器与 curl 都能打开，标准库握手
    却被对端切断（`SSL: UNEXPECTED_EOF_WHILE_READING`）。这不是反爬，是协议栈差异。
    采集真实材料时用系统 curl（Windows 走 Schannel）作为**回退**，而不是放弃这些站点。
    """

    import shutil as _shutil

    for name in ("curl", "curl.exe"):
        found = _shutil.which(name)
        if found:
            try:
                proc = subprocess.run(
                    [found, "--version"], capture_output=True, timeout=5, check=False
                )
                if proc.returncode == 0:
                    return found
            except (OSError, subprocess.SubprocessError):
                continue
    return None


def fetch_text_via_curl(
    curl: str,
    url: str,
    *,
    user_agent: str,
    timeout_s: float = 8.0,
    max_bytes: int = 200_000,
) -> tuple[str, str]:
    """用系统 curl 取一小段文本，返回 (是否成功, 内容或错误)。

    与 `WebFetcher._fetch_via_curl` 同样的理由：部分国内站点的 TLS 只认某些协商方式，
    标准库握手会被切断，而系统 curl 能正常取回。工具本身不绕过任何访问控制。
    """

    import subprocess
    import tempfile

    with tempfile.TemporaryDirectory(prefix="cea-curl-text-") as tmp:
        body_path = Path(tmp) / "body"
        meta_path = Path(tmp) / "meta"
        command = [
            curl,
            "--silent",
            "--show-error",
            "--location",
            "--max-redirs",
            "3",
            "--max-time",
            str(int(timeout_s)),
            "--max-filesize",
            str(max_bytes),
            "--user-agent",
            user_agent,
            "--output",
            str(body_path),
            "--write-out",
            "%{http_code}",
            url,
        ]
        try:
            with meta_path.open("wb") as meta_handle:
                proc = subprocess.run(
                    command,
                    stdout=meta_handle,
                    stderr=subprocess.DEVNULL,
                    timeout=timeout_s + 5,
                    check=False,
                )
        except (OSError, subprocess.SubprocessError) as exc:
            return False, f"curl 不可用：{type(exc).__name__}"
        if proc.returncode != 0:
            return False, f"curl 退出码 {proc.returncode}"
        code = meta_path.read_text(encoding="utf-8", errors="replace").strip()
        if code and code.isdigit() and int(code) >= 400:
            return False, f"HTTP {code}"
        if not body_path.exists():
            return False, "curl 没有写出内容"
        return True, body_path.read_text(encoding="utf-8", errors="replace")


class WebFetcher:
    """获取公开网页：校验目标、跟随重定向、限流限量、抽取正文。

    不执行 JavaScript；网页内容一律当**不可信数据**，不会被解释成指令。
    """

    def __init__(
        self,
        *,
        user_agent: str = "ContentEvidenceAgent/0.3",
        timeout_s: float = DEFAULT_TIMEOUT_S,
        max_redirects: int = DEFAULT_MAX_REDIRECTS,
        max_bytes: int = DEFAULT_MAX_BYTES,
        max_text_chars: int = DEFAULT_MAX_TEXT_CHARS,
        resolver: Callable[[str], Sequence[str]] = default_resolver,
        allow_hosts: Sequence[str] = (),
        deny_hosts: Sequence[str] = (),
        curl_fallback: bool = True,
    ) -> None:
        self.user_agent = user_agent
        self.timeout_s = float(timeout_s)
        self.max_redirects = int(max_redirects)
        self.max_bytes = int(max_bytes)
        self.max_text_chars = int(max_text_chars)
        self.resolver = resolver
        self.allow_hosts = tuple(allow_hosts)
        self.deny_hosts = tuple(deny_hosts)
        #: 标准库 TLS 握手失败时是否回退到系统 curl（见 find_curl 的说明）
        self.curl_fallback = bool(curl_fallback)
        self._curl = find_curl() if self.curl_fallback else None

    # --- 内部：一次请求 -------------------------------------------------

    def _open(self, url: str):
        """返回一个可用作上下文管理器的响应对象。

        顺序：标准库默认协商 → 限定 TLS 1.2 再试 → 交给系统 curl。
        三步都在同一条路径上，任何一步成功就直接返回，不再往下走。
        """

        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": self.user_agent,
                "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.5",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            },
            method="GET",
        )
        try:
            return urllib.request.urlopen(request, timeout=self.timeout_s)
        except urllib.error.URLError as exc:
            if not self._is_tls_handshake_error(exc):
                raise
            first_error = exc

        # 第二步：显式限定 TLS 1.2
        try:
            return self._open_with_tls12(request)
        except Exception as tls12_error:  # noqa: BLE001 - 记录原因后继续回退
            self._last_tls_error = tls12_error

        # 第三步：系统 curl（Windows 走 Schannel，能协商这些站点）
        if self._curl:
            adapted = self._curl_adapted_response(url)
            if adapted is not None:
                return adapted

        raise first_error

    @staticmethod
    def _is_tls_handshake_error(exc: BaseException) -> bool:
        reason = getattr(exc, "reason", exc)
        text = f"{type(reason).__name__}: {reason}"
        return "SSL" in text or "UNEXPECTED_EOF" in text

    def _open_with_tls12(self, request: urllib.request.Request):
        import ssl as _ssl

        context = _ssl.create_default_context()
        context.minimum_version = _ssl.TLSVersion.TLSv1_2
        context.maximum_version = _ssl.TLSVersion.TLSv1_2
        return urllib.request.urlopen(request, timeout=self.timeout_s, context=context)

    def _curl_adapted_response(self, url: str):
        """用 curl 取回并把结果包成"像 urlopen 返回值"的对象。

        即使 curl 取到了**没有正文**的页面（`NO_TEXT`）也要返回这个包装：那是一个
        真实结论（页面是脚本渲染的空壳），不能被降级成"TLS 失败"。只有真正没取到
        （网络/TLS/HTTP 错误）才返回 None。
        """

        result = WebFetchResult(url=url)
        fetched = self._fetch_via_curl(result)
        if fetched.status not in (STATUS_OK, STATUS_NO_TEXT, STATUS_UNSUPPORTED_TYPE):
            return None

        class _CurlResponse:
            def __init__(self, item: WebFetchResult) -> None:
                self._stream = io.BytesIO(bytes(item.raw_bytes))
                self.url = item.final_url or item.url
                self.status = item.http_status or 200
                self.headers = {"Content-Type": item.content_type or "text/html"}
                #: 标记这已经是 curl 的完整结论，调用方不要再走一遍正文抽取
                self.final_result = item

            def read(self, size: int = -1) -> bytes:
                return self._stream.read(size)

            def __enter__(self):
                return self

            def __exit__(self, *exc) -> bool:
                return False

        return _CurlResponse(fetched)

    def _fetch_via_curl(self, result: WebFetchResult) -> WebFetchResult:
        """用系统 curl 取一次，结果写回同一个对象。

        与标准库路径共用同样的后处理（截断、编码猜测、正文抽取、哈希），
        差别只在传输层；不带着 Cookie、不绕过任何访问控制、仍受同一套目标校验约束。

        实现细节：**用文件重定向，不用管道**。受限环境下管道（`capture_output`）
        会被沙箱策略拒绝，而重定向到临时文件没有这个限制，行为也更可预期。
        """

        if not self._curl:
            return result
        import tempfile

        with tempfile.TemporaryDirectory(prefix="cea-curl-") as tmp:
            body_path = Path(tmp) / "body"
            meta_path = Path(tmp) / "meta"
            command = [
                self._curl,
                "--silent",
                "--show-error",
                "--location",
                # 注意：--max-redirs 只接受空格分隔（写成 --max-redirs=3 会被 curl 视为未知参数）
                "--max-redirs",
                str(self.max_redirects),
                "--max-time",
                str(int(self.timeout_s)),
                "--max-filesize",
                str(self.max_bytes),
                "--user-agent",
                self.user_agent,
                "--output",
                str(body_path),
                "--write-out",
                "%{http_code}\n%{content_type}\n%{url_effective}",
                result.url,
            ]
            try:
                with meta_path.open("wb") as meta_handle:
                    proc = subprocess.run(
                        command,
                        stdout=meta_handle,
                        stderr=subprocess.DEVNULL,
                        timeout=self.timeout_s + 5,
                        check=False,
                    )
            except (OSError, subprocess.SubprocessError) as exc:
                result.status = STATUS_UPSTREAM_ERROR
                result.error = f"curl 回退失败：{type(exc).__name__}"
                return result

            if proc.returncode != 0:
                result.status = STATUS_UPSTREAM_ERROR
                result.error = f"curl 回退失败（退出码 {proc.returncode}）"
                return result
            if not body_path.exists() or not meta_path.exists():
                result.status = STATUS_UPSTREAM_ERROR
                result.error = "curl 回退没有写出响应"
                return result

            raw_bytes = body_path.read_bytes()
            meta_lines = meta_path.read_text(encoding="utf-8", errors="replace").splitlines()

        http_code = meta_lines[0].strip() if meta_lines else ""
        result.content_type = meta_lines[1].strip() if len(meta_lines) > 1 else ""
        result.final_url = (meta_lines[2].strip() if len(meta_lines) > 2 else "") or result.url
        result.http_status = int(http_code) if http_code.isdigit() else 0

        if len(raw_bytes) > self.max_bytes:
            result.truncated = True
            raw_bytes = raw_bytes[: self.max_bytes]
        result.raw_bytes = raw_bytes

        if result.http_status and result.http_status >= 400:
            result.status = STATUS_HTTP_ERROR
            result.error = f"HTTP {result.http_status}"
            return result

        lowered = result.content_type.lower()
        if lowered and not any(token in lowered for token in TEXT_CONTENT_TYPES):
            result.status = STATUS_UNSUPPORTED_TYPE
            result.raw_sha256 = sha256_hex(raw_bytes)
            result.error = f"不支持的内容类型：{result.content_type}"
            return result

        text_body, charset = decode_body(raw_bytes, result.content_type)
        result.charset = charset
        text, title = extract_text(text_body)
        result.title = title
        if len(text) > self.max_text_chars:
            result.truncated = True
            text = text[: self.max_text_chars]
        result.text = text
        result.raw_sha256 = sha256_hex(raw_bytes)
        result.text_sha256 = sha256_hex(text.encode("utf-8"))
        if not text.strip():
            result.status = STATUS_NO_TEXT
            result.error = "页面没有可用正文（可能是脚本渲染或纯结构化页面）"
            return result
        result.status = STATUS_OK
        result.error = ""
        return result

    def fetch(self, url: str) -> WebFetchResult:
        """获取一个 URL。任何失败都以**明确状态**返回，不抛异常、不伪装成空结果。"""

        result = WebFetchResult(url=str(url or "").strip())
        current = result.url
        if not current:
            result.status = STATUS_BAD_URL
            result.error = "URL 为空"
            return result

        seen: list[str] = []
        for hop in range(self.max_redirects + 1):
            # 每一跳都重新校验：只校验初始 URL 挡不住"公网跳内网"
            check = check_url(
                current,
                resolver=self.resolver,
                allow_hosts=self.allow_hosts,
                deny_hosts=self.deny_hosts,
            )
            if not check.ok:
                result.status = STATUS_UNSAFE_URL if hop == 0 else STATUS_BLOCKED
                result.error = check.reason
                result.redirects = seen
                return result

            try:
                with self._open(current) as response:
                    result.http_status = int(getattr(response, "status", 200) or 200)
                    result.final_url = str(getattr(response, "url", current) or current)
                    result.content_type = str(response.headers.get("Content-Type", "") or "")
                    # curl 路径已经做完整个结论（含"没有正文"这种真实状态），
                    # 直接采信，不要再拿字节走一遍抽取。
                    settled = getattr(response, "final_result", None)
                    if settled is not None:
                        settled.url = result.url
                        settled.redirects = seen
                        return settled
                    raw = response.read(self.max_bytes + 1)
            except urllib.error.HTTPError as exc:
                location = (exc.headers or {}).get("Location") if exc.headers else None
                if 300 <= int(exc.code) < 400 and location:
                    target = resolve_redirect(current, str(location))
                    if hop >= self.max_redirects:
                        result.status = STATUS_HTTP_ERROR
                        result.http_status = int(exc.code)
                        result.error = f"重定向次数超过上限（{self.max_redirects}）"
                        result.redirects = seen
                        return result
                    seen.append(target)
                    current = target
                    continue
                result.status = STATUS_HTTP_ERROR
                result.http_status = int(exc.code)
                result.error = f"HTTP {exc.code}"
                result.redirects = seen
                return result
            except urllib.error.URLError as exc:
                reason = exc.reason
                if isinstance(reason, socket.timeout) or "timed out" in str(reason).lower():
                    result.status = STATUS_TIMEOUT
                    result.error = f"请求超时（{self.timeout_s}s）"
                elif self._is_tls_handshake_error(exc):
                    # 标准库协商不了就换系统 curl 的 TLS 栈（见 find_curl 的说明）。
                    # 这不是绕过限制：同样的 UA、不带着 Cookie、仍受同一套目标校验约束。
                    if self._curl:
                        recovered = self._fetch_via_curl(result)
                        if recovered.ok:
                            return recovered
                    result.status = STATUS_UPSTREAM_ERROR
                    result.error = (
                        f"TLS 握手失败（系统 curl 回退也不可用）：{reason}"
                    )
                else:
                    result.status = STATUS_UPSTREAM_ERROR
                    result.error = f"网络不可用：{type(reason).__name__}: {reason}"
                result.redirects = seen
                return result
            except (TimeoutError, socket.timeout):
                result.status = STATUS_TIMEOUT
                result.error = f"请求超时（{self.timeout_s}s）"
                result.redirects = seen
                return result
            except ValueError as exc:
                # urlopen 对非法 URL 抛 ValueError
                result.status = STATUS_BAD_URL
                result.error = f"URL 非法：{exc}"
                result.redirects = seen
                return result

            # HTTP 层成功：处理正文
            result.redirects = seen
            if len(raw) > self.max_bytes:
                result.truncated = True
                raw = raw[: self.max_bytes]
            result.raw_bytes = raw

            lowered_type = result.content_type.lower()
            is_text = (
                not lowered_type
                or any(token in lowered_type for token in TEXT_CONTENT_TYPES)
            )
            if not is_text:
                result.status = STATUS_UNSUPPORTED_TYPE
                result.raw_sha256 = sha256_hex(raw)
                result.raw_bytes = raw
                result.error = f"不支持的内容类型：{result.content_type}"
                return result

            body, charset = decode_body(raw, result.content_type)
            result.charset = charset
            text, title = extract_text(body)
            result.title = title
            if len(text) > self.max_text_chars:
                result.truncated = True
                text = text[: self.max_text_chars]
            result.text = text
            result.raw_sha256 = sha256_hex(raw)
            result.text_sha256 = sha256_hex(text.encode("utf-8"))
            if not text.strip():
                result.status = STATUS_NO_TEXT
                result.error = "页面没有可用正文（可能是脚本渲染或纯结构化页面）"
                return result
            result.status = STATUS_OK
            return result

        result.status = STATUS_HTTP_ERROR
        result.error = f"重定向次数超过上限（{self.max_redirects}）"
        result.redirects = seen
        return result


# --- 结果发现 ---------------------------------------------------------


@dataclass(frozen=True)
class SearchHit:
    url: str
    title: str = ""
    snippet: str = ""


class SearchProvider:
    """搜索 API 提供方。没配密钥就明确说没有，绝不伪造结果。"""

    def __init__(
        self,
        *,
        provider: str = "brave",
        api_key_env: str = "BRAVE_SEARCH_API_KEY",
        endpoint: str = "",
        max_results: int = 5,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        api_key: str | None = None,
        opener: Callable[[urllib.request.Request, float], Any] | None = None,
    ) -> None:
        self.provider = provider
        self.api_key_env = api_key_env
        self.endpoint = endpoint or ""
        self.max_results = int(max_results)
        self.timeout_s = float(timeout_s)
        self._api_key = (api_key if api_key is not None else os.environ.get(api_key_env, "")).strip()
        self._opener = opener or (
            lambda request, timeout: urllib.request.urlopen(request, timeout=timeout)
        )

    @property
    def available(self) -> bool:
        return bool(self._api_key)

    def search(self, query: str) -> tuple[list[SearchHit], str]:
        """返回 (结果, 错误)。错误非空表示**这次没有拿到结果**，不是"没有结果"。"""

        text = (query or "").strip()
        if not text:
            return [], "查询词为空"
        if not self.available:
            return [], (
                f"未配置搜索 API 密钥（环境变量 {self.api_key_env}），"
                "本次无法执行发现模式；URL 获取模式不需要密钥。"
            )

        request = urllib.request.Request(
            f"{self.endpoint}?q={urllib.parse.quote(text)}&count={self.max_results}",
            headers={
                "Accept": "application/json",
                "X-Subscription-Token": self._api_key,
            },
            method="GET",
        )
        try:
            with self._opener(request, self.timeout_s) as response:
                payload = json.loads(response.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as exc:
            return [], f"搜索接口返回 HTTP {exc.code}"
        except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
            return [], f"搜索接口不可用：{type(exc).__name__}"
        except (json.JSONDecodeError, UnicodeDecodeError):
            return [], "搜索接口返回的不是合法 JSON"

        hits: list[SearchHit] = []
        for row in (payload.get("web", {}) or {}).get("results", []) or []:
            url = str(row.get("url", "")).strip()
            if not url:
                continue
            hits.append(
                SearchHit(
                    url=url,
                    title=str(row.get("title", "")),
                    snippet=str(row.get("description", "")),
                )
            )
            if len(hits) >= self.max_results:
                break
        # 凭据绝不进结果、日志或快照
        return hits, ""


# --- 快照 -------------------------------------------------------------


def safe_segment(value: str, *, fallback: str = "item") -> str:
    """把任意字符串变成安全的单段文件名。

    只保留字母数字与 `-_.`，其余替换成 `-`；空结果用 fallback；
    长度截断到 40。**绝不**让用户输入直接参与路径拼接。
    """

    text = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "")).strip("-.")
    text = text[:40]
    return text or fallback


class SnapshotStore:
    """把联网材料落盘到 `<run_dir>/snapshots/web/`。

    三种内容分开存、分开标哈希，避免"正文哈希冒充原始网页哈希"：

    | 文件 | 内容 | 哈希字段 |
    |---|---|---|
    | `<name>.raw` | 原始响应字节 | `raw_sha256` |
    | `<name>.txt` | 解码后的正文（抽取后的文本） | `text_sha256` |
    | `<name>.json` | 元数据与两个哈希 | — |

    写入策略：**不覆盖**已有文件（同名时自动加序号）；先写 `.tmp` 再 `os.replace`，
    避免半截文件被当成完整快照。
    """

    def __init__(self, run_dir: Path, *, subdir: str = "snapshots/web") -> None:
        self.root = Path(run_dir) / subdir

    @staticmethod
    def check_run_id(run_id: str) -> str:
        """运行目录只允许由**校验过**的编号定位（与 runio 同一套规则）。"""

        from ..runio import RUN_ID_PATTERN

        text = str(run_id or "").strip()
        if not RUN_ID_PATTERN.match(text):
            raise WebRetrievalError(f"运行编号不合法：{run_id!r}")
        return text

    def allocate(self, base: str) -> str:
        self.root.mkdir(parents=True, exist_ok=True)
        stem = safe_segment(base, fallback="page")
        for index in range(1, 1000):
            name = stem if index == 1 else f"{stem}-{index}"
            if not (self.root / f"{name}.json").exists():
                return name
        raise WebRetrievalError("快照编号已用尽")

    def save(self, result: WebFetchResult, *, base: str = "") -> WebFetchResult:
        """保存快照，回填文件名与元数据路径。返回同一个对象（便于链式使用）。"""

        name = self.allocate(base or urllib.parse.urlsplit(result.url).netloc or "page")
        raw_path = self.root / f"{name}.raw"
        text_path = self.root / f"{name}.txt"
        meta_path = self.root / f"{name}.json"

        if result.raw_bytes:
            _atomic_write_bytes(raw_path, bytes(result.raw_bytes))
        _atomic_write_text(text_path, result.text)
        meta = {
            "snapshot": name,
            "url": result.url,
            "final_url": result.final_url,
            "status": result.status,
            "http_status": result.http_status,
            "content_type": result.content_type,
            "charset": result.charset,
            "title": result.title,
            "fetched_at": result.fetched_at,
            "redirects": list(result.redirects),
            "truncated": result.truncated,
            "error": result.error,
            # 哈希对应哪种内容必须写明：raw=原始响应字节（可能被截断），
            # text=清洗后的正文（UTF-8 编码后）。不写清就是拿正文哈希冒充网页哈希。
            "hashes": {
                "raw_sha256": result.raw_sha256,
                "raw_scope": f"原始响应字节前 {len(result.raw_bytes)} 字节"
                + ("（已按上限截断）" if result.truncated else ""),
                "text_sha256": result.text_sha256,
                "text_scope": "抽取并清洗后的正文，UTF-8 编码",
            },
            "files": {
                "raw": raw_path.name if result.raw_bytes else "",
                "text": text_path.name,
                "meta": meta_path.name,
            },
        }
        _atomic_write_text(
            meta_path, json.dumps(meta, ensure_ascii=False, indent=2)
        )
        result.snapshot_path = f"{self.root.name}/{raw_path.name}" if result.raw_bytes else ""
        result.snapshot_meta_path = f"{self.root.name}/{meta_path.name}"
        return result

    def load_meta(self, name: str) -> dict[str, Any]:
        """读回一份快照的元数据，供人工复核与后续复现使用。"""

        path = self.root / f"{safe_segment(name, fallback='page')}.json"
        if not path.exists():
            raise FileNotFoundError(f"快照不存在：{name}")
        return json.loads(path.read_text(encoding="utf-8"))


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _atomic_write_text(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def load_web_config(root: Path) -> dict[str, Any]:
    """读联网配置。缺文件时用默认值，不阻断工作台。"""

    path = Path(root) / "config" / "web_retrieval.json"
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def build_fetcher(config: Mapping[str, Any] | None = None, **overrides: Any) -> WebFetcher:
    config = config or {}
    kwargs = {
        "user_agent": str(config.get("user_agent") or "ContentEvidenceAgent/0.3"),
        "timeout_s": float(config.get("timeout_s") or DEFAULT_TIMEOUT_S),
        "max_redirects": int(config.get("max_redirects") or DEFAULT_MAX_REDIRECTS),
        "max_bytes": int(config.get("max_bytes") or DEFAULT_MAX_BYTES),
        "max_text_chars": int(config.get("max_text_chars") or DEFAULT_MAX_TEXT_CHARS),
        "allow_hosts": tuple(config.get("allow_hosts") or ()),
        "deny_hosts": tuple(config.get("deny_hosts") or ()),
    }
    kwargs.update(overrides)
    return WebFetcher(**kwargs)


def build_search_provider(
    config: Mapping[str, Any] | None = None, **overrides: Any
) -> SearchProvider:
    config = config or {}
    search = dict(config.get("search") or {})
    kwargs = {
        "provider": str(search.get("provider") or "brave"),
        "api_key_env": str(search.get("api_key_env") or "BRAVE_SEARCH_API_KEY"),
        "endpoint": str(search.get("endpoint") or ""),
        "max_results": int(search.get("max_results") or 5),
        "timeout_s": float(search.get("timeout_s") or DEFAULT_TIMEOUT_S),
    }
    kwargs.update(overrides)
    return SearchProvider(**kwargs)
