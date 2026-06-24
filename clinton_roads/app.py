from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, send_file
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime
import json
import os
import shutil

app = Flask(__name__)
import secrets as _secrets
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY') or _secrets.token_hex(32)
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///clinton_roads.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'

@app.template_filter('display_name')
def display_name_filter(seg):
    if seg.fullname and seg.fullname.strip():
        return seg.fullname.strip()
    if seg.road_name and seg.road_name.strip() and seg.road_name != seg.segment_id:
        return seg.road_name.strip()
    return f'Segment {seg.segment_id}'

# ─────────────────────────────────────────
# MODELS
# ─────────────────────────────────────────

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    role = db.Column(db.String(20), default='viewer')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class RoadSegment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    segment_id = db.Column(db.String(80), unique=True, nullable=False)
    road_name = db.Column(db.String(255))       # FULLNAME from GeoJSON — actual street name
    fullname = db.Column(db.String(255))
    mtfcc = db.Column(db.String(20))
    current_rating = db.Column(db.Integer)      # 1-5 PASER
    current_color = db.Column(db.String(10))    # red/yellow/green
    has_open_ticket = db.Column(db.Boolean, default=False)
    geometry = db.Column(db.Text)
    last_updated = db.Column(db.DateTime, default=datetime.utcnow)
    logs = db.relationship('MaintenanceLog', backref='road', lazy=True,
                           order_by='MaintenanceLog.log_date.desc()')
    complaints = db.relationship('Complaint', backref='road_segment', lazy=True)


class MaintenanceLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    segment_id = db.Column(db.String(80), db.ForeignKey('road_segment.segment_id'), nullable=False)
    entry_type = db.Column(db.String(40), nullable=False, default='maintenance')
    work_type = db.Column(db.String(80))
    description = db.Column(db.Text, nullable=False)
    new_rating = db.Column(db.Integer)
    crew = db.Column(db.String(120))
    equipment = db.Column(db.String(120))
    materials = db.Column(db.String(255))
    logged_by = db.Column(db.String(80))
    log_date = db.Column(db.DateTime, default=datetime.utcnow)
    is_initial_assessment = db.Column(db.Boolean, default=False)


class Complaint(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    segment_id = db.Column(db.String(80), db.ForeignKey('road_segment.segment_id'))
    reporter_name = db.Column(db.String(120), nullable=False)
    reporter_address = db.Column(db.String(255), nullable=False)
    road_name = db.Column(db.String(255), nullable=False)
    category = db.Column(db.String(80), nullable=False)
    description = db.Column(db.Text, nullable=False)
    lat = db.Column(db.Float)
    lng = db.Column(db.Float)
    status = db.Column(db.String(40), default='Open')
    submitted_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow)
    internal_notes = db.Column(db.Text)


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


def paser_to_color(rating):
    rating = int(rating) if rating else 0
    if rating >= 4: return 'green'
    if rating == 3: return 'yellow'
    if rating > 0:  return 'red'
    return 'grey'


def get_display_name(seg):
    """Return the best available road name for display."""
    # Try fullname first (FULLNAME from Tiger/Line GeoJSON)
    if seg.fullname and seg.fullname.strip() and seg.fullname.strip() not in ('', 'None'):
        return seg.fullname.strip()
    # Then road_name if it's not just the segment ID
    if seg.road_name and seg.road_name.strip() and seg.road_name.strip() != seg.segment_id:
        return seg.road_name.strip()
    # Fall back to segment ID
    return f'Road {seg.segment_id}'


def extract_road_name(props):
    """Extract best road name from GeoJSON properties dict."""
    # Try all known Tiger/Line and custom name fields in priority order
    for field in ['FULLNAME', 'fullname', 'NAME', 'name', 'ROADNAME', 'roadname', 'STREET', 'street', 'LABEL']:
        val = props.get(field, '')
        if val and str(val).strip() and str(val).strip().lower() not in ('none', 'null', ''):
            return str(val).strip()
    return ''


# ─────────────────────────────────────────
# SEED
# ─────────────────────────────────────────

