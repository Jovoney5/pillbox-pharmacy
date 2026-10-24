import os
from dotenv import load_dotenv
load_dotenv()  # read .env into environment (for MAIL_* settings, etc.)
import uuid
import random
import string
import secrets
import datetime
from functools import wraps
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
import emails
import db
import payments

app = Flask(__name__)
# Use the SECRET_KEY env var in production (Render sets one); fall back for local dev.
app.secret_key = os.environ.get("SECRET_KEY", "pillbox_secret_2024")

# Keep users logged in for a year (persistent, app-like login until they sign out).
app.permanent_session_lifetime = datetime.timedelta(days=365)


_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


@app.template_filter("nicedate")
def nicedate(value):
    """Format an ISO date (YYYY-MM-DD...) as 'Oct-15-2026'. Returns input unchanged if unparseable."""
    if not value:
        return value
    s = str(value)[:10]
    parts = s.split("-")
    if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit() and parts[2].isdigit():
        try:
            return f"{_MONTHS[int(parts[1]) - 1]}-{parts[2]}-{parts[0]}"
        except (IndexError, ValueError):
            return value
    return value


@app.before_request
def _make_session_permanent():
    session.permanent = True

# Public base URL for building absolute links in emails (e.g. staff invites).
# On Render, set this to https://pillbox-pharmacy.onrender.com so links aren't "localhost".
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")


def external_url(endpoint, **values):
    """Build an absolute URL, forcing the public domain when PUBLIC_BASE_URL is set."""
    if PUBLIC_BASE_URL:
        return PUBLIC_BASE_URL + url_for(endpoint, **values)
    return url_for(endpoint, _external=True, **values)


UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), "static", "uploads")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}

PRODUCTS_FILE      = os.path.join(os.path.dirname(__file__), "products.json")
ORDERS_FILE        = os.path.join(os.path.dirname(__file__), "orders.json")
USERS_FILE         = os.path.join(os.path.dirname(__file__), "users.json")
STAFF_FILE         = os.path.join(os.path.dirname(__file__), "staff.json")
ADS_FILE           = os.path.join(os.path.dirname(__file__), "ads.json")
APPOINTMENTS_FILE  = os.path.join(os.path.dirname(__file__), "appointments.json")

BRANCHES = [
    {"id": "great_george", "name": "Main Branch — 87 Great George Street", "short": "87 Great George St"},
    {"id": "lewis_street", "name": "Second Branch — 17 Lewis Street",       "short": "17 Lewis St"},
]

APPOINTMENT_REASONS = [
    "Prescription Consultation",
    "Medication Review",
    "Blood Pressure Check",
    "General Health Inquiry",
    "Book Order / Stationery Enquiry",
    "Product Availability Query",
    "Insurance / Billing Query",
    "Other",
]

CATEGORIES = {
    "all": "All Products",
    "pharmaceutical": "Pharmaceutical",
    "otc": "Over-the-Counter (OTC)",
    "pharmacy": "Pharmacy & Health",
    "braces": "Braces & Supports",
    "personal_care": "Personal Care",
    "perfumes": "Perfumes & Fragrances",
    "books": "Books",
    "stationery": "Office & Stationery",
}

# Medicine/health-themed placeholder shown for products the admin hasn't
# uploaded a photo for yet. Inline SVG data-URI so it works offline with no asset file.
PLACEHOLDER_IMG = (
    "data:image/svg+xml;utf8,"
    "<svg xmlns='http://www.w3.org/2000/svg' width='400' height='400' viewBox='0 0 400 400'>"
    "<rect width='400' height='400' fill='%23eef2f7'/>"
    "<g transform='translate(200,185)'>"
    "<circle r='78' fill='none' stroke='%23b9c6d6' stroke-width='10'/>"
    "<rect x='-46' y='-14' width='92' height='28' rx='14' fill='%237c5cff'/>"
    "<rect x='-14' y='-46' width='28' height='92' rx='14' fill='%237c5cff'/>"
    "</g>"
    "<text x='200' y='320' font-family='Arial,sans-serif' font-size='24' fill='%238a97a8' "
    "text-anchor='middle'>No photo yet</text></svg>"
)

DISCOUNT_OPTIONS = [0, 5, 10, 15, 20, 25, 30, 40, 50]

DEFAULT_STAFF = [
    {"id": "s1", "name": "Admin User",  "username": "admin",  "password": "pillbox",    "role": "admin"},
    {"id": "s2", "name": "Marcus Reid", "username": "marcus", "password": "deliver123", "role": "delivery"},
]

# Initialise the database and import any existing JSON data (one-time, safe to
# re-run — it won't overwrite tables that already have data).
db.migrate_from_json(os.path.dirname(__file__), default_staff=DEFAULT_STAFF)


# ── Product persistence ──────────────────────────────────────────────────────

def load_products():
    return db.read_list("products")


def save_products(products):
    db.write_list("products", products)


def _selling_price(base, markup):
    """Customer-facing selling price = base cost + markup%."""
    return round(float(base) * (1 + float(markup) / 100), 2)


def get_products():
    """Return products with base_price, markup, original_price, effective_price
    and a normalised images list."""
    products = load_products()
    for p in products:
        # Backfill base/markup for legacy products: current price = base, 0% markup
        if "base_price" not in p:
            p["base_price"] = p.get("price", 0)
        if "markup" not in p:
            p["markup"] = 0
        # Selling price is derived from base + markup (kept in sync on p["price"])
        p["price"] = _selling_price(p["base_price"], p["markup"])

        disc = p.get("discount", 0)
        p["original_price"] = p["price"]
        p["effective_price"] = round(p["price"] * (1 - disc / 100), 2) if disc else p["price"]
        # Normalise: always expose p["images"] as a list
        if not p.get("images"):
            p["images"] = [p["img"]] if p.get("img") else []
        # Fall back to a medicine-themed placeholder until the admin adds a real photo.
        if not p.get("img"):
            p["img"] = PLACEHOLDER_IMG
        if not p["images"]:
            p["images"] = [PLACEHOLDER_IMG]
    return products


def next_id(lst):
    return max((p["id"] for p in lst), default=0) + 1


# ── Orders persistence ───────────────────────────────────────────────────────

def load_orders():
    return db.read_list("orders")


def save_orders(orders):
    db.write_list("orders", orders)


# ── Users persistence ────────────────────────────────────────────────────────

def load_users():
    return db.read_users()


def save_users(users):
    db.write_users(users)


# ── Staff persistence ────────────────────────────────────────────────────────

def load_staff():
    staff = db.read_list("staff")
    return staff if staff else DEFAULT_STAFF[:]


def save_staff(staff):
    db.write_list("staff", staff)


# ── Misc helpers ─────────────────────────────────────────────────────────────

def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def get_cart():
    return session.get("cart", {})


def cart_count():
    return sum(item["qty"] for item in get_cart().values())


def cart_total():
    return sum(item["price"] * item["qty"] for item in get_cart().values())


def load_appointments():
    return db.read_list("appointments")

def save_appointments(appts):
    db.write_list("appointments", appts)

def load_ads():
    return db.read_list("ads")

def save_ads(ads):
    db.write_list("ads", ads)

# How many ads can run simultaneously (effectively unlimited for the shop's needs).
MAX_ACTIVE_ADS = 50

def get_active_ads():
    """Return all ads whose expiry hasn't passed, with full product data attached."""
    ads = load_ads()
    products = get_products()
    now = datetime.datetime.now().isoformat()
    active = []
    for ad in ads:
        if ad.get("expires_at", "") > now:
            prod = next((p for p in products if p["id"] == ad["product_id"]), None)
            if prod:
                active.append({"ad": ad, "product": prod})
    return active[:MAX_ACTIVE_ADS]

def gen_code():
    """Generate 6-char confirmation code like PB4821."""
    return "PB" + "".join(random.choices(string.digits, k=4))


