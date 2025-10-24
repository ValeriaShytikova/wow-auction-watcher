import os
import time
import json
import base64
import math
from typing import List, Dict, Tuple
import requests

# ----------- ПАРАМЕТРЫ ЧЕРЕЗ ENV -----------
REGION = "eu"
NAMESPACE_DYNAMIC = f"dynamic-{REGION}"
NAMESPACE_STATIC = f"static-{REGION}"
LOCALE_CANDIDATES = ["ru_RU", "en_US"]  # пробуем обе локали по очереди
USE_HTML = os.getenv("USE_HTML", "0") == "1"

PRICE_THRESHOLD_G = float(os.getenv("PRICE_THRESHOLD_G", "5000"))  # 5000 золота
SLEEP_BETWEEN_REALMS_SEC = int(os.getenv("SLEEP_BETWEEN_REALMS_SEC", "1"))  # чуть притормозим чтобы не долбить API

BLIZZARD_CLIENT_ID = os.getenv("BLIZZARD_CLIENT_ID")
BLIZZARD_CLIENT_SECRET = os.getenv("BLIZZARD_CLIENT_SECRET")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

GSHEET_SPREADSHEET_ID = os.getenv("GSHEET_SPREADSHEET_ID")
GSHEET_WORKSHEET_NAME = os.getenv("GSHEET_WORKSHEET_NAME", "Items")
# Сервис-аккаунт: base64 JSON в секрете GOOGLE_SERVICE_ACCOUNT_B64
GOOGLE_SERVICE_ACCOUNT_B64 = os.getenv("GOOGLE_SERVICE_ACCOUNT_B64")

# ----------- КОНСТАНТЫ -----------
BASE_AUTH = "https://oauth.battle.net/token"
BASE_API = f"https://{REGION}.api.blizzard.com"
COPPER_PER_GOLD = 10000

# ----------- GOOGLE SHEETS (через gspread) -----------
# Минималистично: не тащим гигантские libs. Возьмём gspread + google-auth
import re

def _extract_id(href: str, kind: str) -> int:
    """
    Достаёт числовой ID из ссылок вида .../connected-realm/1084?namespace=...
    kind: 'connected-realm' | 'realm'
    """
    if not href:
        return None
    m = re.search(rf"/{re.escape(kind)}/(\d+)", href)
    return int(m.group(1)) if m else None
    
def tg_escape(text: str) -> str:
    # Телега в режиме HTML требует экранировать &, <, >
    return (text.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;"))

import gspread
from google.oauth2.service_account import Credentials

def get_gs_client():
    if not GOOGLE_SERVICE_ACCOUNT_B64:
        raise RuntimeError("Missing GOOGLE_SERVICE_ACCOUNT_B64 secret")
    creds_json = json.loads(base64.b64decode(GOOGLE_SERVICE_ACCOUNT_B64).decode("utf-8"))
    scopes = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
    credentials = Credentials.from_service_account_info(creds_json, scopes=scopes)
    return gspread.authorize(credentials)

def load_item_names_from_sheet(spreadsheet_id: str, worksheet_name: str) -> List[str]:
    gc = get_gs_client()
    sh = gc.open_by_key(spreadsheet_id)
    ws = sh.worksheet(worksheet_name)
    values = ws.get_all_records()
    # ожидаем столбец item_name
    names = []
    for row in values:
        name = (row.get("item_name") or "").strip()
        if name:
            names.append(name)
    return names

# ----------- BLIZZARD AUTH -----------
def get_token(client_id: str, client_secret: str) -> str:
    r = requests.post(BASE_AUTH, data={"grant_type":"client_credentials"}, auth=(client_id, client_secret))
    r.raise_for_status()
    return r.json()["access_token"]

# ----------- HELPERS -----------
def human_price(copper: int) -> str:
    g = copper // COPPER_PER_GOLD
    rem = copper % COPPER_PER_GOLD
    s = rem // 100
    c = rem % 100
    return f"{g}g {s}s {c}c"

def send_telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[TELEGRAM] missing creds; skip")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        # parse_mode только если явно включим HTML
        **({"parse_mode": "HTML"} if USE_HTML else {}),
        "disable_web_page_preview": True,
    }
    try:
        r = requests.post(url, data=payload, timeout=30)
        if r.status_code != 200:
            print(f"[TELEGRAM ERROR] {r.status_code}: {r.text[:400]}")
            r.raise_for_status()
    except Exception as e:
        print("Telegram send failed:", e)


