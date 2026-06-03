from __future__ import annotations

import base64
import dataclasses
import datetime as dt
import os
import re
from typing import Any, Iterable

import requests

SPREADSHEET_ID = os.getenv(
    "GOOGLE_SHEETS_SPREADSHEET_ID",
    "1wOqqEtElzHyxOQLfmGcjbqNxQXWcJmtNOxJwk5s2M_o",
)
LEDGER_SHEET_NAME = "台帳管理"
LOG_SHEET_NAME = "自動取込ログ"
RMS_SEARCH_ENDPOINT = "/es/2.0/order/searchOrder/"
RMS_GET_ENDPOINT = "/es/2.0/order/getOrder/"
RMS_BASE = os.getenv("RMS_API_BASE", "https://api.rms.rakuten.co.jp")
SHEETS_BASE = os.getenv("GOOGLE_SHEETS_API_BASE", "https://sheets.googleapis.com/v4")
LOOKBACK_DAYS = 31
ORDER_PROGRESS = [300]
RETAIL_URL = "https://item.rakuten.co.jp/trenditemshop/{item_number}/?variantId=00"
AMAZON_URL = "https://www.amazon.co.jp/dp/{asin}"


@dataclasses.dataclass(frozen=True)
class LedgerRow:
    order_date: dt.datetime | None
    item_name: str
    item_number: str
    order_number: str
    unit_price: int
    quantity: int
    sales_amount: int
    ship_family: str
    ship_first: str
    prefecture: str
    asin: str = ""


def jst_now() -> dt.datetime:
    run_at = os.getenv("RUN_AT_ISO8601")
    if run_at:
        parsed = dt.datetime.fromisoformat(run_at)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone(dt.timedelta(hours=9)))
        return parsed.astimezone(dt.timezone(dt.timedelta(hours=9)))
    return dt.datetime.now(dt.timezone(dt.timedelta(hours=9)))


def esa_auth_header() -> str:
    secret = os.environ["RMS_SERVICE_SECRET"]
    license_key = os.environ["RMS_LICENSE_KEY"]
    token = base64.b64encode(f"{secret}:{license_key}".encode("utf-8")).decode("ascii")
    return f"ESA {token}"


def rms_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "Authorization": esa_auth_header(),
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
    )
    return session


def sheets_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


def request_json(
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    params: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
    timeout: int = 60,
) -> Any:
    response = requests.request(method, url, headers=headers, params=params, json=body, timeout=timeout)
    response.raise_for_status()
    if not response.text.strip():
        return {}
    return response.json()


def rms_post(session: requests.Session, path: str, body: dict[str, Any]) -> Any:
    return request_json("POST", f"{RMS_BASE}{path}", headers=session.headers, body=body)


def sheets_get(token: str, path: str, params: dict[str, Any] | None = None) -> Any:
    return request_json("GET", f"{SHEETS_BASE}{path}", headers=sheets_headers(token), params=params)


def sheets_post(token: str, path: str, body: dict[str, Any]) -> Any:
    return request_json("POST", f"{SHEETS_BASE}{path}", headers=sheets_headers(token), body=body)


def quote_range(sheet_name: str, range_a1: str) -> str:
    return f"{sheet_name.replace("'", "''")}!{range_a1}"


def spreadsheet_meta(token: str) -> dict[str, Any]:
    return sheets_get(token, f"/spreadsheets/{SPREADSHEET_ID}", {"includeGridData": "false"})


def sheet_id_of(spreadsheet: dict[str, Any], title: str) -> int | None:
    for sheet in spreadsheet.get("sheets", []):
        props = sheet.get("properties", {})
        if props.get("title") == title:
            return int(props["sheetId"])
    return None


def ensure_sheet(token: str, spreadsheet: dict[str, Any], title: str) -> int:
    found = sheet_id_of(spreadsheet, title)
    if found is not None:
        return found
    result = sheets_post(
        token,
        f"/spreadsheets/{SPREADSHEET_ID}:batchUpdate",
        {"requests": [{"addSheet": {"properties": {"title": title}}}]},
    )
    return int(result["replies"][0]["addSheet"]["properties"]["sheetId"])