# Synonym/keyword map — maps common search terms to product name fragments or categories
SEARCH_SYNONYMS = {
    # Body / pain
    "pain": ["aspirin", "ibuprofen", "paracetamol", "bandage", "brace"],
    "fever": ["aspirin", "paracetamol", "cough"],
    "headache": ["aspirin", "ibuprofen"],
    "cold": ["cough", "allergy", "vitamin"],
    "flu": ["cough", "vitamin", "allergy"],
    "cough": ["cough"],
    "sneeze": ["allergy"],
    "allergy": ["allergy"],
    "condom": ["family", "contraceptive", "planning"],
    "contraceptive": ["family", "planning"],
    "family planning": ["family", "planning"],
    "safe sex": ["family", "planning"],
    "co": ["cough", "contraceptive"],
    # Pharmacy
    "medicine": ["aspirin", "cough", "allergy", "vitamin"],
    "tablet": ["aspirin", "allergy", "vitamin"],
    "pill": ["aspirin", "vitamin", "allergy"],
    "syrup": ["cough"],
    "vitamin": ["vitamin"],
    "immune": ["vitamin"],
    "blood pressure": ["blood pressure", "monitor"],
    "bp": ["blood pressure", "monitor"],
    "heart": ["blood pressure", "monitor", "cardio", "cholesterol", "aspirin"],
    "cardio": ["blood pressure", "monitor", "cholesterol"],
    "cholesterol": ["cholesterol", "statin", "heart"],
    "hypertension": ["blood pressure", "monitor"],
    "bandage": ["bandage"],
    "wound": ["bandage"],
    # Health conditions — map a condition to the products that treat/relate to it
    "diabetes": ["diabetes", "diabetic", "sugar", "glucose", "insulin", "metformin", "monitor"],
    "diabetic": ["diabetes", "diabetic", "glucose", "insulin", "metformin"],
    "sugar": ["diabetes", "diabetic", "glucose", "insulin", "sugar"],
    "glucose": ["diabetes", "glucose", "monitor", "insulin"],
    "insulin": ["insulin", "diabetes"],
    "bone": ["bone", "calcium", "joint", "osteo", "vitamin d", "brace"],
    "calcium": ["calcium", "bone", "vitamin d"],
    "joint": ["joint", "bone", "arthritis", "brace", "knee", "compression"],
    "arthritis": ["arthritis", "joint", "ibuprofen", "brace"],
    "osteoporosis": ["bone", "calcium", "osteo", "vitamin d"],
    "muscle": ["joint", "compression", "brace", "ibuprofen"],
    "stomach": ["antacid", "digestive", "nausea"],
    "acid": ["antacid", "digestive"],
    "digestion": ["antacid", "digestive"],
    "nausea": ["nausea", "antacid"],
    "diarrhea": ["digestive", "oral rehydration"],
    "eye": ["eye", "vision", "drops"],
    "vision": ["eye", "vision"],
    "ear": ["ear", "drops"],
    "asthma": ["inhaler", "asthma", "allergy"],
    "breathing": ["inhaler", "asthma"],
    "thyroid": ["thyroid"],
    "anemia": ["iron", "vitamin"],
    "iron": ["iron", "vitamin"],
    "sleep": ["sleep", "melatonin"],
    "stress": ["score", "sleep", "vitamin"],
    "diabetes test": ["glucose", "monitor", "diabetes"],
    # Braces
    "ankle": ["ankle", "brace"],
    "sprain": ["ankle", "brace", "wrist"],
    "knee": ["knee", "compression"],
    "wrist": ["wrist", "brace"],
    "support": ["ankle", "knee", "wrist", "brace"],
    "brace": ["brace"],
    "compression": ["compression", "sleeve"],
    "foot": ["ankle", "brace"],
    "injury": ["ankle", "knee", "wrist", "brace", "bandage"],
    "sport": ["ankle", "knee", "wrist"],
    # Personal care
    "deodorant": ["deodorant"],
    "deo": ["deodorant"],
    "body wash": ["body wash"],
    "soap": ["body wash", "dove"],
    "toothpaste": ["toothpaste", "sensodyne"],
    "teeth": ["toothpaste", "sensodyne"],
    "sensitive": ["sensodyne"],
    "hygiene": ["deodorant", "body wash", "toothpaste"],
    "skin": ["body wash", "dove"],
    "smell": ["deodorant", "perfume", "fragrance"],
    # Perfumes
    "perfume": ["perfume", "fragrance", "oud", "chanel", "acqua"],
    "fragrance": ["fragrance", "oud", "chanel", "acqua"],
    "cologne": ["acqua", "fragrance"],
    "scent": ["perfume", "fragrance", "deodorant"],
    "spray": ["perfume", "body mist", "deodorant"],
    "body mist": ["mist", "oud"],
    "chanel": ["chanel"],
    "rose": ["rose", "oud"],
    "oud": ["oud"],
    # Books
    "book": ["anatomy", "score", "healthy", "living"],
    "anatomy": ["anatomy"],
    "health": ["healthy", "living", "vitamin", "blood pressure"],
    "medical": ["anatomy", "blood pressure"],
    "read": ["anatomy", "score", "healthy"],
    "study": ["anatomy"],
    "kids": ["healthy", "living"],
    "mental": ["score"],
    "trauma": ["score"],
    # Stationery
    "pen": ["pen", "pilot"],
    "stationery": ["pen", "notebook", "sticky", "stapler"],
    "office": ["pen", "notebook", "sticky", "stapler"],
    "notebook": ["notebook"],
    "paper": ["notebook"],
    "sticky": ["sticky"],
    "note": ["sticky", "notebook"],
    "stapler": ["stapler"],
    "staple": ["stapler"],
    "school": ["pen", "notebook", "sticky"],
    "write": ["pen", "notebook"],
}


def search_products_ranked(query: str, products: list, limit: int = 3) -> list:
    """Return products ranked by relevance to query. Used for autocomplete."""
    q = query.strip().lower()
    if not q:
        return []

    # Expand query with synonyms. The literal query is a stronger signal than
    # synonym-expanded terms, so we keep them in separate buckets.
    synonym_terms = []
    for key, vals in SEARCH_SYNONYMS.items():
        if q in key or key in q or key.startswith(q) or q.startswith(key):
            synonym_terms.extend(vals)
    synonym_terms = [t for t in dict.fromkeys(synonym_terms) if t != q]  # de-dupe, drop the query itself

    # Tiered scoring so a NAME match always outranks a DESCRIPTION-only match.
    # Name hits live in a high band (>=1000); description-only hits stay below it.
    # Within each band, a literal-query hit beats a synonym hit.
    def term_score(term, name_l, gen_l, desc_l, cat_l, is_query):
        q_boost = 2 if is_query else 1  # literal query weighs double vs. a synonym
        name_pts = 0
        if term == name_l:
            name_pts = 500          # exact name
        elif name_l.startswith(term):
            name_pts = 300          # name starts with term
        elif term in name_l:
            name_pts = 200          # name contains term
        # Generic / "also known as" name — treated like the name, just a notch lower,
        # so searching "Pioglitazone" surfaces a product named "PIOZ".
        if gen_l:
            if term == gen_l:
                name_pts = max(name_pts, 450)
            elif gen_l.startswith(term):
                name_pts = max(name_pts, 280)
            elif term in gen_l:
                name_pts = max(name_pts, 180)
        if name_pts:
            return 1000 + name_pts * q_boost  # NAME band

        other = 0
        if term in desc_l:
            other += 20 * q_boost   # description — last resort
        if term in cat_l:
            other += 15 * q_boost   # category
        return other                 # stays below the 1000 name band

    scores = {}
    for p in products:
        name_l = p["name"].lower()
        gen_l  = str(p.get("generic_name", "")).lower()
        desc_l = p["description"].lower()
        cat_l  = p["category"].lower()

        best = 0
        for term in [q]:
            best = max(best, term_score(term, name_l, gen_l, desc_l, cat_l, is_query=True))
        for term in synonym_terms:
            best = max(best, term_score(term, name_l, gen_l, desc_l, cat_l, is_query=False))

        if best > 0:
            scores[p["id"]] = best

    result = [p for p in products if p["id"] in scores]
    result.sort(key=lambda p: scores[p["id"]], reverse=True)
    return result[:limit]


# ── Context processor ────────────────────────────────────────────────────────

@app.context_processor
def inject_globals():
    uid = session.get("user_id")
    current_user = None
    if uid:
        try:
            users = load_users()
            current_user = users.get(str(uid))
        except Exception:
            pass
    dw_id   = session.get("delivery_worker_id")
    dw_name = session.get("delivery_worker_name")
    # Pending appointment count for admin notification badge
    pending_appts = 0
    if session.get("is_admin"):
        try:
            pending_appts = sum(1 for a in load_appointments() if a.get("status") == "pending")
        except Exception:
            pass
    return {
        "cart_count":       cart_count(),
        "cart_total":       round(cart_total(), 2),
        "is_admin":         session.get("is_admin", False),
        "is_delivery":      session.get("is_delivery", False),
        "current_user":     current_user,
        "delivery_worker":  {"id": dw_id, "name": dw_name} if dw_id else None,
        "pending_appts":    pending_appts,
        "branches":         BRANCHES,
        "appt_reasons":     APPOINTMENT_REASONS,
    }


# ── Decorators ───────────────────────────────────────────────────────────────

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("is_admin"):
            flash("Please log in as admin.", "error")
            return redirect(url_for("admin_login"))
        return f(*args, **kwargs)
    return decorated


def delivery_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("is_delivery"):
            return redirect(url_for("delivery_login"))
        return f(*args, **kwargs)
    return decorated


# ── Public routes ────────────────────────────────────────────────────────────

@app.route("/api/search-suggest")
def search_suggest():
    """Live autocomplete API — returns up to 3 ranked products as JSON."""
    q = request.args.get("q", "").strip()
    if len(q) < 1:
        return jsonify([])
    # Log the search term (even partial) so admin can see what people look for.
    _log_search(q)
    products = get_products()
    results = search_products_ranked(q, products, limit=3)
    payload = [
        {
            "id":             p["id"],
            "name":           p["name"],
            "img":            p["img"],
            "price":          p["effective_price"],
            "original_price": p["original_price"],
            "discount":       p.get("discount", 0),
            "category":       CATEGORIES.get(p["category"], p["category"]),
            "url":            url_for("product", product_id=p["id"]),
        }
        for p in results
    ]
    return jsonify(payload)


@app.route("/")
def index():
    products = get_products()

    # ── Recommendation feed ─────────────────────────────────────────────────
    # Rank the WHOLE catalogue so similar items surface first and keep
    # surfacing until that category runs out — then new arrivals / deals.
    history = session.get("view_history", [])   # product ids, most-recent first

    # Weight the categories the guest has been browsing: the more recently /
    # more often a category was viewed, the higher its weight.
    cat_weight = {}
    for rank, pid in enumerate(history[:10]):
        p = next((x for x in products if x["id"] == pid), None)
        if p:
            # most recent view worth the most; older views add a little more
            cat_weight[p["category"]] = cat_weight.get(p["category"], 0) + (10 - rank)

    max_id = max((p["id"] for p in products), default=1)

    def score(p):
        s = 0.0
        # 1) Similar to what they viewed — the core of "show me more like this"
        s += cat_weight.get(p["category"], 0) * 5
        # 2) New arrivals are tempting even with no history (higher id = newer)
        s += (p["id"] / max_id) * 6
        # 3) Discounts are tempting
        s += min(p.get("discount", 0), 50) * 0.3
        # 4) Push the exact items they just opened slightly down so the feed
        #    feels fresh (similar, not identical)
        if p["id"] in history[:2]:
            s -= 8
        return s

    featured = sorted(products, key=score, reverse=True)   # ALL products, ranked
    personalised = bool(history)

    cat_images = {}
    for key in CATEGORIES:
        if key == "all":
            continue
        match = next((p["img"] for p in products if p["category"] == key), None)
        if match:
            cat_images[key] = match

    # ── Deals row: any product the admin has discounted ────────────────────
    deals = sorted(
        (p for p in products if p.get("discount", 0) > 0),
        key=lambda p: p["discount"], reverse=True
    )[:8]

    return render_template("index.html", featured=featured, categories=CATEGORIES,
                           cat_images=cat_images, personalised=personalised,
                           deals=deals)


