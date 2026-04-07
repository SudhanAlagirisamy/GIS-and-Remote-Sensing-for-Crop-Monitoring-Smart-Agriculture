from flask import Flask, render_template, request, redirect, url_for, session, jsonify
from sentinelhub import (
    SHConfig, BBox, CRS, MimeType,
    SentinelHubRequest, DataCollection, bbox_to_dimensions
)
import matplotlib.pyplot as plt
import numpy as np
import os
from werkzeug.security import generate_password_hash, check_password_hash
from geopy.geocoders import Nominatim

import socket
import threading
import sqlite3

app = Flask(__name__)
app.secret_key = "supersecretkey"

# ======================================================
# 🗄️ DATABASE SETUP
# ======================================================
def init_db():
    conn = sqlite3.connect("users.db")
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE,
            password TEXT
        )
    """)
    conn.commit()
    conn.close()

init_db()

# ======================================================
# 🌍 GPS STORAGE
# ======================================================
latest_gps = {"lat": None, "lon": None}

# ======================================================
# 📡 GPS LISTENER
# ======================================================
def gps_listener():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", 4210))

    while True:
        data, addr = sock.recvfrom(1024)
        msg = data.decode().strip()

        if "LAT" in msg:
            try:
                parts = msg.split(",")
                lat = float(parts[0].split(":")[1])
                lon = float(parts[1].split(":")[1])
                latest_gps["lat"] = lat
                latest_gps["lon"] = lon
            except:
                print("GPS Parse Error")

# ======================================================
# 📍 GET COORDINATES
# ======================================================
def get_coordinates(place):
    geo = Nominatim(user_agent="geo_app")
    loc = geo.geocode(place)

    if loc:
        lat, lon = loc.latitude, loc.longitude
        return lat, lon   # ✅ ONLY POINT
    else:
        raise Exception("Location not found")

# ======================================================
# 🌱 CROP ANALYSIS
# ======================================================
def analyze_crop(ndvi_array):
    if ndvi_array is None or len(ndvi_array) == 0:
        return {"avg_ndvi": 0, "health": "No Data", "crop": "Unknown", "water": "Unknown", "irrigation": "Unknown"}

    # Now ndvi_array is already -1 to 1 from evalscript
    valid_ndvi = ndvi_array[(ndvi_array >= -1) & (ndvi_array <= 1)] # Remove invalid values
    if len(valid_ndvi) == 0:
        return {"avg_ndvi": 0, "health": "No Valid Data", "crop": "Unknown", "water": "Unknown", "irrigation": "Unknown"}
    
    avg_ndvi = float(np.mean(valid_ndvi))
    
    print(f"NDVI Stats - Min: {np.min(valid_ndvi):.3f}, Max: {np.max(valid_ndvi):.3f}, Mean: {avg_ndvi:.3f}")
    
    # Proper thresholds for REAL NDVI (-1 to 1)
    if avg_ndvi < 0.1:
        health = "🪨 Bare Soil / Desert"
    elif avg_ndvi < 0.3:
        health = "🌱 Sparse Vegetation"
    elif avg_ndvi < 0.5:
        health = "🌿 Moderate Crop"
    else:
        health = "🌾 Healthy Crop"

    if avg_ndvi > 0.6:
        crop = "Rice / Sugarcane"
    elif avg_ndvi > 0.4:
        crop = "Wheat / Maize"
    elif avg_ndvi > 0.2:
        crop = "Vegetables / Pulses"
    else:
        crop = "No Crops"

    if avg_ndvi < 0.2:
        irrigation = "⚠️ Dry - No Crops"
    elif avg_ndvi < 0.4:
        irrigation = "💧 Needs Irrigation"
    else:
        irrigation = "✅ Good"

    return {
        "avg_ndvi": round(avg_ndvi, 3),
        "health": health,
        "crop": crop,
        "water": "N/A",
        "irrigation": irrigation
    }
# ======================================================
# 🛰️ FETCH SENTINEL DATA
# ======================================================
def fetch_sentinel(client_id, client_secret,
                   lon_min, lat_min, lon_max, lat_max,
                   start, end, res):

    config = SHConfig()
    config.sh_client_id = client_id
    config.sh_client_secret = client_secret

    bbox = BBox((lon_min, lat_min, lon_max, lat_max), CRS.WGS84)
    size = bbox_to_dimensions(bbox, resolution=res)

    # =========================
    # RGB IMAGE
    # =========================
    rgb_script = """
    //VERSION=3
    function setup() {
        return {
            input: ["B04", "B03", "B02"],
            output: { bands: 3 }
        };
    }

    function evaluatePixel(s) {
        return [s.B04, s.B03, s.B02];
    }
    """

    rgb_req = SentinelHubRequest(
        evalscript=rgb_script,
        input_data=[
            SentinelHubRequest.input_data(
                data_collection=DataCollection.SENTINEL2_L2A,
                time_interval=(start, end)
            )
        ],
        responses=[
            SentinelHubRequest.output_response("default", MimeType.PNG)
        ],
        bbox=bbox,
        size=size,
        config=config
    )

    os.makedirs("static", exist_ok=True)

    rgb = rgb_req.get_data()[0]
    rgb_path = "static/rgb.png"
    plt.imsave(rgb_path, rgb)

    # =========================
    # NDVI
    # =========================
    ndvi_script = """
    //VERSION=3
    function setup() {
        return {
            input: ["B08", "B04"],
            output: { bands: 1, sampleType: SampleType.FLOAT32 }
        };
    }

    function evaluatePixel(sample) {
        var ndvi = (sample.B08 - sample.B04) / (sample.B08 + sample.B04);
        return [ndvi];
    }
    """

    ndvi_req = SentinelHubRequest(
        evalscript=ndvi_script,
        input_data=[
            SentinelHubRequest.input_data(
                data_collection=DataCollection.SENTINEL2_L2A,
                time_interval=(start, end)
            )
        ],
        responses=[
            SentinelHubRequest.output_response("default", MimeType.TIFF)
        ],
        bbox=bbox,
        size=size,
        config=config
    )

    ndvi = ndvi_req.get_data()[0].squeeze()
    ndvi_path = "static/ndvi.png"

    # =========================
    # NDVI VISUALIZATION
    # =========================
    fig, ax = plt.subplots(figsize=(8, 6))

    im = ax.imshow(ndvi, cmap='YlGn')
    plt.colorbar(im, ax=ax)

    fig.savefig(ndvi_path, dpi=100, bbox_inches='tight')
    plt.close(fig)

    return rgb_path, ndvi_path, size, ndvi
# ======================================================
# 🌐 ROUTES
# ======================================================

@app.route("/")
def home():
    return render_template("home.html")

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        u = request.form["username"]
        p = request.form["password"]

        conn = sqlite3.connect("users.db")
        c = conn.cursor()
        c.execute("SELECT * FROM users WHERE username=?", (u,))
        user = c.fetchone()
        conn.close()

        if user and check_password_hash(user[2], p):
            session["user"] = u
            return redirect(url_for("index"))
        else:
            return render_template("login.html", error="Invalid login")

    return render_template("login.html")

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        u = request.form["username"]
        p = generate_password_hash(request.form["password"])

        try:
            conn = sqlite3.connect("users.db")
            c = conn.cursor()
            c.execute("INSERT INTO users (username,password) VALUES (?,?)", (u, p))
            conn.commit()
            conn.close()
            return redirect(url_for("login"))
        except:
            return render_template("register.html", error="User exists")

    return render_template("register.html")

@app.route("/logout")
def logout():
    session.pop("user", None)
    return redirect(url_for("home"))

@app.route("/index", methods=["GET", "POST"])
def index():
    if "user" not in session:
        return redirect(url_for("login"))

    if request.method == "POST":
        try:
            # ✅ Get from form (if provided)
            form_client_id = request.form.get("client_id", "").strip()
            form_client_secret = request.form.get("client_secret", "").strip()

            # ✅ Update session ONLY if new values provided
            if form_client_id:
                session["client_id"] = form_client_id
            if form_client_secret:
                session["client_secret"] = form_client_secret
            
            session.modified = True  # Force session save

            # ✅ Use stored session values
            client_id = session.get("client_id")
            client_secret = session.get("client_secret")

            if not client_id or not client_secret:
                raise Exception("Enter Client ID & Secret once")

            # ...existing code...
            start = request.form.get("start_date")
            end = request.form.get("end_date")
            res = int(request.form.get("resolution"))

            place = request.form.get("place_name")
            lat = request.form.get("lat")
            lon = request.form.get("lon")

            # ✅ Small precise area
            delta_meters = 100   # 🔥 BEST
            delta_deg = delta_meters / 111320

            if place:
                lat, lon = get_coordinates(place)   # ✅ FIXED

                lon_min = lon - delta_deg
                lat_min = lat - delta_deg
                lon_max = lon + delta_deg
                lat_max = lat + delta_deg

            elif lat and lon:
                lat, lon = float(lat), float(lon)

                lon_min = lon - delta_deg
                lat_min = lat - delta_deg
                lon_max = lon + delta_deg
                lat_max = lat + delta_deg

            elif latest_gps["lat"] and latest_gps["lon"]:
                lat = latest_gps["lat"]
                lon = latest_gps["lon"]

                lon_min = lon - delta_deg
                lat_min = lat - delta_deg
                lon_max = lon + delta_deg
                lat_max = lat + delta_deg

            else:
                raise Exception("No location data")

            # ✅ Fetch data
            rgb, ndvi_img, size, ndvi_array = fetch_sentinel(
                client_id, client_secret,
                lon_min, lat_min, lon_max, lat_max,
                start, end, res
            )

            # ✅ Exact point NDVI
            center_ndvi = ndvi_array[
                ndvi_array.shape[0] // 2,
                ndvi_array.shape[1] // 2
            ]

            analysis = analyze_crop(np.array([center_ndvi]))

            return render_template("index.html",
                                   rgb_path=rgb,
                                   ndvi_path=ndvi_img,
                                   aoi_size=size,
                                   analysis=analysis,
                                   user=session["user"])

        except Exception as e:
            return render_template("index.html",
                                   error=str(e),
                                   user=session["user"])

    return render_template("index.html", user=session["user"])

@app.route("/get-location", methods=["GET", "POST"])
def get_location():
    return jsonify(latest_gps)

# ======================================================
# 🚀 RUN
# ======================================================
if __name__ == "__main__":
    threading.Thread(target=gps_listener, daemon=True).start()
    app.run(debug=True, use_reloader=False)