import datetime, time, os
from collections import deque, defaultdict

from flask import Flask, request, jsonify
from geopy.geocoders import Nominatim
import pytz
import swisseph as swe  # pyswisseph

app = Flask(__name__)

# ------------ config ------------
ASTRO_KEY = os.getenv("ASTRO_SERVICE_KEY")  # set in your host env
RATE_MAX = 30            # requests per window
RATE_WINDOW = 60         # seconds
# swe.set_ephe_path("/app/ephe")  # uncomment if you upload Swiss ephemeris files

# ------------ rate limit & auth ------------
_rate = defaultdict(lambda: deque())
def check_rate(ip):
    q = _rate[ip]
    now = time.time()
    while q and now - q[0] > RATE_WINDOW:
        q.popleft()
    if len(q) >= RATE_MAX:
        return False
    q.append(now)
    return True

def require_key():
    if not ASTRO_KEY:
        return True   # if you didn’t set a key, don’t block
    return request.headers.get("X-Astro-Key") == ASTRO_KEY

# ------------ helpers ------------
SIGNS = ["Aries","Taurus","Gemini","Cancer","Leo","Virgo",
         "Libra","Scorpio","Sagittarius","Capricorn","Aquarius","Pisces"]
PLANETS = [
    ("Sun", swe.SUN), ("Moon", swe.MOON), ("Mercury", swe.MERCURY),
    ("Venus", swe.VENUS), ("Mars", swe.MARS), ("Jupiter", swe.JUPITER),
    ("Saturn", swe.SATURN), ("Uranus", swe.URANUS), ("Neptune", swe.NEPTUNE),
    ("Pluto", swe.PLUTO)
]
ASPECTS = [(0,"conjunction"),(60,"sextile"),(90,"square"),(120,"trine"),(180,"opposition")]
NATAL_ORB = 6.0
TRANSIT_ORB = 3.0

def norm360(d):
    d = d % 360.0
    return d + 360.0 if d < 0 else d