# ----------- REALMS -----------
def get_connected_realms(token: str) -> List[int]:
    """
    Возвращает список connected-realm IDs.
    1) Пытаемся через официальный индекс connected-realm.
    2) Если он пуст/ломается — fallback: читаем realm index и собираем connected_realm'ы.
    """
    headers = {"Authorization": f"Bearer {token}"}

    # --- Попытка №1: прямой индекс connected-realms
    url_cr = f"{BASE_API}/data/wow/connected-realm/index"
    params_cr = {"namespace": NAMESPACE_DYNAMIC, "locale": "en_US"}
    r = requests.get(url_cr, headers=headers, params=params_cr, timeout=60)
    if r.status_code != 200:
        print(f"[DEBUG] CR index HTTP {r.status_code}")
        print(f"[DEBUG] URL: {r.url}")
        print(f"[DEBUG] Body: {r.text[:400]}")
    else:
        data = r.json()
        items = data.get("connected_realms") or data.get("results") or []
        ids = []
        for it in items:
            # пробуем разные формы ответа
            href = (it.get("href")
                    or it.get("key", {}).get("href", "")
                    or it.get("_links", {}).get("self", {}).get("href", ""))
            cr_id = _extract_id(href, "connected-realm")
            if cr_id:
                ids.append(cr_id)
        if ids:
            return sorted(set(ids))
        else:
            print("[DEBUG] CR index returned 200 but no IDs were parsed.")
            print(f"[DEBUG] Example item: {str(items[0])[:300] if items else '[]'}")


    # --- Попытка №2 (fallback): realm index -> connected_realm
    print("[DEBUG] Fallback to realm index…")
    url_realm = f"{BASE_API}/data/wow/realm/index"
    params_realm = {"namespace": NAMESPACE_DYNAMIC, "locale": "en_US"}
    rr = requests.get(url_realm, headers=headers, params=params_realm, timeout=60)
    if rr.status_code != 200:
        print(f"[DEBUG] Realm index HTTP {rr.status_code}")
        print(f"[DEBUG] URL: {rr.url}")
        print(f"[DEBUG] Body: {rr.text[:400]}")
        return []

    data_r = rr.json()
    realms = data_r.get("realms") or data_r.get("results") or []
    cr_ids = set()
    for it in realms:
        # структура может быть {"key":{"href": .../realm/ID}, "data":{...}}; ищем connected_realm внутри "data" или через доп. запрос не пойдём — href должен быть внутри
        d = it.get("data") if "data" in it else it
        conn = (d.get("connected_realm", {}) if isinstance(d, dict) else {}).get("href", "")
        if not conn and "connected_realm" in (it or {}):
            conn = it["connected_realm"].get("href", "")
        if conn:
            try:
                cr_ids.add(int(conn.rstrip("/").split("/")[-1]))
            except:
                pass
    ids = sorted(cr_ids)
    if not ids:
        print("[DEBUG] Fallback also produced no IDs. Stopping.")
    return ids


def get_connected_realm_detail(token: str, cr_id: int) -> Dict:
    url = f"{BASE_API}/data/wow/connected-realm/{cr_id}?namespace={NAMESPACE_DYNAMIC}&locale=en_US"
    headers = {"Authorization": f"Bearer {token}"}
    r = requests.get(url, headers=headers, timeout=60)
    r.raise_for_status()
    return r.json()

# ----------- ITEM SEARCH -----------
def search_item_id(token: str, name: str) -> Tuple[int, str]:
    """
    Возвращает (item_id, display_name). Пробуем ru_RU, затем en_US.
    Берём первый результат (orderby=id asc) как наиболее базовый вариант.
    """
    headers = {"Authorization": f"Bearer {token}"}
    for loc in LOCALE_CANDIDATES:
        params = {
            "namespace": NAMESPACE_STATIC,
            "orderby": "id",
            "_pageSize": 1
        }
        # параметр имени: name.<locale>=...
        params[f"name.{loc}"] = name
        url = f"{BASE_API}/data/wow/search/item"
        r = requests.get(url, headers=headers, params=params, timeout=60)
        if r.status_code == 200:
            data = r.json()
            results = data.get("results", [])
            if results:
                item = results[0]
                itm_id = item.get("data", {}).get("id")
                # попытаемся вытащить красивое имя
                display = item.get("data", {}).get("name", {}).get(loc) or item.get("text", name)
                if itm_id:
                    return int(itm_id), display or name
    raise ValueError(f"Item not found by name: {name}")

# ----------- AUCTIONS -----------
def get_auctions_for_connected_realm(token: str, cr_id: int) -> Dict:
    url = f"{BASE_API}/data/wow/connected-realm/{cr_id}/auctions"
    params = {"namespace": NAMESPACE_DYNAMIC, "locale": "en_US"}
    headers = {"Authorization": f"Bearer {token}"}
    r = requests.get(url, headers=headers, params=params, timeout=120)
    r.raise_for_status()
    return r.json()

def check_items_in_auctions(auctions_json: Dict, item_ids: Dict[int, str], threshold_gold: float) -> List[Dict]:
    found = []
    threshold_copper = int(threshold_gold * COPPER_PER_GOLD)
    for a in auctions_json.get("auctions", []):
        item = a.get("item", {})
        item_id = item.get("id")
        if not item_id:
            # иногда структура другая, но у живого аукциона обычно есть id
            continue
        if item_id not in item_ids:
            continue
        buyout = a.get("buyout")
        quantity = a.get("quantity", 1)
        if not buyout or quantity <= 0:
            continue
        per_unit = buyout // quantity
        if per_unit <= threshold_copper:
            found.append({
                "item_id": item_id,
                "item_name": item_ids[item_id],
                "per_unit_copper": per_unit,
                "quantity": quantity,
                "auction_id": a.get("id"),
                "time_left": a.get("time_left"),
                "owner": a.get("owner", "unknown"),
            })
    return found

