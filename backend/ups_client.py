"""
UPS API client — handles OAuth token lifecycle and all API calls.
"""
import os
import time
import base64
import logging
import requests
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

SANDBOX_BASE = "https://wwwcie.ups.com"
PROD_BASE = "https://onlinetools.ups.com"
TOKEN_PATH = "/security/v1/oauth/token"
TRACK_PATH = "/api/track/v1"
QV_PATH = "/api/quantumview/v3/events"


class UPSClient:
    def __init__(self):
        self.client_id = os.getenv("UPS_CLIENT_ID", "")
        self.client_secret = os.getenv("UPS_CLIENT_SECRET", "")
        self.account_number = os.getenv("UPS_ACCOUNT_NUMBER", "")
        env = os.getenv("UPS_ENVIRONMENT", "sandbox").lower()
        self.base_url = SANDBOX_BASE if env == "sandbox" else PROD_BASE
        self._token = None
        self._token_expires_at = 0

    def is_configured(self):
        return bool(self.client_id and self.client_secret)

    def _get_token(self):
        if self._token and time.time() < self._token_expires_at - 60:
            return self._token

        credentials = base64.b64encode(
            f"{self.client_id}:{self.client_secret}".encode()
        ).decode()

        headers = {
            "Authorization": f"Basic {credentials}",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        if self.account_number:
            headers["x-merchant-id"] = self.account_number

        resp = requests.post(
            f"{self.base_url}{TOKEN_PATH}",
            headers=headers,
            data={"grant_type": "client_credentials"},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()

        self._token = data["access_token"]
        expires_in = int(data.get("expires_in", 3600))
        self._token_expires_at = time.time() + expires_in
        logger.info("UPS OAuth token obtained, expires in %d seconds", expires_in)
        return self._token

    def _auth_headers(self, transaction_id=None):
        token = self._get_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "transId": transaction_id or f"txn-{int(time.time())}",
            "transactionSrc": "ups-kpi-webapp",
        }
        return headers

    # ------------------------------------------------------------------ #
    #  Tracking API
    # ------------------------------------------------------------------ #

    def track_package(self, tracking_number, return_milestones=True, return_pod=False):
        """Fetch full tracking detail for a single tracking number."""
        url = f"{self.base_url}{TRACK_PATH}/details/{tracking_number}"
        params = {
            "locale": "en_US",
            "returnMilestones": str(return_milestones).lower(),
            "returnPOD": str(return_pod).lower(),
        }
        resp = requests.get(url, headers=self._auth_headers(), params=params, timeout=15)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()

    def track_by_reference(
        self,
        reference_number,
        shipper_num=None,
        from_date=None,
        to_date=None,
        ref_type="SmallPackage",
    ):
        """Search shipments by your reference/PO number."""
        url = f"{self.base_url}{TRACK_PATH}/reference/details/{reference_number}"
        if from_date is None:
            from_date = (datetime.utcnow() - timedelta(days=14)).strftime("%Y%m%d")
        if to_date is None:
            to_date = datetime.utcnow().strftime("%Y%m%d")
        params = {
            "locale": "en_US",
            "fromPickUpDate": from_date,
            "toPickUpDate": to_date,
            "refNumType": ref_type,
        }
        if shipper_num or self.account_number:
            params["shipperNum"] = shipper_num or self.account_number
        resp = requests.get(url, headers=self._auth_headers(), params=params, timeout=15)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()

    def track_shipment_packages(self, tracking_number, offset=0, count=50):
        """Get all packages in a multi-piece shipment."""
        url = f"{self.base_url}{TRACK_PATH}/shipment/details/{tracking_number}"
        params = {"locale": "en_US", "offset": offset, "count": count}
        resp = requests.get(url, headers=self._auth_headers(), params=params, timeout=15)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------ #
    #  Quantum View API
    # ------------------------------------------------------------------ #

    def get_quantum_view_events(
        self,
        subscription_name,
        begin_dt=None,
        end_dt=None,
        bookmark=None,
        file_name=None,
    ):
        """
        Pull shipment events from a Quantum View subscription.
        begin_dt / end_dt: datetime objects or YYYYMMDDHHmmss strings.
        Returns the parsed JSON response.
        """
        url = f"{self.base_url}{QV_PATH}"

        def _fmt(dt):
            if isinstance(dt, datetime):
                return dt.strftime("%Y%m%d%H%M%S")
            return dt

        if begin_dt is None:
            begin_dt = datetime.utcnow() - timedelta(days=1)
        if end_dt is None:
            end_dt = datetime.utcnow()

        sub_req = {"Name": subscription_name}
        if file_name:
            sub_req["FileName"] = file_name
        elif bookmark:
            sub_req["Bookmark"] = bookmark
        else:
            sub_req["DateTimeRange"] = {
                "BeginDateTime": _fmt(begin_dt),
                "EndDateTime": _fmt(end_dt),
            }

        body = {
            "QuantumViewRequest": {
                "Request": {
                    "TransactionReference": {"CustomerContext": "ups-kpi-webapp"},
                    "RequestAction": "QVEvents",
                },
                "SubscriptionRequest": [sub_req],
            }
        }

        # QV uses XML-style wrapper — send as JSON (v3 supports JSON)
        headers = self._auth_headers()
        resp = requests.post(url, headers=headers, json=body, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def get_quantum_view_all_pages(self, subscription_name, begin_dt, end_dt):
        """
        Paginate through all QV results for a date range, following bookmarks.
        Yields each raw response dict.
        """
        bookmark = None
        while True:
            resp = self.get_quantum_view_events(
                subscription_name=subscription_name,
                begin_dt=begin_dt if not bookmark else None,
                end_dt=end_dt if not bookmark else None,
                bookmark=bookmark,
            )
            yield resp

            # Check if there are more pages
            try:
                bookmark = (
                    resp["QuantumViewResponse"]
                    ["QuantumViewEvents"]
                    .get("Bookmark")
                )
            except (KeyError, TypeError):
                bookmark = None

            if not bookmark:
                break
