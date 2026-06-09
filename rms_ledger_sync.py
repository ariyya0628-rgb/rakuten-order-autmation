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
RMS_PURCHASE_SEARCH_ENDPOINT = "/es/2.0/purchaseItem/searchOrderItem/"
RMS_GET_ENDPOINT = "/es/2.0/order/getOrder/"
RMS_LICENSE_ENDPOINT = "/es/1.0/license-management/license-key/expiry-date"
GOOGLE_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
JST = dt.timezone(dt.timedelta(hours=9))
LOOKBACK_DAYS = 63
MAX_SEARCH_WINDOW_DAYS = 15
ORDER_PROGRESS = [300]
RETAIL_URL = "https://item.rakuten.co.jp/trenditemshop/{item_number}/?variantId=00"
AMAZON_URL = "https://www.amazon.co.jp/dp/{asin}"
PREFECTURE_AREAS = {
    "北海道": "北海道",
    "青森県": "東北",
    "岩手県": "東北",
    "宮城県": "東北",
    "秋田県": "東北",
    "山形県": "東北",
    "福島県": "東北",
    "茨城県": "信越・関東",
    "栃木県": "信越・関東",
    "群馬県": "信越・関東",
    "埼玉県": "信越・関東",
    "千葉県": "信越・関東",
    "東京都": "信越・関東",
    "神奈川県": "信越・関東",
    "新潟県": "信越・関東",
    "山梨県": "信越・関東",
    "長野県": "信越・関東",
    "富山県": "北陸・中部",
    "石川県": "北陸・中部",
    "福井県": "北陸・中部",
    "岐阜県": "北陸・中部",
    "静岡県": "北陸・中部",
    "愛知県": "北陸・中部",
    "三重県": "北陸・中部",
    "滋賀県": "関西",
    "京都府": "関西",
    "大阪府": "関西",
    "兵庫県": "関西",
    "奈良県": "関西",
    "和歌山県": "関西",
    "鳥取県": "中国・四国",
    "島根県": "中国・四国",
    "岡山県": "中国・四国",
    "広島県": "中国・四国",
    "山口県": "中国・四国",
    "徳島県": "中国・四国",
    "香川県": "中国・四国",
    "愛媛県": "中国・四国",
    "高知県": "中国・四国",
    "福岡県": "九州",
    "佐賀県": "九州",
    "長崎県": "九州",
    "熊本県": "九州",
    "大分県": "九州",
    "宮崎県": "九州",
    "鹿児島県": "九州",
    "沖縄県": "沖縄",
}


def rms_secret_diagnostics() -> None:
    raw_service_secret = os.environ.get("RMS_SERVICE_SECRET", "")
    raw_license_key = os.environ.get("RMS_LICENSE_KEY", "")
    service_secret = raw_service_secret.strip()
    license_key = raw_license_key.strip()
    print(
        "rms_secret_check "
        f"service_raw_len={len(raw_service_secret)} "
        f"service_trim_len={len(service_secret)} "
        f"service_prefix_ok={'yes' if service_secret.startswith('SP') else 'no'} "
        f"service_has_quotes={'yes' if any(ch in raw_service_secret for ch in [chr(34), chr(39)]) else 'no'} "
        f"license_raw_len={len(raw_license_key)} "
        f"license_trim_len={len(license_key)} "
        f"license_prefix_ok={'yes' if license_key.startswith('SL') else 'no'} "
        f"license_has_quotes={'yes' if any(ch in raw_license_key for ch in [chr(34), chr(39)]) else 'no'}"
    )
    if not service_secret or not license_key:
        raise RuntimeError("RMS_SERVICE_SECRET or RMS_LICENSE_KEY is empty after trimming spaces")
    if not service_secret.startswith("SP") or not license_key.startswith("SL"):
        raise RuntimeError("RMS_SERVICE_SECRET should start with SP and RMS_LICENSE_KEY should start with SL")


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
    prefecture_area: str
    asin: str = ""


def jst_now() -> dt.datetime:
    value = os.getenv("RUN_AT_ISO8601")
    if value:
        parsed = dt.datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=JST)
        return parsed.astimezone(JST)
    return dt.datetime.now(JST)


def rms_auth_header(*, trim_padding: bool = False) -> str:
    service_secret = os.environ["RMS_SERVICE_SECRET"].strip()
    license_key = os.environ["RMS_LICENSE_KEY"].strip()
    token = base64.b64encode(f"{service_secret}:{license_key}".encode()).decode()
    if trim_padding:
        token = token.rstrip("=")
    return f"ESA {token}"


def rms_session(*, trim_auth_padding: bool = False) -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "Authorization": rms_auth_header(trim_padding=trim_auth_padding),
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
        safe_url = re.sub(r"(licenseKey=)[^&]+", r"\1***", response.url)
        raise RuntimeError(f"{method} {safe_url} returned HTTP {response.status_code}: {detail}")
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


def rms_get(session: requests.Session, path: str, params: dict[str, Any] | None = None) -> Any:
    return request_json("GET", f"{RMS_BASE}{path}", headers=session.headers, params=params)


