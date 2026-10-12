import frappe
from frappe.utils import flt, cint, strip_html

MODULE_VERSION = "20260925a"

PART_NUMBER_CANDIDATES = (
    "custom_part_number",
    "part_number",
    "manufacturer_part_no",
)


def _ensure_brand_main_division(brand_name):
    """Create a Main division for a newly created Brand if it does not already exist."""
    brand_name = str(brand_name or "").strip()
    if not brand_name or not frappe.db.exists("Brand", brand_name):
        return
    existing = frappe.db.exists("brand_division_glgnet", {
        "brand": brand_name,
        "div_name": "Main"
    })
    if existing:
        return
    try:
        frappe.get_doc({
            "doctype": "brand_division_glgnet",
            "brand": brand_name,
            "div_name": "Main",
            "added_on": frappe.utils.nowdate(),
            "notes": "Added during Upload"
        }).insert(ignore_permissions=True)
    except Exception as e:
        frappe.log_error(
            f"Failed to create Main division for brand {brand_name}: {e}",
            "Brand Main Division"
        )




@frappe.whitelist()
def get_module_version():
    return {"status": "success", "version": MODULE_VERSION}


def _escape_like(value):
    text = str(value or "")
    slash = chr(92)
    text = text.replace(slash, slash + slash)
    text = text.replace("%", slash + "%")
    text = text.replace("_", slash + "_")
    return text


def _item_part_number_field():
    """Return the first existing Item field that stores a part number."""
    try:
        meta = frappe.get_meta("Item")
        for fieldname in PART_NUMBER_CANDIDATES:
            if meta.has_field(fieldname):
                return fieldname
    except Exception:
        pass
    return None


def _part_number_select_sql():
    fieldname = _item_part_number_field()
    if fieldname:
        return f"i.`{fieldname}` as part_number"
    return "NULL as part_number"


def _has_child_field(fieldname):
    try:
        return bool(frappe.get_meta("Item Customer Detail").has_field(fieldname))
    except Exception:
        return False


def _manufacturer_part_map(item_codes):
    """Fallback part numbers from Item Manufacturer when Item has no PN field."""
    if not item_codes:
        return {}
    try:
        placeholders = ", ".join(["%s"] * len(item_codes))
        rows = frappe.db.sql(
            f"""
            SELECT item_code, manufacturer_part_no
            FROM `tabItem Manufacturer`
            WHERE item_code IN ({placeholders})
              AND IFNULL(manufacturer_part_no, '') != ''
            ORDER BY is_default DESC, modified DESC
            """,
            tuple(item_codes),
            as_dict=True,
        )
        out = {}
        for row in rows:
            code = row.get("item_code")
            if code and code not in out:
                out[code] = row.get("manufacturer_part_no")
        return out
    except Exception:
        return {}


def _get_customer_items(item_code):
    has_commodity = _has_child_field("commodity_type")
    commodity_select = "commodity_type" if has_commodity else "'' as commodity_type"
    rows = frappe.db.sql(
        f"""
        SELECT
            name,
            customer_name,
            customer_group,
            ref_code,
            {commodity_select}
        FROM `tabItem Customer Detail`
        WHERE parent = %s
          AND parenttype = 'Item'
        ORDER BY idx ASC, name ASC
        """,
        (item_code,),
        as_dict=True,
    ) or []
    out = []
    for row in rows:
        out.append({
            "name": row.get("name") or "",
            "customer_name": row.get("customer_name") or "",
            "customer_group": row.get("customer_group") or "",
            "commodity_type": row.get("commodity_type") or "",
            "ref_code": row.get("ref_code") or "",
        })
    return out


