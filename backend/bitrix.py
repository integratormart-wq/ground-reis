"""Двусторонняя синхронизация рейсов со смарт-процессами Bitrix24."""
import base64
import json
import math
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from email.message import Message
from datetime import date, datetime, timezone
from typing import Optional

from backend import models
from backend.models import RequestStatus, TripType

BITRIX_TIMEOUT = 20
PROCESS_NAME_PUKHTOVOZ = "пухтовоз"
PROCESS_NAME_SAMOSVAL = "самосвал"
PUKHTOVOZ_ENTITY_TYPE_ID = str(os.getenv("BITRIX_PUKHTOVOZ_ENTITY_TYPE_ID", "1088"))
SAMOSVAL_ENTITY_TYPE_ID = str(os.getenv("BITRIX_SAMOSVAL_ENTITY_TYPE_ID", "1092"))
KNOWN_PROCESS_KINDS = {
    PUKHTOVOZ_ENTITY_TYPE_ID: TripType.PUKHTOVOZ,
    SAMOSVAL_ENTITY_TYPE_ID: TripType.SAMOSVAL,
}

# Короткие in-memory кэши убирают повторные служебные REST-запросы Bitrix.
# В кэш не записываются рейсы и пользовательские данные — только схема CRM,
# список процессов и часовой пояс портала. После рестарта Render кэш пустой.
_RUNTIME_CACHE_LOCK = threading.Lock()
_PROCESS_KINDS_CACHE = {}
_FIELD_SCHEMA_CACHE = {}
_PORTAL_TZ_CACHE = {}
_BITRIX_USER_CACHE = {}
PROCESS_CACHE_TTL = 300
FIELD_SCHEMA_CACHE_TTL = 300
PORTAL_TZ_CACHE_TTL = 600
BITRIX_USER_CACHE_TTL = 600


def _cache_get(cache: dict, key, ttl: int):
    now = time.monotonic()
    with _RUNTIME_CACHE_LOCK:
        row = cache.get(key)
        if not row:
            return None
        created_at, value = row
        if now - created_at >= ttl:
            cache.pop(key, None)
            return None
        return value


def _cache_put(cache: dict, key, value):
    with _RUNTIME_CACHE_LOCK:
        cache[key] = (time.monotonic(), value)
    return value


def _clear_runtime_cache():
    """Техническая очистка кэша для тестов/изменения схемы Bitrix."""
    with _RUNTIME_CACHE_LOCK:
        _PROCESS_KINDS_CACHE.clear()
        _FIELD_SCHEMA_CACHE.clear()
        _PORTAL_TZ_CACHE.clear()
        _BITRIX_USER_CACHE.clear()

def _invalidate_field_schema_cache(webhook_base: str, entity_id) -> None:
    cache_key = (_normalize_webhook_base(webhook_base), str(entity_id))
    with _RUNTIME_CACHE_LOCK:
        _FIELD_SCHEMA_CACHE.pop(cache_key, None)



STATUS_STAGE_TITLES = {
    RequestStatus.ACCEPTED: "Водитель назначен",
    RequestStatus.IN_WORK: "Рейс начат",
    RequestStatus.DRIVER_COMPLETED: "Рейс завершен",
    RequestStatus.LOGIST_CONFIRMED: "Успех",
}

# Это логические имена. При отправке они автоматически сопоставляются с реальными
# полями смарт-процесса по коду или русскому названию поля.
FIELD_MAP = {
    "number": "title",
    "planned_at": "ufReisDate",
    "driver_name": "ufDriver",
    "vehicle_name": "ufVehicle",
    "load_address": "ufLoadAddr",
    "unload_address": "ufUnloadAddr",
    "route_name": "ufRoute",
    "trips_count": "ufTripsCount",
    "cargo_type_name": "ufCargoType",
    "tariff_name": "ufTariff",
    "km": "ufKmPlan",
    "volume": "ufVolumePlan",
    "tonnage": "ufTonnagePlan",
    "actual_km": "ufKmFact",
    "actual_volume": "ufVolumeFact",
    "actual_tonnage": "ufTonnageFact",
    "status": "ufStatus",
    "customer_name": "ufCustomer",
    "customer_bitrix_id": "ufCustomerBitrixId",
    "customer_inn": "ufCustomerInn",
    "customer_contact_name": "ufCustomerContact",
    "customer_contact_phone": "ufCustomerPhone",
    "customer_address": "ufCustomerAddress",
    "polygon_name": "ufPolygon",
    "polygon_address": "ufPolygonAddress",
    "polygon_contact": "ufPolygonContact",
    "polygon_phone": "ufPolygonPhone",
    "polygon_navigator_url": "ufPolygonNavigator",
    "attachments": "ufTripFiles",
    "sum_trip": "ufSumTrip",
    "sum_driver": "ufSumDriver",
    "polygon_cost": "ufPolygonCost",
    "odometer": "ufOdometer",
    "fuel_liters": "ufFuelLiters",
    "fuel_price": "ufFuelPrice",
    "fuel_cost": "ufFuelCost",
    "started_at": "ufStartedAt",
    "finished_at": "ufFinishedAt",
    "waste_bin_count": "ufWasteBinCount",
    "site_contact_name": "ufSiteContact",
    "site_contact_phone": "ufSitePhone",
    "site_contact_comment": "ufSiteContactComment",
    "is_empty_run": "ufEmptyRun",
    "empty_run_comment": "ufEmptyRunComment",
    "has_downtime": "ufHasDowntime",
    "downtime_minutes": "ufDowntimeMinutes",
    "downtime_comment": "ufDowntimeComment",
    "comment": "ufComment",
    "logist_comment": "ufLogistComment",
}

FIELD_TITLES = {
    "planned_at": ("дата и время", "дата рейса", "плановая дата и время", "дата/время рейса", "подача машины", "дата"),
    "driver_name": ("водитель", "фио водителя", "водители"),
    # В Bitrix поле теперь называется именно «Госномер». Не подхватываем
    # старые поля «Машина/Машины/Автомобиль», чтобы они больше не могли
    # перехватить синхронизацию.
    "vehicle_name": ("госномер", "государственный номер"),
    "load_address": ("адрес загрузки", "адрес подачи", "загрузка"),
    "unload_address": ("адрес выгрузки", "выгрузка"),
    "route_name": ("маршрут",),
    "trips_count": ("количество рейсов", "число рейсов", "рейсов"),
    "cargo_type_name": ("тип груза", "груз"),
    "tariff_name": ("тариф",),
    "km": ("километраж план", "плановый километраж", "километраж", "км план"),
    "volume": ("объем план", "плановый объем", "объем", "кубатура"),
    "tonnage": ("тоннаж план", "плановый тоннаж", "тонны план", "тоннаж"),
    "actual_km": ("фактический километраж", "факт км", "км факт"),
    "actual_volume": ("фактический объем", "факт объем", "объем факт"),
    "actual_tonnage": ("фактический тоннаж", "тоннаж факт", "тонны факт"),
    "status": ("статус заявки", "статус рейса", "статус"),
    "customer_name": ("заказчик", "клиент", "компания"),
    "customer_bitrix_id": ("id компании битрикс", "bitrix id клиента", "id клиента битрикс"),
    "customer_inn": ("инн заказчика", "инн клиента", "инн"),
    "customer_contact_name": ("контакт заказчика", "контакт клиента", "контактное лицо заказчика"),
    "customer_contact_phone": ("телефон заказчика", "телефон клиента", "телефон компании"),
    "customer_address": ("адрес заказчика", "адрес клиента", "адрес компании"),
    "polygon_name": ("полигон",),
    "polygon_address": ("адрес полигона",),
    "polygon_contact": ("контакт полигона", "диспетчер полигона"),
    "polygon_phone": ("телефон полигона", "телефон диспетчера полигона"),
    "polygon_navigator_url": ("навигация полигона", "ссылка навигатора полигона", "яндекс навигация полигона"),
    "attachments": ("файлы рейса", "фото и файлы", "файлы водителя", "вложения рейса", "файлы"),
    "sum_trip": ("сумма рейса", "стоимость рейса"),
    "sum_driver": ("сумма водителю", "зарплата водителя", "начисление водителю"),
    "polygon_cost": ("затраты полигона", "затраты на полигон", "стоимость полигона", "расходы полигона", "расходы на полигон"),
    "odometer": ("показания спидометра", "спидометр", "одометр"),
    "fuel_liters": ("залито топлива", "топливо литры", "топливо, л", "литры топлива"),
    "fuel_price": ("цена за литр топлива", "цена топлива за литр", "стоимость литра топлива", "цена за литр"),
    "fuel_cost": ("затраты на топливо", "расходы на топливо", "стоимость топлива"),
    "started_at": ("начало рейса", "время начала рейса"),
    "finished_at": ("завершение рейса", "время завершения рейса"),
    "waste_bin_count": ("количество контейнеров", "контейнеры", "кб"),
    "site_contact_name": ("контакт на объекте", "контактное лицо на объекте"),
    "site_contact_phone": ("телефон на объекте", "телефон контакта"),
    "site_contact_comment": ("комментарий к контакту", "комментарий контакта"),
    "is_empty_run": ("холостой прогон",),
    "empty_run_comment": ("комментарий холостого прогона", "причина холостого прогона"),
    "has_downtime": ("был простой", "простой"),
    "downtime_minutes": ("длительность простоя", "простой минут"),
    "downtime_comment": ("комментарий простоя", "причина простоя"),
    "comment": ("комментарий",),
    "logist_comment": ("комментарий логиста",),
}


def _encode_params(params: dict) -> dict:
    """Преобразует вложенные поля в формат fields[title], который ждёт REST Bitrix24."""
    out = {}

    def walk(prefix, value):
        if isinstance(value, dict):
            for key, child in value.items():
                walk(f"{prefix}[{key}]" if prefix else str(key), child)
        elif isinstance(value, (list, tuple)):
            for index, child in enumerate(value):
                walk(f"{prefix}[{index}]", child)
        else:
            out[prefix] = "" if value is None else value

    for key, value in params.items():
        walk(str(key), value)
    return out


def _normalize_webhook_base(webhook_url: str) -> str:
    """Accept both a base webhook URL and a full Bitrix request-generator URL."""
    value = str(webhook_url or "").strip()
    match = re.match(r"^(https://[^/]+/rest/\d+/[^/]+)(?:/.*)?$", value, re.IGNORECASE)
    return (match.group(1) if match else value.rstrip("/")) + "/"


def _http_post(webhook_base: str, method: str, params: dict) -> dict:
    url = _normalize_webhook_base(webhook_base) + method
    data = urllib.parse.urlencode(_encode_params(params)).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=BITRIX_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return {"error": f"HTTP {exc.code}: {exc.read().decode('utf-8', 'ignore')}"}
    except Exception as exc:
        return {"error": repr(exc)}


def get_integration_settings(db) -> Optional[models.IntegrationSetting]:
    return db.query(models.IntegrationSetting).filter(models.IntegrationSetting.provider == "bitrix24").first()


def find_smart_process_ids(webhook_base: str) -> dict:
    response = _http_post(webhook_base, "crm.type.list", {})
    if "error" in response:
        return {"_error": response["error"]}
    result = response.get("result", {})
    items = result.get("types") or result.get("items") or (result if isinstance(result, list) else [])
    found = {}
    for item in items:
        entity_id = item.get("entityTypeId") or item.get("entity_type_id") or item.get("id")
        if entity_id is not None:
            found[str(entity_id)] = item.get("title") or item.get("name") or str(entity_id)
    return found


def resolve_process_kinds(webhook_base: str) -> dict:
    # Реальные ID портала ООО «ГРАУНД». Для них дополнительный crm.type.list
    # не нужен на каждом рейсе; автообнаружение выполняется не чаще раза в 5 минут.
    cache_key = _normalize_webhook_base(webhook_base)
    cached = _cache_get(_PROCESS_KINDS_CACHE, cache_key, PROCESS_CACHE_TTL)
    if cached is not None:
        return dict(cached)
    result = dict(KNOWN_PROCESS_KINDS)
    for entity_id, title in find_smart_process_ids(webhook_base).items():
        if entity_id == "_error":
            continue
        normalized = title.lower()
        if PROCESS_NAME_PUKHTOVOZ in normalized:
            result[str(entity_id)] = TripType.PUKHTOVOZ
        elif PROCESS_NAME_SAMOSVAL in normalized:
            result[str(entity_id)] = TripType.SAMOSVAL
    _cache_put(_PROCESS_KINDS_CACHE, cache_key, dict(result))
    return result


def resolve_process_entity(webhook_base: str, kind) -> Optional[str]:
    kind_value = kind.value if hasattr(kind, "value") else str(kind)
    # В штатной конфигурации 1088/1092 известны заранее — не ждём сеть.
    for entity_id, process_kind in KNOWN_PROCESS_KINDS.items():
        if process_kind.value == kind_value:
            return entity_id
    for entity_id, process_kind in resolve_process_kinds(webhook_base).items():
        if process_kind.value == kind_value:
            return entity_id
    return None


def get_element_fields(webhook_base: str, entity_id: str, force: bool = False) -> dict:
    cache_key = (_normalize_webhook_base(webhook_base), str(entity_id))
    if not force:
        cached = _cache_get(_FIELD_SCHEMA_CACHE, cache_key, FIELD_SCHEMA_CACHE_TTL)
        if cached is not None:
            return cached
    response = _http_post(webhook_base, "crm.item.fields", {"entityTypeId": int(entity_id)})
    if "error" in response:
        return {"_error": response["error"]}
    fields = response.get("result", {}).get("fields", {})
    _cache_put(_FIELD_SCHEMA_CACHE, cache_key, fields)
    return fields


def fetch_item(webhook_base: str, entity_id: int, item_id: int) -> dict:
    response = _http_post(webhook_base, "crm.item.get", {"entityTypeId": int(entity_id), "id": int(item_id)})
    if "error" in response:
        return {"_error": response["error"]}
    return response.get("result", {}).get("item", {})


