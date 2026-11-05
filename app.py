"""
E-commerce shop — Flask + SQLAlchemy
Features: catalogue with categories & search, session cart, checkout,
M-Pesa STK Push (Daraja) or Pay on Delivery, order tracking, admin panel.
"""
import os
import json
import base64
import hmac
import secrets
from datetime import datetime
from functools import wraps

import requests
from flask import (Flask, render_template, request, redirect, url_for,
                   session, flash, abort, jsonify, make_response)
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import or_, func, inspect, text

# ---------------------------------------------------------------- config
app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-change-me")

db_url = os.environ.get("DATABASE_URL", "sqlite:///shop.db")
if db_url.startswith("postgres://"):  # Render gives postgres://
    db_url = db_url.replace("postgres://", "postgresql://", 1)
app.config["SQLALCHEMY_DATABASE_URI"] = db_url
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {"pool_pre_ping": True}
app.config["MAX_CONTENT_LENGTH"] = 3 * 1024 * 1024  # 3 MB uploads

STORE_NAME = os.environ.get("STORE_NAME", "DaveTech Shop")
STORE_PHONE = os.environ.get("STORE_PHONE", "0700 000 000")
CURRENCY = os.environ.get("CURRENCY", "KSh")
DELIVERY_FEE = int(os.environ.get("DELIVERY_FEE", "200"))
FREE_DELIVERY_OVER = int(os.environ.get("FREE_DELIVERY_OVER", "5000"))
DROPSHIP_DAYS = os.environ.get("DROPSHIP_DAYS", "14-25 days")   # default overseas delivery time
DROPSHIP_MAX_QTY = 20
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "admin@example.com")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")
BASE_URL = os.environ.get("BASE_URL", "").rstrip("/")
IMPORTER_API_KEY = os.environ.get("IMPORTER_API_KEY", "")  # shared with Davetech Importer

MPESA = {
    "key": os.environ.get("MPESA_CONSUMER_KEY", ""),
    "secret": os.environ.get("MPESA_CONSUMER_SECRET", ""),
    "shortcode": os.environ.get("MPESA_SHORTCODE", ""),
    "passkey": os.environ.get("MPESA_PASSKEY", ""),
    "env": os.environ.get("MPESA_ENV", "sandbox"),
    "type": os.environ.get("MPESA_TYPE", "CustomerPayBillOnline"),  # or CustomerBuyGoodsOnline
    "till": os.environ.get("MPESA_TILL", ""),  # for Buy Goods: PartyB
}
MPESA_ON = all([MPESA["key"], MPESA["secret"], MPESA["shortcode"], MPESA["passkey"], BASE_URL])

db = SQLAlchemy(app)

# ---------------------------------------------------------------- models
class Category(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), unique=True, nullable=False)
    products = db.relationship("Product", backref="category", lazy=True)


class Product(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(160), nullable=False)
    description = db.Column(db.Text, default="")
    price = db.Column(db.Integer, nullable=False)          # whole shillings
    old_price = db.Column(db.Integer)                      # for "was" price
    stock = db.Column(db.Integer, default=0)
    image = db.Column(db.Text, default="")                 # URL or data: URI
    featured = db.Column(db.Boolean, default=False)
    active = db.Column(db.Boolean, default=True)
    category_id = db.Column(db.Integer, db.ForeignKey("category.id"))
    created = db.Column(db.DateTime, default=datetime.utcnow)
    # --- AliExpress dropshipping
    dropship = db.Column(db.Boolean, default=False)
    supplier_url = db.Column(db.Text, default="")
    supplier_cost = db.Column(db.Integer, default=0)      # KSh you pay AliExpress for the item
    supplier_shipping = db.Column(db.Integer, default=0)  # KSh shipping you pay AliExpress
    delivery_days = db.Column(db.String(40), default="")
    # --- Davetech Importer integration
    external_id = db.Column(db.String(64), index=True)   # importer product id (unique per product)
    images = db.Column(db.Text, default="")               # JSON list of extra image URLs
    sku = db.Column(db.String(80), default="")

    @property
    def gallery(self):
        try:
            extra = json.loads(self.images or "[]")
        except ValueError:
            extra = []
        return [u for u in ([self.image] + [i for i in extra if i != self.image]) if u]

    @property
    def available(self):
        return DROPSHIP_MAX_QTY if self.dropship else max(self.stock or 0, 0)

    @property
    def profit(self):
        return (self.price or 0) - (self.supplier_cost or 0) - (self.supplier_shipping or 0)


