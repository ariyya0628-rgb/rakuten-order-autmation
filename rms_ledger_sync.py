from __future__ import annotations

import base64
import dataclasses
import datetime as dt
import json
import os
import re
from typing import Any

import requests
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2 import service_account

DEFAULT_SPREADSHEET_ID = "1wOqqEtElzHyxOQLfmGcjbqNxQXWcJmtNOxJwk5s2M_o"
SPREADSHEET_ID = (os.getenv("GOOGLE_SHEETS_SPREADSHEET_ID") or DEFAULT_SPREADSHEET_ID).strip()
LEDGER_SHEET_NAME = "台帳管理"
LOG_SHEET_NAME = "自動取込ログ"
RMS_BASE = os.getenv("RMS_API_BASE", "https://api.rms.rakuten.co.jp").rstrip("/")
SHEETS_BASE = os.getenv("GOOGLE_SHEETS_API_BASE", "https://sheets.googleapis.com/v4").rstrip("/")
RMS_SEARCH_ENDPOINT = "/es/2.0/order/searchOrder/"
RMS_GET_ENDPOINT = "/es/2.0/order/getOrder/"
GOOGLE_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
JST = dt.timezone(dt.timedelta(hours=9))
LOOKBACK_DAYS = 31
MAX_SEARCH_WINDOW_DAYS = 15
ORDER_PROGRESS = [100, 200, 300, 400]
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
    value = os.getenv("RUN_AT_ISO8601")
    if value:
        parsed = dt.datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=JST)
        return parsed.astimezone(JST)
    return dt.datetime.now(JST)


def rms_auth_header() -> str:
    service_secret = os.environ["RMS_SERVICE_SECRET"].strip()
    license_key = os.environ["RMS_LICENSE_KEY"].strip()
    token = base64.b64encode(f"{service_secret}:{license_key}".encode()).decode().rstrip("=")
    return f"ESA {token}"


def rms_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "Authorization": rms_auth_header(),
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json",
        }
    )
    return session


def google_token() -> str:
    raw = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not raw:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON is missing")
    try:
        info = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON is invalid. Paste only the JSON text from { to }.") from exc
    credentials = service_account.Credentials.from_service_account_info(info, scopes=[GOOGLE_SCOPE])
    credentials.refresh(GoogleAuthRequest())
    if not credentials.token:
        raise RuntimeError("Google service account token refresh returned no token")
    return credentials.token


def request_json(
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    params: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
) -> Any:
    try:
        response = requests.request(method, url, headers=headers, params=params, json=body, timeout=60)
    except requests.RequestException as exc:
        raise RuntimeError(f"{method} {url} failed before response: {exc}") from exc
    if response.status_code >= 400:
        detail = response.text.strip().replace("\n", " ")[:1200]
        raise RuntimeError(f"{method} {response.url} returned HTTP {response.status_code}: {detail}")
    if not response.text.strip():
        return {}
    return response.json()


def sheets_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


def sheets_get(token: str, path: str, params: dict[str, Any] | None = None) -> Any:
    return request_json("GET", f"{SHEETS_BASE}{path}", headers=sheets_headers(token), params=params)


def sheets_post(token: str, path: str, body: dict[str, Any]) -> Any:
    return request_json("POST", f"{SHEETS_BASE}{path}", headers=sheets_headers(token), body=body)


def rms_post(session: requests.Session, path: str, body: dict[str, Any]) -> Any:
    return request_json("POST", f"{RMS_BASE}{path}", headers=session.headers, body=body)


def quote_range(sheet: str, a1: str) -> str:
    return f"{sheet.replace(chr(39), chr(39) + chr(39))}!{a1}"


def spreadsheet_meta(token: str) -> dict[str, Any]:
    return sheets_get(token, f"/spreadsheets/{SPREADSHEET_ID}", {"includeGridData": "false"})