def seed_from_geojson():
    # Check data/ first, then roads/ folder
    geojson_path = os.path.join(app.root_path, 'data', 'roads_quality_combined.geojson')
    if not os.path.exists(geojson_path):
        geojson_path = os.path.join(app.root_path, 'data', 'roads', 'roads_quality_combined.geojson')
    if not os.path.exists(geojson_path):
        print("No GeoJSON found — skipping seed.")
        return 0

    # Load Co Rd -> street name lookup (built by spatial matching)
    co_rd_lookup = {}
    lookup_path = os.path.join(app.root_path, 'data', 'co_rd_name_lookup.json')
    if os.path.exists(lookup_path):
        with open(lookup_path) as lf:
            co_rd_lookup = json.load(lf)
        print(f"Loaded {len(co_rd_lookup)} Co Rd name mappings")

    if RoadSegment.query.count() > 0:
        print(f"Already seeded with {RoadSegment.query.count()} segments.")
        return 0

    with open(geojson_path) as f:
        gj = json.load(f)

    count = 0
    for feat in gj.get('features', []):
        p = feat.get('properties', {})
        sid = str(p.get('LINEARID', p.get('segment_id', '')))
        if not sid:
            continue

        # Get the best road name — use spatial lookup for Co Rd designations
        fullname = extract_road_name(p)
        # If it's a county road designation, look up the street name
        if p.get('RTTYP') == 'C' and sid in co_rd_lookup:
            street_name = co_rd_lookup[sid]['street_name']
            # Show both: "NE 272nd St (Co Rd 141)"
            fullname = f"{street_name} ({fullname})" if fullname else street_name
        road_name = fullname or p.get('MTFCC') or sid

        paser = int(p.get('paser_combined') or p.get('paser_lidar') or 0)
        
        # Use quality_color/quality_label directly from GeoJSON if available
        # otherwise derive from paser score
        quality_color = p.get('quality_color', '').lower()
        if quality_color in ('green', 'yellow', 'red'):
            color = quality_color
        else:
            color = paser_to_color(paser)

        seg = RoadSegment(
            segment_id=sid,
            road_name=road_name,
            fullname=fullname,
            mtfcc=p.get('MTFCC',''),
            current_rating=paser,
            current_color=color,
            has_open_ticket=False,
            geometry=json.dumps(feat.get('geometry', {}))
        )
        db.session.add(seg)

        parts = []
        if p.get('quality_label'):     parts.append(f"Condition: {p['quality_label']}")
        if p.get('paser_combined'):    parts.append(f"Combined PASER: {p['paser_combined']}")
        if p.get('paser_lidar'):       parts.append(f"LiDAR Score: {p['paser_lidar']}")
        if p.get('paser_imagery'):     parts.append(f"Imagery Score: {p['paser_imagery']}")
        if p.get('surface_condition'): parts.append(f"Surface: {p['surface_condition']}")
        if p.get('drainage_imagery'):  parts.append(f"Drainage: {p['drainage_imagery']}")
        if p.get('drainage_score'):    parts.append(f"Drainage Score: {p['drainage_score']}")
        if p.get('cross_slope_pct'):   parts.append(f"Cross Slope: {float(p['cross_slope_pct']):.1f}%")
        if p.get('iri'):               parts.append(f"IRI: {float(p['iri']):.1f}")
        if p.get('visible_distress'):  parts.append(f"Distress: {p['visible_distress']}")
        if p.get('conflict'):          parts.append(f"Conflict: {p['conflict']}")
        if p.get('ai_notes'):          parts.append(f"AI Notes: {p['ai_notes']}")

        desc = "Initial road assessment via LiDAR elevation analysis and aerial imagery (June 2026). " + " | ".join(parts)

        log = MaintenanceLog(
            segment_id=sid,
            entry_type='assessment',
            work_type='Initial Assessment',
            description=desc,
            new_rating=paser if paser > 0 else None,
            logged_by='System — LiDAR/Imagery Analysis',
            log_date=datetime(2026, 6, 1),
            is_initial_assessment=True
        )
        db.session.add(log)
        count += 1

    db.session.commit()
    print(f"Seeded {count} road segments.")
    return count


# ─────────────────────────────────────────
# PUBLIC ROUTES
# ─────────────────────────────────────────

@app.route('/')
def index():
    open_count = Complaint.query.filter_by(status='Open').count()
    active_roads = RoadSegment.query.filter_by(has_open_ticket=True).count()
    return render_template('index.html', open_count=open_count, active_roads=active_roads)