class StoredImage(db.Model):
    """Photos uploaded through Davetech Importer (e.g. local supplier products)."""
    id = db.Column(db.Integer, primary_key=True)
    mime = db.Column(db.String(30), nullable=False)
    data = db.Column(db.LargeBinary, nullable=False)
    created = db.Column(db.DateTime, default=datetime.utcnow)


class Order(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(12), unique=True, nullable=False)
    name = db.Column(db.String(120), nullable=False)
    phone = db.Column(db.String(20), nullable=False)
    email = db.Column(db.String(120), default="")
    address = db.Column(db.Text, nullable=False)
    notes = db.Column(db.Text, default="")
    subtotal = db.Column(db.Integer, nullable=False)
    delivery = db.Column(db.Integer, nullable=False)
    total = db.Column(db.Integer, nullable=False)
    payment_method = db.Column(db.String(20), default="cod")   # mpesa | cod
    payment_status = db.Column(db.String(20), default="unpaid")  # unpaid|pending|paid|failed
    mpesa_checkout_id = db.Column(db.String(80), index=True)
    mpesa_receipt = db.Column(db.String(40))
    status = db.Column(db.String(20), default="new")  # new|processing|shipped|delivered|cancelled
    supplier_order_no = db.Column(db.String(60), default="")  # AliExpress order number
    tracking_no = db.Column(db.String(60), default="")
    created = db.Column(db.DateTime, default=datetime.utcnow)
    items = db.relationship("OrderItem", backref="order", lazy=True, cascade="all, delete-orphan")

    @property
    def has_dropship(self):
        return any(i.dropship for i in self.items)


class OrderItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey("order.id"), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey("product.id"))
    name = db.Column(db.String(160), nullable=False)
    price = db.Column(db.Integer, nullable=False)
    qty = db.Column(db.Integer, nullable=False)
    dropship = db.Column(db.Boolean, default=False)
    supplier_url = db.Column(db.Text, default="")
    supplier_cost = db.Column(db.Integer, default=0)


ORDER_STATUSES = ["new", "processing", "shipped", "delivered", "cancelled"]

# ---------------------------------------------------------------- helpers
@app.template_filter("money")
def money(v):
    return f"{CURRENCY} {int(v or 0):,}"


@app.context_processor
def inject_globals():
    cart = session.get("cart", {})
    return {
        "STORE_NAME": STORE_NAME, "STORE_PHONE": STORE_PHONE,
        "cart_count": sum(cart.values()),
        "all_categories": Category.query.order_by(Category.name).all(),
        "csrf_token": get_csrf, "now": datetime.utcnow(), "DROPSHIP_DAYS": DROPSHIP_DAYS,
    }


def get_csrf():
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_hex(16)
    return session["_csrf"]


@app.before_request
def check_csrf():
    if request.path.startswith("/api/integration/"):
        return  # authenticated with the importer API key instead of a form token
    if request.method == "POST" and request.endpoint != "mpesa_callback":
        if request.form.get("_csrf") != session.get("_csrf"):
            abort(400, "Form expired — please go back and try again.")


def admin_required(f):
    @wraps(f)
    def wrapper(*a, **kw):
        if not session.get("admin"):
            return redirect(url_for("admin_login", next=request.path))
        return f(*a, **kw)
    return wrapper


def cart_lines():
    """Return (lines, subtotal) from session cart, dropping vanished products."""
    cart = session.get("cart", {})
    lines, subtotal = [], 0
    if cart:
        products = Product.query.filter(Product.id.in_([int(i) for i in cart])).all()
        for p in products:
            qty = min(cart[str(p.id)], p.available)
            if qty <= 0 or not p.active:
                continue
            lines.append({"product": p, "qty": qty, "total": qty * p.price})
            subtotal += qty * p.price
    return lines, subtotal


def delivery_for(subtotal):
    return 0 if subtotal == 0 or subtotal >= FREE_DELIVERY_OVER else DELIVERY_FEE


def normalize_phone(phone):
    p = "".join(ch for ch in phone if ch.isdigit())
    if p.startswith("0"):
        p = "254" + p[1:]
    elif p.startswith("7") or p.startswith("1"):
        p = "254" + p
    return p


# ---------------------------------------------------------------- M-Pesa
def mpesa_base():
    return "https://api.safaricom.co.ke" if MPESA["env"] == "production" else "https://sandbox.safaricom.co.ke"


def mpesa_token():
    r = requests.get(f"{mpesa_base()}/oauth/v1/generate?grant_type=client_credentials",
                     auth=(MPESA["key"], MPESA["secret"]), timeout=20)
    r.raise_for_status()
    return r.json()["access_token"]