def _default_category_id(webhook_base: str, entity_id: str) -> int:
    response = _http_post(webhook_base, "crm.category.list", {"entityTypeId": int(entity_id)})
    categories = response.get("result", {}).get("categories", []) if "error" not in response else []
    for category in categories:
        if category.get("isDefault") in (True, "Y", "1", 1):
            return int(category["id"])
    return int(categories[0]["id"]) if categories else 0


def resolve_stage(webhook_base: str, entity_id: str, status, item_id=None):
    """Resolve a local status to the actual stage ID of this process category."""
    status_value = status if isinstance(status, RequestStatus) else RequestStatus(status)
    target_title = STATUS_STAGE_TITLES.get(status_value)
    if not target_title:
        return None, None
    category_id = None
    if item_id:
        item = fetch_item(webhook_base, int(entity_id), int(item_id))
        raw_category = item.get("categoryId") if isinstance(item, dict) else None
        if raw_category is not None:
            category_id = int(raw_category)
    if category_id is None:
        category_id = _default_category_id(webhook_base, entity_id)
    response = _http_post(webhook_base, "crm.status.list", {
        "filter": {"ENTITY_ID": f"DYNAMIC_{entity_id}_STAGE_{category_id}"},
    })
    statuses = response.get("result", []) if "error" not in response else []
    if isinstance(statuses, dict):
        statuses = statuses.get("statuses") or statuses.get("items") or []
    normalized_target = _normalize(target_title)
    for stage in statuses:
        title = stage.get("NAME") or stage.get("name") or stage.get("title")
        if _normalize(title) == normalized_target:
            stage_id = stage.get("STATUS_ID") or stage.get("statusId") or stage.get("id")
            if isinstance(stage_id, (str, int)) and not isinstance(stage_id, bool) and str(stage_id).strip():
                return str(stage_id), category_id
            return None, category_id
    return None, category_id


def status_from_stage(webhook_base: str, entity_id: int, item: dict):
    """Преобразует текущий канбан-этап Bitrix24 в локальный статус заявки."""
    stage_id = item.get("stageId") or item.get("stage_id")
    category_id = item.get("categoryId") if item.get("categoryId") is not None else item.get("category_id")
    if not stage_id or category_id is None:
        return None
    response = _http_post(webhook_base, "crm.status.list", {
        "filter": {"ENTITY_ID": f"DYNAMIC_{entity_id}_STAGE_{int(category_id)}"},
    })
    stages = response.get("result", []) if "error" not in response else []
    if isinstance(stages, dict):
        stages = stages.get("statuses") or stages.get("items") or []
    status_by_title = {
        _normalize(title): status for status, title in STATUS_STAGE_TITLES.items()
    }
    for stage in stages:
        candidate_id = stage.get("STATUS_ID") or stage.get("statusId") or stage.get("id")
        if str(candidate_id or "") != str(stage_id):
            continue
        title = stage.get("NAME") or stage.get("name") or stage.get("title")
        return status_by_title.get(_normalize(title))
    return None


def _normalize(text) -> str:
    return re.sub(r"[^а-яa-z0-9]+", " ", str(text or "").lower().replace("ё", "е")).strip()


def _field_label(info, fallback="") -> str:
    if not isinstance(info, dict):
        return str(fallback or "")
    for key in ("title", "formLabel", "listLabel"):
        value = info.get(key)
        if isinstance(value, dict):
            value = value.get("ru") or value.get("en") or next((v for v in value.values() if v), "")
        if value not in (None, ""):
            return str(value)
    return str(fallback or "")


def _field_alias_score(title: str, aliases) -> int:
    """Оценивает совпадение названия поля Bitrix с нашим логическим полем.

    Важно: однословные общие алиасы вроде «дата» не должны цепляться за
    системные поля «Дата создания»/«Дата изменения». Точное русское название
    всегда имеет приоритет над старым техническим кодом uf*.
    """
    normalized_title = _normalize(title)
    if not normalized_title:
        return 0
    best = 0
    for index, alias in enumerate(aliases or ()):
        normalized_alias = _normalize(alias)
        if not normalized_alias:
            continue
        if normalized_title == normalized_alias:
            # Более длинный/конкретный алиас чуть предпочтительнее.
            best = max(best, 10000 + len(normalized_alias) * 10 - index)
            continue
        # Частичное совпадение разрешаем только для достаточно конкретных
        # многословных названий. Алиас должен входить в полное название поля,
        # а не наоборот. Иначе короткое поле «Полигон» ошибочно совпадало сразу
        # с «Адрес полигона», «Контакт полигона» и «Навигация полигона».
        if len(normalized_alias.split()) >= 2 and len(normalized_alias) >= 7:
            if normalized_alias in normalized_title:
                best = max(best, 5000 + len(normalized_alias) * 10 - index)
            elif (
                normalized_title in normalized_alias
                and len(normalized_title.split()) >= 2
                and len(normalized_title) >= 7
            ):
                best = max(best, 4500 + len(normalized_title) * 10 - index)
    return best


def resolve_field_map(schema: dict) -> dict:
    """Находит реальные поля Bitrix по их видимым названиям.

    Раньше технические коды вроде ``ufReisDate``/``ufDriver`` имели
    приоритет. Если такие старые поля оставались в Bitrix пустыми, интеграция
    читала их вместо реальных полей «Подача машины», «Водитель», «Тариф»,
    «Адрес подачи» и т.д. Теперь сначала выбирается поле по названию, и лишь
    если его нет — используется legacy-код.
    """
    resolved = {"number": "title"}
    schema = schema or {}
    for logical, configured_code in FIELD_MAP.items():
        if logical == "number":
            continue
        aliases = FIELD_TITLES.get(logical, ())
        candidates = []
        for code, info in schema.items():
            if str(code).startswith("_"):
                continue
            # Системные companyId/contactIds — это ссылки Клиента, а не
            # текстовое пользовательское поле «Заказчик/Компания». Они
            # обрабатываются отдельно ниже при синхронизации клиента.
            if logical == "customer_name" and code in {"companyId", "contactId", "contactIds"}:
                continue
            score = _field_alias_score(_field_label(info, code), aliases)
            if score:
                candidates.append((score, code))
        if candidates:
            candidates.sort(key=lambda row: (-row[0], str(row[1])))
            resolved[logical] = candidates[0][1]
            continue
        if configured_code in schema:
            resolved[logical] = configured_code
    return resolved


def _raw_value_present(value) -> bool:
    """True, если поле Bitrix действительно содержит значение. Ноль считаем значением."""
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set)):
        return any(_raw_value_present(item) for item in value)
    if isinstance(value, dict):
        if not value:
            return False
        # Bitrix часто заворачивает значение в value/VALUE/id/ID.
        preferred = [value.get(key) for key in ("value", "VALUE", "id", "ID", "title", "TITLE", "name", "NAME") if key in value]
        if preferred:
            return any(_raw_value_present(item) for item in preferred)
        return any(_raw_value_present(item) for item in value.values())
    return True


def _logical_candidate_codes(schema: dict, logical: str) -> list[str]:
    """Все подходящие поля Bitrix для логического поля, от самого точного к legacy.

    Это важно после переименований: в смарт-процессе могут одновременно остаться
    два пользовательских поля с одинаковой подписью, одно пустое, второе рабочее.
    """
    schema = schema or {}
    aliases = FIELD_TITLES.get(logical, ())
    ranked = []
    for code, info in schema.items():
        if str(code).startswith("_"):
            continue
        if logical == "customer_name" and code in {"companyId", "contactId", "contactIds"}:
            continue
        score = _field_alias_score(_field_label(info, code), aliases)
        if score:
            ranked.append((score, str(code)))
    ranked.sort(key=lambda row: (-row[0], row[1]))
    result = []
    for _, code in ranked:
        if code not in result:
            result.append(code)
    legacy = FIELD_MAP.get(logical)
    if legacy and legacy in schema and legacy not in result:
        result.append(legacy)
    return result


def resolve_field_map_for_item(schema: dict, item: dict) -> dict:
    """Как resolve_field_map, но среди дублей выбирает поле, реально заполненное в карточке."""
    resolved = resolve_field_map(schema)
    item = item or {}
    for logical in FIELD_TITLES:
        codes = _logical_candidate_codes(schema, logical)
        filled = [code for code in codes if code in item and _raw_value_present(item.get(code))]
        if filled:
            resolved[logical] = filled[0]
    return resolved


def _logical_raw_candidates(item: dict, logical: str, mapping: dict, schema: dict):
    """Возвращает все реально заполненные кандидаты поля в карточке Bitrix."""
    seen = set()
    ordered = []
    preferred = mapping.get(logical) if isinstance(mapping, dict) else None
    if preferred:
        ordered.append(preferred)
    ordered.extend(_logical_candidate_codes(schema, logical))
    legacy = FIELD_MAP.get(logical)
    if legacy:
        ordered.append(legacy)
    for code in ordered:
        if not code or code in seen or code not in item:
            continue
        seen.add(code)
        raw = item.get(code)
        if _raw_value_present(raw):
            yield code, raw, (schema or {}).get(code, {})