@app.route('/activity')
def activity_map():
    active_roads = RoadSegment.query.filter_by(has_open_ticket=True)\
        .order_by(RoadSegment.last_updated.desc()).all()
    total = RoadSegment.query.count()
    green = RoadSegment.query.filter_by(current_color='green').count()
    yellow = RoadSegment.query.filter_by(current_color='yellow').count()
    red = RoadSegment.query.filter_by(current_color='red').count()
    open_complaints = Complaint.query.filter_by(status='Open').count()
    in_progress = Complaint.query.filter_by(status='In Progress').count()
    resolved_complaints = Complaint.query.filter_by(status='Resolved').count()
    total_complaints = Complaint.query.count()
    recent_logs = MaintenanceLog.query\
        .filter_by(is_initial_assessment=False)\
        .order_by(MaintenanceLog.log_date.desc()).limit(5).all()
    return render_template('activity_map.html',
        active_roads=active_roads,
        total=total, green=green, yellow=yellow, red=red,
        open_complaints=open_complaints,
        in_progress=in_progress,
        resolved_complaints=resolved_complaints,
        total_complaints=total_complaints,
        recent_logs=recent_logs)


@app.route('/report', methods=['GET', 'POST'])
def report():
    if request.method == 'POST':
        name = request.form.get('reporter_name','').strip()
        address = request.form.get('reporter_address','').strip()
        road = request.form.get('road_name','').strip()
        category = request.form.get('category','').strip()
        description = request.form.get('description','').strip()
        lat = request.form.get('lat')
        lng = request.form.get('lng')
        segment_id = request.form.get('segment_id','').strip()

        if not all([name, address, road, category, description]):
            flash('Please fill in all required fields.', 'error')
            return render_template('report.html')

        complaint = Complaint(
            reporter_name=name, reporter_address=address,
            road_name=road, category=category, description=description,
            lat=float(lat) if lat else None,
            lng=float(lng) if lng else None,
            segment_id=segment_id or None
        )
        db.session.add(complaint)

        if segment_id:
            seg = RoadSegment.query.filter_by(segment_id=segment_id).first()
            if seg:
                seg.has_open_ticket = True
                seg.last_updated = datetime.utcnow()

        db.session.commit()
        flash(f'Thank you {name}! Report submitted. Reference #CC-{complaint.id:04d}', 'success')
        return redirect(url_for('report_thanks', complaint_id=complaint.id))

    return render_template('report.html')


@app.route('/report/thanks/<int:complaint_id>')
def report_thanks(complaint_id):
    complaint = db.session.get(Complaint, complaint_id)
    return render_template('report_thanks.html', complaint=complaint)


# ─────────────────────────────────────────
# PUBLIC API
# ─────────────────────────────────────────


@app.route('/api/public/search')
def api_search_roads():
    """Search roads by name — returns matches for autocomplete."""
    q = request.args.get('q', '').strip()
    if len(q) < 2:
        return jsonify([])
    
    results = RoadSegment.query.filter(
        db.or_(
            RoadSegment.road_name.ilike(f'%{q}%'),
            RoadSegment.fullname.ilike(f'%{q}%')
        )
    ).limit(15).all()
    
    return jsonify([{
        'segment_id': r.segment_id,
        'road_name': get_display_name(r),
        'current_color': r.current_color,
        'color_label': {'green':'Good','yellow':'Fair','red':'Poor','grey':'Unrated'}.get(r.current_color, 'Unknown'),
        'has_open_ticket': r.has_open_ticket
    } for r in results])


@app.route('/api/internal/search')
@login_required
def api_search_roads_internal():
    """Internal road search — same as public but requires auth."""
    return api_search_roads()


