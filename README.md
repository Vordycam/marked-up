# Marked Up

Find your own vehicle by its plate number. The answer is the
vehicle's location and its type, and nothing else.

Marked Up is deliberately not a registry lookup. It can only find a vehicle
that its owner enrolled, and only for people the owner gave a share code to.
The server keeps one position per vehicle and deletes it the moment sharing
stops. No accounts, no names, no history.

## How it works

1. **Enrol** the vehicle once, on the *My vehicles* tab: plate and type. You get
   an owner key (kept in that browser only) and a six-letter **share code**.
2. **Share**: open *My vehicles* on a phone that rides in the vehicle and tap
   **Share location**. While that page stays open, the phone posts its GPS
   position to your server. Use **Copy phone link** to move the vehicle's keys
   to that phone.
3. **Find**: on any other phone, open the app, type the plate and the share
   code, and see the latest position on a map with a Google Maps link. The
   result refreshes every 15 seconds. **Copy finder link** gives you a link
   that fills both in.

If you look up a plate that is not enrolled, the app offers to open a text to
the LTO service (`LTO VEHICLE <plate>` to 2600), which answers with the make,
model, year and colour on record and nothing about the owner.

## Run it

Python 3.9 or newer, nothing to install:

```
python server.py
```

Then open http://127.0.0.1:8080. The database lands in `data/`.

```
python -m unittest discover -s tests -t .     # 22 tests, about 2 seconds
python tools/make_icons.py                    # redraws the PNG icons
```

Options: `--host`, `--port`, `--db`, or the `HOST`, `PORT` and `MARKEDUP_DB`
environment variables.

## Put it on the internet

Phones only hand out their location to pages served over **HTTPS**, so the
server has to live somewhere with a certificate. The repo carries a
`Dockerfile` and a `render.yaml`; on [Render](https://render.com) choose
*New > Blueprint*, point it at this repo and you get a free HTTPS address in
a few minutes. Any host that runs a Docker image or `python server.py` works.

The free Render plan sleeps when idle and wipes its disk on every deploy. The
app copes: a phone that still holds a vehicle's keys silently re-enrols it the
next time it talks to the server. A paid disk or any host with persistent
storage avoids even that.

## What it will not do

* Find a vehicle nobody enrolled. There is no public source of live vehicle
  positions, and a crowd-sourced one would be a tool for following strangers.
* Keep a trail. Only the latest position is stored.
* Track in the background. A web page stops reading GPS when the phone's
  screen goes off or the browser is closed, so the sharing phone keeps its
  screen on (the app asks for a wake lock). For always-on tracking of a
  vehicle, a dedicated GPS tracker that reports to this server is the next
  step; the Python side is ready to grow a listener for one.
* Prove ownership. Whoever enrols a plate first holds it. Fine for your own
  vehicles on your own server; a public deployment would need a check against
  the OR/CR.

## Layout

```
server.py             entry point
markedup/plates.py   plate normalisation, the vehicle type list
markedup/db.py       SQLite: one row per vehicle, secrets stored hashed
markedup/api.py      the JSON API as plain functions (lookup, enrol, restore, ...)
markedup/http.py     standard-library HTTP server, static files, security headers
static/               the page (index.html, app.js, styles.css, manifest, icons)
tests/                unittest suite that drives a real server on a random port
tools/make_icons.py   draws the PNG icons with the standard library
```

### API

| Method | Path | Who | Purpose |
| --- | --- | --- | --- |
| GET | `/api/types` | anyone | vehicle type list |
| GET | `/api/lookup?plate=&code=` | anyone | find a vehicle (404 unless enrolled and code matches, or public) |
| POST | `/api/vehicles` | anyone | enrol; returns `owner_token` and `share_code` once |
| POST | `/api/vehicles/restore` | key holder | re-create a vehicle from the keys a phone holds |
| GET | `/api/vehicles/{id}` | owner | the vehicle as its owner sees it |
| PATCH | `/api/vehicles/{id}` | owner | change `type` or `visibility` (`code` or `public`) |
| DELETE | `/api/vehicles/{id}` | owner | remove it |
| POST | `/api/vehicles/{id}/location` | owner | `{lat, lng, accuracy}`; also turns sharing on |
| POST | `/api/vehicles/{id}/sharing` | owner | `{sharing: false}` deletes the position |
| POST | `/api/vehicles/{id}/code` | owner | new share code |

Owner calls carry `Authorization: Bearer <owner_token>`. Lookups and
enrolments are rate limited per address. Logs never contain addresses or
query strings.
