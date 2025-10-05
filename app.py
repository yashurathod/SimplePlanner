from flask import Flask, render_template, request, jsonify
import pandas as pd
import requests
from datetime import datetime
import os

app = Flask(__name__)

# Your TFI API key (prefer environment variable TFI_API_KEY)
API_KEY = os.getenv("TFI_API_KEY", "d75c5bbe8ab641149e15c071e520af77")
BASE_URL = "https://api.nationaltransport.ie/gtfsr/v2/TripUpdates?format=json"

# 📄 Lazy-load GTFS static files for robustness
routes_df = None
trips_df = None
stops_df = None

REQUIRED_GTFS_FILES = [
    "routes.txt",
    "trips.txt",
    "stops.txt",
    "stop_times.txt",
]

def gtfs_path(filename: str) -> str:
    return os.path.join("static_data", filename)

def missing_static_files() -> list:
    return [f for f in REQUIRED_GTFS_FILES if not os.path.exists(gtfs_path(f))]

def load_static_data():
    global routes_df, trips_df, stops_df
    if routes_df is not None and trips_df is not None and stops_df is not None:
        return routes_df, trips_df, stops_df
    # Ensure files exist
    missing = missing_static_files()
    if missing:
        raise FileNotFoundError(
            "Missing GTFS static files: " + ", ".join(missing) +
            ". Put them under 'static_data/' from the GTFS schedule feed."
        )
    # Load with safe dtypes so IDs remain strings
    routes_df = pd.read_csv(gtfs_path("routes.txt"), dtype=str)
    trips_df = pd.read_csv(gtfs_path("trips.txt"), dtype=str)
    stops_df = pd.read_csv(gtfs_path("stops.txt"), dtype={
        'stop_id': str, 'stop_name': str, 'stop_lat': float, 'stop_lon': float
    })
    return routes_df, trips_df, stops_df

from math import radians, sin, cos, sqrt, atan2

def haversine(lat1, lon1, lat2, lon2):
    R = 6371  # Earth radius in km
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat/2)**2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon/2)**2
    c = 2 * atan2(sqrt(a), sqrt(1 - a))
    return R * c


@app.route('/')
def home():
    return render_template('index.html')

