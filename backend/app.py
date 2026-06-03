"""
Flask application — REST API endpoints for the UPS KPI webapp.
"""
import os
import logging
from datetime import datetime, timedelta

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from sqlalchemy import func

from models import db, Shipment, Activity, ShipmentException, SyncLog
from ups_client import UPSClient
import sync as sync_module

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "..", "frontend")

app = Flask(__name__, static_folder=FRONTEND_DIR, static_url_path="")
app.config["SECRET_KEY"] = os.getenv("FLASK_SECRET_KEY", "dev-secret-change-me")
app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv("DATABASE_URL", "sqlite:///ups_shipments.db")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db.init_app(app)
CORS(app)

ups = UPSClient()


@app.before_request
def create_tables():
    db.create_all()


# ------------------------------------------------------------------ #
#  Frontend serving
# ------------------------------------------------------------------ #

@app.route("/")
def index():
    return send_from_directory(FRONTEND_DIR, "index.html")

@app.route("/<path:path>")
def static_files(path):
    return send_from_directory(FRONTEND_DIR, path)


# ------------------------------------------------------------------ #
#  Config / status
# ------------------------------------------------------------------ #

@app.route("/api/status")
def api_status():
    return jsonify({
        "configured": ups.is_configured(),
        "environment": os.getenv("UPS_ENVIRONMENT", "sandbox"),
        "account_number": os.getenv("UPS_ACCOUNT_NUMBER", ""),
        "subscriptions": [
            s.strip()
            for s in os.getenv("UPS_QV_SUBSCRIPTIONS", "").split(",")
            if s.strip()
        ],
    })


# ------------------------------------------------------------------ #
#  Sync endpoints
# ------------------------------------------------------------------ #

@app.route("/api/sync/quantum-view", methods=["POST"])
def sync_qv():
    body = request.get_json(silent=True) or {}
    days_back = int(body.get("days_back", 7))
    subscription = body.get("subscription") or os.getenv("UPS_QV_SUBSCRIPTIONS", "")
    if not subscription:
        return jsonify({"error": "No subscription name provided"}), 400

    # QV max window is 7 days — chunk if needed
    subscriptions = [s.strip() for s in subscription.split(",") if s.strip()]
    end_dt = datetime.utcnow()
    begin_dt = end_dt - timedelta(days=min(days_back, 7))

    total = 0
    for sub in subscriptions:
        total += sync_module.sync_quantum_view(sub, begin_dt, end_dt)

    return jsonify({"synced": total, "begin": begin_dt.isoformat(), "end": end_dt.isoformat()})


@app.route("/api/sync/tracking/<tracking_number>", methods=["POST"])
def sync_tracking(tracking_number):
    success = sync_module.enrich_from_tracking_api(tracking_number.strip())
    if not success:
        return jsonify({"error": "Not found or API error"}), 404
    shipment = Shipment.query.filter_by(tracking_number=tracking_number).first()
    return jsonify(shipment.to_dict() if shipment else {})


@app.route("/api/sync/logs")
def sync_logs():
    logs = SyncLog.query.order_by(SyncLog.started_at.desc()).limit(50).all()
    return jsonify([l.to_dict() for l in logs])


# ------------------------------------------------------------------ #
#  Shipments
# ------------------------------------------------------------------ #

@app.route("/api/shipments")
def list_shipments():
    q = Shipment.query

    # Filters
    if request.args.get("service"):
        q = q.filter(Shipment.service_description.ilike(f"%{request.args['service']}%"))
    if request.args.get("dest_state"):
        q = q.filter(Shipment.dest_state == request.args["dest_state"].upper())
    if request.args.get("is_delivered"):
        val = request.args["is_delivered"].lower() == "true"
        q = q.filter(Shipment.is_delivered == val)
    if request.args.get("on_time") is not None and request.args.get("on_time") != "":
        val = request.args["on_time"].lower() == "true"
        q = q.filter(Shipment.on_time == val)
    if request.args.get("from_date"):
        q = q.filter(Shipment.pickup_date >= request.args["from_date"])
    if request.args.get("to_date"):
        q = q.filter(Shipment.pickup_date <= request.args["to_date"])
    if request.args.get("search"):
        term = f"%{request.args['search']}%"
        q = q.filter(
            db.or_(
                Shipment.tracking_number.ilike(term),
                Shipment.reference_numbers.ilike(term),
                Shipment.dest_name.ilike(term),
                Shipment.dest_city.ilike(term),
            )
        )

    total = q.count()
    page = int(request.args.get("page", 1))
    per_page = int(request.args.get("per_page", 50))
    shipments = q.order_by(Shipment.pickup_date.desc()).offset((page - 1) * per_page).limit(per_page).all()

    return jsonify({
        "total": total,
        "page": page,
        "per_page": per_page,
        "shipments": [s.to_dict() for s in shipments],
    })


