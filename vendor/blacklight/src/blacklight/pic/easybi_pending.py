from __future__ import annotations

import json
import random
import re
import uuid
from datetime import date, timedelta
from typing import Any

from blacklight.core import BlacklightError
from blacklight.easybi.auth import client


DASHBOARD_CODE = "43AEDF1183513A70246F75C9623BB14F9828F25A8F0392CA62BB3273AF6C1B21"
APP_ID = 1024285
DETAIL_TITLE = "明细表"
QUERY_HOSTS = [f"https://eb-{index}.jd.com" for index in range(5)]
MONTHS = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}


def _walk(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _normalize_date(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return date.fromisoformat(text[:10]).isoformat()
    except ValueError:
        pass
    match = re.fullmatch(
        r"[A-Za-z]{3}\s+([A-Za-z]{3})\s+(\d{1,2})\s+\d{2}:\d{2}:\d{2}\s+"
        r"(?:CST|GMT(?:\+|%2B)?0800|GMT\+08:00)\s+(\d{4})",
        text,
    )
    if match and match.group(1).title() in MONTHS:
        return date(int(match.group(3)), MONTHS[match.group(1).title()], int(match.group(2))).isoformat()
    raise BlacklightError(f"EasyBI 返回无法识别的日期格式：{text[:80]}")


def _dashboard_parts(session) -> tuple[dict, dict, dict]:
    response = session.get(
        "https://jdp.jd.com/api/app/devcenter/detail",
        params={"appId": APP_ID, "code": DASHBOARD_CODE},
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("code") != 200 or not payload.get("data"):
        raise BlacklightError(f"EasyBI 看板配置读取失败：{payload.get('msg') or payload.get('code')}")
    data = payload["data"]
    setting = json.loads(data["setting"]) if isinstance(data.get("setting"), str) else data.get("setting")
    nodes = list(_walk((setting or {}).get("layout") or []))
    widget = next((item for item in nodes if item.get("title") == DETAIL_TITLE and not item.get("isControl")), None)
    control = next((item for item in nodes if item.get("isControl") and any(
        field.get("caption") == "SPUID" for field in (item.get("targetDimensionFields") or [])
    )), None)
    if not widget or not control:
        raise BlacklightError("EasyBI 带图评价看板结构已变化：找不到明细表或筛选组")
    required = {"日期", "销售员ERP", "SPUID", "是否带图评价标识"}
    measures = {item.get("caption") for item in (widget.get("targetMeasureFields") or [])}
    if not required.issubset(measures):
        raise BlacklightError(f"EasyBI 明细字段已变化：缺少 {sorted(required - measures)}")
    return data, widget, control


def _field_desc(field: dict) -> dict:
    return {
        "caption": field.get("caption") or field.get("field"),
        "field": field.get("field"),
        "fieldId": field.get("id") or 0,
        "fieldType": field.get("fieldType") or "VARCHAR",
        "tableId": field.get("tableId") or "",
    }


def _filter_desc(field: dict, value: str | None, target_date: str) -> dict:
    desc = _field_desc(field)
    item_type = str(field.get("qgItemType") or "select").lower()
    if item_type == "datepicker":
        next_date = (date.fromisoformat(target_date) + timedelta(days=1)).isoformat()
        filters = [
            {"logicSymbol": "AND", "operator": "GREATER_EQUAL", "value": target_date, "valueType": "VALUE"},
            {"logicSymbol": "AND", "operator": "LESS", "value": next_date, "valueType": "VALUE"},
        ]
        return {**desc, "filters": filters, "filterType": "DATEPICKER", "dateType": "date",
                "isExceptionFilter": False}
    filter_type = "INPUT" if item_type == "input" else "SELECT"
    filters = [] if value in (None, "") else [
        {"logicSymbol": "AND", "operator": "EQUAL", "value": [str(value)], "valueType": "VALUE"}
    ]
    return {**desc, "filters": filters, "filterType": filter_type, "dateType": "",
            "isExceptionFilter": False}


def _query_body(widget: dict, control: dict, erp: str, target_date: str, limit: int) -> dict:
    measures = []
    for index, field in enumerate(widget.get("targetMeasureFields") or widget.get("measureFields") or []):
        measures.append({
            "aggregationType": field.get("aggregationType") or field.get("aggregator"),
            "calcField": bool(field.get("calcField", False)),
            "caption": field.get("caption") or field.get("field"),
            "field": field.get("field"),
            "fieldId": field.get("id", index),
            "fieldType": field.get("fieldType") or "double",
            "displayType": field.get("displayType") or {"numberFormat": {}, "dateFormat": "", "type": 0},
        })
    values = {"日期_day": target_date, "销售员ERP": erp, "是否带图评价标识": "否"}
    filter_descs = []
    for field in control.get("targetDimensionFields") or []:
        caption = field.get("qgCaption") or field.get("caption") or field.get("field")
        filter_descs.append(_filter_desc(field, values.get(caption), target_date))
    return {
        "data": {
            "dimensions": [],
            "measures": measures,
            "filterDescs": filter_descs,
            "sortDescs": [],
            "topNDesc": {"limit": int(limit)},
            "alg": {"trends": None, "predictModelId": None, "clustering": None},
        },
        "model": {
            "databaseId": int(widget.get("activeDBId") or 0),
            "dataSourceId": widget.get("activeDatasourceId") or "",
        },
        "attribute": {
            "appId": APP_ID,
            "modeCode": DASHBOARD_CODE,
            "componentId": widget.get("i") or widget.get("id"),
            "title": DETAIL_TITLE,
            "clientSource": "user-view",
            "clientChannel": "web-pc",
            "requestId": str(uuid.uuid4()),
            "batchId": str(uuid.uuid4()),
            "cache": True,
            "clearCache": False,
            "startMinutes": 480,
            "expire": -1,
        },
        "chartDesc": {"chartType": widget.get("chartType", 1), "unusedDatePicker": []},
        "page": False,
        "replace": True,
        "format": True,
        "excelConfig": None,
    }


def _clean(value: Any) -> Any:
    if isinstance(value, dict):
        for key in ("value", "formattedValue", "displayValue", "label"):
            if key in value:
                return value[key]
    return value


def _parse_rows(data: Any) -> list[dict]:
    if not data:
        return []
    if isinstance(data.get("titleList"), list):
        return [{str(key): _clean(value) for key, value in row.items()} for row in data["titleList"]]
    if isinstance(data.get("tableData"), list):
        columns = [item.get("key") or item.get("title") or item.get("name") or ""
                   for item in (data.get("tableTitle") or [])]
        rows = []
        for row in data["tableData"]:
            if isinstance(row, list):
                rows.append({columns[index] or f"col{index}": _clean(value)
                             for index, value in enumerate(row)})
            elif isinstance(row, dict):
                rows.append({str(key): _clean(value) for key, value in row.items()})
        return rows
    dimensions = data.get("dimensions") or []
    measures = data.get("measures") or []
    count = max([len(item.get("data") or []) for item in dimensions + measures] or [0])
    rows = []
    for index in range(count):
        row = {}
        for item in dimensions + measures:
            values = item.get("data") or []
            row[item.get("name") or item.get("caption") or item.get("field")] = _clean(
                values[index] if index < len(values) else ""
            )
        rows.append(row)
    return rows


def _query_date(session, widget: dict, control: dict, erp: str, target_date: str, limit: int) -> list[dict]:
    body = _query_body(widget, control, erp, target_date, limit)
    hosts = list(QUERY_HOSTS)
    random.shuffle(hosts)
    last_error = ""
    for host in hosts[:3]:
        try:
            response = session.post(
                host + "/api/engine/queryForApp",
                data={"queryParams": json.dumps(body, ensure_ascii=False, separators=(",", ":"))},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=60,
            )
            response.raise_for_status()
            payload = response.json()
            if payload.get("code") == 200:
                return _parse_rows(payload.get("data") or {})
            last_error = f"{payload.get('code')}: {payload.get('msg')}"
        except Exception as exc:
            last_error = str(exc)
    raise BlacklightError(f"EasyBI 明细查询失败：{last_error[:240]}")


def pending_spus(erp: str, target_date: str | None = None, limit: int = 5000,
                 fallback_days: int = 1) -> dict:
    erp = str(erp or "").strip().lower()
    if not erp:
        raise BlacklightError("erp 不能为空")
    if limit < 1 or limit > 5000:
        raise BlacklightError("limit 必须在 1~5000")
    initial = date.fromisoformat(target_date) if target_date else date.today() - timedelta(days=1)
    session = client()
    _, widget, control = _dashboard_parts(session)
    chosen_date = ""
    rows = []
    for offset in range(max(0, int(fallback_days)) + 1):
        chosen_date = (initial - timedelta(days=offset)).isoformat()
        rows = _query_date(session, widget, control, erp, chosen_date, limit)
        if rows:
            break
    clean_rows = []
    seen = set()
    for row in rows:
        normalized = {str(key).strip(): value for key, value in row.items() if key is not None}
        row_erp = str(normalized.get("销售员ERP") or "").strip().lower()
        row_date = _normalize_date(normalized.get("日期"))
        flag = str(normalized.get("是否带图评价标识") or "").strip()
        spu = str(normalized.get("SPUID") or "").strip()
        if row_erp and row_erp != erp:
            raise BlacklightError(f"EasyBI 混入其他销售员 ERP：{row_erp}")
        if row_date and row_date != chosen_date:
            raise BlacklightError(f"EasyBI 混入其他日期：{row_date}")
        if flag and flag != "否":
            raise BlacklightError(f"EasyBI 混入已完成带图评价：SPU {spu}")
        if spu and spu not in seen:
            seen.add(spu)
            clean_rows.append(normalized)
    return {
        "erp": erp,
        "date": chosen_date,
        "fallback_used": chosen_date != initial.isoformat(),
        "count": len(clean_rows),
        "spu_ids": [str(row.get("SPUID")) for row in clean_rows],
        "rows": clean_rows,
        "source": "EasyBI 带图评价数据看板/明细表",
        "dashboard_app_id": APP_ID,
    }