@app.route("/shop")
def shop():
    category = request.args.get("category", "all")
    search = request.args.get("search", "").lower()
    products = get_products()
    if category != "all":
        products = [p for p in products if p["category"] == category]
    if search:
        # Use the same ranked search as the autocomplete so results honour
        # name-first ordering, generic names, and the keyword/synonym map.
        products = search_products_ranked(search, products, limit=len(products))
    return render_template("shop.html", products=products, categories=CATEGORIES, active_category=category, search=search)


@app.route("/product/<int:product_id>")
def product(product_id):
    products = get_products()
    prod = next((p for p in products if p["id"] == product_id), None)
    if not prod:
        flash("Product not found.", "error")
        return redirect(url_for("shop"))

    # Record view in session history (keep last 20, most recent first, no dupes)
    history = session.get("view_history", [])
    if product_id in history:
        history.remove(product_id)
    history.insert(0, product_id)
    session["view_history"] = history[:20]

    related = _related_products(prod, products, 0, 8)
    return render_template("product.html", product=prod, related=related)


def _related_products(prod, products, offset, limit):
    """Ordered 'you may also like' list that NEVER runs dry:
    same-category first, then everything else, looped — so infinite scroll
    always has more to show."""
    same = [p for p in products if p["category"] == prod["category"] and p["id"] != prod["id"]]
    other = [p for p in products if p["category"] != prod["category"]]
    ordered = same + other
    if not ordered:
        return []
    # Wrap around so the feed is effectively endless.
    n = len(ordered)
    return [ordered[(offset + i) % n] for i in range(limit)]


@app.route("/api/related/<int:product_id>")
def api_related(product_id):
    products = get_products()
    prod = next((p for p in products if p["id"] == product_id), None)
    if not prod:
        return jsonify([])
    offset = int(request.args.get("offset", 0))
    limit = min(int(request.args.get("limit", 8)), 16)
    items = _related_products(prod, products, offset, limit)
    return jsonify([
        {
            "id": p["id"],
            "name": p["name"],
            "img": p["img"],
            "price": p["effective_price"],
            "discount": p.get("discount", 0),
            "original_price": p["original_price"],
            "url": url_for("product", product_id=p["id"]),
        }
        for p in items
    ])


@app.route("/cart")
def cart():
    cart_items = list(get_cart().values())
    total = round(cart_total(), 2)
    return render_template("cart.html", cart_items=cart_items, total=total)


@app.route("/add_to_cart/<int:product_id>", methods=["POST"])
def add_to_cart(product_id):
    products = get_products()
    prod = next((p for p in products if p["id"] == product_id), None)
    if not prod:
        return jsonify({"error": "Product not found"}), 404
    c = get_cart()
    pid = str(product_id)
    sale_price = prod["effective_price"]
    if pid in c:
        c[pid]["qty"] += 1
    else:
        c[pid] = {"id": prod["id"], "name": prod["name"], "price": sale_price,
                  "img": prod["img"], "qty": 1}
    session["cart"] = c
    return jsonify({"count": cart_count(), "message": "Added to cart"})


def _is_ajax():
    return request.headers.get("X-Requested-With") == "XMLHttpRequest"


def _cart_summary():
    """Totals used to refresh the cart page in place after an AJAX action."""
    total = round(cart_total(), 2)
    shipping = 0 if total >= 5000 else (500 if total > 0 else 0)
    tax = round(total * 0.15, 2)
    return {
        "count":    cart_count(),
        "items":    len(get_cart()),
        "subtotal": total,
        "shipping": shipping,
        "tax":      tax,
        "grand":    round(total + shipping + tax, 2),
    }


@app.route("/remove_from_cart/<int:product_id>", methods=["POST"])
def remove_from_cart(product_id):
    c = get_cart()
    pid = str(product_id)
    if pid in c:
        del c[pid]
        session["cart"] = c
    if _is_ajax():
        return jsonify(_cart_summary())
    return redirect(url_for("cart"))


@app.route("/update_cart/<int:product_id>", methods=["POST"])
def update_cart(product_id):
    qty = int(request.form.get("qty", 1))
    c = get_cart()
    pid = str(product_id)
    if pid in c:
        if qty <= 0:
            del c[pid]
        else:
            c[pid]["qty"] = qty
    session["cart"] = c
    if _is_ajax():
        summary = _cart_summary()
        pdata = get_cart().get(pid)
        summary["line_subtotal"] = round(pdata["price"] * pdata["qty"], 2) if pdata else 0
        return jsonify(summary)
    return redirect(url_for("cart"))


@app.route("/checkout", methods=["GET", "POST"])
def checkout():
    if request.method == "POST":
        first_name  = request.form.get("first_name", "").strip()
        last_name   = request.form.get("last_name", "").strip()
        email       = request.form.get("email", "").strip()
        phone       = request.form.get("phone", "").strip()
        address     = request.form.get("address", "").strip()
        parish      = request.form.get("parish", "").strip()
        fulfillment = request.form.get("fulfillment", "delivery")

        subtotal = round(cart_total(), 2)
        shipping = 0 if subtotal >= 5000 or fulfillment == "pickup" else 500
        tax      = round(subtotal * 0.15, 2)
        total    = round(subtotal * 1.15 + shipping, 2)

        payment_method = request.form.get("payment_method", "cash")
        pay_by_card = payment_method == "card" and payments.is_configured()

        order = {
            "id":                  "ORD-" + "".join(random.choices(string.digits, k=4)),
            "customer_name":       first_name + " " + last_name,
            "customer_email":      email,
            "customer_phone":      phone,
            "items":               list(get_cart().values()),
            "subtotal":            subtotal,
            "shipping":            shipping,
            "tax":                 tax,
            "total":               total,
            "fulfillment":         fulfillment,
            "address":             address if fulfillment == "delivery" else "In-Store Pickup",
            "parish":              parish,
            # Card orders start unpaid until Stripe confirms; cash orders go straight to processing.
            "status":              "pending_payment" if pay_by_card else "processing",
            "payment_method":      "card" if pay_by_card else "cash",
            "payment_status":      "unpaid" if pay_by_card else "not_required",
            "pickup_code":         gen_code(),
            "delivery_worker_id":  None,
            "created_at":          datetime.datetime.now().isoformat(),
            "delivered_at":        None,
            "notes":               "",
        }

        orders = load_orders()
        orders.append(order)
        save_orders(orders)

        if pay_by_card:
            # Redirect to Stripe's hosted payment page. Cart stays until payment
            # succeeds so an abandoned payment doesn't lose the customer's basket.
            session["pending_order_id"] = order["id"]
            success_url = external_url("checkout_success") + "?session_id={CHECKOUT_SESSION_ID}"
            cancel_url = external_url("checkout_cancel")
            pay_url = payments.create_checkout_session(order, success_url, cancel_url)
            if pay_url:
                return redirect(pay_url)
            # Stripe failed — fall back to placing the order as unpaid so nothing is lost
            flash("Card payment is temporarily unavailable. Your order was placed — please pay on collection.", "error")
            order["status"] = "processing"
            order["payment_status"] = "not_required"
            save_orders(orders)

        # Cash / fallback path — confirm immediately
        _reduce_stock_for_order(order)
        emails.send_order_confirmation(order)
        session["cart"] = {}
        session["last_order_id"] = order["id"]
        flash("Order placed successfully! Thank you for shopping at The PillBox Pharmacy.", "success")
        return redirect(url_for("order_confirmation"))

    total = round(cart_total(), 2)
    cart_items = list(get_cart().values())
    return render_template("checkout.html", cart_items=cart_items, total=total,
                           stripe_enabled=payments.is_configured())


@app.route("/checkout/success")
def checkout_success():
    """Return URL after a successful Stripe payment. Verify, mark paid, confirm."""
    session_id = request.args.get("session_id", "")
    order_id = session.get("pending_order_id")
    orders = load_orders()
    order = next((o for o in orders if o["id"] == order_id), None)

    stripe_session = payments.retrieve_session(session_id)
    paid = stripe_session and stripe_session.get("payment_status") == "paid"

    if order and paid:
        order["status"] = "processing"
        order["payment_status"] = "paid"
        order["stripe_session_id"] = session_id
        save_orders(orders)
        _reduce_stock_for_order(order)
        emails.send_order_confirmation(order)
        session["cart"] = {}
        session.pop("pending_order_id", None)
        session["last_order_id"] = order["id"]
        flash("Payment successful! Thank you for shopping at The PillBox Pharmacy.", "success")
        return redirect(url_for("order_confirmation"))

    flash("We couldn't confirm your payment. If you were charged, please contact us.", "error")
    return redirect(url_for("cart"))


@app.route("/checkout/cancel")
def checkout_cancel():
    """Return URL if the customer backs out of the Stripe payment page."""
    order_id = session.pop("pending_order_id", None)
    if order_id:
        # Drop the unpaid pending order so it doesn't clutter the admin.
        orders = load_orders()
        orders = [o for o in orders if not (o["id"] == order_id and o.get("payment_status") == "unpaid")]
        save_orders(orders)
    flash("Payment cancelled — your cart is still here whenever you're ready.", "error")
    return redirect(url_for("cart"))


