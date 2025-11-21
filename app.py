# vineyard_spray_app.py
import threading
import time
import json
from datetime import datetime
from typing import List, Dict

import streamlit as st
from streamlit_folium import st_folium
import folium

from fastapi import FastAPI, Request
import uvicorn
import requests

from shapely.geometry import LineString, Point
from shapely.ops import nearest_points

# --------------------------
# In-memory simple DB (for demo). Replace with SQLite/SQLAlchemy for production.
# --------------------------
PLOTS = {
    # Example plot: list of rows (each a list of (lat, lon)).
    "My Vineyard": [
        [(18.5204, 73.8567), (18.5208, 73.8567)],
        [(18.5204, 73.8569), (18.5208, 73.8569)],
        [(18.5204, 73.8571), (18.5208, 73.8571)],
    ]
}

# Convert to shapely lines
def build_rows_for_plot(plot_name):
    rows = []
    for i, coords in enumerate(PLOTS[plot_name]):
        # shapely expects (lon, lat)
        line = LineString([(lon, lat) for lat, lon in coords])
        rows.append({"row_index": i+1, "geom": line, "sprayed": False})
    return rows

# Global runtime data
runtime = {
    "current_plot": None,
    "rows": {},  # plot_name -> list of row dicts
    "session_active": False,
    "session_started_at": None,
    "current_session_id": None,
    "last_location": None,
    "spray_records": []  # list of dicts
}

# --------------------------
# FastAPI to receive GPS pushes from mobile browser
# --------------------------
api = FastAPI()

@api.post("/update_location")
async def update_location(req: Request):
    data = await req.json()
    # expected: {"lat":..., "lon":..., "session_id":...}
    lat = float(data["lat"])
    lon = float(data["lon"])
    ts = datetime.utcnow().isoformat()
    pts = {"timestamp": ts, "lat": lat, "lon": lon, "session_id": data.get("session_id")}
    # store
    runtime["last_location"] = pts
    # run detection
    process_location(lat, lon, ts)
    return {"status": "ok", "received": pts}

def process_location(lat, lon, ts):
    if not runtime["session_active"] or runtime["current_plot"] is None:
        return
    rows = runtime["rows"][runtime["current_plot"]]
    point = Point(lon, lat)
    # compute distances
    best = None
    best_dist = 1e9
    for r in rows:
        dist = point.distance(r["geom"]) * 111000  # approximate deg->meters (lon/lat approx)
        if dist < best_dist:
            best_dist = dist
            best = r
    # threshold meters
    THRESHOLD_METERS = 6.0
    snap_row = best if best_dist <= THRESHOLD_METERS else None
    rec = {"timestamp": ts, "lat": lat, "lon": lon, "snapped_row_index": snap_row["row_index"] if snap_row else None, "distance_m": best_dist}
    runtime["spray_records"].append(rec)

    # marking logic: if many consecutive hits on same row, mark sprayed
    if snap_row:
        # count recent hits for that row
        recent = [r for r in runtime["spray_records"][-10:] if r["snapped_row_index"] == snap_row["row_index"]]
        if len(recent) >= 3:
            # mark sprayed
            snap_row["sprayed"] = True
            snap_row["last_sprayed_at"] = ts

# --------------------------
# Run FastAPI in background thread
# --------------------------
def run_api():
    uvicorn.run(api, host="0.0.0.0", port=8001, log_level="info")

threading.Thread(target=run_api, daemon=True).start()

# --------------------------
# Streamlit UI
# --------------------------
st.set_page_config(layout="wide", page_title="Vineyard Spray Tracker")

st.title("Vineyard Spray Tracking System")

# Left panel: controls
with st.sidebar:
    st.header("Controls")
    plot_names = list(PLOTS.keys())
    sel_plot = st.selectbox("Select Plot", plot_names)
    if sel_plot and sel_plot != runtime.get("current_plot"):
        runtime["current_plot"] = sel_plot
        runtime["rows"][sel_plot] = build_rows_for_plot(sel_plot)
    st.write("Selected plot:", runtime.get("current_plot"))

    start = st.button("Start Session")
    stop = st.button("Stop Session")
    if start:
        runtime["session_active"] = True
        runtime["session_started_at"] = datetime.utcnow().isoformat()
        runtime["current_session_id"] = f"session_{int(time.time())}"
        runtime["spray_records"] = []
        st.success("Session started. Use your mobile browser to send GPS.")
    if stop:
        runtime["session_active"] = False
        st.info("Session stopped.")

    st.markdown("---")
    st.write("Last location:")
    st.json(runtime.get("last_location"))

    st.markdown("**Instructions to use from phone**")
    st.write("""
    1. Open your phone browser and visit: `http://<server_ip>:8501/mobile` (will be provided below).
    2. Allow location permissions.
    3. Click 'Send Location' — it will POST to the app and mark rows.
    """)

# Create a mobile helper page via query param
query = st.experimental_get_query_params()
page = query.get("page", ["main"])[0]
if page == "mobile":
    st.header("Mobile GPS Sender")
    st.write("Tap to send your current GPS to the server. (This uses browser geolocation and POSTs to backend.)")
    if st.button("Send Location"):
        # JS will be injected to fetch location and POST. Using HTML+JS to run navigator.geolocation
        st.markdown("""
        <script>
        navigator.geolocation.getCurrentPosition(function(pos) {
            const lat = pos.coords.latitude;
            const lon = pos.coords.longitude;
            const payload = {lat: lat, lon: lon, session_id: "%s"};
            fetch("http://%s:8001/update_location", {
                method: "POST",
                headers: {'Content-Type':'application/json'},
                body: JSON.stringify(payload)
            }).then(r => r.json()).then(j => {
                document.body.insertAdjacentHTML('beforeend', '<pre>'+JSON.stringify(j, null, 2)+'</pre>');
            }).catch(e => {
                document.body.insertAdjacentHTML('beforeend', '<pre>ERROR: '+e+'</pre>');
            });
        }, function(err){ alert('Error getting location: '+err.message);});
        </script>
        """ % (runtime.get("current_session_id") or "", st.request.remote_addr or "localhost"), unsafe_allow_html=True)
    st.stop()

# Main map view
st.subheader("Map View")

center = (18.5206, 73.8569)
m = folium.Map(location=center, zoom_start=18)

# draw rows
if runtime["current_plot"]:
    for r in runtime["rows"][runtime["current_plot"]]:
        coords = [(lat, lon) for lon, lat in r["geom"].coords]
        color = "green" if r.get("sprayed") else "red"
        folium.PolyLine(locations=coords, color=color, weight=6, opacity=0.8).add_to(m)
# add last location marker
if runtime.get("last_location"):
    folium.CircleMarker(location=(runtime["last_location"]["lat"], runtime["last_location"]["lon"]),
                        radius=6, color="blue", fill=True, fill_color="blue").add_to(m)

st_folium(m, width=900, height=600)

# Show table of rows
st.subheader("Rows Status")
rows_table = []
if runtime["current_plot"]:
    for r in runtime["rows"][runtime["current_plot"]]:
        rows_table.append({
            "row_index": r["row_index"],
            "sprayed": r.get("sprayed", False),
            "last_sprayed_at": r.get("last_sprayed_at")
        })
st.table(rows_table)
