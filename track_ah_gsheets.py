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
    
def pretty_realms(names):
    # делаem читаемо: 'argent-dawn' -> 'Argent Dawn'
    return ", ".join(n.replace("-", " ").title() for n in names)

import gspread
from google.oauth2.service_account import Credentials

def get_gs_client():
    if not GOOGLE_SERVICE_ACCOUNT_B64:
        raise RuntimeError("Missing GOOGLE_SERVICE_ACCOUNT_B64 secret")
    creds_json = json.loads(base64.b64decode(GOOGLE_SERVICE_ACCOUNT_B64).decode("utf-8"))
    scopes = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
    credentials = Credentials.from_service_account_info(creds_json, scopes=scopes)
    return gspread.authorize(credentials)

def parse_price_to_gold(s: str):
    """
    Парсим '5000', '4 500', '5k', '3,5k', '3500g', '3 500 g' -> float (gold).
    Пусто/None/непарсибельно -> None.
    """
    if s is None:
        return None
    txt = str(s).strip().lower()
    if not txt:
        return None
    import re
    txt = re.sub(r"[^\dkg\.,\s]", "", txt)
    txt = txt.replace(" ", "")
    if "k" in txt:
        num = txt.replace("k", "").replace(",", ".")
        try:
            return float(num) * 1000.0
        except:
            return None
    txt = txt.replace("g", "").replace(",", ".")
    try:
        return float(txt)
    except:
        return None