@app.route("/order-confirmation")
def order_confirmation():
    order_id = session.get("last_order_id")
    order = None
    if order_id:
        orders = load_orders()
        order = next((o for o in orders if o["id"] == order_id), None)
    return render_template("order_confirmation.html", order=order)


@app.route("/about")
def about():
    return render_template("about.html")


@app.route("/contact", methods=["GET", "POST"])
def contact():
    if request.method == "POST":
        flash("Message sent! We'll get back to you within 24 hours.", "success")
        return redirect(url_for("contact"))
    return render_template("contact.html")


# ── Auth routes ──────────────────────────────────────────────────────────────

def normalize_phone(raw):
    """Keep only digits so '(876) 123-4567' and '8761234567' match."""
    return "".join(ch for ch in (raw or "") if ch.isdigit())


def find_user_by_phone(users, phone):
    phone = normalize_phone(phone)
    if not phone:
        return None
    return next((u for u in users.values() if normalize_phone(u.get("phone")) == phone), None)


# Email/password pages now just send users to the home welcome flow.
@app.route("/login")
def login():
    return redirect(url_for("index"))


@app.route("/register")
def register():
    return redirect(url_for("index"))


@app.route("/auth/signup", methods=["POST"])
def auth_signup():
    name  = request.form.get("name", "").strip()
    phone = request.form.get("phone", "").strip()
    pin   = request.form.get("pin", "").strip()
    nphone = normalize_phone(phone)
    if not name or not nphone:
        return jsonify({"success": False, "message": "Please enter your name and phone number."}), 400
    if len(nphone) < 7:
        return jsonify({"success": False, "message": "Please enter a valid phone number."}), 400
    if not (pin.isdigit() and len(pin) == 4):
        return jsonify({"success": False, "message": "PIN must be 4 digits."}), 400
    users = load_users()
    if find_user_by_phone(users, nphone):
        return jsonify({"success": False, "message": "An account with that phone number already exists."}), 409
    uid = str(uuid.uuid4())
    users[uid] = {
        "id": uid, "name": name, "phone": nphone,
        "pin": generate_password_hash(pin),
        "email": None, "avatar": None,
        "whatsapp": nphone, "gender": "", "address": "",
    }
    save_users(users)
    session["user_id"] = uid
    return jsonify({"success": True})


@app.route("/auth/signin", methods=["POST"])
def auth_signin():
    phone = request.form.get("phone", "").strip()
    pin   = request.form.get("pin", "").strip()
    users = load_users()
    user  = find_user_by_phone(users, phone)
    if not user or not user.get("pin") or not check_password_hash(user["pin"], pin):
        return jsonify({"success": False, "message": "Incorrect phone number or PIN."}), 401
    session["user_id"] = user["id"]
    return jsonify({"success": True})


@app.route("/auth/check-phone", methods=["POST"])
def auth_check_phone():
    """Tell the UI whether a phone already has an account (sign in vs sign up)."""
    users = load_users()
    exists = find_user_by_phone(users, request.form.get("phone", "")) is not None
    return jsonify({"exists": exists})


@app.route("/logout")
def logout():
    session.pop("user_id", None)
    session.pop("guest", None)
    flash("You've been logged out.", "success")
    return redirect(url_for("index"))


@app.route("/profile/avatar", methods=["POST"])
def upload_avatar():
    uid = session.get("user_id")
    if not uid:
        return jsonify({"error": "Not logged in"}), 401
    file = request.files.get("avatar")
    if not file or not allowed_file(file.filename):
        return jsonify({"error": "Invalid file"}), 400
    ext = file.filename.rsplit(".", 1)[1].lower()
    filename = f"avatar_{uid}.{ext}"
    file.save(os.path.join(UPLOAD_FOLDER, filename))
    users = load_users()
    users[str(uid)]["avatar"] = filename
    save_users(users)
    return jsonify({"success": True, "avatar": filename})


@app.route("/profile", methods=["POST"])
def update_profile():
    uid = session.get("user_id")
    if not uid:
        return jsonify({"error": "Not logged in"}), 401
    users = load_users()
    user = users.get(str(uid))
    if not user:
        return jsonify({"error": "Not found"}), 404
    for field in ("name", "whatsapp", "gender", "address"):
        if field in request.form:
            user[field] = request.form.get(field, "").strip()
    save_users(users)
    return jsonify({"success": True})


# ── Admin routes ─────────────────────────────────────────────────────────────

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()
        staff = load_staff()
        admin = next((s for s in staff if s["username"] == username and s.get("password") == password
                      and password and s["role"] == "admin" and s.get("status", "active") == "active"), None)
        # Fallback: also check legacy hardcoded credentials
        if admin or (username == "MORGAN" and password == "NEVER"):
            session["is_admin"] = True
            flash("Welcome back, Admin!", "success")
            return redirect(url_for("admin_overview"))
        flash("Invalid username or password.", "error")
    return render_template("admin/login.html")


@app.route("/admin/logout")
def admin_logout():
    session.pop("is_admin", None)
    flash("Logged out successfully.", "success")
    return redirect(url_for("index"))


@app.route("/admin")
@admin_required
def admin_overview():
    """Business overview — the 'at a glance' landing page for the store owner."""
    orders   = load_orders()
    products = get_products()
    appts    = load_appointments()
    now      = datetime.datetime.now()
    today    = now.date().isoformat()

    # ── Revenue ──────────────────────────────────────────────────────────────
    # Count an order's money once it's paid or has progressed past pending payment.
    def is_revenue(o):
        return o.get("status") not in ("pending_payment", "cancelled", "refunded")

    revenue_orders = [o for o in orders if is_revenue(o)]

    def order_day(o):
        try:
            return datetime.datetime.fromisoformat(o.get("created_at", "")).date().isoformat()
        except Exception:
            return ""

    today_revenue = sum(o["total"] for o in revenue_orders if order_day(o) == today)
    today_orders  = sum(1 for o in orders if order_day(o) == today)
    total_revenue = sum(o["total"] for o in revenue_orders)

    # Last 7 days revenue for a mini trend
    sales_by_day = {}
    for i in range(6, -1, -1):
        d = (now - datetime.timedelta(days=i))
        sales_by_day[d.strftime("%a")] = 0.0
    for o in revenue_orders:
        try:
            d = datetime.datetime.fromisoformat(o["created_at"])
            key = d.strftime("%a")
            if (now - d).days <= 6 and key in sales_by_day:
                sales_by_day[key] += o["total"]
        except Exception:
            pass
    max_day = max(sales_by_day.values()) if sales_by_day else 0

    # ── Orders needing attention ─────────────────────────────────────────────
    needs_attention = [o for o in orders if o.get("status") in ("processing", "out_for_delivery")]
    awaiting_payment = sum(1 for o in orders if o.get("status") == "pending_payment")
    recent_orders = sorted(orders, key=lambda o: o.get("created_at", ""), reverse=True)[:6]

    status_counts = {}
    for o in orders:
        status_counts[o["status"]] = status_counts.get(o["status"], 0) + 1

    # ── Inventory / low stock ────────────────────────────────────────────────
    # Products carry an optional "stock" count; flag anything at/under threshold.
    LOW_STOCK_THRESHOLD = 5
    low_stock = [p for p in products if isinstance(p.get("stock"), int) and p["stock"] <= LOW_STOCK_THRESHOLD]
    out_of_stock = [p for p in products if p.get("stock") == 0]

    # ── Appointments ─────────────────────────────────────────────────────────
    pending_appt_list = [a for a in appts if a.get("status") == "pending"]
    upcoming_appts = sorted(
        [a for a in appts if a.get("date", "") >= today and a.get("status") in ("pending", "confirmed")],
        key=lambda a: (a.get("date", ""), a.get("time", "")),
    )[:5]

    return render_template(
        "admin/overview.html",
        today_revenue=today_revenue,
        today_orders=today_orders,
        total_revenue=total_revenue,
        total_orders=len(orders),
        sales_by_day=sales_by_day,
        max_day=max_day,
        needs_attention=needs_attention,
        awaiting_payment=awaiting_payment,
        recent_orders=recent_orders,
        status_counts=status_counts,
        low_stock=low_stock,
        out_of_stock=out_of_stock,
        low_stock_threshold=LOW_STOCK_THRESHOLD,
        total_products=len(products),
        pending_appt_count=len(pending_appt_list),
        upcoming_appts=upcoming_appts,
        categories=CATEGORIES,
    )


@app.route("/admin/products")
@admin_required
def admin_dashboard():
    products = get_products()
    total_products = len(products)
    discounted = sum(1 for p in products if p.get("discount", 0) > 0)
    categories_count = {k: sum(1 for p in products if p["category"] == k) for k in CATEGORIES if k != "all"}
    return render_template("admin/dashboard.html", products=products, categories=CATEGORIES,
                           total_products=total_products, discounted=discounted,
                           categories_count=categories_count, discount_options=DISCOUNT_OPTIONS)


def _save_product_image(file, product_id, slot):
    """Save one uploaded product image for a given slot (0-3). Returns URL path or None."""
    if not file or file.filename == "":
        return None
    if not allowed_file(file.filename):
        return None
    ext = file.filename.rsplit(".", 1)[1].lower()
    filename = f"product_{product_id}_s{slot}.{ext}"
    file.save(os.path.join(UPLOAD_FOLDER, filename))
    return url_for("static", filename=f"uploads/{filename}", _external=False)


