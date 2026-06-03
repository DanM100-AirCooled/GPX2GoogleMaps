"""
ETL logic: pull data from UPS APIs and upsert into the local database.
"""
import logging
import time
from datetime import datetime, timedelta

from models import db, Shipment, Activity, ShipmentException, SyncLog
from ups_client import UPSClient

logger = logging.getLogger(__name__)

ups = UPSClient()


# ------------------------------------------------------------------ #
#  Quantum View sync
# ------------------------------------------------------------------ #

def sync_quantum_view(subscription_name, begin_dt, end_dt):
    log = SyncLog(
        sync_type="quantum_view",
        subscription=subscription_name,
        begin_dt=begin_dt.strftime("%Y%m%d%H%M%S"),
        end_dt=end_dt.strftime("%Y%m%d%H%M%S"),
        status="running",
    )
    db.session.add(log)
    db.session.commit()

    total = 0
    try:
        for page in ups.get_quantum_view_all_pages(subscription_name, begin_dt, end_dt):
            count = _process_qv_response(page)
            total += count
            time.sleep(0.5)  # conservative throttle

        log.status = "success"
        log.records_processed = total
    except Exception as exc:
        logger.exception("Quantum View sync failed: %s", exc)
        log.status = "error"
        log.error_message = str(exc)
    finally:
        log.completed_at = datetime.utcnow()
        db.session.commit()

    return total


def _process_qv_response(response):
    count = 0
    try:
        qv_events = (
            response.get("QuantumViewResponse", {})
            .get("QuantumViewEvents", {})
        )
        sub_events = qv_events.get("SubscriptionEvents", [])
        if isinstance(sub_events, dict):
            sub_events = [sub_events]
    except (AttributeError, TypeError):
        return 0

    for sub_event in sub_events:
        files = sub_event.get("SubscriptionFile", [])
        if isinstance(files, dict):
            files = [files]
        for f in files:
            events = f.get("Manifest", []) + f.get("Origin", []) + \
                     f.get("Delivery", []) + f.get("Exception", []) + \
                     f.get("Generic", [])

            for event_type, event_list in [
                ("manifest", f.get("Manifest", [])),
                ("origin",   f.get("Origin", [])),
                ("delivery", f.get("Delivery", [])),
                ("exception", f.get("Exception", [])),
            ]:
                if isinstance(event_list, dict):
                    event_list = [event_list]
                for event in event_list:
                    _upsert_from_qv_event(event_type, event)
                    count += 1

    db.session.commit()
    return count


def _upsert_from_qv_event(event_type, event):
    pkgs = event.get("Package", [])
    if isinstance(pkgs, dict):
        pkgs = [pkgs]

    for pkg in pkgs:
        tn = pkg.get("TrackingNumber") or event.get("TrackingNumber", "")
        if not tn:
            continue

        shipment = Shipment.query.filter_by(tracking_number=tn).first()
        if not shipment:
            shipment = Shipment(tracking_number=tn, data_source="quantum_view")
            db.session.add(shipment)

        if event_type == "manifest":
            _apply_manifest(shipment, event, pkg)
        elif event_type == "origin":
            _apply_origin(shipment, event)
        elif event_type == "delivery":
            _apply_delivery(shipment, event)
        elif event_type == "exception":
            _apply_exception(shipment, event)


