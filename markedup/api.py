"""The JSON API as plain functions over a Database, independent of HTTP plumbing.

Every handler takes a Request and returns (status, payload). Validation errors
raise ApiError, which handle() turns into a JSON error body.

Privacy rules enforced here:
  * A lookup only succeeds for a vehicle its owner enrolled, and only with the
    owner's share code unless the owner made the vehicle public.
  * A code-protected vehicle looked up without the right code answers exactly
    like a plate that was never enrolled, so the lookup endpoint cannot be used
    to discover which plates exist.
  * Only the latest position is ever stored; stopping sharing deletes it.
"""

import math
import re
import threading
import time
from collections import deque, namedtuple

from .db import PlateTaken, secret_matches
from .plates import TYPE_LABELS, VEHICLE_TYPES, display_plate, normalize_plate

Request = namedtuple("Request", "method path query body token client")

VISIBILITIES = ("code", "public")
STALE_AFTER = 10 * 60          # seconds without an update before a position is flagged stale
ID_RE = re.compile(r"^[0-9a-f]{32}$")
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
CODE_RE = re.compile(r"^[A-Z0-9]{4,12}$")


class ApiError(Exception):
    def __init__(self, status, error, message):
        super().__init__(message)
        self.status = status
        self.error = error
        self.message = message


class RateLimiter:
    """Sliding-window counter: at most `limit` hits per `window` seconds per key."""

    def __init__(self, limit, window, clock=time.time):
        self.limit = limit
        self.window = window
        self.clock = clock
        self.hits = {}
        self.lock = threading.Lock()

    def allow(self, key):
        now = self.clock()
        with self.lock:
            queue = self.hits.setdefault(key, deque())
            while queue and queue[0] <= now - self.window:
                queue.popleft()
            if len(queue) >= self.limit:
                return False
            queue.append(now)
            if len(self.hits) > 10000:
                self._prune(now)
            return True

    def _prune(self, now):
        stale = [k for k, q in self.hits.items() if not q or q[-1] <= now - self.window]
        for key in stale:
            del self.hits[key]