def _resolve_images(request, product_id, existing_images=None):
    """
    Build the images list (up to 4) by merging:
    - uploaded files  (img_file_0 … img_file_3)
    - URL text inputs (img_url_0  … img_url_3)
    - existing images already stored (kept when slot is left blank)
    Returns (images_list, primary_img_str)
    """
    existing = existing_images or []
    images = []
    for slot in range(4):
        file    = request.files.get(f"img_file_{slot}")
        url_val = request.form.get(f"img_url_{slot}", "").strip()
        keep    = request.form.get(f"img_keep_{slot}", "")  # hidden: existing URL

        uploaded = _save_product_image(file, product_id, slot)
        if uploaded:
            images.append(uploaded)
        elif url_val:
            images.append(url_val)
        elif keep:
            images.append(keep)
        # else: slot is empty — skip

    # Remove blank entries
    images = [i for i in images if i]

    # Primary image = first one
    primary = images[0] if images else ""
    return images, primary


@app.route("/admin/product/add", methods=["GET", "POST"])
@admin_required
def admin_add_product():
    if request.method == "POST":
        products = load_products()
        pid = next_id(products)
        images, primary = _resolve_images(request, pid)
        base   = round(float(request.form.get("base_price") or request.form.get("price") or 0), 2)
        markup = float(request.form.get("markup", 0) or 0)
        new_product = {
            "id":          pid,
            "name":        request.form["name"].strip(),
            "base_price":  base,
            "markup":      markup,
            "price":       _selling_price(base, markup),   # selling price = base + markup%
            "category":    request.form["category"],
            "img":         primary,
            "images":      images,
            "generic_name": request.form.get("generic_name", "").strip(),
            "description": request.form["description"].strip(),
            "discount":    int(request.form.get("discount", 0)),
            "stock":       max(0, int(request.form.get("stock", 0) or 0)),
        }
        products.append(new_product)
        save_products(products)
        flash(f'Product "{new_product["name"]}" added successfully!', "success")
        return redirect(url_for("admin_dashboard"))
    return render_template("admin/product_form.html", product=None, categories=CATEGORIES,
                           discount_options=DISCOUNT_OPTIONS)


@app.route("/admin/product/edit/<int:product_id>", methods=["GET", "POST"])
@admin_required
def admin_edit_product(product_id):
    products = load_products()
    prod = next((p for p in products if p["id"] == product_id), None)
    if not prod:
        flash("Product not found.", "error")
        return redirect(url_for("admin_dashboard"))
    if request.method == "POST":
        existing = prod.get("images", [prod.get("img", "")] if prod.get("img") else [])
        images, primary = _resolve_images(request, product_id, existing)
        if not primary:
            primary = prod.get("img", "")  # fallback: keep old primary
        prod["name"]        = request.form["name"].strip()
        prod["base_price"]  = round(float(request.form.get("base_price") or request.form.get("price") or 0), 2)
        prod["markup"]      = float(request.form.get("markup", 0) or 0)
        prod["price"]       = _selling_price(prod["base_price"], prod["markup"])
        prod["category"]    = request.form["category"]
        prod["img"]         = primary
        prod["images"]      = images
        prod["generic_name"] = request.form.get("generic_name", "").strip()
        prod["description"] = request.form["description"].strip()
        prod["discount"]    = int(request.form.get("discount", 0))
        prod["stock"]       = max(0, int(request.form.get("stock", 0) or 0))
        save_products(products)
        flash(f'Product "{prod["name"]}" updated successfully!', "success")
        return redirect(url_for("admin_dashboard"))
    # Normalise existing images for template
    existing_images = prod.get("images", [prod["img"]] if prod.get("img") else [])
    return render_template("admin/product_form.html", product=prod, categories=CATEGORIES,
                           discount_options=DISCOUNT_OPTIONS, existing_images=existing_images)


@app.route("/admin/product/delete/<int:product_id>", methods=["POST"])
@admin_required
def admin_delete_product(product_id):
    products = load_products()
    name = next((p["name"] for p in products if p["id"] == product_id), "Product")
    products = [p for p in products if p["id"] != product_id]
    save_products(products)
    flash(f'"{name}" has been deleted.', "success")
    return redirect(url_for("admin_dashboard"))


# ── QR photo upload ───────────────────────────────────────────────────────────
import hashlib
import hmac


def _qr_token(product_id):
    """Signed token so the QR upload link for a product can't be forged."""
    msg = f"qrupload:{product_id}".encode()
    return hmac.new(app.secret_key.encode(), msg, hashlib.sha256).hexdigest()[:20]


def _qr_token_valid(product_id, token):
    return hmac.compare_digest(_qr_token(product_id), token or "")


@app.route("/admin/product/<int:product_id>/qr")
@admin_required
def admin_product_qr(product_id):
    """Return the public upload URL + product name for the QR popup."""
    products = get_products()
    prod = next((p for p in products if p["id"] == product_id), None)
    if not prod:
        return jsonify({"success": False}), 404
    token = _qr_token(product_id)
    path = url_for("qr_upload", product_id=product_id, token=token)
    url = (PUBLIC_BASE_URL + path) if PUBLIC_BASE_URL else request.url_root.rstrip("/") + path
    return jsonify({"success": True, "url": url, "name": prod["name"]})


@app.route("/qr-upload/<int:product_id>", methods=["GET"])
def qr_upload(product_id):
    """Phone-friendly page reached by scanning the QR. Tokened, no admin login needed."""
    token = request.args.get("token", "")
    if not _qr_token_valid(product_id, token):
        return "Invalid or expired link.", 403
    products = get_products()
    prod = next((p for p in products if p["id"] == product_id), None)
    if not prod:
        return "Product not found.", 404
    return render_template("admin/qr_upload.html", product=prod, token=token)


@app.route("/qr-upload/<int:product_id>", methods=["POST"])
def qr_upload_save(product_id):
    """Receive photos from the phone and append them to the product."""
    token = request.form.get("token", "") or request.args.get("token", "")
    if not _qr_token_valid(product_id, token):
        return jsonify({"success": False, "message": "Invalid link."}), 403
    products = load_products()
    prod = next((p for p in products if p["id"] == product_id), None)
    if not prod:
        return jsonify({"success": False, "message": "Product not found."}), 404

    files = request.files.getlist("photos")
    images = list(prod.get("images") or ([prod["img"]] if prod.get("img") else []))
    saved = 0
    for file in files:
        if not file or file.filename == "" or not allowed_file(file.filename):
            continue
        ext = file.filename.rsplit(".", 1)[1].lower()
        filename = f"product_{product_id}_qr_{uuid.uuid4().hex[:8]}.{ext}"
        file.save(os.path.join(UPLOAD_FOLDER, filename))
        images.append(url_for("static", filename=f"uploads/{filename}", _external=False))
        saved += 1

    if saved:
        prod["images"] = images
        if not prod.get("img"):
            prod["img"] = images[0]
        save_products(products)
    return jsonify({"success": True, "added": saved, "total": len(images)})


@app.route("/admin/product/discount/<int:product_id>", methods=["POST"])
@admin_required
def admin_set_discount(product_id):
    products = load_products()
    prod = next((p for p in products if p["id"] == product_id), None)
    if prod:
        prod["discount"] = int(request.form.get("discount", 0))
        save_products(products)
        return jsonify({"success": True, "discount": prod["discount"],
                        "effective_price": round(prod["price"] * (1 - prod["discount"] / 100), 2)})
    return jsonify({"success": False}), 404


@app.route("/admin/product/stock/<int:product_id>", methods=["POST"])
@admin_required
def admin_set_stock(product_id):
    """Set or adjust a product's on-hand count from the Products table.
    Send either 'stock' (absolute value) or 'delta' (relative, e.g. -1)."""
    products = load_products()
    prod = next((p for p in products if p["id"] == product_id), None)
    if not prod:
        return jsonify({"success": False}), 404
    current = prod.get("stock") if isinstance(prod.get("stock"), int) else 0
    if request.form.get("delta") not in (None, ""):
        try:
            current += int(request.form.get("delta"))
        except (ValueError, TypeError):
            return jsonify({"success": False}), 400
    else:
        try:
            current = int(request.form.get("stock", 0))
        except (ValueError, TypeError):
            return jsonify({"success": False}), 400
    prod["stock"] = max(0, current)
    save_products(products)
    return jsonify({"success": True, "stock": prod["stock"]})


# ── Invoices (stock-in) ───────────────────────────────────────────────────────

def load_invoices():
    return db.read_list("invoices")


def save_invoices(invoices):
    db.write_list("invoices", invoices)


def _invoice_qty_by_product(invoice):
    """Sum the quantities this invoice assigns to each linked product.
    Returns {product_id: total_qty}. Unlinked lines are ignored."""
    totals = {}
    for item in invoice.get("items", []):
        pid = item.get("product_id")
        if not pid:
            continue
        try:
            qty = int(float(item.get("qty") or 0))
        except (ValueError, TypeError):
            qty = 0
        if qty <= 0:
            continue
        totals[pid] = totals.get(pid, 0) + qty
    return totals