def values_get(token: str, sheet_name: str, range_a1: str) -> list[list[Any]]:
    result = sheets_get(
        token,
        f"/spreadsheets/{SPREADSHEET_ID}/values/{quote_range(sheet_name, range_a1)}",
        {"valueRenderOption": "FORMATTED_VALUE"},
    )
    return result.get("values", [])


def existing_order_numbers(token: str) -> set[str]:
    rows = values_get(token, LEDGER_SHEET_NAME, "E:E")
    existing: set[str] = set()
    for row in rows[2:]:
        if row and str(row[0]).strip():
            existing.add(str(row[0]).strip())
    return existing


def search_orders(session: requests.Session, start: dt.datetime, end: dt.datetime) -> list[str]:
    payload = {
        "dateType": 1,
        "orderProgressList": ORDER_PROGRESS,
        "startDatetime": start.isoformat(),
        "endDatetime": end.isoformat(),
    }
    data = rms_post(session, RMS_SEARCH_ENDPOINT, payload)
    return [str(value).strip() for value in (data.get("orderNumberList") or []) if str(value).strip()]


def get_orders(session: requests.Session, order_numbers: list[str]) -> list[dict[str, Any]]:
    details: list[dict[str, Any]] = []
    for index in range(0, len(order_numbers), 100):
        chunk = order_numbers[index : index + 100]
        data = rms_post(session, RMS_GET_ENDPOINT, {"orderNumberList": chunk})
        details.extend(data.get("OrderModelList") or data.get("orderModelList") or [])
    return details


def first_value(obj: Any, *keys: str) -> Any:
    if isinstance(obj, dict):
        for key in keys:
            if key in obj and obj[key] not in (None, ""):
                return obj[key]
        for value in obj.values():
            result = first_value(value, *keys)
            if result not in (None, ""):
                return result
    elif isinstance(obj, list):
        for value in obj:
            result = first_value(value, *keys)
            if result not in (None, ""):
                return result
    return None


def parse_dt(value: Any) -> dt.datetime | None:
    if not value:
        return None
    if isinstance(value, dt.datetime):
        return value
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone(dt.timedelta(hours=9)))
    return parsed


def as_int(value: Any) -> int:
    if value is None or value == "":
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    try:
        return int(float(str(value).strip()))
    except ValueError:
        return 0


def serial_date(value: dt.datetime | None) -> float:
    if value is None:
        return 0.0
    local = value.astimezone(dt.timezone(dt.timedelta(hours=9)))
    base = dt.datetime(1899, 12, 30, tzinfo=dt.timezone(dt.timedelta(hours=9)))
    return (local - base).total_seconds() / 86400


def escape_formula(text: str) -> str:
    return text.replace('"', '""')


def is_asin(value: str) -> bool:
    return bool(re.fullmatch(r"B[A-Z0-9]{9}", value.upper()))


