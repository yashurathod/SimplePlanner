## Summary
- Add /nearest_stops endpoint and geolocation-powered dropdown to select nearby origin stop
- Route validation ensures origin stop comes before destination stop in the same trip
- Realtime TripUpdates used when API key is present; static fallback otherwise
- Lazy-load and validate GTFS static files; friendly errors if missing
- UI shows route, headsign, next departure, and stops count

## Test plan
- Visit /
- Allow location
- Observe nearest stops dropdown populated
- Choose a stop or leave as auto-detect
- Enter destination stop name and submit; confirm results show valid trips, or clear message if none
- Optional: set TFI_API_KEY and check that "Next departure" shows times when feed has data

