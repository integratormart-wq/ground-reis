"""Двусторонняя синхронизация рейсов со смарт-процессами Bitrix24."""
import json
import math
import os
import re
import urllib.error
import urllib.parse
import urllib.request
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
    "planned_date": "ufReisDate",
    "planned_time": "ufReisTime",
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
    "polygon_name": "ufPolygon",
    "sum_trip": "ufSumTrip",
    "sum_driver": "ufSumDriver",
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
    "planned_date": ("дата рейса", "плановая дата", "дата"),
    "planned_time": ("время рейса", "плановое время", "время"),
    "driver_name": ("водитель", "фио водителя"),
    "vehicle_name": ("автомобиль", "машина", "транспорт"),
    "load_address": ("адрес загрузки", "адрес подачи", "загрузка"),
    "unload_address": ("адрес выгрузки", "выгрузка"),
    "route_name": ("маршрут",),
    "trips_count": ("количество рейсов", "число рейсов", "рейсов"),
    "cargo_type_name": ("тип груза", "груз"),
    "tariff_name": ("тариф",),
    "km": ("километраж план", "плановый километраж", "километраж", "км план"),
    "volume": ("объем план", "плановый объем", "объем", "кубатура"),
    "tonnage": ("тоннаж план", "плановый тоннаж", "тонны план"),
    "actual_km": ("фактический километраж", "факт км", "км факт"),
    "actual_volume": ("фактический объем", "факт объем", "объем факт"),
    "actual_tonnage": ("фактический тоннаж", "тоннаж факт", "тонны факт"),
    "status": ("статус заявки", "статус рейса", "статус"),
    "customer_name": ("заказчик", "клиент", "компания"),
    "customer_bitrix_id": ("id компании битрикс", "bitrix id клиента", "id клиента битрикс"),
    "customer_inn": ("инн заказчика", "инн клиента", "инн"),
    "polygon_name": ("полигон",),
    "sum_trip": ("сумма рейса", "стоимость рейса"),
    "sum_driver": ("сумма водителю", "зарплата водителя", "начисление водителю"),
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
    # Реальные ID портала ООО «ГРАУНД». Автообнаружение ниже дополняет
    # таблицу, но маршрутизация не зависит от возможного переименования процесса.
    result = dict(KNOWN_PROCESS_KINDS)
    for entity_id, title in find_smart_process_ids(webhook_base).items():
        if entity_id == "_error":
            continue
        normalized = title.lower()
        if PROCESS_NAME_PUKHTOVOZ in normalized:
            result[str(entity_id)] = TripType.PUKHTOVOZ
        elif PROCESS_NAME_SAMOSVAL in normalized:
            result[str(entity_id)] = TripType.SAMOSVAL
    return result


def resolve_process_entity(webhook_base: str, kind) -> Optional[str]:
    kind_value = kind.value if hasattr(kind, "value") else str(kind)
    for entity_id, process_kind in resolve_process_kinds(webhook_base).items():
        if process_kind.value == kind_value:
            return entity_id
    return None


def get_element_fields(webhook_base: str, entity_id: str) -> dict:
    response = _http_post(webhook_base, "crm.item.fields", {"entityTypeId": int(entity_id)})
    if "error" in response:
        return {"_error": response["error"]}
    return response.get("result", {}).get("fields", {})


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


def resolve_field_map(schema: dict) -> dict:
    """Находит реальные коды пользовательских полей по их русским названиям."""
    resolved = {"number": "title"}
    normalized_schema = {}
    for code, info in schema.items():
        title = info.get("title") or info.get("formLabel") or info.get("listLabel") or code
        normalized_schema[code] = _normalize(title)
    for logical, configured_code in FIELD_MAP.items():
        if logical == "number":
            continue
        if configured_code in schema:
            resolved[logical] = configured_code
            continue
        aliases = tuple(_normalize(x) for x in FIELD_TITLES.get(logical, ()))
        for code, title in normalized_schema.items():
            if title in aliases or any(alias and alias in title for alias in aliases):
                resolved[logical] = code
                break
    return resolved


