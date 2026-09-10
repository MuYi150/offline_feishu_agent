from __future__ import annotations

import json
import time
from urllib.parse import quote

import httpx

from .config import FeishuSettings
from .records import STATUSES, timestamp


class FeishuError(RuntimeError):
    def __init__(self, code: str, *, uncertain: bool = False):
        super().__init__(code)
        self.code = code
        self.uncertain = uncertain


class FeishuGateway:
    """Bounded read retries. Message writes are never automatically replayed."""

    def __init__(self, settings: FeishuSettings, *, client=None, sleeper=time.sleep):
        self.settings = settings
        self.client = client or httpx.Client(timeout=settings.http_timeout)
        self.sleep = sleeper
        self.access_token = ""
        self.expires_at = 0.0
        self._sheets = {}
        self._users = {}

    def close(self):
        self.client.close()

    def _authenticate(self):
        if self.access_token and time.monotonic() < self.expires_at:
            return
        try:
            response = self.client.post(self.settings.api_base_url.rstrip("/") + "/auth/v3/tenant_access_token/internal",
                                        json={"app_id": self.settings.app_id, "app_secret": self.settings.app_secret})
            data = response.json()
            if response.status_code >= 400 or data.get("code") != 0 or not data.get("tenant_access_token"):
                raise FeishuError("authentication_failed")
        except (httpx.HTTPError, ValueError) as exc:
            raise FeishuError("authentication_failed") from exc
        self.access_token = data["tenant_access_token"]
        self.expires_at = time.monotonic() + max(1, int(data.get("expire", 7200)) - 60)

    def request(self, method, path, *, params=None, body=None, binary=False, message=False):#获取飞书访问凭证
        self._authenticate()
        attempts = 1 if message or method != "GET" else self.settings.http_max_retries + 1
        for attempt in range(attempts):
            try:
                response = self.client.request(method, self.settings.api_base_url.rstrip("/") + path,
                                               params=params, json=body,
                                               headers={"Authorization": f"Bearer {self.access_token}"})
            except httpx.HTTPError as exc:
                if attempt + 1 < attempts:
                    self.sleep(min(2 ** attempt, 8))
                    continue
                raise FeishuError("transport_error", uncertain=method != "GET") from exc
            if response.status_code == 429 or response.status_code >= 500:
                if attempt + 1 < attempts:
                    self.sleep(min(2 ** attempt, 8))
                    continue
                raise FeishuError(f"http_{response.status_code}", uncertain=message and response.status_code >= 500)
            if binary and response.status_code < 400:
                return response.content
            try:
                data = response.json()
            except ValueError as exc:
                raise FeishuError("invalid_response", uncertain=method != "GET") from exc
            if response.status_code >= 400 or data.get("code", 0) != 0:
                raise FeishuError(f"feishu_{data.get('code', response.status_code)}")
            return data.get("data", {})
        raise FeishuError("request_failed")

    def node(self, token):
        raw = self.request("GET", "/wiki/v2/spaces/get_node", params={"token": token}).get("node")
        if not raw:
            raise FeishuError("node_missing")
        return self.normalize_node(raw)

    @staticmethod
    def normalize_node(raw):
        return dict(raw, edit_time=timestamp(raw.get("obj_edit_time") or raw.get("node_edit_time") or raw.get("edit_time")),
                    owner=raw.get("owner") or raw.get("owner_id") or raw.get("creator") or raw.get("creator_id") or "")

    def _pages(self, path, params, key="items"):
        items, seen = [], set()
        while True:
            data = self.request("GET", path, params=params)
            items.extend(data.get(key) or (data.get("nodes") if key == "items" else None) or [])
            if not data.get("has_more"):
                return items
            cursor = data.get("page_token") or data.get("next_page_token")
            if not cursor or cursor in seen:
                raise FeishuError("invalid_pagination")
            seen.add(cursor)
            params = dict(params, page_token=cursor)

    def children(self, parent):
        return [self.normalize_node(n) for n in self._pages(
            f"/wiki/v2/spaces/{parent['space_id']}/nodes", {"parent_node_token": parent["node_token"], "page_size": 50})]

    def sheet(self, kind):
        if kind not in self._sheets:
            wiki_token = getattr(self.settings, f"{kind}_wiki_node_token")
            node = self.node(wiki_token)
            if node.get("obj_type") != "sheet":
                raise FeishuError("configured_node_not_sheet")
            obj = node["obj_token"]
            sheets = self.request("GET", f"/sheets/v3/spreadsheets/{obj}/sheets/query").get("sheets", [])
            name = getattr(self.settings, f"{kind}_sheet_name")
            found = next((s for s in sheets if s.get("title") == name), None)
            if not found:
                raise FeishuError("configured_sheet_not_found")
            self._sheets[kind] = (obj, found["sheet_id"])
        return self._sheets[kind]

    def read_values(self, obj, sheet_id, range_a1):
        data = self.request("GET", f"/sheets/v2/spreadsheets/{obj}/values/{quote(sheet_id + '!' + range_a1, safe='')}")
        return (data.get("valueRange") or data.get("value_range") or data).get("values") or []

    def read_sheet(self, kind):
        obj, sheet_id = self.sheet(kind)
        return self.read_embedded_sheet(obj, sheet_id, end_column="N" if kind == "review" else "D")

    def read_embedded_sheet(self, obj, sheet_id, end_column=None):
        sheets = self.request("GET", f"/sheets/v3/spreadsheets/{obj}/sheets/query").get("sheets", [])
        sheet = next((s for s in sheets if s.get("sheet_id") == sheet_id), None)
        if not sheet:
            raise FeishuError("embedded_sheet_missing")
        grid = sheet.get("grid_properties") or {}
        row_count = int(grid.get("row_count", 0))
        columns = int(grid.get("column_count", 0))
        if row_count < 1 or columns < 1:
            raise FeishuError("sheet_dimensions_missing")
        if end_column is None:
            end_column = ""
            while columns:
                columns, remainder = divmod(columns - 1, 26)
                end_column = chr(65 + remainder) + end_column
        rows = []
        for start in range(1, row_count + 1, 1000):
            end = min(row_count, start + 999)
            values = self.read_values(obj, sheet_id, f"A{start}:{end_column}{end}")
            rows.extend(values + [[] for _ in range(end - start + 1 - len(values))])
        while rows and not any(v not in (None, "", []) for v in rows[-1]):
            rows.pop()
        return rows

    def read_bitable(self, app_token, table_id, view_id=None):
        fields = self._pages(f"/bitable/v1/apps/{app_token}/tables/{table_id}/fields", {"page_size": 100})
        names = [f["field_name"] for f in fields]
        params = {"page_size": 500}
        if view_id:
            params["view_id"] = view_id
        records = self._pages(f"/bitable/v1/apps/{app_token}/tables/{table_id}/records", params)
        return [names] + [[r.get("fields", {}).get(name, "") for name in names] for r in records]

    def update_cells(self, kind, row, updates):
        if kind == "review" and any(c not in "ABCDEFGHIJKLMN" or len(c) != 1 for c in updates):
            raise ValueError("禁止新增表格列")
        obj, sheet_id = self.sheet(kind)
        self.request("POST", f"/sheets/v2/spreadsheets/{obj}/values_batch_update", body={"valueRanges": [
            {"range": f"{sheet_id}!{col}{row}:{col}{row}", "values": [[value]]} for col, value in updates.items()]})

    def insert_row(self, row, values):
        if len(values) != 14:
            raise ValueError("新行必须包含 A:N")
        obj, sheet_id = self.sheet("review")
        self.request("PUT", f"/sheets/v2/spreadsheets/{obj}/values", body={"valueRange": {
            "range": f"{sheet_id}!A{row}:N{row}", "values": [values]}})

    def repair_format(self, row, template_row=None):
        obj, sheet_id = self.sheet("review")
        for col, defaults in (("F", ["人工", "AI", "人工+AI"]), ("H", STATUSES)):
            values, options = list(defaults), {"multipleValues": False, "highlightValidData": True}
            if template_row:
                data = self.request("GET", f"/sheets/v2/spreadsheets/{obj}/dataValidation", params={
                    "range": f"{sheet_id}!{col}{template_row}:{col}{template_row}", "dataValidationType": "list"})
                validations = data.get("dataValidations") or []
                if validations:
                    original = validations[0].get("dataValidation") or validations[0].get("data_validation") or validations[0]
                    values = list(dict.fromkeys([*original.get("conditionValues", []), *defaults]))
                    options.update(original.get("options", {}))
                    colors = options.get("colors")
                    if isinstance(colors, list):
                        options["colors"] = (colors + ["#ffffff"] * len(values))[:len(values)]
            self.request("POST", f"/sheets/v2/spreadsheets/{obj}/dataValidation", body={
                "range": f"{sheet_id}!{col}{row}:{col}{row}", "dataValidationType": "list",
                "dataValidation": {"conditionValues": values, "options": options}})
        self.request("PUT", f"/sheets/v2/spreadsheets/{obj}/styles_batch_update", body={"data": [
            {"ranges": [f"{sheet_id}!A{row}:N{row}"], "style": {"backColor": "#ffffff", "foreColor": "#000000"}},
            {"ranges": [f"{sheet_id}!J{row}:L{row}"], "style": {"formatter": "yyyy/MM/dd HH:mm:ss"}},
            {"ranges": [f"{sheet_id}!I{row}:I{row}"], "style": {"formatter": "0"}}]})

    def user(self, owner):
        if isinstance(owner, dict):
            owner = owner.get("open_id") or owner.get("user_id") or owner.get("id") or ""
        owner = str(owner or "")
        if not owner:
            return {"open_id": "", "name": ""}
        if owner not in self._users:
            kind = "open_id" if owner.startswith("ou_") else "union_id" if owner.startswith("on_") else "user_id"
            data = self.request("GET", f"/contact/v3/users/{quote(owner, safe='')}", params={"user_id_type": kind})
            self._users[owner] = data.get("user") or {}
        return self._users[owner]

    def blocks(self, obj):
        return self._pages(f"/docx/v1/documents/{obj}/blocks", {"page_size": 500})

    def raw_content(self, obj):
        return str(self.request("GET", f"/docx/v1/documents/{obj}/raw_content").get("content") or "")

    def export_pdf(self, obj, max_bytes):
        task = self.request("POST", "/drive/v1/export_tasks", body={"file_extension": "pdf", "token": obj, "type": "docx"})
        ticket = task.get("ticket")
        if not ticket:
            raise FeishuError("export_ticket_missing")
        deadline = time.monotonic() + self.settings.export_timeout
        while time.monotonic() < deadline:
            data = self.request("GET", f"/drive/v1/export_tasks/{ticket}", params={"token": obj})
            result = data.get("result") or data
            status = result.get("job_status", result.get("status"))
            if str(status).lower() in {"0", "success", "done"} and result.get("file_token"):
                content = self.request("GET", f"/drive/v1/export_tasks/file/{result['file_token']}/download", binary=True)
                if len(content) > max_bytes or not content.startswith(b"%PDF-"):
                    raise FeishuError("export_invalid_pdf_or_size")
                return content
            if str(status).lower() not in {"none", "1", "2", "pending", "running", "processing"}:
                raise FeishuError("export_failed")
            self.sleep(2)
        raise FeishuError("export_timeout")

    def send_message(self, recipient, content, message_key):
        if not recipient.startswith("ou_"):
            raise FeishuError("recipient_open_id_missing")
        data = self.request("POST", "/im/v1/messages", params={"receive_id_type": "open_id"}, message=True,
                            body={"receive_id": recipient, "msg_type": "text",
                                  "content": json.dumps({"text": content}, ensure_ascii=False), "uuid": message_key[:50]})
        message_id = data.get("message_id")
        if not message_id:
            raise FeishuError("message_receipt_missing", uncertain=True)
        return message_id