@app.route("/api/shipments/<tracking_number>")
def get_shipment(tracking_number):
    shipment = Shipment.query.filter_by(tracking_number=tracking_number).first_or_404()
    data = shipment.to_dict()
    data["activities"] = [a.to_dict() for a in shipment.activities]
    data["exceptions"] = [e.to_dict() for e in shipment.exceptions]
    return jsonify(data)


# ------------------------------------------------------------------ #
#  KPI endpoints
# ------------------------------------------------------------------ #

@app.route("/api/kpis/summary")
def kpi_summary():
    total = Shipment.query.count()
    delivered = Shipment.query.filter_by(is_delivered=True).count()
    in_transit = Shipment.query.filter_by(is_delivered=False).count()
    on_time = Shipment.query.filter_by(on_time=True).count()
    late = Shipment.query.filter_by(on_time=False).count()
    exceptions = ShipmentException.query.count()

    on_time_pct = round((on_time / delivered * 100), 1) if delivered else 0

    avg_weight = db.session.query(func.avg(Shipment.weight)).filter(
        Shipment.weight.isnot(None), Shipment.weight > 0
    ).scalar()

    return jsonify({
        "total_shipments": total,
        "delivered": delivered,
        "in_transit": in_transit,
        "on_time": on_time,
        "late": late,
        "on_time_pct": on_time_pct,
        "exceptions": exceptions,
        "avg_weight_lbs": round(float(avg_weight or 0), 2),
    })


@app.route("/api/kpis/by-service")
def kpi_by_service():
    rows = (
        db.session.query(
            Shipment.service_description,
            func.count(Shipment.id).label("count"),
            func.sum(db.case((Shipment.is_delivered == True, 1), else_=0)).label("delivered"),
            func.sum(db.case((Shipment.on_time == True, 1), else_=0)).label("on_time"),
        )
        .group_by(Shipment.service_description)
        .order_by(func.count(Shipment.id).desc())
        .all()
    )
    return jsonify([
        {
            "service": r.service_description or "Unknown",
            "count": r.count,
            "delivered": r.delivered or 0,
            "on_time": r.on_time or 0,
            "on_time_pct": round((r.on_time or 0) / (r.delivered or 1) * 100, 1),
        }
        for r in rows
    ])


@app.route("/api/kpis/by-state")
def kpi_by_state():
    rows = (
        db.session.query(
            Shipment.dest_state,
            func.count(Shipment.id).label("count"),
        )
        .filter(Shipment.dest_state.isnot(None))
        .group_by(Shipment.dest_state)
        .order_by(func.count(Shipment.id).desc())
        .limit(20)
        .all()
    )
    return jsonify([{"state": r.dest_state, "count": r.count} for r in rows])


@app.route("/api/kpis/daily-volume")
def kpi_daily_volume():
    days = int(request.args.get("days", 30))
    cutoff = (datetime.utcnow() - timedelta(days=days)).strftime("%Y%m%d")

    rows = (
        db.session.query(
            Shipment.pickup_date,
            func.count(Shipment.id).label("count"),
            func.sum(db.case((Shipment.on_time == True, 1), else_=0)).label("on_time"),
        )
        .filter(Shipment.pickup_date >= cutoff)
        .group_by(Shipment.pickup_date)
        .order_by(Shipment.pickup_date)
        .all()
    )
    return jsonify([
        {"date": r.pickup_date, "count": r.count, "on_time": r.on_time or 0}
        for r in rows
    ])


@app.route("/api/kpis/exceptions")
def kpi_exceptions():
    rows = (
        db.session.query(
            ShipmentException.exception_code,
            ShipmentException.description,
            func.count(ShipmentException.id).label("count"),
        )
        .group_by(ShipmentException.exception_code, ShipmentException.description)
        .order_by(func.count(ShipmentException.id).desc())
        .limit(15)
        .all()
    )
    return jsonify([
        {"code": r.exception_code, "description": r.description, "count": r.count}
        for r in rows
    ])


@app.route("/api/kpis/transit-time")
def kpi_transit_time():
    """Average days from pickup to delivery by service type."""
    rows = (
        db.session.query(
            Shipment.service_description,
            Shipment.pickup_date,
            Shipment.actual_delivery_date,
        )
        .filter(
            Shipment.pickup_date.isnot(None),
            Shipment.actual_delivery_date.isnot(None),
            Shipment.is_delivered == True,
        )
        .all()
    )

    service_days = {}
    for r in rows:
        try:
            p = datetime.strptime(r.pickup_date, "%Y%m%d")
            d = datetime.strptime(r.actual_delivery_date, "%Y%m%d")
            days = (d - p).days
            if days < 0 or days > 30:
                continue
            svc = r.service_description or "Unknown"
            service_days.setdefault(svc, []).append(days)
        except (ValueError, TypeError):
            continue

    return jsonify([
        {
            "service": svc,
            "avg_days": round(sum(days_list) / len(days_list), 2),
            "count": len(days_list),
        }
        for svc, days_list in sorted(service_days.items(), key=lambda x: -len(x[1]))
    ])


if __name__ == "__main__":
    with app.app_context():
        db.create_all()
    app.run(host="0.0.0.0", port=5000, debug=True)
