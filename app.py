from flask import Flask, render_template, request, jsonify
import pandas as pd
import requests

app = Flask(__name__)

# Your TFI API key
API_KEY = "d75c5bbe8ab641149e15c071e520af77"
BASE_URL = "https://api.nationaltransport.ie/gtfsr/v2/TripUpdates?format=json"

# 📄 Load GTFS static files once (for speed)
routes = pd.read_csv("static_data/routes.txt")
trips = pd.read_csv("static_data/trips.txt")
stops = pd.read_csv("static_data/stops.txt")

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
        budget = float(request.form['budget'])
        user_lat = float(request.form.get('lat', 0))
        user_lon = float(request.form.get('lon', 0))

        # 1️⃣ Find all stops near your location
        stops['distance'] = stops.apply(
            lambda row: haversine(user_lat, user_lon, row['stop_lat'], row['stop_lon']),
            axis=1
        )
        nearby_stops = stops[stops['distance'] <= 1.0]  # within 1 km radius
        nearby_stop_ids = nearby_stops['stop_id'].tolist()

        # 2️⃣ Find all stops for the destination
        matching_stops = stops[stops['stop_name'].str.contains(destination, case=False, na=False)]
        stop_ids = matching_stops['stop_id'].tolist()

        if not nearby_stop_ids or not stop_ids:
            return render_template('index.html', results=[], message="No nearby or destination stops found")

        # 3️⃣ Find trips that start near you and go to destination
        stop_times = pd.read_csv("static_data/stop_times.txt", usecols=['trip_id', 'stop_id'])
        trips_from_nearby = stop_times[stop_times['stop_id'].isin(nearby_stop_ids)]
        trips_to_dest = stop_times[stop_times['stop_id'].isin(stop_ids)]

        valid_trip_ids = set(trips_from_nearby['trip_id']).intersection(set(trips_to_dest['trip_id']))

        # 4️⃣ Fetch realtime data
        headers = {'x-api-key': API_KEY}
        response = requests.get(BASE_URL, headers=headers)
        data = response.json()

        results = []
        for entity in data.get("entity", []):
            trip_data = entity.get("trip_update", {}).get("trip", {})
            route_id = trip_data.get("route_id")
            trip_id = trip_data.get("trip_id")
            start_time = trip_data.get("start_time", "N/A")

            if trip_id in valid_trip_ids:
                route_row = routes[routes["route_id"] == route_id]
                trip_row = trips[trips["trip_id"] == trip_id]
                if not route_row.empty and not trip_row.empty:
                    route_name = route_row.iloc[0]["route_short_name"]
                    destination_name = trip_row.iloc[0]["trip_headsign"]
                    estimated_cost = 2.10 if budget >= 2.10 else budget
                    results.append({
                        "route": route_name,
                        "destination": destination_name,
                        "start_time": start_time,
                        "estimated_cost": f"€{estimated_cost:.2f}"
                    })

        if not results:
            return render_template('index.html', results=[], message=f"No live buses found from your location to {destination}")

        return render_template('index.html', results=results, destination=destination, budget=budget)

    except Exception as e:
        return jsonify({"error": str(e)})

if __name__ == '__main__':
    app.run(debug=True)