@frappe.whitelist()
def get_inventory(
    page=1,
    page_length=50,
    sort_by="ITEM_CODE",
    sort_order="asc",
    search_query=None,
    warehouse=None,
    hide_zero_qty=1,
):
    """
    Paginated Bin + Item listing for the Inventory tab.
    One row per Bin (item_code + warehouse).
    """
    try:
        page = max(cint(page) or 1, 1)
        page_length = max(cint(page_length) or 50, 1)
        start = (page - 1) * page_length
        hide_zero_qty = cint(hide_zero_qty)
        sort_order = "desc" if str(sort_order).lower() == "desc" else "asc"

        sort_map = {
            "BIN_ID": "b.name",
            "ITEM_CODE": "b.item_code",
            "ITEM_NAME": "i.item_name",
            "WAREHOUSE": "b.warehouse",
            "ACTUAL_QTY": "b.actual_qty",
            "ORDERED_QTY": "b.ordered_qty",
            "RESERVED_QTY": "b.reserved_qty",
            "BRAND": "i.brand",
            "PART_NUMBER": "part_number",
            "WEIGHT": "i.weight_per_unit",
            "STOCK_UOM": "i.stock_uom",
            "MODIFIED": "b.modified",
        }
        actual_sort = sort_map.get(str(sort_by).upper(), "b.item_code")

        where_conds = ["IFNULL(i.disabled, 0) = 0"]
        params = []

        if hide_zero_qty:
            where_conds.append("IFNULL(b.actual_qty, 0) != 0")

        warehouse = (warehouse or "").strip()
        if warehouse:
            where_conds.append("b.warehouse = %s")
            params.append(warehouse)

        q = (search_query or "").strip()
        if len(q) >= 1:
            like_val = f"%{_escape_like(q)}%"
            pn_field = _item_part_number_field()
            pn_clause = f"i.`{pn_field}` LIKE %s" if pn_field else "0=1"
            where_conds.append(
                f"""(
                    b.item_code LIKE %s
                    OR i.item_name LIKE %s
                    OR b.warehouse LIKE %s
                    OR IFNULL(i.brand, '') LIKE %s
                    OR {pn_clause}
                )"""
            )
            params.extend([like_val, like_val, like_val, like_val])
            if pn_field:
                params.append(like_val)

        where_clause = " WHERE " + " AND ".join(where_conds)
        pn_select = _part_number_select_sql()

        count_sql = f"""
            SELECT COUNT(*) as total
            FROM `tabBin` b
            INNER JOIN `tabItem` i ON i.name = b.item_code
            {where_clause}
        """
        count_row = frappe.db.sql(count_sql, tuple(params), as_dict=True)
        total_records = int(count_row[0]["total"]) if count_row else 0
        total_pages = -(-total_records // page_length) if page_length else 1

        data_sql = f"""
            SELECT
                b.name as BIN_ID,
                b.item_code as ITEM_CODE,
                i.item_name as ITEM_NAME,
                b.warehouse as WAREHOUSE,
                b.actual_qty as ACTUAL_QTY,
                b.ordered_qty as ORDERED_QTY,
                b.reserved_qty as RESERVED_QTY,
                b.projected_qty as PROJECTED_QTY,
                b.stock_uom as BIN_UOM,
                b.valuation_rate as VALUATION_RATE,
                b.stock_value as STOCK_VALUE,
                i.image as IMAGE,
                i.brand as BRAND,
                {pn_select},
                i.weight_per_unit as WEIGHT_PER_UNIT,
                i.weight_uom as WEIGHT_UOM,
                i.stock_uom as STOCK_UOM,
                i.item_group as ITEM_GROUP,
                i.description as DESCRIPTION
            FROM `tabBin` b
            INNER JOIN `tabItem` i ON i.name = b.item_code
            {where_clause}
            ORDER BY {actual_sort} {sort_order}, b.item_code ASC
            LIMIT %s OFFSET %s
        """
        data_params = list(params) + [page_length, start]
        data = frappe.db.sql(data_sql, tuple(data_params), as_dict=True) or []

        missing_pn = [d["ITEM_CODE"] for d in data if not d.get("part_number")]
        pn_map = _manufacturer_part_map(missing_pn)
        for row in data:
            if not row.get("part_number"):
                row["part_number"] = pn_map.get(row.get("ITEM_CODE")) or ""
            row["ACTUAL_QTY"] = flt(row.get("ACTUAL_QTY"))
            row["ORDERED_QTY"] = flt(row.get("ORDERED_QTY"))
            row["RESERVED_QTY"] = flt(row.get("RESERVED_QTY"))
            row["PROJECTED_QTY"] = flt(row.get("PROJECTED_QTY"))
            row["WEIGHT_PER_UNIT"] = flt(row.get("WEIGHT_PER_UNIT"))
            row["VALUATION_RATE"] = flt(row.get("VALUATION_RATE"))
            row["STOCK_VALUE"] = flt(row.get("STOCK_VALUE"))
            row["DESCRIPTION_TEXT"] = strip_html(row.get("DESCRIPTION") or "")
            row["IMAGE"] = row.get("IMAGE") or ""

        return {
            "status": "success",
            "data": data,
            "total_records": total_records,
            "total_pages": total_pages,
            "part_number_field": _item_part_number_field(),
        }
    except Exception as e:
        frappe.log_error("Item Management Get Inventory Error", str(e))
        return {"status": "error", "error": str(e)}


@frappe.whitelist()
def get_warehouses():
    """Warehouses that currently have at least one Bin row."""
    try:
        rows = frappe.db.sql(
            """
            SELECT DISTINCT warehouse
            FROM `tabBin`
            WHERE warehouse IS NOT NULL AND warehouse != ''
            ORDER BY warehouse ASC
            """,
            as_dict=True,
        )
        return {
            "status": "success",
            "data": [r.warehouse for r in rows if r.warehouse],
        }
    except Exception as e:
        frappe.log_error("Item Management Get Warehouses Error", str(e))
        return {"status": "error", "error": str(e), "data": []}


@frappe.whitelist()
def get_item_details(item_code, warehouse=None):
    """Expanded item + optional bin snapshot for the details popup."""
    try:
        item_code = (item_code or "").strip()
        if not item_code:
            return {"status": "error", "error": "Item Code is required."}
        if not frappe.db.exists("Item", item_code):
            return {"status": "error", "error": f"Item {item_code} not found."}

        item = frappe.get_doc("Item", item_code)
        pn_field = _item_part_number_field()
        part_number = ""
        if pn_field:
            part_number = item.get(pn_field) or ""
        if not part_number:
            part_number = _manufacturer_part_map([item_code]).get(item_code) or ""

        bin_row = None
        warehouse = (warehouse or "").strip()
        if warehouse:
            bin_row = frappe.db.get_value(
                "Bin",
                {"item_code": item_code, "warehouse": warehouse},
                [
                    "name",
                    "warehouse",
                    "actual_qty",
                    "ordered_qty",
                    "reserved_qty",
                    "projected_qty",
                    "indented_qty",
                    "stock_uom",
                    "valuation_rate",
                    "stock_value",
                ],
                as_dict=True,
            )

        all_bins = frappe.db.sql(
            """
            SELECT
                name as BIN_ID,
                warehouse,
                actual_qty,
                ordered_qty,
                reserved_qty,
                projected_qty,
                stock_uom,
                valuation_rate,
                stock_value
            FROM `tabBin`
            WHERE item_code = %s
            ORDER BY warehouse ASC
            """,
            (item_code,),
            as_dict=True,
        ) or []

        data_sheet = ""
        try:
            if frappe.get_meta("Item").has_field("custom_data_sheet"):
                data_sheet = item.get("custom_data_sheet") or ""
        except Exception:
            data_sheet = ""

        data = {
            "item_code": item.item_code,
            "item_name": item.item_name,
            "item_group": item.item_group,
            "brand": item.brand or "",
            "part_number": part_number,
            "part_number_field": pn_field,
            "image": item.image or "",
            "custom_data_sheet": data_sheet,
            "description": item.description or "",
            "description_text": strip_html(item.description or ""),
            "stock_uom": item.stock_uom or "",
            "customs_tariff_number": item.get("customs_tariff_number") or "",
            "country_of_origin": item.get("country_of_origin") or "",
            "weight_per_unit": flt(item.weight_per_unit),
            "weight_uom": item.weight_uom or "",
            "disabled": cint(item.disabled),
            "is_stock_item": cint(item.is_stock_item),
            "valuation_rate": flt(getattr(item, "valuation_rate", 0)),
            "standard_rate": flt(getattr(item, "standard_rate", 0)),
            "shelf_life_in_days": getattr(item, "shelf_life_in_days", None),
            "warranty_period": getattr(item, "warranty_period", None) or "",
            "current_bin": bin_row,
            "bins": all_bins,
            "customer_items": _get_customer_items(item_code),
            "has_commodity_type": _has_child_field("commodity_type"),
        }
        return {"status": "success", "data": data}
    except Exception as e:
        frappe.log_error("Item Management Get Item Details Error", str(e))
        return {"status": "error", "error": str(e)}


def _ensure_customs_tariff(value):
    """Return an existing Customs Tariff Number name, or create one."""
    tariff = str(value or "").strip()
    if not tariff:
        return None
    if frappe.db.exists("Customs Tariff Number", tariff):
        return tariff
    try:
        existing = frappe.db.get_value("Customs Tariff Number", {"tariff_number": tariff}, "name")
        if existing:
            return existing
    except Exception:
        pass
    doc = frappe.get_doc({
        "doctype": "Customs Tariff Number",
        "tariff_number": tariff,
        "description": tariff,
    })
    doc.insert(ignore_permissions=True)
    return doc.name


@frappe.whitelist()
def create_customs_tariff(tariff_number, description=None):
    """Create-or-return a Customs Tariff Number from the details popup."""
    try:
        tariff = str(tariff_number or "").strip()
        if not tariff:
            return {"status": "error", "error": "Tariff number is required."}
        name = _ensure_customs_tariff(tariff)
        desc = str(description or "").strip()
        if desc and name:
            try:
                doc = frappe.get_doc("Customs Tariff Number", name)
                if not (doc.description or "").strip() or doc.description == doc.tariff_number:
                    doc.description = desc
                    doc.flags.ignore_permissions = True
                    doc.save()
            except Exception:
                pass
        frappe.db.commit()
        return {
            "status": "success",
            "data": {
                "name": name,
                "tariff_number": name,
                "description": desc or name,
                "created": True,
            },
        }
    except Exception as e:
        frappe.log_error("Item Management Create Tariff Error", str(e))
        return {"status": "error", "error": str(e)}



def _sync_customer_items(doc, customer_items, item_code):
    """Replace Item.customer_items with the list sent from the popup."""
    incoming = customer_items if isinstance(customer_items, list) else []
    has_commodity = _has_child_field("commodity_type")

    doc.set("customer_items", [])
    for row in incoming:
        customer_name = str((row or {}).get("customer_name") or "").strip()
        if not customer_name:
            continue
        if not frappe.db.exists("Customer", customer_name):
            frappe.throw(f"Customer not found: {customer_name}")

        customer_group = str((row or {}).get("customer_group") or "").strip()
        if not customer_group:
            customer_group = frappe.db.get_value("Customer", customer_name, "customer_group") or ""

        ref_code = str((row or {}).get("ref_code") or "").strip()
        if not ref_code:
            ref_code = item_code

        child = {
            "customer_name": customer_name,
            "customer_group": customer_group,
            "ref_code": ref_code,
        }
        if has_commodity:
            child["commodity_type"] = str((row or {}).get("commodity_type") or "").strip()
        doc.append("customer_items", child)


@frappe.whitelist()
def update_item_details(payload):
    """
    Update editable Item master fields, including expanded-view fields:
    stock_uom, customs_tariff_number, country_of_origin, customer_items.
    Bin qty / warehouse are ERPNext-managed and are never written here.
    """
    try:
        data = frappe.parse_json(payload) if isinstance(payload, str) else (payload or {})
        item_code = (data.get("item_code") or "").strip()
        if not item_code:
            return {"status": "error", "error": "Item Code is required."}
        if not frappe.db.exists("Item", item_code):
            return {"status": "error", "error": f"Item {item_code} not found."}

        doc = frappe.get_doc("Item", item_code)

        if "item_name" in data and data.get("item_name") is not None:
            name = str(data.get("item_name") or "").strip()
            if name:
                doc.item_name = name

        if "brand" in data:
            brand = str(data.get("brand") or "").strip()
            if brand and not frappe.db.exists("Brand", brand):
                try:
                    frappe.get_doc({"doctype": "Brand", "brand": brand}).insert(ignore_permissions=True)
                    _ensure_brand_main_division(brand)
                except Exception:
                    pass
            doc.brand = brand or None

        if "weight_per_unit" in data:
            doc.weight_per_unit = flt(data.get("weight_per_unit"))

        if "weight_uom" in data:
            uom = str(data.get("weight_uom") or "").strip()
            doc.weight_uom = uom or None

        if "description" in data and data.get("description") is not None:
            doc.description = data.get("description")

        if "stock_uom" in data:
            stock_uom = str(data.get("stock_uom") or "").strip()
            if stock_uom:
                if not frappe.db.exists("UOM", stock_uom):
                    return {"status": "error", "error": f"UOM not found: {stock_uom}"}
                doc.stock_uom = stock_uom

        if "customs_tariff_number" in data:
            doc.customs_tariff_number = _ensure_customs_tariff(data.get("customs_tariff_number"))

        if "country_of_origin" in data:
            country = str(data.get("country_of_origin") or "").strip()
            if country and not frappe.db.exists("Country", country):
                return {"status": "error", "error": f"Country not found: {country}"}
            doc.country_of_origin = country or None

        if "image" in data:
            doc.image = str(data.get("image") or "").strip() or None

        if "custom_data_sheet" in data:
            sheet = str(data.get("custom_data_sheet") or "").strip() or None
            try:
                if frappe.get_meta("Item").has_field("custom_data_sheet"):
                    doc.custom_data_sheet = sheet
            except Exception:
                pass

        pn_field = _item_part_number_field()
        if "part_number" in data and pn_field:
            setattr(doc, pn_field, str(data.get("part_number") or "").strip())

        if "customer_items" in data:
            _sync_customer_items(doc, data.get("customer_items") or [], item_code)

        doc.flags.ignore_permissions = True
        doc.save()
        frappe.db.commit()

        return {
            "status": "success",
            "message": f"Item {item_code} updated.",
            "part_number_field": pn_field,
            "customer_items": _get_customer_items(item_code),
        }
    except Exception as e:
        frappe.log_error("Item Management Update Item Details Error", str(e))
        return {"status": "error", "error": str(e)}


@frappe.whitelist()
def get_brands():
    try:
        brands = frappe.get_all("Brand", pluck="name", order_by="name asc", limit_page_length=0)
        return {"status": "success", "data": brands or []}
    except Exception as e:
        return {"status": "error", "error": str(e), "data": []}


@frappe.whitelist()
def get_uoms():
    try:
        uoms = frappe.get_all("UOM", pluck="name", order_by="name asc", limit_page_length=0)
        return {"status": "success", "data": uoms or []}
    except Exception as e:
        return {"status": "error", "error": str(e), "data": []}


@frappe.whitelist()
def get_countries():
    try:
        countries = frappe.get_all("Country", pluck="name", order_by="name asc", limit_page_length=0)
        return {"status": "success", "data": countries or []}
    except Exception as e:
        return {"status": "error", "error": str(e), "data": []}


@frappe.whitelist()
def search_customers(query=""):
    try:
        q = (query or "").strip()
        like_val = f"%{_escape_like(q)}%"
        rows = frappe.db.sql(
            """
            SELECT name, customer_name, customer_group
            FROM `tabCustomer`
            WHERE disabled = 0
              AND (
                    %s = ''
                    OR name LIKE %s
                    OR customer_name LIKE %s
                  )
            ORDER BY customer_name ASC
            LIMIT 20
            """,
            (q, like_val, like_val),
            as_dict=True,
        ) or []
        return {"status": "success", "data": rows}
    except Exception as e:
        frappe.log_error("Item Management Search Customers Error", str(e))
        return {"status": "error", "error": str(e), "data": []}


@frappe.whitelist()
def search_countries(query=""):
    try:
        q = (query or "").strip()
        like_val = f"%{_escape_like(q)}%"
        rows = frappe.db.sql(
            """
            SELECT name
            FROM `tabCountry`
            WHERE %s = '' OR name LIKE %s OR code LIKE %s
            ORDER BY name ASC
            LIMIT 25
            """,
            (q, like_val, like_val),
            as_dict=True,
        ) or []
        return {"status": "success", "data": [r.name for r in rows]}
    except Exception as e:
        return {"status": "error", "error": str(e), "data": []}


@frappe.whitelist()
def search_customs_tariff(query=""):
    try:
        q = (query or "").strip()
        like_val = f"%{_escape_like(q)}%"
        rows = frappe.db.sql(
            """
            SELECT name, tariff_number, description
            FROM `tabCustoms Tariff Number`
            WHERE %s = ''
               OR name LIKE %s
               OR IFNULL(tariff_number, '') LIKE %s
               OR IFNULL(description, '') LIKE %s
            ORDER BY name ASC
            LIMIT 25
            """,
            (q, like_val, like_val, like_val),
            as_dict=True,
        ) or []
        return {"status": "success", "data": rows}
    except Exception as e:
        # Some sites only have `name` on Customs Tariff Number
        try:
            q = (query or "").strip()
            like_val = f"%{_escape_like(q)}%"
            rows = frappe.db.sql(
                """
                SELECT name
                FROM `tabCustoms Tariff Number`
                WHERE %s = '' OR name LIKE %s
                ORDER BY name ASC
                LIMIT 25
                """,
                (q, like_val),
                as_dict=True,
            ) or []
            return {"status": "success", "data": rows}
        except Exception as e2:
            frappe.log_error("Item Management Search Tariff Error", str(e2))
            return {"status": "error", "error": str(e2), "data": []}


@frappe.whitelist()
def search_uoms(query=""):
    try:
        q = (query or "").strip()
        like_val = f"%{_escape_like(q)}%"
        rows = frappe.db.sql(
            """
            SELECT name
            FROM `tabUOM`
            WHERE %s = '' OR name LIKE %s
            ORDER BY name ASC
            LIMIT 25
            """,
            (q, like_val),
            as_dict=True,
        ) or []
        return {"status": "success", "data": [r.name for r in rows]}
    except Exception as e:
        return {"status": "error", "error": str(e), "data": []}


GL_ITEM_MATCH_DOCTYPE = "GL Item Match"
GL_ITEM_MATCH_ALLOWED_USERS = {
    "Administrator",
    "gl102@glgeneralindustries.com",
    "gl102@glgenralindutries.com",
}


def _can_use_gl_item_match():
    user = (frappe.session.user or "").strip()
    if not user or user == "Guest":
        return False
    if user in GL_ITEM_MATCH_ALLOWED_USERS:
        return True
    email = user.lower()
    if email in GL_ITEM_MATCH_ALLOWED_USERS:
        return True
    roles = frappe.get_roles(user)
    if "Administrator" in roles or "System Manager" in roles:
        return True
    return False


def _require_gl_item_match_access():
    if not _can_use_gl_item_match():
        frappe.throw("GL Item Database matching is limited to Administrator and gl102.")


def _item_custom_pn_field():
    return _item_part_number_field() or "custom_part_number"


def _customer_items_for_codes(item_codes):
    if not item_codes:
        return {}
    placeholders = ", ".join(["%s"] * len(item_codes))
    has_commodity = _has_child_field("commodity_type")
    commodity_select = "commodity_type" if has_commodity else "'' as commodity_type"
    rows = frappe.db.sql(
        f"""
        SELECT
            parent as item_code,
            customer_name,
            customer_group,
            ref_code,
            {commodity_select}
        FROM `tabItem Customer Detail`
        WHERE parenttype = 'Item'
          AND parent IN ({placeholders})
        ORDER BY idx ASC
        """,
        tuple(item_codes),
        as_dict=True,
    ) or []
    out = {}
    for row in rows:
        out.setdefault(row.item_code, []).append({
            "customer_name": row.get("customer_name") or "",
            "customer_group": row.get("customer_group") or "",
            "ref_code": row.get("ref_code") or "",
            "commodity_type": row.get("commodity_type") or "",
        })
    return out


def _item_card(item_code, extra=None):
    pn_field = _item_part_number_field()
    pn_select = f"i.`{pn_field}` as part_number" if pn_field else "NULL as part_number"
    rows = frappe.db.sql(
        f"""
        SELECT
            i.name as item_code,
            i.item_name,
            i.brand,
            i.item_group,
            i.stock_uom,
            i.weight_per_unit,
            i.weight_uom,
            i.image,
            i.disabled,
            {pn_select}
        FROM `tabItem` i
        WHERE i.name = %s
        """,
        (item_code,),
        as_dict=True,
    )
    if not rows:
        return None
    card = rows[0]
    if not card.get("part_number"):
        card["part_number"] = _manufacturer_part_map([item_code]).get(item_code) or ""
    card["customer_items"] = _customer_items_for_codes([item_code]).get(item_code, [])
    if extra:
        card.update(extra)
    return card



def _normalize_part_number(value):
    text = str(value or "").strip().upper()
    return text.replace(" ", "").replace("-", "")


def _classify_matches(candidates, sap=None, customer=None, part_number=None, brand=None):
    """
    Priority:
      1. SAP (ref_code) + Customer → Green if exactly 1, Yellow if >1
      2. Part Number + Brand → Green if exactly 1, Yellow if >1
      3. Part Number only → always Yellow (user must review)
    Red: no candidates.
    """
    unique_items = []
    seen = set()
    for c in candidates:
        code = c.get("item_code")
        if code and code not in seen:
            seen.add(code)
            unique_items.append(c)

    if not unique_items:
        return "red", unique_items, "No match"

    # Group by method
    by_method = {}
    for c in unique_items:
        method = c.get("match_method") or "unknown"
        by_method.setdefault(method, []).append(c)

    # 1. SAP + Customer
    sap_hits = by_method.get("sap_customer", [])
    if sap and customer and sap_hits:
        if len(sap_hits) == 1:
            return "green", sap_hits, "Unique SAP + Customer match"
        return "yellow", sap_hits, f"SAP '{sap}' matched {len(sap_hits)} items for this customer"

    # 2. Part Number + Brand
    pn_brand_hits = by_method.get("part_number_brand", [])
    if part_number and brand and pn_brand_hits:
        if len(pn_brand_hits) == 1:
            return "green", pn_brand_hits, "Unique Part Number + Brand match"
        return "yellow", pn_brand_hits, f"Part number + brand matched {len(pn_brand_hits)} items"

    # 3. Part Number only → always Yellow
    pn_only_hits = by_method.get("part_number", [])
    if part_number and pn_only_hits:
        return "yellow", pn_only_hits, f"Part number only matched {len(pn_only_hits)} item(s) - please review"

    # Fallback
    if len(unique_items) == 1:
        return "green", unique_items, "Unique match"
    return "yellow", unique_items, f"{len(unique_items)} items matched"


def _find_item_candidates(customer=None, sap=None, part_number=None, brand=None):
    customer = (customer or "").strip()
    sap = (sap or "").strip()
    part_number = (part_number or "").strip()
    brand = (brand or "").strip()
    normalized_pn = _normalize_part_number(part_number)
    candidates = []
    seen_pairs = set()

    # 1. SAP + Customer
    if sap and customer:
        rows = frappe.db.sql(
            """
            SELECT
                icd.parent as item_code,
                icd.customer_name,
                icd.ref_code
            FROM `tabItem Customer Detail` icd
            INNER JOIN `tabItem` i ON i.name = icd.parent
            WHERE icd.parenttype = 'Item'
              AND IFNULL(i.disabled, 0) = 0
              AND icd.customer_name = %s
              AND IFNULL(icd.ref_code, '') = %s
            """,
            (customer, sap),
            as_dict=True,
        ) or []
        for row in rows:
            key = (row.item_code, "sap_customer")
            if key in seen_pairs:
                continue
            seen_pairs.add(key)
            card = _item_card(row.item_code, {
                "match_method": "sap_customer",
                "matched_customer": row.customer_name,
                "matched_ref_code": row.ref_code,
            })
            if card:
                candidates.append(card)

    pn_field = _item_part_number_field()
    if normalized_pn and pn_field:
        # 2. Part Number + Brand
        if brand:
            rows = frappe.db.sql(
                f"""
                SELECT i.name as item_code
                FROM `tabItem` i
                WHERE IFNULL(i.disabled, 0) = 0
                  AND IFNULL(i.`{pn_field}`, '') != ''
                  AND REPLACE(REPLACE(UPPER(i.`{pn_field}`), ' ', ''), '-', '') = %s
                  AND UPPER(IFNULL(i.brand, '')) = UPPER(%s)
                """,
                (normalized_pn, brand),
                as_dict=True,
            ) or []
            for row in rows:
                key = (row.item_code, "part_number_brand")
                if key in seen_pairs:
                    continue
                seen_pairs.add(key)
                card = _item_card(row.item_code, {
                    "match_method": "part_number_brand",
                    "matched_customer": "",
                    "matched_ref_code": "",
                })
                if card:
                    candidates.append(card)

        # 3. Part Number only (always collect for Yellow)
        rows = frappe.db.sql(
            f"""
            SELECT i.name as item_code
            FROM `tabItem` i
            WHERE IFNULL(i.disabled, 0) = 0
              AND IFNULL(i.`{pn_field}`, '') != ''
              AND REPLACE(REPLACE(UPPER(i.`{pn_field}`), ' ', ''), '-', '') = %s
            """,
            (normalized_pn,),
            as_dict=True,
        ) or []
        for row in rows:
            key = (row.item_code, "part_number")
            if key in seen_pairs:
                continue
            seen_pairs.add(key)
            card = _item_card(row.item_code, {
                "match_method": "part_number",
                "matched_customer": "",
                "matched_ref_code": "",
            })
            if card:
                candidates.append(card)

    return candidates



@frappe.whitelist()
def search_gl_database_items(query=""):
    """
    Manual link search.
    Matches Item ID, Item Name, custom_part_number, and
    Item Customer Detail.ref_code.
    Each suggestion includes item id, part number, brand,
    ref_code, and the customer linked to that ref_code.
    """
    try:
        _require_gl_item_match_access()
        q = (query or "").strip()
        if len(q) < 1:
            return {"status": "success", "data": []}

        like_val = f"%{_escape_like(q)}%"
        q_lower = q.lower()
        meta = frappe.get_meta("Item")
        pn_fields = [
            fieldname for fieldname in ("custom_part_number", "part_number", "manufacturer_part_no")
            if meta.has_field(fieldname)
        ]
        display_pn = pn_fields[0] if pn_fields else None
        pn_select = f"i.`{display_pn}` as part_number" if display_pn else "NULL as part_number"

        item_params = [like_val, like_val]
        pn_ors = []
        for fieldname in pn_fields:
            pn_ors.append(f"IFNULL(i.`{fieldname}`, '') LIKE %s")
            item_params.append(like_val)
        pn_clause = (" OR " + " OR ".join(pn_ors)) if pn_ors else ""

        item_rows = frappe.db.sql(
            f"""
            SELECT
                i.name as item_code,
                i.item_name,
                i.brand,
                {pn_select}
            FROM `tabItem` i
            WHERE IFNULL(i.disabled, 0) = 0
              AND (
                    i.name LIKE %s
                    OR IFNULL(i.item_name, '') LIKE %s
                    {pn_clause}
              )
            ORDER BY i.name ASC
            LIMIT 25
            """,
            tuple(item_params),
            as_dict=True,
        ) or []

        ref_rows = frappe.db.sql(
            f"""
            SELECT
                i.name as item_code,
                i.item_name,
                i.brand,
                {pn_select},
                icd.customer_name,
                icd.customer_group,
                icd.ref_code
            FROM `tabItem Customer Detail` icd
            INNER JOIN `tabItem` i ON i.name = icd.parent
            WHERE icd.parenttype = 'Item'
              AND IFNULL(i.disabled, 0) = 0
              AND IFNULL(icd.ref_code, '') != ''
              AND icd.ref_code LIKE %s
            ORDER BY
                CASE WHEN icd.ref_code = %s THEN 0 ELSE 1 END,
                icd.ref_code ASC,
                i.name ASC
            LIMIT 25
            """,
            (like_val, q),
            as_dict=True,
        ) or []

        order = []
        by_code = {}

        def ensure(row):
            code = (row.get("item_code") or "").strip()
            if not code:
                return None
            if code not in by_code:
                by_code[code] = {
                    "item_code": code,
                    "item_name": row.get("item_name") or "",
                    "brand": row.get("brand") or "",
                    "part_number": row.get("part_number") or "",
                    "customer_items": [],
                    "matched_ref_code": "",
                    "matched_customer": "",
                    "ref_code": "",
                    "customer_name": "",
                }
                order.append(code)
            hit = by_code[code]
            if not hit["item_name"] and row.get("item_name"):
                hit["item_name"] = row.get("item_name") or ""
            if not hit["brand"] and row.get("brand"):
                hit["brand"] = row.get("brand") or ""
            if not hit["part_number"] and row.get("part_number"):
                hit["part_number"] = row.get("part_number") or ""
            return hit

        for row in ref_rows:
            hit = ensure(row)
            if not hit:
                continue
            customer_name = row.get("customer_name") or ""
            ref_code = row.get("ref_code") or ""
            key = (customer_name, ref_code)
            existing = {(c.get("customer_name") or "", c.get("ref_code") or "") for c in hit["customer_items"]}
            if key not in existing:
                hit["customer_items"].append({
                    "customer_name": customer_name,
                    "customer_group": row.get("customer_group") or "",
                    "ref_code": ref_code,
                    "commodity_type": "",
                })
            if not hit["matched_ref_code"]:
                hit["matched_ref_code"] = ref_code
                hit["matched_customer"] = customer_name
                hit["ref_code"] = ref_code
                hit["customer_name"] = customer_name

        for row in item_rows:
            ensure(row)

        cust_map = _customer_items_for_codes(order)
        for code in order:
            hit = by_code[code]
            existing = {(c.get("customer_name") or "", c.get("ref_code") or "") for c in hit["customer_items"]}
            for customer_row in cust_map.get(code) or []:
                key = (customer_row.get("customer_name") or "", customer_row.get("ref_code") or "")
                if key not in existing:
                    hit["customer_items"].append(customer_row)
                    existing.add(key)

            def customer_rank(customer_row):
                ref_code = str(customer_row.get("ref_code") or "")
                ref_lower = ref_code.lower()
                if ref_lower == q_lower:
                    return (0, ref_code)
                if q_lower in ref_lower:
                    return (1, ref_code)
                return (2, ref_code)

            matched_rows = [c for c in hit["customer_items"] if q_lower in str(c.get("ref_code") or "").lower()]
            other_rows = [c for c in hit["customer_items"] if q_lower not in str(c.get("ref_code") or "").lower()]
            hit["customer_items"] = (sorted(matched_rows, key=customer_rank) + sorted(other_rows, key=customer_rank))[:8]

            matched = next((c for c in hit["customer_items"] if q_lower in str(c.get("ref_code") or "").lower()), None)
            if matched:
                hit["matched_ref_code"] = matched.get("ref_code") or ""
                hit["matched_customer"] = matched.get("customer_name") or ""
                hit["ref_code"] = hit["matched_ref_code"]
                hit["customer_name"] = hit["matched_customer"]
            elif hit["customer_items"]:
                hit["ref_code"] = hit["customer_items"][0].get("ref_code") or ""
                hit["customer_name"] = hit["customer_items"][0].get("customer_name") or ""

        missing_pn = [code for code in order if not by_code[code]["part_number"]]
        pn_map = _manufacturer_part_map(missing_pn)
        for code in missing_pn:
            by_code[code]["part_number"] = pn_map.get(code) or ""

        def suggestion_rank(hit):
            code = str(hit.get("item_code") or "").lower()
            part_number = str(hit.get("part_number") or "").lower()
            ref_code = str(hit.get("matched_ref_code") or hit.get("ref_code") or "").lower()
            if code == q_lower:
                return 0
            if part_number == q_lower:
                return 1
            if ref_code == q_lower:
                return 2
            if ref_code and q_lower in ref_code:
                return 3
            if part_number and q_lower in part_number:
                return 4
            if q_lower in code:
                return 5
            return 6

        out = [by_code[code] for code in order]
        out.sort(key=suggestion_rank)
        return {"status": "success", "data": out[:25]}
    except Exception as e:
        frappe.log_error("Search GL Database Items Error", str(e))
        return {"status": "error", "error": str(e), "data": []}


@frappe.whitelist()
def auto_match_items(payload):
    """
    payload = list of {row_key, customer, sap, part_number}
    Returns traffic-light result per row. Does NOT write the link.
    """
    try:
        _require_gl_item_match_access()
        rows = frappe.parse_json(payload) if isinstance(payload, str) else (payload or [])
        results = []
        for row in rows:
            row_key = (row or {}).get("row_key")
            customer = (row or {}).get("customer") or ""
            sap = (row or {}).get("sap") or ""
            part_number = (row or {}).get("part_number") or ""
            brand = (row or {}).get("brand") or ""
            candidates = _find_item_candidates(customer, sap, part_number, brand)
            color, unique_items, reason = _classify_matches(
                candidates, sap=sap, customer=customer, part_number=part_number, brand=brand
            )
            suggested = unique_items[0] if len(unique_items) == 1 and color == "green" else None
            results.append({
                "row_key": row_key,
                "color": color,
                "reason": reason,
                "suggested_item": suggested,
                "candidates": unique_items,
            })
        return {"status": "success", "data": results}
    except Exception as e:
        frappe.log_error("Auto Match Items Error", str(e))
        return {"status": "error", "error": str(e), "data": []}


@frappe.whitelist()
def get_gl_item_snapshot(item_code):
    """Item master + bins + item prices + attached files for the GL Item Database tab."""
    try:
        _require_gl_item_match_access()
        item_code = (item_code or "").strip()
        if not item_code:
            return {"status": "error", "error": "Item Code is required."}
        if not frappe.db.exists("Item", item_code):
            return {"status": "error", "error": f"Item {item_code} not found."}

        details = get_item_details(item_code)
        if details.get("status") != "success":
            return details
        data = details.get("data") or {}

        prices = []
        try:
            prices = frappe.db.sql(
                """
                SELECT
                    name,
                    price_list,
                    price_list_rate,
                    currency,
                    uom,
                    selling,
                    buying,
                    valid_from,
                    valid_upto
                FROM `tabItem Price`
                WHERE item_code = %s
                ORDER BY selling DESC, price_list ASC
                """,
                (item_code,),
                as_dict=True,
            ) or []
        except Exception:
            prices = []

        files = frappe.db.sql(
            """
            SELECT name, file_name, file_url, is_private
            FROM `tabFile`
            WHERE attached_to_doctype = 'Item'
              AND attached_to_name = %s
            ORDER BY creation DESC
            """,
            (item_code,),
            as_dict=True,
        ) or []

        in_stock = False
        stock_qty = 0.0
        for b in (data.get("bins") or []):
            qty = flt(b.get("actual_qty"))
            stock_qty += qty
            if qty:
                in_stock = True

        data["item_prices"] = prices
        data["files"] = files
        data["in_stock"] = 1 if in_stock else 0
        data["stock_qty"] = stock_qty
        data["has_price_list"] = 1 if prices else 0
        return {"status": "success", "data": data}
    except Exception as e:
        frappe.log_error("Get GL Item Snapshot Error", str(e))
        return {"status": "error", "error": str(e)}


@frappe.whitelist()
def get_raq_item_match(raq_name):
    try:
        _require_gl_item_match_access()
        raq_name = str(raq_name or "").strip()
        if not raq_name:
            return {"status": "error", "error": "Request And Quote name is required."}

        match_row = None
        if frappe.db.exists("DocType", GL_ITEM_MATCH_DOCTYPE):
            match_row = frappe.db.get_value(
                GL_ITEM_MATCH_DOCTYPE,
                {"request_and_quote": raq_name},
                ["name", "item", "match_method", "match_status", "customer", "sap", "part_number"],
                as_dict=True,
            )

        snapshot = None
        if match_row and match_row.get("item"):
            snap = get_gl_item_snapshot(match_row.get("item"))
            if snap.get("status") == "success":
                snapshot = snap.get("data")

        return {
            "status": "success",
            "match": match_row,
            "snapshot": snapshot,
        }
    except Exception as e:
        frappe.log_error("Get RAQ Item Match Error", str(e))
        return {"status": "error", "error": str(e)}


def _save_raq_item_match_impl(raq_name, item_code, match_method="manual", match_status="confirmed"):
    """Write GL Item Match + custom_gl_item. No access gate and no snapshot."""
    raq_name = str(raq_name or "").strip()
    item_code = str(item_code or "").strip()
    if not raq_name or not item_code:
        return {"status": "error", "error": "Request And Quote and Item are required."}
    if not frappe.db.exists("Request And Quote", raq_name):
        return {"status": "error", "error": f"Request And Quote {raq_name} not found."}
    if not frappe.db.exists("Item", item_code):
        return {"status": "error", "error": f"Item {item_code} not found."}
    if not frappe.db.exists("DocType", GL_ITEM_MATCH_DOCTYPE):
        return {"status": "error", "error": "DocType GL Item Match does not exist yet. Create it first."}

    raq = frappe.db.get_value(
        "Request And Quote",
        raq_name,
        ["customer", "sap", "part_number", "quotation_part_number"],
        as_dict=True,
    ) or {}

    existing_name = frappe.db.get_value(
        GL_ITEM_MATCH_DOCTYPE,
        {"request_and_quote": raq_name},
        "name",
    )
    payload = {
        "item": item_code,
        "match_method": match_method or "manual",
        "match_status": match_status or "confirmed",
        "customer": raq.get("customer") or "",
        "sap": raq.get("sap") or "",
        "part_number": raq.get("quotation_part_number") or raq.get("part_number") or "",
    }
    if existing_name:
        doc = frappe.get_doc(GL_ITEM_MATCH_DOCTYPE, existing_name)
        doc.update(payload)
        doc.flags.ignore_permissions = True
        doc.save()
    else:
        doc = frappe.get_doc({
            "doctype": GL_ITEM_MATCH_DOCTYPE,
            "request_and_quote": raq_name,
            **payload,
        })
        doc.flags.ignore_permissions = True
        doc.insert()

    if frappe.get_meta("Request And Quote").has_field("custom_gl_item"):
        frappe.db.set_value("Request And Quote", raq_name, "custom_gl_item", item_code, update_modified=False)

    return {
        "status": "success",
        "message": f"Linked {raq_name} → {item_code}",
        "match": {
            "name": doc.name,
            "item": item_code,
            "match_method": payload["match_method"],
            "match_status": payload["match_status"],
        },
    }


@frappe.whitelist()
def save_raq_item_match(raq_name, item_code, match_method="manual", match_status="confirmed"):
    try:
        _require_gl_item_match_access()
        result = _save_raq_item_match_impl(raq_name, item_code, match_method, match_status)
        if result.get("status") != "success":
            return result
        frappe.db.commit()
        snap = get_gl_item_snapshot(item_code)
        result["snapshot"] = snap.get("data") if snap.get("status") == "success" else None
        return result
    except Exception as e:
        frappe.log_error("Save RAQ Item Match Error", str(e))
        return {"status": "error", "error": str(e)}


def _clear_raq_item_match_impl(raq_name):
    raq_name = str(raq_name or "").strip()
    if not raq_name:
        return {"status": "error", "error": "Request And Quote name is required."}
    if frappe.db.exists("DocType", GL_ITEM_MATCH_DOCTYPE):
        names = frappe.get_all(
            GL_ITEM_MATCH_DOCTYPE,
            filters={"request_and_quote": raq_name},
            pluck="name",
        )
        for name in names:
            frappe.delete_doc(GL_ITEM_MATCH_DOCTYPE, name, ignore_permissions=True)
    if frappe.get_meta("Request And Quote").has_field("custom_gl_item"):
        frappe.db.set_value("Request And Quote", raq_name, "custom_gl_item", None, update_modified=False)
    return {"status": "success"}


@frappe.whitelist()
def clear_raq_item_match(raq_name):
    try:
        _require_gl_item_match_access()
        result = _clear_raq_item_match_impl(raq_name)
        if result.get("status") == "success":
            frappe.db.commit()
        return result
    except Exception as e:
        frappe.log_error("Clear RAQ Item Match Error", str(e))
        return {"status": "error", "error": str(e)}


@frappe.whitelist()
def apply_pending_upload_matches(payload):
    """
    After save_data succeeds, resolve each uploaded line to the newest matching
    Request And Quote row and write GL Item Match.
    payload = [{customer, sap, part_number, item_code, match_method}]
    """
    try:
        _require_gl_item_match_access()
        rows = frappe.parse_json(payload) if isinstance(payload, str) else (payload or [])
        linked = 0
        skipped = []
        for row in rows:
            item_code = str((row or {}).get("item_code") or "").strip()
            customer = str((row or {}).get("customer") or "").strip()
            sap = str((row or {}).get("sap") or "").strip()
            part_number = str((row or {}).get("part_number") or "").strip()
            match_method = str((row or {}).get("match_method") or "manual")
            if not item_code:
                continue

            where = ["rq.creation >= DATE_SUB(NOW(), INTERVAL 30 MINUTE)"]
            params = []
            if customer:
                where.append("rq.customer = %s")
                params.append(customer)
            if sap:
                where.append("rq.sap = %s")
                params.append(sap)
            if part_number:
                where.append("(rq.part_number = %s OR rq.quotation_part_number = %s)")
                params.extend([part_number, part_number])
            if not sap and not part_number:
                skipped.append({"reason": "missing sap and part_number", "row": row})
                continue

            found = frappe.db.sql(
                f"""
                SELECT rq.name
                FROM `tabRequest And Quote` rq
                WHERE {' AND '.join(where)}
                ORDER BY rq.creation DESC
                LIMIT 1
                """,
                tuple(params),
                as_dict=True,
            )
            if not found:
                skipped.append({"reason": "RAQ not found after upload", "row": row})
                continue
            res = save_raq_item_match(found[0].name, item_code, match_method, "confirmed")
            if res.get("status") == "success":
                linked += 1
            else:
                skipped.append({"reason": res.get("error"), "row": row})
        return {"status": "success", "linked": linked, "skipped": skipped}
    except Exception as e:
        frappe.log_error("Apply Pending Upload Matches Error", str(e))
        return {"status": "error", "error": str(e)}


def _default_item_group():
    group = None
    try:
        group = frappe.db.get_single_value("Stock Settings", "item_group")
    except Exception:
        group = None
    if group and frappe.db.exists("Item Group", group):
        return group
    group = frappe.db.get_value("Item Group", {"is_group": 0}, "name")
    if group:
        return group
    if frappe.db.exists("Item Group", "All Item Groups"):
        return "All Item Groups"
    return frappe.db.get_value("Item Group", {}, "name")


def _sanitize_item_code(value):
    text = str(value or "").strip()
    cleaned = []
    for ch in text:
        if ch.isalnum() or ch in ("-", "_", ".", "/"):
            cleaned.append(ch)
        elif ch in (" ",):
            cleaned.append("-")
    out = "".join(cleaned).strip("-")
    return out[:140]


@frappe.whitelist()
def get_item_groups(query=""):
    try:
        q = (query or "").strip()
        like_val = f"%{_escape_like(q)}%"
        rows = frappe.db.sql(
            """
            SELECT name
            FROM `tabItem Group`
            WHERE IFNULL(is_group, 0) = 0
              AND (%s = '' OR name LIKE %s)
            ORDER BY name ASC
            LIMIT 25
            """,
            (q, like_val),
            as_dict=True,
        ) or []
        return {
            "status": "success",
            "data": [r.name for r in rows],
            "default_item_group": _default_item_group(),
        }
    except Exception as e:
        return {"status": "error", "error": str(e), "data": [], "default_item_group": _default_item_group()}


@frappe.whitelist()
def suggest_item_from_raq(raq_name):
    """Defaults for the Create Item form, copied from the RAQ row."""
    try:
        _require_gl_item_match_access()
        raq_name = str(raq_name or "").strip()
        if not raq_name:
            return {"status": "error", "error": "Request And Quote name is required."}
        if not frappe.db.exists("Request And Quote", raq_name):
            return {"status": "error", "error": f"Request And Quote {raq_name} not found."}

        raq = frappe.db.get_value(
            "Request And Quote",
            raq_name,
            [
                "name", "customer", "sap", "contact",
                "item", "qty", "unit", "brand", "part_number", "description", "note",
                "quotation_item", "quotation_qty", "quotation_unit", "quotation_brand",
                "quotation_part_number", "quotation_description", "quotation_note",
                "quotation_co", "quotation_aprox_weight", "quotation_incoterm",
                "sale_price", "quotation_sales_price",
            ],
            as_dict=True,
        ) or {}

        part_number = (raq.get("quotation_part_number") or raq.get("part_number") or "").strip()
        item_label = (raq.get("quotation_item") or raq.get("item") or "").strip()
        unit = (raq.get("quotation_unit") or raq.get("unit") or "").strip()
        brand = (raq.get("quotation_brand") or raq.get("brand") or "").strip()
        description = (raq.get("quotation_description") or raq.get("description") or "").strip()
        weight = raq.get("quotation_aprox_weight")
        country = (raq.get("quotation_co") or "").strip()

        suggested_code = _sanitize_item_code(part_number) or _sanitize_item_code(f"RAQ-{raq_name}")
        if suggested_code and frappe.db.exists("Item", suggested_code):
            base = suggested_code
            n = 2
            while frappe.db.exists("Item", f"{base}-{n}"):
                n += 1
            suggested_code = f"{base}-{n}"

        stock_uom = unit if unit and frappe.db.exists("UOM", unit) else (frappe.db.get_single_value("Stock Settings", "stock_uom") or "Nos")
        country_ok = country if country and frappe.db.exists("Country", country) else ""

        return {
            "status": "success",
            "data": {
                "raq_name": raq_name,
                "raq": raq,
                "item_code": suggested_code,
                "item_name": item_label or part_number or suggested_code,
                "item_group": _default_item_group() or "",
                "brand": brand,
                "part_number": part_number,
                "description": description,
                "stock_uom": stock_uom,
                "weight_per_unit": weight or 0,
                "weight_uom": "",
                "country_of_origin": country_ok,
                "customs_tariff_number": "",
                "is_stock_item": 1,
                "customer_items": [{
                    "customer_name": raq.get("customer") or "",
                    "customer_group": frappe.db.get_value("Customer", raq.get("customer"), "customer_group") if raq.get("customer") else "",
                    "ref_code": (raq.get("sap") or "").strip() or suggested_code,
                    "commodity_type": "",
                }] if raq.get("customer") else [],
            },
        }
    except Exception as e:
        frappe.log_error("Suggest Item From RAQ Error", str(e))
        return {"status": "error", "error": str(e)}


@frappe.whitelist()
def create_item_from_raq(payload):
    """
    Create an Item from the Convert-to-SO modal and immediately write
    GL Item Match + custom_gl_item on the RAQ.

    Stock Settings "Item Naming By" = Naming Series on this site.
    The form Item Code is the supplier part number, not Item.name.
    Item.autoname() assigns the series and overwrites item_code with doc.name.
    The link must use doc.name after insert.
    """
    try:
        _require_gl_item_match_access()
        data = frappe.parse_json(payload) if isinstance(payload, str) else (payload or {})
        raq_name = str(data.get("raq_name") or "").strip()
        item_code = str(data.get("item_code") or "").strip()
        item_name = str(data.get("item_name") or "").strip()
        item_group = str(data.get("item_group") or "").strip() or _default_item_group()
        stock_uom = str(data.get("stock_uom") or "").strip()
        part_number = str(data.get("part_number") or item_code or "").strip()
        uses_naming_series = frappe.db.get_default("item_naming_by") == "Naming Series"

        if not raq_name:
            return {"status": "error", "error": "Request And Quote name is required."}
        if not frappe.db.exists("Request And Quote", raq_name):
            return {"status": "error", "error": f"Request And Quote {raq_name} not found."}
        if not uses_naming_series and not item_code:
            return {"status": "error", "error": "Item Code is required."}
        if not item_name:
            return {"status": "error", "error": "Item Name is required."}
        if not item_group or not frappe.db.exists("Item Group", item_group):
            return {"status": "error", "error": f"Item Group is required and must exist (got: {item_group})."}
        if not stock_uom or not frappe.db.exists("UOM", stock_uom):
            return {"status": "error", "error": f"Stock UOM is required and must exist (got: {stock_uom})."}

        brand = str(data.get("brand") or "").strip()
        if brand and not frappe.db.exists("Brand", brand):
            try:
                frappe.get_doc({"doctype": "Brand", "brand": brand}).insert(ignore_permissions=True)
                _ensure_brand_main_division(brand)
            except Exception:
                pass

        country = str(data.get("country_of_origin") or "").strip()
        if country and not frappe.db.exists("Country", country):
            return {"status": "error", "error": f"Country not found: {country}"}

        # Naming Series never stores the typed code as Item.name.
        # Reuse the Item already created for this part number so a second
        # click does not insert 18438 / 18439 again.
        if uses_naming_series and part_number:
            pn_field = _item_part_number_field()
            existing = None
            if pn_field:
                filters = {pn_field: part_number}
                if brand:
                    filters["brand"] = brand
                existing = frappe.db.get_value("Item", filters, "name")
                if not existing and brand:
                    existing = frappe.db.get_value("Item", {pn_field: part_number}, "name")
            if existing:
                link = _save_raq_item_match_impl(raq_name, existing, "manual", "confirmed")
                if link.get("status") != "success":
                    return {
                        "status": "error",
                        "error": f"Item {existing} already exists but linking to {raq_name} failed: {link.get('error')}",
                        "item_code": existing,
                    }
                frappe.db.commit()
                return {
                    "status": "success",
                    "message": f"Linked existing Item {existing} to {raq_name}.",
                    "item_code": existing,
                    "item_name": item_name,
                    "reused": 1,
                    "match": link.get("match"),
                }
        elif item_code and frappe.db.exists("Item", item_code):
            return {"status": "error", "error": f"Item {item_code} already exists. Select it instead of creating a new one."}

        doc_fields = {
            "doctype": "Item",
            "item_name": item_name,
            "item_group": item_group,
            "stock_uom": stock_uom,
            "is_stock_item": 1 if int(data.get("is_stock_item") or 1) else 0,
            "include_item_in_manufacturing": 0,
            "description": data.get("description") or item_name,
            "brand": brand or None,
            "weight_per_unit": flt(data.get("weight_per_unit")),
            "weight_uom": str(data.get("weight_uom") or "").strip() or None,
            "country_of_origin": country or None,
        }
        if not uses_naming_series:
            doc_fields["item_code"] = item_code

        doc = frappe.get_doc(doc_fields)

        if data.get("customs_tariff_number"):
            doc.customs_tariff_number = _ensure_customs_tariff(data.get("customs_tariff_number"))

        pn_field = _item_part_number_field()
        if pn_field and part_number:
            setattr(doc, pn_field, part_number)

        if data.get("customer_items"):
            _sync_customer_items(doc, data.get("customer_items") or [], item_code or part_number)

        doc.flags.ignore_permissions = True
        doc.insert()

        # This is the name ERPNext actually saved (18440), not 52427254.
        real_item_code = doc.name
        link = _save_raq_item_match_impl(raq_name, real_item_code, "manual", "confirmed")
        if link.get("status") != "success":
            return {
                "status": "error",
                "error": f"Item {real_item_code} was created but linking to {raq_name} failed: {link.get('error')}",
                "item_code": real_item_code,
            }

        frappe.db.commit()
        return {
            "status": "success",
            "message": f"Created Item {real_item_code} and linked it to {raq_name}.",
            "item_code": real_item_code,
            "item_name": item_name,
            "match": link.get("match"),
        }
    except Exception as e:
        frappe.log_error("Create Item From RAQ Error", str(e))
        return {"status": "error", "error": str(e)}


def _existing_matches_by_raq(raq_names):
    """Map RAQ name -> existing GL Item Match row."""
    out = {}
    if not raq_names:
        return out
    names = [str(n) for n in raq_names if n]
    if not names:
        return out

    if frappe.db.exists("DocType", GL_ITEM_MATCH_DOCTYPE):
        placeholders = ", ".join(["%s"] * len(names))
        rows = frappe.db.sql(
            f"""
            SELECT name, request_and_quote, item, match_method, match_status, customer, sap, part_number
            FROM `tab{GL_ITEM_MATCH_DOCTYPE}`
            WHERE request_and_quote IN ({placeholders})
            """,
            tuple(names),
            as_dict=True,
        ) or []
        for row in rows:
            key = str(row.get("request_and_quote") or "")
            if key and key not in out:
                out[key] = row

    missing = [n for n in names if n not in out]
    try:
        if missing and frappe.get_meta("Request And Quote").has_field("custom_gl_item"):
            placeholders = ", ".join(["%s"] * len(missing))
            gl_rows = frappe.db.sql(
                f"""
                SELECT name, custom_gl_item
                FROM `tabRequest And Quote`
                WHERE name IN ({placeholders})
                  AND IFNULL(custom_gl_item, '') != ''
                """,
                tuple(missing),
                as_dict=True,
            ) or []
            for g in gl_rows:
                out[str(g.name)] = {
                    "name": "",
                    "request_and_quote": str(g.name),
                    "item": g.custom_gl_item,
                    "match_method": "existing",
                    "match_status": "confirmed",
                    "customer": "",
                    "sap": "",
                    "part_number": "",
                }
    except Exception:
        pass
    return out


def build_raq_item_match_preview(rows):
    """
    Build the mid-stage linking review payload.
    rows: list of dicts with ID plus optional CUSTOMER/SAP/PART_NUMBER/ORIG_PART_NUMBER/REF.
    Does not write anything.
    """
    incoming = rows if isinstance(rows, list) else []
    ids = []
    by_id = {}
    for row in incoming:
        raq_name = str((row or {}).get("ID") or (row or {}).get("name") or "").strip()
        if not raq_name:
            continue
        ids.append(raq_name)
        by_id[raq_name] = row or {}

    if not ids:
        return []

    missing_fields = [
        n for n in ids
        if not (
            (by_id[n].get("CUSTOMER") or by_id[n].get("customer"))
            and (by_id[n].get("SAP") or by_id[n].get("sap"))
            and (
                by_id[n].get("ORIG_PART_NUMBER")
                or by_id[n].get("PART_NUMBER")
                or by_id[n].get("QUOTATION_PART_NUMBER")
                or by_id[n].get("part_number")
            )
        )
    ]
    if missing_fields:
        placeholders = ", ".join(["%s"] * len(missing_fields))
        db_rows = frappe.db.sql(
            f"""
            SELECT
                name as ID,
                ref as REF,
                customer as CUSTOMER,
                sap as SAP,
                part_number as ORIG_PART_NUMBER,
                quotation_part_number as PART_NUMBER,
                quotation_brand as BRAND,
                item as ORIG_ITEM,
                quotation_item as ITEM
            FROM `tabRequest And Quote`
            WHERE name IN ({placeholders})
            """,
            tuple(missing_fields),
            as_dict=True,
        ) or []
        for db_row in db_rows:
            current = by_id.get(str(db_row.ID), {})
            current = dict(current)
            for key in ("REF", "CUSTOMER", "SAP", "ORIG_PART_NUMBER", "PART_NUMBER", "BRAND", "ORIG_ITEM", "ITEM"):
                if not current.get(key) and db_row.get(key):
                    current[key] = db_row.get(key)
            by_id[str(db_row.ID)] = current

    existing = _existing_matches_by_raq(ids)
    preview = []
    for raq_name in ids:
        src = by_id.get(raq_name, {})
        customer = str(src.get("CUSTOMER") or src.get("customer") or "").strip()
        sap = str(src.get("SAP") or src.get("sap") or "").strip()
        part_number = str(
            src.get("ORIG_PART_NUMBER")
            or src.get("QUOTATION_PART_NUMBER")
            or src.get("PART_NUMBER")
            or src.get("part_number")
            or ""
        ).strip()
        candidates = _find_item_candidates(customer, sap, part_number)
        color, unique_items, reason = _classify_matches(
            candidates, sap=sap, customer=customer, part_number=part_number
        )
        current_match = existing.get(raq_name)
        current_item = (current_match or {}).get("item") or ""
        suggested = None
        if current_item:
            suggested = next((c for c in unique_items if c.get("item_code") == current_item), None)
            if not suggested:
                suggested = _item_card(current_item, {
                    "match_method": (current_match or {}).get("match_method") or "existing",
                })
            color = "green" if color != "yellow" else color
            if current_item and not reason:
                reason = "Already linked"
        elif len(unique_items) == 1 and color == "green":
            suggested = unique_items[0]

        default_action = "authorize" if (current_item or (suggested and color == "green")) else "reject"
        pending_item = current_item or ((suggested or {}).get("item_code") if suggested else "")
        preview.append({
            "ID": raq_name,
            "REF": src.get("REF") or "",
            "CUSTOMER": customer,
            "SAP": sap,
            "PART_NUMBER": part_number,
            "ITEM": src.get("ITEM") or src.get("ORIG_ITEM") or "",
            "BRAND": src.get("BRAND") or src.get("QUOTATION_BRAND") or "",
            "color": color,
            "reason": reason,
            "existing_item": current_item,
            "existing_method": (current_match or {}).get("match_method") or "",
            "suggested_item": suggested,
            "candidates": unique_items,
            "pending_item": pending_item,
            "pending_method": (
                (current_match or {}).get("match_method")
                or (suggested or {}).get("match_method")
                or "manual"
            ),
            "action": default_action,
        })
    return preview


@frappe.whitelist()
def preview_raq_item_matches(payload=None):
    """Admin / GL Item Database users: preview links for a list of RAQ rows or IDs."""
    try:
        _require_gl_item_match_access()
        rows = frappe.parse_json(payload) if isinstance(payload, str) else (payload or [])
        if rows and isinstance(rows, list) and rows and not isinstance(rows[0], dict):
            rows = [{"ID": rid} for rid in rows]
        data = build_raq_item_match_preview(rows)
        return {"status": "success", "data": data}
    except Exception as e:
        frappe.log_error("Preview RAQ Item Matches Error", str(e))
        return {"status": "error", "error": str(e), "data": []}


@frappe.whitelist()
def apply_raq_item_match_decisions(payload=None):
    """
    Apply authorize / alter / reject / unlink decisions from the review modal.
    payload = [{ID, action, item_code, match_method}]
    """
    try:
        _require_gl_item_match_access()
        rows = frappe.parse_json(payload) if isinstance(payload, str) else (payload or [])
        linked = 0
        cleared = 0
        skipped = 0
        errors = []
        for row in rows:
            raq_name = str((row or {}).get("ID") or "").strip()
            action = str((row or {}).get("action") or "reject").strip().lower()
            item_code = str((row or {}).get("item_code") or (row or {}).get("pending_item") or "").strip()
            match_method = str((row or {}).get("match_method") or "manual").strip() or "manual"
            if not raq_name:
                continue
            try:
                if action in ("authorize", "alter"):
                    if not item_code:
                        skipped += 1
                        continue
                    res = _save_raq_item_match_impl(raq_name, item_code, match_method, "confirmed")
                    if res.get("status") == "success":
                        linked += 1
                    else:
                        errors.append(f"{raq_name}: {res.get('error')}")
                elif action in ("unlink", "clear"):
                    res = _clear_raq_item_match_impl(raq_name)
                    if res.get("status") == "success":
                        cleared += 1
                    else:
                        errors.append(f"{raq_name}: {res.get('error')}")
                else:
                    skipped += 1
            except Exception as row_err:
                errors.append(f"{raq_name}: {row_err}")
        frappe.db.commit()
        result = {
            "status": "success",
            "linked": linked,
            "cleared": cleared,
            "skipped": skipped,
        }
        if errors:
            result["warnings"] = errors
        return result
    except Exception as e:
        frappe.log_error("Apply RAQ Item Match Decisions Error", str(e))
        return {"status": "error", "error": str(e)}


# ---------------------------------------------------------------------------
# Price Lists tab
# ---------------------------------------------------------------------------

ITEM_PRICE_DEADLINE_CANDIDATES = ("custom_deadline", "deadline")


def _item_price_has_field(fieldname):
    try:
        return bool(frappe.get_meta("Item Price").has_field(fieldname))
    except Exception:
        return False


def _price_list_has_field(fieldname):
    try:
        return bool(frappe.get_meta("Price List").has_field(fieldname))
    except Exception:
        return False


def _ensure_item_price_deadline_field():
    """
    Ensure Item Price has a Datetime deadline field.
    Prefers existing `deadline` or `custom_deadline`; otherwise creates
    Custom Field `custom_deadline` labelled Deadline, inserted after valid_upto.
    """
    for fieldname in ITEM_PRICE_DEADLINE_CANDIDATES:
        if _item_price_has_field(fieldname):
            return fieldname

    try:
        existing = frappe.db.get_value(
            "Custom Field",
            {"dt": "Item Price", "fieldname": ["in", list(ITEM_PRICE_DEADLINE_CANDIDATES)]},
            "fieldname",
        )
        if existing:
            frappe.clear_cache(doctype="Item Price")
            return existing
    except Exception:
        existing = None

    try:
        doc = frappe.get_doc({
            "doctype": "Custom Field",
            "dt": "Item Price",
            "fieldname": "custom_deadline",
            "label": "Deadline",
            "fieldtype": "Datetime",
            "insert_after": "valid_upto",
            "allow_on_submit": 1,
            "translatable": 0,
        })
        doc.flags.ignore_permissions = True
        doc.insert()
        frappe.db.commit()
        frappe.clear_cache(doctype="Item Price")
        return "custom_deadline"
    except Exception as e:
        frappe.log_error("Ensure Item Price Deadline Field Error", str(e))
        for fieldname in ITEM_PRICE_DEADLINE_CANDIDATES:
            if _item_price_has_field(fieldname):
                return fieldname
        return None


def _deadline_select_sql(alias="ip"):
    fieldname = _ensure_item_price_deadline_field()
    if fieldname:
        return f"{alias}.`{fieldname}` as deadline", fieldname
    return "NULL as deadline", None


def _price_list_assignees(price_list_names):
    """Customers / suppliers that use each Price List as their default."""
    out = {name: {"customers": [], "suppliers": []} for name in price_list_names}
    if not price_list_names:
        return out
    placeholders = ", ".join(["%s"] * len(price_list_names))
    try:
        customers = frappe.db.sql(
            f"""
            SELECT name, customer_name, default_price_list
            FROM `tabCustomer`
            WHERE IFNULL(disabled, 0) = 0
              AND IFNULL(default_price_list, '') != ''
              AND default_price_list IN ({placeholders})
            ORDER BY customer_name ASC
            """,
            tuple(price_list_names),
            as_dict=True,
        ) or []
        for row in customers:
            key = row.get("default_price_list")
            if key in out:
                label = row.get("customer_name") or row.get("name")
                out[key]["customers"].append(label)
    except Exception:
        pass
    try:
        suppliers = frappe.db.sql(
            f"""
            SELECT name, supplier_name, default_price_list
            FROM `tabSupplier`
            WHERE IFNULL(disabled, 0) = 0
              AND IFNULL(default_price_list, '') != ''
              AND default_price_list IN ({placeholders})
            ORDER BY supplier_name ASC
            """,
            tuple(price_list_names),
            as_dict=True,
        ) or []
        for row in suppliers:
            key = row.get("default_price_list")
            if key in out:
                label = row.get("supplier_name") or row.get("name")
                out[key]["suppliers"].append(label)
    except Exception:
        pass
    return out


def _format_assignees(bucket):
    customers = bucket.get("customers") or []
    suppliers = bucket.get("suppliers") or []
    assigned = []
    assigned.extend(customers)
    for name in suppliers:
        assigned.append(f"{name} (Supplier)")
    preview = assigned[:6]
    extra = len(assigned) - len(preview)
    text = ", ".join(preview)
    if extra > 0:
        text = f"{text} +{extra} more" if text else f"+{extra} more"
    return {
        "customers": customers,
        "suppliers": suppliers,
        "assigned_to": text,
        "assigned_count": len(assigned),
    }


@frappe.whitelist()
def get_price_lists(
    page=1,
    page_length=50,
    sort_by="PRICE_LIST",
    sort_order="asc",
    search_query=None,
    hide_disabled=1,
):
    """
    Paginated Price List listing for the Price Lists tab.
    Item count, validity range (min valid_from / max valid_upto or deadline),
    and parties that have the list as their default price list.
    """
    try:
        _ensure_item_price_deadline_field()
        page = max(cint(page) or 1, 1)
        page_length = max(cint(page_length) or 50, 1)
        start = (page - 1) * page_length
        hide_disabled = cint(hide_disabled)
        sort_order = "desc" if str(sort_order).lower() == "desc" else "asc"

        deadline_field = None
        for fieldname in ITEM_PRICE_DEADLINE_CANDIDATES:
            if _item_price_has_field(fieldname):
                deadline_field = fieldname
                break

        if deadline_field:
            max_until_expr = (
                f"MAX(GREATEST("
                f"IFNULL(ip.valid_upto, '0001-01-01'), "
                f"IFNULL(DATE(ip.`{deadline_field}`), '0001-01-01')"
                f"))"
            )
            max_deadline_expr = f"MAX(ip.`{deadline_field}`)"
        else:
            max_until_expr = "MAX(ip.valid_upto)"
            max_deadline_expr = "NULL"

        sort_map = {
            "PRICE_LIST": "pl.name",
            "CURRENCY": "pl.currency",
            "ITEM_COUNT": "item_count",
            "VALID_FROM": "valid_from",
            "VALID_UPTO": "valid_upto",
            "DEADLINE": "deadline",
            "ENABLED": "pl.enabled",
        }
        actual_sort = sort_map.get(str(sort_by).upper(), "pl.name")

        where_conds = ["1=1"]
        params = []
        if hide_disabled:
            where_conds.append("IFNULL(pl.enabled, 0) = 1")

        q = (search_query or "").strip()
        if len(q) >= 1:
            like_val = f"%{_escape_like(q)}%"
            where_conds.append(
                """(
                    pl.name LIKE %s
                    OR IFNULL(pl.currency, '') LIKE %s
                    OR EXISTS (
                        SELECT 1 FROM `tabCustomer` c
                        WHERE c.default_price_list = pl.name
                          AND (c.name LIKE %s OR IFNULL(c.customer_name, '') LIKE %s)
                    )
                    OR EXISTS (
                        SELECT 1 FROM `tabSupplier` s
                        WHERE s.default_price_list = pl.name
                          AND (s.name LIKE %s OR IFNULL(s.supplier_name, '') LIKE %s)
                    )
                )"""
            )
            params.extend([like_val, like_val, like_val, like_val, like_val, like_val])

        where_clause = " WHERE " + " AND ".join(where_conds)

        count_sql = f"""
            SELECT COUNT(*) as total
            FROM `tabPrice List` pl
            {where_clause}
        """
        count_row = frappe.db.sql(count_sql, tuple(params), as_dict=True)
        total_records = int(count_row[0]["total"]) if count_row else 0
        total_pages = -(-total_records // page_length) if page_length else 1

        data_sql = f"""
            SELECT
                pl.name as PRICE_LIST,
                pl.currency as CURRENCY,
                pl.enabled as ENABLED,
                pl.buying as BUYING,
                pl.selling as SELLING,
                COUNT(ip.name) as ITEM_COUNT,
                MIN(ip.valid_from) as VALID_FROM,
                MAX(ip.valid_upto) as VALID_UPTO,
                {max_deadline_expr} as DEADLINE,
                {max_until_expr} as VALID_UNTIL
            FROM `tabPrice List` pl
            LEFT JOIN `tabItem Price` ip ON ip.price_list = pl.name
            {where_clause}
            GROUP BY pl.name, pl.currency, pl.enabled, pl.buying, pl.selling
            ORDER BY {actual_sort} {sort_order}, pl.name ASC
            LIMIT %s OFFSET %s
        """
        data_params = list(params) + [page_length, start]
        data = frappe.db.sql(data_sql, tuple(data_params), as_dict=True) or []

        names = [d.get("PRICE_LIST") for d in data if d.get("PRICE_LIST")]
        assignees = _price_list_assignees(names)
        for row in data:
            extra = _format_assignees(assignees.get(row.get("PRICE_LIST")) or {})
            row.update(extra)
            row["ITEM_COUNT"] = cint(row.get("ITEM_COUNT"))
            row["ENABLED"] = cint(row.get("ENABLED"))
            row["BUYING"] = cint(row.get("BUYING"))
            row["SELLING"] = cint(row.get("SELLING"))
            if str(row.get("VALID_UNTIL") or "").startswith("0001-01-01"):
                row["VALID_UNTIL"] = row.get("VALID_UPTO") or row.get("DEADLINE") or ""

        return {
            "status": "success",
            "data": data,
            "total_records": total_records,
            "total_pages": total_pages,
            "deadline_field": deadline_field,
        }
    except Exception as e:
        frappe.log_error("Item Management Get Price Lists Error", str(e))
        return {"status": "error", "error": str(e), "data": []}


@frappe.whitelist()
def get_price_list_items(
    price_list=None,
    page=1,
    page_length=50,
    sort_by="ITEM_CODE",
    sort_order="asc",
    search_query=None,
):
    """Items (Item Price rows) inside one Price List, with brand + part number + deadline."""
    try:
        price_list = (price_list or "").strip()
        if not price_list:
            return {"status": "error", "error": "Price List is required.", "data": []}
        if not frappe.db.exists("Price List", price_list):
            return {"status": "error", "error": f"Price List {price_list} not found.", "data": []}

        deadline_select, deadline_field = _deadline_select_sql("ip")
        page = max(cint(page) or 1, 1)
        page_length = max(cint(page_length) or 50, 1)
        start = (page - 1) * page_length
        sort_order = "desc" if str(sort_order).lower() == "desc" else "asc"

        sort_map = {
            "ITEM_CODE": "ip.item_code",
            "ITEM_NAME": "i.item_name",
            "BRAND": "i.brand",
            "PART_NUMBER": "part_number",
            "RATE": "ip.price_list_rate",
            "VALID_FROM": "ip.valid_from",
            "VALID_UPTO": "ip.valid_upto",
            "DEADLINE": "deadline",
            "CUSTOMER": "ip.customer",
            "UOM": "ip.uom",
        }
        actual_sort = sort_map.get(str(sort_by).upper(), "ip.item_code")

        where_conds = ["ip.price_list = %s"]
        params = [price_list]

        q = (search_query or "").strip()
        pn_field = _item_part_number_field()
        if len(q) >= 1:
            like_val = f"%{_escape_like(q)}%"
            pn_clause = f"OR i.`{pn_field}` LIKE %s" if pn_field else ""
            where_conds.append(
                f"""(
                    ip.item_code LIKE %s
                    OR IFNULL(ip.item_name, '') LIKE %s
                    OR IFNULL(i.item_name, '') LIKE %s
                    OR IFNULL(i.brand, '') LIKE %s
                    OR IFNULL(ip.customer, '') LIKE %s
                    OR IFNULL(ip.supplier, '') LIKE %s
                    {pn_clause}
                )"""
            )
            params.extend([like_val, like_val, like_val, like_val, like_val, like_val])
            if pn_field:
                params.append(like_val)

        where_clause = " WHERE " + " AND ".join(where_conds)
        pn_select = _part_number_select_sql()

        count_sql = f"""
            SELECT COUNT(*) as total
            FROM `tabItem Price` ip
            LEFT JOIN `tabItem` i ON i.name = ip.item_code
            {where_clause}
        """
        count_row = frappe.db.sql(count_sql, tuple(params), as_dict=True)
        total_records = int(count_row[0]["total"]) if count_row else 0
        total_pages = -(-total_records // page_length) if page_length else 1

        data_sql = f"""
            SELECT
                ip.name as ITEM_PRICE_ID,
                ip.item_code as ITEM_CODE,
                IFNULL(i.item_name, ip.item_name) as ITEM_NAME,
                i.image as IMAGE,
                i.brand as BRAND,
                {pn_select},
                ip.price_list_rate as RATE,
                ip.currency as CURRENCY,
                ip.uom as UOM,
                ip.valid_from as VALID_FROM,
                ip.valid_upto as VALID_UPTO,
                {deadline_select},
                ip.customer as CUSTOMER,
                ip.supplier as SUPPLIER,
                ip.buying as BUYING,
                ip.selling as SELLING,
                ip.price_list as PRICE_LIST
            FROM `tabItem Price` ip
            LEFT JOIN `tabItem` i ON i.name = ip.item_code
            {where_clause}
            ORDER BY {actual_sort} {sort_order}, ip.item_code ASC
            LIMIT %s OFFSET %s
        """
        data_params = list(params) + [page_length, start]
        data = frappe.db.sql(data_sql, tuple(data_params), as_dict=True) or []

        missing_pn = [d["ITEM_CODE"] for d in data if d.get("ITEM_CODE") and not d.get("part_number")]
        pn_map = _manufacturer_part_map(missing_pn)
        for row in data:
            if not row.get("part_number"):
                row["part_number"] = pn_map.get(row.get("ITEM_CODE")) or ""
            row["RATE"] = flt(row.get("RATE"))
            row["BUYING"] = cint(row.get("BUYING"))
            row["SELLING"] = cint(row.get("SELLING"))
            row["IMAGE"] = row.get("IMAGE") or ""

        header = frappe.db.get_value(
            "Price List",
            price_list,
            ["name", "currency", "enabled", "buying", "selling"],
            as_dict=True,
        ) or {"name": price_list}
        assignees = _format_assignees(
            (_price_list_assignees([price_list]).get(price_list) or {})
        )

        return {
            "status": "success",
            "data": data,
            "total_records": total_records,
            "total_pages": total_pages,
            "deadline_field": deadline_field,
            "part_number_field": pn_field,
            "price_list": {
                "PRICE_LIST": header.get("name") or price_list,
                "CURRENCY": header.get("currency") or "",
                "ENABLED": cint(header.get("enabled")),
                "BUYING": cint(header.get("buying")),
                "SELLING": cint(header.get("selling")),
                **assignees,
            },
        }
    except Exception as e:
        frappe.log_error("Item Management Get Price List Items Error", str(e))
        return {"status": "error", "error": str(e), "data": []}


@frappe.whitelist()
def delete_price_list(price_list=None):
    """Delete a Price List and its Item Price rows. Clears default_price_list links first."""
    try:
        price_list = str(price_list or "").strip()
        if not price_list:
            return {"status": "error", "error": "Price List is required."}
        if not frappe.db.exists("Price List", price_list):
            return {"status": "error", "error": f"Price List {price_list} not found."}

        customers = frappe.get_all(
            "Customer",
            filters={"default_price_list": price_list},
            pluck="name",
        ) or []
        for name in customers:
            frappe.db.set_value("Customer", name, "default_price_list", None, update_modified=False)

        suppliers = frappe.get_all(
            "Supplier",
            filters={"default_price_list": price_list},
            pluck="name",
        ) or []
        for name in suppliers:
            frappe.db.set_value("Supplier", name, "default_price_list", None, update_modified=False)

        item_prices = frappe.get_all(
            "Item Price",
            filters={"price_list": price_list},
            pluck="name",
        ) or []
        for name in item_prices:
            frappe.delete_doc("Item Price", name, ignore_permissions=True, force=True)

        frappe.delete_doc("Price List", price_list, ignore_permissions=True, force=True)
        frappe.db.commit()
        return {
            "status": "success",
            "message": f"Price List {price_list} deleted.",
            "deleted_item_prices": len(item_prices),
            "cleared_customers": len(customers),
            "cleared_suppliers": len(suppliers),
        }
    except Exception as e:
        frappe.log_error("Item Management Delete Price List Error", str(e))
        return {"status": "error", "error": str(e)}


@frappe.whitelist()
def update_item_price_deadline(item_price_id, deadline=None):
    """Save the Datetime deadline on one Item Price row."""
    try:
        item_price_id = str(item_price_id or "").strip()
        if not item_price_id:
            return {"status": "error", "error": "Item Price is required."}
        if not frappe.db.exists("Item Price", item_price_id):
            return {"status": "error", "error": f"Item Price {item_price_id} not found."}

        fieldname = _ensure_item_price_deadline_field()
        if not fieldname:
            return {"status": "error", "error": "Could not create or find the Deadline field on Item Price."}

        value = str(deadline or "").strip() or None
        frappe.db.set_value("Item Price", item_price_id, fieldname, value, update_modified=True)
        frappe.db.commit()
        return {
            "status": "success",
            "message": "Deadline updated.",
            "deadline_field": fieldname,
            "deadline": value,
        }
    except Exception as e:
        frappe.log_error("Item Management Update Deadline Error", str(e))
        return {"status": "error", "error": str(e)}



@frappe.whitelist()
def suggest_brands(query=""):
    """Return existing Brand names that are similar to the query (for upload suggestion)."""
    try:
        _require_gl_item_match_access()
        q = (query or "").strip()
        if not q or len(q) < 2:
            return {"status": "success", "data": []}
        like = f"%{_escape_like(q)}%"
        rows = frappe.db.sql(
            """
            SELECT name
            FROM `tabBrand`
            WHERE name LIKE %s
            ORDER BY
              CASE WHEN name = %s THEN 0
                   WHEN name LIKE %s THEN 1
                   ELSE 2 END,
              name ASC
            LIMIT 15
            """,
            (like, q, f"{q}%"),
            as_dict=True,
        ) or []
        return {"status": "success", "data": [r.name for r in rows]}
    except Exception as e:
        frappe.log_error("Suggest Brands Error", str(e))
        return {"status": "error", "error": str(e), "data": []}