def _as_bitrix_value(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return "" if value is None else value




def _planned_at_value(req):
    if not getattr(req, "planned_date", None):
        return ""
    raw_time = str(getattr(req, "planned_time", "") or "").strip()[:5]
    if not raw_time:
        raw_time = "00:00"
    try:
        return datetime.combine(req.planned_date, datetime.strptime(raw_time, "%H:%M").time()).isoformat(timespec="minutes")
    except ValueError:
        return f"{req.planned_date.isoformat()}T{raw_time}"

def _polygon_cost_value(req, db=None):
    """Стоимость полигона для Bitrix, даже если снимок тарифа ещё не зафиксирован.

    Для новых рейсов из Bitrix snapshot появляется позднее, поэтому старый код
    отправлял пустоту. Теперь берём точный тариф полигона по типу груза, а затем
    безопасный legacy-тариф самого полигона.
    """
    if getattr(req, "polygon_cost_manual", None) is not None:
        return float(req.polygon_cost_manual or 0)
    rate = getattr(req, "polygon_rate_snapshot", None)
    unit = str(getattr(req, "polygon_unit_snapshot", "") or "").lower()
    if rate is None and db is not None and getattr(req, "polygon_id", None):
        polygon_tariff = None
        if getattr(req, "cargo_type_id", None):
            polygon_tariff = db.query(models.PolygonTariff).filter(
                models.PolygonTariff.polygon_id == req.polygon_id,
                models.PolygonTariff.cargo_type_id == req.cargo_type_id,
            ).first()
        if polygon_tariff:
            rate = float(polygon_tariff.rate or 0)
            unit = str(polygon_tariff.unit or "м³").lower()
        else:
            polygon = getattr(req, "polygon", None) or db.query(models.Polygon).filter(models.Polygon.id == req.polygon_id).first()
            if polygon:
                method = str(getattr(polygon, "calculation_method", "volume") or "volume").lower()
                if method == "tonnes":
                    rate = float(getattr(polygon, "tonnage_rate", 0) or 0)
                    unit = "т"
                elif method != "manual":
                    rate = float(getattr(polygon, "volume_rate", 0) or 0)
                    unit = "м³"
    if rate is None:
        return ""
    if "т" in unit:
        quantity = req.actual_tonnage if req.actual_tonnage is not None else (req.tonnage or 0)
    else:
        quantity = req.actual_volume if req.actual_volume is not None else (req.volume or 0)
    return float(rate or 0) * float(quantity or 0)


def _day_report_for_trip(req, db=None):
    if db is None or not getattr(req, "driver_id", None) or not getattr(req, "planned_date", None):
        return None
    return db.query(models.DriverDayReport).filter(
        models.DriverDayReport.driver_id == req.driver_id,
        models.DriverDayReport.report_date == req.planned_date,
    ).first()


def _trip_values(req, db=None) -> dict:
    day_report = _day_report_for_trip(req, db)
    fuel_liters = float(day_report.fuel_liters or 0) if day_report else ""
    fuel_price = float(day_report.fuel_price or 0) if day_report else ""
    return {
        "number": req.number or "",
        "planned_at": _planned_at_value(req),
        "driver_name": req.driver.full_name if req.driver else "",
        # В Bitrix поле называется «Машины», а в приложении оно соответствует госномеру.
        "vehicle_name": (req.vehicle.plate if req.vehicle else ""),
        "load_address": req.load_address or "",
        "unload_address": req.unload_address or "",
        "route_name": req.route_name or "",
        "trips_count": req.trips_count if req.trips_count is not None else 1,
        "cargo_type_name": req.cargo_type.name if req.cargo_type else "",
        "tariff_name": req.tariff.title if req.tariff else "",
        "km": req.km or 0,
        "volume": req.volume or 0,
        "tonnage": req.tonnage if req.tonnage is not None else "",
        "actual_km": req.actual_km if req.actual_km is not None else "",
        "actual_volume": req.actual_volume if req.actual_volume is not None else "",
        "actual_tonnage": req.actual_tonnage if req.actual_tonnage is not None else "",
        "status": req.status.value if hasattr(req.status, "value") else str(req.status),
        "customer_name": req.customer.name if req.customer else "",
        "customer_bitrix_id": req.customer.bitrix_company_id if req.customer and req.customer.bitrix_company_id is not None else "",
        "customer_inn": req.customer.inn if req.customer else "",
        "customer_contact_name": req.customer.contact if req.customer else "",
        "customer_contact_phone": req.customer.phone if req.customer else "",
        "customer_address": req.customer.address if req.customer else "",
        "polygon_name": req.polygon.name if req.polygon else "",
        "polygon_address": req.polygon.address if req.polygon else "",
        "polygon_contact": req.polygon.contact if req.polygon else "",
        "polygon_phone": req.polygon.phone if req.polygon else "",
        "polygon_navigator_url": req.polygon.navigator_url if req.polygon else "",
        "sum_trip": req.sum_trip if req.sum_trip is not None else "",
        "sum_driver": req.sum_driver if req.sum_driver is not None else "",
        "polygon_cost": _polygon_cost_value(req, db=db),
        "odometer": float(day_report.odometer or 0) if day_report else "",
        "fuel_liters": fuel_liters,
        "fuel_price": fuel_price,
        "fuel_cost": (fuel_liters * fuel_price) if day_report else "",
        "started_at": _as_bitrix_value(req.started_at),
        "finished_at": _as_bitrix_value(req.finished_at),
        "waste_bin_count": req.waste_bin_count if req.waste_bin_count is not None else "",
        "site_contact_name": req.site_contact_name or "",
        "site_contact_phone": req.site_contact_phone or "",
        "site_contact_comment": req.site_contact_comment or "",
        "is_empty_run": "Да" if req.is_empty_run else "Нет",
        "empty_run_comment": req.empty_run_comment or "",
        "has_downtime": "Да" if req.has_downtime else "Нет",
        "downtime_minutes": req.downtime_minutes if req.downtime_minutes is not None else "",
        "downtime_comment": req.downtime_comment or "",
        "comment": req.comment or "",
        "logist_comment": req.logist_comment or "",
    }


def build_fields(req, field_map=None, db=None) -> dict:
    mapping = field_map or FIELD_MAP
    values = _trip_values(req, db=db)
    return {code: values[logical] for logical, code in mapping.items() if logical in values}



def _field_type(info: dict) -> str:
    data = info.get("data") if isinstance(info, dict) else None
    return str(
        (info or {}).get("type")
        or (info or {}).get("userTypeId")
        or (data or {}).get("userTypeId")
        or (data or {}).get("type")
        or ""
    ).lower()




def _field_code(mapping: dict, logical: str):
    return mapping.get(logical) or FIELD_MAP.get(logical)


def _field_info(schema: dict, mapping: dict, logical: str) -> dict:
    code = _field_code(mapping, logical)
    return schema.get(code, {}) if code and isinstance(schema, dict) else {}


def _field_options(info: dict) -> list:
    """Возвращает варианты list/enumeration поля из разных форматов crm.item.fields."""
    if not isinstance(info, dict):
        return []
    data = info.get("data") if isinstance(info.get("data"), dict) else {}
    for candidate in (info.get("items"), info.get("values"), info.get("enum"), data.get("items"), data.get("values"), data.get("enum")):
        if isinstance(candidate, list):
            return candidate
        if isinstance(candidate, dict):
            return list(candidate.values())
    return []


def _option_label(row) -> str:
    if isinstance(row, dict):
        for key in ("value", "VALUE", "name", "NAME", "title", "TITLE", "label", "LABEL"):
            if row.get(key) not in (None, ""):
                return str(row.get(key)).strip()
    return str(row or "").strip()


def _option_id(row):
    if isinstance(row, dict):
        for key in ("id", "ID", "valueId", "VALUE_ID", "xmlId", "XML_ID"):
            if row.get(key) not in (None, ""):
                return str(row.get(key)).strip()
    return None


def _enum_display_value(raw, info: dict):
    options = _field_options(info)
    if not options:
        return None
    raw_values = raw if isinstance(raw, list) else [raw]
    labels = []
    for value in raw_values:
        scalar = _scalar(value)
        value_text = str(scalar or "").strip()
        matched = next((row for row in options if _option_id(row) == value_text), None)
        if matched is None:
            matched = next((row for row in options if _normalize(_option_label(row)) == _normalize(value_text)), None)
        if matched is not None:
            label = _option_label(matched)
            if label:
                labels.append(label)
    return ", ".join(labels) if labels else None


def _extract_bitrix_user_id(raw):
    """Достаёт ID сотрудника из разных форматов пользовательского поля Bitrix."""
    if isinstance(raw, list):
        for value in raw:
            found = _extract_bitrix_user_id(value)
            if found:
                return found
        return None
    if isinstance(raw, dict):
        for key in ("id", "ID", "userId", "USER_ID", "value", "VALUE"):
            if raw.get(key) not in (None, ""):
                found = _extract_bitrix_user_id(raw.get(key))
                if found:
                    return found
        return None
    text = str(raw or "").strip()
    if not text:
        return None
    # В разных пользовательских полях встречаются 77, U77, USER_77,
    # user:77 и похожие представления одного и того же сотрудника.
    match = re.fullmatch(r"(?:(?:U|USER|EMPLOYEE|STAFF|WORKER|СОТРУДНИК)[_:\- ]*)?(\d+)", text, re.I)
    if not match:
        return None
    try:
        user_id = int(match.group(1))
    except (TypeError, ValueError):
        return None
    return user_id if user_id > 0 else None


def _bitrix_user_record(webhook_base: str, raw):
    """Возвращает безопасную карточку пользователя Bitrix с коротким кэшем.

    Для user.get входящему webhook нужен scope user/user_brief/user_basic.
    Если права не выданы, синхронизация рейса не падает: пишем понятный
    диагностический маркер и продолжаем обрабатывать остальные поля.
    """
    user_id = _extract_bitrix_user_id(raw)
    if not user_id:
        return None
    cache_key = (_normalize_webhook_base(webhook_base), int(user_id))
    cached = _cache_get(_BITRIX_USER_CACHE, cache_key, BITRIX_USER_CACHE_TTL)
    if cached is not None:
        return cached or None
    response = _http_post(webhook_base, "user.get", {"ID": int(user_id)})
    if not isinstance(response, dict):
        return _cache_put(_BITRIX_USER_CACHE, cache_key, {}) or None
    if "error" in response:
        error_code = str(response.get("error") or "").strip().lower()
        description = str(response.get("error_description") or "").strip().lower()
        if "scope" in error_code or "scope" in description or error_code in {"access_denied", "insufficient_scope"}:
            print("BITRIX_DRIVER_USER_SCOPE_MISSING", int(user_id), flush=True)
        else:
            print("BITRIX_DRIVER_USER_LOOKUP_ERROR", int(user_id), error_code[:80], flush=True)
        return _cache_put(_BITRIX_USER_CACHE, cache_key, {}) or None
    rows = response.get("result", [])
    if isinstance(rows, dict):
        rows = rows.get("items") or [rows]
    row = (rows or [None])[0]
    if not isinstance(row, dict):
        return _cache_put(_BITRIX_USER_CACHE, cache_key, {}) or None
    return _cache_put(_BITRIX_USER_CACHE, cache_key, row)


def check_user_directory_access(webhook_base: str) -> dict:
    """Проверяет, может ли текущий webhook читать сотрудников Bitrix.

    Это обязательное право для полей типа «Привязка к сотруднику»: в самом
    рейсе Bitrix хранит ID, а фамилию/имя нужно получить через user.get.
    """
    response = _http_post(webhook_base, "user.get", {"FILTER": {"ACTIVE": "Y"}})
    if not isinstance(response, dict):
        return {"ok": False, "reason": "invalid_response"}
    if "error" in response:
        error_code = str(response.get("error") or "").strip()
        description = str(response.get("error_description") or "").strip()
        lower = f"{error_code} {description}".lower()
        reason = "insufficient_scope" if "scope" in lower else "access_error"
        return {"ok": False, "reason": reason, "error": error_code[:80]}
    return {"ok": True}


def _bitrix_user_full_name(row: dict) -> str:
    if not isinstance(row, dict):
        return ""
    parts = [
        str(row.get("LAST_NAME") or row.get("lastName") or "").strip(),
        str(row.get("NAME") or row.get("name") or "").strip(),
        str(row.get("SECOND_NAME") or row.get("secondName") or "").strip(),
    ]
    return " ".join(part for part in parts if part).strip()


def _user_display_name(webhook_base: str, raw):
    row = _bitrix_user_record(webhook_base, raw)
    if row:
        display = _bitrix_user_full_name(row)
        if display:
            return display
    scalar = _scalar(raw)
    return str(scalar or "").strip()


def _crm_binding_parts(value):
    text = str(_scalar(value) or "").strip()
    match = re.fullmatch(r"([A-Za-z0-9]+)_(\d+)", text)
    if not match:
        return None, None
    prefix, item_id = match.groups()
    prefix_upper = prefix.upper()
    known = {"L": 1, "D": 2, "C": 3, "CO": 4, "SI": 31}
    if prefix_upper in known:
        return known[prefix_upper], int(item_id)
    if prefix_upper.startswith("T"):
        try:
            return int(prefix_upper[1:], 16), int(item_id)
        except ValueError:
            return None, None
    return None, None


def _linked_item_preferred_label(webhook_base: str, entity_type_id: int, item: dict) -> str:
    if entity_type_id == 3:
        return _contact_display_name(item)
    title = str(item.get("title") or item.get("TITLE") or "").strip()
    # Для связанного смарт-процесса «Машины» госномер часто хранится не в title,
    # а в отдельном поле. Ищем его по названию, чтобы в приложение не попадал ID связи.
    schema = get_element_fields(webhook_base, str(entity_type_id))
    if "_error" not in schema:
        plate_aliases = tuple(_normalize(x) for x in (
            "госномер", "государственный номер", "номер автомобиля", "регистрационный номер", "номер машины",
        ))
        for code, info in schema.items():
            label = _normalize(_field_label(info, code))
            if label in plate_aliases or any(alias and alias in label for alias in plate_aliases):
                value = item.get(code)
                enum_value = _enum_display_value(value, info)
                text = str(enum_value if enum_value is not None else _scalar(value) or "").strip()
                if text:
                    return text
    return title


def _crm_binding_display(webhook_base: str, raw):
    raw_values = raw if isinstance(raw, list) else [raw]
    labels = []
    for value in raw_values:
        entity_type_id, item_id = _crm_binding_parts(value)
        if not entity_type_id or not item_id:
            continue
        linked = fetch_item(webhook_base, entity_type_id, item_id)
        if "_error" in linked:
            continue
        label = _linked_item_preferred_label(webhook_base, entity_type_id, linked)
        if label:
            labels.append(label)
    return ", ".join(labels) if labels else None


def _display_field_value(webhook_base: str, raw, info: dict):
    field_type = _field_type(info)
    enum_value = _enum_display_value(raw, info)
    if enum_value is not None:
        return enum_value
    if field_type in {"user", "employee"}:
        return _user_display_name(webhook_base, raw)
    raw_values = raw if isinstance(raw, list) else [raw]
    looks_like_crm_binding = any(_crm_binding_parts(value)[0] for value in raw_values)
    if field_type in {"crm", "crm_entity"} or looks_like_crm_binding:
        linked = _crm_binding_display(webhook_base, raw)
        if linked:
            return linked
    if isinstance(raw, dict):
        for key in ("address", "ADDRESS", "text", "TEXT", "name", "NAME", "title", "TITLE", "value", "VALUE"):
            if raw.get(key) not in (None, ""):
                return str(raw.get(key)).strip()
    if isinstance(raw, list):
        values = [str(_scalar(v) or "").strip() for v in raw]
        return ", ".join(v for v in values if v)
    text = str(raw or "").strip()
    # Некоторые CRM-поля возвращают «ID_123|читаемое значение» или наоборот.
    if "|" in text:
        parts = [part.strip() for part in text.split("|") if part.strip()]
        readable = [part for part in parts if re.search(r"[A-Za-zА-Яа-яЁё]", part) and not re.fullmatch(r"(?:ID\s*[:=]?\s*)?\d+", part, re.I)]
        if readable:
            return max(readable, key=len)
    return text


def _read_display_logical(item: dict, logical: str, mapping: dict, schema: dict, webhook_base: str):
    code = _field_code(mapping, logical)
    if code and code in item:
        return _display_field_value(webhook_base, item.get(code), schema.get(code, {}) if isinstance(schema, dict) else {})
    fallback = FIELD_MAP.get(logical)
    if fallback and fallback in item:
        return _display_field_value(webhook_base, item.get(fallback), schema.get(fallback, {}) if isinstance(schema, dict) else {})
    return ""


def _clean_address_value(webhook_base: str, item: dict, logical: str, mapping: dict, schema: dict) -> str:
    value = _read_display_logical(item, logical, mapping, schema, webhook_base)
    text = str(value or "").strip()
    if not text:
        return ""
    # JSON-строка от адресного поля: берём только читаемый адрес.
    if text.startswith("{") or text.startswith("["):
        try:
            parsed = json.loads(text)
            displayed = _display_field_value(webhook_base, parsed, _field_info(schema, mapping, logical))
            if displayed:
                text = str(displayed).strip()
        except (ValueError, TypeError):
            pass
    # Не сохраняем служебный числовой ID, если Bitrix склеил его с адресом через |.
    if "|" in text:
        parts = [part.strip() for part in text.split("|") if part.strip()]
        readable = [part for part in parts if re.search(r"[A-Za-zА-Яа-яЁё]", part)]
        if readable:
            text = max(readable, key=len)
    return text


def _portal_timezone(webhook_base: str):
    """Возвращает timezone портала; server.time запрашивается максимум раз в 10 минут."""
    cache_key = _normalize_webhook_base(webhook_base)
    cached = _cache_get(_PORTAL_TZ_CACHE, cache_key, PORTAL_TZ_CACHE_TTL)
    if cached is not None:
        return cached
    try:
        response = _http_post(webhook_base, "server.time", {})
        raw = response.get("result") if isinstance(response, dict) else None
        if raw:
            current = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            if current.tzinfo is not None:
                return _cache_put(_PORTAL_TZ_CACHE, cache_key, current.tzinfo)
    except Exception:
        pass
    return None


def _parse_bitrix_planned_at(webhook_base: str, raw):
    """Разбирает поле «Подача машины» так же, как оно показано в Bitrix.

    Если Bitrix отдал UTC/другой offset, приводим его к часовому поясу
    самого портала (server.time), а не к UTC сервера Render.
    """
    text = str(_scalar(raw) or "").strip()
    if not text:
        return None, None
    # Русский/человеческий формат встречается в некоторых пользовательских полях.
    for fmt in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%d.%m.%Y"):
        try:
            parsed = datetime.strptime(text, fmt)
            return parsed.date(), parsed.strftime("%H:%M") if "%H" in fmt else None
        except ValueError:
            pass
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            portal_tz = _portal_timezone(webhook_base)
            if portal_tz is not None:
                parsed = parsed.astimezone(portal_tz)
        return parsed.date(), parsed.strftime("%H:%M")
    except ValueError:
        pass
    try:
        return date.fromisoformat(text[:10]), None
    except ValueError:
        return None, None


def _planned_at_outbound_value(req, webhook_base: str):
    if not getattr(req, "planned_date", None):
        return ""
    raw_time = str(getattr(req, "planned_time", "") or "00:00").strip()[:5] or "00:00"
    try:
        value = datetime.combine(req.planned_date, datetime.strptime(raw_time, "%H:%M").time())
    except ValueError:
        return _planned_at_value(req)
    portal_tz = _portal_timezone(webhook_base)
    if portal_tz is not None:
        value = value.replace(tzinfo=portal_tz)
    return value.isoformat(timespec="minutes")


def _driver_display_value(webhook_base: str, item: dict, mapping: dict, schema: dict) -> str:
    code = _field_code(mapping, "driver_name")
    if not code or code not in item:
        return ""
    raw = item.get(code)
    info = schema.get(code, {}) if isinstance(schema, dict) else {}
    display = str(_display_field_value(webhook_base, raw, info) or "").strip()
    # Поле «Водитель» может быть user/employee, integer либо возвращать
    # префиксированный ID вроде U77. Во всех этих случаях сначала пробуем
    # получить реальное ФИО сотрудника Bitrix.
    user_id = _extract_bitrix_user_id(raw)
    if user_id:
        resolved = _user_display_name(webhook_base, user_id)
        if resolved and not re.fullmatch(r"(?:U|USER[_:\- ]*)?\d+", str(resolved).strip(), re.I):
            return str(resolved).strip()
    return display


def _driver_text_candidates(webhook_base: str, raw, info: dict) -> list[str]:
    """Собирает все безопасные варианты ФИО из поля «Водитель» Bitrix."""
    candidates = []

    def add(value):
        text = str(value or "").strip()
        if not text or text in candidates:
            return
        candidates.append(text)

    add(_display_field_value(webhook_base, raw, info or {}))

    user_id = _extract_bitrix_user_id(raw)
    if user_id:
        row = _bitrix_user_record(webhook_base, user_id)
        if row:
            add(_bitrix_user_full_name(row))
            add(row.get("LAST_NAME") or row.get("lastName"))

    def walk(node):
        if isinstance(node, dict):
            # Сначала именно поля, где Bitrix обычно отдаёт имя/ФИО.
            for key in (
                "fullName", "FULL_NAME", "displayName", "DISPLAY_NAME",
                "lastName", "LAST_NAME", "name", "NAME", "title", "TITLE",
                "label", "LABEL", "text", "TEXT", "value", "VALUE",
            ):
                if node.get(key) not in (None, ""):
                    value = node.get(key)
                    if not isinstance(value, (dict, list, tuple, set)):
                        add(value)
            for value in node.values():
                if isinstance(value, (dict, list, tuple, set)):
                    walk(value)
        elif isinstance(node, (list, tuple, set)):
            for value in node:
                walk(value)
        else:
            text = str(node or "").strip()
            # Чистые ID не помогают сопоставить локального водителя.
            if text and re.search(r"[A-Za-zА-Яа-яЁё]", text) and not re.fullmatch(r"(?:U|USER[_:\- ]*)?\d+", text, re.I):
                add(text)

    walk(raw)
    return candidates


def _driver_binding_candidates(webhook_base: str, raw) -> list[str]:
    """ФИО из CRM-привязки, если поле «Водитель» связано с другим смарт-процессом."""
    result = []
    values = raw if isinstance(raw, list) else [raw]
    aliases = tuple(_normalize(x) for x in (
        "водитель", "фио", "фио водителя", "полное имя", "фамилия имя", "фамилия", "сотрудник",
    ))
    for value in values:
        entity_type_id, linked_id = _crm_binding_parts(value)
        if not entity_type_id or not linked_id:
            continue
        linked = fetch_item(webhook_base, entity_type_id, linked_id)
        if "_error" in linked:
            continue
        title = str(linked.get("title") or linked.get("TITLE") or "").strip()
        if title:
            result.append(title)
        linked_schema = get_element_fields(webhook_base, str(entity_type_id))
        if "_error" in linked_schema:
            continue
        for code, info in linked_schema.items():
            label = _normalize(_field_label(info, code))
            if label not in aliases and not any(alias and alias in label for alias in aliases if len(alias) >= 4):
                continue
            display = str(_display_field_value(webhook_base, linked.get(code), info) or "").strip()
            if display:
                result.append(display)
    unique = []
    for value in result:
        if value not in unique:
            unique.append(value)
    return unique


def _resolve_local_driver(db, webhook_base: str, item: dict, mapping: dict, schema: dict):
    """Разрешает «Водителя» Bitrix в конкретную учётную запись приложения.

    Проверяем не только одно выбранное поле схемы, а все заполненные поля с
    точным названием «Водитель». Это защищает от дублей после переименований.
    """
    all_candidates = []
    for code, raw, info in _logical_raw_candidates(item, "driver_name", mapping, schema):
        for candidate in _driver_text_candidates(webhook_base, raw, info):
            if candidate not in all_candidates:
                all_candidates.append(candidate)
        for candidate in _driver_binding_candidates(webhook_base, raw):
            if candidate not in all_candidates:
                all_candidates.append(candidate)
    for candidate in all_candidates:
        driver = _find_driver_by_surname(db, candidate)
        if driver:
            return driver, candidate
    return None, (all_candidates[0] if all_candidates else "")


def _company_id_from_field(raw, info: dict):
    """Извлекает company ID из штатной или пользовательской CRM-привязки."""
    values = raw if isinstance(raw, list) else [raw]
    for value in values:
        if isinstance(value, dict):
            entity_type = value.get("entityTypeId") or value.get("entity_type_id") or value.get("typeId")
            item_id = value.get("id") or value.get("itemId") or value.get("entityId") or value.get("value")
            try:
                if int(entity_type or 4) == 4 and int(item_id) > 0:
                    return int(item_id)
            except (TypeError, ValueError):
                pass
        entity_type, item_id = _crm_binding_parts(value)
        if entity_type == 4 and item_id:
            return int(item_id)
    field_type = _field_type(info)
    # crm_company нередко возвращает просто числовой ID.
    if "company" in field_type or field_type in {"crm", "crm_entity"}:
        scalar = _scalar(raw)
        try:
            candidate = int(str(scalar).strip())
            return candidate if candidate > 0 else None
        except (TypeError, ValueError):
            return None
    return None


def _find_tariff_by_bitrix_value(db, kind, value: str):
    text = str(value or "").strip()
    if not text:
        return None
    rows = db.query(models.Tariff).filter(models.Tariff.kind == kind).all()
    normalized = _normalize(text)
    exact = [row for row in rows if _normalize(row.title) == normalized]
    if len(exact) == 1:
        return exact[0]
    contains = [row for row in rows if _normalize(row.title) and (
        _normalize(row.title) in normalized or normalized in _normalize(row.title)
    )]
    if len(contains) == 1:
        return contains[0]
    # Если в Bitrix поле «Тариф» хранит саму сумму, связываем только при
    # единственном однозначном совпадении цены среди тарифов нужного типа.
    number_match = re.fullmatch(r"\s*([0-9]+(?:[.,][0-9]+)?)\s*(?:₽|руб(?:\.|лей)?)?\s*", text, re.I)
    if number_match:
        amount = float(number_match.group(1).replace(",", "."))
        priced = []
        for row in rows:
            values = (row.trip_price, row.fixed_sum, row.km_price, row.volume_price)
            if any(v is not None and abs(float(v) - amount) < 0.000001 for v in values):
                priced.append(row)
        if len(priced) == 1:
            return priced[0]
    return None

def _find_driver_by_surname(db, display_name: str):
    text = str(display_name or "").strip()
    if not text:
        return None
    drivers = db.query(models.User).filter(models.User.role == models.UserRole.DRIVER).all()
    normalized = _normalize(text)
    if not normalized:
        return None

    # 1. Полное ФИО — самый надёжный вариант.
    exact = [d for d in drivers if _normalize(d.full_name) == normalized]
    if len(exact) == 1:
        return exact[0]

    # Иногда в Bitrix в поле возвращается логин сотрудника вместо ФИО.
    login_exact = [d for d in drivers if _normalize(getattr(d, "login", "")) == normalized]
    if len(login_exact) == 1:
        return login_exact[0]

    incoming_tokens = [token for token in normalized.split() if len(token) > 1 and not token.isdigit()]
    if not incoming_tokens:
        return None

    # 2. Пользователь просит сопоставлять именно по фамилии. В локальной базе
    # фамилия обычно стоит первой, но поддерживаем и запись «Имя Фамилия».
    # Совпадение принимаем только если оно указывает ровно на одного водителя.
    candidates = []
    for driver in drivers:
        local_tokens = [t for t in _normalize(driver.full_name).split() if len(t) > 1]
        if not local_tokens:
            continue
        surname_tokens = {local_tokens[0], local_tokens[-1]}
        if surname_tokens.intersection(incoming_tokens):
            candidates.append(driver)
    unique = []
    for driver in candidates:
        if driver.id not in {row.id for row in unique}:
            unique.append(driver)
    return unique[0] if len(unique) == 1 else None


def _normalize_plate(value: str) -> str:
    # Российские номера часто вводят смесью латиницы/кириллицы. Для сравнения
    # приводим визуально одинаковые допустимые буквы к одному латинскому виду.
    text = re.sub(r"[^A-Za-zА-Яа-я0-9]", "", str(value or "")).upper().replace("Ё", "Е")
    visual = str.maketrans({
        "А": "A", "В": "B", "Е": "E", "К": "K", "М": "M", "Н": "H",
        "О": "O", "Р": "P", "С": "C", "Т": "T", "У": "Y", "Х": "X",
    })
    return text.translate(visual)


def _find_vehicle_by_bitrix_value(db, display_value: str):
    text = str(display_value or "").strip()
    if not text:
        return None
    normalized_plate = _normalize_plate(text)
    vehicles = db.query(models.Vehicle).all()
    exact_plate = [v for v in vehicles if _normalize_plate(v.plate) == normalized_plate]
    if len(exact_plate) == 1:
        return exact_plate[0]
    embedded = [v for v in vehicles if _normalize_plate(v.plate) and _normalize_plate(v.plate) in normalized_plate]
    if len(embedded) == 1:
        return embedded[0]
    exact_name = [v for v in vehicles if _normalize(v.name) == _normalize(text)]
    if len(exact_name) == 1:
        return exact_name[0]
    return None



def _enum_outbound_value(value, info: dict):
    options = _field_options(info)
    if not options:
        return None
    target = _normalize(value)
    plate_target = _normalize_plate(value)
    exact = []
    contains = []
    for row in options:
        label = _option_label(row)
        option_id = _option_id(row)
        if not label or option_id is None:
            continue
        normalized_label = _normalize(label)
        if normalized_label == target:
            exact.append(option_id)
        elif target and (target in normalized_label or normalized_label in target):
            contains.append(option_id)
        elif plate_target and plate_target in _normalize_plate(label):
            contains.append(option_id)
    if len(exact) == 1:
        return exact[0]
    if len(contains) == 1:
        return contains[0]
    return None


def _bitrix_user_id_by_name(webhook_base: str, full_name: str):
    text = str(full_name or "").strip()
    if not text:
        return None
    tokens = _normalize(text).split()
    surname = tokens[0] if tokens else ""
    attempts = []
    if surname:
        attempts.append({"FILTER": {"LAST_NAME": surname}})
    attempts.append({})
    for params in attempts:
        response = _http_post(webhook_base, "user.get", params)
        if "error" in response:
            error_code = str(response.get("error") or "").strip().lower()
            description = str(response.get("error_description") or "").strip().lower()
            if "scope" in error_code or "scope" in description:
                print("BITRIX_DRIVER_USER_SCOPE_MISSING", "outbound", flush=True)
            continue
        rows = response.get("result", [])
        if isinstance(rows, dict):
            rows = rows.get("items") or [rows]
        matches = []
        surname_matches = []
        for row in rows or []:
            display = " ".join(str(row.get(key) or "").strip() for key in ("LAST_NAME", "NAME", "SECOND_NAME") if str(row.get(key) or "").strip())
            if _normalize(display) == _normalize(text):
                matches.append(row)
            elif surname and surname in {_normalize(row.get("LAST_NAME")), _normalize(row.get("NAME"))}:
                surname_matches.append(row)
        chosen = matches[0] if len(matches) == 1 else (surname_matches[0] if len(surname_matches) == 1 else None)
        if chosen:
            try:
                return int(chosen.get("ID") or chosen.get("id"))
            except (TypeError, ValueError):
                return None
    return None


def _extract_dynamic_entity_ids(value) -> list[int]:
    found = []
    def walk(node):
        if isinstance(node, dict):
            for key, child in node.items():
                walk(key); walk(child)
        elif isinstance(node, (list, tuple, set)):
            for child in node:
                walk(child)
        else:
            text = str(node or "")
            for match in re.finditer(r"DYNAMIC[_:\s-]?(\d+)", text, re.I):
                try:
                    found.append(int(match.group(1)))
                except ValueError:
                    pass
    walk(value)
    result = []
    for entity_id in found:
        if entity_id not in result:
            result.append(entity_id)
    return result


def _preferred_link_field_code(schema: dict, logical: str):
    aliases_by_logical = {
        "vehicle_name": ("госномер", "государственный номер", "номер автомобиля", "регистрационный номер", "номер машины"),
        "polygon_name": ("название полигона", "полигон"),
        "tariff_name": ("название тарифа", "тариф"),
        "cargo_type_name": ("тип груза", "груз"),
    }
    aliases = tuple(_normalize(x) for x in aliases_by_logical.get(logical, ()))
    for code, info in (schema or {}).items():
        label = _normalize(_field_label(info, code))
        if label in aliases or any(alias and alias in label for alias in aliases):
            return code
    return None


def _find_dynamic_binding(webhook_base: str, entity_type_id: int, logical: str, desired: str):
    target = str(desired or "").strip()
    if not target:
        return None
    schema = get_element_fields(webhook_base, str(entity_type_id))
    preferred_code = None if "_error" in schema else _preferred_link_field_code(schema, logical)
    select = ["id", "title"] + ([preferred_code] if preferred_code else [])
    # Сначала точный title — это дешёвый путь.
    response = _http_post(webhook_base, "crm.item.list", {
        "entityTypeId": int(entity_type_id), "filter": {"title": target}, "select": select, "start": 0,
    })
    items = _list_result_items(response) if "error" not in response else []
    if not items:
        response = _http_post(webhook_base, "crm.item.list", {
            "entityTypeId": int(entity_type_id), "select": select, "start": 0,
        })
        items = _list_result_items(response) if "error" not in response else []
    desired_norm = _normalize(target)
    desired_plate = _normalize_plate(target)
    matches = []
    for row in items[:50]:
        candidates = [str(row.get("title") or "").strip()]
        if preferred_code:
            info = schema.get(preferred_code, {})
            candidates.append(str(_display_field_value(webhook_base, row.get(preferred_code), info) or "").strip())
        if any(
            _normalize(candidate) == desired_norm
            or (desired_plate and desired_plate == _normalize_plate(candidate))
            or (desired_plate and desired_plate in _normalize_plate(candidate))
            for candidate in candidates if candidate
        ):
            try:
                matches.append(int(row.get("id")))
            except (TypeError, ValueError):
                pass
    if len(set(matches)) != 1:
        return None
    item_id = matches[0]
    return f"T{int(entity_type_id):x}_{item_id}"


def _prepare_outbound_reference_fields(fields: dict, values: dict, mapping: dict, schema: dict, webhook_base: str):
    """Приводит человекочитаемые значения к ID для list/user/CRM-полей Bitrix.

    Если привязку нельзя однозначно определить, поле пропускается вместо того,
    чтобы из-за одного неверного ID откатить синхронизацию всей карточки рейса.
    """
    prepared = dict(fields)
    for logical, code in mapping.items():
        if code not in prepared or code not in schema:
            continue
        info = schema.get(code, {})
        field_type = _field_type(info)
        value = values.get(logical)
        if value in (None, ""):
            continue
        if _field_options(info):
            mapped = _enum_outbound_value(value, info)
            if mapped is None:
                prepared.pop(code, None)
                print("BITRIX_SYNC_REFERENCE_SKIP", logical, "enumeration", flush=True)
            else:
                prepared[code] = mapped
            continue
        if field_type in {"user", "employee"} or (logical == "driver_name" and field_type in {"integer", "int"}):
            mapped = _bitrix_user_id_by_name(webhook_base, str(value))
            if mapped is None:
                prepared.pop(code, None)
                print("BITRIX_SYNC_REFERENCE_SKIP", logical, "user", flush=True)
            else:
                prepared[code] = mapped
            continue
        if field_type in {"crm", "crm_entity"}:
            entity_ids = _extract_dynamic_entity_ids(info)
            mapped = None
            for target_entity in entity_ids:
                mapped = _find_dynamic_binding(webhook_base, target_entity, logical, str(value))
                if mapped:
                    break
            if mapped is None:
                prepared.pop(code, None)
                print("BITRIX_SYNC_REFERENCE_SKIP", logical, "crm", flush=True)
            else:
                prepared[code] = mapped
    return prepared

def _attachment_field(schema: dict, mapping: dict):
    code = mapping.get("attachments")
    if code and code in schema and _field_type(schema.get(code, {})) == "file":
        return code
    aliases = tuple(_normalize(x) for x in FIELD_TITLES["attachments"])
    for field_code, info in schema.items():
        if _field_type(info) != "file":
            continue
        title = _normalize(info.get("title") or info.get("formLabel") or info.get("listLabel") or field_code)
        if title in aliases or any(alias and alias in title for alias in aliases):
            return field_code
    return None


def _list_result_items(response: dict):
    result = response.get("result", {}) if isinstance(response, dict) else {}
    if isinstance(result, list):
        return result
    return result.get("items") or result.get("types") or []


def _first_phone(item: dict) -> str:
    for row in item.get("fm") or []:
        if isinstance(row, dict) and str(row.get("typeId") or "").upper() == "PHONE":
            value = str(row.get("value") or "").strip()
            if value:
                return value
    for key in ("phoneWork", "phoneMobile", "phone"):
        value = str(item.get(key) or "").strip()
        if value:
            return value
    return ""


def _contact_display_name(item: dict) -> str:
    parts = [str(item.get(key) or "").strip() for key in ("lastName", "name", "secondName")]
    value = " ".join(part for part in parts if part).strip()
    return value or str(item.get("title") or "").strip()


def _find_company_by_name(webhook_base: str, name: str):
    response = _http_post(webhook_base, "crm.item.list", {
        "entityTypeId": 4, "filter": {"title": name}, "select": ["id", "title"],
    })
    for item in _list_result_items(response):
        if _normalize(item.get("title")) == _normalize(name):
            return int(item.get("id"))
    return None


def _company_requisite_inn(webhook_base: str, company_id: int) -> str:
    response = _http_post(webhook_base, "crm.requisite.list", {
        "filter": {"ENTITY_TYPE_ID": 4, "ENTITY_ID": int(company_id)},
        "select": ["ID", "ENTITY_ID", "RQ_INN"],
    })
    if "error" in response:
        return ""
    rows = response.get("result") or []
    if isinstance(rows, dict):
        rows = rows.get("items") or rows.get("requisites") or []
    for row in rows if isinstance(rows, list) else []:
        inn = re.sub(r"\D", "", str((row or {}).get("RQ_INN") or ""))
        if len(inn) in {10, 12}:
            return inn
    return ""


def _find_company_by_inn(webhook_base: str, inn: str):
    clean = re.sub(r"\D", "", str(inn or ""))
    if len(clean) not in {10, 12}:
        return None
    response = _http_post(webhook_base, "crm.requisite.list", {
        "filter": {"RQ_INN": clean},
        "select": ["ID", "ENTITY_ID", "RQ_INN"],
    })
    if "error" in response:
        return None
    rows = response.get("result") or []
    if isinstance(rows, dict):
        rows = rows.get("items") or rows.get("requisites") or []
    for row in rows if isinstance(rows, list) else []:
        entity_id = (row or {}).get("ENTITY_ID")
        if entity_id:
            try:
                return int(entity_id)
            except (TypeError, ValueError):
                pass
    return None


def _ensure_company_inn(webhook_base: str, company_id: int, company_name: str, inn: str):
    clean = re.sub(r"\D", "", str(inn or ""))
    if len(clean) not in {10, 12}:
        return
    existing = _http_post(webhook_base, "crm.requisite.list", {
        "filter": {"ENTITY_TYPE_ID": 4, "ENTITY_ID": int(company_id)},
        "select": ["ID", "ENTITY_ID", "PRESET_ID", "RQ_INN", "NAME"],
    })
    rows = existing.get("result") or [] if "error" not in existing else []
    if isinstance(rows, dict):
        rows = rows.get("items") or rows.get("requisites") or []
    rows = rows if isinstance(rows, list) else []
    for row in rows:
        req_id = (row or {}).get("ID")
        current_inn = re.sub(r"\D", "", str((row or {}).get("RQ_INN") or ""))
        if current_inn == clean:
            return
        if req_id and not current_inn:
            _http_post(webhook_base, "crm.requisite.update", {"id": int(req_id), "fields": {"RQ_INN": clean}})
            return
    presets = _http_post(webhook_base, "crm.requisite.preset.list", {
        "filter": {"ENTITY_TYPE_ID": 4},
        "select": ["ID", "NAME", "ENTITY_TYPE_ID", "COUNTRY_ID"],
    })
    preset_rows = presets.get("result") or [] if "error" not in presets else []
    if isinstance(preset_rows, dict):
        preset_rows = preset_rows.get("items") or preset_rows.get("presets") or []
    preset_id = None
    for row in preset_rows if isinstance(preset_rows, list) else []:
        raw = (row or {}).get("ID")
        if raw:
            try:
                preset_id = int(raw)
                break
            except (TypeError, ValueError):
                pass
    if preset_id:
        _http_post(webhook_base, "crm.requisite.add", {
            "fields": {
                "ENTITY_TYPE_ID": 4,
                "ENTITY_ID": int(company_id),
                "PRESET_ID": preset_id,
                "NAME": company_name or "Реквизиты",
                "ACTIVE": "Y",
                "RQ_INN": clean,
            }
        })


def _ensure_bitrix_customer(customer, webhook_base: str):
    """Создаёт/находит CRM-компанию и контакт только когда локальному заказчику не хватает ID."""
    if not customer:
        return None, None
    company_id = customer.bitrix_company_id
    if not company_id and customer.inn:
        company_id = _find_company_by_inn(webhook_base, customer.inn)
    if not company_id and customer.name:
        company_id = _find_company_by_name(webhook_base, customer.name)
        if not company_id:
            fields = {"title": customer.name}
            company_schema = get_element_fields(webhook_base, "4")
            if customer.address and "address" in company_schema:
                fields["address"] = customer.address
            if customer.phone:
                fields["fm"] = [{"typeId": "PHONE", "valueType": "WORK", "value": customer.phone}]
            response = _http_post(webhook_base, "crm.item.add", {"entityTypeId": 4, "fields": fields})
            if "error" not in response:
                result = response.get("result", {})
                company_id = result.get("id") or result.get("item", {}).get("id")
        if company_id:
            customer.bitrix_company_id = int(company_id)
    if company_id and customer.inn:
        _ensure_company_inn(webhook_base, int(company_id), customer.name, customer.inn)

    contact_id = getattr(customer, "bitrix_contact_id", None)
    if not contact_id and company_id:
        links = _http_post(webhook_base, "crm.company.contact.items.get", {"id": int(company_id)})
        candidates = links.get("result", []) if "error" not in links else []
        if isinstance(candidates, list) and candidates:
            primary = next((row for row in candidates if row.get("IS_PRIMARY") == "Y"), candidates[0])
            raw_contact_id = primary.get("CONTACT_ID")
            if raw_contact_id:
                contact_id = int(raw_contact_id)
                customer.bitrix_contact_id = contact_id

    if not contact_id and (customer.contact or customer.phone):
        clean_name = (customer.contact or customer.name or "Контакт").strip()
        parts = clean_name.split(maxsplit=1)
        fields = {"name": parts[0] if parts else clean_name}
        if len(parts) > 1:
            fields["lastName"] = parts[1]
        if customer.phone:
            fields["fm"] = [{"typeId": "PHONE", "valueType": "WORK", "value": customer.phone}]
        response = _http_post(webhook_base, "crm.item.add", {"entityTypeId": 3, "fields": fields})
        if "error" not in response:
            result = response.get("result", {})
            contact_id = result.get("id") or result.get("item", {}).get("id")
            if contact_id:
                contact_id = int(contact_id)
                customer.bitrix_contact_id = contact_id
                if company_id:
                    _http_post(webhook_base, "crm.company.contact.add", {
                        "id": int(company_id), "fields": {"CONTACT_ID": contact_id, "IS_PRIMARY": "Y"},
                    })
    return int(company_id) if company_id else None, int(contact_id) if contact_id else None


def _type_info_by_entity(webhook_base: str, entity_id: int):
    response = _http_post(webhook_base, "crm.type.getByEntityTypeId", {"entityTypeId": int(entity_id)})
    if "error" in response:
        return {}
    return response.get("result", {}).get("type", {})


def ensure_client_field_enabled(webhook_base: str, entity_id: int) -> dict:
    """Безопасно включает системное поле «Клиент» в SPA, если webhook имеет права администратора CRM."""
    info = _type_info_by_entity(webhook_base, int(entity_id))
    if not info:
        return {"skipped": True, "reason": "type_info_unavailable"}
    if info.get("isClientEnabled") in ("Y", True, 1, "1"):
        return {"ok": True, "already": True}
    type_id = info.get("id")
    if not type_id:
        return {"skipped": True, "reason": "type_id_missing"}
    response = _http_post(webhook_base, "crm.type.update", {"id": int(type_id), "fields": {"isClientEnabled": "Y"}})
    if "error" in response:
        return {"error": response["error"]}
    print("BITRIX_CLIENT_ENABLED", entity_id, flush=True)
    return {"ok": True, "already": False}


def _attachment_bytes(attachment):
    if attachment.content:
        return bytes(attachment.content)
    path = str(attachment.path or "")
    if path and os.path.isfile(path):
        with open(path, "rb") as fh:
            return fh.read()
    return b""


def _remote_file_list(value):
    if not value:
        return []
    if isinstance(value, dict) and "id" in value:
        return [value]
    if isinstance(value, list):
        return [row for row in value if isinstance(row, dict) and row.get("id")]
    return []


def ensure_attachment_field(webhook_base: str, entity_id: int):
    schema = get_element_fields(webhook_base, str(entity_id))
    if "_error" not in schema:
        mapping = resolve_field_map(schema)
        existing = _attachment_field(schema, mapping)
        if existing:
            return existing, schema, {"ok": True, "already": True}
    info = _type_info_by_entity(webhook_base, int(entity_id))
    type_id = info.get("id") if info else None
    if not type_id:
        return None, schema, {"skipped": True, "reason": "type_id_missing"}
    field_name = f"UF_CRM_{int(type_id)}_GROUND_TRIP_FILES"
    response = _http_post(webhook_base, "userfieldconfig.add", {
        "moduleId": "crm",
        "field": {
            "entityId": f"CRM_{int(type_id)}",
            "fieldName": field_name,
            "userTypeId": "file",
            "multiple": "Y",
            "mandatory": "N",
            "editFormLabel": {"ru": "Файлы рейса", "en": "Trip files"},
            "settings": {"EXTENSIONS": ["jpg", "jpeg", "png", "webp", "pdf"]},
        },
    })
    if "error" in response:
        return None, schema, {"error": response["error"], "reason": "file_field_create_failed"}
    schema = get_element_fields(webhook_base, str(entity_id))
    if "_error" in schema:
        return None, schema, {"error": schema["_error"], "reason": "fields_refresh_failed"}
    field_code = _attachment_field(schema, resolve_field_map(schema))
    if field_code:
        print("BITRIX_ATTACHMENT_FIELD_CREATED", entity_id, flush=True)
        return field_code, schema, {"ok": True, "already": False}
    return None, schema, {"error": "bitrix_attachment_field_missing_after_create"}


def sync_attachments(req, db, settings=None) -> dict:
    settings = settings or get_integration_settings(db)
    if not settings or not settings.is_active or not settings.webhook_url:
        return {"skipped": True, "reason": "no_active_integration"}
    entity_id = resolve_process_entity(settings.webhook_url, req.kind)
    if not entity_id:
        return {"skipped": True, "reason": "process_not_found"}
    if not req.bitrix_element_id:
        trip_result = sync_trip(req, db, settings=settings)
        if not trip_result.get("ok"):
            return trip_result
    field_code, schema, field_state = ensure_attachment_field(settings.webhook_url, int(entity_id))
    if not field_code:
        print("BITRIX_ATTACHMENT_SYNC_SKIP", field_state.get("reason") or "file_field_not_found", entity_id, req.id, flush=True)
        return field_state
    mapping = resolve_field_map(schema)
    remote_item = fetch_item(settings.webhook_url, int(entity_id), int(req.bitrix_element_id))
    if "_error" in remote_item:
        return {"error": remote_item["_error"], "action": "get"}
    remote_files = _remote_file_list(remote_item.get(field_code))
    remote_ids = {int(row["id"]) for row in remote_files if row.get("id")}
    local_files = db.query(models.Attachment).filter(models.Attachment.trip_request_id == req.id).order_by(models.Attachment.id).all()
    for attachment in local_files:
        if attachment.bitrix_file_id and int(attachment.bitrix_file_id) not in remote_ids:
            attachment.bitrix_file_id = None
    unsynced = [row for row in local_files if not row.bitrix_file_id]
    if not unsynced:
        return {"ok": True, "action": "attachments", "count": len(remote_files)}
    values = [{"id": int(row["id"])} for row in remote_files]
    uploadable = []
    for attachment in unsynced:
        content = _attachment_bytes(attachment)
        if not content:
            continue
        values.append([attachment.filename or "file", base64.b64encode(content).decode("ascii")])
        uploadable.append(attachment)
    if not uploadable:
        return {"skipped": True, "reason": "attachment_content_missing"}
    response = _http_post(settings.webhook_url, "crm.item.update", {
        "entityTypeId": int(entity_id), "id": int(req.bitrix_element_id), "fields": {field_code: values},
    })
    if "error" in response:
        return {"error": response["error"], "action": "attachments"}
    result_item = response.get("result", {}).get("item") or fetch_item(settings.webhook_url, int(entity_id), int(req.bitrix_element_id))
    final_files = _remote_file_list(result_item.get(field_code) if isinstance(result_item, dict) else None)
    new_ids = [int(row["id"]) for row in final_files if int(row.get("id") or 0) not in remote_ids]
    for attachment, remote_id in zip(uploadable, new_ids):
        attachment.bitrix_file_id = remote_id
        db.add(attachment)
    print("BITRIX_ATTACHMENT_SYNC_OK", entity_id, req.bitrix_element_id, req.id, len(uploadable), flush=True)
    return {"ok": True, "action": "attachments", "count": len(final_files)}


def _download_signed_file(url: str):
    req = urllib.request.Request(url, headers={
        "User-Agent": "ground-reis/1.0", "Accept": "*/*", "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
        "Referer": "https://ground-reis.ru/",
    })
    with urllib.request.urlopen(req, timeout=BITRIX_TIMEOUT) as resp:
        content = resp.read(10 * 1024 * 1024 + 1)
        if len(content) > 10 * 1024 * 1024:
            raise ValueError("bitrix_attachment_too_large")
        content_type = str(resp.headers.get_content_type() or "application/octet-stream").lower()
        filename = "bitrix-file"
        disposition = resp.headers.get("Content-Disposition")
        if disposition:
            msg = Message(); msg["Content-Disposition"] = disposition
            value = msg.get_filename()
            if value:
                filename = value
        return filename, content_type, content


def _guess_allowed_content_type(filename: str, content_type: str, content: bytes):
    value = str(content_type or "").split(";", 1)[0].lower()
    signatures = {
        "image/jpeg": content.startswith(b"\xff\xd8\xff"),
        "image/png": content.startswith(b"\x89PNG\r\n\x1a\n"),
        "image/webp": len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP",
        "application/pdf": content.startswith(b"%PDF-"),
    }
    if value in signatures and signatures[value]:
        return value
    lower_name = str(filename or "").lower()
    for suffix, guessed in ((".jpg", "image/jpeg"), (".jpeg", "image/jpeg"), (".png", "image/png"), (".webp", "image/webp"), (".pdf", "application/pdf")):
        if lower_name.endswith(suffix) and signatures[guessed]:
            return guessed
    return None


def sync_inbound_attachments(item: dict, trip, db, schema: dict, mapping: dict) -> dict:
    field_code = _attachment_field(schema, mapping)
    if not field_code or field_code not in item:
        return {"skipped": True}
    remote_files = _remote_file_list(item.get(field_code))
    remote_ids = {int(row["id"]) for row in remote_files if row.get("id")}
    local_remote = db.query(models.Attachment).filter(
        models.Attachment.trip_request_id == trip.id,
        models.Attachment.bitrix_file_id.isnot(None),
    ).all()
    for attachment in local_remote:
        if int(attachment.bitrix_file_id) not in remote_ids:
            db.delete(attachment)
    existing_ids = {int(row.bitrix_file_id) for row in local_remote if row.bitrix_file_id in remote_ids}
    current_count = db.query(models.Attachment).filter(models.Attachment.trip_request_id == trip.id).count()
    for remote in remote_files:
        remote_id = int(remote.get("id") or 0)
        if not remote_id or remote_id in existing_ids or current_count >= 5:
            continue
        url = str(remote.get("urlMachine") or "")
        if not url:
            continue
        try:
            filename, content_type, content = _download_signed_file(url)
            allowed_type = _guess_allowed_content_type(filename, content_type, content)
            if not allowed_type:
                continue
        except Exception as exc:
            print("BITRIX_ATTACHMENT_IMPORT_ERROR", trip.id, remote_id, type(exc).__name__, flush=True)
            continue
        db.add(models.Attachment(
            trip_request_id=trip.id, filename=filename[:512], content_type=allowed_type,
            size=len(content), path="", content=content, bitrix_file_id=remote_id,
        ))
        current_count += 1
    return {"ok": True, "count": len(remote_files)}




def sync_customer_from_bitrix_event(db, settings=None, company_id=None, contact_id=None) -> dict:
    """Обновляет локального заказчика при изменении карточки компании/контакта в Bitrix24."""
    settings = settings or get_integration_settings(db)
    if not settings or not settings.is_active or not settings.webhook_url:
        return {"skipped": True, "reason": "no_active_integration"}
    customer = None
    if company_id:
        customer = db.query(models.Customer).filter(models.Customer.bitrix_company_id == int(company_id)).first()
    if customer is None and contact_id:
        customer = db.query(models.Customer).filter(models.Customer.bitrix_contact_id == int(contact_id)).first()
    if customer is None:
        return {"ok": True, "skipped": True, "reason": "customer_not_linked"}

    company_item = {}
    contact_item = {}
    if company_id or customer.bitrix_company_id:
        company_item = fetch_item(settings.webhook_url, 4, int(company_id or customer.bitrix_company_id))
        if "_error" in company_item:
            return {"error": company_item["_error"], "action": "company_get"}
    if contact_id or customer.bitrix_contact_id:
        contact_item = fetch_item(settings.webhook_url, 3, int(contact_id or customer.bitrix_contact_id))
        if "_error" in contact_item:
            return {"error": contact_item["_error"], "action": "contact_get"}

    if company_item:
        new_name = str(company_item.get("title") or "").strip()
        if new_name:
            owner = db.query(models.Customer).filter(models.Customer.name == new_name, models.Customer.id != customer.id).first()
            if not owner:
                customer.name = new_name
        customer.address = str(company_item.get("address") or "").strip()
        customer.phone = _first_phone(company_item)
        company_inn = _company_requisite_inn(settings.webhook_url, int(company_id or customer.bitrix_company_id))
        if company_inn:
            customer.inn = company_inn
    if contact_item:
        customer.contact = _contact_display_name(contact_item)
        customer.phone = _first_phone(contact_item)
    db.add(customer)
    db.flush()
    print("BITRIX_CUSTOMER_INBOUND_OK", customer.id, int(company_id or 0), int(contact_id or 0), flush=True)
    return {"ok": True, "action": "customer_update", "customer_id": customer.id}


def sync_customer_from_requisite_event(db, requisite_id: int, settings=None) -> dict:
    settings = settings or get_integration_settings(db)
    if not settings or not settings.webhook_url:
        return {"skipped": True, "reason": "no_active_integration"}
    response = _http_post(settings.webhook_url, "crm.requisite.get", {"id": int(requisite_id)})
    if "error" in response:
        return {"error": response["error"], "action": "customer_update"}
    row = response.get("result") or {}
    try:
        entity_type_id = int(row.get("ENTITY_TYPE_ID") or 0)
        company_id = int(row.get("ENTITY_ID") or 0)
    except (TypeError, ValueError):
        return {"skipped": True, "reason": "customer_not_linked"}
    if entity_type_id != 4 or not company_id:
        return {"skipped": True, "reason": "customer_not_linked"}
    return sync_customer_from_bitrix_event(db, settings=settings, company_id=company_id)

def _log_important_field_map(schema: dict, mapping: dict, entity_type_id, item_id=None):
    important = ("customer_name", "planned_at", "load_address", "driver_name", "vehicle_name", "tariff_name", "polygon_cost")
    parts = []
    for logical in important:
        code = mapping.get(logical)
        label = _field_label(schema.get(code, {}), code) if code else "-"
        parts.append(f"{logical}={code or '-'}:{_normalize(label) or '-'}")
    print("BITRIX_FIELD_MAP", entity_type_id, item_id or 0, " ".join(parts), flush=True)


def sync_trip(req, db, settings=None) -> dict:
    settings = settings or get_integration_settings(db)
    if not settings or not settings.is_active or not settings.webhook_url:
        print("BITRIX_SYNC_SKIP", "no_active_integration", req.id, getattr(req.kind, "value", req.kind), flush=True)
        return {"skipped": True, "reason": "no_active_integration"}
    entity_id = resolve_process_entity(settings.webhook_url, req.kind)
    if not entity_id:
        print("BITRIX_SYNC_SKIP", "process_not_found", req.id, getattr(req.kind, "value", req.kind), flush=True)
        return {"skipped": True, "reason": "process_not_found"}
    schema = get_element_fields(settings.webhook_url, entity_id)
    if "_error" in schema:
        print("BITRIX_SYNC_ERROR", "fields", entity_id, req.id, flush=True)
        return {"error": schema["_error"], "action": "fields"}
    resolved_map = resolve_field_map(schema)
    _log_important_field_map(schema, resolved_map, entity_id, req.bitrix_element_id)
    values = _trip_values(req, db=db)
    fields = {code: values[logical] for logical, code in resolved_map.items() if logical in values}
    planned_code = resolved_map.get("planned_at")
    if planned_code and planned_code in schema:
        fields[planned_code] = _planned_at_outbound_value(req, settings.webhook_url)
    fields = _prepare_outbound_reference_fields(fields, values, resolved_map, schema, settings.webhook_url)
    # Системное поле «Клиент» нужно только для заявок, где действительно выбран заказчик.
    # Не делаем лишние административные REST-вызовы для рейсов без клиента.
    if req.customer and "companyId" not in schema and "contactIds" not in schema:
        ensure_client_field_enabled(settings.webhook_url, int(entity_id))
        _invalidate_field_schema_cache(settings.webhook_url, entity_id)
        refreshed_schema = get_element_fields(settings.webhook_url, entity_id)
        if "_error" not in refreshed_schema:
            schema = refreshed_schema
            resolved_map = resolve_field_map(schema)
            values = _trip_values(req, db=db)
            fields = {code: values[logical] for logical, code in resolved_map.items() if logical in values}
            planned_code = resolved_map.get("planned_at")
            if planned_code and planned_code in schema:
                fields[planned_code] = _planned_at_outbound_value(req, settings.webhook_url)
            fields = _prepare_outbound_reference_fields(fields, values, resolved_map, schema, settings.webhook_url)
    company_id, contact_id = _ensure_bitrix_customer(req.customer, settings.webhook_url) if req.customer else (None, None)
    # Поддерживаем как штатный «Клиент/Компания» (companyId), так и отдельное
    # пользовательское поле «Компания».
    customer_code = resolved_map.get("customer_name")
    if company_id and customer_code and customer_code in schema:
        customer_info = schema.get(customer_code, {})
        customer_type = _field_type(customer_info)
        if "company" in customer_type:
            fields[customer_code] = int(company_id)
        elif customer_type in {"crm", "crm_entity"}:
            entity_ids = _extract_dynamic_entity_ids(customer_info)
            if not entity_ids or 4 in entity_ids:
                fields[customer_code] = f"CO_{int(company_id)}"
    if company_id and "companyId" in schema:
        fields["companyId"] = company_id
    if contact_id and "contactIds" in schema:
        fields["contactIds"] = [contact_id]
    target_stage = STATUS_STAGE_TITLES.get(req.status)
    if target_stage:
        stage_id, category_id = resolve_stage(
            settings.webhook_url, entity_id, req.status, req.bitrix_element_id,
        )
        if not stage_id:
            print("BITRIX_SYNC_ERROR", "stage", entity_id, req.id, req.status.value, flush=True)
            return {"error": "bitrix_stage_not_found", "action": "stage"}
        fields["stageId"] = stage_id
        if not req.bitrix_element_id:
            fields["categoryId"] = category_id
    if req.bitrix_element_id:
        action = "update"
        payload = {"entityTypeId": int(entity_id), "id": int(req.bitrix_element_id), "fields": fields}
        response = _http_post(settings.webhook_url, "crm.item.update", payload)
    else:
        action = "add"
        payload = {"entityTypeId": int(entity_id), "fields": fields}
        response = _http_post(settings.webhook_url, "crm.item.add", payload)
    if "error" in response:
        print("BITRIX_SYNC_ERROR", action, entity_id, req.id, "remote", flush=True)
        return {"error": response["error"], "action": action}
    result = response.get("result", {})
    item_id = result.get("id") or result.get("item", {}).get("id")
    if action == "add" and not item_id:
        print("BITRIX_SYNC_ERROR", action, entity_id, req.id, "missing_item_id", flush=True)
        return {"error": "bitrix_item_id_missing", "action": action}
    if item_id:
        if not req.bitrix_element_id:
            req.bitrix_element_id = int(item_id)
        req.bitrix_entity_type_id = int(entity_id)
        db.add(req)
    print("BITRIX_SYNC_OK", action, entity_id, item_id, req.id, flush=True)
    return {"ok": True, "action": action, "element_id": item_id}


def delete_trip(req, db, settings=None) -> dict:
    settings = settings or get_integration_settings(db)
    if not settings or not settings.is_active or not settings.webhook_url or not req.bitrix_element_id:
        return {"skipped": True}
    # У синхронизированной заявки уже хранится точный смарт-процесс Bitrix24.
    # Используем его первым: повторное автоопределение по названию процесса может
    # вернуть не тот entityTypeId после переименования/дублирования процесса.
    entity_id = req.bitrix_entity_type_id or resolve_process_entity(settings.webhook_url, req.kind)
    if not entity_id:
        return {"skipped": True, "reason": "process_not_found"}
    response = _http_post(settings.webhook_url, "crm.item.delete", {"entityTypeId": int(entity_id), "id": int(req.bitrix_element_id)})
    if "error" not in response:
        return {"ok": True, "action": "delete"}
    error_text = str(response.get("error") or "")
    # Если элемент уже удалён в Bitrix, локальное удаление можно безопасно
    # завершить: системы после этого как раз становятся одинаковыми.
    normalized = error_text.lower()
    if any(marker in normalized for marker in ("not found", "not_found", "notfound", "does not exist", "не найден")):
        return {"ok": True, "action": "delete", "already_missing": True}
    return {"error": error_text}


def _scalar(value):
    if isinstance(value, list):
        return value[0] if value else ""
    if isinstance(value, dict):
        return value.get("value") or value.get("VALUE") or ""
    return value


def _read_logical(item: dict, logical: str, mapping: dict):
    candidates = [mapping.get(logical), FIELD_MAP.get(logical)]
    for code in candidates:
        if code and code in item:
            return _scalar(item.get(code))
    return ""


def _has_logical(item: dict, logical: str, mapping: dict) -> bool:
    return any(code and code in item for code in (mapping.get(logical), FIELD_MAP.get(logical)))


def _to_float(value):
    try:
        return float(str(_scalar(value)).replace(",", "."))
    except (TypeError, ValueError):
        return 0.0


def _optional_nonnegative_float(value, field):
    raw = _scalar(value)
    if raw in (None, ""):
        return None
    try:
        result = float(str(raw).replace(",", "."))
    except (TypeError, ValueError):
        raise ValueError(f"invalid {field}")
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"invalid {field}")
    return result