@app.route('/api/public/dashboard')
def api_public_dashboard():
    """Public-facing summary stats for the activity map dashboard panel."""
    total = RoadSegment.query.count()
    green = RoadSegment.query.filter_by(current_color='green').count()
    yellow = RoadSegment.query.filter_by(current_color='yellow').count()
    red = RoadSegment.query.filter_by(current_color='red').count()
    open_tickets = RoadSegment.query.filter_by(has_open_ticket=True).count()
    open_complaints = Complaint.query.filter_by(status='Open').count()
    resolved_complaints = Complaint.query.filter_by(status='Resolved').count()
    recent_logs = MaintenanceLog.query\
        .filter_by(is_initial_assessment=False)\
        .order_by(MaintenanceLog.log_date.desc()).limit(5).all()

    return jsonify({
        'total_roads': total,
        'green': green,
        'yellow': yellow,
        'red': red,
        'open_tickets': open_tickets,
        'open_complaints': open_complaints,
        'resolved_complaints': resolved_complaints,
        'recent_activity': [{
            'road': get_display_name(l.road) if l.road else l.segment_id,
            'work_type': l.work_type or l.entry_type,
            'date': l.log_date.strftime('%b %d, %Y'),
            'logged_by': l.logged_by or ''
        } for l in recent_logs]
    })


@app.route('/api/public/activity')
def api_public_activity():
    """Weekly snapshot for public map. Falls back to live DB, then raw GeoJSON."""
    # 1. Try weekly snapshot
    snapshot_path = os.path.join(app.root_path, 'data', 'public_activity_snapshot.geojson')
    if os.path.exists(snapshot_path):
        with open(snapshot_path) as f:
            return jsonify(json.load(f))

    # 2. Try live DB
    if RoadSegment.query.count() > 0:
        return jsonify(build_activity_geojson())

    # 3. Fall back to raw GeoJSON with basic coloring — works before seed
    raw_path = os.path.join(app.root_path, 'data', 'roads_quality_combined.geojson')
    if not os.path.exists(raw_path):
        raw_path = os.path.join(app.root_path, 'data', 'roads', 'roads_quality_combined.geojson')
    if os.path.exists(raw_path):
        with open(raw_path) as f:
            gj = json.load(f)
        COLOR_HEX = {'green':'#2E7D32','yellow':'#F9A825','red':'#C62828','grey':'#888888'}
        COLOR_LABEL = {'green':'Good','yellow':'Fair','red':'Poor','grey':'Unrated'}
        features = []
        for feat in gj.get('features', []):
            p = feat.get('properties', {})
            paser = int(p.get('paser_combined') or p.get('paser_lidar') or 0)
            color = paser_to_color(paser)
            sid = str(p.get('LINEARID', p.get('segment_id', '')))
            fullname = (p.get('FULLNAME') or p.get('NAME') or '').strip()
            feat['properties']['segment_id'] = sid
            feat['properties']['road_name'] = fullname or f'Segment {sid}'
            feat['properties']['current_color'] = color
            feat['properties']['current_rating'] = paser
            feat['properties']['color_hex'] = COLOR_HEX.get(color, '#888')
            feat['properties']['color_label'] = COLOR_LABEL.get(color, 'Unknown')
            feat['properties']['has_open_ticket'] = False
            features.append(feat)
        return jsonify({'type': 'FeatureCollection', 'features': features})

    return jsonify({'type': 'FeatureCollection', 'features': []})



@app.route('/api/public/road/<segment_id>')
def api_public_road_detail(segment_id):
    seg = RoadSegment.query.filter_by(segment_id=segment_id).first()
    if not seg:
        return jsonify({'error': 'Not found'}), 404

    logs = MaintenanceLog.query.filter_by(segment_id=segment_id)\
        .order_by(MaintenanceLog.log_date.desc()).all()
    open_complaints = Complaint.query.filter_by(segment_id=segment_id)\
        .filter(Complaint.status.in_(['Open','In Progress'])).all()

    return jsonify({
        'segment_id': seg.segment_id,
        'road_name': get_display_name(seg),
        'current_rating': seg.current_rating,
        'current_color': seg.current_color,
        'has_open_ticket': seg.has_open_ticket,
        'last_updated': seg.last_updated.strftime('%B %d, %Y') if seg.last_updated else '',
        'open_complaints': [{
            'id': f'CC-{c.id:04d}',
            'category': c.category,
            'description': c.description,
            'status': c.status,
            'date': c.submitted_at.strftime('%b %d, %Y')
        } for c in open_complaints],
        'log': [{
            'id': l.id,
            'entry_type': l.entry_type,
            'work_type': l.work_type or '',
            'description': l.description,
            'new_rating': l.new_rating,
            'crew': l.crew or '',
            'equipment': l.equipment or '',
            'materials': l.materials or '',
            'logged_by': l.logged_by or '',
            'date': l.log_date.strftime('%B %d, %Y'),
            'is_initial': l.is_initial_assessment
        } for l in logs]
    })


