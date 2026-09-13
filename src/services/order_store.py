"""订单内存仓库：从 CSV 加载，支持热重载。"""

from __future__ import annotations

import csv
import io
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger()

_REQUIRED_COLUMNS = {"order_id"}
_OPTIONAL_COLUMNS = {
    "email",
    "status",
    "destination_country",
    "currency",
    "total",
    "tracking_number",
    "carrier",
    "created_at",
    "items",
}


def _parse_items(raw: str) -> list[dict[str, Any]]:
    """解析 items 列，支持「商品名 x2; 另一商品 x1」或纯文本。"""
    text = (raw or "").strip()
    if not text:
        return []
    items: list[dict[str, Any]] = []
    for part in re.split(r"[;；|]", text):
        part = part.strip()
        if not part:
            continue
        m = re.match(r"^(.*?)\s*[x×\*]\s*(\d+)\s*$", part, re.IGNORECASE)
        if m:
            items.append({"name": m.group(1).strip(), "quantity": int(m.group(2))})
        else:
            items.append({"name": part, "quantity": 1})
    return items


def _normalize_row(row: dict[str, str]) -> dict[str, Any] | None:
    cleaned = { (k or "").strip().lower(): (v or "").strip() for k, v in row.items() if k }
    order_id = cleaned.get("order_id", "")
    if not order_id:
        return None
    total_raw = cleaned.get("total", "")
    try:
        total = float(total_raw) if total_raw else None
    except ValueError:
        total = total_raw or None
    email = cleaned.get("email", "").lower()
    data: dict[str, Any] = {
        "order_id": order_id,
        "email": email,
        "status": cleaned.get("status") or "Unknown",
        "destination_country": cleaned.get("destination_country") or "",
        "currency": cleaned.get("currency") or "USD",
        "total": total if total is not None else 0,
        "tracking_number": cleaned.get("tracking_number") or "",
        "carrier": cleaned.get("carrier") or "",
        "created_at": cleaned.get("created_at") or "",
        "items": _parse_items(cleaned.get("items", "")),
        "_source": "csv",
    }
    return data


