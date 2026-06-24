"""
cron_snapshot.py
Runs weekly via Render cron job (every Sunday midnight).
Copies the live master database to the public snapshot GeoJSON.
"""

import os
import json
from datetime import datetime

# Bootstrap Flask app context
from app import app, build_activity_geojson

def run():
    with app.app_context():
        print(f"[{datetime.utcnow().isoformat()}] Starting weekly public snapshot refresh...")
        try:
            gj = build_activity_geojson()
            path = os.path.join(app.root_path, 'data', 'public_activity_snapshot.geojson')
            with open(path, 'w') as f:
                json.dump(gj, f)
            print(f"[{datetime.utcnow().isoformat()}] ✅ Snapshot written — {len(gj['features'])} road segments.")
        except Exception as e:
            print(f"[{datetime.utcnow().isoformat()}] ❌ Snapshot failed: {e}")
            raise

if __name__ == '__main__':
    run()
