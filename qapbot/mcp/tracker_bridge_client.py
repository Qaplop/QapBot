"""Thin async HTTP client for the tracker MCP server against qapbot/web_bridge.py's
`/api/tracker/*` endpoints (BUG_FEATURE_TRACKER_PLAN.md Phase 7, §6.4). No new dependency —
aiohttp is already a runtime requirement via web_bridge.py itself.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import aiohttp


class TrackerBridgeError(RuntimeError):
    """Raised for any non-200 bridge response — message is the bridge's own `error` field."""


class TrackerBridgeClient:
    def __init__(self, base_url: str, secret: str, admin_label: str):
        self.base_url = base_url.rstrip("/")
        self.secret = secret
        self.admin_label = admin_label

    def _headers(self) -> Dict[str, str]:
        return {"X-Bridge-Secret": self.secret, "X-Tracker-Admin": self.admin_label}

    async def _get(self, path: str, params: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{self.base_url}{path}", params=params, headers=self._headers()) as resp:
                body = await resp.json()
                if resp.status != 200:
                    raise TrackerBridgeError(body.get("error", f"HTTP {resp.status}"))
                return body

    async def _post(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        async with aiohttp.ClientSession() as session:
            async with session.post(f"{self.base_url}{path}", json=payload, headers=self._headers()) as resp:
                body = await resp.json()
                if resp.status != 200:
                    raise TrackerBridgeError(body.get("error", f"HTTP {resp.status}"))
                return body

    async def list_items(
        self, status: Optional[str] = None, item_type: Optional[str] = None, limit: int = 50
    ) -> List[Dict[str, Any]]:
        params: Dict[str, str] = {"limit": str(limit)}
        if status:
            params["status"] = status
        if item_type:
            params["type"] = item_type
        body = await self._get("/api/tracker/items", params=params)
        return body["items"]

    async def get_item(self, item_number: int) -> Dict[str, Any]:
        return await self._get(f"/api/tracker/items/{item_number}")

    async def create_item(
        self, item_type: str, title: str, description: str,
        details: Optional[str] = None, environment: Optional[str] = None, priority: Optional[str] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"item_type": item_type, "title": title, "description": description}
        if details:
            payload["details"] = details
        if environment:
            payload["environment"] = environment
        if priority:
            payload["priority"] = priority
        return await self._post("/api/tracker/items", payload)

    async def get_attachment_bytes(self, item_number: int, attachment_id: int) -> bytes:
        async with aiohttp.ClientSession() as session:
            url = f"{self.base_url}/api/tracker/items/{item_number}/attachments/{attachment_id}"
            async with session.get(url, headers=self._headers()) as resp:
                if resp.status != 200:
                    raise TrackerBridgeError(await resp.text())
                return await resp.read()

    async def set_status(self, item_number: int, status: str, note: Optional[str] = None) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"status": status}
        if note:
            payload["note"] = note
        return await self._post(f"/api/tracker/items/{item_number}/status", payload)

    async def comment(self, item_number: int, text: str) -> Dict[str, Any]:
        return await self._post(f"/api/tracker/items/{item_number}/comment", {"text": text})

    async def reply_and_invite(self, item_number: int, text: str) -> Dict[str, Any]:
        return await self._post(f"/api/tracker/items/{item_number}/reply-and-invite", {"text": text})

    async def get_thread(self, item_number: int, limit: int = 50) -> List[Dict[str, Any]]:
        body = await self._get(f"/api/tracker/items/{item_number}/thread", params={"limit": str(limit)})
        return body["messages"]

    async def add_testcases(self, item_number: int, cases: List[Dict[str, str]]) -> Dict[str, Any]:
        return await self._post(f"/api/tracker/items/{item_number}/testcases", {"cases": cases})

    async def mark_testcase_passed(self, item_number: int, environment: str) -> Dict[str, Any]:
        return await self._post(f"/api/tracker/items/{item_number}/testcases/pass", {"environment": environment})

    async def mark_testcase_failed(self, item_number: int) -> Dict[str, Any]:
        return await self._post(f"/api/tracker/items/{item_number}/testcases/fail", {})

    async def mark_testcase_result(
        self, item_number: int, testcase_id: int, result: str, note: Optional[str] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"result": result}
        if note:
            payload["note"] = note
        return await self._post(f"/api/tracker/items/{item_number}/testcases/{testcase_id}/result", payload)

    async def move_testcases_done(self, item_number: int, force: bool = False) -> Dict[str, Any]:
        return await self._post(f"/api/tracker/items/{item_number}/testcases/move-done", {"force": force})