def mpesa_stk_push(order):
    ts = datetime.now().strftime("%Y%m%d%H%M%S")
    password = base64.b64encode(f"{MPESA['shortcode']}{MPESA['passkey']}{ts}".encode()).decode()
    party_b = MPESA["till"] if MPESA["type"] == "CustomerBuyGoodsOnline" and MPESA["till"] else MPESA["shortcode"]
    payload = {
        "BusinessShortCode": MPESA["shortcode"], "Password": password, "Timestamp": ts,
        "TransactionType": MPESA["type"], "Amount": order.total,
        "PartyA": normalize_phone(order.phone), "PartyB": party_b,
        "PhoneNumber": normalize_phone(order.phone),
        "CallBackURL": f"{BASE_URL}/mpesa/callback",
        "AccountReference": order.code, "TransactionDesc": f"Order {order.code}",
    }
    r = requests.post(f"{mpesa_base()}/mpesa/stkpush/v1/processrequest", json=payload,
                      headers={"Authorization": f"Bearer {mpesa_token()}"}, timeout=30)
    data = r.json()
    if data.get("ResponseCode") == "0":
        order.mpesa_checkout_id = data["CheckoutRequestID"]
        order.payment_status = "pending"
        return True, data.get("CustomerMessage", "Check your phone")
    return False, data.get("errorMessage") or data.get("ResponseDescription") or "M-Pesa request failed"


def mpesa_stk_query(checkout_id):
    """Ask Safaricom directly whether a payment succeeded (authoritative check)."""
    ts = datetime.now().strftime("%Y%m%d%H%M%S")
    password = base64.b64encode(f"{MPESA['shortcode']}{MPESA['passkey']}{ts}".encode()).decode()
    r = requests.post(f"{mpesa_base()}/mpesa/stkpushquery/v1/query",
                      json={"BusinessShortCode": MPESA["shortcode"], "Password": password,
                            "Timestamp": ts, "CheckoutRequestID": checkout_id},
                      headers={"Authorization": f"Bearer {mpesa_token()}"}, timeout=30)
    return r.json()


@app.route("/mpesa/callback", methods=["POST"])
def mpesa_callback():
    data = (request.get_json(silent=True) or {}).get("Body", {}).get("stkCallback", {})
    order = Order.query.filter_by(mpesa_checkout_id=data.get("CheckoutRequestID")).first()
    if not order or order.payment_status == "paid":   # unknown or already handled (duplicate callback)
        return jsonify({"ResultCode": 0, "ResultDesc": "Accepted"})
    if data.get("ResultCode") == 0:
        items = {i["Name"]: i.get("Value") for i in data.get("CallbackMetadata", {}).get("Item", [])}
        amount_ok = int(float(items.get("Amount") or 0)) >= order.total
        confirmed = False
        try:  # never trust the callback body alone
            q = mpesa_stk_query(order.mpesa_checkout_id)
            confirmed = str(q.get("ResultCode")) == "0"
        except Exception as e:  # Safaricom unreachable: leave pending, admin can re-check
            app.logger.warning("STK query failed for %s: %s", order.code, e)
        if confirmed and amount_ok:
            order.payment_status = "paid"
            order.mpesa_receipt = str(items.get("MpesaReceiptNumber", ""))
            if order.status == "new":
                order.status = "processing"
        elif not amount_ok:
            order.payment_status = "failed"
            order.notes = (order.notes + f"\n[M-Pesa] amount {items.get('Amount')} lower than total").strip()
        else:
            order.payment_status = "pending"
    else:
        order.payment_status = "failed"
    db.session.commit()
    return jsonify({"ResultCode": 0, "ResultDesc": "Accepted"})


# ---------------------------------------------------------------- shop
@app.route("/")
def index():
    q = request.args.get("q", "").strip()
    cat = request.args.get("cat", type=int)
    sort = request.args.get("sort", "new")
    query = Product.query.filter_by(active=True)
    if q:
        like = f"%{q}%"
        query = query.filter(or_(Product.name.ilike(like), Product.description.ilike(like)))
    if cat:
        query = query.filter_by(category_id=cat)
    order_by = {"price_asc": Product.price.asc(), "price_desc": Product.price.desc()}.get(sort, Product.created.desc())
    products = query.order_by(order_by).all()
    featured = [] if (q or cat) else Product.query.filter_by(active=True, featured=True).limit(4).all()
    current_cat = db.session.get(Category, cat) if cat else None
    return render_template("index.html", products=products, featured=featured,
                           q=q, cat=cat, sort=sort, current_cat=current_cat)


@app.route("/product/<int:pid>")
def product(pid):
    p = db.session.get(Product, pid)
    if not p or not p.active:
        abort(404)
    related = Product.query.filter(Product.category_id == p.category_id, Product.id != p.id,
                                   Product.active.is_(True)).limit(4).all()
    return render_template("product.html", p=p, related=related)