def sheet_id(meta: dict[str, Any], title: str) -> int | None:
    for sheet in meta.get("sheets", []):
        props = sheet.get("properties", {})
        if props.get("title") == title:
            return int(props["sheetId"])
    return None


def ensure_sheet(token: str, meta: dict[str, Any], title: str) -> int:
    found = sheet_id(meta, title)
    if found is not None:
        return found
    result = sheets_post(
        token,
        f"/spreadsheets/{SPREADSHEET_ID}:batchUpdate",
        {"requests": [{"addSheet": {"properties": {"title": title}}}]},
    )
    return int(result["replies"][0]["addSheet"]["properties"]["sheetId"])


def values_get(token: str, sheet: str, a1: str) -> list[list[Any]]:
    result = sheets_get(
        token,
        f"/spreadsheets/{SPREADSHEET_ID}/values/{quote_range(sheet, a1)}",
        {"valueRenderOption": "FORMATTED_VALUE"},
    )
    return result.get("values", [])


def existing_order_numbers(token: str) -> set[str]:
    return {
        str(row[0]).strip()
        for row in values_get(token, LEDGER_SHEET_NAME, "E:E")[2:]
        if row and str(row[0]).strip()
    }


def as_rms_datetime(value: dt.datetime) -> str:
    return value.astimezone(JST).strftime("%Y-%m-%dT%H:%M:%S%z")


def search_windows(start: dt.datetime, end: dt.datetime) -> list[tuple[dt.datetime, dt.datetime]]:
    local_start = start.astimezone(JST)
    local_end = end.astimezone(JST).replace(microsecond=0)
    if local_start > local_end:
        return []

    windows: list[tuple[dt.datetime, dt.datetime]] = []
    cursor_day = local_start.date()
    end_day = local_end.date()
    while cursor_day <= end_day:
        chunk_end_day = min(cursor_day + dt.timedelta(days=MAX_SEARCH_WINDOW_DAYS - 1), end_day)
        window_start = dt.datetime.combine(cursor_day, dt.time.min, tzinfo=JST)
        if chunk_end_day == end_day:
            window_end = local_end
        else:
            window_end = dt.datetime.combine(chunk_end_day, dt.time(23, 59, 59), tzinfo=JST)
        windows.append((window_start, window_end))
        cursor_day = chunk_end_day + dt.timedelta(days=1)
    return list(reversed(windows))


def search_orders(session: requests.Session, start: dt.datetime, end: dt.datetime) -> list[str]:
    seen: dict[str, None] = {}
    for window_index, (window_start, window_end) in enumerate(search_windows(start, end), start=1):
        page = 1
        while True:
            payload = {
                "dateType": 1,
                "startDatetime": as_rms_datetime(window_start),
                "endDatetime": as_rms_datetime(window_end),
                "orderProgressList": ORDER_PROGRESS,
                "PaginationRequestModel": {"requestRecordsAmount": 1000, "requestPage": page},
            }
            print(
                f"searchOrder_window={window_index} page={page} "
                f"start={payload['startDatetime']} end={payload['endDatetime']}"
            )
            print(f"searchOrder_payload={json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}")
            data = rms_post(session, RMS_SEARCH_ENDPOINT, payload)
            for number in data.get("orderNumberList") or []:
                value = str(number).strip()
                if value:
                    seen[value] = None
            pagination = data.get("PaginationResponseModel") or data.get("paginationResponseModel") or {}
            total_pages = int(pagination.get("totalPages") or page)
            if page >= total_pages:
                break
            page += 1
    return list(seen.keys())


def get_orders(session: requests.Session, order_numbers: list[str]) -> list[dict[str, Any]]:
    orders: list[dict[str, Any]] = []
    for index in range(0, len(order_numbers), 100):
        data = rms_post(session, RMS_GET_ENDPOINT, {"orderNumberList": order_numbers[index : index + 100]})
        orders.extend(data.get("OrderModelList") or data.get("orderModelList") or [])
    return orders