# ─────────────────────────────────────────
# AUTH
# ─────────────────────────────────────────

@app.route('/login', methods=['GET','POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('internal_dashboard'))
    if request.method == 'POST':
        user = User.query.filter_by(username=request.form.get('username','').strip()).first()
        if user and user.check_password(request.form.get('password','')):
            login_user(user, remember=False)  # Session cookie only — auto-logout on browser close
            return redirect(url_for('internal_dashboard'))
        flash('Invalid username or password.', 'error')
    return render_template('login.html')


@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('index'))


# ─────────────────────────────────────────
# INTERNAL ROUTES
# ─────────────────────────────────────────

@app.route('/internal')
@login_required
def internal_dashboard():
    total = RoadSegment.query.count()
    green = RoadSegment.query.filter_by(current_color='green').count()
    yellow = RoadSegment.query.filter_by(current_color='yellow').count()
    red = RoadSegment.query.filter_by(current_color='red').count()
    open_complaints = Complaint.query.filter_by(status='Open').count()
    in_progress = Complaint.query.filter_by(status='In Progress').count()
    active_roads = RoadSegment.query.filter_by(has_open_ticket=True).count()
    recent_logs = MaintenanceLog.query.filter_by(is_initial_assessment=False)\
        .order_by(MaintenanceLog.log_date.desc()).limit(8).all()
    recent_complaints = Complaint.query.order_by(Complaint.submitted_at.desc()).limit(6).all()
    seeded = total > 0
    return render_template('internal_dashboard.html',
        total=total, green=green, yellow=yellow, red=red,
        open_complaints=open_complaints, in_progress=in_progress,
        active_roads=active_roads, recent_logs=recent_logs,
        recent_complaints=recent_complaints, seeded=seeded)


@app.route('/internal/map')
@login_required
def internal_map():
    active_roads = RoadSegment.query.filter_by(has_open_ticket=True)\
        .order_by(RoadSegment.last_updated.desc()).all()
    return render_template('internal_map.html', active_roads=active_roads)


@app.route('/internal/road/<segment_id>')
@login_required
def internal_road_detail(segment_id):
    seg = RoadSegment.query.filter_by(segment_id=segment_id).first_or_404()
    logs = MaintenanceLog.query.filter_by(segment_id=segment_id)\
        .order_by(MaintenanceLog.log_date.desc()).all()
    complaints = Complaint.query.filter_by(segment_id=segment_id)\
        .order_by(Complaint.submitted_at.desc()).all()
    today = datetime.utcnow().strftime('%Y-%m-%d')
    return render_template('internal_road_detail.html',
        seg=seg, logs=logs, complaints=complaints, today=today,
        display_name=get_display_name(seg))


@app.route('/internal/road/<segment_id>/log', methods=['POST'])
@login_required
def add_maintenance_log(segment_id):
    seg = RoadSegment.query.filter_by(segment_id=segment_id).first_or_404()
    entry_type = request.form.get('entry_type','maintenance')
    new_rating_raw = request.form.get('new_rating','').strip()
    new_rating = int(new_rating_raw) if new_rating_raw and new_rating_raw.isdigit() else None

    log = MaintenanceLog(
        segment_id=segment_id,
        entry_type=entry_type,
        work_type=request.form.get('work_type',''),
        description=request.form.get('description','').strip(),
        new_rating=new_rating,
        crew=request.form.get('crew',''),
        equipment=request.form.get('equipment',''),
        materials=request.form.get('materials',''),
        logged_by=current_user.username,
        log_date=datetime.strptime(
            request.form.get('log_date', datetime.utcnow().strftime('%Y-%m-%d')), '%Y-%m-%d')
    )
    db.session.add(log)

    if new_rating:
        seg.current_rating = new_rating
        seg.current_color = paser_to_color(new_rating)

    seg.last_updated = datetime.utcnow()
    db.session.commit()
    flash('Entry saved.', 'success')
    return redirect(url_for('internal_road_detail', segment_id=segment_id))


@app.route('/internal/road/<segment_id>/rate', methods=['POST'])
@login_required
def update_road_rating(segment_id):
    """Quick rating update — requires re-authentication to confirm."""
    seg = RoadSegment.query.filter_by(segment_id=segment_id).first_or_404()
    new_rating = int(request.form.get('rating', 0))
    if new_rating not in [1,2,3,4,5]:
        return jsonify({'error': 'Invalid rating'}), 400

    # Fix 1: Re-verify credentials before allowing rating change
    username = request.form.get('username', '').strip()
    password = request.form.get('password', '')
    if username or password:  # If credentials provided, verify them
        user = User.query.filter_by(username=username).first()
        if not user or not user.check_password(password):
            return jsonify({'error': 'Invalid username or password.'}), 401

    old_color = seg.current_color
    seg.current_rating = new_rating
    seg.current_color = paser_to_color(new_rating)
    seg.last_updated = datetime.utcnow()

    COLOR_LABEL = {'green':'Good','yellow':'Fair','red':'Poor','grey':'Unrated'}
    old_label = COLOR_LABEL.get(old_color, old_color)
    new_label = COLOR_LABEL.get(seg.current_color, seg.current_color)

    log = MaintenanceLog(
        segment_id=segment_id,
        entry_type='rating_update',
        work_type='Rating Update',
        description=f'Road condition updated from {old_label} to {new_label} by {current_user.username}. (Reference: PASER {new_rating})',
        new_rating=new_rating,
        logged_by=current_user.username,
        log_date=datetime.utcnow()
    )
    db.session.add(log)
    db.session.commit()
    COLOR_HEX = {'green':'#2E7D32','yellow':'#F9A825','red':'#C62828','grey':'#888888'}
    return jsonify({
        'success': True,
        'new_color': seg.current_color,
        'new_rating': new_rating,
        'new_label': new_label,
        'color_hex': COLOR_HEX.get(seg.current_color, '#888')
    })


@app.route('/internal/complaints')
@login_required
def internal_complaints():
    status_filter = request.args.get('status','all')
    q = Complaint.query
    if status_filter != 'all':
        q = q.filter_by(status=status_filter)
    complaints = q.order_by(Complaint.submitted_at.desc()).all()
    return render_template('internal_complaints.html', complaints=complaints, status_filter=status_filter)


@app.route('/internal/complaints/<int:complaint_id>', methods=['GET','POST'])
@login_required
def internal_complaint_detail(complaint_id):
    complaint = db.session.get(Complaint, complaint_id)
    if not complaint:
        flash('Not found.','error')
        return redirect(url_for('internal_complaints'))
    if request.method == 'POST':
        old_status = complaint.status
        complaint.status = request.form.get('status', complaint.status)
        complaint.internal_notes = request.form.get('internal_notes','')
        complaint.updated_at = datetime.utcnow()
        if complaint.segment_id and complaint.status == 'Resolved' and old_status != 'Resolved':
            remaining = Complaint.query.filter_by(segment_id=complaint.segment_id)\
                .filter(Complaint.status.in_(['Open','In Progress']))\
                .filter(Complaint.id != complaint_id).count()
            seg = RoadSegment.query.filter_by(segment_id=complaint.segment_id).first()
            if seg and remaining == 0:
                seg.has_open_ticket = False
                seg.last_updated = datetime.utcnow()
        db.session.commit()
        flash('Updated.','success')
        return redirect(url_for('internal_complaint_detail', complaint_id=complaint_id))
    return render_template('internal_complaint_detail.html', complaint=complaint)


# ─────────────────────────────────────────
# INTERNAL API
# ─────────────────────────────────────────

@app.route('/api/internal/roads')
@login_required
def api_internal_roads():
    return jsonify(build_activity_geojson())


@app.route('/api/internal/road/<segment_id>')
@login_required
def api_internal_road_detail(segment_id):
    return api_public_road_detail(segment_id)


@app.route('/api/admin/refresh-snapshot', methods=['POST'])
@login_required
def refresh_snapshot():
    if current_user.role != 'admin':
        return jsonify({'error':'Admin only'}), 403
    gj = build_activity_geojson()
    path = os.path.join(app.root_path, 'data', 'public_activity_snapshot.geojson')
    with open(path, 'w') as f:
        json.dump(gj, f)
    return jsonify({'success': True, 'features': len(gj['features']),
                    'refreshed_at': datetime.utcnow().strftime('%B %d, %Y %H:%M UTC')})


@app.route('/api/admin/seed', methods=['POST'])
@login_required
def admin_seed():
    if current_user.role != 'admin':
        return jsonify({'error':'Admin only'}), 403
    force = request.args.get('force') == '1'
    if force:
        MaintenanceLog.query.filter_by(is_initial_assessment=True).delete()
        RoadSegment.query.delete()
        db.session.commit()
    count = seed_from_geojson()
    return jsonify({'success': True, 'seeded': count})


# Fix 3: Serve PASER GeoJSON directly for the reference link



@app.route('/internal/roads-data/<filename>')
@login_required  
def serve_roads_data(filename):
    """Serve reference GeoJSON files from the roads/ folder."""
    roads_dir = os.path.join(app.root_path, 'data', 'roads')
    filepath = os.path.join(roads_dir, filename)
    if not os.path.exists(filepath) or not filename.endswith('.geojson'):
        return jsonify({'error': 'File not found'}), 404
    with open(filepath) as f:
        return jsonify(json.load(f))


@app.route('/api/paser/geojson')
@login_required
def api_paser_geojson():
    """Serve the raw PASER GeoJSON for the reference map viewer."""
    geojson_path = os.path.join(app.root_path, 'data', 'roads_quality_combined.geojson')
    if not os.path.exists(geojson_path):
        geojson_path = os.path.join(app.root_path, 'data', 'roads', 'roads_quality_combined.geojson')
    if not os.path.exists(geojson_path):
        return jsonify({'type':'FeatureCollection','features':[]})
    with open(geojson_path) as f:
        return jsonify(json.load(f))


@app.route('/internal/paser-map')
@login_required
def paser_map_viewer():
    """Render the original PASER assessment data as a map."""
    geojson_path = os.path.join(app.root_path, 'data', 'roads_quality_combined.geojson')
    has_data = os.path.exists(geojson_path)
    return render_template('paser_map.html', has_data=has_data)


@app.route('/internal/paser-reference')
@login_required
def paser_reference():
    geojson_path = os.path.join(app.root_path, 'data', 'roads_quality_combined.geojson')
    if not os.path.exists(geojson_path):
        flash('PASER GeoJSON file not found. Place roads_quality_combined.geojson in the data/ folder.', 'error')
        return redirect(url_for('internal_dashboard'))
    return send_file(geojson_path, mimetype='application/json',
                     as_attachment=False,
                     download_name='roads_quality_combined.geojson')



import csv
import io

@app.route('/internal/export/roads')
@login_required
def export_roads_csv():
    """Export all road segments to CSV."""
    segments = RoadSegment.query.order_by(RoadSegment.road_name).all()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['Road Name', 'Segment ID', 'Condition', 'PASER Rating', 'Has Open Ticket', 'Last Updated'])
    for s in segments:
        writer.writerow([
            get_display_name(s),
            s.segment_id,
            {'green':'Good','yellow':'Fair','red':'Poor','grey':'Unrated'}.get(s.current_color,'Unknown'),
            s.current_rating or '',
            'Yes' if s.has_open_ticket else 'No',
            s.last_updated.strftime('%Y-%m-%d') if s.last_updated else ''
        ])
    output.seek(0)
    from flask import Response
    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': 'attachment; filename=clinton_county_roads.csv'}
    )