@app.route("/cart")
def cart():
    lines, subtotal = cart_lines()
    return render_template("cart.html", lines=lines, subtotal=subtotal,
                           delivery=delivery_for(subtotal), free_over=FREE_DELIVERY_OVER)


@app.route("/cart/add/<int:pid>", methods=["POST"])
def cart_add(pid):
    p = db.session.get(Product, pid)
    if not p or not p.active or p.available <= 0:
        flash("Sorry, that item is out of stock.", "error")
        return redirect(request.referrer or url_for("index"))
    qty = max(1, request.form.get("qty", 1, type=int))
    cart = session.get("cart", {})
    cart[str(pid)] = min(cart.get(str(pid), 0) + qty, p.available)
    session["cart"] = cart
    flash(f"Added {p.name} to cart.", "success")
    return redirect(request.form.get("next") or request.referrer or url_for("index"))


@app.route("/cart/update", methods=["POST"])
def cart_update():
    cart = {}
    for key, val in request.form.items():
        if key.startswith("qty_"):
            try:
                q = int(val)
            except ValueError:
                continue
            if q > 0:
                cart[key[4:]] = q
    session["cart"] = cart
    return redirect(url_for("cart"))


@app.route("/cart/remove/<int:pid>", methods=["POST"])
def cart_remove(pid):
    cart = session.get("cart", {})
    cart.pop(str(pid), None)
    session["cart"] = cart
    return redirect(url_for("cart"))


@app.route("/checkout", methods=["GET", "POST"])
def checkout():
    lines, subtotal = cart_lines()
    if not lines:
        flash("Your cart is empty.", "error")
        return redirect(url_for("index"))
    delivery = delivery_for(subtotal)
    has_dropship = any(l["product"].dropship for l in lines)
    ctx = dict(lines=lines, subtotal=subtotal, delivery=delivery, mpesa_on=MPESA_ON, has_dropship=has_dropship)

    if request.method == "POST":
        f = request.form
        name, phone, address = f.get("name", "").strip(), f.get("phone", "").strip(), f.get("address", "").strip()
        method = f.get("payment_method", "cod")
        if not (name and phone and address):
            flash("Please fill in your name, phone and delivery address.", "error")
            return render_template("checkout.html", form=f, **ctx)
        if len(normalize_phone(phone)) != 12:
            flash("Enter a valid Safaricom/Kenyan phone number, e.g. 0712345678.", "error")
            return render_template("checkout.html", form=f, **ctx)

        order = Order(code=secrets.token_hex(3).upper(), name=name, phone=phone,
                      email=f.get("email", "").strip(), address=address,
                      notes=f.get("notes", "").strip(), subtotal=subtotal,
                      delivery=delivery, total=subtotal + delivery,
                      payment_method="mpesa" if ((method == "mpesa" or has_dropship) and MPESA_ON) else "cod")
        for line in lines:
            p = line["product"]
            order.items.append(OrderItem(product_id=p.id, name=p.name, price=p.price, qty=line["qty"],
                                         dropship=bool(p.dropship), supplier_url=p.supplier_url or "",
                                         supplier_cost=(p.supplier_cost or 0) + (p.supplier_shipping or 0)))
            if not p.dropship:
                p.stock -= line["qty"]
        db.session.add(order)
        db.session.commit()
        session["cart"] = {}
        session.setdefault("my_orders", []).append(order.code)
        session.modified = True

        if order.payment_method == "mpesa":
            try:
                ok, msg = mpesa_stk_push(order)
                db.session.commit()
                flash(msg, "success" if ok else "error")
            except Exception as e:  # network / credentials issues
                flash(f"Could not reach M-Pesa ({e}). You can retry from the order page.", "error")
        return redirect(url_for("order_view", code=order.code, phone=order.phone))

    return render_template("checkout.html", form={}, **ctx)


@app.route("/order/<code>")
def order_view(code):
    order = Order.query.filter_by(code=code.upper()).first_or_404()
    phone = request.args.get("phone", "")
    allowed = session.get("admin") or code.upper() in session.get("my_orders", []) \
        or normalize_phone(phone) == normalize_phone(order.phone)
    if not allowed:
        abort(404)
    return render_template("order.html", order=order, mpesa_on=MPESA_ON, statuses=ORDER_STATUSES)


@app.route("/order/<code>/status")
def order_status_json(code):
    order = Order.query.filter_by(code=code.upper()).first_or_404()
    return jsonify({"payment_status": order.payment_status, "status": order.status})