class Api:
    def __init__(self, db, clock=time.time,
                 lookup_limit=(60, 60), enrol_limit=(10, 3600), update_limit=(300, 60)):
        self.db = db
        self.clock = clock
        self.lookups = RateLimiter(*lookup_limit, clock=clock)
        self.enrols = RateLimiter(*enrol_limit, clock=clock)
        self.updates = RateLimiter(*update_limit, clock=clock)

    # --- routing ------------------------------------------------------------

    def handle(self, req):
        try:
            return self._route(req)
        except ApiError as err:
            return err.status, {"error": err.error, "message": err.message}

    def _route(self, req):
        parts = [p for p in req.path.split("/") if p]
        if parts[:1] != ["api"]:
            raise _no_endpoint()
        rest = parts[1:]
        method = req.method

        if rest == ["types"] and method == "GET":
            return 200, {"types": [{"id": tid, "label": label} for tid, label in VEHICLE_TYPES]}
        if rest == ["lookup"] and method == "GET":
            return self.lookup(req)
        if rest == ["vehicles"] and method == "POST":
            return self.enrol(req)
        if rest == ["vehicles", "restore"] and method == "POST":
            return self.restore(req)

        if len(rest) in (2, 3) and rest[0] == "vehicles" and ID_RE.match(rest[1]):
            vehicle = self._owner(rest[1], req.token)
            action = rest[2] if len(rest) == 3 else None
            if action is None:
                if method == "GET":
                    return 200, self._owner_view(vehicle)
                if method == "PATCH":
                    return self.update(vehicle, req)
                if method == "DELETE":
                    self.db.delete(vehicle["id"])
                    return 200, {"deleted": True}
            elif method == "POST":
                if action == "location":
                    return self.set_location(vehicle, req)
                if action == "sharing":
                    return self.set_sharing(vehicle, req)
                if action == "code":
                    return 200, {"share_code": self.db.rotate_code(vehicle["id"])}
        raise _no_endpoint()

    # --- handlers -----------------------------------------------------------

    def lookup(self, req):
        plate = self._plate(req.query.get("plate"))
        if not self.lookups.allow(req.client):
            raise ApiError(429, "rate_limited", "Too many lookups. Wait a minute and try again.")
        vehicle = self.db.get_by_plate(plate)
        if vehicle is None:
            raise _not_found(plate)
        if vehicle["visibility"] != "public":
            code = (req.query.get("code") or "").strip().upper()
            if not self.db.code_matches(vehicle, code):
                raise _not_found(plate)
        return 200, self._public_view(vehicle)

    def enrol(self, req):
        if not self.enrols.allow(req.client):
            raise ApiError(429, "rate_limited", "Too many enrolments from this address. Try again later.")
        plate = self._plate(req.body.get("plate"))
        vtype = self._type(req.body.get("type"))
        visibility = self._visibility(req.body.get("visibility", "code"))
        try:
            vehicle, token, code = self.db.create_vehicle(plate, vtype, visibility, now=self.clock())
        except PlateTaken:
            raise _plate_taken()
        view = self._owner_view(vehicle)
        view["owner_token"] = token
        view["share_code"] = code
        return 201, view

    def restore(self, req):
        """Re-create a vehicle from the keys a phone already holds (after the
        server's database was reset, e.g. a free-tier redeploy)."""
        if not self.enrols.allow(req.client):
            raise ApiError(429, "rate_limited", "Too many enrolments from this address. Try again later.")
        body = req.body
        vehicle_id = body.get("id")
        token = body.get("owner_token")
        code = body.get("share_code")
        if not (isinstance(vehicle_id, str) and ID_RE.match(vehicle_id)):
            raise ApiError(400, "invalid_id", "The vehicle id is not valid.")
        if not (isinstance(token, str) and TOKEN_RE.match(token)):
            raise ApiError(400, "invalid_token", "The owner key is not valid.")
        if not (isinstance(code, str) and CODE_RE.match(code)):
            raise ApiError(400, "invalid_code", "The share code is not valid.")
        existing = self.db.get(vehicle_id)
        if existing is not None:
            if not secret_matches(token, existing["owner_hash"]):
                raise _unauthorized()
            return 200, self._owner_view(existing)
        plate = self._plate(body.get("plate"))
        vtype = self._type(body.get("type"))
        visibility = self._visibility(body.get("visibility", "code"))
        try:
            vehicle, _, _ = self.db.create_vehicle(
                plate, vtype, visibility, now=self.clock(),
                vehicle_id=vehicle_id, owner_token=token, share_code=code,
            )
        except PlateTaken:
            raise _plate_taken()
        return 201, self._owner_view(vehicle)

    def set_location(self, vehicle, req):
        if not self.updates.allow(vehicle["id"]):
            raise ApiError(429, "rate_limited", "Positions are arriving too fast.")
        lat = self._number(req.body.get("lat"), "lat", -90, 90)
        lng = self._number(req.body.get("lng"), "lng", -180, 180)
        accuracy = req.body.get("accuracy")
        if accuracy is not None:
            accuracy = self._number(accuracy, "accuracy", 0, 100000)
        now = self.clock()
        self.db.set_location(vehicle["id"], lat, lng, accuracy, now)
        return 200, {"sharing": True, "location": self._location(lat, lng, accuracy, now)}

    def set_sharing(self, vehicle, req):
        sharing = req.body.get("sharing")
        if not isinstance(sharing, bool):
            raise ApiError(400, "invalid_sharing", "sharing must be true or false.")
        self.db.set_sharing(vehicle["id"], sharing)
        return 200, self._owner_view(self.db.get(vehicle["id"]))

    def update(self, vehicle, req):
        vtype = req.body.get("type")
        visibility = req.body.get("visibility")
        if vtype is not None:
            vtype = self._type(vtype)
        if visibility is not None:
            visibility = self._visibility(visibility)
        self.db.update(vehicle["id"], vtype=vtype, visibility=visibility)
        return 200, self._owner_view(self.db.get(vehicle["id"]))

    # --- helpers ------------------------------------------------------------

    def _owner(self, vehicle_id, token):
        vehicle = self.db.get(vehicle_id)
        if vehicle is None:
            raise ApiError(404, "unknown_vehicle", "This vehicle is not enrolled on this server.")
        if not self.db.owner_matches(vehicle, token):
            raise _unauthorized()
        return vehicle

    @staticmethod
    def _plate(raw):
        plate = normalize_plate(raw)
        if plate is None:
            raise ApiError(400, "invalid_plate", "Enter the plate as letters and digits, 2 to 10 characters.")
        return plate

    @staticmethod
    def _type(raw):
        if raw not in TYPE_LABELS:
            raise ApiError(400, "invalid_type", "Pick a vehicle type from the list.")
        return raw

    @staticmethod
    def _visibility(raw):
        if raw not in VISIBILITIES:
            raise ApiError(400, "invalid_visibility", "Visibility must be 'code' or 'public'.")
        return raw

    @staticmethod
    def _number(value, name, low, high):
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or not low <= value <= high):
            raise ApiError(400, "invalid_" + name, "%s must be a number between %s and %s." % (name, low, high))
        return float(value)

    def _location(self, lat, lng, accuracy, at):
        if lat is None or lng is None or at is None:
            return None
        age = max(0, int(self.clock() - at))
        return {"lat": lat, "lng": lng, "accuracy": accuracy, "at": at,
                "age_seconds": age, "stale": age > STALE_AFTER}

    def _public_view(self, vehicle):
        sharing = bool(vehicle["sharing"])
        location = None
        if sharing:
            location = self._location(vehicle["lat"], vehicle["lng"], vehicle["accuracy"], vehicle["located_at"])
        return {
            "found": True,
            "plate": vehicle["plate"],
            "display": display_plate(vehicle["plate"]),
            "type": vehicle["type"],
            "type_label": TYPE_LABELS.get(vehicle["type"], vehicle["type"]),
            "visibility": vehicle["visibility"],
            "sharing": sharing,
            "location": location,
        }

    def _owner_view(self, vehicle):
        view = self._public_view(vehicle)
        del view["found"]
        view["id"] = vehicle["id"]
        view["created_at"] = vehicle["created_at"]
        return view


def _no_endpoint():
    return ApiError(404, "no_endpoint", "No such endpoint.")


def _not_found(plate):
    return ApiError(404, "not_found",
                    "No vehicle with plate %s is enrolled here, or the share code is wrong." % display_plate(plate))


def _plate_taken():
    return ApiError(409, "plate_taken",
                    "That plate is already enrolled here. Only the phone holding its owner key can change it.")


def _unauthorized():
    return ApiError(401, "unauthorized", "Wrong or missing owner key.")