@dataclass
class OrderStore:
    """按 order_id / email 索引的订单仓库。"""

    by_id: dict[str, dict[str, Any]] = field(default_factory=dict)
    by_email: dict[str, list[str]] = field(default_factory=dict)
    files: list[str] = field(default_factory=list)
    loaded_at: str | None = None
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def clear(self) -> None:
        with self._lock:
            self.by_id.clear()
            self.by_email.clear()
            self.files.clear()
            self.loaded_at = None

    def _index_order(self, order: dict[str, Any]) -> None:
        oid = order["order_id"]
        old = self.by_id.get(oid)
        if old:
            old_email = (old.get("email") or "").lower()
            if old_email:
                bucket = self.by_email.get(old_email)
                if bucket and oid in bucket:
                    bucket.remove(oid)
                if bucket is not None and not bucket:
                    del self.by_email[old_email]
        self.by_id[oid] = order
        email = (order.get("email") or "").lower()
        if email:
            bucket = self.by_email.setdefault(email, [])
            if oid not in bucket:
                bucket.append(oid)

    def merge_order_dict(self, order: dict[str, Any]) -> None:
        """合并单条订单进内存索引。"""
        if not order.get("order_id"):
            return
        with self._lock:
            self._index_order(dict(order))
            self.loaded_at = datetime.utcnow().isoformat() + "Z"

    def load_directory(self, directory: Path) -> int:
        """扫描目录下全部 .csv，全量重建内存索引。返回订单条数。"""
        directory.mkdir(parents=True, exist_ok=True)
        with self._lock:
            self.by_id.clear()
            self.by_email.clear()
            self.files = []
            for path in sorted(directory.glob("*.csv")):
                self._load_file_unlocked(path)
            self.loaded_at = datetime.utcnow().isoformat() + "Z"
            count = len(self.by_id)
        logger.info("order_store_loaded", files=len(self.files), orders=count, dir=str(directory))
        return count

    def merge_csv_bytes(self, content: bytes, *, filename: str = "upload.csv") -> dict[str, Any]:
        """解析 CSV 并合并。返回导入报告（含预览、跳过行）。"""
        text = content.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text))
        if not reader.fieldnames:
            raise ValueError("CSV 缺少表头")
        headers = {h.strip().lower() for h in reader.fieldnames if h}
        if not _REQUIRED_COLUMNS.issubset(headers):
            raise ValueError("CSV 必须包含 order_id 列")

        imported = 0
        overwritten = 0
        skipped: list[dict[str, Any]] = []
        preview: list[dict[str, Any]] = []
        imported_orders: list[dict[str, Any]] = []

        with self._lock:
            for idx, row in enumerate(reader, start=2):
                order = _normalize_row(row)
                if not order:
                    skipped.append({"row": idx, "reason": "缺少 order_id"})
                    continue
                if order["order_id"] in self.by_id:
                    overwritten += 1
                self._index_order(order)
                imported += 1
                imported_orders.append(dict(order))
                if len(preview) < 8:
                    preview.append(
                        {
                            "order_id": order["order_id"],
                            "email": order.get("email") or "",
                            "status": order.get("status") or "",
                            "total": order.get("total"),
                            "tracking_number": order.get("tracking_number") or "",
                        }
                    )
            if filename not in self.files:
                self.files.append(filename)
            self.loaded_at = datetime.utcnow().isoformat() + "Z"

        report = {
            "imported": imported,
            "overwritten": overwritten,
            "skipped": skipped[:50],
            "skipped_count": len(skipped),
            "preview": preview,
            "orders": imported_orders,
        }
        logger.info(
            "order_store_merged",
            filename=filename,
            imported=imported,
            overwritten=overwritten,
            skipped=len(skipped),
            total=len(self.by_id),
        )
        return report
    def _load_file_unlocked(self, path: Path) -> None:
        text = path.read_text(encoding="utf-8-sig")
        reader = csv.DictReader(io.StringIO(text))
        if not reader.fieldnames:
            return
        headers = {h.strip().lower() for h in reader.fieldnames if h}
        if not _REQUIRED_COLUMNS.issubset(headers):
            logger.warning("order_csv_skip_missing_order_id", file=path.name)
            return
        for row in reader:
            order = _normalize_row(row)
            if order:
                self._index_order(order)
        self.files.append(path.name)

    def get(self, order_id: str) -> dict[str, Any] | None:
        with self._lock:
            data = self.by_id.get(order_id)
            return dict(data) if data else None

    def list_by_email(self, email: str) -> list[dict[str, Any]]:
        key = (email or "").lower()
        with self._lock:
            ids = list(self.by_email.get(key, []))
            return [dict(self.by_id[i]) for i in ids if i in self.by_id]

    def list_all(self, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock:
            orders = list(self.by_id.values())
        orders.sort(key=lambda o: o.get("created_at") or "", reverse=True)
        return [dict(o) for o in orders[:limit]]

    def find_by_tracking(self, tracking_number: str) -> dict[str, Any] | None:
        key = (tracking_number or "").strip()
        if not key:
            return None
        with self._lock:
            for order in self.by_id.values():
                if (order.get("tracking_number") or "").strip() == key:
                    return dict(order)
        return None

    def update_order(self, order_id: str, updates: dict[str, Any]) -> dict[str, Any] | None:
        with self._lock:
            existing = self.by_id.get(order_id)
            if not existing:
                return None
            merged = dict(existing)
            for k, v in updates.items():
                if v is None:
                    continue
                if k == "order_id" and v != order_id:
                    # rename handled separately
                    continue
                merged[k] = v
            new_id = updates.get("order_id") or order_id
            if new_id != order_id:
                # remove old index
                old_email = (existing.get("email") or "").lower()
                if old_email and order_id in self.by_email.get(old_email, []):
                    self.by_email[old_email].remove(order_id)
                    if not self.by_email[old_email]:
                        del self.by_email[old_email]
                del self.by_id[order_id]
                merged["order_id"] = new_id
            self._index_order(merged)
            self.loaded_at = datetime.utcnow().isoformat() + "Z"
            return dict(merged)

    def delete_order(self, order_id: str) -> bool:
        with self._lock:
            existing = self.by_id.pop(order_id, None)
            if not existing:
                return False
            email = (existing.get("email") or "").lower()
            if email and order_id in self.by_email.get(email, []):
                self.by_email[email].remove(order_id)
                if not self.by_email[email]:
                    del self.by_email[email]
            self.loaded_at = datetime.utcnow().isoformat() + "Z"
            return True

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "order_count": len(self.by_id),
                "email_count": len(self.by_email),
                "files": list(self.files),
                "loaded_at": self.loaded_at,
            }


_store = OrderStore()


def get_order_store() -> OrderStore:
    return _store


def init_order_store(directory: Path) -> OrderStore:
    """启动时加载目录；目录为空则写入示例 CSV。"""
    directory.mkdir(parents=True, exist_ok=True)
    if not any(directory.glob("*.csv")):
        sample = directory / "sample_orders.csv"
        sample.write_text(_SAMPLE_CSV, encoding="utf-8")
        logger.info("order_sample_csv_created", path=str(sample))
    _store.load_directory(directory)
    return _store


_SAMPLE_CSV = """order_id,email,status,destination_country,currency,total,tracking_number,carrier,created_at,items
ORD-1001,demo@gulf.ae,Shipped,AE,AED,1299.00,DHL987654321,DHL Express,2024-07-01 10:00:00,智能手机 x1
ORD-1002,demo@gulf.ae,Delivered,SA,SAR,899.00,ARX123456789,Aramex,2024-06-15 14:30:00,无线耳机 x1
ORD-2001,alice@example.com,Processing,CN,CNY,4599.00,,SF Express,2024-07-10 09:00:00,笔记本电脑 x1
"""