def _apply_manifest(shipment, event, pkg):
    shipper = event.get("Shipper", {})
    ship_to = event.get("ShipTo", {})
    service = event.get("Service", {})

    shipment.service_code = service.get("Code") or shipment.service_code
    shipment.service_description = service.get("Description") or shipment.service_description

    sa = shipper.get("Address", {})
    shipment.origin_name = shipper.get("CompanyName") or shipment.origin_name
    shipment.origin_city = sa.get("City") or shipment.origin_city
    shipment.origin_state = sa.get("StateProvinceCode") or shipment.origin_state
    shipment.origin_zip = sa.get("PostalCode") or shipment.origin_zip
    shipment.origin_country = sa.get("CountryCode") or shipment.origin_country

    da = ship_to.get("Address", {})
    shipment.dest_name = ship_to.get("CompanyName") or ship_to.get("AttentionName") or shipment.dest_name
    shipment.dest_city = da.get("City") or shipment.dest_city
    shipment.dest_state = da.get("StateProvinceCode") or shipment.dest_state
    shipment.dest_zip = da.get("PostalCode") or shipment.dest_zip
    shipment.dest_country = da.get("CountryCode") or shipment.dest_country

    sched = event.get("PickupDate")
    if sched:
        shipment.pickup_date = sched

    sdd = event.get("ScheduledDeliveryDate") or event.get("ScheduledDeliveryTime", {}).get("Date")
    if sdd:
        shipment.scheduled_delivery_date = sdd

    billing = event.get("BillingOption", {})
    shipment.billing_code = billing.get("Code") or shipment.billing_code

    w = pkg.get("PackageWeight", {})
    try:
        shipment.weight = float(w.get("Weight", 0)) or shipment.weight
    except (TypeError, ValueError):
        pass
    shipment.weight_unit = w.get("UnitOfMeasurement", {}).get("Code") or shipment.weight_unit

    dims = pkg.get("Dimensions", {})
    try:
        shipment.length = float(dims.get("Length", 0)) or shipment.length
        shipment.width = float(dims.get("Width", 0)) or shipment.width
        shipment.height = float(dims.get("Height", 0)) or shipment.height
    except (TypeError, ValueError):
        pass
    shipment.dim_unit = dims.get("UnitOfMeasurement", {}).get("Code") or shipment.dim_unit

    refs = pkg.get("ReferenceNumber", [])
    if isinstance(refs, dict):
        refs = [refs]
    ref_vals = [r.get("Value", "") for r in refs if r.get("Value")]
    if ref_vals:
        shipment.reference_numbers = ",".join(ref_vals)


def _apply_origin(shipment, event):
    pickup = event.get("PickupDate")
    if pickup:
        shipment.pickup_date = pickup


def _apply_delivery(shipment, event):
    delivery_date = event.get("DeliveryDate")
    if delivery_date:
        shipment.actual_delivery_date = delivery_date
        shipment.is_delivered = True
        shipment.status_description = "Delivered"

        if shipment.scheduled_delivery_date and delivery_date:
            shipment.on_time = delivery_date <= shipment.scheduled_delivery_date


def _apply_exception(shipment, event):
    exc = ShipmentException(
        shipment=shipment,
        exception_code=event.get("ExceptionCode"),
        description=event.get("ExceptionDescription"),
        resolution_code=event.get("ResolutionCode"),
        rescheduled_date=event.get("RescheduledDeliveryDate"),
        event_date=event.get("ExceptionDate"),
    )
    db.session.add(exc)


# ------------------------------------------------------------------ #
#  Tracking API enrichment
# ------------------------------------------------------------------ #

def enrich_from_tracking_api(tracking_number):
    """Pull full tracking detail and update/create the shipment record."""
    try:
        data = ups.track_package(tracking_number, return_milestones=True, return_pod=False)
    except Exception as exc:
        logger.warning("Tracking API failed for %s: %s", tracking_number, exc)
        return False

    if not data:
        return False

    try:
        shipments = data["trackResponse"]["shipment"]
    except (KeyError, TypeError):
        return False

    for s_data in shipments:
        packages = s_data.get("package", [])
        if isinstance(packages, dict):
            packages = [packages]
        for pkg in packages:
            _upsert_from_tracking(pkg, tracking_number)

    db.session.commit()
    return True