@app.route("/order/<code>/pay", methods=["POST"])
def order_pay(code):
    order = Order.query.filter_by(code=code.upper()).first_or_404()
    if order.payment_status != "paid" and MPESA_ON:
        if request.form.get("phone"):
            order.phone = request.form["phone"].strip()
        order.payment_method = "mpesa"
        try:
            ok, msg = mpesa_stk_push(order)
            db.session.commit()
            flash(msg, "success" if ok else "error")
        except Exception as e:
            flash(f"Could not reach M-Pesa ({e}).", "error")
    return redirect(url_for("order_view", code=order.code, phone=order.phone))


@app.route("/track", methods=["GET", "POST"])
def track():
    if request.method == "POST":
        code, phone = request.form.get("code", "").strip().upper(), request.form.get("phone", "")
        order = Order.query.filter_by(code=code).first()
        if order and normalize_phone(phone) == normalize_phone(order.phone):
            return redirect(url_for("order_view", code=code, phone=phone))
        flash("No order found with that code and phone number.", "error")
    return render_template("track.html")


# ---------------------------------------------------------------- admin
@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        if (request.form.get("email", "").strip().lower() == ADMIN_EMAIL.lower()
                and secrets.compare_digest(request.form.get("password", ""), ADMIN_PASSWORD)):
            session["admin"] = True
            return redirect(request.args.get("next") or url_for("admin_dashboard"))
        flash("Wrong email or password.", "error")
    return render_template("admin/login.html")


@app.route("/admin/logout")
def admin_logout():
    session.pop("admin", None)
    return redirect(url_for("index"))


@app.route("/admin")
@admin_required
def admin_dashboard():
    stats = {
        "orders": Order.query.count(),
        "new": Order.query.filter_by(status="new").count(),
        "revenue": db.session.query(func.coalesce(func.sum(Order.total), 0))
                     .filter(or_(Order.payment_status == "paid", Order.status == "delivered")).scalar(),
        "products": Product.query.count(),
        "low_stock": Product.query.filter(Product.stock <= 3, Product.active.is_(True),
                                          Product.dropship.is_(False)).all(),
    }
    to_order = [o for o in Order.query.filter(Order.status.in_(["new", "processing"]))
                .order_by(Order.created).all()
                if o.has_dropship and not o.supplier_order_no
                and (o.payment_status == "paid" or o.payment_method == "cod")]
    recent = Order.query.order_by(Order.created.desc()).limit(8).all()
    return render_template("admin/dashboard.html", stats=stats, recent=recent, to_order=to_order)


@app.route("/admin/products")
@admin_required
def admin_products():
    products = Product.query.order_by(Product.created.desc()).all()
    return render_template("admin/products.html", products=products)


def save_product_from_form(p):
    f = request.form
    p.name = f.get("name", "").strip()
    p.description = f.get("description", "").strip()
    p.price = f.get("price", 0, type=int)
    p.old_price = f.get("old_price", type=int) or None
    p.stock = f.get("stock", 0, type=int)
    p.category_id = f.get("category_id", type=int) or None
    p.featured = bool(f.get("featured"))
    p.active = bool(f.get("active"))
    p.dropship = bool(f.get("dropship"))
    p.supplier_url = f.get("supplier_url", "").strip()
    p.supplier_cost = f.get("supplier_cost", 0, type=int) or 0
    p.supplier_shipping = f.get("supplier_shipping", 0, type=int) or 0
    p.delivery_days = f.get("delivery_days", "").strip()
    if p.dropship and p.supplier_url and not p.supplier_url.startswith("http"):
        raise ValueError("AliExpress link must start with https://")
    upload = request.files.get("image_file")
    if upload and upload.filename:
        mime = upload.mimetype or "image/jpeg"
        if not mime.startswith("image/"):
            raise ValueError("Upload must be an image")
        p.image = f"data:{mime};base64," + base64.b64encode(upload.read()).decode()
    elif f.get("image_url", "").strip():
        p.image = f["image_url"].strip()


@app.route("/admin/products/new", methods=["GET", "POST"])
@app.route("/admin/products/<int:pid>/edit", methods=["GET", "POST"])
@admin_required
def admin_product_form(pid=None):
    p = db.session.get(Product, pid) if pid else Product(active=True)
    if pid and not p:
        abort(404)
    if request.method == "POST":
        try:
            save_product_from_form(p)
            if not p.name or p.price <= 0:
                raise ValueError("Name and a price above 0 are required")
        except ValueError as e:
            flash(str(e), "error")
            return render_template("admin/product_form.html", p=p)
        if not pid:
            db.session.add(p)
        db.session.commit()
        flash("Product saved.", "success")
        return redirect(url_for("admin_products"))
    return render_template("admin/product_form.html", p=p)


