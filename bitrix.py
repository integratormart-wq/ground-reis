"""
Интеграция приложения ГРАУНД | Рейсы с Bitrix24.

Двусторонняя (насколько позволяет Bitrix24 REST):
- исходящая: при создании/изменении рейса в приложении данные автоматически
  создают/обновляют элемент смарт-процесса в Bitrix24;
- исключение дублей: каждый рейс хранит bitrix_element_id, повторный вызов
  делает update, а не add;
- обработка ошибок и логирование: все обращения к Bitrix24 обёрнуты в try/except,
  результат и ошибки пишутся в print (видны в логах Render) и возвращаются вызывающему.

Bitrix24 REST работает по webhook:
    https://<ваш_портал>.bitrix24.ru/rest/<user_id>/<webhook_token>/
Метод вызывается POST-параметром `method` и `fields`/`params`.
"""
import os
import json
import urllib.request
import urllib.error
import urllib.parse
from datetime import date, datetime
from typing import Optional

try:
    from backend import models
    from backend.models import TripType
except Exception:  # для автономного импорта в тестах
    models = None
    TripType = None

BITRIX_TIMEOUT = 20

# Названия смарт-процессов в Bitrix24 (ищем по подстроке, регистр не важен)
PROCESS_NAME_PUKHTOVOZ = "пухтовоз"
PROCESS_NAME_SAMOSVAL = "самосвал"

# Маппинг полей приложения -> поля Bitrix24 (имена полей настраиваются в Bitrix,
# здесь заданы разумные дефолтные имена; если в Bitrix другие — поправь словарь).
# Ключ — поле приложения, значение — имя поля в Bitrix24 (типа ufCrm... или стандартный).
FIELD_MAP = {
    "number": "title",            # заголовок элемента = номер рейса
    "planned_date": "ufReisDate", # дата планирования
    "planned_time": "ufReisTime", # время
    "driver_name": "ufDriver",    # водитель (строка)
    "vehicle_name": "ufVehicle",  # автомобиль (строка)
    "load_address": "ufLoadAddr", # адрес погрузки
    "unload_address": "ufUnloadAddr",  # адрес выгрузки
    "route_name": "ufRoute",
    "km": "ufKmPlan",
    "volume": "ufVolumePlan",
    "actual_km": "ufKmFact",
    "actual_volume": "ufVolumeFact",
    "status": "ufStatus",
    "customer_name": "ufCustomer",
    "polygon_name": "ufPolygon",
    "waste_bin_count": "ufBins",
    "sum_driver": "ufSumDriver",
    "comment": "ufComment",
    "logist_comment": "ufLogistComment",
}


def _http_post(webhook_base: str, method: str, params: dict) -> dict:
    """Вызов Bitrix24 REST. Bitrix ждёт form-urlencoded (или GET-параметры), не JSON."""
    url = webhook_base.rstrip("/") + "/" + method
    # Bitrix24 REST принимает POST с телом application/x-www-form-urlencoded
    data = urllib.parse.urlencode(params).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=BITRIX_TIMEOUT) as resp:
            body = resp.read().decode("utf-8")
        return json.loads(body)
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}: {e.read().decode('utf-8', 'ignore')}"}
    except Exception as e:  # сеть/таймаут/JSON
        return {"error": repr(e)}


def get_integration_settings(db) -> Optional["models.IntegrationSetting"]:
    if models is None:
        return None
    return db.query(models.IntegrationSetting).filter(
        models.IntegrationSetting.provider == "bitrix24"
    ).first()


def find_smart_process_ids(webhook_base: str) -> dict:
    """Возвращает {entityTypeId: title} для всех смарт-процессов (типы crm, не сделки)."""
    out = {}
    res = _http_post(webhook_base, "crm.type.list", {})
    if "error" in res:
        return {"_error": res["error"]}
    result = res.get("result", {})
    # crm.type.list возвращает список типов
    items = result.get("types") or result.get("items") or (result if isinstance(result, list) else [])
    for it in items:
        title = (it.get("title") or it.get("name") or "").lower()
        entity = it.get("entityTypeId") or it.get("entityTypeId") or it.get("id")
        if entity is None:
            continue
        out[str(entity)] = it.get("title") or it.get("name") or str(entity)
    return out


def resolve_process_entity(webhook_base: str, kind) -> Optional[str]:
    """По типу рейса (пухтовоз/самосвал) находит entityTypeId нужного смарт-процесса."""
    types = find_smart_process_ids(webhook_base)
    if "_error" in types:
        return None
    kind_val = kind.value if hasattr(kind, "value") else str(kind)
    needle = PROCESS_NAME_PUKHTOVOZ if kind_val == "пухтовоз" else PROCESS_NAME_SAMOSVAL
    for eid, title in types.items():
        if needle in title.lower():
            return eid
    return None


