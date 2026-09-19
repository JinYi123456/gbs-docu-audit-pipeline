"""桥接官方 loader.py（不复制源码，保持单一真相）。

官方 loader 同时支持静态目录与 HTTP 两种源：
    Inbox("data")                    # 本地静态包
    Inbox("http://localhost:8080")   # 官方 Docker 服务
本模块优先用 importlib 动态加载 <data_dir>/loader.py；若不存在（例如只跑 HTTP 模式），
退回等价的纯 stdlib 实现，保证两种源都可用。
"""
from __future__ import annotations

import importlib.util
import json
import os
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

DEFAULT_SOURCE: str = os.environ.get("INBOX_SOURCE", "data")
DEFAULT_DATA_DIR: str = os.environ.get("INBOX_DATA_DIR", "data")


class InboxError(RuntimeError):
    pass


@dataclass(slots=True, frozen=True)
class InboxEmail:
    email_id: str
    sender: str
    subject: str
    body: str
    attachments: tuple[str, ...]
    raw: dict[str, Any]

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> "InboxEmail":
        return cls(
            email_id=str(raw["email_id"]),
            sender=str(raw.get("from") or ""),
            subject=str(raw.get("subject") or ""),
            body=str(raw.get("body") or ""),
            attachments=tuple(raw.get("attachments") or ()),
            raw=raw,
        )

    @property
    def attachment_count(self) -> int:
        return len(self.attachments)

    def attachment_kinds(self) -> set[str]:
        """附件槽位集合：{'SI'} / {'BL'} / {'SI','BL'} / set()"""
        kinds = set()
        for path in self.attachments:
            stem = path.rsplit("/", 1)[-1].rsplit(".", 1)[0].upper()
            tail = stem.rsplit("_", 1)[-1]
            if tail in {"SI", "BL"}:
                kinds.add(tail)
        return kinds


class InboxSource:
    """官方 loader.py 的适配器。"""

    def __init__(self, source: str | None = None, *, data_dir: str | Path | None = None) -> None:
        self.source = (source or DEFAULT_SOURCE).rstrip("/")
        self.data_dir = Path(data_dir or DEFAULT_DATA_DIR)
        self.is_http = self.source.startswith(("http://", "https://"))
        self._official: Any | None = None
        self._loader_checked = False

    # -- 官方 loader 装载 -----------------------------------------------------
    def _official_loader(self) -> Any | None:
        if self._loader_checked:
            return self._official
        self._loader_checked = True
        if self.is_http:
            return None
        loader_path = self.data_dir / "loader.py"
        if not loader_path.is_file():
            return None
        spec = importlib.util.spec_from_file_location("sdoc_official_loader", loader_path)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self._official = module.Inbox(str(self.data_dir))
        return self._official

    # -- 列举 ----------------------------------------------------------------
    def emails(self) -> list[InboxEmail]:
        official = self._official_loader()
        if official is not None:
            return [InboxEmail.from_raw(raw) for raw in official.emails()]
        if self.is_http:
            payload = json.loads(self._http_get(self.source + "/emails"))
        else:
            inbox_dir = self.data_dir / "inbox"
            if not inbox_dir.is_dir():
                raise InboxError(
                    f"找不到 {inbox_dir}。先跑 `make bootstrap` 解出官方 data/ 目录，"
                    "或用 INBOX_SOURCE=http://localhost:8080 走 Docker。"
                )
            payload = [json.loads(path.read_text(encoding="utf-8"))
                       for path in sorted(inbox_dir.glob("email_*.json"))]
        return [InboxEmail.from_raw(raw) for raw in payload]

    def __iter__(self) -> Iterator[InboxEmail]:
        return iter(self.emails())

    def get(self, email_id: str) -> InboxEmail:
        official = self._official_loader()
        if official is not None:
            return InboxEmail.from_raw(official.get(email_id))
        if self.is_http:
            return InboxEmail.from_raw(
                json.loads(self._http_get(f"{self.source}/emails/{email_id}")))
        path = self.data_dir / "inbox" / f"{email_id}.json"
        if not path.is_file():
            raise InboxError(f"没有这封邮件：{email_id}")
        return InboxEmail.from_raw(json.loads(path.read_text(encoding="utf-8")))

    # -- 附件 ----------------------------------------------------------------
    def read_bytes(self, attachment_path: str) -> bytes:
        official = self._official_loader()
        if official is not None:
            return official.read_bytes(attachment_path)
        if self.is_http:
            return self._http_get(self.source + "/" + attachment_path.lstrip("/"))
        return (self.data_dir / attachment_path).read_bytes()

    # -- 提交 ----------------------------------------------------------------
    def submit(self, submission: dict[str, Any]) -> dict[str, Any]:
        if not self.is_http:
            raise InboxError("submit() 需要 HTTP 源（先 docker compose up --build）")
        request = urllib.request.Request(
            self.source + "/submit",
            data=json.dumps(submission).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request) as response:
            return json.loads(response.read())

    def sample_submission(self) -> dict[str, Any]:
        if self.is_http:
            return json.loads(self._http_get(self.source + "/sample_submission"))
        return json.loads((self.data_dir / "sample_submission.json").read_text(encoding="utf-8"))

    # -- HTTP 助手 -----------------------------------------------------------
    @staticmethod
    def _http_get(url: str) -> bytes:
        with urllib.request.urlopen(url) as response:
            return response.read()
