# Clinton County Road & Bridge Management System

## Local Development

```bash
pip install -r requirements.txt
python app.py
```

Open http://localhost:5000

**Default admin login:** `admin` / `clinton2026!`
Change this immediately after first login via the admin users page.

## Add Your GeoJSON Data

Copy your `roads_quality_combined.geojson` into the `data/` folder.

The app will serve it automatically on both the public and internal maps.

To publish the public snapshot (with 1-week lag), log in as admin and click
"Refresh Public Snapshot" on the staff dashboard.

## Deploy to Render

1. Push this folder to a GitHub repo
2. Create a new Web Service on render.com
3. Connect your GitHub repo
4. Render auto-detects `render.yaml` — just deploy

## Structure

```
clinton_roads/
├── app.py                  # Main Flask app
├── requirements.txt
├── render.yaml
├── data/
│   ├── roads_quality_combined.geojson   ← your working data
│   └── roads_public_snapshot.geojson   ← auto-generated weekly snapshot
├── static/
│   └── css/style.css
└── templates/
    ├── base.html
    ├── index.html
    ├── public_map.html
    ├── report.html
    ├── report_thanks.html
    ├── login.html
    ├── internal_dashboard.html
    ├── internal_map.html
    ├── internal_road_detail.html
    ├── internal_complaints.html
    ├── internal_complaint_detail.html
    ├── admin_users.html
    └── partials/
        └── sidebar.html
```

## PASER Color Reference

| Rating | Color   | Meaning   |
|--------|---------|-----------|
| 5      | #1B5E20 | Excellent |
| 4      | #558B2F | Good      |
| 3      | #F57F17 | Fair      |
| 2      | #BF360C | Poor      |
| 1      | #B71C1C | Failed    |