def check_rms_license(session: requests.Session) -> None:
    license_key = os.environ["RMS_LICENSE_KEY"].strip()
    data = rms_get(session, RMS_LICENSE_ENDPOINT, {"licenseKey": license_key})
    expiry = data.get("expiryDate") if isinstance(data, dict) else None
    print(f"rms_license_check=ok expiryDate={expiry or 'unknown'}")


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


def rows_needing_recipient_backfill(token: str) -> dict[str, list[int]]:
    pending: dict[str, list[int]] = {}
    for row_index, row in enumerate(values_get(token, LEDGER_SHEET_NAME, "A:L")[2:], start=2):
        order_number = str(row[4]).strip() if len(row) > 4 else ""
        if not order_number:
            continue
        recipient_values = [str(row[index]).strip() if len(row) > index else "" for index in range(8, 12)]
        if not all(recipient_values):
            pending.setdefault(order_number, []).append(row_index)
    return pending


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


def search_payload(
    window_start: dt.datetime,
    window_end: dt.datetime,
    page: int,
    *,
    date_type: int = 1,
    include_progress: bool = True,
    include_sort: bool = False,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "dateType": date_type,
        "startDatetime": as_rms_datetime(window_start),
        "endDatetime": as_rms_datetime(window_end),
        "PaginationRequestModel": {"requestRecordsAmount": 1000, "requestPage": page},
    }
    if include_progress:
        payload["orderProgressList"] = ORDER_PROGRESS
    if include_sort:
        payload["PaginationRequestModel"]["SortModelList"] = [{"sortColumn": 1, "sortDirection": 1}]
    return payload


def post_search_orders(
    session: requests.Session,
    window_start: dt.datetime,
    window_end: dt.datetime,
    page: int,
    date_type: int = 1,
) -> Any:
    base_variants = [
        (
            f"dateType{date_type}_shipping_wait",
            search_payload(window_start, window_end, page, date_type=date_type, include_progress=True),
        ),
    ]
    variants = [
        (f"rpay.order.searchOrder.{label}", RMS_SEARCH_ENDPOINT, payload)
        for label, payload in base_variants
    ] + [
        (f"purchaseitem.searchOrderItem.{label}", RMS_PURCHASE_SEARCH_ENDPOINT, payload)
        for label, payload in base_variants
    ]
    last_error: RuntimeError | None = None
    for label, path, variant_payload in variants:
        print(f"searchOrder_variant={label}")
        print(f"searchOrder_payload={json.dumps(variant_payload, ensure_ascii=False, separators=(',', ':'))}")
        try:
            return rms_post(session, path, variant_payload)
        except RuntimeError as exc:
            last_error = exc
            print(f"searchOrder_variant_failed={label} detail={type(exc).__name__}: {exc}")
    if last_error is not None:
        raise last_error
    raise RuntimeError("No RMS search endpoint variants were configured")


def search_orders_by_date_type(session: requests.Session, start: dt.datetime, end: dt.datetime, date_type: int) -> list[str]:
    seen: dict[str, None] = {}
    for window_index, (window_start, window_end) in enumerate(search_windows(start, end), start=1):
        page = 1
        while True:
            payload = search_payload(window_start, window_end, page, date_type=date_type, include_progress=True)
            print(
                f"searchOrder_dateType={date_type} window={window_index} page={page} "
                f"start={payload['startDatetime']} end={payload['endDatetime']}"
            )
            data = post_search_orders(session, window_start, window_end, page, date_type=date_type)
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


def search_orders(session: requests.Session, start: dt.datetime, end: dt.datetime) -> list[str]:
    seen: dict[str, None] = {}
    for date_type in (1, 6):
        for number in search_orders_by_date_type(session, start, end, date_type):
            seen[number] = None
    return list(seen.keys())


def get_orders(session: requests.Session, order_numbers: list[str]) -> list[dict[str, Any]]:
    orders: list[dict[str, Any]] = []
    for index in range(0, len(order_numbers), 100):
        data = rms_post(session, RMS_GET_ENDPOINT, {"orderNumberList": order_numbers[index : index + 100], "version": 7})
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


def first_model(obj: Any, *keys: str) -> dict[str, Any]:
    found = first_value(obj, *keys)
    if isinstance(found, dict):
        return found
    return {}


def recipient_from_order(order: dict[str, Any], package: dict[str, Any]) -> tuple[str, str, str, str]:
    recipient = first_model(
        package,
        "SenderModel",
        "senderModel",
        "DeliveryModel",
        "deliveryModel",
        "ShippingModel",
        "shippingModel",
    )
    if not recipient:
        recipient = first_model(
            order,
            "SenderModel",
            "senderModel",
            "DeliveryModel",
            "deliveryModel",
            "ShippingModel",
            "shippingModel",
            "ordererModel",
        )
    family = str(first_value(recipient, "familyName", "lastName") or "").strip()
    first = str(first_value(recipient, "firstName", "givenName") or "").strip()
    prefecture = str(first_value(recipient, "prefecture") or "").strip()
    area = PREFECTURE_AREAS.get(prefecture, "")
    return family, first, prefecture, area