def main():
    # 1) токен
    token = get_token(BLIZZARD_CLIENT_ID, BLIZZARD_CLIENT_SECRET)

    # 2) читаем список предметов из Google Sheet
    names = load_item_names_from_sheet(GSHEET_SPREADSHEET_ID, GSHEET_WORKSHEET_NAME)
    if not names:
        print("No item names in the sheet. Exit quietly.")
        return

    # 3) резолвим в item_id (кэш внутри одного запуска)
    id_map: Dict[int, str] = {}
    for name in names:
        try:
            itm_id, disp = search_item_id(token, name)
            id_map[itm_id] = disp
        except Exception as e:
            print(f"Name -> id not found for '{name}': {e}")

    if not id_map:
        print("No resolvable items. Exit quietly.")
        return

    # 4) берём все EU connected realms
    cr_list = get_connected_realms(token)
    if not cr_list:
        print("⚠️ No EU connected realms fetched. Retrying with a fresh token…")
        time.sleep(2)
        token = get_token(BLIZZARD_CLIENT_ID, BLIZZARD_CLIENT_SECRET)
        cr_list = get_connected_realms(token)
    
    print(f"[DEBUG] EU connected realms: {len(cr_list)}")
    if not cr_list:
        print("❌ Still empty after retry; Blizzard API/namespace may be acting up. Exit.")
        return


    # 5) детализируем имена реалмов (кэш)
    realm_names_cache: Dict[int, List[str]] = {}
    for cr in cr_list:
        try:
            detail = get_connected_realm_detail(token, cr)
            # В некоторых ответах "realms" приходит как список словарей,
            # в редких — попадаются строки/другие типы. Бережно обрабатываем.
            realms = detail.get("realms", []) if isinstance(detail, dict) else []
            names = []
            for realm in realms:
                if isinstance(realm, dict):
                    name_dict = realm.get("name", {}) if isinstance(realm.get("name", {}), dict) else {}
                    nm = (
                        name_dict.get("en_GB") or  # в EU часто en_GB
                        name_dict.get("en_US") or
                        name_dict.get("ru_RU") or
                        realm.get("slug")
                    )
                    if nm:
                        names.append(nm)
                # игнорируем строки/нестандартные элементы
            realm_names_cache[cr] = names or [f"CR-{cr}"]
        except Exception as e:
            realm_names_cache[cr] = [f"CR-{cr}"]
            print(f"realm detail failed for {cr}: {e}")
        time.sleep(0.05)


    # 6) скан аукционов по всем CR
    global_found = []

    for idx, cr in enumerate(cr_list, 1):
        try:
            aj = get_auctions_for_connected_realm(token, cr)
            found = check_items_in_auctions(aj, id_map, PRICE_THRESHOLD_G)
            if found:
                for f in found:
                    # --- экранируем текст для Telegram
                    item_name_raw = f["item_name"]              # без экранирования — мы же plain-text
                    item_id = f["item_id"] if "item_id" in f else None  # добавь item_id в check_items_in_auctions, см. ниже
                    realms_txt = ", ".join(n.replace("-", " ") for n in realm_names_cache.get(cr, [f"CR-{cr}"]))
                    time_left = str(f.get("time_left", ""))
                    price = human_price(f["per_unit_copper"])
                    qty = f["quantity"]
                    auc = f["auction_id"]
                    
                    # при желании — короткая ссылка на wowhead
                    wowhead = f"https://www.wowhead.com/item={item_id}" if item_id else ""
                    
                    txt_lines = [
                        f"🔔 {item_name_raw}" + (f" (ID {item_id})" if item_id else ""),
                        f"Цена/шт: {price}  • Кол-во: {qty}",
                        f"Сервера (EU): {realms_txt}",
                        f"AuctionID: {auc}  • time_left: {time_left}",
                    ]
                    if wowhead:
                        txt_lines.append(wowhead)
                    
                    txt = "\n".join(txt_lines)
                    global_found.append(txt)


            time.sleep(SLEEP_BETWEEN_REALMS_SEC)

        except Exception as e:
            print(f"CR {cr} fetch error: {e}")
            time.sleep(1)


    # 7) шлём уведомления, только если есть находки
    if global_found:
        header = "🧭 <b>Найдены лоты</b> (EU):\n\n"
        # Экранируем каждую запись
        chunks = []
        cur = header
        for block in global_found:
            block_safe = tg_escape(block)
            # Телега жёстко ограничивает ~4096 символов на message
            if len(cur) + len(block_safe) + 2 > 3500:  # оставим запас
                chunks.append(cur)
                cur = header + block_safe + "\n\n"
            else:
                cur += block_safe + "\n\n"
        if cur.strip():
            chunks.append(cur)
    
        for part in chunks:
            send_telegram(part)
    else:
        print("Nothing found; no notification sent.")


if __name__ == "__main__":
    main()