def _upsert_from_tracking(pkg, tracking_number):
    tn = pkg.get("trackingNumber", tracking_number)
    shipment = Shipment.query.filter_by(tracking_number=tn).first()
    if not shipment:
        shipment = Shipment(tracking_number=tn, data_source="tracking_api")
        db.session.add(shipment)

    svc = pkg.get("service", {})
    shipment.service_code = svc.get("code") or shipment.service_code
    shipment.service_description = svc.get("description") or shipment.service_description

    current = pkg.get("currentStatus", {})
    shipment.status_code = current.get("code") or shipment.status_code
    shipment.status_description = current.get("description") or shipment.status_description

    ucix = pkg.get("ucixStatus", "")
    shipment.is_delivered = (ucix == "C") or shipment.is_delivered

    for addr in pkg.get("packageAddress", []):
        a = addr.get("address", {})
        t = addr.get("type", "").upper()
        if t == "ORIGIN":
            shipment.origin_name = addr.get("name") or shipment.origin_name
            shipment.origin_city = a.get("city") or shipment.origin_city
            shipment.origin_state = a.get("stateProvince") or shipment.origin_state
            shipment.origin_zip = a.get("postalCode") or shipment.origin_zip
            shipment.origin_country = a.get("countryCode") or shipment.origin_country
        elif t == "DESTINATION":
            shipment.dest_name = addr.get("name") or shipment.dest_name
            shipment.dest_city = a.get("city") or shipment.dest_city
            shipment.dest_state = a.get("stateProvince") or shipment.dest_state
            shipment.dest_zip = a.get("postalCode") or shipment.dest_zip
            shipment.dest_country = a.get("countryCode") or shipment.dest_country

    for dd in pkg.get("deliveryDate", []):
        dt_type = dd.get("type", "")
        dt_val = dd.get("date", "")
        if dt_type == "SDD":
            shipment.scheduled_delivery_date = dt_val
        elif dt_type == "RDD":
            shipment.rescheduled_delivery_date = dt_val
        elif dt_type == "DEL":
            shipment.actual_delivery_date = dt_val
            shipment.is_delivered = True

    if shipment.actual_delivery_date and shipment.scheduled_delivery_date:
        shipment.on_time = (
            shipment.actual_delivery_date <= shipment.scheduled_delivery_date
        )

    dim = pkg.get("dimension", {})
    try:
        shipment.length = float(dim.get("length", 0)) or shipment.length
        shipment.width = float(dim.get("width", 0)) or shipment.width
        shipment.height = float(dim.get("height", 0)) or shipment.height
        shipment.dim_unit = dim.get("unitOfDimension") or shipment.dim_unit
    except (TypeError, ValueError):
        pass

    wt = pkg.get("weight", {})
    try:
        shipment.weight = float(wt.get("weight", 0)) or shipment.weight
        shipment.weight_unit = wt.get("unitOfMeasurement") or shipment.weight_unit
    except (TypeError, ValueError):
        pass

    shipment.package_count = pkg.get("packageCount", shipment.package_count or 1)

    refs = [r.get("number", "") for r in pkg.get("referenceNumber", []) if r.get("number")]
    if refs:
        shipment.reference_numbers = ",".join(refs)

    # Rebuild activity log
    Activity.query.filter_by(shipment_id=shipment.id).delete()
    for act in pkg.get("activity", []):
        loc = act.get("location", {})
        addr = loc.get("address", {})
        status = act.get("status", {})
        activity = Activity(
            shipment=shipment,
            date=act.get("date"),
            time=act.get("time"),
            gmt_date=act.get("gmtDate"),
            gmt_time=act.get("gmtTime"),
            gmt_offset=act.get("gmtOffset"),
            status_code=status.get("statusCode") or status.get("code"),
            status_description=status.get("description"),
            status_type=status.get("type"),
            city=addr.get("city"),
            state=addr.get("stateProvince"),
            country=addr.get("countryCode"),
            slic=loc.get("slic"),
            logical_scan=act.get("logicalScan"),
        )
        db.session.add(activity)

    return shipment