def _apply_invoice_to_stock(invoice):
    """Reconcile on-hand stock to match this invoice's current line items.

    Adjusts each product's stock by the DELTA between what the invoice adds now
    and what it previously added (tracked in invoice['applied_qty']). This keeps
    on-hand consistent no matter how many times the invoice is edited and saved,
    and correctly handles swapping one product for another.
    """
    products = load_products()
    by_id = {p["id"]: p for p in products}

    now_qty = _invoice_qty_by_product(invoice)
    if "applied_qty" in invoice:
        prev = {int(k): v for k, v in (invoice["applied_qty"] or {}).items()}  # JSON keys are strings
    elif invoice.get("applied"):
        # Legacy invoice applied under the old checkbox flow but never tracked its
        # quantities — assume its current lines were already counted, so no change.
        prev = dict(now_qty)
    else:
        prev = {}

    # Every product touched by this invoice, past or present
    all_pids = set(prev) | set(now_qty)
    changed = False
    for pid in all_pids:
        if pid not in by_id:
            continue
        prod = by_id[pid]
        delta = now_qty.get(pid, 0) - prev.get(pid, 0)
        if delta == 0:
            continue
        prod["stock"] = max(0, int(prod.get("stock", 0)) + delta)
        changed = True

    # Refresh this invoice's expiry/lot batches so they mirror the current lines
    for pid in all_pids:
        if pid in by_id:
            prod = by_id[pid]
            prod["batches"] = [b for b in prod.get("batches", []) if b.get("invoice_id") != invoice["id"]]
    for item in invoice.get("items", []):
        pid = item.get("product_id")
        if not pid or pid not in by_id:
            continue
        try:
            qty = int(float(item.get("qty") or 0))
        except (ValueError, TypeError):
            qty = 0
        if qty <= 0:
            continue
        by_id[pid].setdefault("batches", []).append({
            "qty":        qty,
            "lot":        item.get("lot", ""),
            "expiry":     item.get("expiry", ""),
            "invoice":    invoice.get("invoice_no", ""),
            "invoice_id": invoice["id"],
            "added_at":   datetime.datetime.now().isoformat(),
        })

    if changed or now_qty or prev:
        save_products(products)

    # Remember what we just applied so the next save reconciles against it
    invoice["applied_qty"] = {str(k): v for k, v in now_qty.items()}


def _reduce_stock_for_order(order):
    """Decrement on-hand stock for each item when an order is confirmed.
    Stock never goes below 0. Safe to skip items that aren't tracked products."""
    products = load_products()
    by_id = {p["id"]: p for p in products}
    changed = False
    for item in order.get("items", []):
        pid = item.get("id")
        if pid not in by_id:
            continue
        prod = by_id[pid]
        if not isinstance(prod.get("stock"), int):
            continue  # untracked product — leave it alone
        try:
            qty = int(float(item.get("qty") or 0))
        except (ValueError, TypeError):
            qty = 0
        if qty <= 0:
            continue
        prod["stock"] = max(0, prod["stock"] - qty)
        changed = True
    if changed:
        save_products(products)


@app.route("/admin/invoices")
@admin_required
def admin_invoices():
    """Landing: New / Retrieve / Find product."""
    return render_template("admin/invoices.html")


@app.route("/admin/expired")
@admin_required
def admin_expired():
    """Show every expired item — aggregated from each product's lot/expiry batches,
    plus any invoice line whose expiry has passed (in case stock wasn't applied)."""
    today = datetime.date.today().isoformat()
    rows = []

    # 1) Expired stock batches recorded on products (the authoritative source).
    for p in get_products():
        for b in p.get("batches", []):
            exp = (b.get("expiry") or "")[:10]
            if exp and exp < today:
                rows.append({
                    "product_id": p["id"],
                    "name":       p["name"],
                    "category":   p.get("category", ""),
                    "qty":        b.get("qty", ""),
                    "lot":        b.get("lot", ""),
                    "expiry":     exp,
                    "invoice":    b.get("invoice", ""),
                    "invoice_id": b.get("invoice_id", ""),
                })

    # Sort most-recently-expired first.
    rows.sort(key=lambda r: r["expiry"], reverse=True)

    def days_past(exp):
        try:
            d = datetime.date.fromisoformat(exp)
            return (datetime.date.today() - d).days
        except (ValueError, TypeError):
            return 0

    return render_template("admin/expired.html", rows=rows, days_past=days_past)


@app.route("/admin/invoices/list")
@admin_required
def admin_invoices_list():
    sort = request.args.get("sort", "date_desc")
    invoices = load_invoices()
    if sort == "date_asc":
        invoices.sort(key=lambda x: x.get("date") or x.get("created_at", ""))
    elif sort == "supplier":
        invoices.sort(key=lambda x: (x.get("supplier") or "").lower())
    else:  # date_desc (default)
        invoices.sort(key=lambda x: x.get("date") or x.get("created_at", ""), reverse=True)
    return render_template("admin/invoices_list.html", invoices=invoices, sort=sort)


@app.route("/admin/invoices/new")
@admin_required
def admin_invoice_new():
    return render_template("admin/invoice_edit.html", invoice=None)


@app.route("/admin/invoices/<invoice_id>/view")
@admin_required
def admin_invoice_view(invoice_id):
    inv = next((i for i in load_invoices() if i["id"] == invoice_id), None)
    if not inv:
        flash("Invoice not found.", "error")
        return redirect(url_for("admin_invoices_list"))
    return render_template("admin/invoice_view.html", invoice=inv)


@app.route("/admin/invoices/<invoice_id>")
@admin_required
def admin_invoice_edit(invoice_id):
    inv = next((i for i in load_invoices() if i["id"] == invoice_id), None)
    if not inv:
        flash("Invoice not found.", "error")
        return redirect(url_for("admin_invoices_list"))
    return render_template("admin/invoice_edit.html", invoice=inv)


@app.route("/admin/invoices/save", methods=["POST"])
@admin_required
def admin_invoice_save():
    data = request.get_json(silent=True) or {}
    invoices = load_invoices()
    inv_id = data.get("id")
    now = datetime.datetime.now().isoformat()

    inv = next((i for i in invoices if i["id"] == inv_id), None) if inv_id else None
    is_new = inv is None
    if is_new:
        inv = {"id": uuid.uuid4().hex, "created_at": now}
        invoices.append(inv)

    # Header fields
    for f in ("supplier", "invoice_no", "order_no", "date", "due_date", "pharmacy", "photo", "notes"):
        inv[f] = (data.get(f) or "").strip() if isinstance(data.get(f), str) else data.get(f, "")
    # Line items
    inv["items"] = data.get("items", [])
    # Totals
    inv["totals"] = data.get("totals", {})
    inv["updated_at"] = now

    # Always reconcile on-hand stock + expiry to match this invoice's lines.
    # The delta logic keeps counts consistent across repeated edits/saves.
    _apply_invoice_to_stock(inv)
    inv["applied"] = True

    save_invoices(invoices)

    return jsonify({"success": True, "id": inv["id"]})


@app.route("/admin/invoices/<invoice_id>/delete", methods=["POST"])
@admin_required
def admin_invoice_delete(invoice_id):
    invoices = load_invoices()
    inv = next((i for i in invoices if i["id"] == invoice_id), None)
    if inv:
        # Roll back the on-hand stock this invoice had added, so counts stay consistent.
        inv["items"] = []
        _apply_invoice_to_stock(inv)
    invoices = [i for i in invoices if i["id"] != invoice_id]
    save_invoices(invoices)
    return jsonify({"success": True})


@app.route("/api/admin/product-lookup")
@admin_required
def admin_product_lookup():
    """Search products to auto-fill an invoice line (like AutoRx 'Search Item')."""
    q = request.args.get("q", "").strip().lower()
    if not q:
        return jsonify([])
    # Ranked search: name first, then generic name, then description (last resort),
    # plus the keyword/synonym map — same behaviour as the storefront autocomplete.
    results = []
    for p in search_products_ranked(q, get_products(), limit=12):
        imgs = p.get("images") or ([p["img"]] if p.get("img") else [])
        results.append({
            "id": p["id"], "name": p["name"],
            "cost": p.get("price", 0), "category": p["category"],
            "img": imgs[0] if imgs else "",
            "stock": p.get("stock"),
        })
    return jsonify(results)


@app.route("/api/admin/find-invoices-for-product")
@admin_required
def admin_find_invoices_for_product():
    """Find which saved invoices contain a product (by name)."""
    q = request.args.get("q", "").strip().lower()
    if not q:
        return jsonify([])
    out = []
    for inv in load_invoices():
        for item in inv.get("items", []):
            if q in str(item.get("description", "")).lower():
                out.append({
                    "invoice_id": inv["id"],
                    "invoice_no": inv.get("invoice_no", ""),
                    "supplier":   inv.get("supplier", ""),
                    "date":       inv.get("date", ""),
                    "description": item.get("description", ""),
                    "qty":        item.get("qty", ""),
                    "lot":        item.get("lot", ""),
                    "expiry":     item.get("expiry", ""),
                })
    return jsonify(out)


@app.route("/admin/orders")
@admin_required
def admin_orders():
    orders = load_orders()
    return render_template("admin/orders.html", orders=orders)


@app.route("/admin/orders/<order_id>/status", methods=["POST"])
@admin_required
def admin_update_order_status(order_id):
    orders = load_orders()
    order = next((o for o in orders if o["id"] == order_id), None)
    if not order:
        return jsonify({"success": False, "message": "Order not found"}), 404
    new_status = request.form.get("status", "").strip()
    old_status = order["status"]
    order["status"] = new_status
    if new_status == "delivered" and not order.get("delivered_at"):
        order["delivered_at"] = datetime.datetime.now().isoformat()
    save_orders(orders)
    # Notify the customer when the status actually changes
    if new_status != old_status:
        emails.send_order_status(order)
    return jsonify({"success": True, "status": new_status})