def rows_from_order(order: dict[str, Any]) -> list[LedgerRow]:
    order_number = str(first_value(order, "orderNumber") or "").strip()
    order_date = parse_dt(first_value(order, "orderDatetime", "orderDate", "orderTime", "orderAcceptedDatetime"))
    orderer = first_value(order, "ordererModel") or {}
    if not isinstance(orderer, dict):
        orderer = {}
    ship_family = str(first_value(orderer, "familyName") or "").strip()
    ship_first = str(first_value(orderer, "firstName") or "").strip()
    prefecture = str(first_value(orderer, "prefecture") or "").strip()

    packages = first_value(order, "PackageModelList", "packageModelList") or []
    if not isinstance(packages, list):
        packages = [packages]
    if not packages:
        packages = [{}]

    rows: list[LedgerRow] = []
    for package in packages:
        if not isinstance(package, dict):
            package = {}
        package_goods = as_int(package.get("goodsPrice") or package.get("totalPrice"))
        items = package.get("ItemModelList") or package.get("itemModelList") or []
        if not isinstance(items, list) or not items:
            items = [package]
        for item in items:
            if not isinstance(item, dict):
                item = {}
            item_name = str(first_value(item, "itemName") or "").strip()
            item_number = str(first_value(item, "itemNumber") or "").strip()
            unit_price = as_int(first_value(item, "price"))
            quantity = as_int(first_value(item, "units")) or 1
            sales_amount = package_goods or unit_price * quantity
            asin = item_number.upper() if is_asin(item_number) else ""
            rows.append(
                LedgerRow(
                    order_date=order_date,
                    item_name=item_name,
                    item_number=item_number,
                    order_number=order_number,
                    unit_price=unit_price,
                    quantity=quantity,
                    sales_amount=sales_amount,
                    ship_family=ship_family,
                    ship_first=ship_first,
                    prefecture=prefecture,
                    asin=asin,
                )
            )
    return rows


def format_ledger_rows(rows: list[LedgerRow]) -> list[dict[str, Any]]:
    formatted: list[dict[str, Any]] = []
    for row in rows:
        formatted.append(
            {
                "values": [
                    {"userEnteredValue": {"numberValue": serial_date(row.order_date)}},
                    {
                        "userEnteredValue": {
                            "formulaValue": (
                                f'=HYPERLINK("{RETAIL_URL.format(item_number=row.item_number)}","{escape_formula(row.item_name)}")'
                                if row.item_number
                                else row.item_name
                            )
                        }
                    },
                    {"userEnteredValue": {"stringValue": row.item_number}},
                    {"userEnteredValue": {"stringValue": row.order_number}},
                    {"userEnteredValue": {"numberValue": row.unit_price}},
                    {"userEnteredValue": {"numberValue": row.quantity}},
                    {"userEnteredValue": {"numberValue": row.sales_amount}},
                    {"userEnteredValue": {"stringValue": row.ship_family}},
                    {"userEnteredValue": {"stringValue": row.ship_first}},
                    {"userEnteredValue": {"stringValue": row.prefecture}},
                ]
            }
        )
    return formatted


def format_asin_rows(rows: list[LedgerRow]) -> list[dict[str, Any]]:
    formatted: list[dict[str, Any]] = []
    for row in rows:
        if row.asin:
            formatted.append(
                {
                    "values": [
                        {
                            "userEnteredValue": {
                                "formulaValue": f'=HYPERLINK("{AMAZON_URL.format(asin=row.asin)}","{row.asin}")'
                            }
                        }
                    ]
                }
            )
        else:
            formatted.append({"values": []})
    return formatted


def build_append_requests(sheet_id: int, start_row_index: int, rows: list[LedgerRow]) -> list[dict[str, Any]]:
    source_row = max(start_row_index - 1, 0)
    ledger_rows = format_ledger_rows(rows)
    asin_rows = format_asin_rows(rows)
    requests: list[dict[str, Any]] = [
        {
            "copyPaste": {
                "source": {
                    "sheetId": sheet_id,
                    "startRowIndex": source_row,
                    "endRowIndex": source_row + 1,
                    "startColumnIndex": 1,
                    "endColumnIndex": 11,
                },
                "destination": {
                    "sheetId": sheet_id,
                    "startRowIndex": start_row_index,
                    "endRowIndex": start_row_index + len(rows),
                    "startColumnIndex": 1,
                    "endColumnIndex": 11,
                },
                "pasteType": "PASTE_FORMAT",
            }
        },
        {
            "copyPaste": {
                "source": {
                    "sheetId": sheet_id,
                    "startRowIndex": source_row,
                    "endRowIndex": source_row + 1,
                    "startColumnIndex": 15,
                    "endColumnIndex": 16,
                },
                "destination": {
                    "sheetId": sheet_id,
                    "startRowIndex": start_row_index,
                    "endRowIndex": start_row_index + len(rows),
                    "startColumnIndex": 15,
                    "endColumnIndex": 16,
                },
                "pasteType": "PASTE_FORMAT",
            }
        },
        {
            "updateCells": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": start_row_index,
                    "endRowIndex": start_row_index + len(rows),
                    "startColumnIndex": 1,
                    "endColumnIndex": 11,
                },
                "rows": ledger_rows,
                "fields": "userEnteredValue",
            }
        },
    ]
    if any(row["values"] for row in asin_rows):
        requests.append(
            {
                "updateCells": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": start_row_index,
                        "endRowIndex": start_row_index + len(rows),
                        "startColumnIndex": 15,
                        "endColumnIndex": 16,
                    },
                    "rows": asin_rows,
                    "fields": "userEnteredValue",
                }
            }
        )
    return requests