def get_element_fields(webhook_base: str, entity_id: str) -> dict:
    """Поля элемента смарт-процесса (для отладки/маппинга)."""
    res = _http_post(webhook_base, "crm.item.fields", {"entityTypeId": int(entity_id)})
    if "error" in res:
        return {"_error": res["error"]}
    return res.get("result", {}).get("fields", {})


def _as_bitrix_value(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()[:10] if isinstance(value, date) and not isinstance(value, datetime) else value.isoformat()
    if value is None:
        return ""
    return value


def build_fields(req) -> dict:
    """Строит словарь полей Bitrix24 из объекта рейса приложения."""
    f = {}
    # заголовок
    f[FIELD_MAP["number"]] = req.number or ""
    f[FIELD_MAP["planned_date"]] = _as_bitrix_value(req.planned_date)
    f[FIELD_MAP["planned_time"]] = req.planned_time or ""
    f[FIELD_MAP["driver_name"]] = req.driver.full_name if req.driver else ""
    f[FIELD_MAP["vehicle_name"]] = req.vehicle.name if req.vehicle else ""
    f[FIELD_MAP["load_address"]] = req.load_address or ""
    f[FIELD_MAP["unload_address"]] = req.unload_address or ""
    f[FIELD_MAP["route_name"]] = req.route_name or ""
    f[FIELD_MAP["km"]] = req.km or 0
    f[FIELD_MAP["volume"]] = req.volume or 0
    f[FIELD_MAP["actual_km"]] = req.actual_km or 0
    f[FIELD_MAP["actual_volume"]] = req.actual_volume or 0
    f[FIELD_MAP["status"]] = req.status.value if hasattr(req.status, "value") else str(req.status)
    f[FIELD_MAP["customer_name"]] = req.customer.name if req.customer else ""
    f[FIELD_MAP["polygon_name"]] = req.polygon.name if req.polygon else ""
    f[FIELD_MAP["waste_bin_count"]] = req.waste_bin_count or 0
    f[FIELD_MAP["sum_driver"]] = req.sum_driver or 0
    f[FIELD_MAP["comment"]] = req.comment or ""
    f[FIELD_MAP["logist_comment"]] = req.logist_comment or ""
    return f


def sync_trip(req, db, settings=None) -> dict:
    """
    Создаёт или обновляет элемент смарт-процесса в Bitrix24 для рейса req.
    Возвращает словарь с результатом. Сохраняет bitrix_element_id в req.
    """
    if settings is None:
        settings = get_integration_settings(db)
    if not settings or not settings.is_active or not settings.webhook_url:
        print("BITRIX_SYNC_SKIP no_active_integration", flush=True)
        return {"skipped": True, "reason": "no_active_integration"}
    webhook = settings.webhook_url
    entity = resolve_process_entity(webhook, req.kind)
    if not entity:
        print("BITRIX_SYNC_SKIP process_not_found", str(req.kind), flush=True)
        return {"skipped": True, "reason": "process_not_found"}
    fields = build_fields(req)
    if req.bitrix_element_id:
        # обновляем существующий элемент
        payload = {"entityTypeId": int(entity), "id": int(req.bitrix_element_id), "fields": fields}
        res = _http_post(webhook, "crm.item.update", payload)
        action = "update"
    else:
        payload = {"entityTypeId": int(entity), "fields": fields}
        res = _http_post(webhook, "crm.item.add", payload)
        action = "add"
    if "error" in res:
        print("BITRIX_SYNC_ERROR", action, res["error"], flush=True)
        return {"error": res["error"], "action": action}
    result = res.get("result", {})
    elem_id = result.get("id") or result.get("item", {}).get("id")
    if elem_id and not req.bitrix_element_id:
        req.bitrix_element_id = int(elem_id)
        db.add(req)
    print("BITRIX_SYNC_OK", action, "entity", entity, "elem", elem_id, "trip", req.id, flush=True)
    return {"ok": True, "action": action, "element_id": elem_id}


def delete_trip(req, db, settings=None) -> dict:
    """Удаляет элемент в Bitrix24 при удалении рейса (исключает висячие записи)."""
    if settings is None:
        settings = get_integration_settings(db)
    if not settings or not settings.is_active or not settings.webhook_url or not req.bitrix_element_id:
        return {"skipped": True}
    entity = resolve_process_entity(settings.webhook_url, req.kind)
    if not entity:
        return {"skipped": True, "reason": "process_not_found"}
    res = _http_post(settings.webhook_url, "crm.item.delete",
                     {"entityTypeId": int(entity), "id": int(req.bitrix_element_id)})
    if "error" in res:
        print("BITRIX_DELETE_ERROR", res["error"], flush=True)
        return {"error": res["error"]}
    print("BITRIX_DELETE_OK", req.bitrix_element_id, flush=True)
    return {"ok": True}