@app.route("/admin/products/<int:pid>/delete", methods=["POST"])
@admin_required
def admin_product_delete(pid):
    p = db.session.get(Product, pid) or abort(404)
    if OrderItem.query.filter_by(product_id=pid).first():
        p.active = False  # keep history intact
        flash("Product has orders, so it was hidden instead of deleted.", "success")
    else:
        db.session.delete(p)
        flash("Product deleted.", "success")
    db.session.commit()
    return redirect(url_for("admin_products"))


@app.route("/admin/categories", methods=["GET", "POST"])
@admin_required
def admin_categories():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if name and not Category.query.filter(func.lower(Category.name) == name.lower()).first():
            db.session.add(Category(name=name))
            db.session.commit()
            flash("Category added.", "success")
        else:
            flash("Enter a new, unique category name.", "error")
        return redirect(url_for("admin_categories"))
    cats = Category.query.order_by(Category.name).all()
    return render_template("admin/categories.html", cats=cats)


@app.route("/admin/categories/<int:cid>/delete", methods=["POST"])
@admin_required
def admin_category_delete(cid):
    c = db.session.get(Category, cid) or abort(404)
    for p in c.products:
        p.category_id = None
    db.session.delete(c)
    db.session.commit()
    flash("Category deleted.", "success")
    return redirect(url_for("admin_categories"))


@app.route("/admin/orders")
@admin_required
def admin_orders():
    status = request.args.get("status", "")
    query = Order.query
    if status:
        query = query.filter_by(status=status)
    orders = query.order_by(Order.created.desc()).all()
    return render_template("admin/orders.html", orders=orders, status=status, statuses=ORDER_STATUSES)


@app.route("/admin/orders/<int:oid>", methods=["GET", "POST"])
@admin_required
def admin_order_detail(oid):
    order = db.session.get(Order, oid) or abort(404)
    if request.method == "POST":
        new_status = request.form.get("status")
        if new_status in ORDER_STATUSES:
            if new_status == "cancelled" and order.status != "cancelled":
                for it in order.items:  # return stock
                    p = db.session.get(Product, it.product_id)
                    if p:
                        p.stock += it.qty
            order.status = new_status
        if request.form.get("payment_status") in ("unpaid", "pending", "paid", "failed"):
            order.payment_status = request.form["payment_status"]
        order.supplier_order_no = request.form.get("supplier_order_no", order.supplier_order_no or "").strip()
        new_tracking = request.form.get("tracking_no", order.tracking_no or "").strip()
        if new_tracking and not order.tracking_no and order.status in ("new", "processing"):
            order.status = "shipped"  # tracking added -> mark shipped
        order.tracking_no = new_tracking
        db.session.commit()
        flash("Order updated.", "success")
        return redirect(url_for("admin_order_detail", oid=oid))
    return render_template("admin/order_detail.html", order=order, statuses=ORDER_STATUSES)


# ---------------------------------------------------------------- integration API (Davetech Importer)
API_VERSION = "1.0"


def api_auth(f):
    @wraps(f)
    def wrapper(*a, **kw):
        token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        if not IMPORTER_API_KEY or len(IMPORTER_API_KEY) < 16:
            return jsonify({"error": "IMPORTER_API_KEY is not set on the shop"}), 503
        if not hmac.compare_digest(token.encode(), IMPORTER_API_KEY.encode()):
            return jsonify({"error": "invalid API key"}), 401
        return f(*a, **kw)
    return wrapper


def order_json(o):
    ext = {p.id: p.external_id for p in Product.query.filter(
        Product.id.in_([i.product_id for i in o.items if i.product_id])).all()} if o.items else {}
    return {
        "id": o.id, "code": o.code, "name": o.name, "phone": o.phone, "email": o.email,
        "address": o.address, "notes": o.notes, "subtotal": o.subtotal, "delivery": o.delivery,
        "total": o.total, "payment_method": o.payment_method, "payment_status": o.payment_status,
        "mpesa_receipt": o.mpesa_receipt, "status": o.status,
        "supplier_order_no": o.supplier_order_no or "", "tracking_no": o.tracking_no or "",
        "created": o.created.isoformat() + "Z",
        "items": [{"product_id": i.product_id, "name": i.name, "price": i.price, "qty": i.qty,
                   "dropship": bool(i.dropship), "supplier_url": i.supplier_url or "",
                   "supplier_cost": i.supplier_cost or 0, "external_id": ext.get(i.product_id)}
                  for i in o.items],
    }


@app.route("/api/integration/v1/health")
@api_auth
def api_health():
    return jsonify({"ok": True, "store_name": STORE_NAME, "currency": CURRENCY, "version": API_VERSION})


