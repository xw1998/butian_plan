"""MCP / CLI 的 stdio 编解码：显式 UTF-8，不跟随 Windows 本地代码页。

Windows 上管道里的 `sys.stdin` / `sys.stdout` 默认是本地 ANSI 代码页（简中系统为
cp936/gbk）配 `errors="surrogateescape"`，而 MCP 宿主一律按 UTF-8 说话：

* 宿主发来的 UTF-8 中文被当 GBK 解成乱码（成员名、任务标题在返回里都是乱码）；
* 乱码再被回传时解出的孤立代理字符（U+DC80–U+DCFF），一旦哪里 `.encode("utf-8")`
  就直接抛 `UnicodeEncodeError: surrogates not allowed`，中文一提交就失败。

这里把字节层交给 `sys.stdin.buffer` / `sys.stdout.buffer`，编码固定 UTF-8：
不依赖宿主有没有传 `PYTHONUTF8` / `PYTHONIOENCODING`，也不会被 locale 改写。
"""

from __future__ import annotations

import json
import sys

# 读入时只认 UTF-8；GB18030 是 GBK 的超集，作为老宿主的兼容回退（能被严格解码才用）。
INPUT_ENCODINGS = ("utf-8", "gb18030")


class InputEncodingError(ValueError):
    """请求字节既不是 UTF-8 也不是 GB18030：宁可报错，也不猜成别的字符。"""

    def __init__(self, detail: str):
        super().__init__("input_not_utf8: " + detail)
        self.code = "input_not_utf8"
        self.detail = detail

    def details(self) -> dict:
        return {
            "code": self.code,
            "reason": self.detail,
            "next_actions": [
                {"tool": "butian_status",
                 "why": "确认服务端已就绪，再按 UTF-8 重新发送同一条请求。"},
            ],
        }


def has_lone_surrogate(text: str) -> bool:
    return any(0xD800 <= ord(char) <= 0xDFFF for char in text)


def repair_text(text: str) -> str:
    """把 surrogateescape 产生的孤立代理字符还原成原始字节对应的文本。

    这类字符只可能来自“把 UTF-8 字节按别的编码解”的往返：用 surrogateescape
    编码回字节再按 UTF-8 解码是可逆的。

    注意能力边界：宿主若用 GBK 解过我们的 UTF-8 字节，多数字节对会被解成“看着
    像中文的错字”（已经有损，无法还原），只有解不出来的字节才留下代理字符。
    这里能救的是纯代理字符那部分；真正的根治是两边都按 UTF-8 说话。
    """
    if not has_lone_surrogate(text):
        return text
    try:
        return text.encode("utf-8", "surrogateescape").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text


def repair_surrogates(value):
    """递归修复请求里的孤立代理字符（字符串/列表/字典）。"""
    if isinstance(value, str):
        return repair_text(value)
    if isinstance(value, list):
        return [repair_surrogates(item) for item in value]
    if isinstance(value, tuple):
        return tuple(repair_surrogates(item) for item in value)
    if isinstance(value, dict):
        return {repair_surrogates(key): repair_surrogates(item) for key, item in value.items()}
    return value


def decode_request_line(raw: bytes) -> str:
    """按 UTF-8 解码一行请求；失败时抛 InputEncodingError 而不是静默换编码。"""
    last: Exception | None = None
    for encoding in INPUT_ENCODINGS:
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError as exc:  # 记录最后一个失败，便于报错说明
            last = exc
    raise InputEncodingError(
        "请求字节既不是 UTF-8 也不是 GB18030（%s）；请让宿主以 UTF-8 发送 JSON-RPC 行。" % last)


def frame_bytes(payload) -> bytes:
    """把一帧响应编成 UTF-8 字节。

    正常情况保留可读中文；只有载荷里带着无法编码的孤立代理字符时才退回 ASCII
    转义（`\\udcXX` 仍是合法 JSON），避免整条响应因为一个坏字符丢掉。
    """
    text = json.dumps(payload, ensure_ascii=False)
    try:
        return text.encode("utf-8")
    except UnicodeEncodeError:
        return json.dumps(payload, ensure_ascii=True).encode("ascii")


def write_frame(payload) -> None:
    """写一帧到 stdout 的字节层：逐个 `\\n` 分隔，不受 locale 与换行翻译影响。"""
    data = frame_bytes(payload) + b"\n"
    buffer = getattr(sys.stdout, "buffer", None)
    if buffer is None:  # 测试/重定向里 stdout 可能没有字节层，退回文本流
        sys.stdout.write(data.decode("utf-8"))
        sys.stdout.flush()
        return
    buffer.write(data)
    buffer.flush()


def read_request_line() -> str | None:
    """读一行请求；EOF 返回 None，纯空白行返回空串（由调用方跳过）。"""
    buffer = getattr(sys.stdin, "buffer", None)
    if buffer is None:  # 同上：没有字节层时直接用文本流（已是解码后的 str）
        line = sys.stdin.readline()
        return None if line == "" else line
    raw = buffer.readline()
    if not raw:
        return None
    return decode_request_line(raw)