def rows_from_order(order: dict[str, Any]) -> list[LedgerRow]:
    order_number = str(first_value(order, "orderNumber") or "").strip()
    order_date = parse_dt(first_value(order, "orderDatetime", "orderDate", "orderTime", "orderAcceptedDatetime"))
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
        ship_family, ship_first, prefecture, prefecture_area = recipient_from_order(order, package)
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
                    prefecture_area=prefecture_area,
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
                    cell_string(row.prefecture_area),
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
                    "endColumnIndex": 12,
                },
                "destination": {
                    "sheetId": sheet_id_value,
                    "startRowIndex": start_row_index,
                    "endRowIndex": start_row_index + len(rows),
                    "startColumnIndex": 1,
                    "endColumnIndex": 12,
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
                    "endColumnIndex": 12,
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


def build_recipient_backfill_requests(
    sheet_id_value: int,
    pending_rows: dict[str, list[int]],
    orders: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    requests: list[dict[str, Any]] = []
    for order in orders:
        order_number = str(first_value(order, "orderNumber") or "").strip()
        if order_number not in pending_rows:
            continue
        packages = first_value(order, "PackageModelList", "packageModelList") or [{}]
        package = packages[0] if isinstance(packages, list) and packages else packages
        if not isinstance(package, dict):
            package = {}
        family, first, prefecture, area = recipient_from_order(order, package)
        row_data = {
            "values": [
                cell_string(family),
                cell_string(first),
                cell_string(prefecture),
                cell_string(area),
            ]
        }
        for row_index in pending_rows[order_number]:
            requests.append(
                {
                    "updateCells": {
                        "range": {
                            "sheetId": sheet_id_value,
                            "startRowIndex": row_index,
                            "endRowIndex": row_index + 1,
                            "startColumnIndex": 8,
                            "endColumnIndex": 12,
                        },
                        "rows": [row_data],
                        "fields": "userEnteredValue",
                    }
                }
            )
    return requests


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
        rms_secret_diagnostics()
        order_numbers: list[str] | None = None
        auth_errors: list[str] = []
        for auth_label, trim_auth_padding in [("base64_padded", False), ("base64_no_padding", True)]:
            print(f"rms_auth_variant={auth_label}")
            try:
                session = rms_session(trim_auth_padding=trim_auth_padding)
                check_rms_license(session)
                order_numbers = search_orders(session, run_at - dt.timedelta(days=LOOKBACK_DAYS), run_at)
                break
            except RuntimeError as exc:
                auth_errors.append(f"{auth_label}: {exc}")
                print(f"rms_auth_variant_failed={auth_label} detail={type(exc).__name__}: {exc}")
        if order_numbers is None:
            raise RuntimeError(" / ".join(auth_errors) or "RMS search failed before returning order numbers")
        search_count = len(order_numbers)
        if not order_numbers:
            append_log(token, log_sheet_id, run_at, 0, 0, 0, "")
            print("added=0 error=none")
            return 0

        pending_recipient_rows = rows_needing_recipient_backfill(token)
        existing = existing_order_numbers(token)
        new_order_numbers = [number for number in order_numbers if number not in existing]
        skipped_count = search_count - len(new_order_numbers)
        backfill_order_numbers = [number for number in order_numbers if number in pending_recipient_rows]
        if not new_order_numbers and not backfill_order_numbers:
            append_log(token, log_sheet_id, run_at, search_count, 0, skipped_count, "")
            print("added=0 error=none")
            return 0

        rows: list[LedgerRow] = []
        orders = get_orders(session, sorted(set(new_order_numbers + backfill_order_numbers)))
        for order in orders:
            if str(first_value(order, "orderNumber") or "").strip() in new_order_numbers:
                rows.extend(rows_from_order(order))
        if not rows:
            backfill_requests = build_recipient_backfill_requests(ledger_sheet_id, pending_recipient_rows, orders)
            if backfill_requests:
                sheets_post(token, f"/spreadsheets/{SPREADSHEET_ID}:batchUpdate", {"requests": backfill_requests})
            append_log(token, log_sheet_id, run_at, search_count, 0, skipped_count, "")
            print(f"added=0 backfilled={len(backfill_requests)} error=none")
            return 0

        start_row = max(next_available_row(token) - 1, 0)
        requests = build_append_requests(ledger_sheet_id, start_row, rows)
        backfill_requests = build_recipient_backfill_requests(ledger_sheet_id, pending_recipient_rows, orders)
        requests.extend(backfill_requests)
        sheets_post(token, f"/spreadsheets/{SPREADSHEET_ID}:batchUpdate", {"requests": requests})
        added_count = len(rows)
        append_log(token, log_sheet_id, run_at, search_count, added_count, skipped_count, "")
        print(f"added={added_count} backfilled={len(backfill_requests)} error=none")
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