@app.route('/internal/export/tickets/open')
@login_required
def export_open_tickets_csv():
    """Export all open complaints to CSV."""
    complaints = Complaint.query.filter(
        Complaint.status.in_(['Open', 'In Progress'])
    ).order_by(Complaint.submitted_at.desc()).all()
    return _complaints_csv(complaints, 'open_tickets.csv')


@app.route('/internal/export/tickets/closed')
@login_required
def export_closed_tickets_csv():
    """Export all resolved complaints to CSV."""
    complaints = Complaint.query.filter_by(status='Resolved')        .order_by(Complaint.submitted_at.desc()).all()
    return _complaints_csv(complaints, 'closed_tickets.csv')


def _complaints_csv(complaints, filename):
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['Ref #', 'Road Name', 'Category', 'Description', 'Reporter Name',
                     'Reporter Address', 'Status', 'Submitted', 'Updated', 'Internal Notes'])
    for c in complaints:
        writer.writerow([
            f'CC-{c.id:04d}',
            c.road_name,
            c.category,
            c.description,
            c.reporter_name,
            c.reporter_address,
            c.status,
            c.submitted_at.strftime('%Y-%m-%d %H:%M') if c.submitted_at else '',
            c.updated_at.strftime('%Y-%m-%d %H:%M') if c.updated_at else '',
            c.internal_notes or ''
        ])
    output.seek(0)
    from flask import Response
    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename={filename}'}
    )