def sign_name(lon):
    s = int(lon // 30) % 12
    return SIGNS[s], lon % 30

def aspect_between(a, b, orb):
    d = abs(a - b) % 360.0
    if d > 180:
        d = 360 - d
    for ang, name in ASPECTS:
        diff = abs(d - ang)
        if diff <= orb:
            return name, round(diff, 2)
    return None, None

def geocode(city, country, user_agent_email="contact@lunatwine.com"):
    """Optional: try to resolve lat/lon if not provided; OK to remove later."""
    geocoder = Nominatim(user_agent=f"lunatwine/1.0 ({user_agent_email})", timeout=10)
    q = f"{city}, {country}" if country else city
    loc = geocoder.geocode(q)
    if not loc:
        return None
    return float(loc.latitude), float(loc.longitude)

def local_to_ut(date_str, time_str, tz_name):
    y, m, d = map(int, date_str.split("-"))
    hh, mm = map(int, time_str.split(":"))
    tz = pytz.timezone(tz_name)
    local_dt = tz.localize(datetime.datetime(y, m, d, hh, mm))
    ut_dt = local_dt.astimezone(pytz.utc)
    ut_hour = ut_dt.hour + ut_dt.minute/60.0 + ut_dt.second/3600.0
    jd_ut = swe.julday(ut_dt.year, ut_dt.month, ut_dt.day, ut_hour)
    return jd_ut, ut_dt

def houses_placidus(jd_ut, lat, lon):
    cusps, ascmc = swe.houses(jd_ut, lat, lon, b'P')
    return list(cusps)[:12], ascmc[0], ascmc[1]  # cusps, ASC, MC

def planet_positions(jd_ut, orb=NATAL_ORB):
    iflag = swe.FLG_SWIEPH | swe.FLG_SPEED
    out = []
    for name, pid in PLANETS:
        lon, latp, dist, speed = swe.calc_ut(jd_ut, pid, iflag)[0:4]
        sname, deg = sign_name(lon)
        out.append({
            "name": name,
            "lon": round(norm360(lon), 6),
            "sign": sname,
            "deg_in_sign": round(deg, 2),
            "speed": speed
        })
    return out

def natal_payload(p, require_geo=True):
    date = p.get("date")
    time_s = p.get("time")
    city = (p.get("city") or "").strip()
    country = (p.get("country") or "").strip()
    tz_name = (p.get("timezone") or "").strip()

    if not (date and time_s and tz_name):
        return None, {"error": "date, time, and timezone are required"}

    lat = p.get("lat")
    lon = p.get("lon")
    if (lat is None or lon is None) and require_geo:
        gc = geocode(city, country)
        if not gc:
            return None, {"error": "Could not geocode city/country"}
        lat, lon = gc

    try:
        jd_ut, ut_dt = local_to_ut(date, time_s, tz_name)
    except Exception as e:
        return None, {"error": f"Time conversion failed: {e}"}

    return {"lat": lat, "lon": lon, "tz": tz_name, "jd_ut": jd_ut, "ut_dt": ut_dt}, None

# ------------ endpoints ------------
@app.get("/health")
def health():
    return {"ok": True}

@app.post("/natal")
def natal():
    ip = request.remote_addr or "anon"
    if not check_rate(ip):
        return jsonify({"error": "rate limit"}), 429
    if not require_key():
        return jsonify({"error": "unauthorized"}), 401
    try:
        p = request.get_json(force=True)
    except Exception:
        return jsonify({"error": "Invalid JSON"}), 400

    basis, err = natal_payload(p)
    if err:
        return jsonify(err), 400

    lat, lon = basis["lat"], basis["lon"]
    tz_name, jd_ut, ut_dt = basis["tz"], basis["jd_ut"], basis["ut_dt"]

    cusps, ascv, mcv = houses_placidus(jd_ut, lat, lon)
    planets = planet_positions(jd_ut, NATAL_ORB)

    # assign houses
    def house_for(L, cusps):
        L = norm360(L)
        c = [norm360(x) for x in cusps]
        c2 = c + [c[0] + 360]
        for i in range(12):
            a, b = c2[i], c2[i+1]
            if a <= L < b or (b < a and (L >= a or L < (b % 360))):
                return i + 1
        return 12

    for pl in planets:
        pl["house"] = house_for(pl["lon"], cusps)

    # natal aspects
    aspects = []
    for i in range(len(planets)):
        for j in range(i + 1, len(planets)):
            typ, orb = aspect_between(planets[i]["lon"], planets[j]["lon"], NATAL_ORB)
            if typ:
                aspects.append({"a": planets[i]["name"], "b": planets[j]["name"], "type": typ, "orb": orb})

    houses_out = []
    for i, cusp in enumerate(cusps, start=1):
        sname, deg = sign_name(cusp)
        houses_out.append({
            "num": i,
            "lon": round(norm360(cusp), 6),
            "sign": sname,
            "deg_in_sign": round(deg, 2)
        })

    return jsonify({
        "resolved": {
            "lat": lat, "lon": lon, "timezone": tz_name,
            "ut": ut_dt.strftime("%Y-%m-%d %H:%M"), "jd_ut": jd_ut
        },
        "ascendant": round(norm360(ascv), 6),
        "mc": round(norm360(mcv), 6),
        "planets": planets,
        "houses": houses_out,
        "aspects": aspects
    })

@app.post("/transits")
def transits():
    ip = request.remote_addr or "anon"
    if not check_rate(ip):
        return jsonify({"error": "rate limit"}), 429
    if not require_key():
        return jsonify({"error": "unauthorized"}), 401
    try:
        p = request.get_json(force=True)
    except Exception:
        return jsonify({"error": "Invalid JSON"}), 400

    # natal base
    basis, err = natal_payload(p, require_geo=True)
    if err:
        return jsonify(err), 400
    tz_name = basis["tz"]

    # transit time
    if p.get("now", False):
        tz = pytz.timezone(tz_name)
        now_local = datetime.datetime.now(tz)
        date = now_local.strftime("%Y-%m-%d")
        time_s = now_local.strftime("%H:%M")
    else:
        date = p.get("t_date") or p.get("date")
        time_s = p.get("t_time") or p.get("time")
        tz_name = p.get("t_timezone") or tz_name
        if not (date and time_s and tz_name):
            return jsonify({"error": "t_date/time & t_timezone (or now:true) required"}), 400

    jd_ut, ut_dt = local_to_ut(date, time_s, tz_name)

    # natal longitudes
    n_jd, _ = local_to_ut(p["date"], p["time"], p["timezone"])
    natal_planets = planet_positions(n_jd, NATAL_ORB)

    # transiting positions
    t_planets = planet_positions(jd_ut, TRANSIT_ORB)

    # aspects: transiting to natal
    hits = []
    for t in t_planets:
        for n in natal_planets:
            typ, orb = aspect_between(t["lon"], n["lon"], TRANSIT_ORB)
            if typ:
                # rough days-to-exact estimate (very approximate)
                rel_speed = abs(t["speed"]) or 1e-6
                delta = abs(((t["lon"] - n["lon"]) + 540) % 360 - 180)
                approx_days = round(delta / (rel_speed * 24.0), 1)
                hits.append({
                    "transiting": t["name"],
                    "natal": n["name"],
                    "type": typ,
                    "orb": orb,
                    "approx_days_to_exact": approx_days
                })

    return jsonify({
        "resolved": {"timezone": tz_name, "ut": ut_dt.strftime("%Y-%m-%d %H:%M"), "jd_ut": jd_ut},
        "transiting_planets": t_planets,
        "natal_planets": natal_planets,
        "hits": hits
    })