def first_value(obj: Any, *keys: str) -> Any:
    if isinstance(obj, dict):
        for key in keys:
            if key in obj and obj[key] not in (None, ""):
                return obj[key]
        for value in obj.values():
            found = first_value(value, *keys)
            if found not in (None, ""):
                return found
    if isinstance(obj, list):
        for value in obj:
            found = first_value(value, *keys)
            if found not in (None, ""):
                return found
    return None


def parse_dt(value: Any) -> dt.datetime | None:
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=JST)
    return parsed


def as_int(value: Any) -> int:
    if value in (None, ""):
        return 0
    try:
        return int(float(str(value).strip()))
    except ValueError:
        return 0


def serial_date(value: dt.datetime | None) -> float:
    if value is None:
        return 0.0
    base = dt.datetime(1899, 12, 30, tzinfo=JST)
    return (value.astimezone(base.tzinfo) - base).total_seconds() / 86400


def escape_formula(value: str) -> str:
    return value.replace('"', '""')


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
    packages = first_value(order, "PackageModelList", "packageModelList") or [{}]
    if not isinstance(packages, list):
        packages = [packages]

    rows: list[LedgerRow] = []
    for package in packages:
        if not isinstance(package, dict):
            package = {}
        items = package.get("ItemModelList") or package.get("itemModelList") or [package]
        if not isinstance(items, list):
            items = [items]
        package_total = as_int(package.get("goodsPrice") or package.get("totalPrice"))
        for item in items:
            if not isinstance(item, dict):
                item = {}
            item_number = str(first_value(item, "itemNumber") or "").strip()
            unit_price = as_int(first_value(item, "price"))
            quantity = as_int(first_value(item, "units")) or 1
            rows.append(
                LedgerRow(
                    order_date=order_date,
                    item_name=str(first_value(item, "itemName") or "").strip(),
                    item_number=item_number,
                    order_number=order_number,
                    unit_price=unit_price,
                    quantity=quantity,
                    sales_amount=package_total or unit_price * quantity,
                    ship_family=ship_family,
                    ship_first=ship_first,
                    prefecture=prefecture,
                    asin=item_number.upper() if is_asin(item_number) else "",
                )
            )
    return rows


def cell_string(value: str) -> dict[str, Any]:
    return {"userEnteredValue": {"stringValue": value}}


def ledger_rows(rows: list[LedgerRow]) -> list[dict[str, Any]]:
    formatted: list[dict[str, Any]] = []
    for row in rows:
        if row.item_number:
            name_cell = {
                "userEnteredValue": {
                    "formulaValue": (
                        f'=HYPERLINK("{RETAIL_URL.format(item_number=row.item_number)}",'
                        f'"{escape_formula(row.item_name)}")'
                    )
                }
            }
        else:
            name_cell = cell_string(row.item_name)
        formatted.append(
            {
                "values": [
                    {"userEnteredValue": {"numberValue": serial_date(row.order_date)}},
                    name_cell,
                    cell_string(row.item_number),
                    cell_string(row.order_number),
                    {"userEnteredValue": {"numberValue": row.unit_price}},
                    {"userEnteredValue": {"numberValue": row.quantity}},
                    {"userEnteredValue": {"numberValue": row.sales_amount}},
                    cell_string(row.ship_family),
                    cell_string(row.ship_first),
                    cell_string(row.prefecture),
                ]
            }
        )
    return formatted


def asin_rows(rows: list[LedgerRow]) -> list[dict[str, Any]]:
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