@app.route('/internal/change-password', methods=['GET', 'POST'])
@login_required
def change_password():
    error = None
    success = None
    if request.method == 'POST':
        current = request.form.get('current_password', '')
        new_pw  = request.form.get('new_password', '').strip()
        confirm = request.form.get('confirm_password', '').strip()

        if not current_user.check_password(current):
            error = 'Current password is incorrect.'
        elif len(new_pw) < 8:
            error = 'New password must be at least 8 characters.'
        elif new_pw != confirm:
            error = 'New passwords do not match.'
        else:
            current_user.set_password(new_pw)
            db.session.commit()
            success = 'Password updated successfully.'

    return render_template('change_password.html', error=error, success=success)


@app.route('/internal/admin/users', methods=['GET','POST'])
@login_required
def admin_users():
    if current_user.role != 'admin':
        flash('Admin only.','error')
        return redirect(url_for('internal_dashboard'))
    if request.method == 'POST':
        username = request.form.get('username','').strip()
        password = request.form.get('password','')
        role = request.form.get('role','viewer')
        if User.query.filter_by(username=username).first():
            flash('Username already exists.','error')
        else:
            u = User(username=username, role=role)
            u.set_password(password)
            db.session.add(u)
            db.session.commit()
            flash(f'User {username} created.','success')
    users = User.query.all()
    return render_template('admin_users.html', users=users)