def append_log(
    token: str,
    sheet_id: int,
    ts: dt.datetime,
    search_count: int,
    added: int,
    skipped: int,
    error: str,
) -> None:
    body = {
        "requests": [
            {
                "appendCells": {
                    "sheetId": sheet_id,
                    "rows": [
                        {
                            "values": [
                                {"userEnteredValue": {"stringValue": ts.isoformat(sep=" ", timespec="seconds")}},
                                {"userEnteredValue": {"numberValue": search_count}},
                                {"userEnteredValue": {"numberValue": added}},
                                {"userEnteredValue": {"numberValue": skipped}},
                                {"userEnteredValue": {"stringValue": error}},
                            ]
                        }
                    ],
                    "fields": "userEnteredValue",
                }
            }
        ]
    }
    sheets_post(token, f"/spreadsheets/{SPREADSHEET_ID}:batchUpdate", body)


def next_available_row(token: str) -> int:
    values = values_get(token, LEDGER_SHEET_NAME, "E:E")
    return max(len(values), 2) + 1


def main() -> int:
    token = os.environ["GOOGLE_SHEETS_ACCESS_TOKEN"]
    run_at = jst_now()
    spreadsheet = spreadsheet_meta(token)
    ledger_sheet_id = ensure_sheet(token, spreadsheet, LEDGER_SHEET_NAME)
    log_sheet_id = ensure_sheet(token, spreadsheet, LOG_SHEET_NAME)

    search_count = 0
    added_count = 0
    skipped_count = 0

    try:
        session = rms_session()
        start = run_at - dt.timedelta(days=LOOKBACK_DAYS)
        order_numbers = search_orders(session, start, run_at)
        search_count = len(order_numbers)

        if not order_numbers:
            append_log(token, log_sheet_id, run_at, search_count, 0, 0, "")
            print("added=0 error=none")
            return 0

        existing = existing_order_numbers(token)
        new_order_numbers = [number for number in order_numbers if number not in existing]
        skipped_count = search_count - len(new_order_numbers)
        if not new_order_numbers:
            append_log(token, log_sheet_id, run_at, search_count, 0, skipped_count, "")
            print("added=0 error=none")
            return 0

        order_details = get_orders(session, new_order_numbers)
        rows: list[LedgerRow] = []
        for order in order_details:
            rows.extend(rows_from_order(order))

        if not rows:
            raise RuntimeError("No rows extracted from RMS order payload")

        next_row = next_available_row(token)
        body = {"requests": build_append_requests(ledger_sheet_id, max(next_row - 1, 0), rows)}
        sheets_post(token, f"/spreadsheets/{SPREADSHEET_ID}:batchUpdate", body)
        added_count = len(rows)
        append_log(token, log_sheet_id, run_at, search_count, added_count, skipped_count, "")
        print(f"added={added_count} error=none")
        return 0
    except Exception as exc:  # noqa: BLE001
        try:
            append_log(token, log_sheet_id, run_at, search_count, added_count, skipped_count, str(exc))
        except Exception:
            pass
        print(f"added={added_count} error=yes")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