@app.route("/api/integration/v1/categories", methods=["GET", "POST"])
@api_auth
def api_categories():
    if request.method == "POST":
        name = str((request.get_json(silent=True) or {}).get("name", "")).strip()[:80]
        if not name:
            return jsonify({"error": "name required"}), 400
        c = Category.query.filter(func.lower(Category.name) == name.lower()).first()
        if not c:
            c = Category(name=name)
            db.session.add(c)
            db.session.commit()
        return jsonify({"id": c.id, "name": c.name})
    return jsonify({"categories": [{"id": c.id, "name": c.name} for c in Category.query.order_by(Category.name)]})


@app.route("/api/integration/v1/products/<external_id>", methods=["PUT", "DELETE"])
@api_auth
def api_product(external_id):
    p = Product.query.filter_by(external_id=external_id).first()
    if request.method == "DELETE":
        if not p:
            return jsonify({"result": "not_found"})
        if OrderItem.query.filter_by(product_id=p.id).first():
            p.active = False
            result = "hidden"
        else:
            db.session.delete(p)
            result = "deleted"
        db.session.commit()
        return jsonify({"result": result})

    d = request.get_json(silent=True) or {}
    try:
        name = str(d["name"]).strip()[:160]
        price = int(d["price"])
    except (KeyError, ValueError, TypeError):
        return jsonify({"error": "name and integer price are required"}), 400
    if not name or price <= 0:
        return jsonify({"error": "name and a price above 0 are required"}), 400
    image = str(d.get("image") or "")
    if image and not image.startswith("https://"):
        return jsonify({"error": "image must be an https URL"}), 400
    cat_id = d.get("category_id")
    if cat_id is not None and not db.session.get(Category, int(cat_id)):
        return jsonify({"error": "unknown category_id"}), 400

    created = p is None
    if created:
        p = Product(external_id=external_id, stock=0, featured=False)
        db.session.add(p)
    p.name = name
    p.description = str(d.get("description") or "")
    p.price = price
    p.old_price = int(d["old_price"]) if d.get("old_price") else None
    p.image = image
    p.images = json.dumps([u for u in (d.get("images") or []) if isinstance(u, str) and u.startswith("https://")][:10])
    p.category_id = int(cat_id) if cat_id is not None else None
    p.active = bool(d.get("active", True))
    p.dropship = bool(d.get("dropship", True))
    p.supplier_url = str(d.get("supplier_url") or "")
    p.supplier_cost = int(d.get("supplier_cost") or 0)
    p.supplier_shipping = int(d.get("supplier_shipping") or 0)
    p.delivery_days = str(d.get("delivery_days") or p.delivery_days or "")[:40]
    p.sku = str(d.get("sku") or "")[:80]
    db.session.commit()
    return jsonify({"id": p.id, "created": created})


@app.route("/api/integration/v1/orders")
@api_auth
def api_orders():
    q = Order.query
    since = request.args.get("since")
    if since:
        try:
            q = q.filter(Order.created >= datetime.fromisoformat(since.replace("Z", "")))
        except ValueError:
            return jsonify({"error": "since must be ISO date"}), 400
    if request.args.get("status"):
        q = q.filter_by(status=request.args["status"])
    limit = min(request.args.get("limit", 200, type=int), 1000)
    return jsonify({"orders": [order_json(o) for o in q.order_by(Order.created.desc()).limit(limit)]})


@app.route("/api/integration/v1/orders/<code>", methods=["PATCH"])
@api_auth
def api_order_update(code):
    o = Order.query.filter_by(code=code.upper()).first()
    if not o:
        return jsonify({"error": "order not found"}), 404
    d = request.get_json(silent=True) or {}
    if "supplier_order_no" in d:
        if d["supplier_order_no"] and o.payment_status != "paid" and o.payment_method != "cod":
            return jsonify({"error": "customer payment not verified"}), 400
        o.supplier_order_no = str(d["supplier_order_no"]).strip()[:60]
    if "tracking_no" in d:
        new_tracking = str(d["tracking_no"]).strip()[:60]
        if new_tracking and not o.tracking_no and o.status in ("new", "processing"):
            o.status = "shipped"
        o.tracking_no = new_tracking
    if d.get("status") in ORDER_STATUSES:
        if d["status"] == "cancelled" and o.status != "cancelled":
            for it in o.items:
                prod = db.session.get(Product, it.product_id) if it.product_id else None
                if prod and not prod.dropship:
                    prod.stock += it.qty
        o.status = d["status"]
    db.session.commit()
    return jsonify({"order": order_json(o)})


