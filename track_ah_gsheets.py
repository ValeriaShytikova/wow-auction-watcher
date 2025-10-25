# track_ah_gsheets.py
# Рабочая база + добавлена поддержка per-item max_price из Google Sheet
import os, time, json, base64, re
from typing import List, Dict, Tuple
import requests

# ---- РЕГИОН/НЕЙМСПЕЙС (как в первой рабочей версии) ----
REGION = "eu"
NAMESPACE_DYNAMIC = f"dynamic-{REGION}"
NAMESPACE_STATIC  = f"static-{REGION}"

# ---- Локали для поиска предметов ----
LOCALE_CANDIDATES = ["ru_RU", "en_US"]

# ---- Глобальный дефолт (если в Sheet пусто) ----
PRICE_THRESHOLD_G = float(os.getenv("PRICE_THRESHOLD_G", "5000"))
SLEEP_BETWEEN_REALMS_SEC = int(os.getenv("SLEEP_BETWEEN_REALMS_SEC", "1"))

# ---- Креды ----
BLIZZARD_CLIENT_ID = os.getenv("BLIZZARD_CLIENT_ID")
BLIZZARD_CLIENT_SECRET = os.getenv("BLIZZARD_CLIENT_SECRET")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# ---- Google Sheet ----
GSHEET_SPREADSHEET_ID = os.getenv("GSHEET_SPREADSHEET_ID")
GSHEET_WORKSHEET_NAME = os.getenv("GSHEET_WORKSHEET_NAME", "Items")
GOOGLE_SERVICE_ACCOUNT_B64 = os.getenv("GOOGLE_SERVICE_ACCOUNT_B64")

BASE_AUTH = "https://oauth.battle.net/token"
BASE_API  = f"https://{REGION}.api.blizzard.com"
COPPER_PER_GOLD = 10000

# ---- Google Sheets client (как было) ----
import gspread
from google.oauth2.service_account import Credentials

def get_gs_client():
    if not GOOGLE_SERVICE_ACCOUNT_B64:
        raise RuntimeError("Missing GOOGLE_SERVICE_ACCOUNT_B64")
    creds_json = json.loads(base64.b64decode(GOOGLE_SERVICE_ACCOUNT_B64).decode("utf-8"))
    scopes = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
    credentials = Credentials.from_service_account_info(creds_json, scopes=scopes)
    return gspread.authorize(credentials)

# ---- Парсер цены из второго столбца ----
def parse_price_to_gold(s: str) -> float:
    """
    '5000', '4 500', '5k', '3,5k', '3500g', '3 500 g' -> float (gold)
    Пусто/None -> None
    """
    if s is None:
        return None
    txt = str(s).strip().lower()
    if not txt:
        return None
    txt = re.sub(r"[^\dkg\.,\s]", "", txt)  # оставить цифры/k/g/запятые/точки/пробел
    txt = txt.replace(" ", "")
    if "k" in txt:
        num = txt.replace("k", "").replace(",", ".")
        try: return float(num) * 1000.0
        except: return None
    txt = txt.replace("g", "").replace(",", ".")
    try: return float(txt)
    except: return None

def load_items_with_thresholds(spreadsheet_id: str, worksheet_name: str) -> List[Tuple[str, float]]:
    """
    Ожидаем в листе Items 2 колонки:
    item_name | max_price
    """
    gc = get_gs_client()
    sh = gc.open_by_key(spreadsheet_id)
    ws = sh.worksheet(worksheet_name)
    rows = ws.get_all_records()  # [{'item_name': '...', 'max_price': '...'}, ...]
    items = []
    for r in rows:
        name = (r.get("item_name") or "").strip()
        if not name:
            continue
        raw_thr = r.get("max_price")
        thr = parse_price_to_gold(raw_thr) if raw_thr not in ("", None) else None
        items.append((name, thr))
    return items

# ---- Blizzard auth (как было) ----
def get_token(cid: str, secret: str) -> str:
    r = requests.post(BASE_AUTH, data={"grant_type":"client_credentials"}, auth=(cid, secret))
    r.raise_for_status()
    return r.json()["access_token"]

# ---- Утилиты ----
def human_price(copper: int) -> str:
    g = copper // COPPER_PER_GOLD
    rem = copper % COPPER_PER_GOLD
    s = rem // 100
    c = rem % 100
    return f"{g}g {s}s {c}c"

def send_telegram(text: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram creds missing; message:\n", text)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode":"HTML", "disable_web_page_preview": True}
    try:
        requests.post(url, data=payload, timeout=30).raise_for_status()
    except Exception as e:
        print("Telegram send failed:", e)

# ---- EXACT как в первой рабочей версии: индексы/детали CR через querystring ----
from urllib.parse import urlparse

def get_connected_realms(token: str) -> List[int]:
    # поддерживаем оба формата: {"href": "..."} ИЛИ {"key": {"href": "..."}}
    url = f"{BASE_API}/data/wow/connected-realm/index?namespace={NAMESPACE_DYNAMIC}&locale=en_US"
    headers = {"Authorization": f"Bearer {token}"}
    r = requests.get(url, headers=headers)
    r.raise_for_status()
    data = r.json()

    items = data.get("connected_realms", [])
    print(f"[DEBUG] EU connected_realms in index: {len(items)}")
    if items[:1]:
        print(f"[DEBUG] sample item: {items[0]}")

    ids = []
    for it in items:
        href = it.get("href") or it.get("key", {}).get("href")
        if not href:
            continue
        try:
            # аккуратно вытащим id из path, игнорируя query (?namespace=...)
            path = urlparse(href).path  # например: /data/wow/connected-realm/1080
            last = path.rstrip("/").split("/")[-1]  # '1080'
            cr_id = int(last)
            ids.append(cr_id)
        except Exception as e:
            print(f"[WARN] cannot parse CR id from href={href}: {e}")
    return ids



