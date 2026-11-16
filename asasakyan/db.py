"""SQLite storage. One row per vehicle, holding only its latest position.

Secrets (the owner key and the share code) are stored as SHA-256 hashes, so a
copy of the database file does not let anyone move or find a vehicle.
"""

import hashlib
import hmac
import secrets
import sqlite3
import time
import uuid

SCHEMA = """
CREATE TABLE IF NOT EXISTS vehicles (
    id          TEXT PRIMARY KEY,
    plate       TEXT NOT NULL UNIQUE,
    type        TEXT NOT NULL,
    visibility  TEXT NOT NULL DEFAULT 'code',
    owner_hash  TEXT NOT NULL,
    code_hash   TEXT NOT NULL,
    sharing     INTEGER NOT NULL DEFAULT 0,
    lat         REAL,
    lng         REAL,
    accuracy    REAL,
    located_at  REAL,
    created_at  REAL NOT NULL
);
"""

CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I/L
CODE_LENGTH = 6


class PlateTaken(Exception):
    """Another enrolment already uses this plate."""


def hash_secret(secret):
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def secret_matches(secret, stored_hash):
    if not isinstance(secret, str) or not secret:
        return False
    return hmac.compare_digest(hash_secret(secret), stored_hash)


def new_share_code():
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))


def new_owner_token():
    return secrets.token_urlsafe(24)


class Database:
    def __init__(self, path):
        self.path = path
        conn = self._connect()
        try:
            conn.executescript(SCHEMA)
        finally:
            conn.close()

    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    def _one(self, sql, params=()):
        conn = self._connect()
        try:
            row = conn.execute(sql, params).fetchone()
            return dict(row) if row is not None else None
        finally:
            conn.close()

    def _run(self, sql, params=()):
        conn = self._connect()
        try:
            return conn.execute(sql, params).rowcount
        finally:
            conn.close()

    # --- vehicles -----------------------------------------------------------

    def create_vehicle(self, plate, vtype, visibility, now=None,
                       vehicle_id=None, owner_token=None, share_code=None):
        """Insert a vehicle. Returns (row, owner_token, share_code)."""
        now = time.time() if now is None else now
        vehicle_id = vehicle_id or uuid.uuid4().hex
        owner_token = owner_token or new_owner_token()
        share_code = share_code or new_share_code()
        try:
            self._run(
                "INSERT INTO vehicles (id, plate, type, visibility, owner_hash, code_hash, sharing, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, 0, ?)",
                (vehicle_id, plate, vtype, visibility,
                 hash_secret(owner_token), hash_secret(share_code), now),
            )
        except sqlite3.IntegrityError as exc:
            raise PlateTaken(plate) from exc
        return self.get(vehicle_id), owner_token, share_code

    def get(self, vehicle_id):
        return self._one("SELECT * FROM vehicles WHERE id = ?", (vehicle_id,))

    def get_by_plate(self, plate):
        return self._one("SELECT * FROM vehicles WHERE plate = ?", (plate,))

    def owner_matches(self, vehicle, token):
        return secret_matches(token, vehicle["owner_hash"])

    def code_matches(self, vehicle, code):
        return secret_matches(code, vehicle["code_hash"])

    def set_location(self, vehicle_id, lat, lng, accuracy, now=None):
        now = time.time() if now is None else now
        self._run(
            "UPDATE vehicles SET lat = ?, lng = ?, accuracy = ?, located_at = ?, sharing = 1 WHERE id = ?",
            (lat, lng, accuracy, now, vehicle_id),
        )

    def set_sharing(self, vehicle_id, sharing):
        if sharing:
            self._run("UPDATE vehicles SET sharing = 1 WHERE id = ?", (vehicle_id,))
        else:
            self._run(
                "UPDATE vehicles SET sharing = 0, lat = NULL, lng = NULL, accuracy = NULL, located_at = NULL"
                " WHERE id = ?",
                (vehicle_id,),
            )

    def update(self, vehicle_id, vtype=None, visibility=None):
        if vtype is not None:
            self._run("UPDATE vehicles SET type = ? WHERE id = ?", (vtype, vehicle_id))
        if visibility is not None:
            self._run("UPDATE vehicles SET visibility = ? WHERE id = ?", (visibility, vehicle_id))

    def rotate_code(self, vehicle_id):
        code = new_share_code()
        self._run("UPDATE vehicles SET code_hash = ? WHERE id = ?", (hash_secret(code), vehicle_id))
        return code

    def delete(self, vehicle_id):
        return self._run("DELETE FROM vehicles WHERE id = ?", (vehicle_id,)) > 0