@app.route("/admin/stats")
@admin_required
def admin_stats():
    orders = load_orders()
    now = datetime.datetime.now()

    delivered      = [o for o in orders if o["status"] == "delivered"]
    total_revenue  = sum(o["total"] for o in delivered)
    total_orders   = len(orders)

    status_counts = {}
    for o in orders:
        status_counts[o["status"]] = status_counts.get(o["status"], 0) + 1

    sales_by_day = {}
    for i in range(6, -1, -1):
        d = (now - datetime.timedelta(days=i)).strftime("%b %d")
        sales_by_day[d] = 0
    for o in orders:
        try:
            d = datetime.datetime.fromisoformat(o["created_at"]).strftime("%b %d")
            if d in sales_by_day:
                sales_by_day[d] += o["total"]
        except Exception:
            pass

    product_qty = {}
    for o in orders:
        for item in o.get("items", []):
            name = item["name"]
            product_qty[name] = product_qty.get(name, 0) + item["qty"]
    top_products = sorted(product_qty.items(), key=lambda x: x[1], reverse=True)[:10]

    products_map = {p["id"]: p["category"] for p in load_products()}
    cat_sales = {k: 0 for k in CATEGORIES if k != "all"}
    for o in orders:
        for item in o.get("items", []):
            cat = products_map.get(item["id"], "other")
            if cat in cat_sales:
                cat_sales[cat] += item["price"] * item["qty"]

    refund_count = status_counts.get("refunded", 0)
    return_count = status_counts.get("returned", 0)

    return render_template(
        "admin/stats.html",
        total_revenue=total_revenue,
        total_orders=total_orders,
        status_counts=status_counts,
        sales_by_day=sales_by_day,
        top_products=top_products,
        cat_sales=cat_sales,
        refund_count=refund_count,
        return_count=return_count,
        orders=orders,
        categories=CATEGORIES,
    )


# ── Visitor analytics ─────────────────────────────────────────────────────────

def load_events():
    return db.read_list("events")


def save_events(events):
    db.write_list("events", events)


# Keep only recent events so the table doesn't grow forever (7 days).
_EVENT_RETENTION_DAYS = 7
# A visitor counts as "online" if seen within this many seconds.
_ONLINE_WINDOW_SECONDS = 60


def _device_type(ua):
    ua = (ua or "").lower()
    if any(t in ua for t in ("ipad", "tablet")):
        return "tablet"
    if any(t in ua for t in ("iphone", "android", "mobile")):
        return "phone"
    return "desktop"


@app.route("/api/track", methods=["POST"])
def api_track():
    """Record one visitor event (page view / product click / heartbeat)."""
    data = request.get_json(silent=True) or {}
    device = (data.get("device") or "")[:40]
    if not device:
        return jsonify({"ok": False}), 400

    now = datetime.datetime.now()
    event = {
        "id":        uuid.uuid4().hex,
        "device":    device,
        "type":      (data.get("type") or "view")[:20],      # view | click | heartbeat
        "page":      (data.get("page") or "")[:120],
        "label":     (data.get("label") or "")[:120],         # product name etc.
        "device_type": _device_type(request.headers.get("User-Agent")),
        "ts":        now.isoformat(),
    }
    # Fast single-row insert (no full-table rewrite).
    db.append_row("events", event)
    return jsonify({"ok": True})


def _analytics_summary():
    """Compute the live + historical analytics shown on the dashboard."""
    events = load_events()
    now = datetime.datetime.now()
    online_cutoff = (now - datetime.timedelta(seconds=_ONLINE_WINDOW_SECONDS)).isoformat()
    today = now.date().isoformat()

    # Online now (distinct devices seen in the last window)
    online_devices = {e["device"] for e in events if e.get("ts", "") >= online_cutoff}
    online_by_type = {"phone": set(), "desktop": set(), "tablet": set()}
    for e in events:
        if e.get("ts", "") >= online_cutoff:
            online_by_type.get(e.get("device_type", "desktop"), online_by_type["desktop"]).add(e["device"])

    # Sessions: group a device's events, duration = last - first (per device, today)
    sessions = {}
    for e in events:
        if not e.get("ts", "").startswith(today):
            continue
        d = e["device"]
        s = sessions.setdefault(d, {"first": e["ts"], "last": e["ts"], "type": e.get("device_type"), "count": 0})
        s["first"] = min(s["first"], e["ts"])
        s["last"] = max(s["last"], e["ts"])
        s["count"] += 1

    def dur_secs(s):
        try:
            return int((datetime.datetime.fromisoformat(s["last"]) -
                        datetime.datetime.fromisoformat(s["first"])).total_seconds())
        except Exception:
            return 0

    visitors_today = len(sessions)
    avg_session = int(sum(dur_secs(s) for s in sessions.values()) / visitors_today) if visitors_today else 0

    # Most-clicked products / pages (clicks + views on product pages)
    label_counts = {}
    for e in events:
        if e.get("label"):
            label_counts[e["label"]] = label_counts.get(e["label"], 0) + 1
    top_items = sorted(label_counts.items(), key=lambda x: x[1], reverse=True)[:10]

    # Live activity feed — real actions only (skip heartbeats), and collapse
    # consecutive duplicates per device so we log each action once, not repeatedly.
    feed = []
    last_per_device = {}   # device -> last (verb, what) we logged
    for e in sorted(events, key=lambda x: x.get("ts", "")):   # chronological
        if e.get("type") == "heartbeat":
            continue
        verb = {"view": "viewed", "click": "clicked"}.get(e.get("type"), "visited")
        what = e.get("label") or e.get("page") or "the site"
        key = (verb, what)
        if last_per_device.get(e["device"]) == key:
            continue   # same device doing the same thing — don't repeat
        last_per_device[e["device"]] = key
        try:
            when = datetime.datetime.fromisoformat(e["ts"]).strftime("%b %d, %I:%M:%S %p")
        except Exception:
            when = e.get("ts", "")
        feed.append({
            "device_type": e.get("device_type", "desktop"),
            "text": f"{verb} {what}",
            "when": when,
        })
    feed = list(reversed(feed))[:25]   # most recent first, cap at 25

    return {
        "online": len(online_devices),
        "online_phone": len(online_by_type["phone"]),
        "online_desktop": len(online_by_type["desktop"]),
        "online_tablet": len(online_by_type["tablet"]),
        "visitors_today": visitors_today,
        "avg_session": avg_session,
        "top_items": top_items,
        "feed": feed,
    }


# ── Search logging ────────────────────────────────────────────────────────────

_SEARCH_RETENTION_DAYS = 30


def _log_search(term):
    term = (term or "").strip()
    if not term:
        return
    now = datetime.datetime.now()
    # Fast single-row insert (no full-table rewrite on every keystroke).
    db.append_row("searches", {"id": uuid.uuid4().hex, "term": term[:80], "ts": now.isoformat()})


@app.route("/admin/searches")
@admin_required
def admin_searches():
    searches = db.read_list("searches")

    # Count full terms (case-insensitive)
    counts = {}
    for s in searches:
        t = s.get("term", "").strip().lower()
        if t:
            counts[t] = counts.get(t, 0) + 1
    top_terms = sorted(counts.items(), key=lambda x: x[1], reverse=True)[:50]

    # Recent raw searches (most recent first) with readable time
    recent = []
    for s in sorted(searches, key=lambda x: x.get("ts", ""), reverse=True)[:100]:
        try:
            when = datetime.datetime.fromisoformat(s["ts"]).strftime("%b %d, %I:%M %p")
        except Exception:
            when = s.get("ts", "")
        recent.append({"term": s.get("term", ""), "when": when})

    return render_template("admin/searches.html",
                           top_terms=top_terms, recent=recent, total=len(searches))


@app.route("/api/admin/live-stats")
@admin_required
def api_live_stats():
    return jsonify(_analytics_summary())


@app.route("/admin/analytics")
@admin_required
def admin_analytics():
    return render_template("admin/analytics.html", **_analytics_summary())


@app.route("/admin/staff")
@admin_required
def admin_staff():
    staff = load_staff()
    return render_template("admin/staff.html", staff=staff)


@app.route("/admin/staff/add", methods=["POST"])
@admin_required
def admin_add_staff():
    staff = load_staff()
    name     = request.form.get("name", "").strip()
    username = request.form.get("username", "").strip()
    email    = request.form.get("email", "").strip().lower()
    role     = request.form.get("role", "delivery")

    if not name or not username or not email:
        flash("Name, username and email are all required.", "error")
        return redirect(url_for("admin_staff"))

    if any(s.get("username") == username for s in staff):
        flash(f'Username "{username}" is already taken.', "error")
        return redirect(url_for("admin_staff"))

    # Create the member in "invited" state — no password until they accept.
    token = secrets.token_urlsafe(32)
    new_member = {
        "id":           str(uuid.uuid4()),
        "name":         name,
        "username":     username,
        "email":        email,
        "password":     "",            # set by the invitee when they accept
        "role":         role,
        "status":       "invited",     # invited → active
        "invite_token": token,
        "created_at":   datetime.datetime.now().isoformat(),
    }
    staff.append(new_member)
    save_staff(staff)

    accept_url = external_url("staff_accept_invite", token=token)
    sent = emails.send_staff_invite(new_member, accept_url)

    if sent:
        flash(f'Invitation sent to {email}. {name} can now accept and set a password.', "success")
    else:
        # Email not configured / failed — still show the admin the link so onboarding isn't blocked.
        flash(f'{name} added, but the invite email could not be sent. '
              f'Share this link manually: {accept_url}', "error")
    return redirect(url_for("admin_staff"))


@app.route("/invite/<token>", methods=["GET", "POST"])
def staff_accept_invite(token):
    staff  = load_staff()
    member = next((s for s in staff if s.get("invite_token") == token), None)

    if not member:
        flash("This invitation link is invalid or has already been used.", "error")
        return redirect(url_for("index"))

    if request.method == "POST":
        pw      = request.form.get("password", "").strip()
        confirm = request.form.get("confirm", "").strip()
        if len(pw) < 6:
            flash("Password must be at least 6 characters.", "error")
            return render_template("auth/accept_invite.html", member=member, token=token)
        if pw != confirm:
            flash("Passwords do not match.", "error")
            return render_template("auth/accept_invite.html", member=member, token=token)

        member["password"]     = pw
        member["status"]       = "active"
        member["invite_token"] = None   # single-use — burn the token
        save_staff(staff)
        flash("Your account is active! You can now log in.", "success")
        login_url = url_for("admin_login") if member["role"] == "admin" else url_for("delivery_login")
        return redirect(login_url)

    return render_template("auth/accept_invite.html", member=member, token=token)