def get_connected_realm_detail(token: str, cr_id: int) -> Dict:
    url = f"{BASE_API}/data/wow/connected-realm/{cr_id}?namespace={NAMESPACE_DYNAMIC}&locale=en_US"
    headers = {"Authorization": f"Bearer {token}"}
    r = requests.get(url, headers=headers)
    r.raise_for_status()
    return r.json()

def search_item_id(token: str, name: str) -> Tuple[int, str]:
    headers = {"Authorization": f"Bearer {token}"}
    # тоже строго как было: querystring с namespace/static и локалями
    base = f"{BASE_API}/data/wow/search/item"
    for loc in LOCALE_CANDIDATES:
        url = f"{base}?namespace={NAMESPACE_STATIC}&orderby=id&_pageSize=1&name.{loc}={requests.utils.quote(name)}"
        r = requests.get(url, headers=headers)
        if r.status_code == 200:
            data = r.json()
            res = data.get("results", [])
            if res:
                itm = res[0]
                itm_id = itm.get("data", {}).get("id")
                disp  = itm.get("data", {}).get("name", {}).get(loc) or name
                if itm_id:
                    return int(itm_id), disp
    raise ValueError(f"Item not found: {name}")

def get_auctions_for_connected_realm(token: str, cr_id: int) -> Dict:
    url = f"{BASE_API}/data/wow/connected-realm/{cr_id}/auctions?namespace={NAMESPACE_DYNAMIC}&locale=en_US"
    headers = {"Authorization": f"Bearer {token}"}
    r = requests.get(url, headers=headers)
    r.raise_for_status()
    return r.json()

def check_items_in_auctions(auctions_json: Dict,
                            id_to_name: Dict[int,str],
                            id_to_threshold_gold: Dict[int,float]) -> List[Dict]:
    found = []
    for a in auctions_json.get("auctions", []):
        item = a.get("item", {})
        item_id = item.get("id")
        if not item_id or item_id not in id_to_threshold_gold:
            continue
        buyout = a.get("buyout")
        qty = a.get("quantity", 1)
        if not buyout or qty <= 0:
            continue
        per_unit = buyout // qty
        thr_gold = id_to_threshold_gold[item_id]
        thr_copper = int(thr_gold * COPPER_PER_GOLD)
        if per_unit <= thr_copper:
            found.append({
                "item_id": item_id,
                "item_name": id_to_name[item_id],
                "per_unit_copper": per_unit,
                "quantity": qty,
                "auction_id": a.get("id"),
                "time_left": a.get("time_left"),
                "owner": a.get("owner", "unknown"),
            })
    return found

def main():
    token = get_token(BLIZZARD_CLIENT_ID, BLIZZARD_CLIENT_SECRET)

    # 1) читаем Sheet: [(name, per_item_thr_or_None)]
    rows = load_items_with_thresholds(GSHEET_SPREADSHEET_ID, GSHEET_WORKSHEET_NAME)
    if not rows:
        print("No items in sheet. Exit.")
        return

    # 2) резолвим item_id; собираем карты id->name и id->threshold_gold
    id_to_name: Dict[int,str] = {}
    id_to_thr:  Dict[int,float] = {}
    for name, per_item_thr in rows:
        try:
            itm_id, disp = search_item_id(token, name)
            id_to_name[itm_id] = disp
            id_to_thr[itm_id]  = per_item_thr if per_item_thr is not None else PRICE_THRESHOLD_G
        except Exception as e:
            print(f"[WARN] Item not resolved '{name}': {e}")

    if not id_to_name:
        print("No resolvable items. Exit.")
        return

    # 3) список EU connected realms (как было)
    cr_list = get_connected_realms(token)
    if not cr_list:
        print("No EU connected realms. Exit.")
        return

    # 4) читаем имена реалмов (как было)
    realm_names_cache: Dict[int, List[str]] = {}
    for cr in cr_list:
        try:
            detail = get_connected_realm_detail(token, cr)
            names = []
            for realm in detail.get("realms", []):
                nm = realm.get("name", {}).get("en_US") or realm.get("name", {}).get("ru_RU") or realm.get("slug")
                if nm: names.append(nm)
            realm_names_cache[cr] = names or [f"CR-{cr}"]
        except Exception as e:
            realm_names_cache[cr] = [f"CR-{cr}"]
            print(f"[WARN] realm detail {cr}: {e}")
        time.sleep(0.1)

    # 5) скан аукционов
    global_found = []
    for cr in cr_list:
        try:
            aj = get_auctions_for_connected_realm(token, cr)
            found = check_items_in_auctions(aj, id_to_name, id_to_thr)
            if found:
                realms_txt = ", ".join(realm_names_cache.get(cr, [f"CR-{cr}"]))
                for f in found:
                    price = human_price(f["per_unit_copper"])
                    txt = (f"🔔 <b>{f['item_name']}</b> ≤ {id_to_thr[f['item_id']]:.0f}g/шт\n"
                           f"Цена/шт: <b>{price}</b> • Кол-во: {f['quantity']}\n"
                           f"Сервера (EU): {realms_txt}\n"
                           f"AuctionID: {f['auction_id']} • time_left: {f['time_left']}")
                    global_found.append(txt)
            time.sleep(SLEEP_BETWEEN_REALMS_SEC)
        except Exception as e:
            print(f"[WARN] CR {cr} fetch: {e}")
            time.sleep(1)

    if global_found:
        send_telegram("🧭 <b>Найдены лоты</b> (EU):\n\n" + "\n\n".join(global_found))
    else:
        print("Nothing found; no notification sent.")

if __name__ == "__main__":
    main()
