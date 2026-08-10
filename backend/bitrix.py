"""Двусторонняя синхронизация рейсов со смарт-процессами Bitrix24."""
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime
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
    "km": "ufKmPlan",
    "volume": "ufVolumePlan",
    "actual_km": "ufKmFact",
    "actual_volume": "ufVolumeFact",
    "status": "ufStatus",
    "customer_name": "ufCustomer",
    "polygon_name": "ufPolygon",
    "sum_driver": "ufSumDriver",
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
    "km": ("километраж план", "плановый километраж", "километраж", "км план"),
    "volume": ("объем план", "плановый объем", "объем", "кубатура"),
    "actual_km": ("фактический километраж", "факт км", "км факт"),
    "actual_volume": ("фактический объем", "факт объем", "объем факт"),
    "status": ("статус заявки", "статус рейса", "статус"),
    "customer_name": ("заказчик", "клиент", "компания"),
    "polygon_name": ("полигон",),
    "sum_driver": ("сумма водителю", "зарплата водителя", "начисление водителю"),
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


def _http_post(webhook_base: str, method: str, params: dict) -> dict:
    url = webhook_base.rstrip("/") + "/" + method
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
        "km": req.km or 0,
        "volume": req.volume or 0,
        "actual_km": req.actual_km if req.actual_km is not None else "",
        "actual_volume": req.actual_volume if req.actual_volume is not None else "",
        "status": req.status.value if hasattr(req.status, "value") else str(req.status),
        "customer_name": req.customer.name if req.customer else "",
        "polygon_name": req.polygon.name if req.polygon else "",
        "sum_driver": req.sum_driver or 0,
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
    if req.bitrix_element_id:
        action = "update"
        payload = {"entityTypeId": int(entity_id), "id": int(req.bitrix_element_id), "fields": fields}
        response = _http_post(settings.webhook_url, "crm.item.update", payload)
    else:
        action = "add"
        payload = {"entityTypeId": int(entity_id), "fields": fields}
        response = _http_post(settings.webhook_url, "crm.item.add", payload)
    if "error" in response:
        print("BITRIX_SYNC_ERROR", action, response["error"], flush=True)
        return {"error": response["error"], "action": action}
    result = response.get("result", {})
    item_id = result.get("id") or result.get("item", {}).get("id")
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


def _to_float(value):
    try:
        return float(str(_scalar(value)).replace(",", "."))
    except (TypeError, ValueError):
        return 0.0


def _status(value):
    text = str(_scalar(value) or "").strip()
    for member in RequestStatus:
        if member.value.lower() == text.lower():
            return member
    return RequestStatus.NEW


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
    trip.planned_time = str(_read_logical(item, "planned_time", mapping) or "")[:50]
    trip.load_address = str(_read_logical(item, "load_address", mapping) or "")
    trip.unload_address = str(_read_logical(item, "unload_address", mapping) or "")
    trip.route_name = str(_read_logical(item, "route_name", mapping) or "")
    trip.km = _to_float(_read_logical(item, "km", mapping))
    trip.volume = _to_float(_read_logical(item, "volume", mapping))
    trip.actual_km = _to_float(_read_logical(item, "actual_km", mapping)) or None
    trip.actual_volume = _to_float(_read_logical(item, "actual_volume", mapping)) or None
    trip.sum_driver = _to_float(_read_logical(item, "sum_driver", mapping)) or None
    trip.status = _status(_read_logical(item, "status", mapping))
    trip.comment = str(_read_logical(item, "comment", mapping) or "")
    trip.logist_comment = str(_read_logical(item, "logist_comment", mapping) or "")
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
    if customer_name:
        customer = _find_by_name(db, models.Customer, models.Customer.name, customer_name)
        if not customer:
            customer = models.Customer(name=customer_name); db.add(customer); db.flush()
        trip.customer_id = customer.id
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
