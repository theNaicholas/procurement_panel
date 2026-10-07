import frappe
import json
import base64
import time
import threading
from datetime import datetime, timedelta
import pytz
import requests
from deep_translator import GoogleTranslator
from frappe.utils.file_manager import save_file
import ftplib
import io
import hashlib
import difflib
import re


@frappe.whitelist()
def get_local_suppliers():
    try:
        # Added s.supplier_name to the SELECT query
        raw_suppliers = frappe.db.sql("""
            SELECT
                s.name as name, s.supplier_name, s.supplier_primary_contact, s.custom_default_cc as default_cc, c.name as contact, c.is_primary_contact,
                IFNULL(c.email_id, (SELECT email_id FROM `tabContact Email` WHERE parent = c.name ORDER BY is_primary DESC LIMIT 1)) as email
            FROM `tabSupplier` s
            LEFT JOIN `tabDynamic Link` dl ON dl.link_name = s.name AND dl.link_doctype = 'Supplier' AND dl.parenttype = 'Contact'
            LEFT JOIN `tabContact` c ON c.name = dl.parent
            WHERE s.disabled = 0
        """, as_dict=True)

        brand_map = {}
        try:
            supplier_meta = frappe.get_meta("Supplier")
            brand_field = supplier_meta.get_field("custom_authorized_brands")
            if brand_field and brand_field.options:
                child_table = brand_field.options
                child_meta = frappe.get_meta(child_table)
                link_fieldname = next((df.fieldname for df in child_meta.fields if df.fieldtype == "Link"), None)
                if link_fieldname:
                    supplier_brands_sql = frappe.db.sql(f"SELECT parent, `{link_fieldname}` as brand FROM `tab{child_table}`", as_dict=True)
                    for sb in supplier_brands_sql:
                        if sb.brand: brand_map.setdefault(sb.parent, []).append(sb.brand)
        except Exception:
            pass

        unique_sups = {}
        for s in raw_suppliers:
            sup_name = s.get('name')
            human_name = s.get('supplier_name')  # Extract the new field
            contact_name = s.get('contact')
            email = s.get('email')
            score = 0
            if email: score += 10
            if s.get('is_primary_contact'): score += 5
            if contact_name and contact_name == s.get('supplier_primary_contact'): score += 20

            if sup_name not in unique_sups:
                # Include 'supplier_name': human_name in the final dictionary
                unique_sups[sup_name] = {'name': sup_name, 'supplier_name': human_name, 'contact': contact_name, 'email': email, 'default_cc': s.get('default_cc'), 'brands': brand_map.get(sup_name, []), 'country': '', 'score': score}
            else:
                if score > unique_sups[sup_name]['score']:
                    unique_sups[sup_name].update({'contact': contact_name, 'email': email, 'score': score})

        # Same source as Quick Add / SBM Brand View: force email + contact
        # to Supplier.supplier_primary_contact when that field is set.
        primary_rows = frappe.db.sql("""
            SELECT
                s.name as name,
                s.supplier_primary_contact as contact,
                IFNULL(
                    c.email_id,
                    (SELECT email_id FROM `tabContact Email`
                     WHERE parent = c.name
                     ORDER BY is_primary DESC
                     LIMIT 1)
                ) as email
            FROM `tabSupplier` s
            LEFT JOIN `tabContact` c ON c.name = s.supplier_primary_contact
            WHERE s.disabled = 0
              AND s.supplier_primary_contact IS NOT NULL
              AND s.supplier_primary_contact != ''
        """, as_dict=True)
        for p in primary_rows:
            if p.name not in unique_sups:
                continue
            unique_sups[p.name]['contact'] = p.contact
            if p.email:
                unique_sups[p.name]['email'] = p.email

        # Country from Address linked to Supplier.
        # Prefer Supplier.supplier_primary_address; else any linked Address.
        try:
            names = list(unique_sups.keys())
            if names:
                format_strings = ",".join(["%s"] * len(names))
                country_rows = frappe.db.sql(f"""
                    SELECT
                        s.name as name,
                        IFNULL(
                            pa.country,
                            (
                                SELECT a.country
                                FROM `tabAddress` a
                                INNER JOIN `tabDynamic Link` adl
                                    ON adl.parent = a.name
                                   AND adl.parenttype = 'Address'
                                   AND adl.link_doctype = 'Supplier'
                                   AND adl.link_name = s.name
                                WHERE IFNULL(a.country, '') != ''
                                ORDER BY IFNULL(a.is_primary_address, 0) DESC
                                LIMIT 1
                            )
                        ) as country
                    FROM `tabSupplier` s
                    LEFT JOIN `tabAddress` pa ON pa.name = s.supplier_primary_address
                    WHERE s.name IN ({format_strings})
                """, tuple(names), as_dict=True)
                for row in country_rows:
                    if row.name in unique_sups:
                        unique_sups[row.name]['country'] = (row.country or "").strip()
        except Exception:
            pass

        return {"status": "success", "data": list(unique_sups.values())}
    except Exception as e:
        return {"status": "error", "error": str(e)}


@frappe.whitelist()
def search_supplier_contacts(query):
    query_str = f"%{query}%"
    contacts = frappe.db.sql("""
        SELECT name, email_id, first_name, last_name
        FROM `tabContact`
        WHERE (name LIKE %s OR email_id LIKE %s OR first_name LIKE %s)
        AND email_id IS NOT NULL AND email_id != ''
        AND contact_type = 'Supplier'
        LIMIT 15
    """, (query_str, query_str, query_str), as_dict=True)
    return {"status": "success", "data": contacts}


def _escape_like(value):
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )


def _build_global_search_condition(search_query, search_scope="procurement"):
    """
    True substring CONTAINS across the main-table columns of the active view.
    Does NOT touch Supplier Quote / Customer Order child tables.
    Does NOT wrap columns in LOWER() — Request And Quote text columns are CI,
    and LOWER(col) would block any usable index range.
    """
    q = (search_query or "").strip()
    if len(q) < 2:
        return None, []

    escaped = _escape_like(q)
    like_val = f"%{escaped}%"

    if search_scope == "sales":
        columns = [
            "rq.name",
            "rq.ref",
            "rq.customer_ref",
            "rq.feedback",
            "rq.custom_customer_bid_number",
            "rq.custom_sales_status",
            "rq.customer",
            "rq.`div`",
            "rq.country",
            "rq.contact",
            "rq.email_customr",
            "rq.date",
            "rq.due_date",
            "rq.due_time",
            "rq.rp",
            "rq.st",
            "rq.sap",
            "rq.item",
            "rq.qty",
            "rq.unit",
            "rq.brand",
            "rq.part_number",
            "rq.description",
            "rq.incoterm",
            "rq.sale_price",
            "rq.reference_price",
            "rq.note",
            "rq.quotation_sales_price",
            "rq.procurement_status",
        ]
    else:
        columns = [
            "rq.name",
            "rq.ref",
            "rq.custom_sales_status",
            "rq.due_date",
            "rq.due_time",
            "rq.rep",
            "rq.sap",
            "rq.quotation_item",
            "rq.quotation_qty",
            "rq.quotation_unit",
            "rq.quotation_brand",
            "rq.quotation_part_number",
            "rq.quotation_description",
            "rq.quotation_co",
            "rq.quotation_aprox_weight",
            "rq.quotation_incoterm",
            "rq.quotation_sales_price",
            "rq.procurement_status",
            "rq.test",
            "rq.note",
            "rq.quotation_delivery",
            "rq.quotation_note",
        ]
    pieces = []
    params = []

    if q.isdigit():
        pieces.append("rq.name = %s")
        params.append(q)

    for col in columns:
        pieces.append(f"{col} LIKE %s")
        params.append(like_val)

    return "(" + " OR ".join(pieces) + ")", params





def _attach_gl_item_line_tags(data):
    """
    Attach line-level GL Item tags to a page of Request And Quote rows.
    Uses the already-linked Item only (GL Item Match, then custom_gl_item).
    Does not auto-match. Does not write anything.
    Also attaches SO_ORDER_COUNT from draft and submitted Sales Orders.
    """
    if not data:
        return data

    for d in data:
        d["GL_ITEM"] = ""
        d["GL_IN_STOCK"] = 0
        d["GL_STOCK_QTY"] = 0
        d["GL_HAS_PRICE_LIST"] = 0
        d["SO_ORDER_COUNT"] = 0

    page_ids = [str(d.get("ID")) for d in data if d.get("ID") is not None]
    if not page_ids:
        return data

    item_by_raq = {}

    try:
        if frappe.db.exists("DocType", "GL Item Match"):
            format_strings = ",".join(["%s"] * len(page_ids))
            match_rows = frappe.db.sql(
                f"""
                SELECT request_and_quote, item
                FROM `tabGL Item Match`
                WHERE request_and_quote IN ({format_strings})
                  AND IFNULL(item, '') != ''
                """,
                tuple(page_ids),
                as_dict=True,
            )
            for m in match_rows:
                raq = str(m.get("request_and_quote") or "")
                item = (m.get("item") or "").strip()
                if raq and item:
                    item_by_raq[raq] = item
    except Exception:
        pass

    try:
        if frappe.get_meta("Request And Quote").has_field("custom_gl_item"):
            missing = [i for i in page_ids if i not in item_by_raq]
            if missing:
                format_strings = ",".join(["%s"] * len(missing))
                gl_rows = frappe.db.sql(
                    f"""
                    SELECT name, custom_gl_item
                    FROM `tabRequest And Quote`
                    WHERE name IN ({format_strings})
                      AND IFNULL(custom_gl_item, '') != ''
                    """,
                    tuple(missing),
                    as_dict=True,
                )
                for g in gl_rows:
                    item = (g.get("custom_gl_item") or "").strip()
                    if item:
                        item_by_raq[str(g.name)] = item
    except Exception:
        pass

    item_codes = sorted({v for v in item_by_raq.values() if v})
    stock_map = {}
    price_set = set()
    if item_codes:
        format_items = ",".join(["%s"] * len(item_codes))
        try:
            stock_rows = frappe.db.sql(
                f"""
                SELECT item_code, SUM(IFNULL(actual_qty, 0)) AS qty
                FROM `tabBin`
                WHERE item_code IN ({format_items})
                GROUP BY item_code
                """,
                tuple(item_codes),
                as_dict=True,
            )
            for s in stock_rows:
                stock_map[s.item_code] = float(s.qty or 0)
        except Exception:
            pass
        try:
            price_rows = frappe.db.sql(
                f"""
                SELECT DISTINCT item_code
                FROM `tabItem Price`
                WHERE item_code IN ({format_items})
                """,
                tuple(item_codes),
            )
            price_set = {r[0] for r in price_rows if r and r[0]}
        except Exception:
            pass

    for d in data:
        item = item_by_raq.get(str(d.get("ID")), "")
        if not item:
            continue
        qty = float(stock_map.get(item) or 0)
        d["GL_ITEM"] = item
        d["GL_STOCK_QTY"] = qty
        d["GL_IN_STOCK"] = 1 if qty else 0
        d["GL_HAS_PRICE_LIST"] = 1 if item in price_set else 0

    so_orders = {rid: set() for rid in page_ids}
    part_by_raq = {}
    for d in data:
        rid = str(d.get("ID") or "")
        part = str(d.get("PART_NUMBER") or d.get("QUOTE_PART_NUMBER") or d.get("ORIG_PART_NUMBER") or "").strip()
        if rid and part:
            part_by_raq[rid] = part

    so_has_raq = frappe.get_meta("Sales Order Item").has_field("custom_raq")
    so_has_pn = frappe.get_meta("Sales Order Item").has_field("custom_part_number")
    match_parts = []
    params = []
    if so_has_raq:
        match_parts.append("soi.custom_raq IN ({})".format(", ".join(["%s"] * len(page_ids))))
        params.extend(page_ids)
    if item_codes:
        match_parts.append("soi.item_code IN ({})".format(", ".join(["%s"] * len(item_codes))))
        params.extend(item_codes)
    part_numbers = sorted({v for v in part_by_raq.values() if v})
    if so_has_pn and part_numbers:
        match_parts.append("soi.custom_part_number IN ({})".format(", ".join(["%s"] * len(part_numbers))))
        params.extend(part_numbers)

    if match_parts:
        try:
            so_rows = frappe.db.sql(
                """
                SELECT
                    so.name AS sales_order,
                    soi.custom_raq AS custom_raq,
                    soi.item_code AS item_code,
                    {part_select} AS part_number
                FROM `tabSales Order Item` soi
                INNER JOIN `tabSales Order` so ON so.name = soi.parent
                WHERE soi.parenttype = 'Sales Order'
                  AND so.docstatus < 2
                  AND ({match})
                """.format(
                    part_select="soi.custom_part_number" if so_has_pn else "''",
                    match=" OR ".join(match_parts),
                ),
                tuple(params),
                as_dict=True,
            ) or []
        except Exception:
            so_rows = []

        item_to_raqs = {}
        for rid, item in item_by_raq.items():
            item_to_raqs.setdefault(item, []).append(rid)
        part_to_raqs = {}
        for rid, part in part_by_raq.items():
            part_to_raqs.setdefault(part, []).append(rid)

        for row in so_rows:
            so_name = row.sales_order
            raq = str(row.custom_raq or "")
            if raq in so_orders:
                so_orders[raq].add(so_name)
            for rid in item_to_raqs.get(row.item_code or "", []):
                if rid in so_orders:
                    so_orders[rid].add(so_name)
            for rid in part_to_raqs.get(str(row.part_number or ""), []):
                if rid in so_orders:
                    so_orders[rid].add(so_name)

    for d in data:
        d["SO_ORDER_COUNT"] = len(so_orders.get(str(d.get("ID")), set()))

    return data