def build_append_requests(sheet_id_value: int, start_row_index: int, rows: list[LedgerRow]) -> list[dict[str, Any]]:
    source_row = max(start_row_index - 1, 0)
    requests: list[dict[str, Any]] = [
        {
            "copyPaste": {
                "source": {
                    "sheetId": sheet_id_value,
                    "startRowIndex": source_row,
                    "endRowIndex": source_row + 1,
                    "startColumnIndex": 1,
                    "endColumnIndex": 11,
                },
                "destination": {
                    "sheetId": sheet_id_value,
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
                    "sheetId": sheet_id_value,
                    "startRowIndex": source_row,
                    "endRowIndex": source_row + 1,
                    "startColumnIndex": 15,
                    "endColumnIndex": 16,
                },
                "destination": {
                    "sheetId": sheet_id_value,
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
                    "sheetId": sheet_id_value,
                    "startRowIndex": start_row_index,
                    "endRowIndex": start_row_index + len(rows),
                    "startColumnIndex": 1,
                    "endColumnIndex": 11,
                },
                "rows": ledger_rows(rows),
                "fields": "userEnteredValue",
            }
        },
    ]
    asin = asin_rows(rows)
    if any(row["values"] for row in asin):
        requests.append(
            {
                "updateCells": {
                    "range": {
                        "sheetId": sheet_id_value,
                        "startRowIndex": start_row_index,
                        "endRowIndex": start_row_index + len(rows),
                        "startColumnIndex": 15,
                        "endColumnIndex": 16,
                    },
                    "rows": asin,
                    "fields": "userEnteredValue",
                }
            }
        )
    return requests


def append_log(
    token: str,
    log_sheet_id: int,
    run_at: dt.datetime,
    search_count: int,
    added: int,
    skipped: int,
    error: str,
) -> None:
    row = {
        "values": [
            cell_string(run_at.isoformat(sep=" ", timespec="seconds")),
            {"userEnteredValue": {"numberValue": search_count}},
            {"userEnteredValue": {"numberValue": added}},
            {"userEnteredValue": {"numberValue": skipped}},
            cell_string(error[:3000]),
        ]
    }
    sheets_post(
        token,
        f"/spreadsheets/{SPREADSHEET_ID}:batchUpdate",
        {"requests": [{"appendCells": {"sheetId": log_sheet_id, "rows": [row], "fields": "userEnteredValue"}}]},
    )


def next_available_row(token: str) -> int:
    return max(len(values_get(token, LEDGER_SHEET_NAME, "E:E")), 2) + 1


def main() -> int:
    token = google_token()
    run_at = jst_now()
    meta = spreadsheet_meta(token)
    ledger_sheet_id = ensure_sheet(token, meta, LEDGER_SHEET_NAME)
    log_sheet_id = ensure_sheet(token, meta, LOG_SHEET_NAME)
    search_count = added_count = skipped_count = 0
    try:
        session = rms_session()
        order_numbers = search_orders(session, run_at - dt.timedelta(days=LOOKBACK_DAYS), run_at)
        search_count = len(order_numbers)
        if not order_numbers:
            append_log(token, log_sheet_id, run_at, 0, 0, 0, "")
            print("added=0 error=none")
            return 0

        existing = existing_order_numbers(token)
        new_order_numbers = [number for number in order_numbers if number not in existing]
        skipped_count = search_count - len(new_order_numbers)
        if not new_order_numbers:
            append_log(token, log_sheet_id, run_at, search_count, 0, skipped_count, "")
            print("added=0 error=none")
            return 0

        rows: list[LedgerRow] = []
        for order in get_orders(session, new_order_numbers):
            rows.extend(rows_from_order(order))
        if not rows:
            raise RuntimeError("No ledger rows extracted from RMS order payload")

        start_row = max(next_available_row(token) - 1, 0)
        sheets_post(
            token,
            f"/spreadsheets/{SPREADSHEET_ID}:batchUpdate",
            {"requests": build_append_requests(ledger_sheet_id, start_row, rows)},
        )
        added_count = len(rows)
        append_log(token, log_sheet_id, run_at, search_count, added_count, skipped_count, "")
        print(f"added={added_count} error=none")
        return 0
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
        print(f"added={added_count} error=yes detail={error}")
        try:
            append_log(token, log_sheet_id, run_at, search_count, added_count, skipped_count, error)
        except Exception as log_exc:  # noqa: BLE001
            print(f"log_error={type(log_exc).__name__}: {log_exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
