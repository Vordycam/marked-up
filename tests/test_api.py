"""End-to-end tests: a real ThreadingHTTPServer on a random port, urllib as the client."""

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from asasakyan.api import Api, RateLimiter, Request
from asasakyan.db import Database
from asasakyan.http import create_server


class FakeClock:
    def __init__(self, start=1_700_000_000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class ServerTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.clock = FakeClock()
        cls.db = Database(os.path.join(cls.tmp.name, "test.sqlite3"))
        cls.api = Api(cls.db, clock=cls.clock, lookup_limit=(10000, 60), enrol_limit=(10000, 3600))
        cls.server = create_server("127.0.0.1", 0, cls.api)
        cls.server.RequestHandlerClass.log_request = lambda *args, **kwargs: None
        cls.server.RequestHandlerClass.log_message = lambda *args, **kwargs: None
        cls.base = "http://127.0.0.1:%d" % cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def call(self, method, path, body=None, token=None, raw=None, headers=None):
        data = raw if raw is not None else (json.dumps(body).encode("utf-8") if body is not None else None)
        req = urllib.request.Request(self.base + path, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if token:
            req.add_header("Authorization", "Bearer " + token)
        for name, value in (headers or {}).items():
            req.add_header(name, value)
        try:
            with urllib.request.urlopen(req, timeout=5) as res:
                return res.status, json.loads(res.read() or b"{}"), res.headers
        except urllib.error.HTTPError as err:
            payload = err.read()
            try:
                payload = json.loads(payload or b"{}")
            except ValueError:
                payload = {"raw": payload}
            return err.code, payload, err.headers

    def enrol(self, plate, vtype="pickup", visibility="code"):
        status, data, _ = self.call("POST", "/api/vehicles", {"plate": plate, "type": vtype, "visibility": visibility})
        self.assertEqual(status, 201, data)
        return data


class EnrolAndFindTest(ServerTestCase):
    def test_types_come_from_the_server_with_pickup_first(self):
        status, data, _ = self.call("GET", "/api/types")
        self.assertEqual(status, 200)
        self.assertEqual(data["types"][0], {"id": "pickup", "label": "Pickup"})

    def test_full_life_of_a_vehicle(self):
        v = self.enrol("nbc 1234")
        self.assertEqual(v["plate"], "NBC1234")
        self.assertEqual(v["display"], "NBC 1234")
        self.assertEqual(v["type_label"], "Pickup")
        self.assertFalse(v["sharing"])
        self.assertIsNone(v["location"])
        self.assertRegex(v["owner_token"], r"^[A-Za-z0-9_-]{16,}$")
        self.assertRegex(v["share_code"], r"^[A-Z2-9]{6}$")
        token, code = v["owner_token"], v["share_code"]

        # Without the code the plate looks unenrolled.
        status, data, _ = self.call("GET", "/api/lookup?plate=NBC-1234")
        self.assertEqual(status, 404)
        self.assertEqual(data["error"], "not_found")
        status, data, _ = self.call("GET", "/api/lookup?plate=NBC1234&code=WRONG1")
        self.assertEqual(status, 404)

        # With the code: found, but no position yet.
        status, data, _ = self.call("GET", "/api/lookup?plate=nbc1234&code=" + code.lower())
        self.assertEqual(status, 200, data)
        self.assertTrue(data["found"])
        self.assertFalse(data["sharing"])
        self.assertIsNone(data["location"])
        self.assertNotIn("id", data)
        self.assertNotIn("owner_token", data)

        # The phone in the truck posts a position.
        status, data, _ = self.call("POST", "/api/vehicles/%s/location" % v["id"],
                                    {"lat": 8.4542, "lng": 124.6319, "accuracy": 12.5}, token=token)
        self.assertEqual(status, 200, data)
        self.assertTrue(data["sharing"])
        self.assertEqual(data["location"]["lat"], 8.4542)

        status, data, _ = self.call("GET", "/api/lookup?plate=NBC1234&code=" + code)
        self.assertEqual(status, 200)
        self.assertTrue(data["sharing"])
        loc = data["location"]
        self.assertEqual((loc["lat"], loc["lng"], loc["accuracy"]), (8.4542, 124.6319, 12.5))
        self.assertEqual(loc["age_seconds"], 0)
        self.assertFalse(loc["stale"])

        # Time passes: the position goes stale but is still reported.
        self.clock.advance(11 * 60)
        status, data, _ = self.call("GET", "/api/lookup?plate=NBC1234&code=" + code)
        self.assertEqual(data["location"]["age_seconds"], 660)
        self.assertTrue(data["location"]["stale"])

        # Stopping sharing deletes the position on the server.
        status, data, _ = self.call("POST", "/api/vehicles/%s/sharing" % v["id"], {"sharing": False}, token=token)
        self.assertEqual(status, 200, data)
        self.assertFalse(data["sharing"])
        self.assertIsNone(data["location"])
        self.assertIsNone(self.db.get(v["id"])["lat"])
        status, data, _ = self.call("GET", "/api/lookup?plate=NBC1234&code=" + code)
        self.assertEqual(status, 200)
        self.assertFalse(data["sharing"])
        self.assertIsNone(data["location"])

        # Rotating the code invalidates the old one.
        status, data, _ = self.call("POST", "/api/vehicles/%s/code" % v["id"], {}, token=token)
        self.assertEqual(status, 200)
        new_code = data["share_code"]
        self.assertNotEqual(new_code, code)
        self.assertEqual(self.call("GET", "/api/lookup?plate=NBC1234&code=" + code)[0], 404)
        self.assertEqual(self.call("GET", "/api/lookup?plate=NBC1234&code=" + new_code)[0], 200)

        # Deleting removes everything.
        status, data, _ = self.call("DELETE", "/api/vehicles/%s" % v["id"], token=token)
        self.assertEqual(status, 200)
        self.assertEqual(self.call("GET", "/api/lookup?plate=NBC1234&code=" + new_code)[0], 404)
        self.assertEqual(self.call("GET", "/api/vehicles/%s" % v["id"], token=token)[0], 404)

    def test_public_vehicle_needs_no_code(self):
        v = self.enrol("JPN 777", vtype="jeepney", visibility="public")
        status, data, _ = self.call("GET", "/api/lookup?plate=JPN777")
        self.assertEqual(status, 200, data)
        self.assertEqual(data["type_label"], "Jeepney")
        self.assertEqual(data["visibility"], "public")

        # Switching back to code-protected hides it again.
        status, data, _ = self.call("PATCH", "/api/vehicles/%s" % v["id"], {"visibility": "code", "type": "bus"},
                                    token=v["owner_token"])
        self.assertEqual(status, 200, data)
        self.assertEqual((data["visibility"], data["type"]), ("code", "bus"))
        self.assertEqual(self.call("GET", "/api/lookup?plate=JPN777")[0], 404)

    def test_same_plate_cannot_be_enrolled_twice(self):
        self.enrol("DUP 100")
        status, data, _ = self.call("POST", "/api/vehicles", {"plate": "dup-100", "type": "car"})
        self.assertEqual(status, 409)
        self.assertEqual(data["error"], "plate_taken")

    def test_wrong_or_missing_owner_key(self):
        v = self.enrol("KEY 001")
        for token in (None, "nope", v["owner_token"] + "x"):
            status, data, _ = self.call("POST", "/api/vehicles/%s/location" % v["id"],
                                        {"lat": 1, "lng": 2}, token=token)
            self.assertEqual(status, 401, token)
        status, _, _ = self.call("GET", "/api/vehicles/%s" % ("0" * 32), token=v["owner_token"])
        self.assertEqual(status, 404)

    def test_restore_recreates_a_vehicle_after_the_server_forgot_it(self):
        v = self.enrol("RST 555", vtype="pickup")
        keys = {"id": v["id"], "plate": v["plate"], "type": v["type"], "visibility": v["visibility"],
                "owner_token": v["owner_token"], "share_code": v["share_code"]}

        # Restore while it still exists: a no-op that checks the key.
        status, data, _ = self.call("POST", "/api/vehicles/restore", keys)
        self.assertEqual(status, 200, data)
        bad = dict(keys, owner_token="A" * 32)
        self.assertEqual(self.call("POST", "/api/vehicles/restore", bad)[0], 401)

        # Simulate a free-tier disk reset.
        self.db.delete(v["id"])
        self.assertEqual(self.call("GET", "/api/vehicles/%s" % v["id"], token=v["owner_token"])[0], 404)

        status, data, _ = self.call("POST", "/api/vehicles/restore", keys)
        self.assertEqual(status, 201, data)
        self.assertEqual(data["id"], v["id"])
        status, data, _ = self.call("POST", "/api/vehicles/%s/location" % v["id"],
                                    {"lat": 8.48, "lng": 124.65}, token=v["owner_token"])
        self.assertEqual(status, 200, data)
        status, data, _ = self.call("GET", "/api/lookup?plate=RST555&code=" + v["share_code"])
        self.assertEqual(status, 200, data)
        self.assertEqual(data["location"]["lng"], 124.65)

    def test_restore_rejects_malformed_keys(self):
        for bad in ({"id": "zzz"}, {"id": "a" * 32, "owner_token": "short"},
                    {"id": "a" * 32, "owner_token": "B" * 24, "share_code": "x"}):
            status, data, _ = self.call("POST", "/api/vehicles/restore", bad)
            self.assertEqual(status, 400, data)


class ValidationTest(ServerTestCase):
    def test_bad_enrolments(self):
        cases = [
            ({"plate": "!", "type": "car"}, "invalid_plate"),
            ({"plate": "ABC123", "type": "spaceship"}, "invalid_type"),
            ({"plate": "ABC123", "type": "car", "visibility": "friends"}, "invalid_visibility"),
        ]
        for body, error in cases:
            status, data, _ = self.call("POST", "/api/vehicles", body)
            self.assertEqual((status, data["error"]), (400, error), body)

    def test_bad_positions(self):
        v = self.enrol("POS 001")
        token = v["owner_token"]
        for body in ({"lat": 91, "lng": 0}, {"lat": 0, "lng": -181}, {"lat": "8", "lng": 1},
                     {"lat": True, "lng": 1}, {"lng": 1}, {"lat": 1, "lng": 1, "accuracy": -1}):
            status, data, _ = self.call("POST", "/api/vehicles/%s/location" % v["id"], body, token=token)
            self.assertEqual(status, 400, body)
        status, data, _ = self.call("POST", "/api/vehicles/%s/location" % v["id"],
                                    raw=b'{"lat": NaN, "lng": 1}', token=token)
        self.assertEqual(status, 400)
        status, data, _ = self.call("POST", "/api/vehicles/%s/sharing" % v["id"], {"sharing": "yes"}, token=token)
        self.assertEqual(status, 400)

    def test_bad_bodies(self):
        status, data, _ = self.call("POST", "/api/vehicles", raw=b"[1, 2]")
        self.assertEqual((status, data["error"]), (400, "bad_json"))
        status, data, _ = self.call("POST", "/api/vehicles", raw=b"{not json")
        self.assertEqual(status, 400)
        status, data, _ = self.call("POST", "/api/vehicles", raw=b"x" * (64 * 1024 + 1))
        self.assertEqual(status, 413)

    def test_lookup_needs_a_plate(self):
        status, data, _ = self.call("GET", "/api/lookup")
        self.assertEqual((status, data["error"]), (400, "invalid_plate"))

    def test_unknown_endpoints(self):
        self.assertEqual(self.call("GET", "/api/nothing")[0], 404)
        self.assertEqual(self.call("PUT", "/api/vehicles", {})[0], 404)
        self.assertEqual(self.call("POST", "/index.html", {})[0], 405)


class StaticTest(ServerTestCase):
    def fetch(self, path):
        req = urllib.request.Request(self.base + path)
        try:
            with urllib.request.urlopen(req, timeout=5) as res:
                return res.status, res.headers, res.read()
        except urllib.error.HTTPError as err:
            return err.code, err.headers, err.read()

    def test_index_and_assets_are_served_with_security_headers(self):
        status, headers, body = self.fetch("/")
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("text/html"))
        self.assertIn(b"AsaSakyan", body)
        self.assertIn("frame-src https://www.openstreetmap.org", headers["Content-Security-Policy"])
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(headers["Referrer-Policy"], "no-referrer")

        status, headers, _ = self.fetch("/app.js")
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("text/javascript"))
        status, headers, _ = self.fetch("/manifest.webmanifest")
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("application/manifest+json"))
        status, headers, _ = self.fetch("/icons/icon.svg")
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("image/svg+xml"))

    def test_no_path_escapes_the_static_folder(self):
        for path in ("/../server.py", "/..%2Fserver.py", "/icons/../../server.py", "/%5C..%5Cserver.py", "/nope.txt"):
            status, _, body = self.fetch(path)
            self.assertEqual(status, 404, path)
            self.assertNotIn(b"argparse", body)


class RateLimiterTest(unittest.TestCase):
    def test_sliding_window(self):
        clock = FakeClock()
        limiter = RateLimiter(2, 60, clock=clock)
        self.assertTrue(limiter.allow("a"))
        self.assertTrue(limiter.allow("a"))
        self.assertFalse(limiter.allow("a"))
        self.assertTrue(limiter.allow("b"))
        clock.advance(61)
        self.assertTrue(limiter.allow("a"))

    def test_lookups_are_limited_per_client(self):
        with tempfile.TemporaryDirectory() as folder:
            api = Api(Database(os.path.join(folder, "r.sqlite3")), lookup_limit=(2, 60))
            req = Request("GET", "/api/lookup", {"plate": "ABC1234"}, {}, None, "1.2.3.4")
            self.assertEqual(api.handle(req)[0], 404)
            self.assertEqual(api.handle(req)[0], 404)
            self.assertEqual(api.handle(req)[0], 429)
            other = req._replace(client="5.6.7.8")
            self.assertEqual(api.handle(other)[0], 404)


if __name__ == "__main__":
    unittest.main()