def _as_bitrix_value(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return "" if value is None else value


def _trip_values(req) -> dict:
    return {
        "number": req.number or "",
        "planned_date": _as_bitrix_value(req.planned_date),
        "planned_time": req.planned_time or "",
        "driver_name": req.driver.full_name if req.driver else "",
        "vehicle_name": (f"{req.vehicle.name} {req.vehicle.plate}" if req.vehicle else ""),
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
        "polygon_name": req.polygon.name if req.polygon else "",
        "sum_trip": req.sum_trip if req.sum_trip is not None else "",
        "sum_driver": req.sum_driver or 0,
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


def build_fields(req, field_map=None) -> dict:
    mapping = field_map or FIELD_MAP
    values = _trip_values(req)
    return {code: values[logical] for logical, code in mapping.items() if logical in values}


def sync_trip(req, db, settings=None) -> dict:
    settings = settings or get_integration_settings(db)
    if not settings or not settings.is_active or not settings.webhook_url:
        return {"skipped": True, "reason": "no_active_integration"}
    entity_id = resolve_process_entity(settings.webhook_url, req.kind)
    if not entity_id:
        return {"skipped": True, "reason": "process_not_found"}
    schema = get_element_fields(settings.webhook_url, entity_id)
    if "_error" in schema:
        return {"error": schema["_error"], "action": "fields"}
    fields = build_fields(req, resolve_field_map(schema))
    target_stage = STATUS_STAGE_TITLES.get(req.status)
    if target_stage:
        stage_id, category_id = resolve_stage(
            settings.webhook_url, entity_id, req.status, req.bitrix_element_id,
        )
        if not stage_id:
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
        print("BITRIX_SYNC_ERROR", action, flush=True)
        return {"error": response["error"], "action": action}
    result = response.get("result", {})
    item_id = result.get("id") or result.get("item", {}).get("id")
    if action == "add" and not item_id:
        print("BITRIX_SYNC_ERROR", action, "missing_item_id", flush=True)
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
    entity_id = resolve_process_entity(settings.webhook_url, req.kind)
    if not entity_id:
        return {"skipped": True, "reason": "process_not_found"}
    response = _http_post(settings.webhook_url, "crm.item.delete", {"entityTypeId": int(entity_id), "id": int(req.bitrix_element_id)})
    return {"ok": True} if "error" not in response else {"error": response["error"]}


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


def sync_from_bitrix(item_id: int, entity_type_id: int, db, settings=None) -> dict:
    """Создаёт или обновляет локальную заявку после события смарт-процесса."""
    settings = settings or get_integration_settings(db)
    if not settings or not settings.is_active or not settings.webhook_url:
        return {"skipped": True, "reason": "no_active_integration"}
    kinds = resolve_process_kinds(settings.webhook_url)
    kind = kinds.get(str(entity_type_id))
    if not kind:
        return {"skipped": True, "reason": "foreign_process"}
    item = fetch_item(settings.webhook_url, entity_type_id, item_id)
    if "_error" in item:
        return {"error": item["_error"]}
    schema = get_element_fields(settings.webhook_url, str(entity_type_id))
    mapping = resolve_field_map(schema) if "_error" not in schema else FIELD_MAP

    number = str(_read_logical(item, "number", mapping) or item.get("title") or f"Б24-{item_id}").strip()
    trip = db.query(models.TripRequest).filter(
        models.TripRequest.bitrix_element_id == int(item_id),
        models.TripRequest.bitrix_entity_type_id == int(entity_type_id),
    ).first()
    if not trip:
        trip = db.query(models.TripRequest).filter(models.TripRequest.number == number, models.TripRequest.kind == kind).first()
    created = trip is None
    if created:
        trip = models.TripRequest(
            number=number,
            planned_date=date.today(),
            kind=kind,
            status=RequestStatus.NEW,
            bitrix_element_id=int(item_id),
            bitrix_entity_type_id=int(entity_type_id),
        )
        db.add(trip)

    raw_date = _read_logical(item, "planned_date", mapping)
    if raw_date:
        try:
            trip.planned_date = date.fromisoformat(str(raw_date)[:10])
        except ValueError:
            pass
    trip.number = number
    for logical, attr, limit in (
        ("planned_time", "planned_time", 50),
        ("load_address", "load_address", None),
        ("unload_address", "unload_address", None),
        ("route_name", "route_name", None),
    ):
        if _has_logical(item, logical, mapping):
            value = str(_read_logical(item, logical, mapping) or "")
            setattr(trip, attr, value[:limit] if limit else value)
    try:
        km = _optional_nonnegative_float(_read_logical(item, "km", mapping), "km")
        volume = _optional_nonnegative_float(_read_logical(item, "volume", mapping), "volume")
        tonnage = _optional_nonnegative_float(_read_logical(item, "tonnage", mapping), "tonnage")
        actual_km = _optional_nonnegative_float(_read_logical(item, "actual_km", mapping), "actual_km")
        actual_volume = _optional_nonnegative_float(_read_logical(item, "actual_volume", mapping), "actual_volume")
        actual_tonnage = _optional_nonnegative_float(_read_logical(item, "actual_tonnage", mapping), "actual_tonnage")
        trips_count = _optional_nonnegative_int(_read_logical(item, "trips_count", mapping), "trips_count")
        waste_bin_count = _optional_nonnegative_int(_read_logical(item, "waste_bin_count", mapping), "waste_bin_count")
        started_at = _optional_datetime(_read_logical(item, "started_at", mapping), "started_at")
        finished_at = _optional_datetime(_read_logical(item, "finished_at", mapping), "finished_at")
        customer_bitrix_id = _optional_nonnegative_int(_read_logical(item, "customer_bitrix_id", mapping), "customer_bitrix_id")
        customer_inn = _optional_inn(_read_logical(item, "customer_inn", mapping))
        is_empty_run = _optional_bool(_read_logical(item, "is_empty_run", mapping), "is_empty_run")
        has_downtime = _optional_bool(_read_logical(item, "has_downtime", mapping), "has_downtime")
        downtime_minutes = _optional_nonnegative_int(_read_logical(item, "downtime_minutes", mapping), "downtime_minutes")
    except ValueError as exc:
        return {"error": str(exc)}
    if trips_count is not None and trips_count < 1:
        return {"error": "invalid trips_count"}

    for logical, value in {
        "km": km, "volume": volume, "tonnage": tonnage, "actual_km": actual_km,
        "actual_volume": actual_volume, "actual_tonnage": actual_tonnage,
        "trips_count": trips_count,
        "waste_bin_count": waste_bin_count,
    }.items():
        if _has_logical(item, logical, mapping):
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

    driver = _find_by_name(db, models.User, models.User.full_name, _read_logical(item, "driver_name", mapping))
    if driver and driver.role == models.UserRole.DRIVER:
        trip.driver_id = driver.id
    vehicle_text = str(_read_logical(item, "vehicle_name", mapping) or "").strip()
    if vehicle_text:
        vehicle = db.query(models.Vehicle).filter(models.Vehicle.name.ilike(vehicle_text)).first()
        if not vehicle:
            vehicle = db.query(models.Vehicle).filter(models.Vehicle.plate.ilike(f"%{vehicle_text}%")).first()
        if not vehicle:
            vehicle = next((v for v in db.query(models.Vehicle).all() if v.name.lower() in vehicle_text.lower() or v.plate.lower() in vehicle_text.lower()), None)
        if vehicle:
            trip.vehicle_id = vehicle.id
    polygon_name = str(_read_logical(item, "polygon_name", mapping) or "").strip()
    if polygon_name:
        polygon = _find_by_name(db, models.Polygon, models.Polygon.name, polygon_name)
        if not polygon:
            polygon = models.Polygon(name=polygon_name); db.add(polygon); db.flush()
        trip.polygon_id = polygon.id
    customer_name = str(_read_logical(item, "customer_name", mapping) or "").strip()
    if customer_name or customer_bitrix_id is not None or customer_inn:
        customer = None
        if customer_bitrix_id is not None:
            customer = db.query(models.Customer).filter(models.Customer.bitrix_company_id == customer_bitrix_id).first()
        if not customer and customer_inn:
            customer = db.query(models.Customer).filter(models.Customer.inn == customer_inn).first()
        if not customer and customer_name:
            normalized_name = _normalize(customer_name)
            customer = next((row for row in db.query(models.Customer).all() if _normalize(row.name) == normalized_name), None)
        if not customer:
            if not customer_name:
                return {"error": "customer_name_required"}
            customer = models.Customer(name=customer_name); db.add(customer); db.flush()
        id_owner = db.query(models.Customer).filter(
            models.Customer.bitrix_company_id == customer_bitrix_id,
            models.Customer.id != customer.id,
        ).first() if customer_bitrix_id is not None else None
        inn_owner = db.query(models.Customer).filter(
            models.Customer.inn == customer_inn,
            models.Customer.id != customer.id,
        ).first() if customer_inn else None
        normalized_name_owner = None
        if customer_name:
            normalized_name = _normalize(customer_name)
            normalized_name_owner = next(
                (
                    row for row in db.query(models.Customer).filter(models.Customer.id != customer.id).all()
                    if _normalize(row.name) == normalized_name
                ),
                None,
            )
        if id_owner or inn_owner or normalized_name_owner:
            return {"error": "customer_identity_conflict"}
        if customer_name:
            customer.name = customer_name
        if customer_bitrix_id is not None:
            customer.bitrix_company_id = customer_bitrix_id
        if customer_inn:
            customer.inn = customer_inn
        trip.customer_id = customer.id
    cargo_name = str(_read_logical(item, "cargo_type_name", mapping) or "").strip()
    if cargo_name:
        cargo = _find_by_name(db, models.CargoType, models.CargoType.name, cargo_name)
        if cargo:
            trip.cargo_type_id = cargo.id
    tariff_name = str(_read_logical(item, "tariff_name", mapping) or "").strip()
    if tariff_name:
        tariff = _find_by_name(db, models.Tariff, models.Tariff.title, tariff_name)
        if tariff and tariff.kind == kind:
            trip.tariff_id = tariff.id
    db.flush()
    print("BITRIX_INBOUND_OK", "add" if created else "update", entity_type_id, item_id, trip.id, flush=True)
    return {"ok": True, "action": "add" if created else "update", "trip_id": trip.id}


def extract_event_identifiers(payload: dict):
    event = str(payload.get("event") or payload.get("EVENT") or "").upper()
    flattened = {str(k).upper(): _scalar(v) for k, v in payload.items()}
    data = payload.get("data") or payload.get("DATA") or {}
    fields = data.get("FIELDS", {}) if isinstance(data, dict) else {}
    item_id = fields.get("ID") or data.get("ID") if isinstance(data, dict) else None
    entity_id = fields.get("ENTITY_TYPE_ID") or fields.get("ENTITYTYPEID") if isinstance(fields, dict) else None
    item_id = item_id or flattened.get("DATA[FIELDS][ID]") or flattened.get("DATA[ID]")
    entity_id = entity_id or flattened.get("DATA[FIELDS][ENTITY_TYPE_ID]") or flattened.get("DATA[FIELDS][ENTITYTYPEID]") or flattened.get("DATA[ENTITY_TYPE_ID]")
    return event, int(item_id) if item_id else None, int(entity_id) if entity_id else None