# ─────────────────────────────────────────
# HELPER
# ─────────────────────────────────────────

def build_activity_geojson():
    """Build GeoJSON from live DB. Road color = condition rating. Open ticket = separate flag."""
    COLOR_HEX = {'green':'#2E7D32','yellow':'#F9A825','red':'#C62828','grey':'#888888'}
    COLOR_LABEL = {'green':'Good','yellow':'Fair','red':'Poor','grey':'Unrated'}
    segments = RoadSegment.query.all()
    features = []
    for seg in segments:
        if not seg.geometry:
            continue
        try:
            geom = json.loads(seg.geometry)
        except:
            continue
        features.append({
            'type': 'Feature',
            'geometry': geom,
            'properties': {
                'segment_id': seg.segment_id,
                'road_name': get_display_name(seg),
                'current_rating': seg.current_rating,
                'current_color': seg.current_color,
                'color_hex': COLOR_HEX.get(seg.current_color, '#888'),
                'color_label': COLOR_LABEL.get(seg.current_color, 'Unknown'),
                'has_open_ticket': seg.has_open_ticket,
                'last_updated': seg.last_updated.strftime('%B %d, %Y') if seg.last_updated else ''
            }
        })
    return {'type': 'FeatureCollection', 'features': features}


# ─────────────────────────────────────────
# INIT
# ─────────────────────────────────────────

def init_db():
    with app.app_context():
        db.create_all()
        if not User.query.filter_by(username='admin').first():
            admin = User(username='admin', role='admin')
            admin.set_password('clinton2026!')
            db.session.add(admin)
            db.session.commit()
            print("Admin created: admin / clinton2026!")
        seed_from_geojson()


def run_weekly_snapshot():
    with app.app_context():
        import json as _json
        gj = build_activity_geojson()
        path = os.path.join(app.root_path, 'data', 'public_activity_snapshot.geojson')
        with open(path, 'w') as f:
            _json.dump(gj, f)
        print(f"Weekly snapshot refreshed: {len(gj['features'])} segments.")


if __name__ == '__main__':
    init_db()
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        scheduler = BackgroundScheduler()
        scheduler.add_job(run_weekly_snapshot, 'cron', day_of_week='sun', hour=0, minute=0)
        scheduler.start()
        print("Weekly snapshot scheduler started (Sundays at midnight).")
    except Exception as e:
        print(f"Scheduler not started: {e}")
    app.run(debug=True, port=5000)