@app.route('/get_routes', methods=['POST'])
def get_routes():
    try:
        destination = request.form['destination']
        # Budget may not be relevant for routing; keep parsing for backward compatibility
        budget_raw = request.form.get('budget')
        budget = float(budget_raw) if budget_raw is not None and budget_raw != '' else 0.0
        user_lat = float(request.form.get('lat', 0))
        user_lon = float(request.form.get('lon', 0))

        if not user_lat or not user_lon:
            return render_template(
                'index.html',
                results=[],
                message="Couldn't get your location. Please allow location access and try again.",
            )

        # Ensure static data is available
        try:
            routes, trips, stops = load_static_data()
        except FileNotFoundError as fe:
            return render_template('index.html', results=[], message=str(fe))

        # 1) Determine your single nearest stop (no radius filter)
        stops_with_distance = stops.assign(
            distance=stops.apply(
                lambda row: haversine(user_lat, user_lon, row['stop_lat'], row['stop_lon']),
                axis=1,
            )
        )
        nearest_stop_row = stops_with_distance.sort_values('distance').iloc[0]
        origin_stop_id = str(nearest_stop_row['stop_id'])
        origin_stop_name = nearest_stop_row['stop_name']

        # 2) Resolve destination stop by exact match first, then contains
        dest_exact = stops[stops['stop_name'].str.lower() == destination.strip().lower()]
        dest_matches = dest_exact if not dest_exact.empty else stops[
            stops['stop_name'].str.contains(destination, case=False, na=False)
        ]
        if dest_matches.empty:
            return render_template(
                'index.html',
                results=[],
                message=f"No destination stop found matching '{destination}'. Try the exact bus stop name.",
            )
        destination_stop_row = dest_matches.iloc[0]
        destination_stop_id = str(destination_stop_row['stop_id'])
        destination_stop_name = destination_stop_row['stop_name']

        if origin_stop_id == destination_stop_id:
            return render_template(
                'index.html',
                results=[],
                message="You're already at the destination stop.",
            )

        # 3) Find trips where origin stop occurs before destination stop on the same trip
        stop_times = pd.read_csv(
            "static_data/stop_times.txt",
            usecols=['trip_id', 'stop_id', 'stop_sequence'],
            dtype={'trip_id': str, 'stop_id': str, 'stop_sequence': int},
        )
        origin_times = (
            stop_times[stop_times['stop_id'] == origin_stop_id][['trip_id', 'stop_sequence']]
            .rename(columns={'stop_sequence': 'origin_sequence'})
        )
        dest_times = (
            stop_times[stop_times['stop_id'] == destination_stop_id][['trip_id', 'stop_sequence']]
            .rename(columns={'stop_sequence': 'dest_sequence'})
        )
        segments = origin_times.merge(dest_times, on='trip_id', how='inner')
        segments = segments[segments['origin_sequence'] < segments['dest_sequence']]

        if segments.empty:
            return render_template(
                'index.html',
                results=[],
                message=f"No direct buses found from '{origin_stop_name}' to '{destination_stop_name}'.",
            )

        # For each trip, keep the shortest segment between origin and destination
        segments['stops_count'] = segments['dest_sequence'] - segments['origin_sequence']
        segments = segments.sort_values(['trip_id', 'stops_count']).drop_duplicates(['trip_id'], keep='first')
        valid_trip_ids = set(segments['trip_id'])

        # Quick lookup for origin/destination sequences per trip
        seq_by_trip = {
            row.trip_id: {
                'origin_sequence': int(row.origin_sequence),
                'dest_sequence': int(row.dest_sequence),
                'stops_count': int(row.stops_count),
            }
            for row in segments.itertuples(index=False)
        }

        # 4) Fetch realtime data (optional). If unavailable, still return static segments.
        results = []
        try:
            headers = {'x-api-key': API_KEY}
            response = requests.get(BASE_URL, headers=headers, timeout=8)
            data = response.json()
        except Exception:
            data = {"entity": []}

        seen_trip_ids = set()
        for entity in data.get("entity", []):
            trip_update = entity.get("trip_update", {})
            trip_data = trip_update.get("trip", {})
            trip_id = trip_data.get("trip_id")
            route_id = trip_data.get("route_id")
            if not trip_id or trip_id not in valid_trip_ids:
                continue

            # Determine next departure time at origin stop if available
            next_departure = "N/A"
            for stu in trip_update.get("stop_time_update", []) or []:
                if str(stu.get("stop_id")) == origin_stop_id:
                    ts = (
                        (stu.get("departure") or {}).get("time")
                        or (stu.get("arrival") or {}).get("time")
                    )
                    if ts:
                        try:
                            next_departure = datetime.fromtimestamp(int(ts)).strftime("%H:%M")
                        except Exception:
                            next_departure = "N/A"
                    break

            trip_row = trips[trips["trip_id"] == trip_id]
            route_short_name = None
            headsign = None
            if not trip_row.empty:
                headsign = trip_row.iloc[0].get("trip_headsign", "")
                if route_id is None:
                    route_id = trip_row.iloc[0].get("route_id")
            if route_id is not None:
                route_row = routes[routes["route_id"] == route_id]
                if not route_row.empty:
                    route_short_name = route_row.iloc[0].get("route_short_name", route_id)

            seq = seq_by_trip.get(trip_id, {})
            results.append({
                "route": route_short_name or str(route_id or ""),
                "headsign": headsign or "",
                "origin_stop": origin_stop_name,
                "destination_stop": destination_stop_name,
                "next_departure": next_departure,
                "stops_count": seq.get('stops_count', None),
            })
            seen_trip_ids.add(trip_id)

        # Fallback: include static trips not present in realtime feed
        missing_trip_ids = list(valid_trip_ids - seen_trip_ids)
        for trip_id in missing_trip_ids[:10]:  # limit fallback entries
            trip_row = trips[trips["trip_id"] == trip_id]
            if trip_row.empty:
                continue
            route_id = trip_row.iloc[0].get("route_id")
            headsign = trip_row.iloc[0].get("trip_headsign", "")
            route_short_name = None
            if route_id is not None:
                route_row = routes[routes["route_id"] == route_id]
                if not route_row.empty:
                    route_short_name = route_row.iloc[0].get("route_short_name", route_id)

            seq = seq_by_trip.get(trip_id, {})
            results.append({
                "route": route_short_name or str(route_id or ""),
                "headsign": headsign or "",
                "origin_stop": origin_stop_name,
                "destination_stop": destination_stop_name,
                "next_departure": "N/A",
                "stops_count": seq.get('stops_count', None),
            })

        if not results:
            return render_template(
                'index.html',
                results=[],
                message=f"No buses found from '{origin_stop_name}' to '{destination_stop_name}' right now.",
            )

        # Sort: realtime first by soonest departure, then others
        def sort_key(item):
            nd = item.get("next_departure")
            return (0 if nd != "N/A" else 1, nd)

        results.sort(key=sort_key)

        return render_template(
            'index.html',
            results=results,
            origin_stop=origin_stop_name,
            destination_stop=destination_stop_name,
        )

    except Exception as e:
        return jsonify({"error": str(e)})

if __name__ == '__main__':
    app.run(debug=True)