@app.route("/book-appointment", methods=["POST"])
def book_appointment():
    name   = request.form.get("appt_name", "").strip()
    phone  = request.form.get("appt_phone", "").strip()
    email  = request.form.get("appt_email", "").strip()
    branch = request.form.get("appt_branch", "").strip()
    date   = request.form.get("appt_date", "").strip()
    time   = request.form.get("appt_time", "").strip()
    reason = request.form.get("appt_reason", "").strip()
    notes  = request.form.get("appt_notes", "").strip()

    if not all([name, phone, branch, date, time, reason]):
        return jsonify({"success": False, "message": "Please fill in all required fields."}), 400

    branch_label = next((b["name"] for b in BRANCHES if b["id"] == branch), branch)
    appt = {
        "id":           "APT-" + "".join(random.choices(string.digits, k=5)),
        "name":         name,
        "phone":        phone,
        "email":        email,
        "branch":       branch,
        "branch_label": branch_label,
        "date":         date,
        "time":         time,
        "reason":       reason,
        "notes":        notes,
        "status":       "pending",
        "feedback":     "",
        "created_at":   datetime.datetime.now().isoformat(),
        "updated_at":   datetime.datetime.now().isoformat(),
    }
    appts = load_appointments()
    appts.append(appt)
    save_appointments(appts)

    # Send confirmation email if the customer gave one
    emails.send_appointment_confirmation(appt)

    return jsonify({"success": True, "id": appt["id"],
                    "message": f"Appointment booked for {date} at {time}. Your reference is {appt['id']}."})


@app.route("/admin/appointments")
@admin_required
def admin_appointments():
    appts = load_appointments()
    appts_sorted = sorted(appts, key=lambda a: (a["date"], a["time"]), reverse=True)
    pending = sum(1 for a in appts if a["status"] == "pending")
    completed = sum(1 for a in appts if a["status"] == "completed")
    missed = sum(1 for a in appts if a["status"] == "missed")
    return render_template("admin/appointments.html", appointments=appts_sorted,
                           pending=pending, completed=completed, missed=missed)


@app.route("/admin/appointments/<appt_id>/update", methods=["POST"])
@admin_required
def admin_update_appointment(appt_id):
    appts = load_appointments()
    appt  = next((a for a in appts if a["id"] == appt_id), None)
    if not appt:
        return jsonify({"success": False, "message": "Appointment not found"}), 404
    old_status = appt["status"]
    appt["status"]     = request.form.get("status", appt["status"])
    appt["feedback"]   = request.form.get("feedback", appt.get("feedback", ""))
    appt["updated_at"] = datetime.datetime.now().isoformat()
    save_appointments(appts)

    # Notify the customer when the status actually changes
    if appt["status"] != old_status:
        emails.send_appointment_status(appt)

    return jsonify({"success": True, "status": appt["status"]})


@app.route("/admin/manual")
@admin_required
def admin_manual():
    return render_template("admin/manual.html")


@app.route("/admin/staff/delete/<staff_id>", methods=["POST"])
@admin_required
def admin_delete_staff(staff_id):
    staff = load_staff()
    staff = [s for s in staff if s["id"] != staff_id]
    save_staff(staff)
    flash("Staff member removed.", "success")
    return redirect(url_for("admin_staff"))


# Ad duration options in seconds
AD_DURATIONS = [
    {"label": "2 Minutes",  "seconds": 120},
    {"label": "30 Minutes", "seconds": 1800},
    {"label": "1 Hour",     "seconds": 3600},
    {"label": "6 Hours",    "seconds": 21600},
    {"label": "1 Day",      "seconds": 86400},
    {"label": "2 Days",     "seconds": 172800},
    {"label": "7 Days",     "seconds": 604800},
]

@app.route("/admin/ads")
@admin_required
def admin_ads():
    products = get_products()
    active_ads = get_active_ads()
    active_ids = {a["ad"]["product_id"] for a in active_ads}
    return render_template("admin/ads.html", products=products, active_ads=active_ads,
                           active_ids=active_ids, durations=AD_DURATIONS,
                           categories=CATEGORIES,
                           now=datetime.datetime.now().isoformat())

@app.route("/admin/ads/set", methods=["POST"])
@admin_required
def admin_set_ad():
    product_id = int(request.form.get("product_id"))
    seconds    = int(request.form.get("seconds", 3600))
    ads = load_ads()
    # Remove any existing ad for this product
    ads = [a for a in ads if a["product_id"] != product_id]
    # Allow many ads to run at once
    now = datetime.datetime.now()
    active = [a for a in ads if a.get("expires_at", "") > now.isoformat()]
    if len(active) >= MAX_ACTIVE_ADS:
        return jsonify({"success": False, "message": f"Maximum {MAX_ACTIVE_ADS} ads already running. Remove one first."}), 400
    expires_at = (now + datetime.timedelta(seconds=seconds)).isoformat()
    ads.append({
        "product_id": product_id,
        "expires_at": expires_at,
        "created_at": now.isoformat(),
        "duration_seconds": seconds,
    })
    save_ads(ads)
    return jsonify({"success": True, "expires_at": expires_at})

@app.route("/admin/ads/remove/<int:product_id>", methods=["POST"])
@admin_required
def admin_remove_ad(product_id):
    ads = load_ads()
    ads = [a for a in ads if a["product_id"] != product_id]
    save_ads(ads)
    flash("Ad removed.", "success")
    return redirect(url_for("admin_ads"))

@app.route("/api/active-ads")
def api_active_ads():
    """Public endpoint — homepage polls this to refresh ad data."""
    active = get_active_ads()
    return jsonify([
        {
            "product_id":  a["product"]["id"],
            "name":        a["product"]["name"],
            "img":         a["product"]["images"][0] if a["product"].get("images") else a["product"]["img"],
            "price":       a["product"]["effective_price"],
            "original_price": a["product"]["original_price"],
            "discount":    a["product"].get("discount", 0),
            "url":         url_for("product", product_id=a["product"]["id"]),
            "category":    CATEGORIES.get(a["product"]["category"], a["product"]["category"]),
            "expires_at":  a["ad"]["expires_at"],
        }
        for a in active
    ])


# ── Delivery routes ───────────────────────────────────────────────────────────

@app.route("/delivery/login", methods=["GET", "POST"])
def delivery_login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()
        staff = load_staff()
        worker = next(
            (s for s in staff if s["username"] == username and s.get("password") == password
             and password and s["role"] == "delivery" and s.get("status", "active") == "active"),
            None,
        )
        if worker:
            session["is_delivery"]         = True
            session["delivery_worker_id"]   = worker["id"]
            session["delivery_worker_name"] = worker["name"]
            return redirect(url_for("delivery_dashboard"))
        flash("Invalid credentials.", "error")
    return render_template("delivery/login.html")


@app.route("/delivery/manual")
@delivery_required
def delivery_manual():
    return render_template("delivery/manual.html")


@app.route("/delivery/logout")
def delivery_logout():
    session.pop("is_delivery", None)
    session.pop("delivery_worker_id", None)
    session.pop("delivery_worker_name", None)
    return redirect(url_for("index"))


@app.route("/delivery/dashboard")
@delivery_required
def delivery_dashboard():
    orders = load_orders()
    today  = datetime.date.today().isoformat()
    today_orders = [
        o for o in orders
        if o.get("fulfillment") == "delivery"
        and o.get("status") in ("processing", "out_for_delivery")
        and o.get("created_at", "")[:10] == today
    ]
    # Also include recent orders (last 7 days) so dashboard isn't empty during dev
    recent_cutoff = (datetime.datetime.now() - datetime.timedelta(days=7)).isoformat()
    if not today_orders:
        today_orders = [
            o for o in orders
            if o.get("fulfillment") == "delivery"
            and o.get("status") in ("processing", "out_for_delivery")
            and o.get("created_at", "") >= recent_cutoff
        ]
    return render_template("delivery/dashboard.html", today_orders=today_orders,
                           worker_id=session.get("delivery_worker_id"),
                           worker_name=session.get("delivery_worker_name"))


@app.route("/delivery/claim/<order_id>", methods=["POST"])
@delivery_required
def delivery_claim(order_id):
    orders = load_orders()
    order  = next((o for o in orders if o["id"] == order_id), None)
    if not order:
        return jsonify({"success": False, "message": "Order not found"}), 404
    order["status"]             = "out_for_delivery"
    order["delivery_worker_id"] = session.get("delivery_worker_id")
    save_orders(orders)
    emails.send_order_status(order)
    return jsonify({"success": True})


@app.route("/delivery/verify/<order_id>", methods=["GET", "POST"])
@delivery_required
def delivery_verify(order_id):
    orders = load_orders()
    order  = next((o for o in orders if o["id"] == order_id), None)
    if not order:
        if request.method == "POST":
            return jsonify({"success": False, "message": "Order not found"}), 404
        flash("Order not found.", "error")
        return redirect(url_for("delivery_dashboard"))

    if request.method == "POST":
        code = request.form.get("code", "").strip()
        if code.upper() == order.get("pickup_code", ""):
            order["status"]       = "delivered"
            order["delivered_at"] = datetime.datetime.now().isoformat()
            save_orders(orders)
            emails.send_order_status(order)
            return jsonify({"success": True, "message": "Delivery confirmed!"})
        return jsonify({"success": False, "message": "Incorrect code. Please try again."})

    return render_template("delivery/verify.html", order=order)


if __name__ == "__main__":
    app.run(debug=True, port=5001)