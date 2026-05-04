"""
StockFlow Inventory Management System - Backend API
Flask + SQLite + ReportLab PDF generation
"""

import sqlite3, os, io, json
from datetime import datetime, date
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_RIGHT, TA_LEFT
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    HRFlowable, KeepTogether
)
from reportlab.graphics.shapes import Drawing, Rect, String
from reportlab.graphics.charts.barcharts import VerticalBarChart
from reportlab.graphics import renderPDF

app = Flask(__name__)
CORS(app)

# DB_PATH = os.path.join(os.path.dirname(__file__), "inventory.db")
basedir = os.path.abspath(os.path.dirname(__file__))
DB_PATH = os.path.join(basedir, "inventory.db")

# ─────────────────────────────────────────────
# DATABASE SETUP
# ─────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn

def init_db():
    conn = get_db()
    c = conn.cursor()

    c.executescript("""
    CREATE TABLE IF NOT EXISTS products (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        name        TEXT    NOT NULL,
        sku         TEXT,
        category    TEXT    NOT NULL DEFAULT 'Other',
        supplier    TEXT,
        quantity    INTEGER NOT NULL DEFAULT 0,
        low_stock   INTEGER NOT NULL DEFAULT 10,
        cost_price  REAL    NOT NULL DEFAULT 0,
        sell_price  REAL    NOT NULL DEFAULT 0,
        description TEXT,
        created_at  TEXT    DEFAULT (datetime('now')),
        updated_at  TEXT    DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS sales (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        customer     TEXT    DEFAULT 'Walk-in',
        sale_date    TEXT    NOT NULL,
        payment      TEXT    DEFAULT 'Cash',
        notes        TEXT,
        total        REAL    NOT NULL DEFAULT 0,
        created_at   TEXT    DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS sale_items (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        sale_id    INTEGER NOT NULL REFERENCES sales(id) ON DELETE CASCADE,
        product_id INTEGER NOT NULL REFERENCES products(id),
        quantity   INTEGER NOT NULL,
        unit_price REAL    NOT NULL,
        cost_price REAL    NOT NULL DEFAULT 0
    );

    CREATE INDEX IF NOT EXISTS idx_sale_items_sale ON sale_items(sale_id);
    CREATE INDEX IF NOT EXISTS idx_sale_items_prod ON sale_items(product_id);
    """)

    # No seed data - start with a clean empty database
    conn.commit()
    conn.close()

# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

def row_to_dict(row):
    return dict(row) if row else None

def rows_to_list(rows):
    return [dict(r) for r in rows]

# ─────────────────────────────────────────────
# PRODUCTS API
# ─────────────────────────────────────────────

@app.route("/api/products", methods=["GET"])
def get_products():
    conn = get_db()
    search = request.args.get("search","").strip()
    cat    = request.args.get("category","").strip()
    stock  = request.args.get("stock","").strip()
    sql    = "SELECT * FROM products WHERE 1=1"
    params = []
    if search:
        sql += " AND (name LIKE ? OR sku LIKE ? OR category LIKE ?)"
        params += [f"%{search}%"]*3
    if cat:
        sql += " AND category=?"
        params.append(cat)
    if stock == "low":
        sql += " AND quantity>0 AND quantity<=low_stock"
    elif stock == "out":
        sql += " AND quantity=0"
    sql += " ORDER BY name"
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return jsonify(rows_to_list(rows))

@app.route("/api/products/<int:pid>", methods=["GET"])
def get_product(pid):
    conn = get_db()
    row = conn.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "Not found"}), 404
    return jsonify(row_to_dict(row))