def _optional_nonnegative_int(value, field):
    parsed = _optional_nonnegative_float(value, field)
    if parsed is None:
        return None
    if not parsed.is_integer():
        raise ValueError(f"invalid {field}")
    return int(parsed)


def _optional_bool(value, field):
    raw = str(_scalar(value) or "").strip().lower()
    if not raw:
        return None
    if raw in {"да", "y", "yes", "1", "true"}:
        return True
    if raw in {"нет", "n", "no", "0", "false"}:
        return False
    raise ValueError(f"invalid {field}")


def _optional_inn(value):
    raw = re.sub(r"\D", "", str(_scalar(value) or ""))
    if not raw:
        return None
    if len(raw) not in {10, 12}:
        raise ValueError("invalid customer_inn")
    return raw


def _optional_datetime(value, field):
    raw = str(_scalar(value) or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed
    except ValueError:
        raise ValueError(f"invalid {field}")


def _status(value):
    text = str(_scalar(value) or "").strip()
    for member in RequestStatus:
        if member.value.lower() == text.lower():
            return member
    return None


def _find_by_name(db, model, field, value):
    value = str(_scalar(value) or "").strip()
    if not value:
        return None
    return db.query(model).filter(field.ilike(value)).first()


def list_recent_trip_ids(webhook_base: str, entity_type_id: int, limit: int = 10) -> list[int]:
    """Возвращает последние изменённые элементы смарт-процесса для страховочной сверки."""
    safe_limit = max(1, min(int(limit or 10), 50))
    response = _http_post(webhook_base, "crm.item.list", {
        "entityTypeId": int(entity_type_id),
        "order": {"updatedTime": "DESC"},
        "select": ["id"],
        "start": 0,
    })
    if "error" in response:
        return []
    result = []
    for item in _list_result_items(response):
        try:
            item_id = int(item.get("id"))
        except (TypeError, ValueError, AttributeError):
            continue
        if item_id > 0:
            result.append(item_id)
        if len(result) >= safe_limit:
            break
    return result


def pull_recent_trips(db, settings=None, limit_per_process: int = 10) -> dict:
    """Best-effort сверка последних рейсов Bitrix → приложение.

    Webhook остаётся основным каналом. Эта сверка страхует от пропущенного события
    Bitrix/прокси и не должна откатывать остальные элементы из-за одной плохой карточки.
    """
    settings = settings or get_integration_settings(db)
    if not settings or not settings.is_active or not settings.webhook_url:
        return {"skipped": True, "reason": "no_active_integration"}
    summary = {"ok": True, "checked": 0, "synced": 0, "errors": 0}
    for entity_id in (int(PUKHTOVOZ_ENTITY_TYPE_ID), int(SAMOSVAL_ENTITY_TYPE_ID)):
        for item_id in list_recent_trip_ids(settings.webhook_url, entity_id, limit=limit_per_process):
            summary["checked"] += 1
            savepoint = db.begin_nested()
            try:
                result = sync_from_bitrix(item_id, entity_id, db, settings=settings)
                if result.get("error"):
                    savepoint.rollback()
                    summary["errors"] += 1
                    print("BITRIX_RECONCILE_ITEM_ERROR", entity_id, item_id, flush=True)
                    continue
                savepoint.commit()
                if result.get("ok"):
                    summary["synced"] += 1
            except Exception as exc:
                savepoint.rollback()
                summary["errors"] += 1
                print("BITRIX_RECONCILE_EXCEPTION", entity_id, item_id, type(exc).__name__, flush=True)
    return summary


def sync_from_bitrix(item_id: int, entity_type_id: int, db, settings=None) -> dict:
    """Создаёт или обновляет локальную заявку после события смарт-процесса."""
    settings = settings or get_integration_settings(db)
    if not settings or not settings.is_active or not settings.webhook_url:
        return {"skipped": True, "reason": "no_active_integration"}
    kind = KNOWN_PROCESS_KINDS.get(str(entity_type_id))
    if not kind:
        kind = resolve_process_kinds(settings.webhook_url).get(str(entity_type_id))
    if not kind:
        return {"skipped": True, "reason": "foreign_process"}
    item = fetch_item(settings.webhook_url, entity_type_id, item_id)
    if "_error" in item:
        return {"error": item["_error"]}
    schema = get_element_fields(settings.webhook_url, str(entity_type_id))
    mapping = resolve_field_map_for_item(schema, item) if "_error" not in schema else FIELD_MAP
    if "_error" not in schema:
        _log_important_field_map(schema, mapping, entity_type_id, item_id)

    number = str(_read_logical(item, "number", mapping) or item.get("title") or f"Б24-{item_id}").strip()
    trip = db.query(models.TripRequest).filter(
        models.TripRequest.bitrix_element_id == int(item_id),
        models.TripRequest.bitrix_entity_type_id == int(entity_type_id),
    ).first()
    if not trip:
        number_match = db.query(models.TripRequest).filter(
            models.TripRequest.number == number,
            models.TripRequest.kind == kind,
        ).first()
        # По номеру связываем только ещё не привязанную локальную заявку
        # (обычный echo после создания из приложения). Если этот номер уже
        # принадлежит другому элементу Bitrix, это отдельный рейс.
        if number_match and (
            not number_match.bitrix_element_id
            or (
                int(number_match.bitrix_element_id) == int(item_id)
                and int(number_match.bitrix_entity_type_id or entity_type_id) == int(entity_type_id)
            )
        ):
            trip = number_match
    created = trip is None
    if created:
        # В Bitrix названия элементов могут повторяться, а локальный number
        # уникален. Не теряем новый рейс из-за UNIQUE constraint.
        if db.query(models.TripRequest).filter(models.TripRequest.number == number).first():
            suffix = f"Б24-{entity_type_id}-{item_id}"
            base = number[: max(1, 255 - len(suffix) - 3)]
            number = f"{base} [{suffix}]"
        trip = models.TripRequest(
            number=number,
            planned_date=date.today(),
            kind=kind,
            status=RequestStatus.NEW,
            bitrix_element_id=int(item_id),
            bitrix_entity_type_id=int(entity_type_id),
        )
        db.add(trip)

    # Берём именно фактическое поле «Подача машины/Дата и время», а не
    # системную «Дату создания», и сохраняем дату/время в часовом поясе портала.
    planned_code = _field_code(mapping, "planned_at")
    raw_planned_at = item.get(planned_code) if planned_code and planned_code in item else None
    planned_date, planned_time = _parse_bitrix_planned_at(settings.webhook_url, raw_planned_at)
    if planned_date is not None:
        trip.planned_date = planned_date
    if planned_time is not None:
        trip.planned_time = planned_time
    trip.number = number
    for logical, attr, limit in (
        ("load_address", "load_address", None),
        ("unload_address", "unload_address", None),
        ("route_name", "route_name", None),
    ):
        if _has_logical(item, logical, mapping):
            if logical in {"load_address", "unload_address"}:
                value = _clean_address_value(settings.webhook_url, item, logical, mapping, schema)
            else:
                value = str(_read_display_logical(item, logical, mapping, schema, settings.webhook_url) or "")
            setattr(trip, attr, value[:limit] if limit else value)
    def inbound_value(parser, logical, field):
        """Плохое одно поле Bitrix не должно отменять создание/обновление всего рейса."""
        raw = _read_logical(item, logical, mapping)
        try:
            return parser(raw, field) if field is not None else parser(raw)
        except ValueError:
            print("BITRIX_INBOUND_FIELD_SKIPPED", logical, entity_type_id, item_id, flush=True)
            return None

    km = inbound_value(_optional_nonnegative_float, "km", "km")
    volume = inbound_value(_optional_nonnegative_float, "volume", "volume")
    tonnage = inbound_value(_optional_nonnegative_float, "tonnage", "tonnage")
    actual_km = inbound_value(_optional_nonnegative_float, "actual_km", "actual_km")
    actual_volume = inbound_value(_optional_nonnegative_float, "actual_volume", "actual_volume")
    actual_tonnage = inbound_value(_optional_nonnegative_float, "actual_tonnage", "actual_tonnage")
    trips_count = inbound_value(_optional_nonnegative_int, "trips_count", "trips_count")
    waste_bin_count = inbound_value(_optional_nonnegative_int, "waste_bin_count", "waste_bin_count")
    odometer = inbound_value(_optional_nonnegative_float, "odometer", "odometer")
    fuel_liters = inbound_value(_optional_nonnegative_float, "fuel_liters", "fuel_liters")
    fuel_price = inbound_value(_optional_nonnegative_float, "fuel_price", "fuel_price")
    fuel_cost = inbound_value(_optional_nonnegative_float, "fuel_cost", "fuel_cost")
    started_at = inbound_value(_optional_datetime, "started_at", "started_at")
    finished_at = inbound_value(_optional_datetime, "finished_at", "finished_at")
    customer_bitrix_id = inbound_value(_optional_nonnegative_int, "customer_bitrix_id", "customer_bitrix_id")
    customer_inn = inbound_value(_optional_inn, "customer_inn", None)
    is_empty_run = inbound_value(_optional_bool, "is_empty_run", "is_empty_run")
    has_downtime = inbound_value(_optional_bool, "has_downtime", "has_downtime")
    downtime_minutes = inbound_value(_optional_nonnegative_int, "downtime_minutes", "downtime_minutes")

    # Пустая карточка Bitrix часто отдаёт числовые поля как 0. Для количества
    # рейсов это не повод откатывать весь webhook: оставляем старое значение,
    # а для нового рейса используем безопасный минимум 1.
    if trips_count is not None and trips_count < 1:
        print("BITRIX_INBOUND_FIELD_SKIPPED trips_count_zero", entity_type_id, item_id, flush=True)
        trips_count = None

    for logical, value in {
        "km": km, "volume": volume, "tonnage": tonnage, "actual_km": actual_km,
        "actual_volume": actual_volume, "actual_tonnage": actual_tonnage,
        "trips_count": trips_count,
        "waste_bin_count": waste_bin_count,
    }.items():
        if _has_logical(item, logical, mapping) and not (logical == "trips_count" and value is None):
            setattr(trip, logical, value)
    if created:
        trip.km = 0 if km is None else km
        trip.volume = 0 if volume is None else volume
        trip.trips_count = 1 if trips_count is None else trips_count
    if _has_logical(item, "started_at", mapping):
        trip.started_at = started_at
    if _has_logical(item, "finished_at", mapping):
        trip.finished_at = finished_at
    if _has_logical(item, "is_empty_run", mapping):
        trip.is_empty_run = bool(is_empty_run)
    if _has_logical(item, "empty_run_comment", mapping):
        trip.empty_run_comment = str(_read_logical(item, "empty_run_comment", mapping) or "")
    if _has_logical(item, "has_downtime", mapping):
        trip.has_downtime = bool(has_downtime)
    if _has_logical(item, "downtime_minutes", mapping):
        trip.downtime_minutes = downtime_minutes
    if _has_logical(item, "downtime_comment", mapping):
        trip.downtime_comment = str(_read_logical(item, "downtime_comment", mapping) or "")
    stage_status = status_from_stage(settings.webhook_url, entity_type_id, item)
    if stage_status:
        trip.status = stage_status
    elif _has_logical(item, "status", mapping):
        inbound_status = _status(_read_logical(item, "status", mapping))
        if inbound_status:
            trip.status = inbound_status
    if _has_logical(item, "comment", mapping):
        trip.comment = str(_read_logical(item, "comment", mapping) or "")
    if _has_logical(item, "logist_comment", mapping):
        trip.logist_comment = str(_read_logical(item, "logist_comment", mapping) or "")
    for logical, attr in (
        ("site_contact_name", "site_contact_name"),
        ("site_contact_phone", "site_contact_phone"),
        ("site_contact_comment", "site_contact_comment"),
    ):
        if _has_logical(item, logical, mapping):
            setattr(trip, attr, str(_read_logical(item, logical, mapping) or ""))
    trip.bitrix_element_id = int(item_id)
    trip.bitrix_entity_type_id = int(entity_type_id)

    driver, driver_text = _resolve_local_driver(db, settings.webhook_url, item, mapping, schema)
    if driver:
        trip.driver_id = driver.id
        print("BITRIX_INBOUND_DRIVER_MATCHED", entity_type_id, item_id, driver.id, _normalize(driver.full_name), flush=True)
    elif driver_text:
        print("BITRIX_INBOUND_DRIVER_NOT_MATCHED", entity_type_id, item_id, _normalize(driver_text), flush=True)

    vehicle = None
    vehicle_text = ""
    for _, raw_vehicle, vehicle_info in _logical_raw_candidates(item, "vehicle_name", mapping, schema):
        candidate_text = str(_display_field_value(settings.webhook_url, raw_vehicle, vehicle_info) or "").strip()
        if not candidate_text:
            continue
        if not vehicle_text:
            vehicle_text = candidate_text
        vehicle = _find_vehicle_by_bitrix_value(db, candidate_text)
        if vehicle:
            break
    if vehicle:
        trip.vehicle_id = vehicle.id
        print("BITRIX_INBOUND_VEHICLE_MATCHED", entity_type_id, item_id, vehicle.id, _normalize_plate(vehicle.plate), flush=True)
    elif vehicle_text:
        print("BITRIX_INBOUND_VEHICLE_NOT_MATCHED", entity_type_id, item_id, _normalize_plate(vehicle_text), flush=True)
    polygon_name = str(_read_display_logical(item, "polygon_name", mapping, schema, settings.webhook_url) or "").strip()
    if polygon_name:
        polygon = _find_by_name(db, models.Polygon, models.Polygon.name, polygon_name)
        if not polygon:
            normalized_polygon = _normalize(polygon_name)
            matches = [row for row in db.query(models.Polygon).all() if _normalize(row.name) == normalized_polygon]
            polygon = matches[0] if len(matches) == 1 else None
        if not polygon and re.search(r"[A-Za-zА-Яа-яЁё]", polygon_name):
            polygon = models.Polygon(name=polygon_name); db.add(polygon); db.flush()
        if polygon:
            # Название выбранного полигона связывает рейс с записью Polygon.
            # Отдельные реквизиты полигона обновляем только из действительно
            # отдельных полей Bitrix и никогда не копируем само название в
            # адрес, контакт, телефон или навигацию.
            polygon_name_norm = _normalize(polygon_name)
            for logical, attr in (("polygon_address", "address"), ("polygon_contact", "contact"), ("polygon_phone", "phone"), ("polygon_navigator_url", "navigator_url")):
                if _has_logical(item, logical, mapping):
                    value = _clean_address_value(settings.webhook_url, item, logical, mapping, schema) if logical == "polygon_address" else str(_read_display_logical(item, logical, mapping, schema, settings.webhook_url) or "").strip()
                    if value and _normalize(value) != polygon_name_norm:
                        setattr(polygon, attr, value)
            trip.polygon_id = polygon.id
        else:
            print("BITRIX_INBOUND_POLYGON_NOT_MATCHED", entity_type_id, item_id, _normalize(polygon_name), flush=True)
    customer_name = str(_read_display_logical(item, "customer_name", mapping, schema, settings.webhook_url) or "").strip()
    custom_company_ref_id = None
    customer_field_code = _field_code(mapping, "customer_name")
    if customer_field_code and customer_field_code in item:
        customer_info = schema.get(customer_field_code, {}) if isinstance(schema, dict) else {}
        custom_company_ref_id = _company_id_from_field(item.get(customer_field_code), customer_info)
        if custom_company_ref_id:
            customer_bitrix_id = custom_company_ref_id
    client_field_present = any(key in item for key in ("companyId", "contactId", "contactIds"))
    built_in_company_id = _optional_nonnegative_int(item.get("companyId"), "companyId") if item.get("companyId") not in (None, "", 0, "0") else None
    raw_contact_ids = item.get("contactIds") or ([] if item.get("contactId") in (None, "", 0, "0") else [item.get("contactId")])
    if not isinstance(raw_contact_ids, list):
        raw_contact_ids = [raw_contact_ids]
    built_in_contact_id = None
    for raw_contact_id in raw_contact_ids:
        try:
            candidate = int(raw_contact_id)
        except (TypeError, ValueError):
            continue
        if candidate > 0:
            built_in_contact_id = candidate
            break
    company_lookup_id = built_in_company_id or custom_company_ref_id
    company_item = fetch_item(settings.webhook_url, 4, company_lookup_id) if company_lookup_id else {}
    contact_item = fetch_item(settings.webhook_url, 3, built_in_contact_id) if built_in_contact_id else {}
    if company_lookup_id and "_error" not in company_item:
        customer_name = str(company_item.get("title") or customer_name or "").strip()
        customer_bitrix_id = int(company_lookup_id)
        if not customer_inn:
            customer_inn = _company_requisite_inn(settings.webhook_url, int(company_lookup_id))
    if client_field_present and not built_in_company_id and not built_in_contact_id and not customer_name and not customer_inn:
        trip.customer_id = None
    elif customer_name or customer_bitrix_id is not None or customer_inn or built_in_contact_id:
        customer = None
        if customer_bitrix_id is not None:
            customer = db.query(models.Customer).filter(models.Customer.bitrix_company_id == customer_bitrix_id).first()
        if not customer and customer_inn:
            customer = db.query(models.Customer).filter(models.Customer.inn == customer_inn).first()
        if not customer and customer_name:
            normalized_name = _normalize(customer_name)
            customer = next((row for row in db.query(models.Customer).all() if _normalize(row.name) == normalized_name), None)
        if not customer:
            # Если Bitrix прислал только ссылку на компанию, а текущий REST-вебхук
            # не имеет доступа прочитать её карточку, рейс всё равно должен появиться.
            # Клиента дотянем следующим событием/ручной сверкой, когда данные доступны.
            if not customer_name:
                customer = None
            else:
                customer = models.Customer(name=customer_name); db.add(customer); db.flush()

        if customer:
            # При конфликте идентичностей приоритет у явного Bitrix companyId, затем ИНН.
            # Конфликт названий больше не отменяет синхронизацию всего рейса.
            id_owner = db.query(models.Customer).filter(
                models.Customer.bitrix_company_id == customer_bitrix_id,
                models.Customer.id != customer.id,
            ).first() if customer_bitrix_id is not None else None
            inn_owner = db.query(models.Customer).filter(
                models.Customer.inn == customer_inn,
                models.Customer.id != customer.id,
            ).first() if customer_inn else None
            if id_owner:
                customer = id_owner
            elif inn_owner:
                customer = inn_owner
        if customer:
            # Не переименовываем запись в имя, уже занятое другим локальным заказчиком:
            # это сохраняет уникальность справочника, но не блокирует сам рейс.
            name_conflict = False
            if customer_name:
                normalized_name = _normalize(customer_name)
                name_conflict = any(
                    _normalize(row.name) == normalized_name
                    for row in db.query(models.Customer).filter(models.Customer.id != customer.id).all()
                )
            if customer_name and not name_conflict:
                customer.name = customer_name
            if customer_bitrix_id is not None:
                customer.bitrix_company_id = customer_bitrix_id
            if customer_inn and not (inn_owner and inn_owner.id != customer.id):
                customer.inn = customer_inn
            elif customer_inn and inn_owner and inn_owner.id != customer.id:
                print("BITRIX_INBOUND_CUSTOMER_INN_PRESERVED", entity_type_id, item_id, flush=True)
            if built_in_contact_id:
                customer.bitrix_contact_id = built_in_contact_id
            if company_lookup_id and "_error" not in company_item:
                customer.address = str(company_item.get("address") or "").strip()
                customer.phone = _first_phone(company_item)
            if built_in_contact_id and "_error" not in contact_item:
                customer.contact = _contact_display_name(contact_item)
                customer.phone = _first_phone(contact_item)
            trip.customer_id = customer.id
            if _has_logical(item, "customer_contact_name", mapping):
                customer.contact = str(_read_logical(item, "customer_contact_name", mapping) or "")
            if _has_logical(item, "customer_contact_phone", mapping):
                customer.phone = str(_read_logical(item, "customer_contact_phone", mapping) or "")
            if _has_logical(item, "customer_address", mapping):
                customer.address = str(_read_logical(item, "customer_address", mapping) or "")
    cargo_name = str(_read_display_logical(item, "cargo_type_name", mapping, schema, settings.webhook_url) or "").strip()
    if cargo_name:
        cargo = _find_by_name(db, models.CargoType, models.CargoType.name, cargo_name)
        if not cargo:
            normalized_cargo = _normalize(cargo_name)
            matches = [row for row in db.query(models.CargoType).all() if _normalize(row.name) == normalized_cargo]
            cargo = matches[0] if len(matches) == 1 else None
        if cargo:
            trip.cargo_type_id = cargo.id
    tariff_name = str(_read_display_logical(item, "tariff_name", mapping, schema, settings.webhook_url) or "").strip()
    if tariff_name:
        tariff = _find_tariff_by_bitrix_value(db, kind, tariff_name)
        if tariff:
            trip.tariff_id = tariff.id
        else:
            print("BITRIX_INBOUND_TARIFF_NOT_MATCHED", entity_type_id, item_id, _normalize(tariff_name), flush=True)

    # Показания спидометра/топливо живут в отчёте дня водителя, но могут
    # редактироваться из Bitrix. Обновляем отчёт только если известны водитель,
    # машина и дата; остальные данные рейса при этом не блокируем.
    report_fields_present = any(_has_logical(item, logical, mapping) for logical in (
        "odometer", "fuel_liters", "fuel_price", "fuel_cost",
    ))
    if report_fields_present and trip.driver_id and trip.vehicle_id and trip.planned_date:
        day_report = db.query(models.DriverDayReport).filter(
            models.DriverDayReport.driver_id == trip.driver_id,
            models.DriverDayReport.report_date == trip.planned_date,
        ).first()
        if not day_report:
            day_report = models.DriverDayReport(
                driver_id=trip.driver_id, report_date=trip.planned_date, vehicle_id=trip.vehicle_id,
                total_km=0, odometer=0, fuel_liters=0, fuel_price=0,
            )
            db.add(day_report)
        day_report.vehicle_id = trip.vehicle_id
        if _has_logical(item, "odometer", mapping) and odometer is not None:
            day_report.odometer = odometer
        if _has_logical(item, "fuel_liters", mapping) and fuel_liters is not None:
            day_report.fuel_liters = fuel_liters
        if _has_logical(item, "fuel_price", mapping) and fuel_price is not None:
            day_report.fuel_price = fuel_price
        elif _has_logical(item, "fuel_cost", mapping) and fuel_cost is not None and float(day_report.fuel_liters or 0) > 0:
            day_report.fuel_price = float(fuel_cost) / float(day_report.fuel_liters)

    db.flush()
    if "_error" not in schema:
        sync_inbound_attachments(item, trip, db, schema, mapping)
    print("BITRIX_INBOUND_OK", "add" if created else "update", entity_type_id, item_id, trip.id, flush=True)
    return {"ok": True, "action": "add" if created else "update", "trip_id": trip.id}


def extract_event_identifiers(payload: dict):
    raw_event = str(payload.get("event") or payload.get("EVENT") or "").upper()
    event = raw_event
    event_entity_id = None
    match = re.match(r"^(ONCRMDYNAMICITEM(?:ADD|UPDATE|DELETE))(?:_(\d+))?$", raw_event)
    if match:
        event = match.group(1)
        event_entity_id = match.group(2)

    flattened = {str(k).upper(): _scalar(v) for k, v in payload.items()}
    data = payload.get("data") or payload.get("DATA") or {}
    fields = {}
    if isinstance(data, dict):
        fields = data.get("FIELDS") or data.get("fields") or {}
    if not isinstance(fields, dict):
        fields = {}

    def first(mapping, *keys):
        if not isinstance(mapping, dict):
            return None
        lowered = {str(key).lower(): value for key, value in mapping.items()}
        for key in keys:
            value = lowered.get(str(key).lower())
            if value not in (None, ""):
                return _scalar(value)
        return None

    item_id = first(fields, "ID", "id") or first(data, "ID", "id")
    entity_id = first(fields, "ENTITY_TYPE_ID", "ENTITYTYPEID", "entityTypeId", "entity_type_id")
    entity_id = entity_id or first(data, "ENTITY_TYPE_ID", "ENTITYTYPEID", "entityTypeId", "entity_type_id")
    item_id = item_id or flattened.get("DATA[FIELDS][ID]") or flattened.get("DATA[ID]")
    entity_id = (
        entity_id
        or flattened.get("DATA[FIELDS][ENTITY_TYPE_ID]")
        or flattened.get("DATA[FIELDS][ENTITYTYPEID]")
        or flattened.get("DATA[FIELDS][ENTITYTYPEID]")
        or flattened.get("DATA[ENTITY_TYPE_ID]")
        or flattened.get("DATA[ENTITYTYPEID]")
        or event_entity_id
    )
    try:
        item_id = int(item_id) if item_id not in (None, "") else None
    except (TypeError, ValueError):
        item_id = None
    try:
        entity_id = int(entity_id) if entity_id not in (None, "") else None
    except (TypeError, ValueError):
        entity_id = None
    return event, item_id, entity_id