@frappe.whitelist()
def get_quotes(page=1, page_length=50, sort_by='ID', sort_order='desc', search_query=None, filters=None, skip_count=0, search_scope="procurement", count_only=0):
    try:
        start = (int(page) - 1) * int(page_length)
        skip_count = int(skip_count or 0)
        count_only = int(count_only or 0)
        # We join both tables and alias them to prevent 'Ambiguous column name' errors
        base_sql = "FROM `tabRequest And Quote` rq"
        where_conds = []
        params = []
        # ====================== CACHING LAYER (inside the existing try) ======================
        if isinstance(filters, (list, dict)):
            filter_str = json.dumps(filters, sort_keys=True, default=str)
        else:
            filter_str = filters or ""
        filter_hash = hashlib.md5(filter_str.encode("utf-8")).hexdigest()
        search_part = (search_query or "").replace(" ", "_")[:60]
        cache_key = f"proc_get_quotes_v6:{page}:{page_length}:{sort_by}:{sort_order}:{search_part}:{filter_hash}:{search_scope}:sc{skip_count}:co{count_only}"
        cached = frappe.cache().get_value(cache_key)
        if cached:
            return cached
        # ====================================================================================
        if filters:
            try:
                if isinstance(filters, list):
                    flist = filters
                elif isinstance(filters, str):
                    flist = json.loads(filters)
                else:
                    flist = []
                field_map = {
                    'id': 'rq.name', 'ref': 'rq.ref', 'customer_ref': 'rq.customer_ref',
                    'rfq_date': 'rq.quotation_rfqdate', 'rep': 'rq.rep', 'item': 'rq.quotation_item',
                    'qty': 'rq.quotation_qty', 'unit': 'rq.quotation_unit', 'brand': 'rq.quotation_brand',
                    'part_number': 'rq.quotation_part_number', 'description': 'rq.quotation_description',
                    'co': 'rq.quotation_co', 'weight': 'rq.quotation_aprox_weight', 'incoterm': 'rq.quotation_incoterm',
                    'sales_price': 'rq.quotation_sales_price', 'delivery': 'rq.quotation_delivery', 'note': 'rq.quotation_note',
                    'orig_note': 'rq.note',
                    'due_time': 'rq.due_time', 'sap': 'rq.sap',
                    'custom_sales_status': 'rq.custom_sales_status',
                    'procurement_status': 'rq.procurement_status',
                    'test': 'rq.test',
                    # Added missing Sales fields so the Flip Page inherits filters perfectly
                    'customer': 'rq.customer', 'contact': 'rq.contact',
                    'email_customer': 'rq.email_customr', 'date': 'rq.date',
                    'date_req': 'rq.date_req', 'due_date': 'rq.due_date',
                    'rp': 'rq.rp', 'st': 'rq.st', 'div': 'rq.div',
                    'country': 'rq.country', 'reference_price': 'rq.reference_price',
                    'sale_price': 'rq.sale_price',
                    'feedback': 'rq.feedback',
                    'customer_bid_number': 'rq.custom_customer_bid_number'
                }
                for f in flist:
                    field = f.get('field')
                    op = f.get('operator')
                    val = f.get('value', '')
                    val2 = f.get('value2', '')
                    # Special filter: row became an order (linked Customer Order exists)
                    if field == 'has_customer_order':
                        wants_order = op in ('equals', 'not_empty', 'contains') and str(val) not in ('0', 'false', 'False', '')
                        if op == 'is_empty' or op == 'not_equals' or str(val) in ('0', 'false', 'False'):
                            wants_order = False
                        if wants_order:
                            where_conds.append("EXISTS (SELECT 1 FROM `tabCustomer Order` co WHERE co.id = rq.name)")
                        else:
                            where_conds.append("NOT EXISTS (SELECT 1 FROM `tabCustomer Order` co WHERE co.id = rq.name)")
                        continue
                    # Due Soon (Procurement): empty/null OR exact W-WAITING only.
                    # Any other procurement_status is excluded.
                    if field == 'due_soon_open_status':
                        where_conds.append(
                            "("
                            "rq.procurement_status IS NULL "
                            "OR rq.procurement_status = '' "
                            "OR rq.procurement_status = 'W-WAITING'"
                            ")"
                        )
                        continue
                    # Pending Submission (Sales Today/Future badge):
                    # quoted by procurement, not yet submitted, closed, or awarded.
                    # Blank / NULL sales status is treated as still pending.
                    if field == 'pending_submission':
                        where_conds.append(
                            "("
                            "rq.procurement_status = 'Q-QUOTED' "
                            "AND IFNULL(rq.custom_sales_status, '') NOT IN ("
                            "'S-SUBMITTED', 'C-CLOSED', 'A-AWARDED'"
                            ")"
                            ")"
                        )
                        continue
                    if field not in field_map: continue
                    col = field_map[field]
                    if op == 'is_empty':
                        where_conds.append(f"({col} IS NULL OR {col} = '')")
                    elif op == 'not_empty':
                        where_conds.append(f"({col} IS NOT NULL AND {col} != '')")
                    elif op == 'equals':
                        where_conds.append(f"{col} = %s")
                        params.append(val)
                    elif op == 'not_equals':
                        where_conds.append(f"{col} != %s")
                        params.append(val)
                    elif op == 'gte':
                        where_conds.append(f"{col} >= %s")
                        params.append(val)
                    elif op == 'lte':
                        where_conds.append(f"{col} <= %s")
                        params.append(val)
                    elif op == 'contains':
                        if field == 'sap':
                            # Force the FULLTEXT index and use a cleaner pattern for short codes
                            where_conds.append(f"MATCH({col}) AGAINST(%s IN BOOLEAN MODE)")
                            params.append(f"+{val}*")
                        elif field == 'st':
                            # Special handling for multi-value ST fields
                            where_conds.append(f"""
                                (
                                    {col} = %s
                                    OR {col} LIKE %s
                                    OR {col} LIKE %s
                                    OR {col} LIKE %s
                                )
                            """)
                            params.extend([
                                val,
                                f"{val}/%",
                                f"%/{val}",
                                f"%/{val}/%"
                            ])
                        elif field == 'part_number':
                            # Fast substring filter on quotation_part_number.
                            # Do NOT wrap the column in LOWER() — that blocks a BTREE
                            # index scan. Collation on the column is already CI, so
                            # 'z1134' still matches '53535z1134' and 'z1134565656'.
                            escaped_pn = (
                                str(val)
                                .replace("\\", "\\\\")
                                .replace("%", "\\%")
                                .replace("_", "\\_")
                            )
                            where_conds.append(f"{col} LIKE %s")
                            params.append(f"%{escaped_pn}%")
                        else:
                            # True substring match (case-insensitive) for all other fields
                            where_conds.append(f"LOWER({col}) LIKE LOWER(%s)")
                            params.append(f"%{val}%")
                    elif op == 'not_contains':
                        if field == 'part_number':
                            escaped_pn = (
                                str(val)
                                .replace("\\", "\\\\")
                                .replace("%", "\\%")
                                .replace("_", "\\_")
                            )
                            where_conds.append(f"{col} NOT LIKE %s")
                            params.append(f"%{escaped_pn}%")
                        else:
                            where_conds.append(f"LOWER({col}) NOT LIKE LOWER(%s)")
                            params.append(f"%{val}%")
                    elif op == 'starts_with':
                        if field == 'part_number':
                            escaped_pn = (
                                str(val)
                                .replace("\\", "\\\\")
                                .replace("%", "\\%")
                                .replace("_", "\\_")
                            )
                            where_conds.append(f"{col} LIKE %s")
                            params.append(f"{escaped_pn}%")
                        else:
                            where_conds.append(f"LOWER({col}) LIKE LOWER(%s)")
                            params.append(f"{val}%")
                    elif op == 'regex':
                        where_conds.append(f"{col} REGEXP %s")
                        params.append(val)
                    elif op == 'between':
                        where_conds.append(f"{col} BETWEEN %s AND %s")
                        params.extend([val, val2])
            except Exception as e:
                frappe.log_error("Filter Parse Error", str(e))
        if search_query:
            search_cond, search_params = _build_global_search_condition(
                search_query, search_scope or "procurement"
            )
            if search_cond:
                where_conds.append(search_cond)
                params.extend(search_params)
        where_clause = ""
        if where_conds:
            where_clause = " WHERE " + " AND ".join(where_conds)
        total_count = 0
        use_count_query = bool(where_conds)
        if not where_conds:
            # Unfiltered loads - keep the existing fast path (no COUNT scan at all)
            total_count = 500000
        elif skip_count:
            # Deferred count mode: do not calculate the total yet
            total_count = None
        sort_map = {
            'ID': 'rq.name', 'REF': 'rq.ref', 'CUSTOMER_REF': 'rq.customer_ref', 'RFQDATE': 'rq.quotation_rfqdate',
            'DUE_TIME': 'rq.due_time', 'REP': 'rq.rep', 'SAP': 'rq.sap', 'ITEM': 'rq.quotation_item',
            'QTY': 'rq.quotation_qty', 'UNIT': 'rq.quotation_unit', 'BRAND': 'rq.quotation_brand', 'PART_NUMBER': 'rq.quotation_part_number',
            'DESCRIPTION': 'rq.quotation_description', 'CO': 'rq.quotation_co', 'APROX_WEIGHT': 'rq.quotation_aprox_weight',
            'INCOTERM': 'rq.quotation_incoterm', 'SALES_PRICE': 'rq.quotation_sales_price', 'DELIVERY': 'rq.quotation_delivery', 'NOTE': 'rq.quotation_note',
            'ORIG_NOTE': 'rq.note',
            'CUSTOMER': 'rq.customer', 'CONTACT': 'rq.contact', 'EMAIL_CUSTOMER': 'rq.email_customr',
            'DATE': 'rq.date', 'DATE REQ': 'rq.date_req', 'DUE_DATE': 'rq.due_date',
            'RP': 'rq.rp', 'ST': 'rq.st', 'DIV': 'rq.div', 'COUNTRY': 'rq.country',
            'CREATION_TIME': 'rq.creation',
            'CREATION_DATE': 'rq.creation',
            'CUSTOM_SALES_STATUS': 'rq.custom_sales_status',
            'PROCUREMENT_STATUS': 'rq.procurement_status',
            'TEST': 'rq.test',
            'SALE_PRICE': 'rq.sale_price', 'REFERENCE_PRICE': 'rq.reference_price',
            'FEEDBACK': 'rq.feedback',
            'CUSTOMER_BID_NUMBER': 'rq.custom_customer_bid_number'
        }
        actual_sort = sort_map.get(sort_by, 'rq.name')
        
        # Force MySQL to use WHERE indexes instead of the ORDER BY trap when filtering
        if where_conds and actual_sort in ['rq.name', 'q.name']:
            actual_sort = 'CAST(rq.name AS UNSIGNED)'
            
        count_params = tuple(params)
        data = []
        if not count_only:
            sql = f"""
                SELECT
                    rq.name as ID, rq.ref as REF, rq.country as COUNTRY, rq.rep as REP, 
                    rq.quotation_item as ITEM, rq.quotation_qty as QTY, rq.quotation_unit as UNIT, rq.quotation_part_number as PART_NUMBER, 
                    rq.quotation_brand as BRAND, rq.quotation_description as DESCRIPTION, rq.quotation_co as CO, 
                    rq.quotation_sales_price as SALES_PRICE, rq.quotation_delivery as DELIVERY, 
                    rq.quotation_aprox_weight as APROX_WEIGHT, rq.quotation_incoterm as INCOTERM, 
                    rq.quotation_attachment as ATTACHMENT, rq.quotation_note as NOTE, rq.quotation_rfqdate as RFQDATE, 
                    rq.customer_ref as CUSTOMER_REF,
                    rq.due_date as DUE_DATE, rq.due_time as DUE_TIME, rq.sap as SAP, rq.attachment as RFQ_ATTACHMENT,
                    rq.customer as CUSTOMER, rq.`div` as `DIV`, rq.contact as CONTACT, rq.email_customr as EMAIL_CUSTOMER,
                    rq.date as DATE, rq.st as ST, rq.sale_price as SALE_PRICE, rq.rp as RP,
                    rq.item as ORIG_ITEM, rq.qty as ORIG_QTY, rq.unit as ORIG_UNIT, rq.brand as ORIG_BRAND,
                    rq.part_number as ORIG_PART_NUMBER, rq.description as ORIG_DESCRIPTION, rq.note as ORIG_NOTE,
                    DATE_FORMAT(rq.creation, '%%H:%%i') as CREATION_TIME,
                    DATE_FORMAT(rq.creation, '%%Y-%%m-%%d') as CREATION_DATE,
                    rq.custom_sales_status as CUSTOM_SALES_STATUS,
                    rq.custom_sales_status as CUSTOM_SALES_STATUS,
                    rq.procurement_status as PROCUREMENT_STATUS,
                    rq.test as TEST,
                    rq.modified as MODIFIED,
                    rq.feedback as FEEDBACK,
                    rq.custom_customer_bid_number as CUSTOMER_BID_NUMBER,
                    (SELECT c.customer_details FROM `tabCustomer` c WHERE c.name = rq.customer LIMIT 1) as CUSTOMER_DETAILS
                {base_sql} {where_clause}
                ORDER BY {actual_sort} {sort_order}
                LIMIT %s OFFSET %s
            """
            params.extend([int(page_length), int(start)])
            data = frappe.db.sql(sql, tuple(params), as_dict=True)
            # Flag rows that have at least one linked Customer Order (same link used by the Sales sub-table)
            if data:
                page_ids = [str(d.get("ID")) for d in data if d.get("ID") is not None]
                ordered_set = set()
                if page_ids:
                    format_strings = ",".join(["%s"] * len(page_ids))
                    ordered_rows = frappe.db.sql(
                        f"SELECT DISTINCT id FROM `tabCustomer Order` WHERE id IN ({format_strings})",
                        tuple(page_ids),
                    )
                    ordered_set = {str(r[0]) for r in ordered_rows if r and r[0] is not None}
                for d in data:
                    d["HAS_CUSTOMER_ORDER"] = 1 if str(d.get("ID")) in ordered_set else 0
                _attach_gl_item_line_tags(data)
        # ====================== GET ACCURATE TOTAL FROM COUNT(*) ======================
        if use_count_query and not skip_count:
            if (not count_only) and start == 0 and len(data) < int(page_length):
                total_count = len(data)
            else:
                try:
                    count_sql = f"SELECT COUNT(*) as total {base_sql} {where_clause}"
                    count_result = frappe.db.sql(count_sql, count_params, as_dict=True)
                    total_count = count_result[0]['total'] if count_result else 0
                except Exception:
                    # Fallback (should rarely happen)
                    total_count = len(data)
        # Safely compute total_pages only when we actually have a numeric total_count.
        # When skip_count=1 and filters are active we return None so the frontend
        # can keep showing the previous pagination info while the background count runs.
        if total_count is None:
            total_pages = None
        else:
            total_pages = -(-total_count // int(page_length))
        result = {
            "status": "success",
            "data": data,
            "total_records": total_count,
            "total_pages": total_pages
        }
        frappe.cache().set_value(cache_key, result, expires_in_sec=2)
        return result
    except Exception as e:
        frappe.log_error("Proc Batching Get Quotes Error", str(e))
        return {"status": "error", "error": str(e)}


@frappe.whitelist()
def send_rfq_emails(selected_items_json, suppliers_json, subject, message_body, attachments_json="[]"):
    try:
        items = json.loads(selected_items_json)
        suppliers = json.loads(suppliers_json)
        attachments_data = json.loads(attachments_json)

        if not items or not suppliers:
            return {"status": "error", "error": "Items and Suppliers are required."}

        frappe_attachments = []
        for att in attachments_data:
            frappe_attachments.append({
                "fname": att['filename'],
                "fcontent": base64.b64decode(att['filedata'])
            })

        for sup in suppliers:
            if sup.get('to_email') or sup.get('email'):

                raw_to = sup.get('to_email') or sup.get('email')
                to_emails = [e.strip() for e in raw_to.split(',') if e.strip()]

                cc_emails = []
                if sup.get('cc_email'):
                    cc_emails.extend([e.strip() for e in sup.get('cc_email').split(',') if e.strip()])
                cc_emails.append("rfq@glgeneralindustries.com")

                frappe.sendmail(
                    recipients=to_emails,
                    cc=cc_emails,
                    sender="rfq@glgeneralindustries.com",
                    reply_to="rfq@glgeneralindustries.com",
                    subject=subject,
                    content=message_body,
                    attachments=frappe_attachments,
                    expose_recipients="header",
                    now=True
                )

        ny_tz = pytz.timezone('America/New_York')
        now = datetime.now(ny_tz)
        date_str = now.strftime("%m/%d")
        time_str = now.strftime("%H:%M")
        # Only the part after the status letter goes into the Proc. Notes (test) field
        log_string = f"sent {date_str}//{time_str}"

        item_ids = [i.get('ID') for i in items if i.get('ID')]

        if item_ids:
            format_strings = ','.join(['%s'] * len(item_ids))
            existing_records = frappe.db.sql(
                f"""SELECT name as ID,
                           procurement_status as PROC_STATUS,
                           test as PROC_NOTES
                    FROM `tabRequest And Quote`
                    WHERE name IN ({format_strings})""",
                tuple(item_ids),
                as_dict=True
            )

            for rec in existing_records:
                docname = rec.get('ID')
                current_status = str(rec.get('PROC_STATUS') or "").strip()
                current_notes  = str(rec.get('PROC_NOTES') or "").strip()

                # Already marked as W and already has a “sent \ldots” note → skip
                if current_status == "W" and "sent " in current_notes:
                    continue

                # Use Document API so a proper Version record is created
                # (Activity History will show the change)
                doc = frappe.get_doc("Request And Quote", docname)
                doc.procurement_status = "W-WAITING"

                # Append the new timestamped note (do not lose any previous notes)
                if log_string not in current_notes:
                    if current_notes:
                        doc.test = f"{current_notes}\n{log_string}"
                    else:
                        doc.test = log_string

                doc.flags.ignore_permissions = True
                doc.save()

            frappe.db.commit()

        # Create Supplier Quote records for each selected item and supplier
        for item in items:
            row_id = item.get('ID')
            if not row_id:
                continue
            
            for sup in suppliers:
                sup_name = sup.get('name')
                sup_email = sup.get('to_email') or sup.get('email')
                
                is_blank_supplier = not sup_name or sup_name == 'Unnamed Supplier'
                
                # If it's completely blank with no email, skip it entirely
                if is_blank_supplier and not sup_email:
                    continue
                
                try:
                    field_values = {}

                    if is_blank_supplier:
                        # Map email to email_contact, leaving supplier link field empty
                        field_values["email_contact"] = sup_email
                    else:
                        # Link standard database supplier
                        field_values["supplier"] = sup_name
                        field_values["supplier_name"] = sup_name
                        if sup_email:
                            field_values["email_contact"] = sup_email

                    _insert_supplier_quote(row_id, field_values)
                except Exception as sq_err:
                    frappe.log_error("Auto-Create Supplier Quote Error", str(sq_err))

        return {"status": "success", "message": f"Emails dispatched to {len(suppliers)} suppliers."}

    except Exception as e:
        frappe.log_error("Proc Batching Email Error", str(e))
        return {"status": "error", "error": str(e)}

@frappe.whitelist()
def get_all_brands():
    """Fetches all brands from the database"""
    try:
        return [b.name for b in frappe.get_all("Brand", limit=0)]
    except Exception:
        return []

@frappe.whitelist()
def manage_brand(brand_name):
    """Creates a new brand"""
    try:
        if not frappe.db.exists("Brand", brand_name):
            frappe.get_doc({"doctype": "Brand", "brand": brand_name}).insert(ignore_permissions=True)
            return {"status": "success"}
        return {"status": "error", "error": "Brand already exists"}
    except Exception as e:
        return {"status": "error", "error": str(e)}

@frappe.whitelist()
def manage_supplier(payload):
    """Creates or edits a supplier and its primary contact in real-time"""
    try:
        data = json.loads(payload)
        is_new = data.get('is_new', False)

        sup_name = data.get('supplier_name')
        contact_name = data.get('contact_name')
        contact_email = data.get('contact_email')
        default_cc = data.get('default_cc')
        brands = data.get('brands', [])

        if is_new:
            if frappe.db.exists("Supplier", sup_name):
                return {"status": "error", "error": "Supplier already exists"}

            group_name = frappe.db.get_value("Supplier Group", {}, "name") or "All Supplier Groups"

            sup = frappe.get_doc({
                "doctype": "Supplier",
                "supplier_name": sup_name,
                "supplier_group": group_name
            })
            sup.insert(ignore_permissions=True)
        else:
            sup = frappe.get_doc("Supplier", data.get('original_name'))

        sup_meta = frappe.get_meta("Supplier")
        brand_field = sup_meta.get_field("custom_authorized_brands")
        if brand_field and brand_field.options:
            child_doctype = brand_field.options
            link_fieldname = next((df.fieldname for df in frappe.get_meta(child_doctype).fields if df.fieldtype == "Link"), "brand")
            sup.set("custom_authorized_brands", [])
            for b in brands:
                sup.append("custom_authorized_brands", {link_fieldname: b})
        sup.custom_default_cc = default_cc
        sup.save(ignore_permissions=True)

        selected_contact_id = data.get('selected_contact_id')
        new_primary_contact_name = None

        if is_new and contact_name:
            contact = frappe.get_doc({
                "doctype": "Contact",
                "first_name": contact_name,
                "is_primary_contact": 1,
                "links": [{"link_doctype": "Supplier", "link_name": sup.name}]
            })
            if contact_email:
                contact.append("email_ids", {"email_id": contact_email, "is_primary": 1})
            contact.insert(ignore_permissions=True)
            new_primary_contact_name = contact.name

        elif not is_new:
            if selected_contact_id == 'new_contact' and contact_name:
                linked_contacts = frappe.db.sql("""
                    SELECT c.name FROM `tabContact` c
                    JOIN `tabDynamic Link` dl ON dl.parent = c.name
                    WHERE dl.link_doctype = 'Supplier' AND dl.link_name = %s
                """, sup.name, as_dict=True)

                for lc in linked_contacts:
                    c_doc = frappe.get_doc("Contact", lc.name)
                    if c_doc.is_primary_contact == 1:
                        c_doc.is_primary_contact = 0
                        c_doc.save(ignore_permissions=True)

                contact = frappe.get_doc({
                    "doctype": "Contact",
                    "first_name": contact_name,
                    "is_primary_contact": 1,
                    "links": [{"link_doctype": "Supplier", "link_name": sup.name}]
                })
                if contact_email:
                    contact.append("email_ids", {"email_id": contact_email, "is_primary": 1})
                contact.insert(ignore_permissions=True)
                new_primary_contact_name = contact.name

            elif selected_contact_id and selected_contact_id != 'new_contact':
                linked_contacts = frappe.db.sql("""
                    SELECT c.name FROM `tabContact` c
                    JOIN `tabDynamic Link` dl ON dl.parent = c.name
                    WHERE dl.link_doctype = 'Supplier' AND dl.link_name = %s
                """, sup.name, as_dict=True)

                for lc in linked_contacts:
                    c_doc = frappe.get_doc("Contact", lc.name)
                    is_target = (c_doc.name == selected_contact_id)
                    if c_doc.is_primary_contact != int(is_target):
                        c_doc.is_primary_contact = int(is_target)
                        c_doc.save(ignore_permissions=True)

                new_primary_contact_name = selected_contact_id

        if new_primary_contact_name:
            frappe.db.set_value("Supplier", sup.name, "supplier_primary_contact", new_primary_contact_name)

        return {"status": "success"}
    except Exception as e:
        frappe.log_error("Manage Supplier Error", str(e))
        return {"status": "error", "error": str(e)}

@frappe.whitelist()
def get_supplier_contacts_list(supplier_name):
    try:
        contacts = frappe.db.sql("""
            SELECT c.name, c.first_name, c.last_name, c.email_id, c.is_primary_contact
            FROM `tabContact` c
            JOIN `tabDynamic Link` dl ON dl.parent = c.name
            WHERE dl.link_doctype = 'Supplier' AND dl.link_name = %s
            ORDER BY c.is_primary_contact DESC
        """, supplier_name, as_dict=True)
        return {"status": "success", "data": contacts}
    except Exception as e:
        return {"status": "error", "error": str(e)}

@frappe.whitelist()
def translate_with_llm(text):
    prompt = f"Translate the following technical industrial part description to English. It might be in Spanish, or a mix of both. Reply ONLY with the English translation, no extra text, no conversation, no quotes: {text}"
    try:
        url = 'https://gl-translate.glautomation.duckdns.org/api/generate'
        response = requests.post(url, json={
            "model": "qwen2.5:0.5b",
            "prompt": prompt,
            "stream": False
        }, timeout=20)
        translated = response.json().get('response', '').strip()
        return {"status": "success", "data": translated}
    except Exception as e:
        frappe.log_error("Ollama Translation Error", str(e))
        return {"status": "error", "error": str(e)}

_GOOGLE_TRANSLATE_LOCK = threading.Lock()
_GOOGLE_TRANSLATE_LAST = 0.0
_GOOGLE_TRANSLATE_MIN_INTERVAL = 0.35
_GOOGLE_MAX_CHARS = 4500
_GOOGLE_ROW_SEP = "\n###RFQROW###\n"
_GOOGLE_CUSTOM_DICT = {
    "Bomb": "Pump",
    "bomb": "pump",
    "Fusible": "Fuse",
    "fusible": "fuse",
}
_SPANISH_HINT_RE = re.compile(
    r"[áéíóúñüÁÉÍÓÚÑÜ¿¡]"
    r"|CAPACIDAD|TECNOLOG|DISEÑ|RESPALDO|APLICACI"
    r"|MANTENIMIENTO|VIDA UTIL|BATERIA|BATERÍA"
    r"|\bPARA\b|\bCON\b|\bTODO\b|\bLIBRE\b",
    re.IGNORECASE,
)


def _apply_translate_custom_dict(translated):
    text = translated or ""
    for bad_word, correct_word in _GOOGLE_CUSTOM_DICT.items():
        text = text.replace(bad_word, correct_word)
    return text


def _looks_spanish(text):
    return bool(_SPANISH_HINT_RE.search(text or ""))


def _parse_google_single_json(payload):
    if not payload or not isinstance(payload, list) or not payload[0]:
        return ""
    parts = []
    for chunk in payload[0]:
        if chunk and chunk[0]:
            parts.append(chunk[0])
    return "".join(parts).strip()


def _translate_via_google_json(text, client):
    """
    Unofficial translate.googleapis.com JSON endpoint.
    client=gtx has been globally 429 since ~2026-09-14.
    client=at and client=dict-chrome-ex still return JSON.
    """
    resp = requests.get(
        "https://translate.googleapis.com/translate_a/single",
        params={
            "client": client,
            "sl": "auto",
            "tl": "en",
            "dt": "t",
            "q": text,
        },
        headers={
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json,text/plain,*/*",
        },
        timeout=8,
    )
    if resp.status_code != 200:
        raise Exception("Google {0} HTTP {1}".format(client, resp.status_code))
    try:
        data = resp.json()
    except Exception:
        raise Exception("Google {0} returned non-JSON".format(client))
    translated = _parse_google_single_json(data)
    if not translated:
        raise Exception("Google {0} empty translation".format(client))
    return translated


def _translate_via_mymemory(text):
    resp = requests.get(
        "https://api.mymemory.translated.net/get",
        params={
            "q": text[:500],
            "langpair": "es|en",
        },
        timeout=8,
    )
    if resp.status_code != 200:
        raise Exception("MyMemory HTTP {0}".format(resp.status_code))
    data = resp.json() or {}
    translated = ((data.get("responseData") or {}).get("translatedText") or "").strip()
    if not translated:
        raise Exception("MyMemory empty translation")
    if translated.lower().startswith("my memory warning"):
        raise Exception(translated)
    return translated


def _google_translate_en_once(text, retries=2):
    """
    Translate one string. Tries working Google clients first, then MyMemory.
    Never uses deep_translator's HTML scrape (that is what has been hanging).
    """
    global _GOOGLE_TRANSLATE_LAST
    text = (text or "").strip()
    if not text:
        return text

    backends = [
        ("google-at", lambda: _translate_via_google_json(text, "at")),
        ("google-dict", lambda: _translate_via_google_json(text, "dict-chrome-ex")),
        ("mymemory", lambda: _translate_via_mymemory(text)),
    ]

    last_err = None
    for attempt in range(retries):
        for name, fn in backends:
            with _GOOGLE_TRANSLATE_LOCK:
                now = time.monotonic()
                wait = _GOOGLE_TRANSLATE_MIN_INTERVAL - (now - _GOOGLE_TRANSLATE_LAST)
                if wait > 0:
                    time.sleep(wait)
                try:
                    translated = fn()
                    _GOOGLE_TRANSLATE_LAST = time.monotonic()
                    translated = _apply_translate_custom_dict(translated)
                    if translated and translated.strip():
                        return translated
                except Exception as e:
                    _GOOGLE_TRANSLATE_LAST = time.monotonic()
                    last_err = e
                    continue
        time.sleep(1.0 * (attempt + 1))

    if last_err:
        raise last_err
    raise Exception("All translation backends failed")


def _google_translate_en_list(texts):
    cleaned = [(t if isinstance(t, str) else str(t or "")) for t in (texts or [])]
    out = list(cleaned)
    pending = []

    for idx, raw in enumerate(cleaned):
        text = raw.strip()
        if text:
            pending.append((idx, text))

    chunks = []
    current_idxs = []
    current_parts = []
    current_len = 0
    sep_len = len(_GOOGLE_ROW_SEP)

    for idx, text in pending:
        extra = len(text) if not current_parts else (sep_len + len(text))
        if current_parts and (current_len + extra) > _GOOGLE_MAX_CHARS:
            chunks.append((current_idxs, current_parts))
            current_idxs = []
            current_parts = []
            current_len = 0
            extra = len(text)
        current_idxs.append(idx)
        current_parts.append(text)
        current_len += extra

    if current_parts:
        chunks.append((current_idxs, current_parts))

    errors = []
    for idxs, parts in chunks:
        blob = _GOOGLE_ROW_SEP.join(parts)
        used_split = False
        try:
            translated_blob = _google_translate_en_once(blob)
            split = [s.strip() for s in translated_blob.split("###RFQROW###") if s.strip() or len(parts) == 1]
            if len(split) == len(parts):
                for i, translated in zip(idxs, split):
                    out[i] = _apply_translate_custom_dict(translated.strip())
                used_split = True
        except Exception as e:
            errors.append(str(e))

        if used_split:
            continue

        for i, original in zip(idxs, parts):
            try:
                out[i] = _google_translate_en_once(original)
            except Exception as e:
                frappe.log_error("RFQ Translator Error", str(e))
                errors.append(str(e))
                out[i] = original

    still_spanish = []
    for idx, original in pending:
        if _looks_spanish(out[idx]) and (out[idx].strip() == original.strip() or _looks_spanish(out[idx])):
            # If output still clearly Spanish, the backend did not translate
            if out[idx].strip() == original.strip() or _looks_spanish(out[idx]):
                if out[idx].strip() == original.strip():
                    still_spanish.append(idx + 1)

    if still_spanish and all(out[i] == cleaned[i] for i, _ in pending):
        detail = errors[-1] if errors else "backends returned the original text"
        raise Exception(
            "Translation backends did not change the Spanish text. Last error: {0}".format(detail)
        )

    return out


@frappe.whitelist()
def translate_with_local(text):
    if not text or not str(text).strip():
        return {"status": "success", "data": text}
    try:
        translated = _google_translate_en_once(text)
        return {"status": "success", "data": translated}
    except Exception as e:
        frappe.log_error("RFQ Translator Error", str(e))
        return {
            "status": "error",
            "error": "Translator failed: {0}".format(str(e)),
        }


@frappe.whitelist()
def translate_with_local_batch(texts=None):
    try:
        if isinstance(texts, str):
            try:
                texts = json.loads(texts)
            except Exception:
                texts = [texts]
        if not isinstance(texts, (list, tuple)):
            texts = [texts] if texts else []
        translated = _google_translate_en_list(list(texts))
        return {"status": "success", "data": translated}
    except Exception as e:
        frappe.log_error("RFQ Translator Batch Error", str(e))
        return {
            "status": "error",
            "error": "Translator failed: {0}".format(str(e)),
        }




@frappe.whitelist()
def update_supplier_contact(contact_name, new_name, new_email):
    try:
        doc = frappe.get_doc("Contact", contact_name)
        doc.first_name = new_name
        doc.last_name = "" 
        if new_email:
            found = False
            for row in doc.email_ids:
                if row.is_primary:
                    row.email_id = new_email
                    found = True
                    break
            if not found:
                doc.append("email_ids", {"email_id": new_email, "is_primary": 1})
        doc.save(ignore_permissions=True)
        return {"status": "success"}
    except Exception as e:
        frappe.log_error("Update Contact Error", str(e))
        return {"status": "error", "error": str(e)}

@frappe.whitelist()
def delete_supplier_contact(contact_name):
    try:
        frappe.delete_doc("Contact", contact_name, ignore_permissions=True)
        return {"status": "success"}
    except Exception as e:
        frappe.log_error("Delete Contact Error", str(e))
        return {"status": "error", "error": str(e)}



def _is_numeric_sales_price(value):
    """True only when sales price is a number (optional sign, commas, decimal)."""
    text = str(value or "").strip().replace(",", "")
    if not text:
        return False
    if any(ch.isalpha() for ch in text):
        return False
    try:
        float(text)
        return True
    except (TypeError, ValueError):
        return False


def _next_supplier_quote_name():
    """Next numeric name for Supplier Quote. Avoids field:id collisions
    and stays within a short/numeric `name` column."""
    max_id_result = frappe.db.sql("""
        SELECT MAX(CAST(name AS UNSIGNED)) as max_id
        FROM `tabSupplier Quote`
    """, as_dict=True)
    current_max = 0
    if max_id_result and max_id_result[0].max_id:
        current_max = int(max_id_result[0].max_id)
    return str(current_max + 1)


def _insert_supplier_quote(row_id, field_values):
    """
    Insert a Supplier Quote linked to a Request And Quote row.

    `id` stays as the RAQ link (many quotes per request).
    `name` is a unique autoincrement value so the primary key does not
    collide when a second supplier is saved on the same request.
    """
    doc = frappe.get_doc({
        "doctype": "Supplier Quote",
        "id": row_id,
        **field_values
    })
    doc.name = _next_supplier_quote_name()
    doc.flags.name_set = True
    doc.insert(ignore_permissions=True)
    return doc


def _user_can_delete_customer_orders():
    """Sales T3, Procurement T3, Administrator, or System Manager may delete Customer Order rows."""
    user = frappe.session.user
    roles = frappe.get_roles(user)
    return (
        user == "Administrator"
        or "System Manager" in roles
        or "Sales T3" in roles
        or "Procurement T3" in roles
    )


def _sync_customer_orders(row_id, customer_order_records):
    """
    Treat the incoming list as the full desired state for this Request And Quote:
      - insert rows flagged is_new (or missing DOC_NAME)
      - update existing rows by DOC_NAME
      - delete DB rows that are no longer in the list
    Deletion is restricted to Sales T3 / Procurement T3 / Admin.
    If customer_order_records is None, do nothing (caller omitted the key).
    """
    if customer_order_records is None:
        return

    incoming_names = set()

    for ord in customer_order_records:
        is_new = ord.get('is_new', False)
        doc_name = ord.get('DOC_NAME')

        ord_date = ord.get('DATE') or None
        country = ord.get('COUNTRY') or ""
        order_number = ord.get('ORDER_NUMBER') or ""
        order_due_date = ord.get('ORDER_DUE_DATE') or None
        modification = ord.get('MODIFICATION') or None
        order_price_ea = ord.get('ORDER_PRICE_EA') or ""
        note_order = ord.get('NOTE_ORDER') or ""
        qty = ord.get('QTY') or ""

        if is_new or not doc_name:
            new_ord = frappe.get_doc({
                "doctype": "Customer Order",
                "id": row_id,
                "date": ord_date,
                "country": country,
                "order_number": order_number,
                "order_due_date": order_due_date,
                "modification": modification,
                "order_price_ea": order_price_ea,
                "note_order": note_order,
                "qty": qty
            })
            new_ord.insert(ignore_permissions=True)
            if new_ord.name:
                incoming_names.add(str(new_ord.name))
        else:
            incoming_names.add(str(doc_name))
            update_ord_sql = """
                UPDATE `tabCustomer Order`
                SET date=%s, country=%s, order_number=%s, order_due_date=%s, modification=%s, order_price_ea=%s, note_order=%s, qty=%s
                WHERE name=%s
            """
            frappe.db.sql(
                update_ord_sql,
                (ord_date, country, order_number, order_due_date, modification, order_price_ea, note_order, qty, doc_name)
            )

    if not _user_can_delete_customer_orders():
        return

    existing = frappe.db.sql(
        "SELECT name FROM `tabCustomer Order` WHERE id = %s",
        (row_id,),
        as_dict=True
    )
    for row in existing:
        existing_name = str(row.name)
        if existing_name not in incoming_names:
            frappe.delete_doc("Customer Order", existing_name, ignore_permissions=True, force=1)


def _current_user_st_code(user=None):
    """3-digit User.st of the real logged-in user. Pass user explicitly if this request later calls set_user('Administrator')."""
    from my_custom_app.process_request_test import get_logged_in_user_st_code
    return get_logged_in_user_st_code(user) or ""


def _apply_submitted_by(doc, new_status, user=None):
    """
    Stamp custom_requested_by when custom_sales_status is changing to S-SUBMITTED.
    Previous status can be blank or any other value. Already S-SUBMITTED is left untouched.
    Returns new_status unchanged so the existing assignment behavior stays the same.
    """
    previous = str(doc.custom_sales_status or "").strip()
    incoming = "" if new_status is None else str(new_status).strip()
    if incoming == "S-SUBMITTED" and previous != "S-SUBMITTED":
        doc.custom_requested_by = _current_user_st_code(user)
    return new_status


def _modified_stamp(val):
    if val is None:
        return ""
    return str(val).replace("T", " ").split(".")[0].strip()


def _reject_stale_raq_write(doc, data):
    # Stale-row guard is OFF.
    # Set STALE_RAQ_GUARD = True to turn it back on later.
    STALE_RAQ_GUARD = False
    if not STALE_RAQ_GUARD:
        return None

    incoming = data.get("MODIFIED")
    if incoming in (None, ""):
        return None
    if _modified_stamp(incoming) == _modified_stamp(doc.modified):
        return None

    grace_seconds = 90
    age_seconds = None
    try:
        from frappe.utils import now_datetime, get_datetime
        age_seconds = (now_datetime() - get_datetime(doc.modified)).total_seconds()
    except Exception:
        age_seconds = None

    same_user = str(doc.modified_by or "") == str(frappe.session.user or "")
    if same_user and age_seconds is not None and 0 <= age_seconds <= grace_seconds:
        return None

    return {
        "status": "error",
        "stale": True,
        "error": (
            "Record {0} was changed after you opened it. "
            "Refresh the row and save again."
        ).format(doc.name),
        "server_modified": str(doc.modified),
    }


def _clear_raq_list_cache():
    cache = frappe.cache()
    for prefix in ("proc_get_quotes_v6:", "proc_get_sales_rfq_v6:"):
        try:
            cache.delete_keys("{0}*".format(prefix))
        except Exception:
            pass


PROC_PAYLOAD_FIELDS = (
    ("REF", "ref"),
    ("RFQDATE", "quotation_rfqdate"),
    ("REP", "rep"),
    ("ITEM", "quotation_item"),
    ("QTY", "quotation_qty"),
    ("UNIT", "quotation_unit"),
    ("BRAND", "quotation_brand"),
    ("PART_NUMBER", "quotation_part_number"),
    ("DESCRIPTION", "quotation_description"),
    ("CO", "quotation_co"),
    ("APROX_WEIGHT", "quotation_aprox_weight"),
    ("INCOTERM", "quotation_incoterm"),
    ("SALES_PRICE", "quotation_sales_price"),
    ("DELIVERY", "quotation_delivery"),
    ("NOTE", "quotation_note"),
    ("DUE_DATE", "due_date"),
    ("DUE_TIME", "due_time"),
    ("SAP", "sap"),
    ("TEST", "test"),
)


SALES_PAYLOAD_FIELDS = (
    ("DATE", "date"),
    ("COUNTRY", "country"),
    ("CUSTOMER", "customer"),
    ("DIV", "div"),
    ("CONTACT", "contact"),
    ("EMAIL_CUSTOMER", "email_customr"),
    ("CUSTOMER_REF", "customer_ref"),
    ("DUE_DATE", "due_date"),
    ("DUE_TIME", "due_time"),
    ("SAP", "sap"),
    ("RP", "rp"),
    ("ST", "st"),
    ("SALE_PRICE", "sale_price"),
    ("REF", "ref"),
    ("ITEM", "item"),
    ("QTY", "qty"),
    ("UNIT", "unit"),
    ("BRAND", "brand"),
    ("PART_NUMBER", "part_number"),
    ("DESCRIPTION", "description"),
    ("INCOTERM", "incoterm"),
    ("NOTE", "note"),
    ("FEEDBACK", "feedback"),
    ("CUSTOMER_BID_NUMBER", "custom_customer_bid_number"),
)


def _apply_mapped_payload(doc, data, field_map):
    for src, dest in field_map:
        if src in data:
            setattr(doc, dest, data.get(src))


@frappe.whitelist()
def update_quote_record(payload):
    try:
        data = json.loads(payload)
        row_id = data.get('ID')
        if not row_id:
            return {"status": "error", "error": "Row ID is required."}

        # ------------------------------------------------------------------
        # Use Document API so Frappe creates a proper Version record
        # (this is what the Activity History button reads)
        # ------------------------------------------------------------------
        doc = frappe.get_doc("Request And Quote", row_id)

        stale = _reject_stale_raq_write(doc, data)
        if stale:
            return stale

        _apply_mapped_payload(doc, data, PROC_PAYLOAD_FIELDS)
        if "CUSTOM_SALES_STATUS" in data:
            doc.custom_sales_status = _apply_submitted_by(doc, data.get("CUSTOM_SALES_STATUS"))
        if "PROCUREMENT_STATUS" in data:
            doc.procurement_status = data.get("PROCUREMENT_STATUS")

        sales_price_value = str(doc.quotation_sales_price or "").strip()
        if _is_numeric_sales_price(sales_price_value):
            doc.procurement_status = "Q-QUOTED"

        doc.flags.ignore_permissions = True
        doc.save()
        _clear_raq_list_cache()
        
        # ------------------------------------------------------------------
        # Supplier Quote child rows (left as SQL for now – can be converted later)
        # ------------------------------------------------------------------
        supplier_records = data.get('supplier_records', [])
        for sup in supplier_records:
            is_new = sup.get('is_new', False)
            doc_name = sup.get('DOC_NAME')

            sup_name = sup.get('SUPPLIER_NAME') or ""
            sup_id = sup.get('SUPPLIER_ID') or ""
            contact = sup.get('CONTACT') or ""
            email_contact = sup.get('EMAIL_CONTACT') or ""
            cost_ea = sup.get('COST_EA') or ""
            logistics_fee = sup.get('LOGISTICS_FEE') or ""
            delivery = sup.get('DELIVERY') or ""
            incoterms = sup.get('INCOTERMS') or ""
            additional_fees = sup.get('ADDITIONAL_FEES') or ""
            noted = sup.get('NOTED') or ""

            if is_new:
                _insert_supplier_quote(row_id, {
                    "supplier_name": sup_name,
                    "supplier": sup_id,
                    "contact": contact,
                    "email_contact": email_contact,
                    "cost_ea": cost_ea,
                    "logistics_fee": logistics_fee,
                    "delivery": delivery,
                    "incoterms": incoterms,
                    "additional_fees": additional_fees,
                    "noted": noted
                })
            else:
                update_sup_sql = """
                    UPDATE `tabSupplier Quote`
                    SET supplier_name=%s, supplier=%s, contact=%s, email_contact=%s, cost_ea=%s, logistics_fee=%s, delivery=%s, incoterms=%s, additional_fees=%s, noted=%s
                    WHERE name=%s
                """
                params = (sup_name, sup_id, contact, email_contact, cost_ea, logistics_fee, delivery, incoterms, additional_fees, noted, doc_name)
                frappe.db.sql(update_sup_sql, params)

        if "customer_order_records" in data:
            _sync_customer_orders(row_id, data.get("customer_order_records") or [])

        frappe.db.commit()
        return {
            "status": "success",
            "modified": str(doc.modified),
            "modified_by": str(doc.modified_by or frappe.session.user or "")
        }

    except Exception as e:
        frappe.log_error("Proc Batching Update Error", str(e))
        return {"status": "error", "error": str(e)}


@frappe.whitelist()
def bulk_update_quote_records(payload):
    try:
        records = json.loads(payload)
        if not records:
            return {"status": "error", "error": "No records provided."}

        for data in records:
            row_id = data.get('ID')
            if not row_id:
                continue

            # ------------------------------------------------------------------
            # Use Document API so Frappe creates a proper Version record
            # ------------------------------------------------------------------
            doc = frappe.get_doc("Request And Quote", row_id)

            stale = _reject_stale_raq_write(doc, data)
            if stale:
                return stale

            _apply_mapped_payload(doc, data, PROC_PAYLOAD_FIELDS)
            if "CUSTOM_SALES_STATUS" in data:
                doc.custom_sales_status = _apply_submitted_by(doc, data.get("CUSTOM_SALES_STATUS"))
            if "PROCUREMENT_STATUS" in data:
                doc.procurement_status = data.get("PROCUREMENT_STATUS")

            sales_price_value = str(doc.quotation_sales_price or "").strip()
            if _is_numeric_sales_price(sales_price_value):
                doc.procurement_status = "Q-QUOTED"

            doc.flags.ignore_permissions = True
            doc.save()
            _clear_raq_list_cache()

            # ------------------------------------------------------------------
            # Supplier Quote child rows (kept as SQL for now)
            # ------------------------------------------------------------------
            supplier_records = data.get('supplier_records', [])
            for sup in supplier_records:
                is_new = sup.get('is_new', False)
                doc_name = sup.get('DOC_NAME')

                sup_name = sup.get('SUPPLIER_NAME') or ""
                sup_id = sup.get('SUPPLIER_ID') or ""
                contact = sup.get('CONTACT') or ""
                email_contact = sup.get('EMAIL_CONTACT') or ""
                cost_ea = sup.get('COST_EA') or ""
                logistics_fee = sup.get('LOGISTICS_FEE') or ""
                delivery = sup.get('DELIVERY') or ""
                incoterms = sup.get('INCOTERMS') or ""
                additional_fees = sup.get('ADDITIONAL_FEES') or ""
                noted = sup.get('NOTED') or ""

                if is_new:
                    _insert_supplier_quote(row_id, {
                        "supplier_name": sup_name,
                        "supplier": sup_id,
                        "contact": contact,
                        "email_contact": email_contact,
                        "cost_ea": cost_ea,
                        "logistics_fee": logistics_fee,
                        "delivery": delivery,
                        "incoterms": incoterms,
                        "additional_fees": additional_fees,
                        "noted": noted
                    })
                else:
                    update_sup_sql = """
                        UPDATE `tabSupplier Quote`
                        SET supplier_name=%s, supplier=%s, contact=%s, email_contact=%s, cost_ea=%s, logistics_fee=%s, delivery=%s, incoterms=%s, additional_fees=%s, noted=%s
                        WHERE name=%s
                    """
                    params = (sup_name, sup_id, contact, email_contact, cost_ea, logistics_fee, delivery, incoterms, additional_fees, noted, doc_name)
                    frappe.db.sql(update_sup_sql, params)

            if "customer_order_records" in data:
                _sync_customer_orders(row_id, data.get("customer_order_records") or [])

        frappe.db.commit()
        return {"status": "success", "message": f"Updated {len(records)} records."}

    except Exception as e:
        frappe.log_error("Proc Batching Bulk Update Error", str(e))
        return {"status": "error", "error": str(e)}


@frappe.whitelist()
def get_quote_suppliers(item_ids):
    try:
        ids = json.loads(item_ids)
        if not ids:
            return {"status": "success", "data": {}}

        format_strings = ','.join(['%s'] * len(ids))
        sql = f"""
            SELECT name as DOC_NAME, id as ID, supplier_name as SUPPLIER_NAME, supplier as SUPPLIER_ID, contact as CONTACT, email_contact as EMAIL_CONTACT, cost_ea as COST_EA, logistics_fee as LOGISTICS_FEE, delivery as DELIVERY, incoterms as INCOTERMS, additional_fees as ADDITIONAL_FEES, noted as NOTED, attachments as ATTACHMENTS
            FROM `tabSupplier Quote`
            WHERE id IN ({format_strings})
        """
        rows = frappe.db.sql(sql, tuple(ids), as_dict=True)

        result = {}
        for row in rows:
            # Enforce string type to guarantee flawless matching with Vue's string keys
            item_id = str(row['ID']) 
            if item_id not in result:
                result[item_id] = []
            result[item_id].append(row)

        return {"status": "success", "data": result}
    except Exception as e:
        frappe.log_error("Proc Batching Get Suppliers Error", str(e))
        return {"status": "error", "error": str(e)}


@frappe.whitelist()
def save_supplier_quote(payload):
    """Create or update a single Supplier Quote row from the GLNET tab."""
    try:
        data = json.loads(payload) if isinstance(payload, str) else (payload or {})
        row_id = data.get("ID")
        if not row_id:
            return {"status": "error", "error": "Row ID is required."}

        field_values = {
            "supplier_name": data.get("SUPPLIER_NAME") or "",
            "supplier": data.get("SUPPLIER_ID") or "",
            "contact": data.get("CONTACT") or "",
            "email_contact": data.get("EMAIL_CONTACT") or "",
            "cost_ea": data.get("COST_EA") or "",
            "logistics_fee": data.get("LOGISTICS_FEE") or "",
            "delivery": data.get("DELIVERY") or "",
            "incoterms": data.get("INCOTERMS") or "",
            "additional_fees": data.get("ADDITIONAL_FEES") or "",
            "noted": data.get("NOTED") or "",
        }

        doc_name = data.get("DOC_NAME") or ""
        is_new = data.get("is_new", False) or not doc_name

        if is_new:
            doc = _insert_supplier_quote(row_id, field_values)
            frappe.db.commit()
            return {"status": "success", "doc_name": doc.name}

        if not frappe.db.exists("Supplier Quote", doc_name):
            return {"status": "error", "error": "Supplier Quote was not found."}

        update_sup_sql = """
            UPDATE `tabSupplier Quote`
            SET supplier_name=%s, supplier=%s, contact=%s, email_contact=%s,
                cost_ea=%s, logistics_fee=%s, delivery=%s, incoterms=%s,
                additional_fees=%s, noted=%s
            WHERE name=%s
        """
        frappe.db.sql(
            update_sup_sql,
            (
                field_values["supplier_name"],
                field_values["supplier"],
                field_values["contact"],
                field_values["email_contact"],
                field_values["cost_ea"],
                field_values["logistics_fee"],
                field_values["delivery"],
                field_values["incoterms"],
                field_values["additional_fees"],
                field_values["noted"],
                doc_name,
            ),
        )
        frappe.db.commit()
        return {"status": "success", "doc_name": doc_name}

    except Exception as e:
        frappe.log_error("Save Supplier Quote Error", str(e))
        return {"status": "error", "error": str(e)}


@frappe.whitelist()
def delete_supplier_quote(doc_name):
    """Permanently delete one Supplier Quote. Procurement T2 / T3 / Admin only."""
    try:
        doc_name = (doc_name or "").strip()
        if not doc_name:
            return {"status": "error", "error": "Supplier Quote name is required."}

        user = frappe.session.user
        roles = frappe.get_roles(user)
        can_delete = (
            user == "Administrator"
            or "System Manager" in roles
            or "Procurement T3" in roles
            or "Procurement T2" in roles
        )
        if not can_delete:
            return {"status": "error", "error": "Only Procurement T2 / T3 can delete supplier quotes."}

        if not frappe.db.exists("Supplier Quote", doc_name):
            return {"status": "success", "message": "Supplier Quote already removed."}

        frappe.delete_doc("Supplier Quote", doc_name, ignore_permissions=True, force=1)
        frappe.db.commit()
        return {"status": "success"}

    except Exception as e:
        frappe.log_error("Delete Supplier Quote Error", str(e))
        return {"status": "error", "error": str(e)}

@frappe.whitelist()
def search_sap(sap_value):
    if not sap_value:
        return {"status": "success", "data": []}

    try:
        rfq_records = frappe.db.sql("""
            SELECT name as ID, customer_ref as CUSTOMER_REF, customer as CUSTOMER, part_number as PART_NUMBER, brand as BRAND, date as DATE, rp as RP
            FROM `tabRequest And Quote`
            WHERE sap = %s
        """, (sap_value,), as_dict=True)

        result = []
        for rfq in rfq_records:
            id_value = rfq['ID']

            quote_records = frappe.db.sql("""
                SELECT ref as REF, quotation_sales_price as SALES_PRICE, quotation_part_number as PART_NUMBER, quotation_brand as BRAND, quotation_rfqdate as RFQDATE, rep as REP
                FROM `tabRequest And Quote`
                WHERE name = %s
            """, (id_value,), as_dict=True)

            supplier_records = frappe.db.sql("""
                SELECT name as DOC_NAME, supplier_name as SUPPLIER_NAME, supplier as SUPPLIER_ID, contact as CONTACT, email_contact as EMAIL_CONTACT, cost_ea as COST_EA, logistics_fee as LOGISTICS_FEE, delivery as DELIVERY, incoterms as INCOTERMS, additional_fees as ADDITIONAL_FEES, noted as NOTED
                FROM `tabSupplier Quote`
                WHERE id = %s
            """, (id_value,), as_dict=True)

            order_records = frappe.db.sql("""
                SELECT order_number as `ORDER NUMBER`, date as DATE, order_price_ea as `ORDER PRICE_EA`
                FROM `tabCustomer Order`
                WHERE id = %s
            """, (id_value,), as_dict=True)

            result.append({
                'id': rfq['ID'],
                'customer_ref': rfq['CUSTOMER_REF'],
                'customer': rfq['CUSTOMER'],
                'part_number': rfq['PART_NUMBER'],
                'brand': rfq['BRAND'],
                'date': rfq['DATE'],
                'rp': rfq['RP'],
                'quotes': quote_records,
                'suppliers': supplier_records,
                'orders': order_records
            })

        return {"status": "success", "data": result}

    except Exception as e:
        frappe.log_error("Proc Batching SAP Search Error", str(e))
        return {"status": "error", "error": str(e)}
        

@frappe.whitelist()
def delete_internal_attachment(quote_id, file_url):
    try:
        parents = frappe.get_all("GLNet Record Attachments", filters={"quote_id": quote_id}, limit=1)
        if not parents:
            return {"status": "error", "error": "Parent record not found."}

        doc = frappe.get_doc("GLNet Record Attachments", parents[0].name)
        original_len = len(doc.attachment_list)
        doc.attachment_list = [row for row in doc.attachment_list if row.attach != file_url]

        if len(doc.attachment_list) < original_len:
            doc.save(ignore_permissions=True)
            return {"status": "success"}
        else:
            return {"status": "error", "error": "File not found in record."}

    except Exception as e:
        frappe.log_error("Delete Internal Attachment Error", str(e))
        return {"status": "error", "error": str(e)}

@frappe.whitelist()
def get_proc_alerts():
    try:
        sap_alerts = frappe.get_all("Sap_Alert", fields=["name", "sap", "customer", "alert_type", "star"])
        pn_alerts = frappe.get_all("PN_Alert", fields=["name", "part_number", "star"])
        return {"status": "success", "sap": sap_alerts, "pn": pn_alerts}
    except Exception as e:
        return {"status": "error", "error": str(e)}

@frappe.whitelist()
def save_proc_alert(doctype, payload):
    try:
        data = json.loads(payload)
        if data.get('name'):
            doc = frappe.get_doc(doctype, data.get('name'))
            doc.update(data)
        else:
            doc = frappe.new_doc(doctype)
            doc.update(data)
        doc.save(ignore_permissions=True)
        return {"status": "success"}
    except Exception as e:
        return {"status": "error", "error": str(e)}

@frappe.whitelist()
def delete_proc_alert(doctype, name):
    try:
        frappe.delete_doc(doctype, name, ignore_permissions=True)
        return {"status": "success"}
    except Exception as e:
        return {"status": "error", "error": str(e)}


@frappe.whitelist()
def search_graph_suppliers(query=""):
    try:
        q_str = f"%{query}%"

        sql = """
            SELECT DISTINCT s.name, s.supplier_primary_contact as primary_contact_name
            FROM `tabSupplier` s
            LEFT JOIN `tabDynamic Link` dl ON dl.link_name = s.name AND dl.link_doctype = 'Supplier' AND dl.parenttype = 'Contact'
            LEFT JOIN `tabContact` c ON c.name = dl.parent
            LEFT JOIN `tabContact Email` ce ON ce.parent = c.name
            WHERE s.disabled = 0
            AND (s.name LIKE %s OR ce.email_id LIKE %s)
            LIMIT 50
        """
        suppliers = frappe.db.sql(sql, (q_str, q_str), as_dict=True)

        for sup in suppliers:
            if sup.primary_contact_name:
                email = frappe.db.get_value("Contact Email", {"parent": sup.primary_contact_name, "is_primary": 1}, "email_id")
                sup['primary_email'] = email

            addr_link = frappe.db.get_value("Dynamic Link", {"link_name": sup.name, "link_doctype": "Supplier", "parenttype": "Address"}, "parent")
            if addr_link:
                addr_doc = frappe.get_doc("Address", addr_link)
                sup['address'] = f"{addr_doc.address_line1 or ''}, {addr_doc.city or ''}, {addr_doc.state or ''}".strip(', ')

        return suppliers
    except Exception as e:
        return []

@frappe.whitelist()
def get_supplier_brands(supplier_name):
    try:
        supplier_meta = frappe.get_meta("Supplier")
        brand_field = supplier_meta.get_field("custom_authorized_brands")
        if not brand_field or not brand_field.options: return []

        child_table = brand_field.options
        link_fieldname = next((df.fieldname for df in frappe.get_meta(child_table).fields if df.fieldtype == "Link"), "brand")

        brands = frappe.db.sql(f"SELECT `{link_fieldname}` FROM `tab{child_table}` WHERE parent = %s", supplier_name)
        return [b[0] for b in brands if b[0]]
    except Exception:
        return []

@frappe.whitelist()
def get_brand_suppliers(brand_name):
    try:
        supplier_meta = frappe.get_meta("Supplier")
        brand_field = supplier_meta.get_field("custom_authorized_brands")
        if not brand_field or not brand_field.options: return []

        child_table = brand_field.options
        link_fieldname = next((df.fieldname for df in frappe.get_meta(child_table).fields if df.fieldtype == "Link"), "brand")

        parents = frappe.db.sql(f"SELECT parent FROM `tab{child_table}` WHERE `{link_fieldname}` = %s", brand_name)
        parent_names = [p[0] for p in parents]

        if not parent_names: return []

        format_strings = ','.join(['%s'] * len(parent_names))
        sql = f"""
            SELECT s.name, s.supplier_primary_contact as primary_contact_name
            FROM `tabSupplier` s
            WHERE s.name IN ({format_strings})
        """
        suppliers = frappe.db.sql(sql, tuple(parent_names), as_dict=True)

        for sup in suppliers:
            if sup.primary_contact_name:
                sup['primary_email'] = frappe.db.get_value("Contact Email", {"parent": sup.primary_contact_name, "is_primary": 1}, "email_id")
            addr_link = frappe.db.get_value("Dynamic Link", {"link_name": sup.name, "link_doctype": "Supplier", "parenttype": "Address"}, "parent")
            if addr_link:
                addr_doc = frappe.get_doc("Address", addr_link)
                sup['address'] = f"{addr_doc.address_line1 or ''}, {addr_doc.city or ''}".strip(', ')

        return suppliers
    except Exception:
        return []

@frappe.whitelist()
def link_supplier_brand(supplier_name, brand_name):
    try:
        sup = frappe.get_doc("Supplier", supplier_name)

        brand_field = frappe.get_meta("Supplier").get_field("custom_authorized_brands")
        child_doctype = brand_field.options
        link_fieldname = next((df.fieldname for df in frappe.get_meta(child_doctype).fields if df.fieldtype == "Link"), "brand")

        exists = any(row.get(link_fieldname) == brand_name for row in sup.get("custom_authorized_brands", []))
        if not exists:
            sup.append("custom_authorized_brands", {link_fieldname: brand_name})
            sup.save(ignore_permissions=True)

        return {"status": "success"}
    except Exception as e:
        return {"status": "error", "error": str(e)}

@frappe.whitelist()
def unlink_supplier_brand(supplier_name, brand_name):
    try:
        sup = frappe.get_doc("Supplier", supplier_name)

        brand_field = frappe.get_meta("Supplier").get_field("custom_authorized_brands")
        link_fieldname = next((df.fieldname for df in frappe.get_meta(brand_field.options).fields if df.fieldtype == "Link"), "brand")

        original_len = len(sup.get("custom_authorized_brands", []))
        sup.custom_authorized_brands = [row for row in sup.get("custom_authorized_brands", []) if row.get(link_fieldname) != brand_name]

        if len(sup.custom_authorized_brands) < original_len:
            sup.save(ignore_permissions=True)

        return {"status": "success"}
    except Exception as e:
        return {"status": "error", "error": str(e)}


@frappe.whitelist()
def get_dashboard_data(user_rep=None):
    try:
        rep_filter = ""
        params = []
        if user_rep and str(user_rep).strip():
            rep_filter = " AND rq.rep = %s "
            params.append(str(user_rep).strip())

        result = {}

        # 1. TODAY METRICS
        today_sql = f"""
            SELECT
                COUNT(CASE WHEN (rq.procurement_status IS NULL OR rq.procurement_status = '') THEN 1 END) as day_empty,
                COUNT(CASE WHEN rq.procurement_status LIKE '%%W%%' AND rq.quotation_note LIKE '%%SO:%%' AND rq.quotation_note NOT LIKE '%%SO: ||%%' THEN 1 END) as day_previous_so,
                COUNT(CASE WHEN rq.procurement_status LIKE '%%W%%' AND rq.ref LIKE '%%Z%%' THEN 1 END) as day_z_ref,
                COUNT(CASE WHEN rq.procurement_status LIKE '%%W%%' AND rq.ref LIKE '%%VOC%%' THEN 1 END) as day_voc,
                COUNT(CASE WHEN rq.procurement_status LIKE '%%W%%' AND rq.ref LIKE '%%V%%' AND rq.ref NOT LIKE '%%VOC%%' THEN 1 END) as day_validation,
                COUNT(CASE WHEN rq.procurement_status LIKE '%%W%%' AND rq.ref LIKE '%%P%%' THEN 1 END) as day_p,
                COUNT(CASE WHEN rq.procurement_status LIKE '%%W%%' AND rq.ref LIKE '%%B%%' THEN 1 END) as day_b,
                COUNT(CASE WHEN rq.procurement_status LIKE '%%W%%' THEN 1 END) as day_w_total,
                COUNT(CASE WHEN (rq.procurement_status IS NULL OR rq.procurement_status = '' OR rq.procurement_status NOT LIKE '%%W%%') THEN 1 END) as day_24h_no_activation
            FROM `tabRequest And Quote` rq
            WHERE DATE(rq.quotation_rfqdate) = CURDATE() {rep_filter}
        """
        today_row = frappe.db.sql(today_sql, tuple(params), as_dict=True)
        if today_row:
            result.update(today_row[0])

        # 2. TOMORROW + FUTURE
        tomorrow_future_sql = f"""
            SELECT
                COUNT(CASE WHEN DATE(rq.quotation_rfqdate) = CURDATE() + INTERVAL 1 DAY AND (rq.procurement_status IS NULL OR rq.procurement_status = '') THEN 1 END) as tomorrow_empties,
                COUNT(CASE WHEN DATE(rq.quotation_rfqdate) = CURDATE() + INTERVAL 1 DAY AND rq.procurement_status LIKE '%%W%%' THEN 1 END) as tomorrow_ws,
                COUNT(CASE WHEN DATE(rq.quotation_rfqdate) >= CURDATE() + INTERVAL 2 DAY AND (rq.procurement_status IS NULL OR rq.procurement_status = '') THEN 1 END) as future_empties,
                COUNT(CASE WHEN DATE(rq.quotation_rfqdate) >= CURDATE() + INTERVAL 2 DAY AND rq.procurement_status LIKE '%%W%%' THEN 1 END) as future_ws
            FROM `tabRequest And Quote` rq
            WHERE 1=1 {rep_filter}
        """
        tf_row = frappe.db.sql(tomorrow_future_sql, tuple(params), as_dict=True)
        if tf_row:
            result.update(tf_row[0])

        result['open_convenios'] = 0

        # 3. EXCEPTIONS & NQ METRICS
        # NOTE: NQ still lives in quotation_sales_price – left unchanged
        exceptions_sql = f"""
            SELECT
                COUNT(CASE WHEN rq.rep NOT IN ('100','101','102','113','114','115','117') AND CAST(rq.name AS UNSIGNED) > 4080971 THEN 1 END) as bad_assignment,
                COUNT(CASE WHEN CAST(rq.name AS UNSIGNED) > 4080971 AND (rq.quotation_rfqdate = '0000-00-00' OR rq.quotation_rfqdate IS NULL OR
                    ((rq.procurement_status IS NULL OR rq.procurement_status = '') AND DATE(rq.quotation_rfqdate) <= CURDATE() - INTERVAL 1 DAY)) THEN 1 END) as bad_date,
                COUNT(CASE WHEN DATE(rq.quotation_rfqdate) = CURDATE() AND rq.procurement_status LIKE '%%W%%' AND DATEDIFF(CURDATE(), DATE(rq.date)) >= 4 THEN 1 END) as uploaded_4plus_days,
                COUNT(CASE WHEN rq.quotation_sales_price LIKE '%%NQ%%' AND rq.quotation_note LIKE '%%SO:%%' AND rq.quotation_note NOT LIKE '%%SO: ||%%' AND DATE(rq.quotation_rfqdate) >= CURDATE() THEN 1 END) as nq_previous_so,
                COUNT(CASE WHEN rq.quotation_sales_price LIKE '%%NQ%%' AND rq.ref LIKE '%%Z%%' AND DATE(rq.quotation_rfqdate) >= CURDATE() THEN 1 END) as nq_z_ref,
                COUNT(CASE WHEN rq.quotation_sales_price LIKE '%%NQ%%' AND rq.ref LIKE '%%VOC%%' AND DATE(rq.quotation_rfqdate) >= CURDATE() THEN 1 END) as nq_voc,
                COUNT(CASE WHEN rq.quotation_sales_price LIKE '%%NQ%%' AND rq.ref LIKE '%%P%%' AND DATE(rq.quotation_rfqdate) >= CURDATE() THEN 1 END) as nq_p
            FROM `tabRequest And Quote` rq
            WHERE 1=1 {rep_filter}
        """
        exc_row = frappe.db.sql(exceptions_sql, tuple(params), as_dict=True)
        if exc_row:
            result.update(exc_row[0])

        result['nq_catalog_brands'] = 0

        # 4. WEEKLY WORKLOAD (FIXED)
        weekly_workload = []
        try:
            weekly_sql = f"""
                SELECT 
                    DATE(rq.quotation_rfqdate) as work_date,
                    COUNT(CASE WHEN (rq.procurement_status IS NULL OR rq.procurement_status = '') THEN 1 END) as empties,
                    COUNT(CASE WHEN rq.procurement_status LIKE '%%W%%' THEN 1 END) as ws
                FROM `tabRequest And Quote` rq
                WHERE DATE(rq.quotation_rfqdate) BETWEEN CURDATE() AND CURDATE() + INTERVAL 7 DAY {rep_filter}
                GROUP BY DATE(rq.quotation_rfqdate)
                ORDER BY work_date
            """
            weekly_rows = frappe.db.sql(weekly_sql, tuple(params), as_dict=True)

            now_dt = datetime.now()
            date_map = {str(r['work_date']): r for r in weekly_rows}

            for offset in range(8):
                target_date = now_dt + timedelta(days=offset)
                date_key = target_date.strftime('%Y-%m-%d')
                row = date_map.get(date_key, {'empties': 0, 'ws': 0})

                if offset == 0:
                    label = "Today"
                else:
                    label = target_date.strftime('%A')

                weekly_workload.append({
                    "day_label": label,
                    "empties": int(row.get('empties', 0)),
                    "ws": int(row.get('ws', 0))
                })
        except Exception as e:
            frappe.log_error("Weekly Workload Chart Error", str(e))
            weekly_workload = []

        result['weekly_workload'] = weekly_workload

        # 5. DUE SOON
        result['day_due_soon'] = 0
        try:
            sql_soon = f"""
                SELECT rq.quotation_rfqdate as RFQDATE, rq.due_time as DUE_TIME
                FROM `tabRequest And Quote` rq
                WHERE 1=1 {rep_filter}
                AND DATE(rq.quotation_rfqdate) = CURDATE()
            """
            soon_rows = frappe.db.sql(sql_soon, tuple(params), as_dict=True)

            due_soon_count = 0
            now = datetime.now()
            for row in soon_rows:
                try:
                    due_str = str(row.get('RFQDATE', '')).split()[0] + " " + (row.get('DUE_TIME') or "23:59:59")
                    due_dt = datetime.strptime(due_str, "%Y-%m-%d %H:%M:%S")
                    diff_hours = (due_dt - now).total_seconds() / 3600
                    if 0 < diff_hours <= 3:
                        due_soon_count += 1
                except:
                    pass
            result['day_due_soon'] = due_soon_count
        except Exception as e:
            frappe.log_error("Due Soon calculation error", str(e))

        return {"status": "success", "data": result, "weekly_workload": weekly_workload}

    except Exception as e:
        frappe.log_error("Dashboard Data Error", str(e))
        return {"status": "error", "error": str(e)}



@frappe.whitelist()
def get_quick_filter_badges(user_rep=None, user_st=None, group=None, mode=None, customer=None, scope=None, horizon=None):
    """
    When group == 'all' (or None) → return every badge in one (or two) efficient queries.
    When group is a specific key → keep the old single-badge behaviour for compatibility.
    mode = 'sales' → personalise on REF containing ST + '-'; otherwise personalise on rp (procurement default).
    scope = 'global' → do NOT add the personalisation filter (T3 Global toggle).
    scope = 'personal' / None → keep existing personalisation.
    horizon = 'tomorrow' → Future badge counts use due_date = tomorrow.
    horizon = 'future' / None → Future badge counts use due_date >= tomorrow.
    Today badge counts are always computed in the same query so a horizon switch refreshes them too.
    """
    try:
        is_proc_t3 = "Procurement T3" in frappe.get_roles(frappe.session.user)
        scope_norm = (scope or "personal").strip().lower()
        if scope_norm not in ("global", "personal"):
            scope_norm = "personal"
        horizon_norm = (horizon or "future").strip().lower()
        if horizon_norm not in ("future", "tomorrow"):
            horizon_norm = "future"
        future_date_pred = (
            "rq.due_date = CURDATE() + INTERVAL 1 DAY"
            if horizon_norm == "tomorrow"
            else "rq.due_date >= CURDATE() + INTERVAL 1 DAY"
        )
        # ---------- CACHE ----------
        customer_cache = str(customer).strip().replace(" ", "_") if customer else "all"
        session_user = (frappe.session.user or "").strip().lower()
        cache_key = f"proc_quick_badges_v14_{mode or 'proc'}_{user_rep or 'all'}_{user_st or 'all'}_{group or 'all'}_{customer_cache}_{scope_norm}_{horizon_norm}_{'t3' if is_proc_t3 else 'not3'}_{session_user}"
        cached = frappe.cache().get_value(cache_key)
        if cached:
            return cached
        start_ts = datetime.now()
        result = {}
        ny_tz = pytz.timezone('America/New_York')
        now = datetime.now(ny_tz)
        # Mode-aware personalisation filter
        # Sales Personal = REF contains the user's ST code followed by '-'
        # Sales Global   = no REF filter
        # Procurement Personal = rq.rep = user_rep
        # Procurement Global   = no rep filter
        personalize_filter = ""
        params = []
        if mode == "sales":
            if scope_norm != "global" and user_st and str(user_st).strip():
                st_val = str(user_st).strip()
                if not st_val.endswith("-"):
                    st_val = st_val + "-"
                personalize_filter = " AND rq.ref LIKE %s "
                params.append(f"%{st_val}%")
        elif (
            scope_norm != "global"
            and user_rep
            and str(user_rep).strip()
        ):
            personalize_filter = " AND rq.rep = %s "
            params.append(str(user_rep).strip())
        customer_filter = ""
        customer_params = []
        if customer and str(customer).strip():
            customer_filter = " AND rq.customer = %s "
            customer_params.append(str(customer).strip())
            personalize_filter += customer_filter
            params.append(str(customer).strip())
        # ================================================================
        # BULK MODE (group == 'all' or None) – the fast path
        # ================================================================
        if group is None or group == 'all':

            # ---- 1. One range scan for every pure-COUNT badge ----
            # Base filter already restricts to due_date >= today → index-friendly
            bulk_sql = f"""
                SELECT
                    /* TODAY */
                    COUNT(CASE WHEN rq.due_date < CURDATE() + INTERVAL 1 DAY THEN 1 END)                                          AS qf_today_total,
                    COUNT(CASE WHEN rq.due_date < CURDATE() + INTERVAL 1 DAY AND rq.procurement_status LIKE '%%W%%' THEN 1 END) AS qf_today_w,
                    COUNT(CASE WHEN rq.due_date < CURDATE() + INTERVAL 1 DAY AND (rq.procurement_status IS NULL OR rq.procurement_status = '') THEN 1 END) AS qf_today_empty,
                    COUNT(CASE WHEN rq.due_date < CURDATE() + INTERVAL 1 DAY AND rq.ref LIKE '%%Z%%' THEN 1 END)                   AS qf_today_z,
                    COUNT(CASE WHEN rq.due_date < CURDATE() + INTERVAL 1 DAY AND rq.ref REGEXP '^[a-zA-Z0-9]+-[0-9]+-(VOC|V|P|B|F)[a-zA-Z0-9]+' THEN 1 END) AS qf_today_voc,
                    COUNT(CASE WHEN rq.due_date < CURDATE() + INTERVAL 1 DAY AND (rq.procurement_status LIKE '%%x%%' OR rq.procurement_status LIKE '%%X%%') THEN 1 END) AS qf_today_xs,
                    COUNT(CASE WHEN rq.due_date < CURDATE() + INTERVAL 1 DAY AND rq.procurement_status = 'Q-QUOTED' AND IFNULL(rq.custom_sales_status, '') NOT IN ('S-SUBMITTED', 'C-CLOSED', 'A-AWARDED') THEN 1 END) AS qf_today_pending_sub,

                    /* FUTURE or TOMORROW (same keys; predicate chosen by horizon) */
                    COUNT(CASE WHEN {future_date_pred} THEN 1 END)                                      AS qf_future_total,
                    COUNT(CASE WHEN {future_date_pred} AND rq.procurement_status LIKE '%%W%%' THEN 1 END) AS qf_future_w,
                    COUNT(CASE WHEN {future_date_pred} AND (rq.procurement_status IS NULL OR rq.procurement_status = '') THEN 1 END) AS qf_future_empty,
                    COUNT(CASE WHEN {future_date_pred} AND rq.ref LIKE '%%Z%%' THEN 1 END)               AS qf_future_z,
                    COUNT(CASE WHEN {future_date_pred} AND rq.ref REGEXP '^[a-zA-Z0-9]+-[0-9]+-(VOC|V|P|B|F)[a-zA-Z0-9]+' THEN 1 END) AS qf_future_voc,
                    COUNT(CASE WHEN {future_date_pred} AND (rq.procurement_status LIKE '%%x%%' OR rq.procurement_status LIKE '%%X%%') THEN 1 END) AS qf_future_xs,
                    COUNT(CASE WHEN {future_date_pred} AND rq.procurement_status = 'Q-QUOTED' AND IFNULL(rq.custom_sales_status, '') NOT IN ('S-SUBMITTED', 'C-CLOSED', 'A-AWARDED') THEN 1 END) AS qf_future_pending_sub
                FROM `tabRequest And Quote` rq FORCE INDEX (idx_raq_due_date)
                WHERE rq.due_date >= CURDATE()
                {personalize_filter}
            """
            bulk_row = frappe.db.sql(bulk_sql, tuple(params), as_dict=True)
            if bulk_row:
                result.update(bulk_row[0])

            # Global alert counts. Never use personalize_filter here.
            # Personal / selected-rep scope must not hide 113, 114, 116, or 118.
            result["qf_alert_today_xs"] = 0
            result["qf_alert_future_xs"] = 0
            result["qf_alert_today_total"] = 0
            result["qf_alert_future_total"] = 0
            alert_user = session_user.split("@")[0]
            alert_reps = {
                "gl101": ("113", "114"),
                "gl100": ("116",),
                "gl102": ("118",),
            }.get(alert_user)
            if alert_reps and (mode or "procurement") != "sales":
                rep_placeholders = ", ".join(["%s"] * len(alert_reps))
                alert_sql = f"""
                    SELECT
                        COUNT(CASE
                            WHEN rq.due_date >= CURDATE()
                             AND rq.due_date < CURDATE() + INTERVAL 1 DAY
                             AND (rq.procurement_status LIKE '%%x%%' OR rq.procurement_status LIKE '%%X%%')
                             AND TRIM(rq.rep) IN ({rep_placeholders})
                            THEN 1 END) AS qf_alert_today_xs,
                        COUNT(CASE
                            WHEN {future_date_pred}
                             AND (rq.procurement_status LIKE '%%x%%' OR rq.procurement_status LIKE '%%X%%')
                             AND TRIM(rq.rep) IN ({rep_placeholders})
                            THEN 1 END) AS qf_alert_future_xs
                    FROM `tabRequest And Quote` rq FORCE INDEX (idx_raq_due_date)
                    WHERE rq.due_date >= CURDATE()
                """
                alert_params = list(alert_reps) + list(alert_reps)
                if alert_user == "gl102":
                    allowed = ("100", "101", "102", "113", "114", "116", "118")
                    allowed_placeholders = ", ".join(["%s"] * len(allowed))
                    alert_sql = f"""
                        SELECT
                            COUNT(CASE
                                WHEN rq.due_date >= CURDATE()
                                 AND rq.due_date < CURDATE() + INTERVAL 1 DAY
                                 AND (rq.procurement_status LIKE '%%x%%' OR rq.procurement_status LIKE '%%X%%')
                                 AND TRIM(rq.rep) IN ({rep_placeholders})
                                THEN 1 END) AS qf_alert_today_xs,
                            COUNT(CASE
                                WHEN {future_date_pred}
                                 AND (rq.procurement_status LIKE '%%x%%' OR rq.procurement_status LIKE '%%X%%')
                                 AND TRIM(rq.rep) IN ({rep_placeholders})
                                THEN 1 END) AS qf_alert_future_xs,
                            COUNT(CASE
                                WHEN rq.due_date >= CURDATE()
                                 AND rq.due_date < CURDATE() + INTERVAL 1 DAY
                                 AND IFNULL(TRIM(rq.rep), '') != ''
                                 AND TRIM(rq.rep) NOT IN ({allowed_placeholders})
                                THEN 1 END) AS qf_alert_today_total,
                            COUNT(CASE
                                WHEN {future_date_pred}
                                 AND IFNULL(TRIM(rq.rep), '') != ''
                                 AND TRIM(rq.rep) NOT IN ({allowed_placeholders})
                                THEN 1 END) AS qf_alert_future_total
                        FROM `tabRequest And Quote` rq FORCE INDEX (idx_raq_due_date)
                        WHERE rq.due_date >= CURDATE()
                    """
                    alert_params = list(alert_reps) + list(alert_reps) + list(allowed) + list(allowed)
                alert_row = frappe.db.sql(alert_sql, tuple(alert_params), as_dict=True)
                if alert_row:
                    result["qf_alert_today_xs"] = int(alert_row[0].get("qf_alert_today_xs") or 0)
                    result["qf_alert_future_xs"] = int(alert_row[0].get("qf_alert_future_xs") or 0)
                    result["qf_alert_today_total"] = int(alert_row[0].get("qf_alert_today_total") or 0)
                    result["qf_alert_future_total"] = int(alert_row[0].get("qf_alert_future_total") or 0)

            # ---- 2. Assign badge (different date rule + no personalisation filter) ----
            # Intentionally left without personalize_filter – Assign is procurement-only and global
            # Rule: due_date >= (today - 1 day)  AND  rp is empty
            try:
                assign_row = frappe.db.sql("""
                    SELECT COUNT(*) as count
                    FROM `tabRequest And Quote` rq
                    WHERE rq.due_date >= CURDATE() - INTERVAL 1 DAY
                      AND (rq.rep IS NULL OR rq.rep = '')
                """, as_dict=True)
                result['qf_future_assign'] = assign_row[0]['count'] if assign_row else 0
            except Exception as e:
                frappe.log_error("Assign Badge Calc Error", str(e))
                result['qf_future_assign'] = 0
                
            
            # ---- 2b. Uploaded Today (Sales Today badge) ----
            # date = today, independent of due_date. Same personalize_filter as other badges.
            try:
                uploaded_row = frappe.db.sql(
                    f"""
                    SELECT COUNT(*) AS qf_today_uploaded
                    FROM `tabRequest And Quote` rq
                    WHERE rq.date >= CURDATE()
                      AND rq.date < CURDATE() + INTERVAL 1 DAY
                    {personalize_filter}
                    """,
                    tuple(params),
                    as_dict=True,
                )
                result["qf_today_uploaded"] = (
                    uploaded_row[0]["qf_today_uploaded"] if uploaded_row else 0
                )
            except Exception as e:
                frappe.log_error("Uploaded Badge Calc Error", str(e))
                result["qf_today_uploaded"] = 0

            # ---- 3. Due-Soon (needs Python time arithmetic) ----
            try:
                # Procurement view: only empty/null or exact W-WAITING.
                # Sales view keeps the previous “any status” behaviour.
                due_soon_status_filter = """
                  AND (
                        rq.procurement_status IS NULL
                     OR rq.procurement_status = ''
                     OR rq.procurement_status = 'W-WAITING'
                  )
                """

                # Only the columns we need + only today
                proc_raw = frappe.db.sql(f"""
                    SELECT rq.due_date as proc_date, rq.due_time
                    FROM `tabRequest And Quote` rq
                    WHERE rq.due_date >= CURDATE() AND rq.due_date < CURDATE() + INTERVAL 1 DAY
                    {due_soon_status_filter}
                    {personalize_filter}
                """, tuple(params), as_dict=True)

                proc_due_soon = 0
                for d in proc_raw:
                    try:
                        due_time = d.get('due_time') or '23:59:59'
                        rfq_date = d.get('proc_date')
                        if hasattr(rfq_date, 'strftime'):
                            date_str = rfq_date.strftime('%Y-%m-%d')
                        else:
                            date_str = str(rfq_date).split(' ')[0]
                        dt = datetime.strptime(f"{date_str} {due_time}", "%Y-%m-%d %H:%M:%S")
                        dt = ny_tz.localize(dt)
                        diff = (dt - now).total_seconds() / 3600
                        if 0 < diff <= 3:
                            proc_due_soon += 1
                    except Exception:
                        continue
                result['qf_proc_due_soon'] = proc_due_soon
            except Exception as e:
                frappe.log_error("Due Soon Calc Error", str(e))
                result['qf_proc_due_soon'] = 0

            # ---- 4. Escalated (any date – status only) ----
            # Status is stored as the exact value "E" (see escalate_records).
            # Use equality so the BTREE index on procurement_status can be used.
            try:
                esc_sql = f"""
                    SELECT COUNT(*) AS qf_escalated
                    FROM `tabRequest And Quote` rq
                    WHERE rq.procurement_status = 'E'
                    {personalize_filter}
                """
                esc_row = frappe.db.sql(esc_sql, tuple(params), as_dict=True)
                result['qf_escalated'] = int(esc_row[0]['qf_escalated']) if esc_row else 0
            except Exception as e:
                frappe.log_error("Escalated Badge Calc Error", str(e))
                result['qf_escalated'] = 0

            # ---- 5. Became an Order (DISABLED – restore with the Orders badge) ----
            # try:
            #     order_sql = f"""
            #         SELECT COUNT(DISTINCT rq.name) AS qf_has_order
            #         FROM `tabRequest And Quote` rq
            #         INNER JOIN `tabCustomer Order` co ON co.id = rq.name
            #         WHERE 1=1
            #         {personalize_filter}
            #     """
            #     order_row = frappe.db.sql(order_sql, tuple(params), as_dict=True)
            #     result['qf_has_order'] = int(order_row[0]['qf_has_order']) if order_row else 0
            # except Exception as e:
            #     frappe.log_error("Has Order Badge Calc Error", str(e))
            #     result['qf_has_order'] = 0

            elapsed_ms = int((datetime.now() - start_ts).total_seconds() * 1000)
            result['_elapsed_ms'] = elapsed_ms
            result['_group'] = 'all'

            final = {"status": "success", "data": result}
            frappe.cache().set_value(cache_key, final, expires_in_sec=5)
            return final

        # ================================================================
        # LEGACY SINGLE-GROUP MODE (kept for safety / future use)
        # ================================================================
        # ------------------------------------------------------------------
        # TODAY TOTAL
        # ------------------------------------------------------------------
        if group == 'today_total':
            row = frappe.db.sql(f"""
                SELECT COUNT(*) as qf_today_total
                FROM `tabRequest And Quote` rq
                WHERE rq.due_date >= CURDATE() AND rq.due_date < CURDATE() + INTERVAL 1 DAY
                {personalize_filter}
            """, tuple(params), as_dict=True)
            if row:
                result['qf_today_total'] = row[0]['qf_today_total']

        # ------------------------------------------------------------------
        # TODAY W's
        # ------------------------------------------------------------------
        elif group == 'today_w':
            row = frappe.db.sql(f"""
                SELECT COUNT(*) as qf_today_w
                FROM `tabRequest And Quote` rq
                WHERE rq.due_date >= CURDATE() AND rq.due_date < CURDATE() + INTERVAL 1 DAY
                  AND rq.procurement_status LIKE '%%W%%'
                {personalize_filter}
            """, tuple(params), as_dict=True)
            if row:
                result['qf_today_w'] = row[0]['qf_today_w']

        # ------------------------------------------------------------------
        # TODAY EMPTIES
        # ------------------------------------------------------------------
        elif group == 'today_empty':
            row = frappe.db.sql(f"""
                SELECT COUNT(*) as qf_today_empty
                FROM `tabRequest And Quote` rq
                WHERE rq.due_date >= CURDATE() AND rq.due_date < CURDATE() + INTERVAL 1 DAY
                  AND (rq.procurement_status IS NULL OR rq.procurement_status = '')
                {personalize_filter}
            """, tuple(params), as_dict=True)
            if row:
                result['qf_today_empty'] = row[0]['qf_today_empty']

        # ------------------------------------------------------------------
        # TODAY Z REF
        # ------------------------------------------------------------------
        elif group == 'today_z':
            row = frappe.db.sql(f"""
                SELECT COUNT(*) as qf_today_z
                FROM `tabRequest And Quote` rq
                WHERE rq.due_date >= CURDATE() AND rq.due_date < CURDATE() + INTERVAL 1 DAY
                  AND rq.ref LIKE '%%Z%%'
                {personalize_filter}
            """, tuple(params), as_dict=True)
            if row:
                result['qf_today_z'] = row[0]['qf_today_z']

        # ------------------------------------------------------------------
        # TODAY VOC / V / P / B / F
        # ------------------------------------------------------------------
        elif group == 'today_voc':
            row = frappe.db.sql(f"""
                SELECT COUNT(*) as qf_today_voc
                FROM `tabRequest And Quote` rq
                WHERE rq.due_date >= CURDATE() AND rq.due_date < CURDATE() + INTERVAL 1 DAY
                  AND rq.ref REGEXP '^[a-zA-Z0-9]+-[0-9]+-(VOC|V|P|B|F)[a-zA-Z0-9]+'
                {personalize_filter}
            """, tuple(params), as_dict=True)
            if row:
                result['qf_today_voc'] = row[0]['qf_today_voc']

        # ------------------------------------------------------------------
        # TODAY X's
        # ------------------------------------------------------------------
        elif group == 'today_xs':
            row = frappe.db.sql(f"""
                SELECT COUNT(*) as qf_today_xs
                FROM `tabRequest And Quote` rq
                WHERE rq.due_date >= CURDATE() AND rq.due_date < CURDATE() + INTERVAL 1 DAY
                  AND (rq.procurement_status LIKE '%%x%%' OR rq.procurement_status LIKE '%%X%%')
                {personalize_filter}
            """, tuple(params), as_dict=True)
            if row:
                result['qf_today_xs'] = row[0]['qf_today_xs']
            row = frappe.db.sql(f"""
                SELECT COUNT(*) as qf_today_xs
                FROM `tabRequest And Quote` rq
                WHERE rq.due_date >= CURDATE() AND rq.due_date < CURDATE() + INTERVAL 1 DAY
                  AND (rq.procurement_status LIKE '%%x%%' OR rq.procurement_status LIKE '%%X%%')
                {xs_filter}
            """, xs_params, as_dict=True)
            if row:
                result['qf_today_xs'] = row[0]['qf_today_xs']

        # ------------------------------------------------------------------
        # Pending Submission
        # ------------------------------------------------------------------
        elif group == 'today_pending_sub':
            row = frappe.db.sql(f"""
                SELECT COUNT(*) as qf_today_pending_sub
                FROM `tabRequest And Quote` rq
                WHERE rq.due_date >= CURDATE() AND rq.due_date < CURDATE() + INTERVAL 1 DAY
                  AND rq.procurement_status = 'Q-QUOTED'
                  AND IFNULL(rq.custom_sales_status, '') NOT IN ('S-SUBMITTED', 'C-CLOSED', 'A-AWARDED')
                {personalize_filter}
            """, tuple(params), as_dict=True)
            if row:
                result['qf_today_pending_sub'] = row[0]['qf_today_pending_sub']
        
        
        
        
        
        
        # ------------------------------------------------------------------
        # TODAY DUE SOON (still needs the Python time filter)
        # ------------------------------------------------------------------
        elif group == 'today_due_soon':
            try:
                due_soon_status_filter = """
                  AND (
                        rq.procurement_status IS NULL
                     OR rq.procurement_status = ''
                     OR rq.procurement_status = 'W-WAITING'
                  )
                """

                proc_raw = frappe.db.sql(f"""
                    SELECT rq.due_date as proc_date, rq.due_time 
                    FROM `tabRequest And Quote` rq 
                    WHERE rq.due_date >= CURDATE() AND rq.due_date < CURDATE() + INTERVAL 1 DAY
                    {due_soon_status_filter}
                    {personalize_filter}
                """, tuple(params), as_dict=True)

                proc_due_soon = 0
                for d in proc_raw:
                    try:
                        due_time = d.get('due_time') or '23:59:59'
                        rfq_date = d.get('proc_date')
                        if hasattr(rfq_date, 'strftime'):
                            date_str = rfq_date.strftime('%Y-%m-%d')
                        else:
                            date_str = str(rfq_date).split(' ')[0]
                        dt = datetime.strptime(f"{date_str} {due_time}", "%Y-%m-%d %H:%M:%S")
                        dt = ny_tz.localize(dt)
                        diff = (dt - now).total_seconds() / 3600
                        if 0 < diff <= 3:
                            proc_due_soon += 1
                    except Exception:
                        continue
                result['qf_proc_due_soon'] = proc_due_soon
            except Exception as e:
                frappe.log_error("Due Soon Calc Error", str(e))
                result['qf_proc_due_soon'] = 0

        # ------------------------------------------------------------------
        # FUTURE TOTAL
        # ------------------------------------------------------------------
        elif group == 'future_total':
            row = frappe.db.sql(f"""
                SELECT COUNT(*) as qf_future_total
                FROM `tabRequest And Quote` rq
                WHERE rq.due_date >= CURDATE() + INTERVAL 1 DAY
                {personalize_filter}
            """, tuple(params), as_dict=True)
            if row:
                result['qf_future_total'] = row[0]['qf_future_total']

        # ------------------------------------------------------------------
        # FUTURE W's
        # ------------------------------------------------------------------
        elif group == 'future_w':
            row = frappe.db.sql(f"""
                SELECT COUNT(*) as qf_future_w
                FROM `tabRequest And Quote` rq
                WHERE rq.due_date >= CURDATE() + INTERVAL 1 DAY
                  AND rq.procurement_status LIKE '%%W%%'
                {personalize_filter}
            """, tuple(params), as_dict=True)
            if row:
                result['qf_future_w'] = row[0]['qf_future_w']

        # ------------------------------------------------------------------
        # FUTURE EMPTIES
        # ------------------------------------------------------------------
        elif group == 'future_empty':
            row = frappe.db.sql(f"""
                SELECT COUNT(*) as qf_future_empty
                FROM `tabRequest And Quote` rq
                WHERE rq.due_date >= CURDATE() + INTERVAL 1 DAY
                  AND (rq.procurement_status IS NULL OR rq.procurement_status = '')
                {personalize_filter}
            """, tuple(params), as_dict=True)
            if row:
                result['qf_future_empty'] = row[0]['qf_future_empty']

        # ------------------------------------------------------------------
        # FUTURE Z REF
        # ------------------------------------------------------------------
        elif group == 'future_z':
            row = frappe.db.sql(f"""
                SELECT COUNT(*) as qf_future_z
                FROM `tabRequest And Quote` rq
                WHERE rq.due_date >= CURDATE() + INTERVAL 1 DAY
                  AND rq.ref LIKE '%%Z%%'
                {personalize_filter}
            """, tuple(params), as_dict=True)
            if row:
                result['qf_future_z'] = row[0]['qf_future_z']

        # ------------------------------------------------------------------
        # FUTURE VOC / V / P / B / F
        # ------------------------------------------------------------------
        elif group == 'future_voc':
            row = frappe.db.sql(f"""
                SELECT COUNT(*) as qf_future_voc
                FROM `tabRequest And Quote` rq
                WHERE rq.due_date >= CURDATE() + INTERVAL 1 DAY
                  AND rq.ref REGEXP '^[a-zA-Z0-9]+-[0-9]+-(VOC|V|P|B|F)[a-zA-Z0-9]+'
                {personalize_filter}
            """, tuple(params), as_dict=True)
            if row:
                result['qf_future_voc'] = row[0]['qf_future_voc']

        # ------------------------------------------------------------------
        # FUTURE ASSIGN
        # ------------------------------------------------------------------
        elif group == 'future_assign':
            try:
                assign_row = frappe.db.sql("""
                    SELECT COUNT(*) as count
                    FROM `tabRequest And Quote` rq
                    WHERE rq.due_date >= '2026-06-01' AND (rq.rp IS NULL OR rq.rp = '')
                """, as_dict=True)
                result['qf_future_assign'] = assign_row[0]['count'] if assign_row else 0
            except Exception as e:
                frappe.log_error("Assign Badge Calc Error", str(e))
                result['qf_future_assign'] = 0
                
        
        
        # ------------------------------------------------------------------
        # FUTURE X's
        # ------------------------------------------------------------------
        elif group == 'future_xs':
            row = frappe.db.sql(f"""
                SELECT COUNT(*) as qf_future_xs
                FROM `tabRequest And Quote` rq
                WHERE rq.due_date >= CURDATE() + INTERVAL 1 DAY
                  AND (rq.procurement_status LIKE '%%x%%' OR rq.procurement_status LIKE '%%X%%')
                {personalize_filter}
            """, tuple(params), as_dict=True)
            if row:
                result['qf_future_xs'] = row[0]['qf_future_xs']
            row = frappe.db.sql(f"""
                SELECT COUNT(*) as qf_future_xs
                FROM `tabRequest And Quote` rq
                WHERE rq.due_date >= CURDATE() + INTERVAL 1 DAY
                  AND (rq.procurement_status LIKE '%%x%%' OR rq.procurement_status LIKE '%%X%%')
                {xs_filter}
            """, xs_params, as_dict=True)
            if row:
                result['qf_future_xs'] = row[0]['qf_future_xs']
        
        
        # ------------------------------------------------------------------
        # Pending Submission - Future
        # ------------------------------------------------------------------
        elif group == 'future_pending_sub':
            row = frappe.db.sql(f"""
                SELECT COUNT(*) as qf_future_pending_sub
                FROM `tabRequest And Quote` rq
                WHERE rq.due_date >= CURDATE() + INTERVAL 1 DAY
                  AND rq.procurement_status = 'Q-QUOTED'
                  AND IFNULL(rq.custom_sales_status, '') NOT IN ('S-SUBMITTED', 'C-CLOSED', 'A-AWARDED')
                {personalize_filter}
            """, tuple(params), as_dict=True)
            if row:
                result['qf_future_pending_sub'] = row[0]['qf_future_pending_sub']
                
        
        elif group == "today_uploaded":
            row = frappe.db.sql(
                f"""
                SELECT COUNT(*) AS qf_today_uploaded
                FROM `tabRequest And Quote` rq
                WHERE rq.date >= CURDATE()
                  AND rq.date < CURDATE() + INTERVAL 1 DAY
                {personalize_filter}
                """,
                tuple(params),
                as_dict=True,
            )
            if row:
                result["qf_today_uploaded"] = row[0]["qf_today_uploaded"]
        
        

        # ------------------------------------------------------------------
        # Fallback
        # ------------------------------------------------------------------
        else:
            pass

        elapsed_ms = int((datetime.now() - start_ts).total_seconds() * 1000)
        result['_elapsed_ms'] = elapsed_ms
        result['_group'] = group or 'all'

        final = {"status": "success", "data": result}
        frappe.cache().set_value(cache_key, final, expires_in_sec=5)
        return final

    except Exception as e:
        frappe.log_error("Quick Filter Badge Data Error", str(e))
        return {"status": "error", "error": str(e)}        


@frappe.whitelist()
def get_sales_rfq_records(page=1, page_length=50, sort_by='ID', sort_order='desc', search_query=None, filters=None, skip_count=0, search_scope="sales", count_only=0):
    """Sales view of RFQ records - adapted to use tabRequest And Quote"""
    try:
        start = (int(page) - 1) * int(page_length)
        skip_count = int(skip_count or 0)
        count_only = int(count_only or 0)
        base_sql = "FROM `tabRequest And Quote` rq"
        where_conds = []
        params = []
        # ====================== CACHING LAYER (Sales Panel) ======================
        filter_str = filters or ""
        filter_hash = hashlib.md5(filter_str.encode("utf-8")).hexdigest()
        search_part = (search_query or "").replace(" ", "_")[:60]
        cache_key = f"proc_get_sales_rfq_v6:{page}:{page_length}:{sort_by}:{sort_order}:{search_part}:{filter_hash}:{search_scope}:sc{skip_count}:co{count_only}"
        cached = frappe.cache().get_value(cache_key)
        if cached:
            return cached
        # =======================================================================
        if filters:
            try:
                flist = json.loads(filters)
                field_map = {
                    'id': 'rq.name', 'ref': 'rq.ref', 'customer_ref': 'rq.customer_ref',
                    'date': 'rq.date', 'rp': 'rq.rep', 'item': 'rq.item',
                    'qty': 'rq.qty', 'unit': 'rq.unit', 'brand': 'rq.brand',
                    'part_number': 'rq.part_number', 'description': 'rq.description',
                    'country': 'rq.country', 'incoterm': 'rq.incoterm',
                    'sale_price': 'rq.sale_price', 'quote_price': 'rq.quotation_sales_price',
                    'sales_price': 'rq.quotation_sales_price',          # ← added for quick-filter parity
                    'due_date': 'rq.due_date',
                    'due_time': 'rq.due_time', 'sap': 'rq.sap', 'customer': 'rq.customer',
                    'div': 'rq.`div`', 'st': 'rq.st', 'contact': 'rq.contact', 
                    'email_customer': 'rq.email_customr', 'weight': 'rq.weight', 
                    'feedback': 'rq.feedback',
                    'customer_bid_number': 'rq.custom_customer_bid_number',
                    'delivery': 'rq.delivery', 'note': 'rq.note',
                    'custom_sales_status': 'rq.custom_sales_status',
                    'procurement_status': 'rq.procurement_status',
                    'rfq_date': 'rq.date', 'rep': 'rq.rep',
                    'reference_price': 'rq.reference_price'          # ← REQUIRED for Ref Price filter
                }
                for f in flist:
                    field = f.get('field')
                    op = f.get('operator')
                    val = f.get('value', '')
                    val2 = f.get('value2', '')
                    # Special filter: row became an order (linked Customer Order exists)
                    if field == 'has_customer_order':
                        wants_order = op in ('equals', 'not_empty', 'contains') and str(val) not in ('0', 'false', 'False', '')
                        if op == 'is_empty' or op == 'not_equals' or str(val) in ('0', 'false', 'False'):
                            wants_order = False
                        if wants_order:
                            where_conds.append("EXISTS (SELECT 1 FROM `tabCustomer Order` co WHERE co.id = rq.name)")
                        else:
                            where_conds.append("NOT EXISTS (SELECT 1 FROM `tabCustomer Order` co WHERE co.id = rq.name)")
                        continue
                    # Due Soon: empty/null OR exact W-WAITING only.
                    # Any other procurement_status is excluded.
                    if field == 'due_soon_open_status':
                        where_conds.append(
                            "("
                            "rq.procurement_status IS NULL "
                            "OR rq.procurement_status = '' "
                            "OR rq.procurement_status = 'W-WAITING'"
                            ")"
                        )
                        continue
                    # Pending Submission (Sales Today/Future badge):
                    # quoted by procurement, not yet submitted, closed, or awarded.
                    # Blank / NULL sales status is treated as still pending.
                    if field == 'pending_submission':
                        where_conds.append(
                            "("
                            "rq.procurement_status = 'Q-QUOTED' "
                            "AND IFNULL(rq.custom_sales_status, '') NOT IN ("
                            "'S-SUBMITTED', 'C-CLOSED', 'A-AWARDED'"
                            ")"
                            ")"
                        )
                        continue
                    if field not in field_map: continue
                    col = field_map[field]
                    if op == 'is_empty':
                        where_conds.append(f"({col} IS NULL OR {col} = '')")
                    elif op == 'not_empty':
                        where_conds.append(f"({col} IS NOT NULL AND {col} != '')")
                    elif op == 'equals':
                        # Date / DateTime columns: use a 1-day range so a BTREE
                        # on `date` / `due_date` can be used. A plain "=" on a
                        # DATETIME column only matches midnight and cannot use
                        # the index as well if the column is wrapped in DATE().
                        if field in ('date', 'due_date', 'rfq_date', 'date_req'):
                            where_conds.append(
                                f"({col} >= %s AND {col} < DATE_ADD(%s, INTERVAL 1 DAY))"
                            )
                            params.extend([val, val])
                        else:
                            where_conds.append(f"{col} = %s")
                            params.append(val)
                    elif op == 'not_equals':
                        where_conds.append(f"{col} != %s")
                        params.append(val)
                    elif op == 'gte':
                        where_conds.append(f"{col} >= %s")
                        params.append(val)
                    elif op == 'lte':
                        where_conds.append(f"{col} <= %s")
                        params.append(val)
                    elif op == 'contains':
                        if field == 'sap':
                            # Force the FULLTEXT index and use a cleaner pattern for short codes
                            where_conds.append(f"MATCH({col}) AGAINST(%s IN BOOLEAN MODE)")
                            params.append(f"+{val}*")
                        if field == 'st':
                            # Special handling for multi-value ST fields
                            # (e.g. user ST = "308" must match "308/301", "301/308", etc.)
                            where_conds.append(f"""
                                (
                                    {col} = %s
                                    OR {col} LIKE %s
                                    OR {col} LIKE %s
                                    OR {col} LIKE %s
                                )
                            """)
                            params.extend([
                                val,                 # exact
                                f"{val}/%",          # starts with val/
                                f"%/{val}",          # ends with /val
                                f"%/{val}/%"         # contains /val/
                            ])
                        elif field == 'part_number':
                            # Fast substring filter on part_number.
                            # Do NOT wrap the column in LOWER() — that blocks a BTREE
                            # index scan. Collation on the column is already CI, so
                            # 'z1134' still matches '53535z1134' and 'z1134565656'.
                            escaped_pn = (
                                str(val)
                                .replace("\\", "\\\\")
                                .replace("%", "\\%")
                                .replace("_", "\\_")
                            )
                            where_conds.append(f"{col} LIKE %s")
                            params.append(f"%{escaped_pn}%")
                        else:
                            # True substring match (case-insensitive) for all other fields
                            where_conds.append(f"LOWER({col}) LIKE LOWER(%s)")
                            params.append(f"%{val}%")
                    elif op == 'not_contains':
                        if field == 'part_number':
                            escaped_pn = (
                                str(val)
                                .replace("\\", "\\\\")
                                .replace("%", "\\%")
                                .replace("_", "\\_")
                            )
                            where_conds.append(f"{col} NOT LIKE %s")
                            params.append(f"%{escaped_pn}%")
                        else:
                            where_conds.append(f"LOWER({col}) NOT LIKE LOWER(%s)")
                            params.append(f"%{val}%")
                    elif op == 'starts_with':
                        if field == 'part_number':
                            escaped_pn = (
                                str(val)
                                .replace("\\", "\\\\")
                                .replace("%", "\\%")
                                .replace("_", "\\_")
                            )
                            where_conds.append(f"{col} LIKE %s")
                            params.append(f"{escaped_pn}%")
                        else:
                            where_conds.append(f"LOWER({col}) LIKE LOWER(%s)")
                            params.append(f"{val}%")
                    elif op == 'regex':
                        where_conds.append(f"{col} REGEXP %s")
                        params.append(val)
                    elif op == 'between':
                        where_conds.append(f"{col} BETWEEN %s AND %s")
                        params.extend([val, val2])
            except Exception as e:
                frappe.log_error("Sales Filter Parse Error", str(e))
        if search_query:
            search_cond, search_params = _build_global_search_condition(
                search_query, search_scope or "sales"
            )
            if search_cond:
                where_conds.append(search_cond)
                params.extend(search_params)
        where_clause = " WHERE " + " AND ".join(where_conds) if where_conds else ""
        # ====================== OPTIMIZED TOTAL COUNT ======================
        use_count_query = bool(where_conds)
        total_count = 0
        if not where_conds:
            total_count = 500000
        elif skip_count:
            total_count = None
        sort_map = {
            'ID': 'rq.name', 'REF': 'rq.ref', 'CUSTOMER_REF': 'rq.customer_ref', 'DATE': 'rq.date',
            'DUE_DATE': 'rq.due_date', 'DUE_TIME': 'rq.due_time', 'RP': 'rq.rep', 'SAP': 'rq.sap',
            'ITEM': 'rq.item', 'QTY': 'rq.qty', 'UNIT': 'rq.unit', 'BRAND': 'rq.brand',
            'PART_NUMBER': 'rq.part_number', 'DESCRIPTION': 'rq.description', 'COUNTRY': 'rq.country',
            'INCOTERM': 'rq.incoterm', 'SALE_PRICE': 'rq.sale_price', 'SALES_PRICE': 'rq.quotation_sales_price', 
            'NOTE': 'rq.note', 'CUSTOMER': 'rq.customer', 'DIV': 'rq.`div`', 'CONTACT': 'rq.contact',
            'FEEDBACK': 'rq.feedback',
            'CUSTOMER_BID_NUMBER': 'rq.custom_customer_bid_number',
            'CREATION_TIME': 'rq.creation',
            'CUSTOM_SALES_STATUS': 'rq.custom_sales_status',
            'PROCUREMENT_STATUS': 'rq.procurement_status',
            'EMAIL_CUSTOMER': 'rq.email_customr',
            'REFERENCE_PRICE': 'rq.reference_price',   # ← so column header sort works
            'ST': 'rq.st'                              # ← also missing, added for completeness
        }
        actual_sort = sort_map.get(sort_by, 'rq.name')
        # Collect the filter field names so we can pick an index that matches
        # the actual WHERE clause instead of always pinning due_date.
        filter_fields = set()
        try:
            _flist_for_idx = filters
            if isinstance(_flist_for_idx, str):
                _flist_for_idx = json.loads(_flist_for_idx) if _flist_for_idx else []
            if isinstance(_flist_for_idx, list):
                filter_fields = { (f or {}).get('field') for f in _flist_for_idx }
        except Exception:
            filter_fields = set()
        # CAST(name AS CHAR) blocks PRIMARY and is the wrong cast for numeric names.
        # Only apply a cast when we are NOT filtering by the primary key.
        if where_conds and actual_sort == 'rq.name' and 'id' not in filter_fields:
            actual_sort = 'CAST(rq.name AS UNSIGNED)'
        count_params = tuple(params)
        # Pick FORCE INDEX only when it matches the filter. Never force due_date
        # onto an ID / Date query — that is what made Sales ID Equals so slow.
        force_index = ""
        if where_conds:
            if 'id' in filter_fields:
                force_index = " FORCE INDEX (PRIMARY)"
            elif 'date' in filter_fields:
                date_idx = frappe.db.sql("""
                    SELECT 1 FROM information_schema.STATISTICS
                    WHERE TABLE_SCHEMA = DATABASE()
                      AND TABLE_NAME = 'tabRequest And Quote'
                      AND INDEX_NAME = 'idx_raq_date'
                    LIMIT 1
                """)
                if date_idx:
                    force_index = " FORCE INDEX (idx_raq_date)"
            elif 'due_date' in filter_fields or 'st' in filter_fields:
                due_idx = frappe.db.sql("""
                    SELECT 1 FROM information_schema.STATISTICS
                    WHERE TABLE_SCHEMA = DATABASE()
                      AND TABLE_NAME = 'tabRequest And Quote'
                      AND INDEX_NAME = 'idx_raq_due_date'
                    LIMIT 1
                """)
                if due_idx:
                    force_index = " FORCE INDEX (idx_raq_due_date)"
        data = []
        if not count_only:
            sql = f"""
                SELECT
                    rq.name as ID, rq.ref as REF, rq.date as DATE, rq.country as COUNTRY,
                    rq.customer as CUSTOMER, rq.`div` as `DIV`, rq.contact as CONTACT,
                    rq.email_customr as EMAIL_CUSTOMER, rq.customer_ref as CUSTOMER_REF,
                    rq.due_date as DUE_DATE, rq.due_time as DUE_TIME, rq.sap as SAP,
                    rq.item as ITEM, rq.qty as QTY, rq.unit as UNIT, rq.part_number as PART_NUMBER,
                    rq.brand as BRAND, rq.description as DESCRIPTION, rq.incoterm as INCOTERM,
                    rq.note as NOTE, rq.sale_price as SALE_PRICE, rq.reference_price as REFERENCE_PRICE,
                    rq.date_req as `DATE REQ`, rq.st as ST,
                    DATE_FORMAT(rq.creation, '%%H:%%i') as CREATION_TIME,
                    
                    rq.rep as RP, rq.quotation_sales_price as SALES_PRICE, rq.quotation_attachment as ATTACHMENT,
                    rq.attachment as RFQ_ATTACHMENT,
                    
                    rq.quotation_item as QUOTE_ITEM, rq.quotation_qty as QUOTE_QTY, rq.quotation_unit as QUOTE_UNIT,
                    rq.quotation_brand as QUOTE_BRAND, rq.quotation_part_number as QUOTE_PART_NUMBER,
                    rq.quotation_co as QUOTE_CO, rq.quotation_aprox_weight as QUOTE_APROX_WEIGHT,
                    rq.quotation_incoterm as QUOTE_INCOTERM, rq.quotation_delivery as QUOTE_DELIVERY,
                    rq.quotation_description as QUOTE_DESCRIPTION, rq.quotation_note as QUOTE_NOTE,
                    rq.feedback as FEEDBACK,
                    rq.custom_customer_bid_number as CUSTOMER_BID_NUMBER,
                    rq.custom_sales_status as CUSTOM_SALES_STATUS,
                    rq.procurement_status as PROCUREMENT_STATUS,
                    rq.custom_uploaded_by as UPLOADED_BY,
                    rq.custom_requested_by as SUBMITTED_BY,
                    rq.modified as MODIFIED,
                    (SELECT c.customer_details FROM `tabCustomer` c WHERE c.name = rq.customer LIMIT 1) as CUSTOMER_DETAILS
                FROM `tabRequest And Quote` rq{force_index}
                {where_clause}
                ORDER BY {actual_sort} {sort_order}
                LIMIT %s OFFSET %s
            """
            params.extend([int(page_length), int(start)])
            data = frappe.db.sql(sql, tuple(params), as_dict=True)
            # Flag rows that have at least one linked Customer Order
            if data:
                page_ids = [str(d.get("ID")) for d in data if d.get("ID") is not None]
                ordered_set = set()
                if page_ids:
                    format_strings = ",".join(["%s"] * len(page_ids))
                    ordered_rows = frappe.db.sql(
                        f"SELECT DISTINCT id FROM `tabCustomer Order` WHERE id IN ({format_strings})",
                        tuple(page_ids),
                    )
                    ordered_set = {str(r[0]) for r in ordered_rows if r and r[0] is not None}
                for d in data:
                    d["HAS_CUSTOMER_ORDER"] = 1 if str(d.get("ID")) in ordered_set else 0
                    d["internal_attachments"] = []
                _attach_gl_item_line_tags(data)
            else:
                data = []
        # ====================== GET TOTAL FROM COUNT(*) ======================
        if use_count_query and not skip_count:
            if (not count_only) and start == 0 and len(data) < int(page_length):
                total_count = len(data)
            else:
                try:
                    count_sql = f"SELECT COUNT(*) as total FROM `tabRequest And Quote` rq{force_index} {where_clause}"
                    count_result = frappe.db.sql(count_sql, count_params, as_dict=True)
                    total_count = count_result[0]['total'] if count_result else 0
                except Exception:
                    total_count = len(data)
        elif not where_conds:
            total_count = 500000  # Fast path for unfiltered loads (same as Procurement)
        ##for d in data:
            ##d['internal_attachments'] = []
        # Safely compute total_pages only when we actually have a numeric total_count.
        # When skip_count=1 and filters/search are active we return None so the frontend
        # can keep showing the previous pagination info while the background count runs.
        if total_count is None:
            total_pages = None
        else:
            total_pages = -(-total_count // int(page_length))
        result = {
            "status": "success",
            "data": data,
            "total_records": total_count,
            "total_pages": total_pages
        }
        frappe.cache().set_value(cache_key, result, expires_in_sec=2)
        return result
    except Exception as e:
        frappe.log_error("Sales Panel Get RFQ Records Error", str(e))
        return {"status": "error", "error": str(e)}

@frappe.whitelist()
def get_quote_records_by_ids(selected_ids):
    """Returns full Quote + Request For Quote records for the given list of IDs.
       Used by the Create Customer Quotation feature so it works across pagination."""
    try:
        ids = json.loads(selected_ids)
        if not ids:
            return {"status": "error", "error": "No IDs provided"}

        format_strings = ','.join(['%s'] * len(ids))
        
        sql = f"""
            SELECT
                rq.name as ID, 
                rq.ref as REF, 
                rq.date as DATE, 
                rq.country as COUNTRY,
                rq.customer as CUSTOMER, 
                rq.contact as CONTACT,
                rq.email_customr as EMAIL_CUSTOMER, 
                rq.customer_ref as CUSTOMER_REF,
                rq.due_date as DUE_DATE, 
                rq.due_time as DUE_TIME, 
                rq.sap as SAP,
                rq.quotation_item as ITEM, 
                rq.quotation_qty as QTY, 
                rq.quotation_unit as UNIT, 
                rq.quotation_part_number as PART_NUMBER,
                rq.quotation_brand as BRAND, 
                rq.quotation_description as DESCRIPTION, 
                rq.quotation_incoterm as INCOTERM,
                rq.quotation_note as NOTE, 
                rq.sale_price as SALE_PRICE, 
                rq.quotation_sales_price as SALES_PRICE,
                rq.quotation_aprox_weight as APROX_WEIGHT,
                rq.quotation_delivery as DELIVERY,
                rq.quotation_co as CO,
                rq.reference_price as REFERENCE_PRICE,
                rq.rp as RP, 
                rq.st as ST,
                rq.quotation_attachment as ATTACHMENT, 
                rq.attachment as RFQ_ATTACHMENT
            FROM `tabRequest And Quote` rq 
            WHERE rq.name IN ({format_strings})
            ORDER BY FIELD(rq.name, {format_strings})
        """
        
        # We pass the ids twice because of the ORDER BY FIELD
        params = tuple(ids) + tuple(ids)
        data = frappe.db.sql(sql, params, as_dict=True)

        return {"status": "success", "data": data}

    except Exception as e:
        frappe.log_error("get_quote_records_by_ids Error", str(e))
        return {"status": "error", "error": str(e)}


@frappe.whitelist()
def update_sales_rfq_record(payload):
    try:
        data = json.loads(payload)
        row_id = data.get('ID')
        if not row_id:
            return {"status": "error", "error": "Row ID is required."}

        # === BANNED DATE ENFORCEMENT ===
        due_date = data.get('DUE_DATE')
        if due_date:
            check = frappe.call('my_custom_app.procurement_panel.is_date_banned', check_date=due_date)
            if check.get('is_banned'):
                return {"status": "error", "error": f"Cannot save record. {due_date} is a banned date. Please choose another due date."}
        # ================================================

        # ------------------------------------------------------------------
        # Use Document API so Frappe creates a proper Version record
        # ------------------------------------------------------------------
        doc = frappe.get_doc("Request And Quote", row_id)

        stale = _reject_stale_raq_write(doc, data)
        if stale:
            return stale

        _apply_mapped_payload(doc, data, SALES_PAYLOAD_FIELDS)
        if "CUSTOM_SALES_STATUS" in data:
            doc.custom_sales_status = _apply_submitted_by(doc, data.get("CUSTOM_SALES_STATUS"))
        if "PROCUREMENT_STATUS" in data:
            doc.procurement_status = data.get("PROCUREMENT_STATUS")

        doc.flags.ignore_permissions = True
        doc.save()
        _clear_raq_list_cache()

        # ------------------------------------------------------------------
        # Customer Order child rows — insert / update / delete missing
        # Only sync when the frontend actually sent the key, so a procurement
        # save that never loaded orders cannot wipe the sub-table.
        # ------------------------------------------------------------------
        if "customer_order_records" in data:
            _sync_customer_orders(row_id, data.get("customer_order_records") or [])

        frappe.db.commit()
        return {
            "status": "success",
            "modified": str(doc.modified),
            "modified_by": str(doc.modified_by or frappe.session.user or "")
        }

    except Exception as e:
        frappe.log_error("Sales Panel Update Error", str(e))
        return {"status": "error", "error": str(e)}


@frappe.whitelist()
def bulk_update_sales_rfq_records(payload):
    try:
        records = json.loads(payload)
        if not records:
            return {"status": "error", "error": "No records provided."}

        for data in records:
            row_id = data.get('ID')
            if not row_id:
                continue

            # ------------------------------------------------------------------
            # Use Document API so Frappe creates a proper Version record
            # ------------------------------------------------------------------
            doc = frappe.get_doc("Request And Quote", row_id)

            stale = _reject_stale_raq_write(doc, data)
            if stale:
                return stale

            _apply_mapped_payload(doc, data, SALES_PAYLOAD_FIELDS)
            if "CUSTOM_SALES_STATUS" in data:
                doc.custom_sales_status = _apply_submitted_by(doc, data.get("CUSTOM_SALES_STATUS"))
            if "PROCUREMENT_STATUS" in data:
                doc.procurement_status = data.get("PROCUREMENT_STATUS")

            doc.flags.ignore_permissions = True
            doc.save()
            _clear_raq_list_cache()

            if "customer_order_records" in data:
                _sync_customer_orders(row_id, data.get("customer_order_records") or [])

        frappe.db.commit()
        return {"status": "success", "message": f"Updated {len(records)} records."}

    except Exception as e:
        frappe.log_error("Sales Panel Bulk Update Error", str(e))
        return {"status": "error", "error": str(e)}


@frappe.whitelist()
def create_sales_rfq_record():
    # ============================================================
    # CRITICAL SAFETY: Capture the real user FIRST and never allow
    # a None / empty / "None" value to be restored later.
    # ============================================================
    current_user = frappe.session.user
    if not current_user or current_user in (None, "None", "", "Guest"):
        current_user = "Guest"
    
    
    # NEW early exit – the incoming session is already unusable
    if current_user == "Guest" and frappe.session.user in (None, "None", ""):
        return {
            "status": "error",
            "error": "Session expired or invalid (User None). Please refresh the page, log in again, and retry the upload."
        }

    try:
        # Temporarily run as Administrator to fully bypass all role/permission checks
        # This is safe because it only affects this specific internal tool operation
        uploader_st = _current_user_st_code(current_user)
        frappe.set_user("Administrator")

        new_doc = frappe.new_doc("Request And Quote")
        new_doc.custom_uploaded_by = uploader_st
        new_doc.insert()
        
        frappe.db.commit()
        
        return {"status": "success", "new_id": new_doc.name}
        
    except Exception as e:
        frappe.log_error("Sales Panel Create Record Error", str(e))
        return {"status": "error", "error": str(e)}
        
    finally:
        # CRITICAL: never write None back into the session
        if current_user and current_user not in (None, "None", ""):
            frappe.set_user(current_user)
        else:
            frappe.set_user("Guest")
        
        
@frappe.whitelist()
def upload_sales_internal_attachments(quote_id, sap, payload):
    try:
        files = json.loads(payload)
        if not files:
            return {"status": "error", "error": "No files provided."}

        # Use ignore_permissions when getting or creating the attachment parent
        if frappe.db.exists("Sales-GLNet Record Attachments", {"quote_id": quote_id}):
            doc = frappe.get_doc("Sales-GLNet Record Attachments", {"quote_id": quote_id})
        else:
            doc = frappe.new_doc("Sales-GLNet Record Attachments")
            doc.quote_id = quote_id
            doc.sap = sap

        for f in files:
            binary_content = base64.b64decode(f['filedata'])
            saved_file = save_file(
                f['filename'], binary_content,
                "Sales-GLNet Record Attachments", doc.name, is_private=0
            )
            doc.append("attachment_list", {
                "part_number": f.get('part_number', ''),
                "corrected_name": f.get('corrected_name', ''),
                "attach": saved_file.file_url
            })

        doc.save(ignore_permissions=True)   # ← Added here
        return {"status": "success", "message": f"{len(files)} files uploaded successfully."}

    except Exception as e:
        frappe.log_error("Sales Panel Internal Upload Error", str(e))
        return {"status": "error", "error": str(e)}


@frappe.whitelist()
def delete_sales_internal_attachment(quote_id, file_url):
    try:
        parents = frappe.get_all("Sales-GLNet Record Attachments", filters={"quote_id": quote_id}, limit=1)
        if not parents:
            return {"status": "error", "error": "Parent record not found."}

        doc = frappe.get_doc("Sales-GLNet Record Attachments", parents[0].name)
        original_len = len(doc.attachment_list)
        doc.attachment_list = [row for row in doc.attachment_list if row.attach != file_url]

        if len(doc.attachment_list) < original_len:
            doc.save(ignore_permissions=True)
            return {"status": "success"}
        return {"status": "error", "error": "File not found in record."}

    except Exception as e:
        frappe.log_error("Sales Panel Delete Attachment Error", str(e))
        return {"status": "error", "error": str(e)}


@frappe.whitelist()
def search_sales_sap(sap_value):
    if not sap_value:
        return {"status": "success", "data": []}

    try:
        rfq_records = frappe.db.sql("""
            SELECT name as ID, customer_ref as CUSTOMER_REF, customer as CUSTOMER,
                   part_number as PART_NUMBER, brand as BRAND, date as DATE, rp as RP
            FROM `tabRequest For Quote`
            WHERE sap = %s
        """, (sap_value,), as_dict=True)

        result = []
        for rfq in rfq_records:
            id_value = rfq['ID']

            quote_records = frappe.db.sql("""
                SELECT ref as REF, sales_price as SALES_PRICE, part_number as PART_NUMBER,
                       brand as BRAND, rfqdate as RFQDATE, rep as REP
                FROM `tabQuote`
                WHERE name = %s
            """, (id_value,), as_dict=True)

            result.append({
                'id': rfq['ID'],
                'customer_ref': rfq['CUSTOMER_REF'],
                'customer': rfq['CUSTOMER'],
                'part_number': rfq['PART_NUMBER'],
                'brand': rfq['BRAND'],
                'date': rfq['DATE'],
                'rp': rfq['RP'],
                'quotes': quote_records,
                'suppliers': [],   # Intentionally hidden for Sales users
                'orders': []
            })

        return {"status": "success", "data": result}

    except Exception as e:
        frappe.log_error("Sales Panel SAP Search Error", str(e))
        return {"status": "error", "error": str(e)}


def _so_row_get(row, *keys):
    """Read a SQL dict value by alias, ignoring key case and skipping blanks."""
    if not row:
        return None
    lowered = {str(k).lower(): v for k, v in row.items()}
    for key in keys:
        val = lowered.get(str(key).lower())
        if val is not None and str(val).strip() != "":
            return val
    return None


def _so_parse_rate(raw):
    """Use a numeric sale/quote price; ignore W / NQ / text statuses."""
    if raw is None:
        return 0
    text = str(raw).strip().replace(",", "")
    if not text:
        return 0
    try:
        return float(text)
    except ValueError:
        return 0


def _require_convert_so_access():
    user = (frappe.session.user or "").strip()
    roles = frappe.get_roles(user)
    if user == "Administrator" or "System Manager" in roles or "Sales T3" in roles:
        return
    frappe.throw("Only Administrator can convert Request And Quote rows to a Sales Order.")


def _so_get_linked_item_code(raq_name):
    """
    Resolve the ERPNext Item already assigned to this RAQ.
    Priority:
      1. GL Item Match.item
      2. Request And Quote.custom_gl_item (if the field exists)
    Returns item_code or None. Does NOT invent TEMP-ITEM.
    """
    raq_name = str(raq_name or "").strip()
    if not raq_name:
        return None

    item_code = None
    if frappe.db.exists("DocType", "GL Item Match"):
        item_code = frappe.db.get_value(
            "GL Item Match",
            {"request_and_quote": raq_name},
            "item",
        )

    if not item_code:
        try:
            if frappe.get_meta("Request And Quote").has_field("custom_gl_item"):
                item_code = frappe.db.get_value("Request And Quote", raq_name, "custom_gl_item")
        except Exception:
            item_code = None

    item_code = (item_code or "").strip()
    if item_code and frappe.db.exists("Item", item_code):
        return item_code
    return None


def _so_set_if_field(doc, fieldname, value):
    if value in (None, ""):
        return
    try:
        if doc.meta.has_field(fieldname):
            doc.set(fieldname, value)
    except Exception:
        pass


@frappe.whitelist()
def preview_convert_to_sales_order(selected_ids):
    """
    Evaluate selected RAQ rows BEFORE creating a Sales Order.
    Returns header (customer/contact) + per-row match status so the
    admin can link or create an Item in the Convert modal.
    Quotation fields are not filled from the original sales columns.
    Does not write anything.
    """
    try:
        _require_convert_so_access()
        id_list = json.loads(selected_ids) if isinstance(selected_ids, str) else (selected_ids or [])
        id_list = [str(x) for x in id_list if x is not None and str(x).strip() != ""]
        if not id_list:
            return {"status": "error", "error": "No records selected."}

        format_strings = ",".join(["%s"] * len(id_list))
        has_custom_gl = False
        try:
            has_custom_gl = bool(frappe.get_meta("Request And Quote").has_field("custom_gl_item"))
        except Exception:
            has_custom_gl = False
        custom_gl_select = "rq.custom_gl_item as custom_gl_item," if has_custom_gl else "NULL as custom_gl_item,"

        rows = frappe.db.sql(f"""
            SELECT
                rq.name as ID,
                rq.ref as REF,
                rq.customer as customer,
                rq.`div` as `div`,
                rq.email_customr as email_customer,
                rq.contact as contact,
                rq.customer_ref as customer_ref,
                rq.sap as sap,
                rq.due_date as due_date,
                rq.item as sales_item,
                rq.qty as sales_qty,
                rq.unit as sales_unit,
                rq.brand as sales_brand,
                rq.part_number as sales_part_number,
                rq.description as sales_description,
                rq.sale_price as sale_price,
                rq.note as sales_note,
                rq.incoterm as sales_incoterm,
                rq.quotation_item as quotation_item,
                rq.quotation_qty as quotation_qty,
                rq.quotation_unit as quotation_unit,
                rq.quotation_brand as quotation_brand,
                rq.quotation_part_number as quotation_part_number,
                rq.quotation_description as quotation_description,
                rq.quotation_incoterm as quotation_incoterm,
                rq.quotation_sales_price as quotation_sales_price,
                rq.quotation_note as quotation_note,
                rq.quotation_co as quotation_co,
                rq.quotation_aprox_weight as quotation_aprox_weight,
                {custom_gl_select}
                rq.country as country
            FROM `tabRequest And Quote` rq
            WHERE rq.name IN ({format_strings})
            ORDER BY FIELD(rq.name, {format_strings})
        """, tuple(id_list) + tuple(id_list), as_dict=True)

        if not rows:
            return {"status": "error", "error": "Could not find matching RFQ data."}

        customers = []
        for r in rows:
            cust = (r.get("customer") or "").strip()
            if cust and cust not in customers:
                customers.append(cust)

        header = rows[0]
        customer = (header.get("customer") or "").strip()
        customer_ok = bool(customer and frappe.db.exists("Customer", customer))

        match_map = {}
        if frappe.db.exists("DocType", "GL Item Match"):
            match_rows = frappe.db.sql(f"""
                SELECT request_and_quote, name, item, match_method, match_status, customer, sap, part_number
                FROM `tabGL Item Match`
                WHERE request_and_quote IN ({format_strings})
            """, tuple(id_list), as_dict=True) or []
            for m in match_rows:
                match_map[str(m.request_and_quote)] = m

        preview_rows = []
        field_errors = []
        ready_count = 0
        missing_count = 0

        if not _so_row_get(header, "due_date"):
            field_errors.append(
                f"RAQ {header.get('ID')}: Due Date is empty, please update before continuing."
            )
        header_incoterm_raw = _so_row_get(header, "quotation_incoterm")
        header_incoterm_code, header_named_place = _so_split_incoterm(header_incoterm_raw)
        if not header_incoterm_raw or not header_incoterm_code:
            field_errors.append(
                f"RAQ {header.get('ID')}: Quotation Incoterm is empty or does not start with a 3-letter code, please update before continuing."
            )

        for r in rows:
            raq_name = str(r.get("ID"))
            linked = _so_get_linked_item_code(raq_name)
            match_row = match_map.get(raq_name)
            item_name = ""
            item_brand = ""
            item_pn = ""
            item_uom = ""
            if linked:
                item_vals = frappe.db.get_value(
                    "Item",
                    linked,
                    ["item_name", "brand", "stock_uom"],
                    as_dict=True,
                ) or {}
                item_name = item_vals.get("item_name") or ""
                item_brand = item_vals.get("brand") or ""
                item_uom = item_vals.get("stock_uom") or ""
                pn_field = None
                try:
                    from my_custom_app.item_management import _item_part_number_field
                    pn_field = _item_part_number_field()
                except Exception:
                    pn_field = None
                if pn_field:
                    item_pn = frappe.db.get_value("Item", linked, pn_field) or ""

            part_number = _so_row_get(r, "quotation_part_number")
            qty_raw = _so_row_get(r, "quotation_qty")
            unit = _so_row_get(r, "quotation_unit")
            brand = _so_row_get(r, "quotation_brand")
            description = _so_row_get(r, "quotation_description")
            item_label = _so_row_get(r, "quotation_item")
            note = _so_row_get(r, "quotation_note")
            rate_raw = _so_row_get(r, "quotation_sales_price")
            incoterm_raw = _so_row_get(r, "quotation_incoterm")
            incoterm_code, named_place = _so_split_incoterm(incoterm_raw)
            row_errors = []

            qty = None
            if qty_raw is None or str(qty_raw).strip() == "":
                row_errors.append(
                    f"RAQ {raq_name}: Quotation Quantity is 0 or empty, please update before continuing."
                )
            else:
                try:
                    qty = float(str(qty_raw).strip().replace(",", ""))
                except ValueError:
                    qty = None
                if qty is None or qty == 0:
                    row_errors.append(
                        f"RAQ {raq_name}: Quotation Quantity is 0 or empty, please update before continuing."
                    )

            if not unit:
                row_errors.append(
                    f"RAQ {raq_name}: Quotation Unit is empty, please update before continuing."
                )
            elif not frappe.db.exists("UOM", unit):
                row_errors.append(
                    f"RAQ {raq_name}: Quotation Unit \"{unit}\" is not a UOM, please update before continuing."
                )

            rate = None
            if rate_raw is None or str(rate_raw).strip() == "":
                row_errors.append(
                    f"RAQ {raq_name}: Quotation Sales Price is 0 or empty, please update before continuing."
                )
            else:
                try:
                    rate = float(str(rate_raw).strip().replace(",", ""))
                except ValueError:
                    rate = None
                if rate is None or rate == 0:
                    row_errors.append(
                        f"RAQ {raq_name}: Quotation Sales Price is 0 or empty, please update before continuing."
                    )

            if not description:
                row_errors.append(
                    f"RAQ {raq_name}: Quotation Description is empty, please update before continuing."
                )
            if not note:
                row_errors.append(
                    f"RAQ {raq_name}: Quotation Note is empty, please update before continuing."
                )
            if not part_number:
                row_errors.append(
                    f"RAQ {raq_name}: Quotation Part Number is empty, please update before continuing."
                )
            if not brand:
                row_errors.append(
                    f"RAQ {raq_name}: Quotation Brand is empty, please update before continuing."
                )
            elif not frappe.db.exists("Brand", brand):
                row_errors.append(
                    f"RAQ {raq_name}: Quotation Brand \"{brand}\" is not a Brand, please update before continuing."
                )
            if not incoterm_raw or not incoterm_code:
                row_errors.append(
                    f"RAQ {raq_name}: Quotation Incoterm is empty or does not start with a 3-letter code, please update before continuing."
                )

            field_errors.extend(row_errors)
            status = "ready" if linked else "missing"
            if status == "ready":
                ready_count += 1
            else:
                missing_count += 1

            preview_rows.append({
                "ID": raq_name,
                "REF": r.get("REF") or "",
                "customer": r.get("customer") or "",
                "contact": r.get("contact") or "",
                "email_customer": r.get("email_customer") or "",
                "sap": r.get("sap") or "",
                "customer_ref": r.get("customer_ref") or "",
                "due_date": r.get("due_date"),
                "item_label": item_label or "",
                "part_number": part_number or "",
                "qty": qty if qty is not None else "",
                "unit": unit or "",
                "brand": brand or "",
                "description": description or "",
                "sale_price": r.get("sale_price") or "",
                "quotation_sales_price": r.get("quotation_sales_price") or "",
                "rate": rate if rate is not None else "",
                "incoterm": incoterm_raw or "",
                "named_place": named_place or "",
                "country_of_origin": r.get("quotation_co") or "",
                "weight": r.get("quotation_aprox_weight") or "",
                "note": note or "",
                "status": status,
                "linked_item": linked or "",
                "linked_item_name": item_name,
                "linked_item_brand": item_brand,
                "linked_item_part_number": item_pn,
                "linked_item_uom": item_uom,
                "match_method": (match_row.get("match_method") if match_row else "") or "",
                "match_status": (match_row.get("match_status") if match_row else "") or "",
                "suggested_item_code": "",
                "suggested_item_name": part_number or item_label or raq_name,
                "field_errors": row_errors,
            })

        return {
            "status": "success",
            "header": {
                "customer": customer,
                "customer_ok": customer_ok,
                "customers": customers,
                "multiple_customers": len(customers) > 1,
                "contact": header.get("contact") or "",
                "email_customer": header.get("email_customer") or "",
                "div": header.get("div") or "",
                "customer_ref": header.get("customer_ref") or "",
                "due_date": header.get("due_date"),
                "incoterm": header_incoterm_raw or "",
                "named_place": header_named_place or "",
            },
            "rows": preview_rows,
            "ready_count": ready_count,
            "missing_count": missing_count,
            "field_errors": field_errors,
            "field_error_count": len(field_errors),
            "can_convert": bool(
                customer_ok
                and len(customers) == 1
                and missing_count == 0
                and ready_count > 0
                and not field_errors
            ),
        }
    except Exception as e:
        frappe.log_error("Preview Convert to SO Error", str(e))
        return {"status": "error", "error": str(e)}

@frappe.whitelist()
def convert_to_sales_order(selected_ids):
    try:
        _require_convert_so_access()
        id_list = json.loads(selected_ids) if isinstance(selected_ids, str) else (selected_ids or [])
        id_list = [str(x) for x in id_list if x is not None and str(x).strip() != ""]
        if not id_list:
            return {"status": "error", "error": "No records selected."}

        format_strings = ",".join(["%s"] * len(id_list))
        rows = frappe.db.sql(f"""
            SELECT
                name as ID,
                customer,
                `div`,
                email_customr as email_customer,
                contact,
                sap,
                customer_ref,
                due_date,
                item,
                qty,
                unit,
                brand,
                part_number,
                description,
                sale_price,
                note,
                incoterm,
                quotation_item,
                quotation_qty,
                quotation_unit,
                quotation_brand,
                quotation_part_number,
                quotation_description,
                quotation_incoterm,
                quotation_sales_price,
                quotation_note,
                quotation_delivery
            FROM `tabRequest And Quote`
            WHERE name IN ({format_strings})
        """, tuple(id_list), as_dict=True)

        records = {str(r.get("ID")): r for r in rows}
        if not records:
            return {"status": "error", "error": "Could not find matching RFQ data."}

        header = records.get(id_list[0])
        if not header:
            return {"status": "error", "error": "Could not find matching RFQ data."}

        customer = _so_row_get(header, "customer")
        if not customer:
            return {"status": "error", "error": "Customer field is empty on the selected records."}

        if not frappe.db.exists("Customer", customer):
            return {
                "status": "error",
                "error": f'Customer "{customer}" is not a valid Customer master record.',
            }

        other_customers = []
        for rec_id in id_list:
            row = records.get(rec_id)
            if not row:
                continue
            other = (_so_row_get(row, "customer") or "").strip()
            if other and other != customer and other not in other_customers:
                other_customers.append(other)
        if other_customers:
            return {
                "status": "error",
                "error": (
                    "Selected rows belong to more than one Customer "
                    f"({customer}, {', '.join(other_customers)}). "
                    "A Sales Order can only be created for a single Customer."
                ),
            }

        unresolved = []
        resolved_items = {}
        for rec_id in id_list:
            item_code = _so_get_linked_item_code(rec_id)
            if not item_code:
                unresolved.append(rec_id)
            else:
                resolved_items[rec_id] = item_code

        if unresolved:
            return {
                "status": "error",
                "error": (
                    "These Request And Quote rows have no ERPNext Item assigned: "
                    + ", ".join(unresolved)
                    + ". Open Convert to Sales Order, link or create an Item for each row, then convert."
                ),
                "unresolved": unresolved,
            }

        missing = []
        prepared_rows = []

        if not frappe.get_meta("Sales Order Item").has_field("custom_current_eta"):
            return {
                "status": "error",
                "error": "Sales Order Item is missing the Date field custom_current_eta.",
            }
        if not frappe.get_meta("Sales Order Item").has_field("custom_customer_ref_number"):
            return {
                "status": "error",
                "error": "Sales Order Item is missing the field custom_customer_ref_number.",
            }
        if not frappe.get_meta("Sales Order Item").has_field("custom_incoterms"):
            return {
                "status": "error",
                "error": "Sales Order Item is missing the Link field custom_incoterms.",
            }
        if not frappe.get_meta("Sales Order Item").has_field("custom_raq"):
            return {
                "status": "error",
                "error": "Sales Order Item is missing the Link field custom_raq.",
            }

        header_incoterm_raw = _so_row_get(header, "quotation_incoterm")
        header_incoterm_code, header_named_place = _so_split_incoterm(header_incoterm_raw)
        if not header_incoterm_raw or not header_incoterm_code:
            missing.append(
                f"RAQ {id_list[0]}: Quotation Incoterm is empty or does not start with a 3-letter code, please update before continuing."
            )
        header_incoterm_name = _so_ensure_incoterm(header_incoterm_code) if header_incoterm_code else None

        for rec_id in id_list:
            row = records.get(rec_id)
            if not row:
                missing.append(f"RAQ {rec_id}: Request And Quote row was not found.")
                continue

            part_number = _so_row_get(row, "quotation_part_number")
            qty_raw = _so_row_get(row, "quotation_qty")
            unit = _so_row_get(row, "quotation_unit")
            brand = _so_row_get(row, "quotation_brand")
            description = _so_row_get(row, "quotation_description")
            note = _so_row_get(row, "quotation_note")
            rate_raw = _so_row_get(row, "quotation_sales_price")
            delivery_raw = _so_row_get(row, "quotation_delivery")
            item_incoterm_raw = _so_row_get(row, "quotation_incoterm")
            item_incoterm_code, item_named_place = _so_split_incoterm(item_incoterm_raw)

            delivery_days = None
            if delivery_raw is None or str(delivery_raw).strip() == "":
                missing.append(
                    f"RAQ {rec_id}: Quotation Delivery is empty, please update before continuing."
                )
            else:
                try:
                    delivery_days = int(float(str(delivery_raw).strip().replace(",", "")))
                except ValueError:
                    delivery_days = None
                if delivery_days is None or delivery_days < 0:
                    missing.append(
                        f"RAQ {rec_id}: Quotation Delivery must be a number of days, please update before continuing."
                    )

            qty = None
            if qty_raw is None or str(qty_raw).strip() == "":
                missing.append(
                    f"RAQ {rec_id}: Quotation Quantity is 0 or empty, please update before continuing."
                )
            else:
                try:
                    qty = float(str(qty_raw).strip().replace(",", ""))
                except ValueError:
                    qty = None
                if qty is None or qty == 0:
                    missing.append(
                        f"RAQ {rec_id}: Quotation Quantity is 0 or empty, please update before continuing."
                    )

            if not unit:
                missing.append(
                    f"RAQ {rec_id}: Quotation Unit is empty, please update before continuing."
                )
            elif not frappe.db.exists("UOM", unit):
                missing.append(
                    f"RAQ {rec_id}: Quotation Unit \"{unit}\" is not a UOM, please update before continuing."
                )

            rate = None
            if rate_raw is None or str(rate_raw).strip() == "":
                missing.append(
                    f"RAQ {rec_id}: Quotation Sales Price is 0 or empty, please update before continuing."
                )
            else:
                try:
                    rate = float(str(rate_raw).strip().replace(",", ""))
                except ValueError:
                    rate = None
                if rate is None or rate == 0:
                    missing.append(
                        f"RAQ {rec_id}: Quotation Sales Price is 0 or empty, please update before continuing."
                    )

            if not description:
                missing.append(
                    f"RAQ {rec_id}: Quotation Description is empty, please update before continuing."
                )
            if not note:
                missing.append(
                    f"RAQ {rec_id}: Quotation Note is empty, please update before continuing."
                )
            if not part_number:
                missing.append(
                    f"RAQ {rec_id}: Quotation Part Number is empty, please update before continuing."
                )
            if not brand:
                missing.append(
                    f"RAQ {rec_id}: Quotation Brand is empty, please update before continuing."
                )
            elif not frappe.db.exists("Brand", brand):
                missing.append(
                    f"RAQ {rec_id}: Quotation Brand \"{brand}\" is not a Brand, please update before continuing."
                )
            incoterm_name = ""
            if not item_incoterm_raw or not item_incoterm_code:
                missing.append(
                    f"RAQ {rec_id}: Quotation Incoterm is empty or does not start with a 3-letter code, please update before continuing."
                )
            else:
                if frappe.get_meta("Incoterm").has_field("code"):
                    incoterm_name = frappe.db.get_value("Incoterm", {"code": item_incoterm_code}, "name") or ""
                if not incoterm_name and frappe.db.exists("Incoterm", item_incoterm_code):
                    incoterm_name = item_incoterm_code
                if not incoterm_name:
                    missing.append(
                        f"RAQ {rec_id}: Quotation Incoterm \"{item_incoterm_code}\" does not match an Incoterm code, please update before continuing."
                    )
            item_code = resolved_items[rec_id]
            customer_ref_rows = frappe.db.sql(
                """
                SELECT ref_code
                FROM `tabItem Customer Detail`
                WHERE parent = %s
                  AND parenttype = 'Item'
                  AND customer_name = %s
                  AND IFNULL(ref_code, '') != ''
                """,
                (item_code, customer),
                as_dict=True,
            ) or []
            customer_ref_codes = []
            for ref_row in customer_ref_rows:
                ref_code = str(ref_row.get("ref_code") or "").strip()
                if ref_code and ref_code not in customer_ref_codes:
                    customer_ref_codes.append(ref_code)
            customer_ref_number = ""
            if not customer_ref_codes:
                missing.append(
                    f"RAQ {rec_id}: Item {item_code} has no Item Customer Detail ref_code for customer {customer}, please update before continuing."
                )
            elif len(customer_ref_codes) > 1:
                missing.append(
                    f"RAQ {rec_id}: Item {item_code} has more than one Item Customer Detail ref_code for customer {customer}, please update before continuing."
                )
            else:
                customer_ref_number = customer_ref_codes[0]
            prepared_rows.append({
                "rec_id": rec_id,
                "item_code": item_code,
                "part_number": part_number,
                "qty": qty,
                "unit": unit,
                "brand": brand,
                "description": description,
                "note": note,
                "rate": rate,
                "named_place": item_named_place,
                "delivery_days": delivery_days,
                "customer_ref_number": customer_ref_number,
                "incoterm_name": incoterm_name,
            })

        if missing:
            return {"status": "error", "error": "<br>".join(missing)}

        today = frappe.utils.today()
        for prepared in prepared_rows:
            prepared["current_eta"] = frappe.utils.add_days(today, prepared["delivery_days"])

        earliest_eta = min(prepared["current_eta"] for prepared in prepared_rows)

        so_doc = frappe.new_doc("Sales Order")
        so_doc.customer = customer
        so_doc.delivery_date = earliest_eta

        contact = _so_row_get(header, "contact")
        if contact and frappe.db.exists("Contact", contact):
            _so_set_if_field(so_doc, "contact_person", contact)
        else:
            primary_contact = frappe.db.get_value("Customer", customer, "customer_primary_contact")
            if primary_contact:
                _so_set_if_field(so_doc, "contact_person", primary_contact)

        primary_address = frappe.db.get_value("Customer", customer, "customer_primary_address")
        if primary_address:
            _so_set_if_field(so_doc, "customer_address", primary_address)

        if header_incoterm_name:
            _so_set_if_field(so_doc, "incoterm", header_incoterm_name)
        if header_named_place:
            _so_set_if_field(so_doc, "custom_named_place", header_named_place)

        items_added = 0
        for prepared in prepared_rows:
            so_description = f"{prepared['description']}\n{prepared['note']}"
            so_item = {
                "item_code": prepared["item_code"],
                "delivery_date": prepared["current_eta"],
                "description": so_description,
                "uom": prepared["unit"],
                "qty": prepared["qty"],
                "rate": prepared["rate"],
            }
            so_doc.append("items", so_item)
            so_child = so_doc.items[-1]
            _so_set_if_field(so_child, "custom_part_number", prepared["part_number"])
            _so_set_if_field(so_child, "brand", prepared["brand"])
            _so_set_if_field(so_child, "custom_named_place", prepared["named_place"])
            _so_set_if_field(so_child, "custom_current_eta", prepared["current_eta"])
            _so_set_if_field(so_child, "custom_customer_ref_number", prepared["customer_ref_number"])
            _so_set_if_field(so_child, "custom_incoterms", prepared["incoterm_name"])
            _so_set_if_field(so_child, "custom_raq", prepared["rec_id"])
            items_added += 1

        if items_added == 0:
            return {"status": "error", "error": "No matching item rows were found for the selected records."}

        so_doc.insert(ignore_permissions=True)
        return {"status": "success", "so_name": so_doc.name}
    except Exception as e:
        frappe.log_error("Sales Panel Convert to SO Error", str(e))
        return {"status": "error", "error": str(e)}

        
@frappe.whitelist()
def upload_doctype_attachments(doctype, docname, payload, fieldname="attachment"):
    try:
        files = json.loads(payload)
        if not files:
            return {"status": "error", "error": "No files provided."}

        doc = frappe.get_doc(doctype, docname)
        
        current_attachments_str = doc.get(fieldname)
        try:
            current_attachments = json.loads(current_attachments_str) if current_attachments_str else []
        except Exception:
            current_attachments = []
            
        if not isinstance(current_attachments, list):
            current_attachments = []

        for f in files:
            binary_content = base64.b64decode(f['filedata'])
            saved_file = save_file(
                f['filename'], binary_content,
                doctype, docname, is_private=0
            )
            
            current_attachments.append({
                "name": saved_file.file_name,
                "usrName": f['filename'],
                "file_url": saved_file.file_url
            })

        doc.db_set(fieldname, json.dumps(current_attachments))
        frappe.db.commit()
        return {"status": "success", "message": f"{len(files)} files uploaded.", "new_attachment_str": json.dumps(current_attachments)}

    except Exception as e:
        frappe.log_error("DocType Attachment Upload Error", str(e))
        return {"status": "error", "error": str(e)}


@frappe.whitelist()
def delete_doctype_attachment(doctype, docname, file_index, fieldname="attachment"):
    try:
        doc = frappe.get_doc(doctype, docname)
        
        current_attachments_str = doc.get(fieldname)
        try:
            current_attachments = json.loads(current_attachments_str) if current_attachments_str else []
        except Exception:
            current_attachments = []

        idx = int(file_index)
        if isinstance(current_attachments, list) and 0 <= idx < len(current_attachments):
            file_to_delete = current_attachments.pop(idx)
            
            if file_to_delete.get("file_url"):
                try:
                    file_doc = frappe.get_all("File", filters={"file_url": file_to_delete["file_url"]}, limit=1)
                    if file_doc:
                        frappe.delete_doc("File", file_doc[0].name, ignore_permissions=True)
                except Exception:
                    pass 
            
            # === FIX: When list is empty, store NULL/empty instead of "[]"
            if len(current_attachments) == 0:
                doc.db_set(fieldname, None)                    # This makes v-if hide the icon
                new_value_to_return = ""
            else:
                new_value = json.dumps(current_attachments)
                doc.db_set(fieldname, new_value)
                new_value_to_return = new_value

            frappe.db.commit()
            return {"status": "success", "new_attachment_str": new_value_to_return}
        else:
            return {"status": "error", "error": "File not found in array."}

    except Exception as e:
        frappe.log_error("Delete DocType Attachment Error", str(e))
        return {"status": "error", "error": str(e)}


@frappe.whitelist()
def get_customer_orders(item_ids):
    try:
        ids = json.loads(item_ids)
        if not ids:
            return {"status": "success", "data": {}}

        format_strings = ','.join(['%s'] * len(ids))
        sql = f"""
            SELECT name as DOC_NAME, id as ID, date as DATE, country as COUNTRY, order_number as ORDER_NUMBER, order_due_date as ORDER_DUE_DATE, attachment as ATTACHMENT, modification as MODIFICATION, attachment_mod as ATTACHMENT_MOD, note_order as NOTE_ORDER, order_price_ea as ORDER_PRICE_EA, qty as QTY
            FROM `tabCustomer Order`
            WHERE id IN ({format_strings})
        """
        rows = frappe.db.sql(sql, tuple(ids), as_dict=True)

        result = {}
        for row in rows:
            item_id = str(row['ID']) 
            if item_id not in result:
                result[item_id] = []
            result[item_id].append(row)

        return {"status": "success", "data": result}
    except Exception as e:
        frappe.log_error("Sales Batching Get Orders Error", str(e))
        return {"status": "error", "error": str(e)}


@frappe.whitelist()
def save_customer_order(payload):
    """Create or update a single Customer Order row. Does not touch sibling order rows."""
    try:
        data = json.loads(payload) if isinstance(payload, str) else (payload or {})
        row_id = data.get("ID")
        if not row_id:
            return {"status": "error", "error": "Row ID is required."}

        field_values = {
            "date": data.get("DATE") or None,
            "country": data.get("COUNTRY") or "",
            "order_number": data.get("ORDER_NUMBER") or "",
            "order_due_date": data.get("ORDER_DUE_DATE") or None,
            "modification": data.get("MODIFICATION") or None,
            "order_price_ea": data.get("ORDER_PRICE_EA") or "",
            "note_order": data.get("NOTE_ORDER") or "",
            "qty": data.get("QTY") or "",
        }

        doc_name = data.get("DOC_NAME") or ""
        is_new = data.get("is_new", False) or not doc_name

        if is_new:
            new_ord = frappe.get_doc({
                "doctype": "Customer Order",
                "id": row_id,
                **field_values
            })
            new_ord.insert(ignore_permissions=True)
            frappe.db.commit()
            return {"status": "success", "doc_name": new_ord.name}

        if not frappe.db.exists("Customer Order", doc_name):
            return {"status": "error", "error": "Customer Order was not found."}

        update_ord_sql = """
            UPDATE `tabCustomer Order`
            SET date=%s, country=%s, order_number=%s, order_due_date=%s,
                modification=%s, order_price_ea=%s, note_order=%s, qty=%s
            WHERE name=%s
        """
        frappe.db.sql(
            update_ord_sql,
            (
                field_values["date"],
                field_values["country"],
                field_values["order_number"],
                field_values["order_due_date"],
                field_values["modification"],
                field_values["order_price_ea"],
                field_values["note_order"],
                field_values["qty"],
                doc_name,
            ),
        )
        frappe.db.commit()
        return {"status": "success", "doc_name": doc_name}

    except Exception as e:
        frappe.log_error("Save Customer Order Error", str(e))
        return {"status": "error", "error": str(e)}


@frappe.whitelist()
def delete_customer_order(doc_name):
    """Permanently delete one Customer Order. Sales T3 / Procurement T3 / Admin only."""
    try:
        doc_name = (doc_name or "").strip()
        if not doc_name:
            return {"status": "error", "error": "Customer Order name is required."}

        if not _user_can_delete_customer_orders():
            return {"status": "error", "error": "Only Sales T3 / Procurement T3 can delete customer orders."}

        if not frappe.db.exists("Customer Order", doc_name):
            return {"status": "success", "message": "Customer Order already removed."}

        frappe.delete_doc("Customer Order", doc_name, ignore_permissions=True, force=1)
        frappe.db.commit()
        return {"status": "success"}

    except Exception as e:
        frappe.log_error("Delete Customer Order Error", str(e))
        return {"status": "error", "error": str(e)}



# ============================================================
# BANNED DATES MANAGEMENT (for upload / due_date enforcement)
# ============================================================

@frappe.whitelist()
def get_banned_dates():
    """Returns all banned dates for the management modal in the custom page."""
    try:
        banned = frappe.get_all(
            "banned_dates_upload",
            fields=["name", "date", "reason", "added_on", "added_by"],
            order_by="date desc"
        )
        return {"status": "success", "data": banned}
    except Exception as e:
        frappe.log_error("Get Banned Dates Error", str(e))
        return {"status": "error", "error": str(e)}


@frappe.whitelist()
def add_banned_date(banned_date, reason=""):
    """Adds a new banned date. Prevents duplicate dates."""
    try:
        if not banned_date:
            return {"status": "error", "error": "Date is required."}

        if frappe.db.exists("banned_dates_upload", {"date": banned_date}):
            return {"status": "error", "error": "This date is already banned."}

        doc = frappe.get_doc({
            "doctype": "banned_dates_upload",
            "date": banned_date,
            "reason": reason or "No reason provided",
            "added_on": frappe.utils.now_datetime(),
            "added_by": frappe.session.user
        })
        doc.insert(ignore_permissions=True)
        return {"status": "success", "message": "Banned date added successfully."}
    except Exception as e:
        frappe.log_error("Add Banned Date Error", str(e))
        return {"status": "error", "error": str(e)}


@frappe.whitelist()
def delete_banned_date(doc_name):
    """Deletes a banned date record."""
    try:
        if not doc_name:
            return {"status": "error", "error": "Document name is required."}
        frappe.delete_doc("banned_dates_upload", doc_name, ignore_permissions=True)
        return {"status": "success"}
    except Exception as e:
        frappe.log_error("Delete Banned Date Error", str(e))
        return {"status": "error", "error": str(e)}


@frappe.whitelist()
def is_date_banned(check_date):
    """Reusable check used by frontend and update methods before allowing a due_date."""
    try:
        if not check_date:
            return {"status": "success", "is_banned": False}
        exists = frappe.db.exists("banned_dates_upload", {"date": check_date})
        return {"status": "success", "is_banned": bool(exists)}
    except Exception as e:
        return {"status": "error", "error": str(e)}


@frappe.whitelist()
def search_customer_contacts(customer, query=""):
    try:
        # Fetch all contacts for this customer to perform smart matching
        all_contacts = frappe.db.sql("""
            SELECT c.name, c.email_id, c.first_name, c.last_name, c.st
            FROM `tabContact` c
            INNER JOIN `tabDynamic Link` dl ON dl.parent = c.name
            WHERE dl.link_doctype = 'Customer' AND dl.link_name = %s
        """, (customer,), as_dict=True)

        # ------------------------------------------------------------------
        # Resolve Contact.st (User Link / e-mail) → 3-digit User.st code
        # so the upload pre-visualization never shows the e-mail address.
        # ------------------------------------------------------------------
        def _resolve_st(user_link):
            if not user_link:
                return ''
            val = str(user_link).strip()
            if val.isdigit() and len(val) == 3:
                return val
            if frappe.db.exists("User", val):
                code = frappe.db.get_value("User", val, "st")
                if code and str(code).isdigit() and len(str(code)) == 3:
                    return str(code)
            try:
                user = frappe.get_doc("User", {"email": val})
                code = user.get("st")
                if code and str(code).isdigit() and len(str(code)) == 3:
                    return str(code)
            except Exception:
                pass
            return ''

        for c in all_contacts:
            c['st'] = _resolve_st(c.get('st'))

        if not query:
            return {"status": "success", "data": all_contacts[:15]}

        query_lower = query.lower()
        exact_matches = []
        suggested_matches = []

        for c in all_contacts:
            full_name = f"{c.first_name or ''} {c.last_name or ''}".strip().lower()
            email = (c.email_id or '').lower()
            
            # Exact/Substring Match
            if query_lower in full_name or query_lower in email:
                c['match_type'] = 'exact'
                exact_matches.append(c)
            else:
                # Fuzzy Match for suggestions (60% similarity threshold)
                name_ratio = difflib.SequenceMatcher(None, query_lower, full_name).ratio()
                email_ratio = difflib.SequenceMatcher(None, query_lower, email).ratio()
                if name_ratio > 0.6 or email_ratio > 0.6:
                    c['match_type'] = 'suggested'
                    suggested_matches.append(c)

        # Combine results, prioritizing exact matches
        results = exact_matches + suggested_matches
        return {"status": "success", "data": results[:15]}
    except Exception as e:
        return {"status": "error", "error": str(e)}

@frappe.whitelist()
def create_customer_contact(first_name, last_name, email_id, customer):
    try:
        if not first_name:
            return {"status": "error", "error": "First name is required."}
        
        contact = frappe.get_doc({
            "doctype": "Contact",
            "first_name": first_name,
            "last_name": last_name or "",
            "is_primary_contact": 1,
            "links": [{"link_doctype": "Customer", "link_name": customer}]
        })
        
        if email_id:
            contact.append("email_ids", {"email_id": email_id, "is_primary": 1})
            
        contact.insert(ignore_permissions=True)
        
        return {
            "status": "success", 
            "data": {
                "name": contact.name,
                "first_name": contact.first_name,
                "last_name": contact.last_name,
                "email_id": email_id
            }
        }
    except Exception as e:
        frappe.log_error("Create Customer Contact Error", str(e))
        return {"status": "error", "error": str(e)}
        

@frappe.whitelist()
def update_customer_contact(contact_id, first_name, last_name, email_id):
    try:
        if not contact_id:
            return {"status": "error", "error": "Contact ID is required for editing."}
        
        contact = frappe.get_doc("Contact", contact_id)
        if first_name:
            contact.first_name = first_name
        if last_name is not None:
            contact.last_name = last_name
            
        if email_id:
            found = False
            for row in contact.email_ids:
                if row.is_primary:
                    row.email_id = email_id
                    found = True
                    break
            if not found:
                contact.append("email_ids", {"email_id": email_id, "is_primary": 1})
        
        contact.save(ignore_permissions=True)
        
        return {
            "status": "success", 
            "data": {
                "name": contact.name,
                "first_name": contact.first_name,
                "last_name": contact.last_name,
                "email_id": email_id
            }
        }
    except Exception as e:
        frappe.log_error("Update Customer Contact Error", str(e))
        return {"status": "error", "error": str(e)}

# ========== SBM BACKEND METHODS ==========
@frappe.whitelist()
def get_sbm_supplier_details(supplier_name):
    try:
        sup = frappe.get_doc("Supplier", supplier_name)
        
        # Get Available Addresses via Dynamic Link
        addr_links = frappe.get_all("Dynamic Link", filters={"link_doctype": "Supplier", "link_name": supplier_name, "parenttype": "Address"}, fields=["parent"])
        available_addresses = []
        for al in addr_links:
            try:
                addr = frappe.get_doc("Address", al.parent)
                available_addresses.append({
                    "name": addr.name, "address_title": addr.address_title, "city": addr.city,
                    "address_type": addr.address_type, "address_line1": addr.address_line1,
                    "country": addr.country, "pincode": addr.pincode
                })
            except Exception:
                pass

        # Get Available Contacts via Dynamic Link
        cont_links = frappe.get_all("Dynamic Link", filters={"link_doctype": "Supplier", "link_name": supplier_name, "parenttype": "Contact"}, fields=["parent"])
        available_contacts = []
        for cl in cont_links:
            try:
                cnt = frappe.get_doc("Contact", cl.parent)
                available_contacts.append({
                    "name": cnt.name, "first_name": cnt.first_name, "last_name": cnt.last_name,
                    "email_id": cnt.email_id, "phone": cnt.phone
                })
            except Exception:
                pass
        # Build Relationship Hierarchy
        brand_relationships = []
        try:
            rels = frappe.get_all("Supplier Brand Relationship", filters={"supplier": supplier_name}, fields=["*"])
            
            for rel in rels:
                r_data = dict(rel)
                
                # Fetch Division Details
                if r_data.get('division'):
                    try:
                        div_doc = frappe.get_doc("brand_division_glgnet", r_data['division'])
                        r_data['_div_display'] = div_doc.div_name or div_doc.name
                        r_data['division_details'] = {
                            "div_name": div_doc.div_name, "pn_example_1": div_doc.pn_example_1, 
                            "pn_example_2": div_doc.pn_example_2, "notes": div_doc.notes
                        }
                        
                        # Fetch Responsible Reps linked to this division
                        resp_docs = frappe.get_all("Brand Division Responsible", filters={"brand_division": r_data['division']}, fields=["name"])
                        if resp_docs:
                            resp = frappe.get_doc("Brand Division Responsible", resp_docs[0].name)
                            r_data['responsible_details'] = {
                                "responsible_reps": [{"user": rep.user, "priority": rep.priority} for rep in resp.get("responsible_reps", [])]
                            }
                        else:
                            r_data['responsible_details'] = { "responsible_reps": [] }
                    except Exception:
                        pass
                brand_relationships.append(r_data)
        except Exception as e:
            # If the DocTypes don't exist yet, catch the error silently so the profile still loads
            pass

        current_tags = []
        try:
            current_tags = frappe.get_all(
                "Tag Link",
                filters={"document_type": "Supplier", "document_name": supplier_name},
                pluck="tag"
            )
        except Exception:
            pass

        data = {
            "name": sup.name,
            "supplier_name": sup.supplier_name,
            "supplier_primary_address": sup.supplier_primary_address,
            "supplier_primary_contact": sup.supplier_primary_contact,
            "custom_default_cc": sup.get("custom_default_cc"),
            "supplier_details": sup.get("supplier_details") or "",
            "website": sup.get("website") or "",
            "tags": current_tags,
            "available_addresses": available_addresses,
            "available_contacts": available_contacts,
            "brand_relationships": brand_relationships
        }
        return {"status": "success", "data": data}  
    except Exception as e:
        frappe.log_error("Get SBM Details Error", str(e))
        return {"status": "error", "error": str(e)}

@frappe.whitelist()
def update_sbm_general_details(payload):
    try:
        data = json.loads(payload)
        sup = frappe.get_doc("Supplier", data.get("supplier_name"))
        sup.supplier_primary_address = data.get("supplier_primary_address")
        sup.supplier_primary_contact = data.get("supplier_primary_contact")
        sup.custom_default_cc = data.get("custom_default_cc")
        if "supplier_details" in data:
            sup.supplier_details = data.get("supplier_details")
        if "website" in data:
            sup.website = data.get("website") or ""
        sup.save(ignore_permissions=True)
        return {"status": "success"}
    except Exception as e:
        return {"status": "error", "error": str(e)}

@frappe.whitelist()
def create_sbm_address(supplier_name, payload):
    try:
        data = json.loads(payload)
        addr = frappe.get_doc({
            "doctype": "Address",
            "address_title": data.get("address_title"),
            "address_type": data.get("address_type"),
            "address_line1": data.get("address_line1"),
            "city": data.get("city"),
            "country": data.get("country"),
            "pincode": data.get("pincode"),
            "links": [{"link_doctype": "Supplier", "link_name": supplier_name}]
        })
        addr.insert(ignore_permissions=True)
        
        # Auto-set as primary if none exists
        sup = frappe.get_doc("Supplier", supplier_name)
        if not sup.supplier_primary_address:
            sup.supplier_primary_address = addr.name
            sup.save(ignore_permissions=True)
            
        return {"status": "success"}
    except Exception as e:
        frappe.log_error("SBM Address Create Error", str(e))
        return {"status": "error", "error": str(e)}

@frappe.whitelist()
def create_sbm_contact(supplier_name, payload):
    try:
        data = json.loads(payload)
        contact = frappe.get_doc({
            "doctype": "Contact",
            "first_name": data.get("first_name"),
            "last_name": data.get("last_name"),
            "links": [{"link_doctype": "Supplier", "link_name": supplier_name}]
        })
        if data.get("email_id"):
            contact.append("email_ids", {"email_id": data.get("email_id"), "is_primary": 1})
            
        contact.insert(ignore_permissions=True)
        
        # Auto-set as primary if none exists
        sup = frappe.get_doc("Supplier", supplier_name)
        if not sup.supplier_primary_contact:
            sup.supplier_primary_contact = contact.name
            sup.save(ignore_permissions=True)
            
        return {"status": "success"}
    except Exception as e:
        frappe.log_error("SBM Contact Create Error", str(e))
        return {"status": "error", "error": str(e)}

@frappe.whitelist()
def update_sbm_address(payload):
    try:
        data = json.loads(payload)
        if not data.get("name"):
            return {"status": "error", "error": "Address Name is required for update."}
            
        addr = frappe.get_doc("Address", data.get("name"))
        addr.address_title = data.get("address_title")
        addr.address_type = data.get("address_type")
        addr.address_line1 = data.get("address_line1")
        addr.city = data.get("city")
        addr.country = data.get("country")
        addr.pincode = data.get("pincode")
        addr.save(ignore_permissions=True)
            
        return {"status": "success"}
    except Exception as e:
        frappe.log_error("SBM Address Update Error", str(e))
        return {"status": "error", "error": str(e)}

@frappe.whitelist()
def update_sbm_contact(payload):
    try:
        data = json.loads(payload)
        if not data.get("name"):
            return {"status": "error", "error": "Contact Name is required for update."}
            
        contact = frappe.get_doc("Contact", data.get("name"))
        contact.first_name = data.get("first_name")
        contact.last_name = data.get("last_name")
        
        if data.get("email_id"):
            found = False
            for row in contact.email_ids:
                if row.is_primary:
                    row.email_id = data.get("email_id")
                    found = True
                    break
            if not found:
                contact.append("email_ids", {"email_id": data.get("email_id"), "is_primary": 1})
                
        contact.save(ignore_permissions=True)
            
        return {"status": "success"}
    except Exception as e:
        frappe.log_error("SBM Contact Update Error", str(e))
        return {"status": "error", "error": str(e)}

@frappe.whitelist()
def delete_sbm_supplier(supplier_name):
    """
    Permanently delete a Supplier.
    Procurement T3 / Administrator / System Manager only.
    """
    try:
        supplier_name = (supplier_name or "").strip()
        if not supplier_name:
            return {"status": "error", "error": "Supplier is required."}

        roles = frappe.get_roles(frappe.session.user)
        is_admin = frappe.session.user == "Administrator" or "System Manager" in roles
        if not is_admin and "Procurement T3" not in roles:
            return {"status": "error", "error": "Only Procurement T3 can delete suppliers."}

        if not frappe.db.exists("Supplier", supplier_name):
            return {"status": "error", "error": "Supplier not found."}

        # Clearing the Link first prevents Frappe from trying to delete the
        # primary Contact under the current user's (T3) permissions.
        frappe.db.set_value(
            "Supplier",
            supplier_name,
            "supplier_primary_contact",
            None,
            update_modified=False,
        )

        contact_names = frappe.db.sql(
            """
            SELECT DISTINCT parent
            FROM `tabDynamic Link`
            WHERE parenttype = 'Contact'
              AND link_doctype = 'Supplier'
              AND link_name = %s
            """,
            (supplier_name,),
            as_list=True,
        )
        for row in contact_names:
            cname = row[0] if row else None
            if not cname:
                continue
            try:
                frappe.delete_doc("Contact", cname, ignore_permissions=True, force=1)
            except Exception as ce:
                frappe.log_error("Delete SBM Supplier Contact Error", f"{cname}: {str(ce)}")

        rel_names = frappe.get_all(
            "Supplier Brand Relationship",
            filters={"supplier": supplier_name},
            pluck="name",
        )
        for rel_name in rel_names:
            try:
                frappe.delete_doc(
                    "Supplier Brand Relationship",
                    rel_name,
                    ignore_permissions=True,
                    force=1,
                )
            except Exception as re:
                frappe.log_error("Delete SBM Supplier Rel Error", f"{rel_name}: {str(re)}")

        frappe.delete_doc("Supplier", supplier_name, ignore_permissions=True, force=1)
        frappe.db.commit()
        return {"status": "success"}
    except Exception as e:
        frappe.log_error("Delete SBM Supplier Error", str(e))
        return {"status": "error", "error": str(e)}


@frappe.whitelist()
def delete_sbm_contact(supplier_name, contact_name):
    """
    Permanently delete a Contact that is linked to the given Supplier.
    Procurement T3 / Administrator / System Manager only.
    """
    try:
        supplier_name = (supplier_name or "").strip()
        contact_name = (contact_name or "").strip()
        if not supplier_name or not contact_name:
            return {"status": "error", "error": "Supplier and Contact are required."}

        roles = frappe.get_roles(frappe.session.user)
        is_admin = frappe.session.user == "Administrator" or "System Manager" in roles
        if not is_admin and "Procurement T3" not in roles:
            return {"status": "error", "error": "Only Procurement T3 can delete supplier contacts."}

        if not frappe.db.exists("Contact", contact_name):
            return {"status": "error", "error": "Contact not found."}

        linked = frappe.db.exists(
            "Dynamic Link",
            {
                "parent": contact_name,
                "parenttype": "Contact",
                "link_doctype": "Supplier",
                "link_name": supplier_name,
            },
        )
        if not linked:
            return {"status": "error", "error": "This contact is not linked to the selected supplier."}

        sup = frappe.get_doc("Supplier", supplier_name)
        if sup.supplier_primary_contact == contact_name:
            sup.supplier_primary_contact = None
            sup.save(ignore_permissions=True)

        frappe.delete_doc("Contact", contact_name, ignore_permissions=True)
        frappe.db.commit()
        return {"status": "success"}
    except Exception as e:
        frappe.log_error("Delete SBM Contact Error", str(e))
        return {"status": "error", "error": str(e)}
        
@frappe.whitelist()
def save_sbm_relationship(payload):
    try:
        data = json.loads(payload)
        
        if data.get("is_new"):
            doc = frappe.new_doc("Supplier Brand Relationship")
            doc.supplier = data.get("supplier")
        else:
            doc = frappe.get_doc("Supplier Brand Relationship", data.get("name"))

        doc.brand = data.get("brand")
        
        # Smart resolution: if the user typed the human-readable name instead of the hash
        div_input = data.get("division")
        if div_input and not frappe.db.exists("brand_division_glgnet", div_input):
            real_div = frappe.db.get_value("brand_division_glgnet", {"div_name": div_input, "brand": doc.brand}, "name")
            if real_div:
                div_input = real_div

        doc.division = div_input
        doc.type = data.get("type")
        doc.notes = data.get("notes")

        # Bypass Frappe's internal cross-doctype validations that throw the mismatch error
        doc.flags.ignore_validate = True

        doc.save(ignore_permissions=True)

        # --- UPDATE NESTED DIVISION DETAILS ---
        div_details = data.get("division_details")
        if div_details and div_input and frappe.db.exists("brand_division_glgnet", div_input):
            div_doc = frappe.get_doc("brand_division_glgnet", div_input)
            div_doc.div_name = div_details.get("div_name")
            div_doc.pn_example_1 = div_details.get("pn_example_1")
            div_doc.pn_example_2 = div_details.get("pn_example_2")
            div_doc.notes = div_details.get("notes")
            div_doc.save(ignore_permissions=True)

        # --- UPDATE NESTED BRAND DIVISION RESPONSIBLE ---
        resp_details = data.get("responsible_details")
        if resp_details and div_input:
            resp_docs = frappe.get_all("Brand Division Responsible", filters={"brand_division": div_input}, fields=["name"])
            if resp_docs:
                resp_doc = frappe.get_doc("Brand Division Responsible", resp_docs[0].name)
            else:
                resp_doc = frappe.new_doc("Brand Division Responsible")
                resp_doc.brand_division = div_input
                resp_doc.brand = data.get("brand")

            # Clear and rebuild child table
            resp_doc.set("responsible_reps", [])
            for rep in resp_details.get("responsible_reps", []):
                if rep.get("user"):
                    resp_doc.append("responsible_reps", {
                        "user": rep.get("user"),
                        "priority": rep.get("priority")
                    })
            resp_doc.flags.ignore_validate = True
            resp_doc.save(ignore_permissions=True)

        return {"status": "success"}
    except Exception as e:
        frappe.log_error("SBM Relationship Save Error", str(e))
        return {"status": "error", "error": str(e)}

@frappe.whitelist()
def delete_sbm_relationship(name):
    try:
        frappe.delete_doc("Supplier Brand Relationship", name, ignore_permissions=True)
        return {"status": "success"}
    except Exception as e:
        frappe.log_error("SBM Relationship Delete Error", str(e))
        return {"status": "error", "error": str(e)}

@frappe.whitelist()
def get_sbm_divisions():
    try:
        divs = frappe.get_all("brand_division_glgnet", fields=["name", "brand", "div_name"])
        return {"status": "success", "data": divs}
    except Exception as e:
        return {"status": "error", "error": str(e)}

@frappe.whitelist()
def create_sbm_division(payload):
    try:
        data = json.loads(payload)
        
        doc = frappe.get_doc({
            "doctype": "brand_division_glgnet",
            "brand": data.get("brand"),
            "div_name": data.get("div_name"),
            "pn_example_1": data.get("pn_example_1"),
            "pn_example_2": data.get("pn_example_2"),
            "notes": data.get("notes")
        })
        doc.insert(ignore_permissions=True)
        return {"status": "success", "data": {"name": doc.name, "brand": doc.brand, "div_name": doc.div_name}}
    except Exception as e:
        frappe.log_error("SBM Division Create Error", str(e))
        return {"status": "error", "error": str(e)}

@frappe.whitelist()
def get_sbm_brand_list():
    """
    Union of Brand names + brands used on brand_division_glgnet
    + brands used on Supplier Brand Relationship.
    Includes division / relationship counts for the left panel.
    """
    try:
        names = set()

        try:
            for n in frappe.get_all("Brand", pluck="name", limit=0) or []:
                if n:
                    names.add(n)
        except Exception:
            pass

        try:
            for row in frappe.get_all("brand_division_glgnet", fields=["brand"], limit=0) or []:
                if row.get("brand"):
                    names.add(row.get("brand"))
        except Exception:
            pass

        try:
            for row in frappe.get_all("Supplier Brand Relationship", fields=["brand"], limit=0) or []:
                if row.get("brand"):
                    names.add(row.get("brand"))
        except Exception:
            pass

        div_counts = {}
        try:
            for row in frappe.get_all("brand_division_glgnet", fields=["brand"], limit=0) or []:
                b = row.get("brand")
                if b:
                    div_counts[b] = div_counts.get(b, 0) + 1
        except Exception:
            pass

        rel_counts = {}
        try:
            for row in frappe.get_all("Supplier Brand Relationship", fields=["brand"], limit=0) or []:
                b = row.get("brand")
                if b:
                    rel_counts[b] = rel_counts.get(b, 0) + 1
        except Exception:
            pass

        data = []
        for n in sorted(names, key=lambda x: str(x).lower()):
            data.append({
                "name": n,
                "division_count": div_counts.get(n, 0),
                "supplier_link_count": rel_counts.get(n, 0)
            })

        return {"status": "success", "data": data}
    except Exception as e:
        frappe.log_error("Get SBM Brand List Error", str(e))
        return {"status": "error", "error": str(e)}


@frappe.whitelist()
def get_sbm_brand_details(brand_name):
    """
    Inverse of get_sbm_supplier_details:
    - all brand_division_glgnet rows for the brand
    - all Supplier Brand Relationship rows for the brand, grouped under those divisions
    - orphan relationships (no division / unknown division) in a virtual bucket
    """
    try:
        brand_name = (brand_name or "").strip()
        if not brand_name:
            return {"status": "error", "error": "Brand name is required."}

        divisions = frappe.get_all(
            "brand_division_glgnet",
            filters={"brand": brand_name},
            fields=["name", "brand", "div_name", "pn_example_1", "pn_example_2", "notes",
                    "modified", "modified_by", "creation", "owner"],
            order_by="div_name asc",
            limit=0
        ) or []

        rels = frappe.get_all(
            "Supplier Brand Relationship",
            filters={"brand": brand_name},
            fields=["name", "supplier", "brand", "division", "type", "notes",
                    "modified", "modified_by", "creation", "owner"],
            limit=0
        ) or []

        supplier_ids = list({r.supplier for r in rels if r.get("supplier")})
        supplier_map = {}
        contacts_by_supplier = {}
        if supplier_ids:
            format_strings = ",".join(["%s"] * len(supplier_ids))
            raw_sups = frappe.db.sql(f"""
                SELECT
                    s.name as name,
                    s.supplier_name as supplier_name,
                    s.supplier_primary_contact as primary_contact,
                    s.custom_default_cc as default_cc,
                    IFNULL(
                        c.email_id,
                        (SELECT email_id FROM `tabContact Email`
                         WHERE parent = c.name
                         ORDER BY is_primary DESC LIMIT 1)
                    ) as email,
                    IFNULL(
                        pa.country,
                        (
                            SELECT a.country
                            FROM `tabAddress` a
                            INNER JOIN `tabDynamic Link` adl
                                ON adl.parent = a.name
                               AND adl.parenttype = 'Address'
                               AND adl.link_doctype = 'Supplier'
                               AND adl.link_name = s.name
                            WHERE IFNULL(a.country, '') != ''
                            ORDER BY IFNULL(a.is_primary_address, 0) DESC
                            LIMIT 1
                        )
                    ) as country
                FROM `tabSupplier` s
                LEFT JOIN `tabContact` c ON c.name = s.supplier_primary_contact
                LEFT JOIN `tabAddress` pa ON pa.name = s.supplier_primary_address
                WHERE s.name IN ({format_strings})
            """, tuple(supplier_ids), as_dict=True)
            for s in raw_sups:
                supplier_map[s.name] = s

            contact_rows = frappe.db.sql(f"""
                SELECT
                    dl.link_name as supplier,
                    c.name as contact_name,
                    c.first_name,
                    c.last_name,
                    c.is_primary_contact,
                    IFNULL(
                        c.email_id,
                        (SELECT email_id FROM `tabContact Email`
                         WHERE parent = c.name
                         ORDER BY is_primary DESC LIMIT 1)
                    ) as email_id
                FROM `tabContact` c
                INNER JOIN `tabDynamic Link` dl
                    ON dl.parent = c.name
                   AND dl.parenttype = 'Contact'
                   AND dl.link_doctype = 'Supplier'
                WHERE dl.link_name IN ({format_strings})
                ORDER BY c.is_primary_contact DESC, c.first_name ASC
            """, tuple(supplier_ids), as_dict=True)

            for row in contact_rows:
                contacts_by_supplier.setdefault(row.supplier, []).append({
                    "name": row.contact_name,
                    "first_name": row.first_name or "",
                    "last_name": row.last_name or "",
                    "email_id": row.email_id or "",
                    "is_primary_contact": int(row.is_primary_contact or 0)
                })

        def enrich_rel(rel):
            info = supplier_map.get(rel.supplier) or {}
            return {
                "name": rel.name,
                "supplier": rel.supplier,
                "supplier_name": info.get("supplier_name") or rel.supplier,
                "email": info.get("email") or "",
                "default_cc": info.get("default_cc") or "",
                "country": info.get("country") or "",
                "contacts": contacts_by_supplier.get(rel.supplier, []),
                "brand": rel.brand,
                "division": rel.division,
                "type": rel.type or "OTHER",
                "notes": rel.notes or "",
                "modified": rel.modified,
                "modified_by": rel.modified_by,
                "creation": rel.creation,
                "owner": rel.owner
            }

        rels_by_div = {}
        orphan_rels = []
        known_div_ids = {d.name for d in divisions}

        for rel in rels:
            div_id = (rel.division or "").strip()
            if not div_id or div_id not in known_div_ids:
                orphan_rels.append(enrich_rel(rel))
            else:
                rels_by_div.setdefault(div_id, []).append(enrich_rel(rel))

        output_divs = []
        for d in divisions:
            output_divs.append({
                "name": d.name,
                "brand": d.brand,
                "div_name": d.div_name or d.name,
                "pn_example_1": d.pn_example_1 or "",
                "pn_example_2": d.pn_example_2 or "",
                "notes": d.notes or "",
                "modified": d.modified,
                "modified_by": d.modified_by,
                "creation": d.creation,
                "owner": d.owner,
                "is_virtual": False,
                "suppliers": rels_by_div.get(d.name, [])
            })

        if orphan_rels:
            output_divs.append({
                "name": "",
                "brand": brand_name,
                "div_name": "General / No Division",
                "pn_example_1": "",
                "pn_example_2": "",
                "notes": "",
                "is_virtual": True,
                "suppliers": orphan_rels
            })

        return {
            "status": "success",
            "data": {
                "brand": brand_name,
                "divisions": output_divs
            }
        }
    except Exception as e:
        frappe.log_error("Get SBM Brand Details Error", str(e))
        return {"status": "error", "error": str(e)}


@frappe.whitelist()
def update_sbm_division(payload):
    """Save editable fields on an existing brand_division_glgnet record."""
    try:
        data = json.loads(payload)
        name = (data.get("name") or "").strip()
        if not name:
            return {"status": "error", "error": "Division name is required."}
        if not frappe.db.exists("brand_division_glgnet", name):
            return {"status": "error", "error": f'Division "{name}" was not found.'}

        doc = frappe.get_doc("brand_division_glgnet", name)
        if "div_name" in data:
            doc.div_name = data.get("div_name")
        if "pn_example_1" in data:
            doc.pn_example_1 = data.get("pn_example_1")
        if "pn_example_2" in data:
            doc.pn_example_2 = data.get("pn_example_2")
        if "notes" in data:
            doc.notes = data.get("notes")
        doc.save(ignore_permissions=True)
        return {"status": "success"}
    except Exception as e:
        frappe.log_error("Update SBM Division Error", str(e))
        return {"status": "error", "error": str(e)}

@frappe.whitelist()
def rename_sbm_brand(old_name, new_name):
    """
    Rename a Brand document and keep SBM tables in sync.
    Brand.name is the brand string used on brand_division_glgnet.brand
    and Supplier Brand Relationship.brand.
    """
    try:
        old_name = (old_name or "").strip()
        new_name = (new_name or "").strip()
        if not old_name or not new_name:
            return {"status": "error", "error": "Both the current name and the new name are required."}
        if old_name == new_name:
            return {"status": "success", "name": new_name}

        if frappe.db.exists("Brand", new_name) and new_name != old_name:
            return {"status": "error", "error": f'A brand named "{new_name}" already exists.'}

        if frappe.db.exists("Brand", old_name):
            frappe.rename_doc("Brand", old_name, new_name, force=True, ignore_permissions=True)
        else:
            # Brand string existed only on SBM tables — create the target Brand if needed
            if not frappe.db.exists("Brand", new_name):
                frappe.get_doc({"doctype": "Brand", "brand": new_name}).insert(ignore_permissions=True)

        # Keep SBM tables aligned even if Link cascade did not catch a custom field
        frappe.db.sql(
            "UPDATE `tabbrand_division_glgnet` SET brand = %s WHERE brand = %s",
            (new_name, old_name)
        )
        frappe.db.sql(
            "UPDATE `tabSupplier Brand Relationship` SET brand = %s WHERE brand = %s",
            (new_name, old_name)
        )

        try:
            frappe.db.sql(
                "UPDATE `tabBrand Division Responsible` SET brand = %s WHERE brand = %s",
                (new_name, old_name)
            )
        except Exception:
            pass

        frappe.db.commit()
        return {"status": "success", "name": new_name}
    except Exception as e:
        frappe.log_error("Rename SBM Brand Error", str(e))
        return {"status": "error", "error": str(e)}


@frappe.whitelist()
def delete_sbm_brand(brand_name):
    """
    Delete a Brand and every SBM record that belongs to it:
    - Supplier Brand Relationship rows for this brand
    - Brand Division Responsible rows for its divisions
    - brand_division_glgnet rows for this brand
    - the Brand document itself
    """
    try:
        brand_name = (brand_name or "").strip()
        if not brand_name:
            return {"status": "error", "error": "Brand name is required."}

        rels = frappe.get_all(
            "Supplier Brand Relationship",
            filters={"brand": brand_name},
            pluck="name"
        ) or []
        for rel_name in rels:
            frappe.delete_doc("Supplier Brand Relationship", rel_name, ignore_permissions=True, force=True)

        divs = frappe.get_all(
            "brand_division_glgnet",
            filters={"brand": brand_name},
            pluck="name"
        ) or []
        for div_name in divs:
            resp_docs = frappe.get_all(
                "Brand Division Responsible",
                filters={"brand_division": div_name},
                pluck="name"
            ) or []
            for resp_name in resp_docs:
                frappe.delete_doc("Brand Division Responsible", resp_name, ignore_permissions=True, force=True)
            frappe.delete_doc("brand_division_glgnet", div_name, ignore_permissions=True, force=True)

        if frappe.db.exists("Brand", brand_name):
            frappe.delete_doc("Brand", brand_name, ignore_permissions=True, force=True)

        frappe.db.commit()
        return {"status": "success"}
    except Exception as e:
        frappe.log_error("Delete SBM Brand Error", str(e))
        return {"status": "error", "error": str(e)}


@frappe.whitelist()
def delete_sbm_division(division_name):
    """
    Delete one brand_division_glgnet record.
    Supplier Brand Relationship rows that pointed at it keep the supplier-brand
    link; their division field is cleared so they appear under General / No Division.

    Allowed: Procurement T2, Procurement T3, Administrator, System Manager.

    Uses SQL for unlink / fallback deletes so a broken Supplier Link
    (e.g. supp_mock_a on Brand Division Responsible) cannot abort the delete.
    """
    try:
        roles = frappe.get_roles(frappe.session.user)
        is_admin = frappe.session.user == "Administrator" or "System Manager" in roles
        if not is_admin and "Procurement T3" not in roles and "Procurement T2" not in roles:
            return {"status": "error", "error": "Only Procurement T2 / T3 can delete divisions."}

        division_name = (division_name or "").strip()
        if not division_name:
            return {"status": "error", "error": "Division name is required."}
        if not frappe.db.exists("brand_division_glgnet", division_name):
            return {"status": "error", "error": f'Division "{division_name}" was not found.'}

        # Unlink relationships without loading/validating the Supplier Link
        if frappe.db.exists("DocType", "Supplier Brand Relationship"):
            frappe.db.sql(
                """
                UPDATE `tabSupplier Brand Relationship`
                SET division = NULL
                WHERE division = %s
                """,
                (division_name,),
            )

        # Remove Brand Division Responsible rows (and their child tables)
        if frappe.db.exists("DocType", "Brand Division Responsible"):
            resp_docs = frappe.get_all(
                "Brand Division Responsible",
                filters={"brand_division": division_name},
                pluck="name"
            ) or []
            for resp_name in resp_docs:
                try:
                    frappe.delete_doc(
                        "Brand Division Responsible",
                        resp_name,
                        ignore_permissions=True,
                        force=True
                    )
                except Exception:
                    try:
                        meta = frappe.get_meta("Brand Division Responsible")
                        for df in meta.get_table_fields():
                            child_dt = df.options
                            if child_dt:
                                frappe.db.sql(
                                    f"DELETE FROM `tab{child_dt}` WHERE parent = %s",
                                    (resp_name,),
                                )
                    except Exception:
                        pass
                    frappe.db.sql(
                        "DELETE FROM `tabBrand Division Responsible` WHERE name = %s",
                        (resp_name,),
                    )

        # Delete the division itself
        try:
            frappe.delete_doc(
                "brand_division_glgnet",
                division_name,
                ignore_permissions=True,
                force=True
            )
        except Exception:
            frappe.db.sql(
                "DELETE FROM `tabbrand_division_glgnet` WHERE name = %s",
                (division_name,),
            )

        frappe.db.commit()
        return {"status": "success"}
    except Exception as e:
        frappe.log_error("Delete SBM Division Error", str(e))
        return {"status": "error", "error": str(e)}

        

@frappe.whitelist()
def get_sbm_brand_selection_data(target_brand):
    try:
        target_clean = target_brand.lower().strip()
        rels = frappe.get_all("Supplier Brand Relationship", fields=["name", "supplier", "brand", "division"])
        
        matched_rels = []
        for r in rels:
            b_clean = (r.brand or '').lower().strip()
            if target_clean in b_clean:
                matched_rels.append(r)
            else:
                import re
                escaped = re.escape(b_clean)
                if re.search(r'\b' + escaped + r'\b', target_clean, re.IGNORECASE):
                    matched_rels.append(r)

        grouped_data = {}
        for r in matched_rels:
            b_name = r.brand
            if b_name not in grouped_data:
                grouped_data[b_name] = {"brand": b_name, "divisions": {}}
            
            div_id = r.division or 'General'
            if div_id not in grouped_data[b_name]["divisions"]:
                div_info = {
                    "div_name": "General / No Division",
                    "pn_example_1": "",
                    "pn_example_2": "",
                    "notes": "",
                    "match_conditions": [],
                    "suppliers": []
                }
                if div_id != 'General':
                    try:
                        div_doc = frappe.get_doc("brand_division_glgnet", div_id)
                        div_info["div_name"] = div_doc.div_name or div_doc.name
                        div_info["pn_example_1"] = div_doc.pn_example_1 or ""
                        div_info["pn_example_2"] = div_doc.pn_example_2 or ""
                        div_info["notes"] = div_doc.notes or ""
                    except Exception:
                        pass
                    try:
                        if frappe.db.exists("DocType", "Brand Division Match Condition"):
                            rules = frappe.get_all(
                                "Brand Division Match Condition",
                                filters={"brand_division": div_id, "disabled": 0},
                                fields=[
                                    "name",
                                    "field_to_match",
                                    "match_condition",
                                    "example_value",
                                    "min_length",
                                    "priority",
                                    "notes"
                                ],
                                order_by="priority desc",
                                limit_page_length=1000,
                            ) or []
                            div_info["match_conditions"] = rules
                    except Exception:
                        div_info["match_conditions"] = []
                grouped_data[b_name]["divisions"][div_id] = div_info
            
            sup_name = r.supplier
            sup_details = frappe.db.get_value("Supplier", sup_name, ["supplier_name", "custom_default_cc", "supplier_primary_contact", "supplier_primary_address"], as_dict=True)
            
            email = ""
            if sup_details and sup_details.get("supplier_primary_contact"):
                email = frappe.db.get_value("Contact Email", {"parent": sup_details.get("supplier_primary_contact"), "is_primary": 1}, "email_id") or ""

            country = ""
            try:
                primary_addr = (sup_details.get("supplier_primary_address") if sup_details else None) or ""
                if primary_addr:
                    country = (frappe.db.get_value("Address", primary_addr, "country") or "").strip()
                if not country:
                    linked_country = frappe.db.sql("""
                        SELECT a.country
                        FROM `tabAddress` a
                        INNER JOIN `tabDynamic Link` adl
                            ON adl.parent = a.name
                           AND adl.parenttype = 'Address'
                           AND adl.link_doctype = 'Supplier'
                           AND adl.link_name = %s
                        WHERE IFNULL(a.country, '') != ''
                        ORDER BY IFNULL(a.is_primary_address, 0) DESC
                        LIMIT 1
                    """, (sup_name,))
                    if linked_country and linked_country[0] and linked_country[0][0]:
                        country = str(linked_country[0][0]).strip()
            except Exception:
                country = ""
            
            div_suppliers = grouped_data[b_name]["divisions"][div_id]["suppliers"]
            if not any(s['name'] == sup_name for s in div_suppliers):
                div_suppliers.append({
                    "name": sup_name,
                    "email": email,
                    "default_cc": sup_details.get("custom_default_cc") if sup_details else "",
                    "brands": [b_name],
                    "country": country
                })

        output = []
        for b_name, b_data in grouped_data.items():
            div_list = []
            for d_id, d_data in b_data["divisions"].items():
                if d_data["suppliers"]:
                    div_list.append(d_data)
            if div_list:
                output.append({"brand": b_name, "divisions": div_list})
        
        output.sort(key=lambda x: x["brand"].lower())
        return {"status": "success", "data": output}
    except Exception as e:
        frappe.log_error("SBM Brand Selection Error", str(e))
        return {"status": "error", "error": str(e)}   
        


@frappe.whitelist()
def get_supplier_tags_for_sbm():
    """Return only Tags that are marked for the Supplier doctype."""
    try:
        tags = frappe.get_all(
            "Tag",
            filters={"custom_for_doctype": "Supplier"},
            fields=["name"],
            order_by="name asc"
        )
        return {"status": "success", "data": [t.name for t in tags]}
    except Exception as e:
        frappe.log_error("Get Supplier Tags Error", str(e))
        return {"status": "error", "error": str(e)}


# ========== END SBM BACKEND METHODS ==========



@frappe.whitelist()
def add_tag_to_supplier(supplier_name, tag):
    """Attach an existing Tag to a Supplier (no creation of new tags)."""
    try:
        if not supplier_name or not tag:
            return {"status": "error", "error": "Supplier and Tag are required."}
        # Safety: only allow tags that are intended for Supplier
        if not frappe.db.exists("Tag", {"name": tag, "custom_for_doctype": "Supplier"}):
            return {"status": "error", "error": "Tag is not allowed for Supplier."}
        from frappe.desk.doctype.tag.tag import add_tag
        add_tag(tag, "Supplier", supplier_name)
        return {"status": "success"}
    except Exception as e:
        frappe.log_error("Add Tag to Supplier Error", str(e))
        return {"status": "error", "error": str(e)}


@frappe.whitelist()
def remove_tag_from_supplier(supplier_name, tag):
    """Detach a Tag from a Supplier."""
    try:
        if not supplier_name or not tag:
            return {"status": "error", "error": "Supplier and Tag are required."}
        from frappe.desk.doctype.tag.tag import remove_tag
        remove_tag(tag, "Supplier", supplier_name)
        return {"status": "success"}
    except Exception as e:
        frappe.log_error("Remove Tag from Supplier Error", str(e))
        return {"status": "error", "error": str(e)}


@frappe.whitelist()
def get_sbm_relationship_history(rel_name):
    """Return Version history for a Supplier Brand Relationship (creation → now)."""
    try:
        if not rel_name:
            return {"status": "error", "error": "Relationship name required."}
        versions = frappe.get_all(
            "Version",
            filters={"ref_doctype": "Supplier Brand Relationship", "docname": rel_name},
            fields=["name", "creation", "owner", "data"],
            order_by="creation desc",
            limit=50
        )
        # Parse the JSON diff into a lighter structure for the UI
        history = []
        for v in versions:
            try:
                changes = json.loads(v.data) if v.data else {}
                changed_fields = list(changes.get("changed", {}).keys()) if isinstance(changes.get("changed"), dict) else []
                history.append({
                    "version": v.name,
                    "creation": v.creation,
                    "owner": v.owner,
                    "changed_fields": changed_fields
                })
            except Exception:
                history.append({
                    "version": v.name,
                    "creation": v.creation,
                    "owner": v.owner,
                    "changed_fields": []
                })
        return {"status": "success", "data": history}
    except Exception as e:
        frappe.log_error("SBM Relationship History Error", str(e))
        return {"status": "error", "error": str(e)}





@frappe.whitelist()
def get_raq_activity_history(docname):
    """
    TEMPORARY DIAGNOSTIC VERSION
    Returns normal history + raw Version data so we can inspect the structure.
    """
    try:
        if not docname:
            return {"status": "error", "error": "Document name is required."}

        # 1. Creation info
        doc_info = frappe.db.get_value(
            "Request And Quote",
            docname,
            ["owner", "creation"],
            as_dict=True
        )

        history = []
        raw_versions_debug = []

        if doc_info:
            history.append({
                "version": "creation",
                "creation": doc_info.creation,
                "owner": doc_info.owner or "Unknown",
                "field": "Created",
                "old_value": "",
                "new_value": "Document created"
            })

        # 2. Load Versions
        versions = frappe.get_all(
            "Version",
            filters={
                "ref_doctype": "Request And Quote",
                "docname": str(docname)
            },
            fields=["name", "creation", "owner", "data"],
            order_by="creation desc",
            limit=200
        )

        for v in versions:
            # Keep the raw data for diagnosis
            raw_versions_debug.append({
                "version_name": v.name,
                "creation": str(v.creation),
                "owner": v.owner,
                "raw_data": v.data
            })

            # Try the normal parsing (same as before)
            try:
                changes = json.loads(v.data) if v.data else {}
            except Exception:
                changes = {}

            changed = changes.get("changed") or []
            parsed_any = False

            if isinstance(changed, list):
                for item in changed:
                    if isinstance(item, (list, tuple)) and len(item) >= 3:
                        field, old_val, new_val = item[0], item[1], item[2]
                    elif isinstance(item, (list, tuple)) and len(item) == 2:
                        field, old_val, new_val = item[0], "", item[1]
                    else:
                        continue

                    history.append({
                        "version": v.name,
                        "creation": v.creation,
                        "owner": v.owner,
                        "field": field,
                        "old_value": "" if old_val is None else str(old_val),
                        "new_value": "" if new_val is None else str(new_val)
                    })
                    parsed_any = True

            elif isinstance(changed, dict):
                for field, vals in changed.items():
                    if isinstance(vals, (list, tuple)) and len(vals) >= 2:
                        old_val, new_val = vals[0], vals[1]
                    else:
                        old_val, new_val = "", vals

                    history.append({
                        "version": v.name,
                        "creation": v.creation,
                        "owner": v.owner,
                        "field": field,
                        "old_value": "" if old_val is None else str(old_val),
                        "new_value": "" if new_val is None else str(new_val)
                    })
                    parsed_any = True

            if not parsed_any:
                history.append({
                    "version": v.name,
                    "creation": v.creation,
                    "owner": v.owner,
                    "field": "Updated",
                    "old_value": "",
                    "new_value": "Document updated (details not available)"
                })

        history.sort(key=lambda x: str(x.get("creation") or ""))

        # Return both the normal history AND the raw debug data
        return {
            "status": "success",
            "data": history,
            "debug_versions": raw_versions_debug   # ← this is the important part
        }

    except Exception as e:
        frappe.log_error("RAQ Activity History Error", str(e))
        return {"status": "error", "error": str(e)}
        
        

@frappe.whitelist()
def clone_request_and_quote_records(records_json):
    """
    Creates new Request And Quote documents by cloning selected rows.
    - due_date, quotation_sales_price and rp are intentionally left empty (or set only if the user provided a new due_date).
    - Each record carries its own 'reason' which is prepended to both description and quotation_description as **reason** original_text.
    - Optional per-row attachment is saved into the quotation_attachment field of the NEW document only.
    """
    try:
        records = json.loads(records_json)
        if not records:
            return {"status": "error", "error": "No records provided for cloning."}

        created_count = 0
        errors = []

        for idx, row in enumerate(records):
            try:
                # --- Banned-date enforcement (server-side safety net) ---
                due_date = row.get("DUE_DATE")
                if due_date:
                    exists = frappe.db.exists("banned_dates_upload", {"date": due_date})
                    if exists:
                        errors.append(f"Row {idx + 1}: Due date {due_date} is banned.")
                        continue

                # --- Build the new document ---
                new_doc = frappe.new_doc("Request And Quote")
                
                
                # REF: use exactly what the user left in the clone modal.
                # Do NOT re-apply Customer.custom_customer_category — the letter
                # is already inside the source REF (e.g. C312-xxxxx).
                # Z-Check may still prepend a single "Z" if it is not already there.
                # ------------------------------------------------------------------
                original_ref = str(row.get("REF") or "").strip()
                final_ref = original_ref

                z_checked = bool(row.get("Z_CHECK") or row.get("z_check") or row.get("is_z"))
                if z_checked and final_ref.upper()[:1] != "Z":
                    final_ref = "Z" + final_ref
                # ------------------------------------------------------------------

                # Header / common fields
                new_doc.ref = final_ref
                new_doc.due_date = due_date or None
                new_doc.due_time = row.get("DUE_TIME") or None
                new_doc.sap = row.get("SAP") or ""
                new_doc.st = row.get("ST") or ""
                new_doc.customer = row.get("CUSTOMER") or ""
                new_doc.div = row.get("DIV") or ""
                new_doc.contact = row.get("CONTACT") or ""
                new_doc.email_customr = row.get("EMAIL_CUSTOMER") or ""
                new_doc.customer_ref = row.get("CUSTOMER_REF") or ""
                new_doc.date = row.get("DATE") or None
                new_doc.country = row.get("COUNTRY") or ""
                new_doc.custom_uploaded_by = _current_user_st_code()
                new_doc.custom_sales_status = _apply_submitted_by(
                    new_doc,
                    row.get("CUSTOM_SALES_STATUS") or ""
                )

                # quotation_sales_price stays empty on clone
                new_doc.quotation_sales_price = ""

                # RP + Sale Price come from the clone modal (user-editable)
                rp_val = row.get("RP") if row.get("RP") not in (None, "") else row.get("REP")
                new_doc.rep = rp_val or None
                new_doc.rp = rp_val or None
                new_doc.sale_price = row.get("SALE_PRICE") or ""

                # Quantity typed in the clone modal wins for BOTH qty fields
                cloned_qty = row.get("QTY")
                if cloned_qty in (None, ""):
                    cloned_qty = row.get("ORIG_QTY") or ""

                # Request-side item fields
                new_doc.item = row.get("ORIG_ITEM") or row.get("ITEM") or ""
                new_doc.qty = cloned_qty
                new_doc.unit = row.get("UNIT") or row.get("ORIG_UNIT") or ""
                new_doc.brand = row.get("ORIG_BRAND") or row.get("BRAND") or ""
                new_doc.part_number = row.get("ORIG_PART_NUMBER") or row.get("PART_NUMBER") or ""
                new_doc.note = row.get("ORIG_NOTE") or row.get("NOTE") or ""

                # Quotation-side item fields
                new_doc.quotation_item = row.get("ITEM") or ""
                new_doc.quotation_qty = cloned_qty
                new_doc.quotation_unit = row.get("UNIT") or ""
                new_doc.quotation_brand = row.get("BRAND") or ""
                new_doc.quotation_part_number = row.get("PART_NUMBER") or ""
                new_doc.quotation_co = row.get("CO") or ""
                new_doc.quotation_aprox_weight = row.get("APROX_WEIGHT") or ""
                new_doc.quotation_incoterm = row.get("INCOTERM") or ""
                new_doc.quotation_delivery = row.get("DELIVERY") or ""
                new_doc.quotation_note = row.get("NOTE") or ""

                # Description handling – use the per-item reason
                item_reason = (row.get("reason") or "").strip()
                if not item_reason:
                    errors.append(f"Row {idx + 1}: Reason is mandatory.")
                    continue

                edited_desc = (row.get("DESCRIPTION") or "").strip()
                if not edited_desc:
                    edited_desc = row.get("ORIG_DESCRIPTION") or row.get("QUOTE_DESCRIPTION") or ""

                new_doc.description = f"**{item_reason}** {edited_desc}".strip()
                new_doc.quotation_description = f"**{item_reason}** {edited_desc}".strip()
                
                # ============================================================
                # Force correct sequential naming (identical logic to save_data)
                # Reason: AUTO_INCREMENT is returning None on this doctype,
                # so Frappe is not using MariaDB auto-increment.
                # We manually calculate the next ID to continue from the
                # current highest numeric name.
                # ============================================================

                max_id_result = frappe.db.sql("""
                    SELECT MAX(CAST(name AS UNSIGNED)) as max_id 
                    FROM `tabRequest And Quote`
                """, as_dict=True)

                current_max = max_id_result[0].max_id if max_id_result and max_id_result[0].max_id else 0
                next_id = current_max + 1

                # Force the name and mark it so it doesn't get overwritten
                new_doc.name = str(next_id)
                new_doc.flags.name_set = True          # Prevents Frappe from overriding the name

                # Insert (now with the correct high ID)
                new_doc.insert(ignore_permissions=True)

                # --- Optional attachment (saved only on the NEW document) ---
                att_name = row.get("attachment_filename")
                att_b64 = row.get("attachment_base64")
                if att_name and att_b64:
                    try:
                        binary = base64.b64decode(att_b64)
                        saved = save_file(
                            att_name,
                            binary,
                            "Request And Quote",
                            new_doc.name,
                            is_private=0
                        )
                        # Store in the same JSON array format used by upload_doctype_attachments
                        # IMPORTANT: write to the "attachment" field (Opp Attach column),
                        # NOT to "quotation_attachment" (Quote Attach column)
                        attachment_json = json.dumps([{
                            "name": saved.file_name,
                            "usrName": att_name,
                            "file_url": saved.file_url
                        }])
                        new_doc.db_set("attachment", attachment_json)
                    except Exception as att_err:
                        frappe.log_error("Clone Attachment Error", str(att_err))
                        # Do not fail the whole clone because of an attachment problem

                created_count += 1

            except Exception as row_err:
                frappe.log_error("Clone Single Row Error", str(row_err))
                errors.append(f"Row {idx + 1}: {str(row_err)}")

        frappe.db.commit()

        if created_count == 0 and errors:
            return {"status": "error", "error": "No records could be created. " + " | ".join(errors)}

        result = {"status": "success", "created": created_count}
        if errors:
            result["warnings"] = errors
        return result

    except Exception as e:
        frappe.log_error("Clone Request And Quote Error", str(e))
        return {"status": "error", "error": str(e)}


# ========== CUSTOMER MANAGEMENT BACKEND METHODS ==========
@frappe.whitelist()
def get_local_customers():
    try:
        raw_customers = frappe.db.sql("""
            SELECT
                c.name as name, c.customer_name, c.customer_primary_contact,
                IFNULL(ct.email_id, (SELECT email_id FROM `tabContact Email` WHERE parent = ct.name ORDER BY is_primary DESC LIMIT 1)) as email
            FROM `tabCustomer` c
            LEFT JOIN `tabDynamic Link` dl ON dl.link_name = c.name AND dl.link_doctype = 'Customer' AND dl.parenttype = 'Contact'
            LEFT JOIN `tabContact` ct ON ct.name = dl.parent
            WHERE c.disabled = 0
        """, as_dict=True)

        unique_custs = {}
        for row in raw_customers:
            cust_name = row.get('name')
            human_name = row.get('customer_name')
            contact_name = row.get('customer_primary_contact')
            email = row.get('email')
            score = 0
            if email: score += 10
            if contact_name: score += 5

            if cust_name not in unique_custs:
                unique_custs[cust_name] = {
                    'name': cust_name,
                    'customer_name': human_name,
                    'contact': contact_name,
                    'email': email,
                    'score': score
                }
            else:
                if score > unique_custs[cust_name]['score']:
                    unique_custs[cust_name].update({'contact': contact_name, 'email': email, 'score': score})

        return {"status": "success", "data": list(unique_custs.values())}
    except Exception as e:
        frappe.log_error("Get Local Customers Error", str(e))
        return {"status": "error", "error": str(e)}
        



@frappe.whitelist()
def create_cm_customer(payload):
    """Create a new Customer from the Customer Management modal."""
    try:
        data = json.loads(payload)
        customer_name = (data.get("customer_name") or "").strip()
        customer_group = (data.get("customer_group") or "").strip()

        if not customer_name:
            return {"status": "error", "error": "Customer Name is required."}
        if not customer_group:
            return {"status": "error", "error": "Customer Group is required."}

        # Prevent duplicates by customer_name
        if frappe.db.exists("Customer", {"customer_name": customer_name}):
            return {"status": "error", "error": f'A customer with the name "{customer_name}" already exists.'}

        doc = frappe.get_doc({
            "doctype": "Customer",
            "customer_name": customer_name,
            "customer_group": customer_group,
            "customer_details": data.get("customer_details") or ""
        })

        # Optional custom fields – only set them if they exist on the doctype
        if data.get("custom_customer_category"):
            if hasattr(doc, "custom_customer_category"):
                doc.custom_customer_category = data.get("custom_customer_category")

        if data.get("custom_customer_account_number"):
            if hasattr(doc, "custom_customer_account_number"):
                doc.custom_customer_account_number = data.get("custom_customer_account_number")

        doc.insert(ignore_permissions=True)
        frappe.db.commit()

        return {
            "status": "success",
            "data": {
                "name": doc.name,
                "customer_name": doc.customer_name
            }
        }
    except Exception as e:
        frappe.log_error("Create CM Customer Error", str(e))
        return {"status": "error", "error": str(e)}


@frappe.whitelist()
def get_cm_customer_form_options():
    """
    Return lists of Customer Groups and Customer Categories for the New Customer modal.
    Uses ignore_permissions so any Sales user can see the options.
    """
    try:
        # Customer Groups (standard)
        groups = frappe.get_all(
            "Customer Group",
            filters={"is_group": 0},
            fields=["name"],
            order_by="name asc",
            ignore_permissions=True
        )

        # Customer Categories – try to read the options of the custom Select field
        categories = []
        payment_terms = []
        try:
            meta = frappe.get_meta("Customer")
            field = meta.get_field("custom_customer_category")
            if field and field.options:
                categories = [o.strip() for o in field.options.split("\n") if o.strip()]

            pt_field = meta.get_field("custom_customer_payment_terms")
            if pt_field and pt_field.options:
                payment_terms = [o.strip() for o in pt_field.options.split("\n") if o.strip()]
        except Exception:
            # Fallback for categories only
            try:
                cats = frappe.get_all(
                    "Customer Category",
                    fields=["name"],
                    order_by="name asc",
                    ignore_permissions=True
                )
                categories = [c.name for c in cats]
            except Exception:
                categories = []

        return {
            "status": "success",
            "customer_groups": [g.name for g in groups],
            "customer_categories": categories,
            "customer_payment_terms": payment_terms
        }
    except Exception as e:
        frappe.log_error("Get CM Customer Form Options Error", str(e))
        return {
            "status": "error",
            "error": str(e),
            "customer_groups": [],
            "customer_categories": []
        }


@frappe.whitelist()
def get_cm_customer_details(customer_name):
    try:
        cust = frappe.get_doc("Customer", customer_name)

        # Available Addresses via Dynamic Link (force full result set + unique parents)
        addr_links = frappe.get_all(
            "Dynamic Link",
            filters={"link_doctype": "Customer", "link_name": customer_name, "parenttype": "Address"},
            fields=["parent"],
            limit_page_length=0
        )
        seen_addr = set()
        available_addresses = []
        for al in addr_links:
            parent = al.parent
            if parent in seen_addr:
                continue
            seen_addr.add(parent)
            try:
                addr = frappe.get_doc("Address", parent)
                available_addresses.append({
                    "name": addr.name,
                    "address_title": addr.address_title,
                    "city": addr.city,
                    "address_type": addr.address_type,
                    "address_line1": addr.address_line1,
                    "country": addr.country,
                    "pincode": addr.pincode,
                    "st": getattr(addr, "st", None) or ""
                })
            except Exception:
                pass

        # Available Contacts via Dynamic Link (force full result set + unique parents + include st)
        cont_links = frappe.get_all(
            "Dynamic Link",
            filters={"link_doctype": "Customer", "link_name": customer_name, "parenttype": "Contact"},
            fields=["parent"],
            limit_page_length=0
        )
        seen_cont = set()
        available_contacts = []
        for cl in cont_links:
            parent = cl.parent
            if parent in seen_cont:
                continue
            seen_cont.add(parent)
            try:
                cnt = frappe.get_doc("Contact", parent)
                available_contacts.append({
                    "name": cnt.name,
                    "first_name": cnt.first_name,
                    "last_name": cnt.last_name,
                    "email_id": cnt.email_id,
                    "phone": cnt.phone,
                    "st": getattr(cnt, "st", None) or ""          # Link field to User
                })
            except Exception:
                pass

        current_tags = []
        try:
            current_tags = frappe.get_all(
                "Tag Link",
                filters={"document_type": "Customer", "document_name": customer_name},
                pluck="tag"
            )
        except Exception:
            pass

        data = {
            "name": cust.name,
            "customer_name": cust.customer_name,
            "customer_primary_address": cust.customer_primary_address,
            "customer_primary_contact": cust.customer_primary_contact,
            "customer_details": cust.get("customer_details") or "",
            "custom_customer_category": cust.get("custom_customer_category") or "",
            "custom_customer_payment_terms": cust.get("custom_customer_payment_terms") or "",
            "custom_current_strategies": cust.get("custom_current_strategies") or "",
            "st": cust.get("st") or "",
            "tags": current_tags,
            "available_addresses": available_addresses,
            "available_contacts": available_contacts
        }
        return {"status": "success", "data": data}
    except Exception as e:
        frappe.log_error("Get CM Customer Details Error", str(e))
        return {"status": "error", "error": str(e)}


@frappe.whitelist()
def get_customer_payment_terms(customer_name):
    """Return custom_customer_payment_terms for a Customer, ignoring permissions."""
    try:
        if not customer_name:
            return {"status": "success", "payment_terms": ""}
        value = frappe.db.get_value("Customer", customer_name, "custom_customer_payment_terms") or ""
        return {"status": "success", "payment_terms": value}
    except Exception as e:
        frappe.log_error("Get Customer Payment Terms Error", str(e))
        return {"status": "error", "error": str(e), "payment_terms": ""}    


@frappe.whitelist()
def update_cm_general_details(payload):
    try:
        data = json.loads(payload)
        cust = frappe.get_doc("Customer", data.get("customer_name"))
        cust.customer_primary_address = data.get("customer_primary_address")
        cust.customer_primary_contact = data.get("customer_primary_contact")
        if "customer_details" in data:
            cust.customer_details = data.get("customer_details")
        # Persist the category Select field
        if "custom_customer_category" in data:
            cust.custom_customer_category = data.get("custom_customer_category") or ""
        # Persist payment terms Select field
        if "custom_customer_payment_terms" in data:
            cust.custom_customer_payment_terms = data.get("custom_customer_payment_terms") or ""
        # Persist current strategies (free-text field on Customer)
        if "custom_current_strategies" in data:
            cust.custom_current_strategies = data.get("custom_current_strategies") or ""
        # Persist Customer.st (Data field)
        if "st" in data:
            cust.st = data.get("st") or ""
        cust.save(ignore_permissions=True)

        # Also persist any Contact.st (User) changes that were made in the CM window
        contacts_st = data.get("contacts_st") or []
        for item in contacts_st:
            contact_name = item.get("name")
            new_st = item.get("st") or ""
            if not contact_name:
                continue
            try:
                cdoc = frappe.get_doc("Contact", contact_name)
                # Only write if the value actually changed
                if (getattr(cdoc, "st", None) or "") != new_st:
                    cdoc.st = new_st
                    cdoc.save(ignore_permissions=True)
            except Exception as ce:
                frappe.log_error("CM Contact ST Update Error", f"{contact_name}: {str(ce)}")

        return {"status": "success"}
    except Exception as e:
        frappe.log_error("Update CM General Details Error", str(e))
        return {"status": "error", "error": str(e)}


@frappe.whitelist()
def create_cm_address(customer_name, payload):
    try:
        data = json.loads(payload)
        addr = frappe.get_doc({
            "doctype": "Address",
            "address_title": data.get("address_title"),
            "address_type": data.get("address_type"),
            "address_line1": data.get("address_line1"),
            "city": data.get("city"),
            "country": data.get("country"),
            "pincode": data.get("pincode"),
            "st": data.get("st") or "",
            "links": [{"link_doctype": "Customer", "link_name": customer_name}]
        })
        addr.insert(ignore_permissions=True)

        # Auto-set as primary if none exists
        cust = frappe.get_doc("Customer", customer_name)
        if not cust.customer_primary_address:
            cust.customer_primary_address = addr.name
            cust.save(ignore_permissions=True)

        return {"status": "success"}
    except Exception as e:
        frappe.log_error("CM Address Create Error", str(e))
        return {"status": "error", "error": str(e)}


@frappe.whitelist()
def update_cm_address(payload):
    try:
        data = json.loads(payload)
        if not data.get("name"):
            return {"status": "error", "error": "Address Name is required for update."}

        addr = frappe.get_doc("Address", data.get("name"))
        addr.address_title = data.get("address_title")
        addr.address_type = data.get("address_type")
        addr.address_line1 = data.get("address_line1")
        addr.city = data.get("city")
        addr.country = data.get("country")
        addr.pincode = data.get("pincode")
        addr.st = data.get("st") or ""
        addr.save(ignore_permissions=True)

        return {"status": "success"}
    except Exception as e:
        frappe.log_error("CM Address Update Error", str(e))
        return {"status": "error", "error": str(e)}


@frappe.whitelist()
def create_cm_contact(customer_name, payload):
    try:
        data = json.loads(payload)
        contact = frappe.get_doc({
            "doctype": "Contact",
            "first_name": data.get("first_name"),
            "last_name": data.get("last_name"),
            "links": [{"link_doctype": "Customer", "link_name": customer_name}]
        })
        if data.get("email_id"):
            contact.append("email_ids", {"email_id": data.get("email_id"), "is_primary": 1})

        contact.insert(ignore_permissions=True)

        # Auto-set as primary if none exists
        cust = frappe.get_doc("Customer", customer_name)
        if not cust.customer_primary_contact:
            cust.customer_primary_contact = contact.name
            cust.save(ignore_permissions=True)

        return {"status": "success"}
    except Exception as e:
        frappe.log_error("CM Contact Create Error", str(e))
        return {"status": "error", "error": str(e)}


@frappe.whitelist()
def update_cm_contact(payload):
    try:
        data = json.loads(payload)
        if not data.get("name"):
            return {"status": "error", "error": "Contact Name is required for update."}

        contact = frappe.get_doc("Contact", data.get("name"))
        contact.first_name = data.get("first_name")
        contact.last_name = data.get("last_name")

        if data.get("email_id"):
            found = False
            for row in contact.email_ids:
                if row.is_primary:
                    row.email_id = data.get("email_id")
                    found = True
                    break
            if not found:
                contact.append("email_ids", {"email_id": data.get("email_id"), "is_primary": 1})

        contact.save(ignore_permissions=True)

        return {"status": "success"}
    except Exception as e:
        frappe.log_error("CM Contact Update Error", str(e))
        return {"status": "error", "error": str(e)}

@frappe.whitelist()
def delete_cm_contact(customer_name, contact_name):
    """
    Permanently delete a Contact that is linked to the given Customer.
    Sales T3 / Administrator / System Manager only.
    """
    try:
        customer_name = (customer_name or "").strip()
        contact_name = (contact_name or "").strip()
        if not customer_name or not contact_name:
            return {"status": "error", "error": "Customer and Contact are required."}

        roles = frappe.get_roles(frappe.session.user)
        is_admin = frappe.session.user == "Administrator" or "System Manager" in roles
        if not is_admin and "Sales T3" not in roles:
            return {"status": "error", "error": "Only Sales T3 can delete customer contacts."}

        if not frappe.db.exists("Contact", contact_name):
            return {"status": "error", "error": "Contact not found."}

        linked = frappe.db.exists(
            "Dynamic Link",
            {
                "parent": contact_name,
                "parenttype": "Contact",
                "link_doctype": "Customer",
                "link_name": customer_name,
            },
        )
        if not linked:
            return {"status": "error", "error": "This contact is not linked to the selected customer."}

        cust = frappe.get_doc("Customer", customer_name)
        if cust.customer_primary_contact == contact_name:
            cust.customer_primary_contact = None
            cust.save(ignore_permissions=True)

        frappe.delete_doc("Contact", contact_name, ignore_permissions=True)
        frappe.db.commit()
        return {"status": "success"}
    except Exception as e:
        frappe.log_error("Delete CM Contact Error", str(e))
        return {"status": "error", "error": str(e)}


@frappe.whitelist()
def get_customer_tags_for_cm():
    """Return only Tags that are marked for the Customer doctype."""
    try:
        tags = frappe.get_all(
            "Tag",
            filters={"custom_for_doctype": "Customer"},
            fields=["name"],
            order_by="name asc"
        )
        return {"status": "success", "data": [t.name for t in tags]}
    except Exception as e:
        frappe.log_error("Get Customer Tags Error", str(e))
        return {"status": "error", "error": str(e)}


@frappe.whitelist()
def add_tag_to_customer(customer_name, tag):
    """Attach an existing Tag to a Customer (no creation of new tags)."""
    try:
        if not customer_name or not tag:
            return {"status": "error", "error": "Customer and Tag are required."}
        if not frappe.db.exists("Tag", {"name": tag, "custom_for_doctype": "Customer"}):
            return {"status": "error", "error": "Tag is not allowed for Customer."}
        from frappe.desk.doctype.tag.tag import add_tag
        add_tag(tag, "Customer", customer_name)
        return {"status": "success"}
    except Exception as e:
        frappe.log_error("Add Tag to Customer Error", str(e))
        return {"status": "error", "error": str(e)}


@frappe.whitelist()
def remove_tag_from_customer(customer_name, tag):
    """Detach a Tag from a Customer."""
    try:
        if not customer_name or not tag:
            return {"status": "error", "error": "Customer and Tag are required."}
        from frappe.desk.doctype.tag.tag import remove_tag
        remove_tag(tag, "Customer", customer_name)
        return {"status": "success"}
    except Exception as e:
        frappe.log_error("Remove Tag from Customer Error", str(e))
        return {"status": "error", "error": str(e)}


@frappe.whitelist()
def get_active_users_for_st():
    """Return only enabled Users that have Sales T1, Sales T2 or Sales T3."""
    try:
        # Get all User names that hold at least one of the three Sales tiers
        sales_user_names = frappe.db.sql("""
            SELECT DISTINCT parent
            FROM `tabHas Role`
            WHERE role IN ('Sales T1', 'Sales T2', 'Sales T3')
              AND parenttype = 'User'
        """, as_dict=False)

        if not sales_user_names:
            return {"status": "success", "data": []}

        # Flatten the list of tuples
        name_list = [row[0] for row in sales_user_names]

        users = frappe.get_all(
            "User",
            filters={
                "name": ["in", name_list],
                "enabled": 1,
                "user_type": "System User"
            },
            fields=["name", "full_name"],
            order_by="full_name asc",
            limit_page_length=0
        )

        # Fallback to name when full_name is empty
        for u in users:
            if not u.get("full_name"):
                u["full_name"] = u["name"]

        return {"status": "success", "data": users}
    except Exception as e:
        frappe.log_error("Get Active Users for ST Error", str(e))
        return {"status": "error", "error": str(e)}

@frappe.whitelist()
def get_current_user_roles():
    """Return the list of roles of the currently logged-in user."""
    try:
        roles = frappe.get_roles(frappe.session.user)
        return {"status": "success", "roles": roles}
    except Exception as e:
        frappe.log_error("Get Current User Roles Error", str(e))
        return {"status": "error", "error": str(e), "roles": []}


@frappe.whitelist()
def get_status_options(status_type):
    """
    status_type: 'sales'  → field custom_sales_status
                 'procurement' → field procurement_status
    """
    try:
        fieldname = "custom_sales_status" if status_type == "sales" else "procurement_status"
        options = []

        # -------------------------------------------------
        # 1. Property Setter (this is where Customize Form writes the options)
        # -------------------------------------------------
        ps_value = frappe.db.get_value(
            "Property Setter",
            {
                "doc_type": "Request And Quote",
                "field_name": fieldname,
                "property": "options"
            },
            "value"
        )
        if ps_value:
            options = [o.strip() for o in str(ps_value).split("\n") if o.strip()]

        # -------------------------------------------------
        # 2. Fresh meta (ignore cache)
        # -------------------------------------------------
        if not options:
            meta = frappe.get_meta("Request And Quote", cached=False)
            field = meta.get_field(fieldname)
            if field and field.options:
                options = [o.strip() for o in field.options.split("\n") if o.strip()]

        # -------------------------------------------------
        # 3. Last resort – read the DocField table directly
        # -------------------------------------------------
        if not options:
            df_options = frappe.db.get_value(
                "DocField",
                {
                    "parent": "Request And Quote",
                    "fieldname": fieldname
                },
                "options"
            )
            if df_options:
                options = [o.strip() for o in str(df_options).split("\n") if o.strip()]

        return {"status": "success", "data": options}

    except Exception as e:
        frappe.log_error("Get Status Options Error", str(e))
        return {"status": "error", "error": str(e), "data": []}

@frappe.whitelist()
def add_status_option(status_type, new_option):
    """
    Appends a new option to the Select field via Property Setter.
    Only Administrator / System Manager may call this.
    """
    try:
        if not (frappe.session.user == "Administrator" or "System Manager" in frappe.get_roles()):
            return {"status": "error", "error": "Only Administrator can add status options."}

        new_option = (new_option or "").strip()
        if not new_option:
            return {"status": "error", "error": "Option value cannot be empty."}

        fieldname = "custom_sales_status" if status_type == "sales" else "procurement_status"

        # Current options
        meta = frappe.get_meta("Request And Quote")
        field = meta.get_field(fieldname)
        current = []
        if field and field.options:
            current = [o.strip() for o in field.options.split("\n") if o.strip()]

        if new_option in current:
            return {"status": "error", "error": f'Option "{new_option}" already exists.'}

        new_options_str = "\n".join(current + [new_option])

        # Create or update Property Setter
        existing = frappe.db.get_value(
            "Property Setter",
            {
                "doc_type": "Request And Quote",
                "field_name": fieldname,
                "property": "options"
            },
            "name"
        )
        if existing:
            frappe.db.set_value("Property Setter", existing, "value", new_options_str)
        else:
            frappe.get_doc({
                "doctype": "Property Setter",
                "doctype_or_field": "DocField",
                "doc_type": "Request And Quote",
                "field_name": fieldname,
                "property": "options",
                "value": new_options_str,
                "property_type": "Text"
            }).insert(ignore_permissions=True)

        frappe.clear_cache(doctype="Request And Quote")
        return {"status": "success", "message": f'Option "{new_option}" added.'}
    except Exception as e:
        frappe.log_error("Add Status Option Error", str(e))
        return {"status": "error", "error": str(e)}



@frappe.whitelist()
def get_local_customers_for_user(user=None):
    """
    Return customers that have Customer.st or a linked Address.st
    whose resolved 3-digit code matches the logged-in user's ST code.
    Used by the “My Customers” tab in Customer Management.
    """
    try:
        from my_custom_app.process_request_test import (
            resolve_user_st_code,
            get_logged_in_user_st_code,
        )

        target_user = (user or frappe.session.user or "").strip()
        if not target_user or target_user in ("Guest", "Administrator"):
            return {"status": "success", "data": []}

        # Resolve the logged-in user to a clean 3-digit ST code
        target_code = get_logged_in_user_st_code(target_user)
        if not target_code:
            return {"status": "success", "data": []}

        # Collect every value that can resolve to this 3-digit code
        # (the code itself + any User.name that stores this code + that User's email)
        users_with_code = frappe.db.sql("""
            SELECT name, email
            FROM `tabUser`
            WHERE st = %s AND enabled = 1
        """, (target_code,), as_dict=True)

        possible_st_values = {target_code}
        for u in users_with_code:
            if u.name:
                possible_st_values.add(u.name)
            if u.email:
                possible_st_values.add(u.email)

        possible_st_values = list(possible_st_values)
        if not possible_st_values:
            return {"status": "success", "data": []}

        format_str = ", ".join(["%s"] * len(possible_st_values))

        # 1. Customers whose own st field matches any of the possible values
        cust_from_self = frappe.db.sql(f"""
            SELECT name AS customer
            FROM `tabCustomer`
            WHERE st IN ({format_str})
              AND disabled = 0
        """, tuple(possible_st_values), as_dict=True)

        # 2. Customers that have at least one linked Address whose st matches
        cust_from_addr = frappe.db.sql(f"""
            SELECT DISTINCT dl.link_name AS customer
            FROM `tabAddress` a
            INNER JOIN `tabDynamic Link` dl
                ON dl.parent = a.name
               AND dl.parenttype = 'Address'
               AND dl.link_doctype = 'Customer'
            WHERE a.st IN ({format_str})
        """, tuple(possible_st_values), as_dict=True)

        all_customer_names = set()
        for row in cust_from_self:
            all_customer_names.add(row.customer)
        for row in cust_from_addr:
            all_customer_names.add(row.customer)

        if not all_customer_names:
            return {"status": "success", "data": []}

        customer_names = list(all_customer_names)
        format_strings2 = ", ".join(["%s"] * len(customer_names))

        # 3. Same enrichment / de-duplication logic as before
        raw_customers = frappe.db.sql(f"""
            SELECT
                c.name AS name,
                c.customer_name,
                c.customer_primary_contact,
                IFNULL(
                    ct.email_id,
                    (SELECT email_id
                     FROM `tabContact Email`
                     WHERE parent = ct.name
                     ORDER BY is_primary DESC
                     LIMIT 1)
                ) AS email
            FROM `tabCustomer` c
            LEFT JOIN `tabDynamic Link` dl
                   ON dl.link_name = c.name
                  AND dl.link_doctype = 'Customer'
                  AND dl.parenttype = 'Contact'
            LEFT JOIN `tabContact` ct ON ct.name = dl.parent
            WHERE c.disabled = 0
              AND c.name IN ({format_strings2})
        """, tuple(customer_names), as_dict=True)

        unique_custs = {}
        for row in raw_customers:
            cust_name = row.get("name")
            human_name = row.get("customer_name")
            contact_name = row.get("customer_primary_contact")
            email = row.get("email")
            score = 0
            if email:
                score += 10
            if contact_name:
                score += 5

            if cust_name not in unique_custs:
                unique_custs[cust_name] = {
                    "name": cust_name,
                    "customer_name": human_name,
                    "contact": contact_name,
                    "email": email,
                    "score": score,
                }
            else:
                if score > unique_custs[cust_name]["score"]:
                    unique_custs[cust_name].update({
                        "contact": contact_name,
                        "email": email,
                        "score": score,
                    })

        return {"status": "success", "data": list(unique_custs.values())}

    except Exception as e:
        frappe.log_error("Get Local Customers For User Error", str(e))
        return {"status": "error", "error": str(e)}


@frappe.whitelist()
def get_cm_customer_statistics(customer_name, period="1m"):
    """
    Live aggregation of Request And Quote metrics for a customer.
    period: '1m' (default), '3m', '1y'
    Filters:
      - rq.customer = customer_name
      - DATE(rq.date) >= CURDATE() - INTERVAL X
    """
    try:
        if not customer_name:
            return {"status": "error", "error": "customer_name is required"}

        # Resolve interval
        period = (period or "1m").strip().lower()
        if period == "3m":
            interval_sql = "INTERVAL 3 MONTH"
        elif period == "1y":
            interval_sql = "INTERVAL 1 YEAR"
        else:
            # default / '1m'
            interval_sql = "INTERVAL 1 MONTH"

        # ---------- Core KPI counts ----------
        kpi_sql = f"""
            SELECT
                COUNT(*) AS total_requests,

                COUNT(CASE
                    WHEN rq.quotation_sales_price IS NULL OR rq.quotation_sales_price = ''
                    THEN 1 END) AS empty_count,

                COUNT(CASE
                    WHEN rq.quotation_sales_price IS NOT NULL
                     AND rq.quotation_sales_price != ''
                     AND LOWER(rq.quotation_sales_price) LIKE '%%w%%'
                    THEN 1 END) AS w_count,

                COUNT(CASE
                    WHEN rq.quotation_sales_price IS NOT NULL
                     AND rq.quotation_sales_price != ''
                     AND LOWER(rq.quotation_sales_price) NOT LIKE '%%w%%'
                     AND LOWER(rq.quotation_sales_price) NOT LIKE '%%nq%%'
                     AND LOWER(rq.quotation_sales_price) NOT LIKE '%%consulta%%'
                     AND LOWER(rq.quotation_sales_price) NOT LIKE '%%csr%%'
                     AND LOWER(rq.quotation_sales_price) NOT LIKE '%%etn%%'
                    THEN 1 END) AS priced_count,

                COUNT(CASE
                    WHEN rq.custom_sales_status = 'SUBMITTED'
                    THEN 1 END) AS submitted_count
            FROM `tabRequest And Quote` rq
            WHERE rq.customer = %s
              AND rq.date IS NOT NULL
              AND DATE(rq.date) >= DATE_SUB(CURDATE(), {interval_sql})
        """
        kpi_row = frappe.db.sql(kpi_sql, (customer_name,), as_dict=True)
        kpi = kpi_row[0] if kpi_row else {}

        total_requests = int(kpi.get("total_requests") or 0)
        empty_count    = int(kpi.get("empty_count") or 0)
        w_count        = int(kpi.get("w_count") or 0)
        priced_count   = int(kpi.get("priced_count") or 0)
        submitted_count = int(kpi.get("submitted_count") or 0)

        # Not Priced = everything that is not Priced
        not_priced_count = total_requests - priced_count

        # ---------- Awarded (RAQ name matched in Customer Order.id with a real order_number) ----------
        awarded_sql = f"""
            SELECT COUNT(DISTINCT rq.name) AS awarded_count
            FROM `tabRequest And Quote` rq
            INNER JOIN `tabCustomer Order` co ON co.id = rq.name
            WHERE rq.customer = %s
              AND rq.date IS NOT NULL
              AND DATE(rq.date) >= DATE_SUB(CURDATE(), {interval_sql})
              AND co.order_number IS NOT NULL
              AND co.order_number != ''
        """
        awarded_row = frappe.db.sql(awarded_sql, (customer_name,), as_dict=True)
        awarded_count = int(awarded_row[0].get("awarded_count") or 0) if awarded_row else 0

        # ---------- Top 10 Brands (quotation_brand) ----------
        brands_sql = f"""
            SELECT
                rq.quotation_brand AS brand,
                COUNT(*) AS cnt
            FROM `tabRequest And Quote` rq
            WHERE rq.customer = %s
              AND rq.date IS NOT NULL
              AND DATE(rq.date) >= DATE_SUB(CURDATE(), {interval_sql})
              AND rq.quotation_brand IS NOT NULL
              AND rq.quotation_brand != ''
            GROUP BY rq.quotation_brand
            ORDER BY cnt DESC
            LIMIT 10
        """
        top_brands = frappe.db.sql(brands_sql, (customer_name,), as_dict=True) or []

        # ---------- Top 10 Part Numbers (quotation_part_number) ----------
        pn_sql = f"""
            SELECT
                rq.quotation_part_number AS part_number,
                COUNT(*) AS cnt
            FROM `tabRequest And Quote` rq
            WHERE rq.customer = %s
              AND rq.date IS NOT NULL
              AND DATE(rq.date) >= DATE_SUB(CURDATE(), {interval_sql})
              AND rq.quotation_part_number IS NOT NULL
              AND rq.quotation_part_number != ''
            GROUP BY rq.quotation_part_number
            ORDER BY cnt DESC
            LIMIT 10
        """
        top_part_numbers = frappe.db.sql(pn_sql, (customer_name,), as_dict=True) or []

        data = {
            "period": period,
            "total_requests": total_requests,
            "empty_count": empty_count,
            "w_count": w_count,
            "priced_count": priced_count,
            "submitted_count": submitted_count,
            "not_priced_count": not_priced_count,
            "awarded_count": awarded_count,
            "top_brands": top_brands,
            "top_part_numbers": top_part_numbers
        }
        return {"status": "success", "data": data}

    except Exception as e:
        frappe.log_error("Get CM Customer Statistics Error", str(e))
        return {"status": "error", "error": str(e)}

@frappe.whitelist()
def get_current_user_groups():
    """Return the list of User Groups for the currently logged-in user, bypassing standard API permission limits."""
    try:
        groups = frappe.db.get_all("User Group Member", filters={"user": frappe.session.user}, pluck="parent")
        return {"status": "success", "groups": groups}
    except Exception as e:
        frappe.log_error("Get Current User Groups Error", str(e))
        return {"status": "error", "error": str(e), "groups": []}
        

@frappe.whitelist()
def get_scope_rep_users(group_type="procurement"):
    """
    Scope 'Select Rep' dropdown.

    Procurement:
      User Group name == 'Procurement'
      code = User.rep
      (frontend applies this code to Request And Quote.rp)

    Sales:
      User Group name == 'Sales'
      code = User.st
      (frontend applies this code to Request And Quote.ref)
    """
    try:
        group_type = (group_type or "procurement").strip().lower()
        if group_type == "sales":
            group_name = "Sales"
            code_field = "st"
        else:
            group_name = "Procurement"
            code_field = "rep"

        rows = frappe.db.sql(
            """
            SELECT DISTINCT
                u.name,
                IFNULL(NULLIF(u.full_name, ''), u.name) AS full_name,
                IFNULL(u.rep, '') AS rep,
                IFNULL(u.st, '') AS st
            FROM `tabUser` u
            INNER JOIN `tabUser Group Member` ugm
                ON ugm.user = u.name
               AND ugm.parent = %s
            WHERE u.enabled = 1
              AND u.name NOT IN ('Guest', 'Administrator')
            ORDER BY full_name ASC
            """,
            (group_name,),
            as_dict=True,
        ) or []

        data = []
        seen = set()
        for r in rows:
            name = (r.get("name") or "").strip()
            if not name or name in seen:
                continue
            code = (r.get(code_field) or "").strip()
            if not code:
                continue
            seen.add(name)
            data.append({
                "name": name,
                "full_name": r.get("full_name") or name,
                "rep": (r.get("rep") or "").strip(),
                "st": (r.get("st") or "").strip(),
                "code": code,
            })
        return {"status": "success", "data": data}
    except Exception as e:
        frappe.log_error("Get Scope Rep Users Error", str(e))
        return {"status": "error", "error": str(e), "data": []}

@frappe.whitelist()
def get_customer_creation_options():
    """Fetch Customer Groups and Custom Customer Category options bypassing role restrictions."""
    try:
        # Fetch all Customer Groups
        groups = frappe.db.get_all("Customer Group", pluck="name", order_by="name asc")
        
        # Fetch options for custom_customer_category from Customer metadata
        category_options = []
        meta = frappe.get_meta("Customer")
        cat_field = meta.get_field("custom_customer_category")
        if cat_field and cat_field.options:
            category_options = [opt.strip() for opt in cat_field.options.split("\n") if opt.strip()]

        return {
            "status": "success",
            "customer_groups": groups,
            "category_options": category_options
        }
    except Exception as e:
        frappe.log_error("Get Customer Creation Options Error", str(e))
        return {"status": "error", "error": str(e)}


@frappe.whitelist()
def create_customer_custom(payload):
    """Creates a new Customer with ignore_permissions=True, setting customer_type='Company' and default territory."""
    try:
        data = json.loads(payload)
        
        # Default territory under the hood so standard ERPNext validation succeeds
        default_territory = frappe.db.get_single_value("Selling Settings", "territory") or "All Territories"
        if not frappe.db.exists("Territory", default_territory):
            first_territory = frappe.db.get_value("Territory", {}, "name")
            default_territory = first_territory or "All Territories"

        doc = frappe.get_doc({
            "doctype": "Customer",
            "customer_name": data.get("customer_name"),
            "customer_group": data.get("customer_group"),
            "customer_type": "Company",  # Hardcoded on creation
            "territory": default_territory,
            "custom_customer_account_number": data.get("custom_customer_account_number"),
            "custom_customer_category": data.get("custom_customer_category"),
            "customer_details": data.get("customer_details") or ""
        })
        
        doc.insert(ignore_permissions=True)
        frappe.db.commit()
        
        return {
            "status": "success",
            "customer_name": doc.name
        }
    except Exception as e:
        frappe.log_error("Create Customer Custom Error", str(e))
        return {"status": "error", "error": str(e)}


@frappe.whitelist()
def search_upload_customers(query=""):
    try:
        filters = {}
        if query:
            filters = [
                ["Customer", "name", "like", f"%{query}%"]
            ]
        
        customers = frappe.get_all(
            "Customer",
            filters=filters,
            fields=["name", "customer_name", "customer_group"],
            limit_page_length=15,
            order_by="name asc"
        )
        return {"status": "success", "data": customers}
    except Exception as e:
        return {"status": "error", "error": str(e)}


@frappe.whitelist()
def session_ping():
    """
    Extremely lightweight keep-alive.
    Called by the Procurement/Sales Panel every few minutes so the
    Frappe session never expires while a user is filling a long form.
    """
    try:
        frappe.local.response.headers["Cache-Control"] = "no-store, private"
        frappe.local.response.headers["Pragma"] = "no-cache"
    except Exception:
        pass

    return {
        "status": "success",
        "user": frappe.session.user,
        "full_name": frappe.utils.get_fullname(frappe.session.user),
        "ts": frappe.utils.now_datetime().isoformat()
    }


@frappe.whitelist()
def get_st_for_ref(customer, div=None):
    """Return the ST code that should be used inside REF + the customer's category letter
    + country from the customer's primary Address."""
    try:
        from my_custom_app.process_request_test import get_st_code_for_ref
        st = get_st_code_for_ref(customer, div)
        cat = (frappe.db.get_value("Customer", customer, "custom_customer_category") or "").strip()

        country = ""
        primary_address = frappe.db.get_value("Customer", customer, "customer_primary_address")
        if primary_address:
            country = (frappe.db.get_value("Address", primary_address, "country") or "").strip()

        return {"status": "success", "st": st, "category": cat, "country": country}
    except Exception as e:
        frappe.log_error("get_st_for_ref Error", str(e))
        return {"status": "error", "error": str(e), "st": "", "category": "", "country": ""}
        


@frappe.whitelist()
def escalate_records(ids, reason):
    """
    Set procurement_status = 'E' and write the reason into the 'test' field
    (Proc. Notes) for one or more Request And Quote documents.
    Uses the Document API so Version history is created.
    """
    try:
        id_list = json.loads(ids) if isinstance(ids, str) else (ids or [])
        reason = (reason or "").strip()

        if not id_list:
            return {"status": "error", "error": "No records selected."}
        if not reason:
            return {"status": "error", "error": "Escalation reason is required."}

        updated = 0
        errors = []

        for docname in id_list:
            try:
                doc = frappe.get_doc("Request And Quote", docname)
                doc.procurement_status = "E"
                # Write reason into the custom field named "test" (Proc. Notes)
                if hasattr(doc, "test"):
                    doc.test = reason
                else:
                    # Fallback in case the field is not yet present on the meta
                    frappe.db.set_value("Request And Quote", docname, "test", reason, update_modified=False)

                doc.flags.ignore_permissions = True
                doc.save()
                updated += 1
            except Exception as row_err:
                frappe.log_error("Escalate Single Record Error", f"{docname}: {str(row_err)}")
                errors.append(f"{docname}: {str(row_err)}")

        frappe.db.commit()

        result = {"status": "success", "updated": updated}
        if errors:
            result["warnings"] = errors
        return result

    except Exception as e:
        frappe.log_error("Escalate Records Error", str(e))
        return {"status": "error", "error": str(e)}

@frappe.whitelist()
def get_dashboard_snapshot():
    """
    Fetches a live snapshot of Request And Quote records that are either 
    empty/null or 'W-WAITING' in procurement_status.
    """
    try:
        sql = """
            SELECT
                name as ID,
                ref as REF,
                due_date as DUE_DATE,
                due_time as DUE_TIME,
                brand as BRAND,
                part_number as PART_NUMBER,
                procurement_status as PROCUREMENT_STATUS,
                test as TEST,
                note as NOTE,
                rep as REP,
                date as ASSIGN_DATE,
                modified as MODIFIED
            FROM `tabRequest And Quote`
            WHERE (procurement_status IS NULL OR procurement_status = '' OR procurement_status = 'W-WAITING')
              AND due_date >= CURDATE()
        """
        records = frappe.db.sql(sql, as_dict=True)

        # Bulk fetch W-WAITING timestamp from Version table
        record_names = [r.ID for r in records if r.PROCUREMENT_STATUS == 'W-WAITING']
        w_times = {}
        if record_names:
            format_strings = ','.join(['%s'] * len(record_names))
            v_sql = f"""
                SELECT docname, creation
                FROM `tabVersion`
                WHERE ref_doctype = 'Request And Quote'
                  AND docname IN ({format_strings})
                  AND data LIKE '%%W-WAITING%%'
                ORDER BY creation ASC
            """
            versions = frappe.db.sql(v_sql, tuple(record_names), as_dict=True)
            for v in versions:
                if v.docname not in w_times:
                    w_times[v.docname] = v.creation  # Capture first time it entered W-WAITING

        ny_tz = pytz.timezone('America/New_York')
        now = datetime.now(ny_tz).replace(tzinfo=None)

        for r in records:
            # Calculate Days Since Assigned
            if r.ASSIGN_DATE:
                try:
                    # Safely handle string splitting for date conversion
                    assign_val = str(r.ASSIGN_DATE).split(' ')[0]
                    assign_dt = datetime.strptime(assign_val, '%Y-%m-%d')
                    r['days_since_assigned'] = (now.date() - assign_dt.date()).days
                except:
                    r['days_since_assigned'] = 0
            else:
                r['days_since_assigned'] = 0

            # Calculate Hours Since entering W-WAITING
            if r.PROCUREMENT_STATUS == 'W-WAITING':
                w_time = w_times.get(r.ID)
                if not w_time:
                    # Fallback to the row's modified date if no version log is found
                    w_time = r.MODIFIED
                
                if w_time:
                    # Convert to datetime object if Frappe returns it as a string
                    if isinstance(w_time, str):
                        w_time = datetime.strptime(w_time.split('.')[0], '%Y-%m-%d %H:%M:%S')
                    diff = now - w_time
                    r['hours_since_w'] = round(diff.total_seconds() / 3600.0, 1)
                else:
                    r['hours_since_w'] = 0
            else:
                r['hours_since_w'] = 0

        return {"status": "success", "data": records}

    except Exception as e:
        frappe.log_error("Proc Report Snapshot Error", str(e))
        return {"status": "error", "error": str(e)}


def _so_split_incoterm(raw):
    """
    RAQ stores free text such as:
      EXW NY
      EXW Milan, Italy
      CIF Callo Puerto Peru
    ERPNext Incoterm is a 3-letter Link (EXW, FOB, CIF).
    Returns (code, named_place).
    """
    text = str(raw or "").strip()
    if not text:
        return "", ""

    i = 0
    letters = []
    while i < len(text) and len(letters) < 3:
        ch = text[i]
        if ch.isalpha():
            letters.append(ch)
            i += 1
            continue
        if ch in (" ", "\t") and not letters:
            i += 1
            continue
        break

    if len(letters) != 3:
        return "", text

    code = "".join(letters).upper()
    rest = text[i:].strip()
    while rest and rest[0] in (" ", "\t", ",", "-", ".", "/", ":"):
        rest = rest[1:].strip()
    return code, rest


def _so_ensure_incoterm(code):
    """Return an existing Incoterm name, or create the 3-letter code if missing."""
    code = str(code or "").strip().upper()
    if not code or len(code) != 3 or not code.isalpha():
        return None

    if frappe.db.exists("Incoterm", code):
        return code

    try:
        existing = frappe.db.get_value("Incoterm", {"title": code}, "name")
        if existing:
            return existing
    except Exception:
        pass

    try:
        doc = frappe.new_doc("Incoterm")
        if doc.meta.has_field("title"):
            doc.title = code
        if doc.meta.has_field("incoterm"):
            doc.incoterm = code
        doc.flags.ignore_permissions = True
        doc.insert(ignore_permissions=True)
        if doc.name != code:
            try:
                frappe.rename_doc("Incoterm", doc.name, code, force=True, ignore_permissions=True)
                return code
            except Exception:
                return doc.name
        return doc.name
    except Exception:
        frappe.log_error("Convert SO Ensure Incoterm Error", frappe.get_traceback())
        return None


@frappe.whitelist()
def get_erp_so_history(raq_name):
    """
    Sales Order history for one Request And Quote row.
    Match order: Sales Order Item.custom_raq, then linked Item, then part number.
    """
    try:
        raq_name = str(raq_name or "").strip()
        if not raq_name or not frappe.db.exists("Request And Quote", raq_name):
            return {"status": "error", "error": "Request And Quote was not found.", "count": 0, "rows": []}

        raq_fields = ["customer", "sap", "part_number", "quotation_part_number"]
        if frappe.get_meta("Request And Quote").has_field("custom_gl_item"):
            raq_fields.append("custom_gl_item")
        raq = frappe.db.get_value("Request And Quote", raq_name, raq_fields, as_dict=True) or {}

        item_codes = []
        if frappe.db.exists("DocType", "GL Item Match"):
            linked = frappe.db.get_value("GL Item Match", {"request_and_quote": raq_name}, "item")
            if linked:
                item_codes.append(linked)

        custom_gl = str(raq.get("custom_gl_item") or "").strip()
        if custom_gl and custom_gl not in item_codes and frappe.db.exists("Item", custom_gl):
            item_codes.append(custom_gl)

        part_number = str(raq.get("quotation_part_number") or raq.get("part_number") or "").strip()
        so_has_raq = frappe.get_meta("Sales Order Item").has_field("custom_raq")
        so_has_pn = frappe.get_meta("Sales Order Item").has_field("custom_part_number")

        where = ["soi.parenttype = 'Sales Order'", "so.docstatus < 2"]
        params = []
        match_parts = []

        if so_has_raq:
            match_parts.append("soi.custom_raq = %s")
            params.append(raq_name)
        if item_codes:
            match_parts.append("soi.item_code IN ({})".format(", ".join(["%s"] * len(item_codes))))
            params.extend(item_codes)
        if part_number and so_has_pn:
            match_parts.append("IFNULL(soi.custom_part_number, '') = %s")
            params.append(part_number)

        if not match_parts:
            return {"status": "success", "count": 0, "match_via": "", "rows": []}

        where.append("(" + " OR ".join(match_parts) + ")")
        rows = frappe.db.sql(
            """
            SELECT
                so.name AS sales_order,
                so.transaction_date AS date,
                so.customer AS customer,
                so.status AS status,
                so.docstatus AS docstatus,
                soi.item_code AS item_code,
                soi.qty AS qty,
                soi.rate AS rate,
                soi.warehouse AS warehouse,
                soi.delivery_date AS delivery_date
            FROM `tabSales Order Item` soi
            INNER JOIN `tabSales Order` so ON so.name = soi.parent
            WHERE {where}
            ORDER BY so.transaction_date DESC, so.name DESC
            """.format(where=" AND ".join(where)),
            tuple(params),
            as_dict=True,
        ) or []

        data = []
        orders = []
        for row in rows:
            orders.append(row.sales_order)
            data.append({
                "sales_order": row.sales_order,
                "date": str(row.date or ""),
                "customer": row.customer or "",
                "status": "Draft" if int(row.docstatus or 0) == 0 else (row.status or "Submitted"),
                "item_code": row.item_code or "",
                "qty": row.qty or 0,
                "rate": row.rate or 0,
                "warehouse": row.warehouse or "",
                "delivery_date": str(row.delivery_date or ""),
            })

        return {
            "status": "success",
            "count": len(set(orders)),
            "match_via": "custom_raq" if so_has_raq else "item",
            "rows": data,
        }
    except Exception as e:
        frappe.log_error("Get ERP SO History Error", str(e))
        return {"status": "error", "error": str(e), "count": 0, "rows": []}


@frappe.whitelist()
def get_raq_rows_for_edit(ids, search_scope="procurement"):
    """
    Latest Request And Quote rows for the records the user is about to edit.
    Same column aliases as the active panel view. No list cache.
    Does not load the rest of the filtered page.
    """
    try:
        id_list = json.loads(ids) if isinstance(ids, str) else (ids or [])
        id_list = [str(i) for i in id_list if i is not None and str(i).strip() != ""]
        if not id_list:
            return {"status": "success", "data": []}

        format_strings = ",".join(["%s"] * len(id_list))
        if (search_scope or "procurement") == "sales":
            sql = f"""
                SELECT
                    rq.name as ID, rq.ref as REF, rq.date as DATE, rq.country as COUNTRY,
                    rq.customer as CUSTOMER, rq.`div` as `DIV`, rq.contact as CONTACT,
                    rq.email_customr as EMAIL_CUSTOMER, rq.customer_ref as CUSTOMER_REF,
                    rq.due_date as DUE_DATE, rq.due_time as DUE_TIME, rq.sap as SAP,
                    rq.item as ITEM, rq.qty as QTY, rq.unit as UNIT, rq.part_number as PART_NUMBER,
                    rq.brand as BRAND, rq.description as DESCRIPTION, rq.incoterm as INCOTERM,
                    rq.note as NOTE, rq.sale_price as SALE_PRICE, rq.reference_price as REFERENCE_PRICE,
                    rq.date_req as `DATE REQ`, rq.st as ST,
                    DATE_FORMAT(rq.creation, '%%H:%%i') as CREATION_TIME,
                    rq.rep as RP, rq.quotation_sales_price as SALES_PRICE, rq.quotation_attachment as ATTACHMENT,
                    rq.attachment as RFQ_ATTACHMENT,
                    rq.quotation_item as QUOTE_ITEM, rq.quotation_qty as QUOTE_QTY, rq.quotation_unit as QUOTE_UNIT,
                    rq.quotation_brand as QUOTE_BRAND, rq.quotation_part_number as QUOTE_PART_NUMBER,
                    rq.quotation_co as QUOTE_CO, rq.quotation_aprox_weight as QUOTE_APROX_WEIGHT,
                    rq.quotation_incoterm as QUOTE_INCOTERM, rq.quotation_delivery as QUOTE_DELIVERY,
                    rq.quotation_description as QUOTE_DESCRIPTION, rq.quotation_note as QUOTE_NOTE,
                    rq.feedback as FEEDBACK,
                    rq.custom_customer_bid_number as CUSTOMER_BID_NUMBER,
                    rq.custom_sales_status as CUSTOM_SALES_STATUS,
                    rq.procurement_status as PROCUREMENT_STATUS,
                    rq.custom_uploaded_by as UPLOADED_BY,
                    rq.custom_requested_by as SUBMITTED_BY,
                    rq.modified as MODIFIED,
                    (SELECT c.customer_details FROM `tabCustomer` c WHERE c.name = rq.customer LIMIT 1) as CUSTOMER_DETAILS
                FROM `tabRequest And Quote` rq
                WHERE rq.name IN ({format_strings})
            """
        else:
            sql = f"""
                SELECT
                    rq.name as ID, rq.ref as REF, rq.country as COUNTRY, rq.rep as REP,
                    rq.quotation_item as ITEM, rq.quotation_qty as QTY, rq.quotation_unit as UNIT, rq.quotation_part_number as PART_NUMBER,
                    rq.quotation_brand as BRAND, rq.quotation_description as DESCRIPTION, rq.quotation_co as CO,
                    rq.quotation_sales_price as SALES_PRICE, rq.quotation_delivery as DELIVERY,
                    rq.quotation_aprox_weight as APROX_WEIGHT, rq.quotation_incoterm as INCOTERM,
                    rq.quotation_attachment as ATTACHMENT, rq.quotation_note as NOTE, rq.quotation_rfqdate as RFQDATE,
                    rq.customer_ref as CUSTOMER_REF,
                    rq.due_date as DUE_DATE, rq.due_time as DUE_TIME, rq.sap as SAP, rq.attachment as RFQ_ATTACHMENT,
                    rq.customer as CUSTOMER, rq.`div` as `DIV`, rq.contact as CONTACT, rq.email_customr as EMAIL_CUSTOMER,
                    rq.date as DATE, rq.st as ST, rq.sale_price as SALE_PRICE, rq.rp as RP,
                    rq.item as ORIG_ITEM, rq.qty as ORIG_QTY, rq.unit as ORIG_UNIT, rq.brand as ORIG_BRAND,
                    rq.part_number as ORIG_PART_NUMBER, rq.description as ORIG_DESCRIPTION, rq.note as ORIG_NOTE,
                    DATE_FORMAT(rq.creation, '%%H:%%i') as CREATION_TIME,
                    DATE_FORMAT(rq.creation, '%%Y-%%m-%%d') as CREATION_DATE,
                    rq.custom_sales_status as CUSTOM_SALES_STATUS,
                    rq.procurement_status as PROCUREMENT_STATUS,
                    rq.test as TEST,
                    rq.modified as MODIFIED,
                    rq.feedback as FEEDBACK,
                    rq.custom_customer_bid_number as CUSTOMER_BID_NUMBER
                FROM `tabRequest And Quote` rq
                WHERE rq.name IN ({format_strings})
            """
        data = frappe.db.sql(sql, tuple(id_list), as_dict=True) or []
        ordered = {str(d.get("ID")): d for d in data}
        data = [ordered[i] for i in id_list if i in ordered]
        if data:
            page_ids = [str(d.get("ID")) for d in data if d.get("ID") is not None]
            ordered_set = set()
            if page_ids:
                format_ids = ",".join(["%s"] * len(page_ids))
                ordered_rows = frappe.db.sql(
                    f"SELECT DISTINCT id FROM `tabCustomer Order` WHERE id IN ({format_ids})",
                    tuple(page_ids),
                )
                ordered_set = {str(r[0]) for r in ordered_rows if r and r[0] is not None}
            for d in data:
                d["HAS_CUSTOMER_ORDER"] = 1 if str(d.get("ID")) in ordered_set else 0
            _attach_gl_item_line_tags(data)
        return {"status": "success", "data": data}
    except Exception as e:
        frappe.log_error("Get RAQ Rows For Edit Error", str(e))
        return {"status": "error", "error": str(e), "data": []}


@frappe.whitelist()
def set_procurement_panel_version(enabled):
    user = frappe.session.user
    if not user or user == "Guest":
        frappe.throw("Login required")
    value = 1 if str(enabled) in ("1", "true", "True") else 0
    frappe.db.set_value(
        "User",
        user,
        "custom_procurement_panel_version",
        value,
        update_modified=False,
    )
    frappe.db.commit()
    return {"status": "success", "custom_procurement_panel_version": value}