@app.route("/api/products", methods=["POST"])
def create_product():
    data = request.json
    if not data.get("name") or not data.get("sell_price"):
        return jsonify({"error": "name and sell_price required"}), 400
    conn = get_db()
    cur = conn.execute(
        """INSERT INTO products(name,sku,category,supplier,quantity,low_stock,cost_price,sell_price,description)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        (data["name"], data.get("sku",""), data.get("category","Other"),
         data.get("supplier",""), int(data.get("quantity",0)),
         int(data.get("low_stock",10)), float(data.get("cost_price",0)),
         float(data["sell_price"]), data.get("description",""))
    )
    pid = cur.lastrowid
    conn.commit()
    row = conn.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
    conn.close()
    return jsonify(row_to_dict(row)), 201

@app.route("/api/products/<int:pid>", methods=["PUT"])
def update_product(pid):
    data = request.json
    conn = get_db()
    conn.execute(
        """UPDATE products SET name=?,sku=?,category=?,supplier=?,quantity=?,
           low_stock=?,cost_price=?,sell_price=?,description=?,updated_at=datetime('now')
           WHERE id=?""",
        (data["name"], data.get("sku",""), data.get("category","Other"),
         data.get("supplier",""), int(data.get("quantity",0)),
         int(data.get("low_stock",10)), float(data.get("cost_price",0)),
         float(data["sell_price"]), data.get("description",""), pid)
    )
    conn.commit()
    row = conn.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
    conn.close()
    return jsonify(row_to_dict(row))

@app.route("/api/products/<int:pid>", methods=["DELETE"])
def delete_product(pid):
    conn = get_db()
    try:
        # Find all sales that contain this product
        affected_sales = conn.execute(
            "SELECT DISTINCT sale_id FROM sale_items WHERE product_id=?", (pid,)
        ).fetchall()

        # Remove this product's line from all sale_items
        conn.execute("DELETE FROM sale_items WHERE product_id=?", (pid,))

        # Recalculate totals for affected sales (or delete sale if now empty)
        for row in affected_sales:
            sid = row["sale_id"]
            remaining = conn.execute(
                "SELECT SUM(quantity*unit_price) as new_total FROM sale_items WHERE sale_id=?", (sid,)
            ).fetchone()
            if remaining["new_total"] is None:
                # Sale has no items left — delete it entirely
                conn.execute("DELETE FROM sales WHERE id=?", (sid,))
            else:
                conn.execute("UPDATE sales SET total=? WHERE id=?", (remaining["new_total"], sid))

        # Now safe to delete the product
        conn.execute("DELETE FROM products WHERE id=?", (pid,))
        conn.commit()
        conn.close()
        return jsonify({"deleted": pid})
    except Exception as e:
        conn.rollback()
        conn.close()
        return jsonify({"error": str(e)}), 500

@app.route("/api/categories", methods=["GET"])
def get_categories():
    conn = get_db()
    rows = conn.execute("SELECT DISTINCT category FROM products ORDER BY category").fetchall()
    conn.close()
    return jsonify([r["category"] for r in rows])

# ─────────────────────────────────────────────
# SALES API
# ─────────────────────────────────────────────

@app.route("/api/sales", methods=["GET"])
def get_sales():
    conn = get_db()
    date_filter = request.args.get("date","")
    sql = "SELECT * FROM sales"
    params = []
    if date_filter:
        sql += " WHERE sale_date=?"
        params.append(date_filter)
    sql += " ORDER BY sale_date DESC, id DESC"
    sales = rows_to_list(conn.execute(sql, params).fetchall())
    for sale in sales:
        items = conn.execute(
            """SELECT si.*, p.name as product_name, p.sku
               FROM sale_items si JOIN products p ON p.id=si.product_id
               WHERE si.sale_id=?""", (sale["id"],)
        ).fetchall()
        sale["items"] = rows_to_list(items)
    conn.close()
    return jsonify(sales)

@app.route("/api/sales/<int:sid>", methods=["GET"])
def get_sale(sid):
    conn = get_db()
    sale = row_to_dict(conn.execute("SELECT * FROM sales WHERE id=?", (sid,)).fetchone())
    if not sale:
        return jsonify({"error": "Not found"}), 404
    items = conn.execute(
        """SELECT si.*, p.name as product_name, p.sku
           FROM sale_items si JOIN products p ON p.id=si.product_id
           WHERE si.sale_id=?""", (sid,)
    ).fetchall()
    sale["items"] = rows_to_list(items)
    conn.close()
    return jsonify(sale)

@app.route("/api/sales", methods=["POST"])
def create_sale():
    data = request.json
    items = data.get("items", [])
    if not items:
        return jsonify({"error": "No items provided"}), 400

    conn = get_db()
    total = 0.0

    # Validate stock
    for it in items:
        prod = row_to_dict(conn.execute("SELECT * FROM products WHERE id=?", (it["product_id"],)).fetchone())
        if not prod:
            conn.close(); return jsonify({"error": f"Product {it['product_id']} not found"}), 400
        if it["quantity"] > prod["quantity"]:
            conn.close(); return jsonify({"error": f"Insufficient stock for {prod['name']}. Available: {prod['quantity']}"}), 400
        total += it["quantity"] * prod["sell_price"]

    sale_date = data.get("sale_date", date.today().isoformat())
    cur = conn.execute(
        "INSERT INTO sales(customer,sale_date,payment,notes,total) VALUES(?,?,?,?,?)",
        (data.get("customer","Walk-in"), sale_date,
         data.get("payment","Cash"), data.get("notes",""), total)
    )
    sid = cur.lastrowid

    for it in items:
        prod = row_to_dict(conn.execute("SELECT * FROM products WHERE id=?", (it["product_id"],)).fetchone())
        conn.execute(
            "INSERT INTO sale_items(sale_id,product_id,quantity,unit_price,cost_price) VALUES(?,?,?,?,?)",
            (sid, it["product_id"], it["quantity"], prod["sell_price"], prod["cost_price"])
        )
        conn.execute("UPDATE products SET quantity=quantity-? WHERE id=?", (it["quantity"], it["product_id"]))

    conn.commit()
    sale = row_to_dict(conn.execute("SELECT * FROM sales WHERE id=?", (sid,)).fetchone())
    sale_items = rows_to_list(conn.execute(
        "SELECT si.*, p.name as product_name FROM sale_items si JOIN products p ON p.id=si.product_id WHERE si.sale_id=?", (sid,)
    ).fetchall())
    sale["items"] = sale_items
    conn.close()
    return jsonify(sale), 201

@app.route("/api/sales/<int:sid>", methods=["DELETE"])
def delete_sale(sid):
    conn = get_db()
    # Restore stock
    items = conn.execute("SELECT * FROM sale_items WHERE sale_id=?", (sid,)).fetchall()
    for it in items:
        conn.execute("UPDATE products SET quantity=quantity+? WHERE id=?", (it["quantity"], it["product_id"]))
    conn.execute("DELETE FROM sales WHERE id=?", (sid,))
    conn.commit()
    conn.close()
    return jsonify({"deleted": sid})

# ─────────────────────────────────────────────
# DASHBOARD API
# ─────────────────────────────────────────────

@app.route("/api/dashboard", methods=["GET"])
def dashboard():
    conn = get_db()
    p = conn.execute("""
        SELECT COUNT(*) as total,
               SUM(quantity*cost_price) as stock_value,
               SUM(CASE WHEN quantity=0 THEN 1 ELSE 0 END) as out_of_stock,
               SUM(CASE WHEN quantity>0 AND quantity<=low_stock THEN 1 ELSE 0 END) as low_stock
        FROM products
    """).fetchone()
    s = conn.execute("""
        SELECT COUNT(*) as total_sales, COALESCE(SUM(total),0) as revenue,
               COALESCE(SUM(si.quantity*si.unit_price - si.quantity*si.cost_price),0) as profit
        FROM sales sa JOIN sale_items si ON si.sale_id=sa.id
    """).fetchone()
    conn.close()
    return jsonify({**dict(p), **dict(s)})

# ─────────────────────────────────────────────
# REPORT DATA API
# ─────────────────────────────────────────────

@app.route("/api/reports/revenue", methods=["GET"])
def report_revenue():
    conn = get_db()
    rows = conn.execute("""
        SELECT sale_date, SUM(total) as revenue,
               COUNT(*) as orders
        FROM sales GROUP BY sale_date ORDER BY sale_date DESC LIMIT 30
    """).fetchall()
    conn.close()
    return jsonify(rows_to_list(rows))

@app.route("/api/reports/top-products", methods=["GET"])
def report_top_products():
    conn = get_db()
    rows = conn.execute("""
        SELECT p.name, p.category, p.sku,
               SUM(si.quantity) as units_sold,
               SUM(si.quantity*si.unit_price) as revenue,
               SUM(si.quantity*(si.unit_price-si.cost_price)) as profit
        FROM sale_items si JOIN products p ON p.id=si.product_id
        GROUP BY si.product_id ORDER BY revenue DESC LIMIT 10
    """).fetchall()
    conn.close()
    return jsonify(rows_to_list(rows))

@app.route("/api/reports/by-category", methods=["GET"])
def report_by_category():
    conn = get_db()
    rows = conn.execute("""
        SELECT p.category,
               SUM(si.quantity) as units_sold,
               SUM(si.quantity*si.unit_price) as revenue,
               SUM(si.quantity*(si.unit_price-si.cost_price)) as profit
        FROM sale_items si JOIN products p ON p.id=si.product_id
        GROUP BY p.category ORDER BY revenue DESC
    """).fetchall()
    conn.close()
    return jsonify(rows_to_list(rows))

# ─────────────────────────────────────────────
# PDF REPORT GENERATION
# ─────────────────────────────────────────────

BRAND_DARK  = colors.HexColor("#0f172a")
BRAND_BLUE  = colors.HexColor("#3b82f6")
BRAND_GREEN = colors.HexColor("#10b981")
BRAND_AMBER = colors.HexColor("#f59e0b")
BRAND_RED   = colors.HexColor("#ef4444")
BRAND_LIGHT = colors.HexColor("#f8fafc")
BRAND_BORDER= colors.HexColor("#e2e8f0")
BRAND_MUTED = colors.HexColor("#64748b")
WHITE       = colors.white

def make_styles():
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle("title", parent=base["Normal"],
            fontSize=22, fontName="Helvetica-Bold", textColor=BRAND_DARK,
            spaceAfter=4),
        "subtitle": ParagraphStyle("subtitle", parent=base["Normal"],
            fontSize=11, fontName="Helvetica", textColor=BRAND_MUTED,
            spaceAfter=2),
        "section": ParagraphStyle("section", parent=base["Normal"],
            fontSize=13, fontName="Helvetica-Bold", textColor=BRAND_DARK,
            spaceBefore=14, spaceAfter=6),
        "normal": ParagraphStyle("normal", parent=base["Normal"],
            fontSize=9, fontName="Helvetica", textColor=BRAND_DARK, leading=13),
        "small": ParagraphStyle("small", parent=base["Normal"],
            fontSize=8, fontName="Helvetica", textColor=BRAND_MUTED),
        "kpi_val": ParagraphStyle("kpi_val", parent=base["Normal"],
            fontSize=18, fontName="Helvetica-Bold", textColor=BRAND_BLUE,
            alignment=TA_CENTER),
        "kpi_lbl": ParagraphStyle("kpi_lbl", parent=base["Normal"],
            fontSize=8, fontName="Helvetica", textColor=BRAND_MUTED,
            alignment=TA_CENTER),
    }

def pkr(n):
    return f"Rs {float(n):,.0f}"

def header_footer(canvas, doc):
    canvas.saveState()
    w, h = A4
    # Top bar
    canvas.setFillColor(BRAND_DARK)
    canvas.rect(0, h-28*mm, w, 28*mm, fill=1, stroke=0)
    canvas.setFillColor(WHITE)
    canvas.setFont("Helvetica-Bold", 14)
    canvas.drawString(20*mm, h-16*mm, "StockFlow Inventory Management")
    canvas.setFont("Helvetica", 9)
    canvas.drawRightString(w-20*mm, h-16*mm, f"Generated: {datetime.now().strftime('%d %b %Y, %H:%M')}")
    # Footer
    canvas.setFillColor(BRAND_LIGHT)
    canvas.rect(0, 0, w, 12*mm, fill=1, stroke=0)
    canvas.setFillColor(BRAND_MUTED)
    canvas.setFont("Helvetica", 8)
    canvas.drawString(20*mm, 4*mm, "StockFlow — Confidential Business Report")
    canvas.drawRightString(w-20*mm, 4*mm, f"Page {doc.page}")
    canvas.restoreState()

def kpi_table(data_list):
    """data_list: [(value, label, color), ...]"""
    S = make_styles()
    cells = []
    for val, lbl, col in data_list:
        vs = ParagraphStyle("v", parent=S["kpi_val"], textColor=col)
        cells.append([Paragraph(val, vs), Paragraph(lbl, S["kpi_lbl"])])
    cols = len(data_list)
    col_w = (A4[0] - 40*mm) / cols
    t = Table([[c[0] for c in cells],[c[1] for c in cells]],
              colWidths=[col_w]*cols)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0,0),(-1,-1), BRAND_LIGHT),
        ("ROWBACKGROUNDS",(0,0),(-1,-1),[BRAND_LIGHT, BRAND_LIGHT]),
        ("BOX",(0,0),(-1,-1), 0.5, BRAND_BORDER),
        ("INNERGRID",(0,0),(-1,-1), 0.3, BRAND_BORDER),
        ("VALIGN",(0,0),(-1,-1),"MIDDLE"),
        ("TOPPADDING",(0,0),(-1,-1),8),
        ("BOTTOMPADDING",(0,0),(-1,-1),8),
    ]))
    return t

def styled_table(headers, rows, col_widths=None):
    S = make_styles()
    hdr_style = ParagraphStyle("th", parent=S["small"],
        textColor=WHITE, fontName="Helvetica-Bold", fontSize=8)
    data = [[Paragraph(h, hdr_style) for h in headers]]
    for i, row in enumerate(rows):
        styled_row = []
        for cell in row:
            p = Paragraph(str(cell), S["normal"] if i%2==0 else
                ParagraphStyle("alt", parent=S["normal"], backColor=BRAND_LIGHT))
            styled_row.append(p)
        data.append(styled_row)
    if col_widths is None:
        n = len(headers)
        col_widths = [(A4[0]-40*mm)/n]*n
    t = Table(data, colWidths=col_widths, repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0,0),(-1,0), BRAND_DARK),
        ("ROWBACKGROUNDS",(0,1),(-1,-1),[WHITE, BRAND_LIGHT]),
        ("GRID",(0,0),(-1,-1), 0.3, BRAND_BORDER),
        ("VALIGN",(0,0),(-1,-1),"MIDDLE"),
        ("TOPPADDING",(0,0),(-1,-1),5),
        ("BOTTOMPADDING",(0,0),(-1,-1),5),
        ("LEFTPADDING",(0,0),(-1,-1),6),
        ("RIGHTPADDING",(0,0),(-1,-1),6),
    ]))
    return t

@app.route("/api/reports/pdf/inventory", methods=["GET"])
def pdf_inventory():
    conn = get_db()
    products = rows_to_list(conn.execute("SELECT * FROM products ORDER BY category, name").fetchall())
    dash = row_to_dict(conn.execute("""
        SELECT COUNT(*) as total, SUM(quantity*cost_price) as stock_value,
               SUM(CASE WHEN quantity=0 THEN 1 ELSE 0 END) as out_of_stock,
               SUM(CASE WHEN quantity>0 AND quantity<=low_stock THEN 1 ELSE 0 END) as low_stock
        FROM products""").fetchone())
    conn.close()

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
        topMargin=32*mm, bottomMargin=18*mm,
        leftMargin=20*mm, rightMargin=20*mm)
    S = make_styles()
    story = []

    story.append(Paragraph("Inventory Status Report", S["title"]))
    story.append(Paragraph(f"As of {date.today().strftime('%d %B %Y')} — {len(products)} products listed", S["subtitle"]))
    story.append(Spacer(1, 6*mm))
    story.append(HRFlowable(width="100%", thickness=0.5, color=BRAND_BORDER))
    story.append(Spacer(1, 4*mm))

    story.append(kpi_table([
        (str(dash["total"]),           "Total Products",   BRAND_BLUE),
        (pkr(dash["stock_value"] or 0),"Stock Value",      BRAND_GREEN),
        (str(dash["low_stock"] or 0),  "Low Stock Items",  BRAND_AMBER),
        (str(dash["out_of_stock"] or 0),"Out of Stock",    BRAND_RED),
    ]))
    story.append(Spacer(1, 6*mm))

    story.append(Paragraph("Product Listing", S["section"]))
    cw = [12*mm,50*mm,28*mm,22*mm,20*mm,22*mm,22*mm,24*mm]
    rows = []
    for p in products:
        margin = round((p["sell_price"]-p["cost_price"])/p["sell_price"]*100) if p["sell_price"] else 0
        if p["quantity"] == 0:   status = "Out of Stock"
        elif p["quantity"] <= p["low_stock"]: status = "Low Stock"
        else: status = "In Stock"
        rows.append([
            str(p["id"]), p["name"], p["category"],
            p["sku"] or "—", str(p["quantity"]),
            pkr(p["cost_price"]), pkr(p["sell_price"]),
            f"{margin}% / {status}"
        ])
    story.append(styled_table(
        ["ID","Product Name","Category","SKU","Qty","Cost","Price","Margin/Status"],
        rows, cw
    ))
    story.append(Spacer(1, 4*mm))

    # Category summary
    cats = {}
    for p in products:
        c = p["category"]
        if c not in cats: cats[c] = {"count":0,"qty":0,"value":0}
        cats[c]["count"] += 1
        cats[c]["qty"] += p["quantity"]
        cats[c]["value"] += p["quantity"]*p["cost_price"]
    story.append(Paragraph("Summary by Category", S["section"]))
    cat_rows = [[c, str(v["count"]), str(v["qty"]), pkr(v["value"])]
                for c,v in sorted(cats.items())]
    story.append(styled_table(["Category","Products","Total Qty","Stock Value"], cat_rows,
        [55*mm,35*mm,35*mm,45*mm]))

    doc.build(story, onFirstPage=header_footer, onLaterPages=header_footer)
    buf.seek(0)
    return send_file(buf, mimetype="application/pdf",
        as_attachment=True, download_name="inventory_report.pdf")


@app.route("/api/reports/pdf/sales", methods=["GET"])
def pdf_sales():
    date_from = request.args.get("from", "")
    date_to   = request.args.get("to",   "")
    conn = get_db()
    sql = """SELECT sa.*, 
             GROUP_CONCAT(p.name||' x'||si.quantity, '; ') as items_desc,
             SUM(si.quantity*(si.unit_price-si.cost_price)) as profit
             FROM sales sa
             JOIN sale_items si ON si.sale_id=sa.id
             JOIN products p ON p.id=si.product_id"""
    params = []
    if date_from and date_to:
        sql += " WHERE sa.sale_date BETWEEN ? AND ?"
        params = [date_from, date_to]
    elif date_from:
        sql += " WHERE sa.sale_date >= ?"
        params = [date_from]
    sql += " GROUP BY sa.id ORDER BY sa.sale_date DESC, sa.id DESC"
    sales = rows_to_list(conn.execute(sql, params).fetchall())

    top = rows_to_list(conn.execute("""
        SELECT p.name, SUM(si.quantity) as units, SUM(si.quantity*si.unit_price) as rev,
               SUM(si.quantity*(si.unit_price-si.cost_price)) as profit
        FROM sale_items si JOIN products p ON p.id=si.product_id
        GROUP BY si.product_id ORDER BY rev DESC LIMIT 5
    """).fetchall())

    by_cat = rows_to_list(conn.execute("""
        SELECT p.category, SUM(si.quantity*si.unit_price) as revenue,
               SUM(si.quantity*(si.unit_price-si.cost_price)) as profit
        FROM sale_items si JOIN products p ON p.id=si.product_id
        GROUP BY p.category ORDER BY revenue DESC
    """).fetchall())
    conn.close()

    total_rev = sum(s["total"] for s in sales)
    total_profit = sum(s["profit"] or 0 for s in sales)
    total_orders = len(sales)
    avg_order = total_rev/total_orders if total_orders else 0

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
        topMargin=32*mm, bottomMargin=18*mm,
        leftMargin=20*mm, rightMargin=20*mm)
    S = make_styles()
    story = []

    period = f"{date_from} to {date_to}" if date_from else "All Time"
    story.append(Paragraph("Sales & Revenue Report", S["title"]))
    story.append(Paragraph(f"Period: {period} — {total_orders} transactions", S["subtitle"]))
    story.append(Spacer(1, 6*mm))
    story.append(HRFlowable(width="100%", thickness=0.5, color=BRAND_BORDER))
    story.append(Spacer(1, 4*mm))

    story.append(kpi_table([
        (str(total_orders),   "Total Orders",  BRAND_BLUE),
        (pkr(total_rev),      "Total Revenue", BRAND_GREEN),
        (pkr(total_profit),   "Net Profit",    BRAND_GREEN),
        (pkr(avg_order),      "Avg Order",     BRAND_AMBER),
    ]))
    story.append(Spacer(1, 6*mm))

    story.append(Paragraph("Top 5 Products by Revenue", S["section"]))
    story.append(styled_table(
        ["Product","Units Sold","Revenue","Profit"],
        [[r["name"], str(r["units"]), pkr(r["rev"]), pkr(r["profit"])] for r in top],
        [70*mm,30*mm,40*mm,30*mm]
    ))
    story.append(Spacer(1, 4*mm))

    story.append(Paragraph("Revenue by Category", S["section"]))
    story.append(styled_table(
        ["Category","Revenue","Profit"],
        [[r["category"], pkr(r["revenue"]), pkr(r["profit"])] for r in by_cat],
        [70*mm,55*mm,45*mm]
    ))
    story.append(Spacer(1, 4*mm))

    story.append(Paragraph("Sales Transaction Log", S["section"]))
    cw2 = [10*mm,22*mm,30*mm,70*mm,22*mm,16*mm]
    rows = [[str(s["id"]), s["sale_date"], s["customer"] or "Walk-in",
             (s["items_desc"] or "")[:55], pkr(s["total"]), s["payment"]]
            for s in sales]
    story.append(styled_table(["#","Date","Customer","Items","Total","Payment"], rows, cw2))

    doc.build(story, onFirstPage=header_footer, onLaterPages=header_footer)
    buf.seek(0)
    return send_file(buf, mimetype="application/pdf",
        as_attachment=True, download_name="sales_report.pdf")


@app.route("/api/reports/pdf/sale/<int:sid>", methods=["GET"])
def pdf_invoice(sid):
    conn = get_db()
    sale = row_to_dict(conn.execute("SELECT * FROM sales WHERE id=?", (sid,)).fetchone())
    if not sale:
        conn.close(); return jsonify({"error": "Not found"}), 404
    items = rows_to_list(conn.execute(
        """SELECT si.*, p.name, p.sku, p.category
           FROM sale_items si
           JOIN products p ON p.id = si.product_id
           WHERE si.sale_id = ?""", (sid,)
    ).fetchall())
    conn.close()

    total         = sale["total"]
    inv_num       = f"INV-{str(sid).zfill(5)}"
    generated     = datetime.now().strftime("%d %B %Y, %H:%M")
    sale_date_fmt = sale["sale_date"]
    customer      = sale["customer"] or "Walk-in Customer"
    payment       = sale["payment"]
    notes         = sale.get("notes", "") or ""

    # ── PAGE SETUP ────────────────────────────────────────────────
    buf = io.BytesIO()
    PAGE_W, PAGE_H = A4
    LEFT = RIGHT = 18*mm
    TOP_BAND   = 44*mm   # height of dark header band drawn by canvas
    BOT_BAND   = 14*mm   # height of light footer band
    BODY_W     = PAGE_W - LEFT - RIGHT

    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        topMargin    = TOP_BAND + 8*mm,   # content starts below header band
        bottomMargin = BOT_BAND + 6*mm,
        leftMargin   = LEFT,
        rightMargin  = RIGHT,
    )

    S = make_styles()

    # ── CANVAS HEADER / FOOTER ────────────────────────────────────
    def draw_page(canvas, doc):
        canvas.saveState()

        # ── Header dark band ──
        canvas.setFillColor(BRAND_DARK)
        canvas.rect(0, PAGE_H - TOP_BAND, PAGE_W, TOP_BAND, fill=1, stroke=0)

        # Blue bottom edge of header
        canvas.setFillColor(BRAND_BLUE)
        canvas.rect(0, PAGE_H - TOP_BAND - 2*mm, PAGE_W, 2*mm, fill=1, stroke=0)

        # Left: brand
        canvas.setFillColor(WHITE)
        canvas.setFont("Helvetica-Bold", 20)
        canvas.drawString(LEFT, PAGE_H - 20*mm, "StockFlow")
        canvas.setFont("Helvetica", 9)
        canvas.setFillColor(colors.HexColor("#94a3b8"))
        canvas.drawString(LEFT, PAGE_H - 28*mm, "Inventory Management System")

        # Right: invoice number + label
        canvas.setFillColor(WHITE)
        canvas.setFont("Helvetica-Bold", 22)
        canvas.drawRightString(PAGE_W - RIGHT, PAGE_H - 20*mm, inv_num)
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(colors.HexColor("#94a3b8"))
        canvas.drawRightString(PAGE_W - RIGHT, PAGE_H - 28*mm, "SALES INVOICE")

        # Generated date (centered in header)
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(colors.HexColor("#64748b"))
        canvas.drawCentredString(PAGE_W / 2, PAGE_H - 38*mm, f"Generated: {generated}")

        # ── Footer light band ──
        canvas.setFillColor(colors.HexColor("#f1f5f9"))
        canvas.rect(0, 0, PAGE_W, BOT_BAND, fill=1, stroke=0)
        canvas.setFillColor(colors.HexColor("#64748b"))
        canvas.setFont("Helvetica", 8)
        canvas.drawString(LEFT, 5*mm, "StockFlow  -  Confidential")
        canvas.drawRightString(PAGE_W - RIGHT, 5*mm, "Thank you for your business!")

        canvas.restoreState()

    story = []

    # ── BILL-TO / META BLOCK ──────────────────────────────────────
    lbl_s = ParagraphStyle("inv_lbl", fontSize=7, fontName="Helvetica-Bold",
                           textColor=BRAND_MUTED, spaceAfter=3)
    val_s = ParagraphStyle("inv_val", fontSize=11, fontName="Helvetica-Bold",
                           textColor=BRAND_DARK)
    paid_s = ParagraphStyle("inv_paid", fontSize=11, fontName="Helvetica-Bold",
                            textColor=BRAND_GREEN)

    col4 = BODY_W / 4
    meta = Table(
        [
            [Paragraph("BILL TO",        lbl_s),
             Paragraph("INVOICE DATE",   lbl_s),
             Paragraph("PAYMENT METHOD", lbl_s),
             Paragraph("STATUS",         lbl_s)],
            [Paragraph(customer,         val_s),
             Paragraph(sale_date_fmt,    val_s),
             Paragraph(payment,          val_s),
             Paragraph("PAID",           paid_s)],
        ],
        colWidths=[col4] * 4,
    )
    meta.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, -1), colors.HexColor("#f8fafc")),
        ("BOX",           (0, 0), (-1, -1), 0.5, BRAND_BORDER),
        ("INNERGRID",     (0, 0), (-1, -1), 0.3, BRAND_BORDER),
        ("TOPPADDING",    (0, 0), (-1, -1), 9),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 9),
        ("LEFTPADDING",   (0, 0), (-1, -1), 10),
        ("VALIGN",        (0, 0), (-1, -1), "TOP"),
    ]))
    story.append(meta)
    story.append(Spacer(1, 7*mm))

    # ── ITEMS TABLE ───────────────────────────────────────────────
    story.append(Paragraph("Items", ParagraphStyle("sec", fontSize=11,
        fontName="Helvetica-Bold", textColor=BRAND_DARK, spaceAfter=4)))

    # Column widths: #, SKU, Product, Category, Qty, Unit Price, Subtotal
    cw = [8*mm, 20*mm, 60*mm, 30*mm, 14*mm, 28*mm, 28*mm]

    rows = []
    for i, it in enumerate(items):
        rows.append([
            str(i + 1),
            it["sku"] or "-",
            it["name"],
            it["category"],
            str(it["quantity"]),
            pkr(it["unit_price"]),
            pkr(it["unit_price"] * it["quantity"]),
        ])

    story.append(styled_table(
        ["#", "SKU", "Product", "Category", "Qty", "Unit Price", "Subtotal"],
        rows, cw
    ))
    story.append(Spacer(1, 8*mm))

    # ── TOTALS (right-aligned block, no profit) ───────────────────
    lbl_r = ParagraphStyle("tot_l", fontSize=10, textColor=BRAND_MUTED,
                           alignment=TA_RIGHT)
    val_r = ParagraphStyle("tot_v", fontSize=10, fontName="Helvetica-Bold",
                           textColor=BRAND_DARK, alignment=TA_RIGHT)
    grand_l = ParagraphStyle("grd_l", fontSize=13, fontName="Helvetica-Bold",
                              textColor=WHITE, alignment=TA_RIGHT)
    grand_v = ParagraphStyle("grd_v", fontSize=13, fontName="Helvetica-Bold",
                              textColor=WHITE, alignment=TA_RIGHT)

    TOT_W = 170*mm
    tot = Table(
        [
            [Paragraph("Subtotal", lbl_r),      Paragraph(pkr(total), val_r)],
            [Paragraph("Tax (0%)", lbl_r),       Paragraph("Rs 0",     val_r)],
            [Paragraph("TOTAL AMOUNT", grand_l), Paragraph(pkr(total), grand_v)],
        ],
        colWidths=[TOT_W - 55*mm, 55*mm],
    )
    tot.setStyle(TableStyle([
        ("ALIGN",         (0, 0), (-1, -1), "RIGHT"),
        ("TOPPADDING",    (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LEFTPADDING",   (0, 0), (-1, -1), 10),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 10),
        ("LINEABOVE",     (0, 0), (-1, 0),  0.4, BRAND_BORDER),
        ("LINEBELOW",     (0, 1), (-1, 1),  0.4, BRAND_BORDER),
        ("BACKGROUND",    (0, 2), (-1, 2),  BRAND_DARK),
        ("TOPPADDING",    (0, 2), (-1, 2),  10),
        ("BOTTOMPADDING", (0, 2), (-1, 2),  10),
    ]))

    # Push totals to the right
    wrapper = Table([[None, tot]],
                    colWidths=[BODY_W - TOT_W, TOT_W])
    wrapper.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story.append(wrapper)

    # ── NOTES ─────────────────────────────────────────────────────
    if notes:
        story.append(Spacer(1, 6*mm))
        notes_tbl = Table(
            [[Paragraph("Notes:", ParagraphStyle("nl", fontSize=8,
                fontName="Helvetica-Bold", textColor=BRAND_MUTED)),
              Paragraph(notes, S["small"])]],
            colWidths=[16*mm, BODY_W - 16*mm],
        )
        notes_tbl.setStyle(TableStyle([
            ("BACKGROUND",    (0, 0), (-1, -1), colors.HexColor("#fefce8")),
            ("BOX",           (0, 0), (-1, -1), 0.4, colors.HexColor("#fde68a")),
            ("TOPPADDING",    (0, 0), (-1, -1), 7),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
            ("LEFTPADDING",   (0, 0), (-1, -1), 8),
            ("VALIGN",        (0, 0), (-1, -1), "TOP"),
        ]))
        story.append(notes_tbl)

    doc.build(story, onFirstPage=draw_page, onLaterPages=draw_page)
    buf.seek(0)
    return send_file(buf, mimetype="application/pdf",
        as_attachment=True, download_name=f"invoice_{inv_num}.pdf")
# Replace the very bottom of your file with ONLY this:
# ONLY this at the very bottom
with app.app_context():
    init_db()

if __name__ == "__main__":
    app.run(debug=True, port=5050)