@app.route("/api/integration/v1/images", methods=["POST"])
@api_auth
def api_image_upload():
    d = request.get_json(silent=True) or {}
    mime = str(d.get("mime") or "")
    if mime not in ("image/jpeg", "image/png", "image/webp"):
        return jsonify({"error": "only jpeg, png or webp images"}), 400
    try:
        raw = base64.b64decode(str(d.get("data") or ""), validate=True)
    except (ValueError, TypeError):
        return jsonify({"error": "invalid image data"}), 400
    if not raw or len(raw) > 2 * 1024 * 1024:
        return jsonify({"error": "image must be under 2 MB"}), 400
    img = StoredImage(mime=mime, data=raw)
    db.session.add(img)
    db.session.commit()
    base = BASE_URL or request.url_root.rstrip("/")
    if base.startswith("http://") and "localhost" not in base and "127.0.0.1" not in base:
        base = "https://" + base[len("http://"):]
    ext = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}[mime]
    return jsonify({"url": f"{base}/img/{img.id}.{ext}", "id": img.id})


@app.route("/img/<int:image_id>.<ext>")
def stored_image(image_id, ext):
    img = db.session.get(StoredImage, image_id)
    if not img:
        abort(404)
    resp = make_response(img.data)
    resp.headers["Content-Type"] = img.mime
    resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return resp


@app.errorhandler(404)
def not_found(e):
    return render_template("message.html", title="Page not found",
                           text="We couldn't find what you were looking for."), 404


# ---------------------------------------------------------------- setup
def seed():
    if Category.query.first():
        return
    cats = {n: Category(name=n) for n in ["Electronics", "Fashion", "Home & Kitchen", "Beauty", "Groceries"]}
    db.session.add_all(cats.values())
    img = "https://picsum.photos/seed/{}/600/600"
    demo = [
        ("Wireless Earbuds", "Electronics", 2499, 3200, 25, True, "Bluetooth 5.3 earbuds with charging case and 24h battery."),
        ("Smart Watch", "Electronics", 4999, 6500, 12, True, "Fitness tracking, heart rate, notifications. Works with Android & iPhone."),
        ("Power Bank 20000mAh", "Electronics", 2200, None, 30, False, "Fast-charge power bank with two USB ports and USB-C."),
        ("Men's Cotton T-Shirt", "Fashion", 850, None, 50, False, "100% cotton, available in M, L, XL."),
        ("Ladies Handbag", "Fashion", 2800, 3500, 8, True, "Stylish faux-leather handbag with zip pockets."),
        ("Non-stick Frying Pan", "Home & Kitchen", 1650, None, 20, False, "28cm non-stick pan, suitable for gas and electric cookers."),
        ("Electric Kettle 1.8L", "Home & Kitchen", 1999, 2400, 15, True, "Stainless steel, auto shut-off, boils in minutes."),
        ("Body Lotion 400ml", "Beauty", 650, None, 40, False, "Moisturising lotion with shea butter."),
        ("Basmati Rice 5kg", "Groceries", 1250, None, 35, False, "Long-grain aromatic basmati rice."),
        ("Cooking Oil 3L", "Groceries", 1100, 1250, 3, False, "Pure vegetable cooking oil."),
    ]
    for i, (name, cat, price, old, stock, feat, desc) in enumerate(demo):
        ds = name in ("Wireless Earbuds", "Smart Watch", "Ladies Handbag", "Power Bank 20000mAh")
        db.session.add(Product(name=name, category=cats[cat], price=price, old_price=old, stock=stock,
                               featured=feat, description=desc, image=img.format(i + 11),
                               dropship=ds, supplier_url="https://www.aliexpress.com/" if ds else "",
                               supplier_cost=int(price * 0.4) if ds else 0,
                               supplier_shipping=200 if ds else 0))
    db.session.commit()


def add_missing_columns():
    """Add new columns to existing tables (simple auto-migration)."""
    insp = inspect(db.engine)
    for table in db.metadata.sorted_tables:
        if not insp.has_table(table.name):
            continue
        existing = {c["name"] for c in insp.get_columns(table.name)}
        for col in table.columns:
            if col.name in existing:
                continue
            col_type = col.type.compile(dialect=db.engine.dialect)
            default = ""
            if col.default is not None and not callable(col.default.arg):
                val = col.default.arg
                if isinstance(val, bool):
                    val = ("TRUE" if val else "FALSE") if db.engine.dialect.name == "postgresql" else int(val)
                elif isinstance(val, str):
                    val = "'" + val.replace("'", "''") + "'"
                default = f" DEFAULT {val}"
            with db.engine.begin() as conn:
                conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN {col.name} {col_type}{default}'))


with app.app_context():
    db.create_all()
    add_missing_columns()
    if os.environ.get("SEED_DEMO", "1") == "1":
        seed()

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
