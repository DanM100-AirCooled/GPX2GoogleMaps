"""
SQLAlchemy models for persisting UPS shipment data.
"""
from datetime import datetime
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


class Shipment(db.Model):
    __tablename__ = "shipments"

    id = db.Column(db.Integer, primary_key=True)
    tracking_number = db.Column(db.String(34), unique=True, nullable=False, index=True)

    # Service
    service_code = db.Column(db.String(10))
    service_description = db.Column(db.String(100))

    # Status
    status_code = db.Column(db.String(20))
    status_description = db.Column(db.String(200))
    is_delivered = db.Column(db.Boolean, default=False)

    # Addresses
    origin_name = db.Column(db.String(100))
    origin_city = db.Column(db.String(100))
    origin_state = db.Column(db.String(10))
    origin_zip = db.Column(db.String(20))
    origin_country = db.Column(db.String(10))

    dest_name = db.Column(db.String(100))
    dest_city = db.Column(db.String(100))
    dest_state = db.Column(db.String(10))
    dest_zip = db.Column(db.String(20))
    dest_country = db.Column(db.String(10))

    # Dates (stored as date strings YYYYMMDD for direct UPS format compatibility)
    scheduled_delivery_date = db.Column(db.String(10))   # SDD
    rescheduled_delivery_date = db.Column(db.String(10)) # RDD
    actual_delivery_date = db.Column(db.String(10))      # DEL
    pickup_date = db.Column(db.String(10))               # from manifest/origin event

    # On-time flag: True=on time, False=late, None=not yet delivered
    on_time = db.Column(db.Boolean, nullable=True)

    # Physical details
    weight = db.Column(db.Float)
    weight_unit = db.Column(db.String(5))
    length = db.Column(db.Float)
    width = db.Column(db.Float)
    height = db.Column(db.Float)
    dim_unit = db.Column(db.String(10))
    package_count = db.Column(db.Integer, default=1)

    # Reference numbers (store as comma-separated string for simplicity)
    reference_numbers = db.Column(db.String(500))

    # Billing
    billing_code = db.Column(db.String(5))   # 01=Shipper, 02=Consignee, 03=Third Party

    # Source: "quantum_view" or "tracking_api"
    data_source = db.Column(db.String(20))

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    activities = db.relationship("Activity", backref="shipment", cascade="all, delete-orphan", lazy=True)
    exceptions = db.relationship("ShipmentException", backref="shipment", cascade="all, delete-orphan", lazy=True)

    def to_dict(self):
        return {
            "id": self.id,
            "tracking_number": self.tracking_number,
            "service_code": self.service_code,
            "service_description": self.service_description,
            "status_code": self.status_code,
            "status_description": self.status_description,
            "is_delivered": self.is_delivered,
            "origin_city": self.origin_city,
            "origin_state": self.origin_state,
            "origin_country": self.origin_country,
            "dest_name": self.dest_name,
            "dest_city": self.dest_city,
            "dest_state": self.dest_state,
            "dest_zip": self.dest_zip,
            "dest_country": self.dest_country,
            "scheduled_delivery_date": self.scheduled_delivery_date,
            "rescheduled_delivery_date": self.rescheduled_delivery_date,
            "actual_delivery_date": self.actual_delivery_date,
            "pickup_date": self.pickup_date,
            "on_time": self.on_time,
            "weight": self.weight,
            "weight_unit": self.weight_unit,
            "package_count": self.package_count,
            "reference_numbers": self.reference_numbers,
            "data_source": self.data_source,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class Activity(db.Model):
    __tablename__ = "activities"

    id = db.Column(db.Integer, primary_key=True)
    shipment_id = db.Column(db.Integer, db.ForeignKey("shipments.id"), nullable=False, index=True)

    date = db.Column(db.String(10))        # YYYYMMDD
    time = db.Column(db.String(10))        # HHMMSS
    gmt_date = db.Column(db.String(10))
    gmt_time = db.Column(db.String(10))
    gmt_offset = db.Column(db.String(10))

    status_code = db.Column(db.String(20))
    status_description = db.Column(db.String(200))
    status_type = db.Column(db.String(5))

    city = db.Column(db.String(100))
    state = db.Column(db.String(10))
    country = db.Column(db.String(10))
    slic = db.Column(db.String(20))        # Site Location Indicator Code

    logical_scan = db.Column(db.Boolean)   # True=logical event, False=physical

    def to_dict(self):
        return {
            "date": self.date,
            "time": self.time,
            "gmt_date": self.gmt_date,
            "gmt_time": self.gmt_time,
            "gmt_offset": self.gmt_offset,
            "status_code": self.status_code,
            "status_description": self.status_description,
            "status_type": self.status_type,
            "city": self.city,
            "state": self.state,
            "country": self.country,
            "slic": self.slic,
            "logical_scan": self.logical_scan,
        }


class ShipmentException(db.Model):
    __tablename__ = "shipment_exceptions"

    id = db.Column(db.Integer, primary_key=True)
    shipment_id = db.Column(db.Integer, db.ForeignKey("shipments.id"), nullable=False, index=True)

    exception_code = db.Column(db.String(20))
    description = db.Column(db.String(500))
    resolution_code = db.Column(db.String(20))
    rescheduled_date = db.Column(db.String(10))
    event_date = db.Column(db.String(10))

    def to_dict(self):
        return {
            "exception_code": self.exception_code,
            "description": self.description,
            "resolution_code": self.resolution_code,
            "rescheduled_date": self.rescheduled_date,
            "event_date": self.event_date,
        }


class SyncLog(db.Model):
    __tablename__ = "sync_log"

    id = db.Column(db.Integer, primary_key=True)
    sync_type = db.Column(db.String(20))      # "quantum_view" or "tracking"
    subscription = db.Column(db.String(100))
    begin_dt = db.Column(db.String(20))
    end_dt = db.Column(db.String(20))
    records_processed = db.Column(db.Integer, default=0)
    status = db.Column(db.String(20))         # "success", "error"
    error_message = db.Column(db.Text)
    started_at = db.Column(db.DateTime, default=datetime.utcnow)
    completed_at = db.Column(db.DateTime)

    def to_dict(self):
        return {
            "id": self.id,
            "sync_type": self.sync_type,
            "subscription": self.subscription,
            "begin_dt": self.begin_dt,
            "end_dt": self.end_dt,
            "records_processed": self.records_processed,
            "status": self.status,
            "error_message": self.error_message,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
        }