def load_items_with_thresholds(spreadsheet_id: str, worksheet_name: str):
    """
    Читает лист Items и возвращает список (name, per_item_thr_or_None).
    Принимает гибкие заголовки: item_name / Item Name, max_price / MaxPrice / Max Price.
    """
    gc = get_gs_client()
    sh = gc.open_by_key(spreadsheet_id)
    ws = sh.worksheet(worksheet_name)
    rows = ws.get_all_records()  # [{'item_name': '...', 'max_price': '...'}, ...]

    items = []
    for r in rows:
        # нормализуем ключи: нижний регистр, убираем пробелы, дефисы, приводим к snake_case
        r_norm = {str(k).strip().lower().replace(" ", "_").replace("-", "_"): v for k, v in r.items()}

        name = (r_norm.get("item_name") or r_norm.get("item") or r_norm.get("name") or "").strip()
        if not name:
            continue

        raw_thr = r_norm.get("max_price") or r_norm.get("maxprice") or r_norm.get("price_max")
        thr = parse_price_to_gold(raw_thr) if raw_thr not in ("", None) else None
        items.append((name, thr))
    return items


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
def search_item_id(token: str, name: str) -> tuple[int, str]:
    """
    Возвращает (item_id, display_name) только при точном совпадении по английскому названию (без учёта регистра).
    """
    headers = {"Authorization": f"Bearer {token}"}
    params = {
        "namespace": NAMESPACE_STATIC,
        "_pageSize": 100,
        "orderby": "id",
        "name.en_US": name
    }

    url = f"{BASE_API}/data/wow/search/item"
    r = requests.get(url, headers=headers, params=params, timeout=60)
    r.raise_for_status()
    data = r.json()

    results = data.get("results", []) or []
    wanted = name.strip().lower()

    for it in results:
        d = it.get("data", {}) or {}
        item_id = d.get("id")
        names = d.get("name", {}) or {}
        en_name = (names.get("en_US") or "").strip().lower()
        if en_name == wanted:
            display_name = names.get("en_US") or names.get("ru_RU") or it.get("text") or name
            return int(item_id), display_name

    raise ValueError(f"Exact English match not found: {name}")


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
def check_items_in_auctions_per_item(auctions_json: Dict,
                                     item_ids: Dict[int, str],
                                     id_to_threshold_gold: Dict[int, float]) -> List[Dict]:
    """
    То же, что check_items_in_auctions, только порог берём из per-item словаря id_to_threshold_gold.
    """
    found = []
    for a in auctions_json.get("auctions", []):
        item = a.get("item", {})
        item_id = item.get("id")
        if not item_id:
            continue
        if item_id not in item_ids:
            continue
        buyout = a.get("buyout")
        quantity = a.get("quantity", 1)
        if not buyout or quantity <= 0:
            continue
        per_unit = buyout // quantity

        thr_gold = id_to_threshold_gold.get(item_id)
        if thr_gold is None:
            thr_gold = PRICE_THRESHOLD_G  # запасной фолбэк
        thr_copper = int(thr_gold * COPPER_PER_GOLD)

        if per_unit <= thr_copper:
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

    # 2) читаем список предметов + индивидуальные пороги из Google Sheet
    rows = load_items_with_thresholds(GSHEET_SPREADSHEET_ID, GSHEET_WORKSHEET_NAME)
    if not rows:
        print("No item names in the sheet. Exit quietly.")
        return
    
    # 3) резолвим в item_id и собираем два словаря:
    #    id_map: id -> display_name
    #    id_thr: id -> threshold_gold (per-item), если None -> подставим глобальный ниже
    id_map: Dict[int, str] = {}
    id_thr: Dict[int, float] = {}
    for (name, per_item_thr) in rows:
        try:
            itm_id, disp = search_item_id(token, name)
            id_map[itm_id] = disp
            id_thr[itm_id] = per_item_thr if per_item_thr is not None else PRICE_THRESHOLD_G
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
    # Группируем находки: (item_id, item_name) -> { realm_str -> rec }
    grouped = {}

    for idx, cr in enumerate(cr_list, 1):
        try:
            aj = get_auctions_for_connected_realm(token, cr)
            found = check_items_in_auctions_per_item(aj, id_map, id_thr)

            if found:
                # красивое имя кластера реалмов
                realms_names = realm_names_cache.get(cr, [f"CR-{cr}"])
                realm_str = pretty_realms(realms_names)

                # по одному предмету на этот connected realm оставим самый дешёвый per-unit и суммарный qty
                per_item_best = {}  # item_id -> rec

                for f in found:
                    item_id = f.get("item_id")
                    item_name = f["item_name"]
                    price_copper = f["per_unit_copper"]
                    qty = int(f["quantity"])
                    auc = f.get("auction_id")
                    time_left = str(f.get("time_left", ""))

                    cur = per_item_best.get(item_id)
                    if cur is None or price_copper < cur["per_unit_copper"]:
                        per_item_best[item_id] = {
                            "item_id": item_id,
                            "item_name": item_name,
                            "per_unit_copper": price_copper,
                            "quantity": qty,
                            "auction_id": auc,
                            "time_left": time_left,
                        }
                    else:
                        # если нашлась дороже — игнорируем, если такая же — докидываем количество
                        if price_copper == cur["per_unit_copper"]:
                            cur["quantity"] += qty

                # теперь покладём лучшую запись по каждому предмету в общую группировку
                for item_id, rec in per_item_best.items():
                    key = (item_id, rec["item_name"])
                    bucket = grouped.setdefault(key, {})
                    # если по этому connected realm уже есть запись — оставляем более дешёвую
                    prev = bucket.get(realm_str)
                    if (prev is None) or (rec["per_unit_copper"] < prev["per_unit_copper"]):
                        bucket[realm_str] = rec

            time.sleep(SLEEP_BETWEEN_REALMS_SEC)

        except Exception as e:
            print(f"CR {cr} fetch error: {e}")
            time.sleep(1)



    # 7) шлём уведомления, только если есть находки
    # Отправляем: один предмет — одно сообщение со списком CR
    if grouped:
        header_tpl = "🧭 Найдены лоты (EU)\n"
        # plain-text режим по умолчанию (USE_HTML = 0)
        # если захочешь — включим HTML, но сейчас не надо

        for (item_id, item_name), realms_map in grouped.items():
            thr_show = int(id_thr.get(item_id, PRICE_THRESHOLD_G))
            lines = [f"🔔 {item_name} (ID {item_id}) — порог ≤ {thr_show}g/шт"]

            # сортируем кластеры по цене
            entries = sorted(
                realms_map.items(),
                key=lambda kv: kv[1]["per_unit_copper"]
            )
            for realm_str, rec in entries:
                price = human_price(rec["per_unit_copper"])
                qty = rec["quantity"]
                auc = rec.get("auction_id")
                tleft = rec.get("time_left", "")
                lines.append(f"- {price} • x{qty} • {realm_str} • auc {auc} • {tleft}")

            # при желании: короткая ссылка на wowhead
            lines.append(f"https://www.wowhead.com/item={item_id}")

            msg = header_tpl + "\n".join(lines)

            # режем на чанки, если вдруг очень длинно
            parts = []
            cur = ""
            for ln in msg.split("\n"):
                if len(cur) + len(ln) + 1 > 3500:
                    parts.append(cur)
                    cur = ln + "\n"
                else:
                    cur += ln + "\n"
            if cur.strip():
                parts.append(cur)

            for part in parts:
                send_telegram(part)

    else:
        print("Nothing found; no notification sent.")



if __name__ == "__main__":
    main()
