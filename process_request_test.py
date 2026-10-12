import pandas as pd
import pdfplumber
import re
from datetime import date, datetime, time, timedelta
import frappe
import mysql.connector
import json
import os
import io
import openpyxl
import holidays
from dateutil.parser import parse



# Holidays setup for Peru, Chile, and USA
pe_holidays = holidays.Peru()
cl_holidays = holidays.Chile()
us_holidays = holidays.UnitedStates()

# Mapping for ST to REF prefixes (update with actual ST values from ERPNext)
ST_REF_MAPPING = {
    '208': 'B208-',  # Added for Arauco Chile
    '218': 'A218-',
    '219': 'A219-',  # Added for Arauco Chile
    '306': 'A306-',
    '307': 'A307-',
    '309': 'A309-',
    '314': 'A314-',
    '310': 'A310-',  # Example for Arauco Chile - Replace with actual value
    '311': 'A311-'   # Example for Arauco Argentina - Replace with actual value
}

def parse_contact_name(name):
    parts = name.split()
    if len(parts) == 1:
        return {'first_name': parts[0], 'middle_name': '', 'last_name': '', 'second_last_name': ''}
    elif len(parts) == 2:
        return {'first_name': parts[0], 'middle_name': '', 'last_name': parts[1], 'second_last_name': ''}
    elif len(parts) == 3:
        return {'first_name': parts[0], 'middle_name': parts[1], 'last_name': parts[2], 'second_last_name': ''}
    elif len(parts) == 4:
        return {'first_name': parts[0], 'middle_name': parts[1], 'last_name': parts[2], 'second_last_name': parts[3]}
    else:
        return {'first_name': parts[0], 'middle_name': ' '.join(parts[1:-2]), 'last_name': parts[-2], 'second_last_name': parts[-1]}

def extract_value_after_label(text, label):
    pattern = re.compile(rf"{label}\s+(.+?)(?=\s+-\s*$|\s+-\s*\n|\n\s*[A-Za-z]+|\s*$)", re.DOTALL)
    match = pattern.search(text)
    return match.group(1).strip() if match else ''

def contact_exists(customer, email):
    contacts = frappe.db.sql("""
        SELECT c.name
        FROM `tabContact` c
        INNER JOIN `tabDynamic Link` dl ON c.name = dl.parent AND dl.link_doctype = 'Customer' AND dl.link_name = %s
        WHERE EXISTS (
            SELECT 1 FROM `tabContact Email` ce WHERE ce.parent = c.name AND ce.email_id = %s
        )
    """, (customer, email), as_dict=True)
    return bool(contacts)

def is_holiday(day):
    return day in pe_holidays or day in cl_holidays or day in us_holidays

def is_weekend(day):
    return day.weekday() >= 5

def is_business_day(day):
    return not is_weekend(day) and not is_holiday(day)

def previous_business_day(day):
    day = day - timedelta(days=1)
    while not is_business_day(day):
        day -= timedelta(days=1)
    return day

def _get_db_connection():
    pass


def get_note_for_sap(sap):
    import time as time_mod
    t_start = time_mod.perf_counter()

    if not sap:
        return "-.-||QU:-.-"

    # 1. All historical Request And Quote rows for this SAP (newest first)
    t0 = time_mod.perf_counter()
    raq_results = frappe.get_all(
        "Request And Quote",
        filters={"sap": sap},
        fields=["name", "due_date", "quotation_sales_price", "creation", "rp"],
        order_by="creation DESC"
    )
    raq_time = round(time_mod.perf_counter() - t0, 3)

    if not raq_results:
        return "-.-||QU:-.-"

    raq_ids = [r.name for r in raq_results]

    # 2. Single bulk fetch of any linked Customer Orders
    so_date_field = "date" if frappe.db.has_column("Customer Order", "date") else (
        "transaction_date" if frappe.db.has_column("Customer Order", "transaction_date") else "creation"
    )
    has_so_price = frappe.db.has_column("Customer Order", "order_price_ea")
    so_fields = [so_date_field, "id"]
    if has_so_price:
        so_fields.append("order_price_ea")

    t0 = time_mod.perf_counter()
    orders = frappe.get_all(
        "Customer Order",
        filters={"id": ["in", raq_ids]},
        fields=so_fields,
        order_by="creation DESC"
    )
    order_time = round(time_mod.perf_counter() - t0, 3)

    # First (newest) order wins for each id
    order_map = {}
    for o in orders:
        oid = o.get("id")
        if oid not in order_map:
            order_map[oid] = o

    so_part = "-.-"
    for raq in raq_results:          # already newest-first
        if raq.name in order_map:
            o = order_map[raq.name]
            order_date = o.get(so_date_field)
            month_year = order_date.strftime('%m/%Y') if hasattr(order_date, 'strftime') else 'Unknown'
            price = o.get('order_price_ea', 0) if has_so_price else 0
            so_part = f"SO:{month_year}-{raq.name}-${price}"
            break

    # 3. QU part
    qu_part = "QU:-.-"
    for raq in raq_results:
        sales_price = raq.get("quotation_sales_price")
        quote_date = raq.get("due_date") or raq.get("creation")
        if sales_price is not None and str(sales_price).strip() != "":
            month_year = quote_date.strftime('%m/%Y') if hasattr(quote_date, 'strftime') else 'Unknown'
            qu_part = f"QU:{month_year}-{raq.name}-{sales_price}"
            break
        else:
            if qu_part == "QU:-.-":
                month_year = quote_date.strftime('%m/%Y') if hasattr(quote_date, 'strftime') else 'Unknown'
                qu_part = f"QU:{month_year}-{raq.name}-No price"

    total = round(time_mod.perf_counter() - t_start, 3)

    # Attach detailed timings to the global timings list if it exists
    # (we will collect them via a side-channel in the next step)
    if not hasattr(frappe.local, "_sap_timings"):
        frappe.local._sap_timings = []
    frappe.local._sap_timings.append({
        "sap": sap,
        "raq_query": raq_time,
        "order_query": order_time,
        "total": total,
        "raq_count": len(raq_results),
        "order_count": len(orders)
    })

    return f"{so_part}||{qu_part}"


def _is_same_codelco_customer(customer_name):
    """
    Corporacion Nacional Del Cobre and any customer whose name contains
    CODELCO are the same customer for part-number cross-search.
    """
    if not customer_name:
        return False
    n = str(customer_name).upper()
    return (
        "CODELCO" in n
        or "CORPORACION NACIONAL DEL COBRE" in n
        or "CORPORCACION NACIONAL DEL COBRE" in n
    )


def part_number_cross_search(part_number, current_sap=None, current_customer=None):
    import time as time_mod
    t_start = time_mod.perf_counter()

    if not part_number:
        return None

    pn_val = str(part_number).strip()
    if not pn_val or pn_val.lower() in ["none", "nan", "null"]:
        return None

    try:
        # 1. Historical rows from the unified doctype only
        query = """
            SELECT name AS ID, customer AS CUSTOMER, quotation_sales_price AS SALES_PRICE, brand AS BRAND
            FROM `tabRequest And Quote`
            WHERE part_number = %s
              AND (sap != %s OR sap IS NULL)
        """
        params = [pn_val, current_sap if current_sap else ""]

        if current_customer:
            query += " AND customer NOT LIKE %s"
            params.append(f"%{current_customer}%")

            # Codelco family: historical rows stored as "...CODELCO..." are
            # the same customer, not a cross.
            if _is_same_codelco_customer(current_customer):
                query += " AND IFNULL(customer, '') NOT LIKE %s"
                query += " AND IFNULL(customer, '') NOT LIKE %s"
                params.append("%CODELCO%")
                params.append("%Corporacion Nacional Del Cobre%")

        query += " ORDER BY name DESC LIMIT 50"

        t0 = time_mod.perf_counter()
        pn_history = frappe.db.sql(query, tuple(params), as_dict=True)
        history_time = round(time_mod.perf_counter() - t0, 3)

        if not pn_history:
            return None

        # 2. Single bulk check for which of these IDs already have a Customer Order
        hist_ids = [h["ID"] for h in pn_history]
        order_ids = set()
        order_time = 0.0
        if hist_ids:
            t0 = time_mod.perf_counter()
            order_res = frappe.db.sql(
                """
                SELECT DISTINCT id
                FROM `tabCustomer Order`
                WHERE id IN %s
                """,
                (tuple(hist_ids),),
                as_dict=True
            )
            order_time = round(time_mod.perf_counter() - t0, 3)
            order_ids = {r.id for r in order_res}

        seen = set()
        matches = []

        for hist_rec in pn_history:
            h_id = hist_rec["ID"]
            h_cust = hist_rec.get("CUSTOMER", "")
            sales_price = hist_rec.get("SALES_PRICE")
            brand = hist_rec.get("BRAND", "") or ""

            # Safety net in case a Codelco variant slipped past the SQL filter
            if _is_same_codelco_customer(current_customer) and _is_same_codelco_customer(h_cust):
                continue

            has_valid_quote = _is_valid_price(sales_price)
            has_order = h_id in order_ids

            key = (h_cust.lower().strip(), brand.lower().strip())
            if key in seen:
                continue
            seen.add(key)

            if has_valid_quote or has_order:
                matches.append({
                    "id": h_id,
                    "customer": h_cust,
                    "brand": brand,
                    "has_quote": has_valid_quote,
                    "has_order": has_order
                })

        total = round(time_mod.perf_counter() - t_start, 3)

        if not hasattr(frappe.local, "_pn_timings"):
            frappe.local._pn_timings = []
        frappe.local._pn_timings.append({
            "part_number": pn_val,
            "history_query": history_time,
            "order_query": order_time,
            "total": total,
            "history_count": len(pn_history),
            "match_count": len(matches)
        })

        if not matches:
            return None

        return {
            "part_number": pn_val,
            "found_records": len(matches),
            "matches": matches
        }

    except Exception as e:
        frappe.log_error(f"Error in part_number_cross_search: {str(e)}", "PN Cross Search Error")
        return None

def _is_valid_price(price_val):
    """Checks if a sales price is a real price or just a placeholder/note."""
    if price_val is None:
        return False
    p = str(price_val).upper().strip()
    blacklist = ('W', 'NQ', 'CONSULTA', 'EN ESPERA', 'CSR', 'WEXT', 'Q', 'PENDIENTE', 'NONE', '-', 'OBSOLETO')

    if any(p.startswith(item) for item in blacklist):
        return False
    if p in ["", "0", "0.00"]:
        return False
    return True


def build_cross_search_description_note(cross_result):
    """
    Builds the cross-search summary in the new format:
    /*Cross 1 =ID xxxx Customer Name - Brand xxxxx /// Cross 2 =ID xxxx Customer Name - Brand xxxxx */
    """
    if not cross_result or not cross_result.get('matches'):
        return ''

    matches = cross_result.get('matches', [])
    if not matches:
        return ''

    parts = []
    counter = 1

    for match in matches:
        customer = match.get('customer', '').strip()
        brand = match.get('brand', '').strip()
        match_id = match.get('id', '')

        if not customer:
            continue

        line = f"Cross {counter} ={match_id} {customer}"

        if brand:
            line += f" - Brand {brand}"

        parts.append(line)
        counter += 1

    if not parts:
        return ''

    formatted = " /*" + " /// ".join(parts) + " */"
    return formatted



def get_st_for_customer_div(customer, div):
    """Return the 3-digit ST code of the Address whose address_title matches DIV (case-insensitive)."""
    if not customer or not div:
        return ''
    addresses = frappe.db.sql("""
        SELECT a.st
        FROM `tabAddress` a
        INNER JOIN `tabDynamic Link` dl ON a.name = dl.parent AND dl.link_doctype = 'Customer'
        WHERE LOWER(a.address_title) = LOWER(%s)
          AND LOWER(dl.link_name) = LOWER(%s)
        LIMIT 1
    """, (div, customer), as_dict=True)
    if addresses:
        return resolve_user_st_code(addresses[0].get('st'))
    return ''


def get_st_code_for_ref(customer, div=None):
    """
    Preferred source for the ST segment of REF:
    1. Customer.st (resolved to 3-digit code)
    2. Address whose address_title matches DIV (case-insensitive)
    Returns '' when nothing is found.
    """
    if not customer:
        return ''
    # 1. Customer takes absolute precedence
    cust_st = frappe.db.get_value("Customer", customer, "st")
    code = resolve_user_st_code(cust_st)
    if code:
        return code
    # 2. Fall back to matching shipping address
    if div:
        return get_st_for_customer_div(customer, div)
    return ''


def get_logged_in_user_st_code(user=None):
    """Return the 3-digit ST code of the currently logged-in (or given) User."""
    user = user or frappe.session.user
    if not user or user in ("Guest", "Administrator", "None", ""):
        return ''
    return resolve_user_st_code(user)



def get_contact_details(contact_name, customer=None):
    if not contact_name:
        return None
    contact_name = re.sub(r'\s+', ' ', contact_name.strip()).lower()
    name_parts = contact_name.split()
    if len(name_parts) < 1:
        return None
        
    join_clause = ""
    where_customer = ""
    params_customer = []
    
    if customer:
        join_clause = "INNER JOIN `tabDynamic Link` dl ON dl.parent = c.name"
        where_customer = "AND dl.link_doctype = 'Customer' AND LOWER(dl.link_name) = LOWER(%s)"
        params_customer = [customer]

    first_name = name_parts[0]
    if len(name_parts) == 2:
        last_name = name_parts[1]
        query = f"""
            SELECT c.name, c.email_id, c.st
            FROM `tabContact` c
            {join_clause}
            WHERE LOWER(c.first_name) = %s AND LOWER(c.last_name) = %s
            {where_customer}
        """
        params = [first_name, last_name] + params_customer
        contacts = frappe.db.sql(query, tuple(params), as_dict=True)
        if contacts:
            raw_st = contacts[0].get('st', '')
            return {
                'email_id': contacts[0].get('email_id', ''),
                'st': resolve_user_st_code(raw_st)
            }
        return None
        
    possible_combinations = []
    if len(name_parts) == 3:
        possible_combinations = [
            (name_parts[0], name_parts[1], name_parts[2], ''),
            (name_parts[0], '', name_parts[1], name_parts[2]),
        ]
    elif len(name_parts) == 4:
        possible_combinations = [
            (name_parts[0], name_parts[1], name_parts[2], name_parts[3]),
        ]
        
    for combo in possible_combinations:
        first, middle, last, second_last = combo
        query = f"""
            SELECT c.name, c.email_id, c.st
            FROM `tabContact` c
            {join_clause}
            WHERE LOWER(c.first_name) = %s
            AND (LOWER(c.middle_name) = %s OR c.middle_name IS NULL)
            AND (LOWER(c.last_name) = %s OR c.last_name IS NULL)
            AND (LOWER(c.second_last_name) = %s OR c.second_last_name IS NULL)
            {where_customer}
        """
        params = [first, middle, last, second_last] + params_customer
        contacts = frappe.db.sql(query, tuple(params), as_dict=True)
        if contacts:
            raw_st = contacts[0].get('st', '')
            return {
                'email_id': contacts[0].get('email_id', ''),
                'st': resolve_user_st_code(raw_st)
            }
    return None

def get_contact_details_by_email(email, customer=None):
    if not email:
        return None
        
    join_clause = ""
    where_customer = ""
    params = [email.strip().lower()]
    
    if customer:
        join_clause = "INNER JOIN `tabDynamic Link` dl ON dl.parent = c.name"
        where_customer = "AND dl.link_doctype = 'Customer' AND LOWER(dl.link_name) = LOWER(%s)"
        params.append(customer)

    query = f"""
        SELECT c.name, c.email_id, c.st, c.first_name, c.last_name
        FROM `tabContact` c
        LEFT JOIN `tabContact Email` ce ON ce.parent = c.name
        {join_clause}
        WHERE LOWER(ce.email_id) = %s
        {where_customer}
        LIMIT 1
    """
    contacts = frappe.db.sql(query, tuple(params), as_dict=True)
    if contacts:
        row = contacts[0]
        # Resolve the Link → 3-digit code
        row['st'] = resolve_user_st_code(row.get('st'))
        return row
    return None
    

def remove_thousand_separators(qty_str):
    # This will remove any periods or commas used as thousand separators.
    return qty_str.replace('.', '').replace(',', '')


_PORTAL_ES_MONTHS = {
    "enero": "January",
    "febrero": "February",
    "marzo": "March",
    "abril": "April",
    "mayo": "May",
    "junio": "June",
    "julio": "July",
    "agosto": "August",
    "septiembre": "September",
    "setiembre": "September",
    "octubre": "October",
    "noviembre": "November",
    "diciembre": "December",
}

_PORTAL_ES_WEEKDAYS = (
    "lunes",
    "martes",
    "miercoles",
    "jueves",
    "viernes",
    "sabado",
    "domingo",
)


def parse_portal_datetime(raw, dayfirst=True):
    """
    Parse a Codelco portal datetime.

    dateutil only knows English month names. On a Spanish value such as
    'viernes, 31 julio, 2026 a las 09:43' it skips 'julio', then reads
    '09' from '09:43' as the month. Day 31 of month 9 raises
    'day is out of range for month'.

    Spanish month and weekday words are normalized before parsing.
    Numeric dates such as '31/07/2026 09:43' still parse as before.
    """
    text = str(raw or "").strip()
    if not text or text.lower() in ("nan", "none", "nat"):
        raise ValueError(f"Empty date: {raw}")

    normalized = text.lower()
    normalized = (
        normalized
        .replace("á", "a")
        .replace("é", "e")
        .replace("í", "i")
        .replace("ó", "o")
        .replace("ú", "u")
        .replace("ü", "u")
    )
    for weekday in _PORTAL_ES_WEEKDAYS:
        normalized = re.sub(rf"\b{weekday}\b,?", " ", normalized)
    normalized = re.sub(r"\ba las\b", " ", normalized)
    normalized = re.sub(r"\bde\b", " ", normalized)
    normalized = re.sub(r"\bhrs?\b", " ", normalized)
    for es_month, en_month in sorted(_PORTAL_ES_MONTHS.items(), key=lambda item: len(item[0]), reverse=True):
        normalized = re.sub(rf"\b{es_month}\b", en_month, normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip(" ,")
    return parse(normalized, fuzzy=True, dayfirst=dayfirst)


def process_html_file(file_url, customer, save=False):
    import time as time_mod          # ← renamed so it does not shadow datetime.time
    timings = []                     # list of (label, seconds)
    t0 = time_mod.perf_counter()

    def _mark(label):
        nonlocal t0
        now = time_mod.perf_counter()
        timings.append((label, round(now - t0, 3)))
        t0 = now

    try:
        known_brands_list = [
            "vulco", "warman", "kress", "cocesa", "ifm", "abb", "ab",
            "atlas copco", "general electric", "auma reister", "rosemount"
        ]
        known_brands_set = set()
        for brand in known_brands_list:
            brand_words = tuple(word.lower() for word in brand.split())
            known_brands_set.add(brand_words)
        _mark("1. brand set init")

        file_content = frappe.get_doc("File", {"file_url": file_url}).get_content()
        _mark("2. File.get_content()")

        list_of_df = pd.read_html(file_content)
        _mark("3. pd.read_html()")

        filename = file_url.split('/')[-1]

        ref_match = re.search(r'\d+', filename)
        ref = ref_match.group(0) if ref_match else ''

        intro_df = list_of_df[0]
        header_df = list_of_df[1]
        date_df = list_of_df[2]
        items_df = list_of_df[4]

        # Display the content of items_df row 2, column 0 in red font
        try:
            items_df_field = str(items_df.iloc[2, 0])
            frappe.msgprint(
                f'<span style="color: red; font-weight: bold;">Items_df Row 2, Column 0: {items_df_field}</span>',
                title="Codelco HTML Variation Check"
            )
        except Exception as e:
            frappe.msgprint(
                f'<span style="color: red; font-weight: bold;">Error accessing items_df row 2, column 0: {str(e)}</span>',
                title="Codelco HTML Variation Check"
            )

        if intro_df.iloc[0, 0] == "Introduction":
            language = 'en'
        else:
            language = 'es'

        downloaded_raw = None
        for index, row in intro_df.iterrows():
            for col in intro_df.columns:
                cell = str(row[col])
                match = re.search(r'\[(.*?)\]', cell)
                if match:
                    downloaded_raw = match.group(1)
                    break
            if downloaded_raw:
                break
        if not downloaded_raw:
            frappe.throw("Could not find downloaded date and time in intro_df")

        downloaded_datetime = parse_portal_datetime(downloaded_raw, dayfirst=(language != 'en'))
        downloaded_date = downloaded_datetime.strftime('%Y-%m-%d')
        downloaded_time = downloaded_datetime.strftime('%H:%M:%S')

        published_raw = date_df.iloc[1, 1]
        published_datetime = parse_portal_datetime(published_raw, dayfirst=(language != 'en'))
        published_date = published_datetime.strftime('%Y-%m-%d')
        published_time = published_datetime.strftime('%H:%M:%S')

        current_datetime = datetime.now()
        current_date = current_datetime.strftime('%Y-%m-%d')
        current_time = current_datetime.strftime('%H:%M:%S')

        overview_df = None
        for df in list_of_df:
            if df.apply(lambda row: 'commodity' in str(row).lower() or 'mercancía' in str(row).lower(), axis=1).any():
                overview_df = df
                break

        reference_price = ''
        if overview_df is not None:
            commodity_row = overview_df[overview_df.apply(lambda row: 'commodity' in str(row).lower() or 'mercancía' in str(row).lower(), axis=1)]
            if not commodity_row.empty:
                code_value = commodity_row.iloc[0, 1]
                code_match = re.search(r'M\d+\.\d+\.\d+', str(code_value))
                reference_price = code_match.group(0) if code_match else ''

        # Modified DIV parsing logic: Search for the row with "Regions" or "Regiones" in column 0, then extract value from column 1
        div = ''
        label_to_search = "regions" if language == 'en' else "regiones"
        for index, row in header_df.iterrows():
            label = str(row[0]).lower()
            if label_to_search in label:
                div_raw = str(row[1])
                # Extract after the first whitespace (e.g., remove "TE01 " from "TE01 El Teniente")
                div_search = re.search(r"^\S+\s+(.*)$", div_raw)
                div_final = div_search.group(1).strip() if div_search else div_raw.strip()
                div = div_final.upper() if customer == "Codelco" else div_final
                break

        inco = 'EXW NY'
        notes = 'FT'
        buyer_name = header_df[1][1]
        due_datetime_raw = date_df[1][2]
        frappe.msgprint(f"Raw due datetime: {due_datetime_raw}")

        new_date_str = None
        display_date_str = ''
        due_time_postgre = None
        due_time_display = ''
        try:
            due_dt = parse_portal_datetime(due_datetime_raw, dayfirst=(language != 'en'))
            new_date_str = due_dt.strftime('%Y-%m-%d')
            display_date_str = due_dt.strftime('%m/%d/%Y')
            if re.search(r"\d{1,2}:\d{2}", str(due_datetime_raw)):
                due_time_postgre = due_dt.strftime('%H:%M:%S')
                due_time_display = due_dt.strftime('%H:%M')
            frappe.msgprint(
                f"Parsed due date (ISO): {new_date_str}, Display (mm/dd/yyyy): {display_date_str}, Time: {due_time_postgre or ''}"
            )
        except Exception as e:
            frappe.msgprint(f"No due date found in raw data: {due_datetime_raw} ({e})")

        today = date.today()
        postgre_formated_date = today.strftime('%Y-%m-%d')

        # CHANGED: Offset increased from 15 to 17 to account for 2 new rows (1.6 and 5.2)
        if customer.lower() == 'arauco chile':
            items_list = items_df.iloc[0:]
        else:
            items_list = items_df.iloc[17:]

        # Clean duplicate consecutive item name rows where column 1 is empty/NaN
        items_list = items_list.reset_index(drop=True)
        to_remove = []
        for i in range(1, len(items_list)):
            current_col0 = str(items_list.iloc[i, 0])
            prev_col0 = str(items_list.iloc[i-1, 0])
            current_col1_raw = items_list.iloc[i, 1] if len(items_list.columns) > 1 else np.nan
            if current_col0 == prev_col0 and (pd.isna(current_col1_raw) or str(current_col1_raw).strip() == ''):
                to_remove.append(i)
        items_list = items_list.drop(to_remove).reset_index(drop=True)

        _mark("4. dataframe extraction + all debug msgprints")

        item_counter = 1
        parsed_data = []

        # The old customer/div ST logic is now ignored. Data is fetched directly from the Contact.
        contact_info = get_contact_details(buyer_name, customer)
        email_customer = contact_info.get('email_id') if contact_info else None

        # NEW RULE 1: ST of the Request And Quote always comes from the logged-in user
        st = get_logged_in_user_st_code()
        frappe.log_error(
            f"UPLOAD ST DEBUG | session.user={frappe.session.user} | resolved_st={st} | contact_st={contact_info.get('st') if contact_info else None}",
            "ST Upload Debug"
        )

        contact_match_warn = 'name_mapped' if contact_info else ''

        # NEW RULE 2: REF = category letter + ST-from-Customer-or-matching-Address + "-"
        st_for_ref = get_st_code_for_ref(customer, div)
        cat = (frappe.db.get_value("Customer", customer, "custom_customer_category") or "").strip()
        if st_for_ref:
            ref_prefix = f"{cat}{st_for_ref}-"
        else:
            ref_prefix = ""

        if email_customer is None:
            name_parts = buyer_name.split()
            first_name = name_parts[0] if name_parts else ''
            middle_name = name_parts[1] if len(name_parts) > 2 else ''
            last_name = name_parts[-2] if len(name_parts) > 2 else name_parts[1] if len(name_parts) == 2 else ''
            second_last_name = name_parts[-1] if len(name_parts) > 3 else ''
            contact_data = {
                'customer': customer,
                'first_name': first_name,
                'middle_name': middle_name,
                'last_name': last_name,
                'second_last_name': second_last_name,
                'email': ''
            }
        else:
            contact_data = None

        _mark("5. contact lookup")

        for inda, _ in enumerate(items_list.index):
            cell_name_raw = str(items_list.iloc[inda][0])
            item_number_search = re.search(r"^(\d+)\s", cell_name_raw)

            # Skip attachment links and footer disclaimers, which usually contain '.pdf', 'bases de', or 'información a considerar'
            skip_phrases = [".pdf", "bases de", "información a considerar", "informacion a considerar"]
            is_valid_item = item_number_search and not any(phrase in cell_name_raw.lower() for phrase in skip_phrases)

            if is_valid_item:
                item = item_counter

                # Use specific offsets based on customer formatting
                if customer.lower() == 'arauco chile':
                    sap_raw, description_raw, date_req_raw, qty_unit_raw = '', '', '', ''

                    # Scan dynamically ahead to find the fields, as Ariba table rows vary
                    for offset in range(1, 35):
                        if (inda + offset) >= len(items_list): break
                        row_label = str(items_list.iloc[inda + offset][0]).strip().lower()

                        # Stop scanning if we hit the next item's number
                        if re.search(r"^(\d+)\s", str(items_list.iloc[inda + offset][0])) and "bases de licita" not in row_label:
                            break

                        row_val = str(items_list.iloc[inda + offset][1])
                        if row_val.lower() == 'nan': row_val = ''

                        if row_label == 'material code': sap_raw = row_val
                        elif row_label == 'texto ampliado': description_raw = row_val
                        elif row_label == 'requested delivery date': date_req_raw = row_val
                        elif row_label == 'quantity': qty_unit_raw = row_val

                    # Extract only numbers after zeros and before the first space
                    sap = sap_raw.split()[0].lstrip('0') if sap_raw else ''
                else:
                    sap_raw = str(items_list.iloc[inda + 1][1]) if (inda + 1) < len(items_list) else ''
                    sap = re.sub(r"^0+", '', sap_raw).replace("'", "")
                    description_raw = str(items_list.iloc[inda + 6][1]) if (inda + 6) < len(items_list) else ''
                    date_req_raw = str(items_list.iloc[inda + 7][1]) if (inda + 7) < len(items_list) else ''
                    qty_unit_raw = str(items_list.iloc[inda + 2][1]) if (inda + 2) < len(items_list) else ''

                qty_unit_raw = str(items_list.iloc[inda + 2][1]) if (inda + 2) < len(items_list) else ''

                # ### CHANGE: Corrected regex to capture numbers with periods/commas ###
                qty_search = re.search(r"^([\d,.]+)", qty_unit_raw)

                qty_raw = qty_search.group(0) if qty_search else ''
                qty = remove_thousand_separators(qty_raw)
                unit_parts = qty_unit_raw.split()
                unit = 'EA'
                if len(unit_parts) > 1:
                    unit_raw = unit_parts[1]
                    unit = 'EA' if unit_raw.lower() in ['unidades', 'each'] else unit_raw.upper()

                text = re.sub(r'^\d+\s+', '', cell_name_raw)
                words = text.split()
                part_number = ''
                brand = ''

                if words:
                    if re.match(r'[a-zA-Z0-9]+', words[-1]):
                        part_number = words[-1]
                        pre_words = words[:-1]
                    else:
                        pre_words = words

                    if pre_words:
                        for k in range(len(pre_words), 0, -1):
                            candidate = tuple(word.lower() for word in pre_words[-k:])
                            if candidate in known_brands_set:
                                brand = ' '.join(pre_words[-k:])
                                break

                brand_status = frappe.db.get_value("Brand", brand, "brand_status") if brand else None
                is_banned = brand_status == "BAN"

                item_data = {
                    'REF': ref_prefix,
                    'DATE': postgre_formated_date,
                    'COUNTRY': 'CHILE',
                    'CUSTOMER': customer.upper(),
                    'DIV': div,
                    'CONTACT': buyer_name,
                    'EMAIL_CUSTOMER': email_customer if email_customer else '',
                    'contact_match_warn': contact_match_warn,
                    'CUSTOMER_REF': ref,
                    'DUE_DATE': new_date_str,
                    'DUE_TIME': due_time_postgre,
                    'DISPLAY_DUE_DATE': display_date_str,
                    'DISPLAY_DUE_TIME': due_time_display,
                    'ORIGINAL_DUE_DATE': new_date_str,
                    'ORIGINAL_DUE_TIME': due_time_postgre,
                    'SAP': sap,
                    'ITEM': str(item),
                    'QTY': qty,
                    'UNIT': unit,
                    'PART_NUMBER': part_number,
                    'BRAND': brand,
                    'IS_BANNED': is_banned,
                    'DESCRIPTION': description_raw,
                    'INCOTERM': inco,
                    'ATTACHMENT': '',
                    'NOTE': notes,
                    'SALE-PRICE': None,
                    'REFERENCE_PRICE': reference_price,
                    'DATE REQ': date_req_raw,
                    'RP': '',
                    'ST': st,
                    'DOWNLOADED_DATE': downloaded_date,
                    'DOWNLOADED_TIME': downloaded_time,
                    'PUBLISHED_DATE': published_date,
                    'PUBLISHED_TIME': published_time,
                    'CURRENT_DATE': current_date,
                    'CURRENT_TIME': current_time
                }
                if email_customer is None:
                    item_data['contact_not_found'] = True
                    item_data['contact_data'] = contact_data
                parsed_data.append(item_data)
                item_counter += 1

        _mark("6. item extraction loop finished")

        if parsed_data:
            original_due_date = parsed_data[0]['ORIGINAL_DUE_DATE']
            original_due_time = parsed_data[0]['ORIGINAL_DUE_TIME']
            adjusted = False
            due_date_note = None

            if original_due_date and original_due_time:
                try:
                    due_date_obj = datetime.strptime(original_due_date, '%Y-%m-%d').date()
                    due_time_obj = datetime.strptime(original_due_time, '%H:%M:%S').time()

                    formatted_date = due_date_obj.strftime('%m/%d/%y')
                    formatted_time = due_time_obj.strftime('%-I%p').lower() if due_time_obj.strftime('%-I%p') != '0AM' else '12am'
                    due_date_note = f"DD:{formatted_date}-{formatted_time}"

                    adjusted_due_date = due_date_obj
                    if due_time_obj <= time(12, 0):
                        adjusted_due_date = previous_business_day(due_date_obj)
                        adjusted = True
                    while not is_business_day(adjusted_due_date):
                        adjusted_due_date = previous_business_day(adjusted_due_date)
                        adjusted = True

                    if adjusted:
                        adjusted_date_str = adjusted_due_date.strftime('%Y-%m-%d')
                        adjusted_display_date_str = adjusted_due_date.strftime('%m/%d/%Y')
                        adjusted_due_time = '17:00:00'
                        adjusted_display_due_time = '17:00'
                        for item in parsed_data:
                            item['DUE_DATE'] = adjusted_date_str
                            item['DISPLAY_DUE_DATE'] = adjusted_display_date_str
                            item['DUE_TIME'] = adjusted_due_time
                            item['DISPLAY_DUE_TIME'] = adjusted_display_due_time

                    for item in parsed_data:
                        if due_date_note:
                            current_note = item.get('NOTE', '')
                            item['NOTE'] = f"{current_note} - {due_date_note}" if current_note else due_date_note

                except ValueError as e:
                    frappe.msgprint(f"Error adjusting date/time: {str(e)}")
                    frappe.log_error(f"Invalid date or time format: {original_due_date}, {original_due_time}, Error: {str(e)}")

        _mark("7. before SAP + PN lookups")

        import time as time_mod

        # ---------- Build caches ----------
        t_cache_start = time_mod.perf_counter()
        sap_note_cache = {}
        pn_cross_cache = {}

        for item in parsed_data:
            sap = (item.get("SAP") or "").strip()
            if sap and sap not in sap_note_cache:
                sap_note_cache[sap] = get_note_for_sap(sap)

            pn = (item.get("PART_NUMBER") or "").strip()
            cust = (item.get("CUSTOMER") or "").strip()
            cache_key = (pn, sap, cust)
            if pn and cache_key not in pn_cross_cache:
                pn_cross_cache[cache_key] = part_number_cross_search(
                    part_number=pn,
                    current_sap=sap or None,
                    current_customer=cust or None
                )
        cache_build_time = round(time_mod.perf_counter() - t_cache_start, 3)

        # ---------- Apply results ----------
        t_apply_start = time_mod.perf_counter()
        for item in parsed_data:
            sap = (item.get("SAP") or "").strip()
            if sap:
                sap_note = sap_note_cache.get(sap, "-.-||QU:-.-")
                item["NOTE"] = f"{sap_note} - {item['NOTE']}" if item.get("NOTE") else sap_note

            pn = (item.get("PART_NUMBER") or "").strip()
            cust = (item.get("CUSTOMER") or "").strip()
            cache_key = (pn, sap, cust)
            pn_cross_result = pn_cross_cache.get(cache_key)

            if pn_cross_result and pn_cross_result.get("matches"):
                item["PN_CROSS_SEARCH"] = pn_cross_result
                cross_note = build_cross_search_description_note(pn_cross_result)
                if cross_note:
                    current_desc = item.get("DESCRIPTION") or ""
                    item["DESCRIPTION"] = f"{cross_note} {current_desc}".strip()
        apply_time = round(time_mod.perf_counter() - t_apply_start, 3)

        _mark("8. after SAP + PN lookups")

        # Collect detailed timings
        detailed = {
            "sap_calls": getattr(frappe.local, "_sap_timings", []),
            "pn_calls":  getattr(frappe.local, "_pn_timings", []),
            "cache_build_seconds": cache_build_time,
            "apply_seconds": apply_time,
            "unique_saps": list(sap_note_cache.keys()),
            "unique_pns": list(pn_cross_cache.keys()),
        }

        if hasattr(frappe.local, "_sap_timings"):
            del frappe.local._sap_timings
        if hasattr(frappe.local, "_pn_timings"):
            del frappe.local._pn_timings

        if parsed_data:
            parsed_data[0]["_timings"] = timings
            parsed_data[0]["_total_seconds"] = round(sum(t[1] for t in timings), 3)
            parsed_data[0]["_detailed_sap_pn"] = detailed

        if save:
            saved_ids = []
            try:
                for item in parsed_data:
                    doc = frappe.get_doc({
                        "doctype": "Request For Quote",
                        "ref": str(item.get('REF', '')),
                        "date": str(item.get('DATE', '')),
                        "country": str(item.get('COUNTRY', '')),
                        "customer": str(item.get('CUSTOMER', '')),
                        "div": str(item.get('DIV', '')),
                        "contact": str(item.get('CONTACT', '')),
                        "email_customer": str(item.get('EMAIL_CUSTOMER', '')),
                        "customer_ref": str(item.get('CUSTOMER_REF', '')),
                        "due_date": str(item.get('DUE_DATE', '')) if item.get('DUE_DATE') else None,
                        "due_time": str(item.get('DUE_TIME', '')) if item.get('DUE_TIME') else None,
                        "sap": str(item.get('SAP', '')),
                        "item": str(item.get('ITEM', '')),
                        "qty": str(item.get('QTY', '')) if item.get('QTY') else '0',
                        "unit": str(item.get('UNIT', '')),
                        "part_number": str(item.get('PART_NUMBER', '')),
                        "brand": str(item.get('BRAND', '')),
                        "description": str(item.get('DESCRIPTION', '')),
                        "incoterm": str(item.get('INCOTERM', '')),
                        "attachment": str(item.get('ATTACHMENT', '')),
                        "note": str(item.get('NOTE', '')),
                        "sale_price": str(item.get('SALE-PRICE', '')) if item.get('SALE-PRICE') else None,
                        "reference_price": str(item.get('REFERENCE_PRICE', '')) if item.get('REFERENCE_PRICE') else None,
                        "date_req": str(item.get('DATE REQ', '')) if item.get('DATE REQ') else None,
                        "rp": str(item.get('RP', '')),
                        "st": str(item.get('ST', ''))
                    })
                    doc.insert(ignore_permissions=True)
                    saved_ids.append(doc.name)
            except Exception as e:
                frappe.log_error(f"Error saving parsed data: {str(e)}", "File Save Error")
                frappe.throw(f"Error saving data: {str(e)}")
            return saved_ids
        return parsed_data

    except Exception as e:
        frappe.msgprint(f"Error processing HTML file: {str(e)}")
        frappe.throw(f"Error processing HTML file: {str(e)}")


def process_pdf_file(file_url, customer, save=False):
    frappe.log_error(f"Processing PDF for customer: {customer}", "Customer Debug")
    try:
        file_content = frappe.get_doc("File", {"file_url": file_url}).get_content()
        with pdfplumber.open(io.BytesIO(file_content)) as pdf:
            text = ""
            for page in pdf.pages:
                text += page.extract_text() or ""
        today = date.today()
        postgre_formated_date = today.strftime('%Y-%m-%d')
        country = 'Chile' if customer == 'Arauco Chile' else 'Argentina' if customer == 'Arauco Argentina' else ''
        current_datetime = datetime.now()
        current_date = current_datetime.strftime('%Y-%m-%d')
        current_time = current_datetime.strftime('%H:%M:%S')

        if customer.lower() == 'arauco argentina':
            document_name = file_url.split('/')[-1]
            customer_ref_match = re.search(r'\b\d{10}\b', document_name)
            customer_ref = customer_ref_match.group(0) if customer_ref_match else ''
            customer_doc = frappe.get_doc("Customer", customer) if frappe.db.exists("Customer", customer) else None
            frappe.log_error(f"Customer: {customer}, Customer Doc: {customer_doc.name if customer_doc else 'Not Found'}", "Customer Doc Debug")
            # NEW RULES (Aug 2026)
            # ST of the Request And Quote always comes from the logged-in user
            st = get_logged_in_user_st_code()
            frappe.log_error(
                f"UPLOAD ST DEBUG | session.user={frappe.session.user} | resolved_st={st} | contact_st={contact_info.get('st') if contact_info else None}",
                "ST Upload Debug"
            )

            propietario_match = re.search(r"Propietario\s+(.+)", text)
            propietario = propietario_match.group(1).strip() if propietario_match else ''
            planta_match = re.search(r"Planta\s+(.+)", text)
            div = planta_match.group(1).strip() if planta_match else ''

            contact_info = get_contact_details(propietario, customer)
            email_customer = contact_info.get('email_id') if contact_info else None

            # REF = category letter + ST-from-Customer-or-matching-Address + "-"
            st_for_ref = get_st_code_for_ref(customer, div)
            cat = (frappe.db.get_value("Customer", customer, "custom_customer_category") or "").strip()
            if st_for_ref:
                ref_prefix = f"{cat}{st_for_ref}-"
            else:
                ref_prefix = ""

            # --- MODIFICATION: Disabled contact creation logic for Arauco Argentina ---
            contact_data = None
            # The logic that created 'contact_data' and flagged 'contact_not_found' has been removed.
            # If the contact does not exist, processing will continue without attempting to create one.

            items = re.split(r"3\.2\.1\.\d+", text)
            parsed_data = []
            for i, item_text in enumerate(items[1:], start=1):
                item_text = item_text.strip()
                if not item_text:
                    continue
                description_match = re.search(r"^(.*?)(?=\nCantidad|\Z)", item_text, re.DOTALL)
                description = description_match.group(1).strip() if description_match else ''
                cantidad_match = re.search(r"Cantidad\s+(.+)", item_text)
                cantidad = cantidad_match.group(1).strip() if cantidad_match else ''
                qty, unit = '', ''
                if cantidad:
                    qty_unit_parts = cantidad.split(' ')
                    if len(qty_unit_parts) >= 2:
                        qty_raw = qty_unit_parts[0]
                        qty = remove_thousand_separators(qty_raw)
                        unit = qty_unit_parts[1]
                if not cantidad:
                    continue
                sap_match = re.search(r"Código de material SAP\s+(.+)", item_text)
                sap = sap_match.group(1).strip() if sap_match else ''
                fecha_entrega_match = re.search(r"Fecha de entrega solicitada\s+(.+)", text)
                date_req = fecha_entrega_match.group(1).strip() if fecha_entrega_match else ''
                brand = ''  # Brand extraction logic could be added here if available in PDF
                brand_status = frappe.db.get_value("Brand", brand, "brand_status") if brand else None
                is_banned = brand_status == "BAN"
                item_data = {
                    'REF': ref_prefix,
                    'DATE': postgre_formated_date,
                    'COUNTRY': country,
                    'CUSTOMER': customer,
                    'DIV': div,
                    'CONTACT': propietario,
                    'EMAIL_CUSTOMER': email_customer if email_customer else '',
                    'contact_match_warn': contact_match_warn,
                    'CUSTOMER_REF': customer_ref,
                    'DUE_DATE': None,
                    'DUE_TIME': None,
                    'SAP': sap,
                    'ITEM': str(i),
                    'QTY': qty,
                    'UNIT': unit,
                    'PART_NUMBER': '',
                    'BRAND': brand,
                    'IS_BANNED': is_banned,
                    'DESCRIPTION': description,
                    'INCOTERM': 'EXW',
                    'ATTACHMENT': '',
                    'NOTE': '',
                    'SALE-PRICE': None,
                    'REFERENCE_PRICE': None,
                    'DATE REQ': date_req,
                    'RP': '',
                    'ST': st,
                    'CURRENT_DATE': current_date,
                    'CURRENT_TIME': current_time
                }
                # This check was removed to prevent flagging for new contact creation
                # if email_customer is None:
                #     item_data['contact_not_found'] = True
                #     item_data['contact_data'] = contact_data
                parsed_data.append(item_data)

        elif customer == 'Arauco Chile':
            customer_doc = frappe.get_doc("Customer", customer) if frappe.db.exists("Customer", customer) else None
            frappe.log_error(f"Customer: {customer}, Customer Doc: {customer_doc.name if customer_doc else 'Not Found'}", "Customer Doc Debug")
            # NEW RULES (Aug 2026)
            st = get_logged_in_user_st_code()
            frappe.log_error(
                f"UPLOAD ST DEBUG | session.user={frappe.session.user} | resolved_st={st} | contact_st={contact_info.get('st') if contact_info else None}",
                "ST Upload Debug"
            )

            # We still need div later in the block – keep the existing div extraction
            # (the lines that parse lines / sections stay exactly as they are)

            # REF = category letter + ST-from-Customer-or-matching-Address + "-"
            # Note: at this point in the original code div may not yet be fully known.
            # The final REF is still corrected inside save_data, so we set a safe default here.
            st_for_ref = get_st_code_for_ref(customer, '')   # empty div is fine – Customer.st is preferred
            cat = (frappe.db.get_value("Customer", customer, "custom_customer_category") or "").strip()
            if st_for_ref:
                ref_prefix = f"{cat}{st_for_ref}-"
            else:
                ref_prefix = ""
            lines = text.split('\n')
            customer_ref_match = re.search(r"RFQ:\s*(\d+)", lines[0])
            customer_ref = customer_ref_match.group(1) if customer_ref_match else ''
            sections = re.split(r"RFQ:\s*\d+\s*-\s*Supplier Response", text)[1:]
            parsed_data = []
            first_item = True
            contact = ''
            email_contact = ''
            contact_data = None
            for section in sections:
                section = section.strip()
                if not section:
                    continue
                line_match = re.search(r"Line:\s*(\d+)", section)
                if not line_match:
                    continue
                item = line_match.group(1).lstrip('0')
                sap_match = re.search(r"Buyer Part ID:\s*([A-Z0-9]+)", section)
                sap = sap_match.group(1).lstrip('0') if sap_match else ''
                qty_unit_match = re.search(r"Quantity\s+(\d+)(?:\s*(\w+))?", section)
                if qty_unit_match:
                    qty_raw = qty_unit_match.group(1)
                    qty = remove_thousand_separators(qty_raw)
                    unit = qty_unit_match.group(2) if qty_unit_match.group(2) else 'EA'
                else:
                    qty = '1'
                    unit = 'EA'
                date_req_match = re.search(r"Requested Delivery Date\s+(.+)", section)
                date_req = date_req_match.group(1).strip() if date_req_match else ''
                description = ''
                start_patterns = [r"Precio sin Descuento\s+0"]
                end_patterns = [
                    r"Proveedor", r"Correo Comprador", r"Empresa Representada",
                    r"Garantía", r"Marca", r"Nombre Comprador", r"Peso Bruto Total \(Kg\)",
                    r"Precio sin Descuento", r"Teléfono Comprador", r"Validez de la Oferta \(dias\)",
                    r"Volumen \(m3\)", r"RFQ:\s*\d+\s*-\s*Supplier Response"
                ]
                start_match = re.search("|".join(start_patterns), section)
                if start_match:
                    description_start = start_match.end()
                    end_match = re.search("|".join(end_patterns), section[description_start:])
                    description_end = (description_start + end_match.start()) if end_match else len(section)
                    description = section[description_start:description_end].strip()
                    description = re.sub(r'^\s*-\s*', '', description)
                    description_lines = description.split('\n')
                    cleaned_description = [line for line in description_lines if
                                           not any(re.match(pattern, line.strip()) for pattern in end_patterns)]
                    description = '\n'.join(cleaned_description).strip()
                if first_item:
                    contact = extract_value_after_label(section, "Nombre Comprador")
                    email_contact = extract_value_after_label(section, "Correo Comprador")

                    # --- MODIFICATION: Disabled automatic contact creation for Arauco Chile ---
                    # The following block that automatically creates a contact has been commented out.
                    # if not contact_exists(customer, email_contact):
                    #     name_parts = parse_contact_name(contact)
                    #     contact_doc = frappe.get_doc({
                    #         "doctype": "Contact",
                    #         "first_name": name_parts['first_name'],
                    #         "middle_name": name_parts['middle_name'],
                    #         "last_name": name_parts['last_name'],
                    #         "second_last_name": name_parts['second_last_name'],
                    #         "email_ids": [{
                    #             "email_id": email_contact,
                    #             "is_primary": 1
                    #         }],
                    #         "links": [{
                    #             "link_doctype": "Customer",
                    #             "link_name": customer
                    #         }]
                    #     })
                    #     contact_doc.insert(ignore_permissions=True)
                    # --- END OF DISABLED BLOCK ---

                    first_item = False
                brand = ''  # Brand extraction logic could be added here if available in PDF
                brand_status = frappe.db.get_value("Brand", brand, "brand_status") if brand else None
                is_banned = brand_status == "BAN"
                item_data = {
                    'REF': ref_prefix,
                    'DATE': postgre_formated_date,
                    'COUNTRY': country,
                    'CUSTOMER': customer,
                    'DIV': '',
                    'CONTACT': contact,
                    'EMAIL_CUSTOMER': email_contact,
                    'CUSTOMER_REF': customer_ref,
                    'DUE_DATE': None,
                    'DUE_TIME': None,
                    'SAP': sap,
                    'ITEM': item,
                    'QTY': qty,
                    'UNIT': unit,
                    'PART_NUMBER': '',
                    'BRAND': brand,
                    'IS_BANNED': is_banned,
                    'DESCRIPTION': description,
                    'INCOTERM': 'EXW',
                    'ATTACHMENT': '',
                    'NOTE': '',
                    'SALE-PRICE': None,
                    'REFERENCE_PRICE': None,
                    'DATE REQ': date_req,
                    'RP': '',
                    'ST': st,
                    'CURRENT_DATE': current_date,
                    'CURRENT_TIME': current_time
                }
                parsed_data.append(item_data)

        # =========================================================
        # --- NEW ASMAR LOGIC BLOCK ---
        # =========================================================
        elif customer.lower() == 'asmar':
            country = 'Chile'
            customer_name_mapped = 'Asmar'
            document_name = file_url.split('/')[-1]

            # NEW RULES (Aug 2026)
            st = get_logged_in_user_st_code()

            # Extract DIV first – it is required by get_st_code_for_ref
            div_match = re.search(r"Astilleros y Maestranzas de La ARMADA.*?\n(.*?)\s+N[°º]\s*Sol", text, re.IGNORECASE)
            div = div_match.group(1).strip() if div_match else ''

            # REF = category letter + ST-from-Customer-or-matching-Address + "-"
            st_for_ref = get_st_code_for_ref(customer_name_mapped, div)
            cat = (frappe.db.get_value("Customer", customer_name_mapped, "custom_customer_category") or "").strip()
            if st_for_ref:
                ref_prefix = f"{cat}{st_for_ref}-"
            else:
                ref_prefix = ""

            # Asmar never builds a contact_info dict; keep the debug log safe
            contact_info = None
            frappe.log_error(
                f"UPLOAD ST DEBUG | session.user={frappe.session.user} | resolved_st={st} | contact_st={contact_info.get('st') if contact_info else None}",
                "ST Upload Debug"
            )

            contact_match = re.search(r"comunicarse con:\s*\n(.*?)\s+Fono", text, re.IGNORECASE)
            contact = contact_match.group(1).strip() if contact_match else ''

            email_match = re.search(r"Email:\s*([^\s]+)", text, re.IGNORECASE)
            email_customer = email_match.group(1).strip() if email_match else ''

            ref_match = re.search(r"N[°º]\s*Sol.*?\n.*?(\d+)\s*/\s*(\d+)", text, re.IGNORECASE)
            customer_ref = ref_match.group(2).strip() if ref_match else ''

            due_date, due_time = None, None
            display_due_date, display_due_time = '', ''

            due_match = re.search(r"hasta el:\s*(\d{2}-\d{2}-\d{4})\s*(\d{1,2}:\d{2}(?::\d{2})?)", text, re.IGNORECASE)
            if due_match:
                raw_date = due_match.group(1)
                raw_time = due_match.group(2)

                try:
                    dt_obj = datetime.strptime(raw_date, "%d-%m-%Y")
                    due_date = dt_obj.strftime("%Y-%m-%d")
                    display_due_date = dt_obj.strftime("%m/%d/%Y")
                except ValueError:
                    due_date = raw_date
                    display_due_date = raw_date

                try:
                    if raw_time.count(':') == 2:
                        time_obj = datetime.strptime(raw_time, "%H:%M:%S")
                    else:
                        time_obj = datetime.strptime(raw_time, "%H:%M")
                    due_time = time_obj.strftime("%H:%M:%S")
                    display_due_time = time_obj.strftime("%H:%M")
                except ValueError:
                    due_time = raw_time
                    display_due_time = raw_time

            # --- APPLY PRIOR BUSINESS DAY LOGIC ---
            original_note = ""
            if due_date and due_time:
                original_note = f"NEW RFQ/REQ FT/VEN {display_due_date} {display_due_time}"
                try:
                    dt_obj = datetime.strptime(due_date, "%Y-%m-%d").date()
                    adjusted_date = dt_obj - timedelta(days=1)

                    # Loop backwards until we hit a valid business day
                    while not is_business_day(adjusted_date):
                        adjusted_date -= timedelta(days=1)

                    due_date = adjusted_date.strftime("%Y-%m-%d")
                    display_due_date = adjusted_date.strftime("%m/%d/%Y")
                    due_time = "15:00:00"
                    display_due_time = "15:00"
                except Exception as e:
                    frappe.log_error(f"Date adjustment error: {e}", "Asmar Date Adjust")

            items_data = []
            text_with_spaces = ""

            # Re-read PDF preserving visual spaces to detect indentations
            with pdfplumber.open(io.BytesIO(file_content)) as pdf_temp:
                for page in pdf_temp.pages:
                    text_with_spaces += page.extract_text(keep_blank_chars=True) or ""

            table_match = re.search(r"Total por [ií]tem(.*?)(?:Proveedor Nacional|Esta solicitud|RESTRICCIONES)", text_with_spaces, re.DOTALL | re.IGNORECASE)

            if table_match:
                table_text = table_match.group(1).strip('\n')
                lines = table_text.split('\n')
                current_item = None
                last_parsed_item_num = 0

                unit_pattern = r"(NUMERO|PAQUETE|METRO|JUEGO|KILOGRAMO|LITRO|PAR|CONJUNTO|MILLAR|KIT|C/U|CJTO|MT|MTS|KG|EA|UNIDAD)"

                for line in lines:
                    # Skip empty blank lines, but calculate spaces on lines with content
                    if not line.strip(): continue
                    leading_spaces = len(line) - len(line.lstrip())
                    clean_line = line.strip()

                    extracted_unit = None
                    extracted_qty = None

                    # 1. Safely extract Unit and QTY anywhere in the line
                    # FIX: Removed the extra parenthesis around {unit_pattern} to prevent nested groups
                    u_match = re.search(rf"\b{unit_pattern}\s+([\d\.,]+)\s*[\W_]*$", clean_line, re.IGNORECASE)

                    if u_match:
                        extracted_unit = u_match.group(1).upper()
                        extracted_qty = u_match.group(2).replace('.', '').replace(',', '')
                        # Delete the unit/qty string from the description safely
                        clean_line = clean_line[:u_match.start()].strip()
                    else:
                        # Fallback in case PDF extracts them backwards (Qty then Unit)
                        u_match_rev = re.search(rf"\b([\d\.,]+)\s+{unit_pattern}\s*[\W_]*$", clean_line, re.IGNORECASE)
                        if u_match_rev:
                            extracted_qty = u_match_rev.group(1).replace('.', '').replace(',', '')
                            extracted_unit = u_match_rev.group(2).upper()
                            clean_line = clean_line[:u_match_rev.start()].strip()

                    # 2. Check if the line is a genuinely new Item entry
                    match_new = re.match(r"^(\d+)(?:\s+(.+))?$", clean_line)
                    is_new_item = False

                    # Must start with a number AND be flush to the left margin (< 4 spaces)
                    if match_new and leading_spaces < 4:
                        matched_num = int(match_new.group(1))
                        # Sequence Validation: Must be greater than the last item
                        if matched_num > last_parsed_item_num:
                            is_new_item = True

                    if is_new_item:
                        if current_item:
                            items_data.append(current_item)

                        last_parsed_item_num = matched_num
                        text_part = match_new.group(2) or ""
                        current_item = {
                            'raw_text': text_part + " ",
                            'unit': extracted_unit,
                            'qty': extracted_qty
                        }
                    else:
                        # Rejected sequence or indented text -> Treat as continuation of description
                        if current_item:
                            current_item['raw_text'] += clean_line + " "
                            if extracted_unit and not current_item.get('unit'): current_item['unit'] = extracted_unit
                            if extracted_qty and not current_item.get('qty'): current_item['qty'] = extracted_qty

                # Final flush
                if current_item:
                    items_data.append(current_item)

                # Normalize the dictionaries
                for itm in items_data:
                    if not itm.get('unit'): itm['unit'] = 'EA'
                    if not itm.get('qty'): itm['qty'] = '1'
                    itm['unit'] = "EA" if itm['unit'] == "NUMERO" else itm['unit']
                    itm['desc'] = re.sub(r'\s+', ' ', itm['raw_text']).strip()

            if not items_data:
                items_data.append({'desc': '', 'unit': 'EA', 'qty': '1'})

            parsed_data = []
            for idx, data in enumerate(items_data):
                brand = ''
                brand_status = frappe.db.get_value("Brand", brand, "brand_status") if brand else None
                is_banned = brand_status == "BAN"

                item_payload = {
                    'REF': ref_prefix,
                    'DATE': postgre_formated_date,
                    'COUNTRY': country,
                    'CUSTOMER': customer_name_mapped,
                    'DIV': div,
                    'CONTACT': contact,
                    'EMAIL_CUSTOMER': email_customer,
                    'CUSTOMER_REF': customer_ref,
                    'DUE_DATE': due_date,
                    'DUE_TIME': due_time,
                    'DISPLAY_DUE_DATE': display_due_date,
                    'DISPLAY_DUE_TIME': display_due_time,
                    'ORIGINAL_DUE_DATE': due_date,
                    'ORIGINAL_DUE_TIME': due_time,
                    'SAP': '',
                    'ITEM': str(idx + 1),  # Forces 1, 2, 3 sequential numbering
                    'QTY': data['qty'],
                    'UNIT': data['unit'],
                    'PART_NUMBER': '',
                    'BRAND': brand,
                    'IS_BANNED': is_banned,
                    'DESCRIPTION': data['desc'],
                    'INCOTERM': 'EXW NY',
                    'ATTACHMENT': '',
                    'NOTE': original_note,
                    'SALE-PRICE': None,
                    'REFERENCE_PRICE': None,
                    'DATE REQ': '',
                    'RP': '',
                    'ST': st,
                    'CURRENT_DATE': current_date,
                    'CURRENT_TIME': current_time,
                    'FILE': document_name
                }
                parsed_data.append(item_payload)
        # =========================================================
        # --- END ASMAR LOGIC BLOCK ---
        # =========================================================

        # =========================================================
        # --- NEW SHOUGANG PERU LOGIC BLOCK ---
        # =========================================================
        elif customer.lower() == 'shougang peru':
            # 1. SET DEFAULT VALUES
            country = 'Peru'
            customer_name_mapped = 'Shougang Peru'
            document_name = file_url.split('/')[-1]

            # NEW RULES (Aug 2026)
            st = get_logged_in_user_st_code()
            frappe.log_error(
                f"UPLOAD ST DEBUG | session.user={frappe.session.user} | resolved_st={st} | contact_st={contact_info.get('st') if contact_info else None}",
                "ST Upload Debug"
            )

            # REF will be set after we know DIV (Shougang currently leaves DIV empty,
            # so Customer.st will be used if present)
            st_for_ref = get_st_code_for_ref(customer_name_mapped, '')
            cat = (frappe.db.get_value("Customer", customer_name_mapped, "custom_customer_category") or "").strip()
            if st_for_ref:
                ref_prefix = f"{cat}{st_for_ref}-"
            else:
                ref_prefix = ""

            # 2. EXTRACT HEADER DATA VIA TEXT REGEX
            contact_match = re.search(r"COMPRADOR\s+(.+)", text, re.IGNORECASE)
            contact = contact_match.group(1).strip() if contact_match else ''

            email_customer = '' # Email is not present in standard Shougang headers

            ref_match = re.search(r"SOLPED\s+(\d+)", text, re.IGNORECASE)
            customer_ref = ref_match.group(1).strip() if ref_match else ''

            due_date, due_time = None, '12:00:00'
            display_due_date, display_due_time = '', '12:00'
            due_match = re.search(r"PLAZO DE ENTREGA DE OFERTA\s+(\d{2}/\d{2}/\d{4})", text, re.IGNORECASE)
            if due_match:
                try:
                    dt_obj = datetime.strptime(due_match.group(1), "%d/%m/%Y")
                    due_date = dt_obj.strftime("%Y-%m-%d")         # DB format
                    display_due_date = dt_obj.strftime("%m/%d/%Y") # HTML UI format
                except ValueError:
                    due_date = due_match.group(1)
                    display_due_date = due_match.group(1)

            # 3. EXTRACT TABLE ITEMS VIA PDFPLUMBER
            items_data = []

            with pdfplumber.open(io.BytesIO(file_content)) as pdf_temp:
                for page in pdf_temp.pages:
                    tables = page.extract_tables()
                    for table in tables:
                        if not table or len(table) < 2: continue

                        # Check if this is the Shougang items table
                        headers = [str(h).lower().replace('\n', ' ') for h in table[0] if h]
                        if any('codigo sap' in h for h in headers) or any('texto breve' in h for h in headers):
                            current_item = None
                            item_counter = 1

                            for row in table[1:]:
                                if not any(row): continue

                                col0 = str(row[0]).strip() if len(row) > 0 and row[0] else ''

                                # A new item row starts with a numeric code like '00010'
                                if col0.isdigit():
                                    if current_item:
                                        items_data.append(current_item)

                                    qty_raw = str(row[1]).strip() if len(row) > 1 and row[1] else ''
                                    sap = str(row[2]).strip() if len(row) > 2 and row[2] else ''
                                    desc = str(row[4]).replace('\n', ' ').strip() if len(row) > 4 and row[4] else ''
                                    pn = str(row[5]).strip() if len(row) > 5 and row[5] else ''
                                    unit_raw = str(row[6]).strip() if len(row) > 6 and row[6] else ''
                                    date_req = str(row[8]).replace('\n', ' ').strip() if len(row) > 8 and row[8] else ''

                                    qty = qty_raw.replace('.', '').replace(',', '') if qty_raw else '1'
                                    unit = 'EA' if unit_raw.upper() in ['C/U', 'UND'] else unit_raw.upper()

                                    current_item = {
                                        'item': str(item_counter),
                                        'qty': qty,
                                        'sap': sap,
                                        'desc': desc,
                                        'unit': unit,
                                        'pn': pn,
                                        'date_req': date_req
                                    }
                                    item_counter += 1
                                elif current_item and col0:
                                    # This handles extended descriptions that drop down to the next row
                                    # Stop parsing if we hit the footer terms and conditions
                                    if "IMPORTANTE:" in col0 or "SOLO SE CONSIDERARAN" in col0 or "CONDICIONES" in col0:
                                        break
                                    # Skip header-like filler labels like "TEXTO PEDIDO DE COMPRAS"
                                    if col0 not in ['TEXTO PEDIDO DE COMPRAS :', 'TEXTO POSICION :']:
                                        current_item['desc'] += " \n" + col0.replace('\n', ' ').strip()

                            if current_item:
                                items_data.append(current_item)

            # --- SAFETY NET ---
            if not items_data:
                items_data.append({'item': '1', 'qty': '1', 'sap': '', 'desc': '', 'unit': 'EA', 'pn': '', 'date_req': ''})

            # 4. BUILD THE PARSED DATA DICTIONARY
            parsed_data = []
            for data in items_data:
                brand = ''
                brand_status = frappe.db.get_value("Brand", brand, "brand_status") if brand else None
                is_banned = brand_status == "BAN"

                item_payload = {
                    'REF': ref_prefix,
                    'DATE': postgre_formated_date,
                    'COUNTRY': country,
                    'CUSTOMER': customer_name_mapped,
                    'DIV': '',
                    'CONTACT': contact,
                    'EMAIL_CUSTOMER': email_customer,
                    'CUSTOMER_REF': customer_ref,
                    'DUE_DATE': due_date,
                    'DUE_TIME': due_time,
                    'DISPLAY_DUE_DATE': display_due_date,  # Added for UI
                    'DISPLAY_DUE_TIME': display_due_time,  # Added for UI
                    'ORIGINAL_DUE_DATE': due_date,         # Added for UI
                    'ORIGINAL_DUE_TIME': due_time,         # Added for UI
                    'SAP': data['sap'],
                    'ITEM': data['item'],
                    'QTY': data['qty'],
                    'UNIT': data['unit'],
                    'PART_NUMBER': data['pn'],
                    'BRAND': brand,
                    'IS_BANNED': is_banned,
                    'DESCRIPTION': data['desc'],
                    'INCOTERM': 'EXW NY',
                    'ATTACHMENT': '',
                    'NOTE': '',
                    'SALE-PRICE': None,
                    'REFERENCE_PRICE': None,
                    'DATE REQ': data['date_req'],
                    'RP': '',
                    'ST': st,
                    'CURRENT_DATE': current_date,
                    'CURRENT_TIME': current_time,
                    'FILE': document_name
                }
                parsed_data.append(item_payload)
        # =========================================================
        # --- END SHOUGANG PERU LOGIC BLOCK ---
        # =========================================================





        else:
            frappe.throw(f"Unsupported customer: {customer}")

        sap_note_cache = {}
        pn_cross_cache = {}          # key = (part_number, sap, customer)

        for item in parsed_data:
            sap = (item.get("SAP") or "").strip()
            if sap and sap not in sap_note_cache:
                sap_note_cache[sap] = get_note_for_sap(sap)

            pn = (item.get("PART_NUMBER") or "").strip()
            cust = (item.get("CUSTOMER") or "").strip()
            cache_key = (pn, sap, cust)
            if pn and cache_key not in pn_cross_cache:
                pn_cross_cache[cache_key] = part_number_cross_search(
                    part_number=pn,
                    current_sap=sap or None,
                    current_customer=cust or None
                )

        # Apply the pre-computed results
        for item in parsed_data:
            sap = (item.get("SAP") or "").strip()
            if sap:
                sap_note = sap_note_cache.get(sap, "-.-||QU:-.-")
                item["NOTE"] = f"{sap_note} - {item['NOTE']}" if item.get("NOTE") else sap_note

            pn = (item.get("PART_NUMBER") or "").strip()
            cust = (item.get("CUSTOMER") or "").strip()
            cache_key = (pn, sap, cust)
            pn_cross_result = pn_cross_cache.get(cache_key)

            if pn_cross_result and pn_cross_result.get("matches"):
                item["PN_CROSS_SEARCH"] = pn_cross_result
                cross_note = build_cross_search_description_note(pn_cross_result)
                if cross_note:
                    current_desc = item.get("DESCRIPTION") or ""
                    item["DESCRIPTION"] = f"{cross_note} {current_desc}".strip()

        if save:
            conn = _get_db_connection()
            cursor = conn.cursor()
            saved_ids = []
            try:
                for item in parsed_data:
                    fields = [k for k in item.keys() if item[k] is not None and k not in ['contact_not_found', 'contact_data', 'CURRENT_DATE', 'CURRENT_TIME', 'IS_BANNED', 'DISPLAY_DUE_DATE', 'DISPLAY_DUE_TIME', 'ORIGINAL_DUE_DATE', 'ORIGINAL_DUE_TIME']]
                    values = [item[k] for k in fields]
                    placeholders = ", ".join(["%s"] * len(values))
                    query = f"INSERT INTO `request for quote` ({', '.join(fields)}) VALUES ({placeholders})"
                    cursor.execute(query, tuple(values))
                    saved_ids.append(cursor.lastrowid)
                conn.commit()
            except Exception as e:
                conn.rollback()
                frappe.throw(f"Error saving PDF data: {str(e)}")
            finally:
                cursor.close()
                conn.close()
            return saved_ids
        return parsed_data
    except Exception as e:
        frappe.throw(f"Error processing PDF file: {str(e)}")

def process_excel_file(file_url, customer, save=False):
    try:
        frappe.log_error("=== process_excel_file STARTED ===", "Upload Debug")
        file_content = frappe.get_doc("File", {"file_url": file_url}).get_content()
        # Using data_only=True ensures formulas are evaluated into values
        wb = openpyxl.load_workbook(io.BytesIO(file_content), data_only=True)
        ws = wb.active  # Selects the first/currently active sheet

        records = []
        headers = []

        REQUIRED_COLUMNS = {
            'REF', 'CUSTOM_SALES_STATUS', 'DATE', 'COUNTRY', 'CUSTOMER', 'DIV',
            'CONTACT', 'EMAIL_CUSTOMER', 'CUSTOMER_REF', 'DUE_DATE', 'DUE_TIME',
            'SAP', 'ITEM', 'QTY', 'UNIT', 'PART_NUMBER', 'BRAND', 'DESCRIPTION',
            'INCOTERM', 'NOTE', 'SALE-PRICE', 'REFERENCE_PRICE', 'DATE REQ', 'ST'
        }

        # Sales-view export columns that must never block an import.
        # They are display-only on Sales and are not written back to Request And Quote.
        IGNORED_IMPORT_COLUMNS = {
            'SALES_PRICE',              # old export header for Quote Price
            'QUOTATION_SALES_PRICE',    # new export header for Quote Price
            'PROCUREMENT_STATUS',       # Proc. Status
            'QUOTE_PRICE',
            'PROC_STATUS'
        }

        # Helper: normalise header for comparison
        def _norm(h):
            if not h:
                return ''
            return h.upper().replace('-', '_').replace(' ', '_')

        # Safe date normalizer (no import inside the hot path)
        def normalize_excel_date(value):
            if value is None or str(value).strip() == '':
                return None

            # Already a date/datetime object from openpyxl
            if isinstance(value, (datetime, date)):
                return value.strftime('%Y-%m-%d')

            value_str = str(value).strip()

            # Try the most common formats first
            for fmt in ('%m/%d/%Y', '%d/%m/%Y', '%Y-%m-%d', '%m-%d-%Y', '%d-%m-%Y'):
                try:
                    return datetime.strptime(value_str, fmt).strftime('%Y-%m-%d')
                except ValueError:
                    continue

            # Optional fallback using dateutil (only if available)
            try:
                from dateutil.parser import parse as dateutil_parse
                return dateutil_parse(value_str, dayfirst=False).strftime('%Y-%m-%d')
            except Exception:
                return None

        # Safe time normalizer – forces every value into clean HH:MM:SS for the Time field
        def normalize_excel_time(value):
            if value is None or (isinstance(value, str) and value.strip() == ''):
                return None

            from datetime import time as dt_time

            # Already a pure time object
            if isinstance(value, dt_time):
                return value.strftime('%H:%M:%S')

            # datetime object → take only the time part
            if isinstance(value, datetime):
                return value.time().strftime('%H:%M:%S')

            # Excel serial time (float / int = fraction of a day)
            if isinstance(value, (int, float)):
                try:
                    # Convert fraction-of-day → total seconds, keep only the time-of-day
                    total_seconds = int(round(float(value) * 24 * 3600)) % (24 * 3600)
                    hours   = total_seconds // 3600
                    minutes = (total_seconds % 3600) // 60
                    seconds = total_seconds % 60
                    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
                except Exception:
                    return None

            # String – try the formats we actually see in real files
            value_str = str(value).strip()

            for fmt in (
                '%H:%M:%S',
                '%H:%M:%S.%f',          # with milliseconds
                '%I:%M:%S %p',          # 6:23:56 AM
                '%I:%M:%S.%f %p',
                '%H:%M',
                '%I:%M %p',
                '%H:%M:%S %p',
                '%I:%M:%S%p',           # no space before AM/PM
                '%I:%M%p',
            ):
                try:
                    return datetime.strptime(value_str, fmt).strftime('%H:%M:%S')
                except ValueError:
                    continue

            # Last-resort free-form parse
            try:
                from dateutil.parser import parse as dateutil_parse
                return dateutil_parse(value_str).strftime('%H:%M:%S')
            except Exception:
                return None

        # Pre-load the allowed options for the Select field (once)
        def get_custom_sales_status_options():
            meta = frappe.get_meta("Request And Quote")
            field = meta.get_field("custom_sales_status")
            if not field or not field.options:
                return set()
            return {opt.strip() for opt in field.options.split("\n") if opt.strip()}

        allowed_status_options = get_custom_sales_status_options()

        # Iterate through rows dynamically to handle flat tables
        for row in ws.iter_rows(values_only=True):
            # Skip entirely empty rows
            if not any(row):
                continue

            # The first non-empty row is treated as the header row
            if not headers:
                headers = [str(cell).strip() if cell is not None else '' for cell in row]

                # ---------- COLUMN VALIDATION (supports both Create and Update modes) ----------
                normalised_headers = {_norm(h) for h in headers if h}

                # Detect update mode: presence of an ID column
                has_id_column = 'ID' in normalised_headers

                # Never allow a raw "NAME" column (Frappe reserved)
                if 'NAME' in normalised_headers:
                    frappe.throw(
                        "The Excel file must not contain a 'NAME' column. "
                        "Use 'ID' when you want to update existing records."
                    )

                ALLOWED_COLUMNS = REQUIRED_COLUMNS | {'ID'}
                allowed_normalised = {_norm(c) for c in ALLOWED_COLUMNS}
                ignored_normalised = {_norm(c) for c in IGNORED_IMPORT_COLUMNS}

                if has_id_column:
                    # UPDATE MODE – any subset of the allowed columns is fine, but nothing outside the set
                    extra = (normalised_headers - allowed_normalised) - ignored_normalised
                    if extra:
                        frappe.throw(
                            "Excel file (update mode) contains unexpected columns: "
                            + ", ".join(sorted(extra)) +
                            ".\nOnly the following columns are permitted when an ID column is present:\n• "
                            + "\n• ".join(sorted(ALLOWED_COLUMNS))
                        )
                else:
                    # CREATE MODE – keep the original strict exact-match behaviour
                    required_normalised = {_norm(c) for c in REQUIRED_COLUMNS}
                    missing = required_normalised - normalised_headers
                    extra = (normalised_headers - required_normalised) - ignored_normalised

                    if missing or extra:
                        msg_parts = []
                        if missing:
                            msg_parts.append(f"Missing required columns: {', '.join(sorted(missing))}")
                        if extra:
                            msg_parts.append(f"Unexpected extra columns: {', '.join(sorted(extra))}")
                        frappe.throw(
                            "Excel file columns do not match the required Sales upload format.\n"
                            + "\n".join(msg_parts) +
                            "\n\nPlease download the official template and use only those column headers."
                        )
                # ---------------------------------------------
                continue

            # Zip headers with the current row's values to create a dictionary
            record = {headers[i]: (row[i] if row[i] is not None else '') for i in range(min(len(headers), len(row))) if headers[i]}

            # Only append if the row has actual data
            if any(record.values()):
                records.append(record)

        current_datetime = datetime.now()
        current_date = current_datetime.strftime('%Y-%m-%d')
        current_time = current_datetime.strftime('%H:%M:%S')
        parsed_data = []

        # Iterate through the flat records
        for record in records:
            contact_name_raw = str(record.get('CONTACT', '')).strip()
            email_raw = str(record.get('EMAIL_CUSTOMER', '')).strip()
            
            contact_info = None
            contact_match_warn = ''
            
            # Prioritize email lookup if email is provided in Excel
            if email_raw:
                contact_info = get_contact_details_by_email(email_raw)
                if contact_info:
                    db_name = f"{contact_info.get('first_name','')} {contact_info.get('last_name','')}".strip().lower()
                    if contact_name_raw and contact_name_raw.lower() != db_name:
                        contact_match_warn = 'email_prioritized'
            
            # Fallback to name lookup if no email was provided or email wasn't found
            if not contact_info and contact_name_raw:
                contact_info = get_contact_details(contact_name_raw)
                if contact_info:
                    contact_match_warn = 'name_mapped'
            
            email_customer = contact_info.get('email_id') if contact_info else None
            # NEW RULE: ST of the Request And Quote always comes from the logged-in user
            st = get_logged_in_user_st_code()
            frappe.log_error(
                f"UPLOAD ST DEBUG | session.user={frappe.session.user} | resolved_st={st} | contact_st={contact_info.get('st') if contact_info else None}",
                "ST Upload Debug"
            )

            if email_customer is None:
                name_parts = contact_name_raw.split()
                first_name = name_parts[0] if name_parts else ''
                middle_name = name_parts[1] if len(name_parts) > 2 else ''
                last_name = name_parts[-2] if len(name_parts) > 2 else name_parts[1] if len(name_parts) == 2 else ''
                second_last_name = name_parts[-1] if len(name_parts) > 3 else ''
                contact_data = {
                    'customer': str(record.get('CUSTOMER', customer)).strip() or customer,
                    'first_name': first_name,
                    'middle_name': middle_name,
                    'last_name': last_name,
                    'second_last_name': second_last_name,
                    'email': ''
                }
            else:
                contact_data = None

            brand = str(record.get('BRAND', '')).strip()
            brand_status = frappe.db.get_value("Brand", brand, "brand_status") if brand else None
            is_banned = brand_status == "BAN"

            qty_raw = str(record.get('QTY', ''))
            qty = remove_thousand_separators(qty_raw) if qty_raw else ''

            # --- Validate custom_sales_status (Select field) ---
            status_val = str(record.get('CUSTOM_SALES_STATUS', '') or record.get('custom_sales_status', '')).strip()
            if status_val and allowed_status_options and status_val not in allowed_status_options:
                frappe.throw(
                    f"Invalid value '{status_val}' for column CUSTOM_SALES_STATUS "
                    f"(REF/SAP = {record.get('REF') or record.get('SAP') or '(unknown)'}).\n"
                    f"Allowed options are:\n• " + "\n• ".join(sorted(allowed_status_options))
                )

            # -----------------------------------------------------------------
            # Build item_data ONLY from columns that actually exist in the Excel.
            # This is critical for update mode: we must never send a field that
            # the user did not list in the spreadsheet.
            # -----------------------------------------------------------------
            item_data = {}

            # Helper that only adds the key when the original header was present
            def _add(excel_key, value):
                # excel_key is the normalised form we already use (_norm)
                # We keep the original uppercase key that the rest of the pipeline expects
                if _norm(excel_key) in normalised_headers:
                    item_data[excel_key] = value

            # Always try to carry the ID when the column exists
            if has_id_column:
                raw_id = record.get('ID') or record.get('id') or ''
                item_data['ID'] = str(raw_id).strip()

            _add('REF',                 str(record.get('REF', '')).strip())
            _add('CUSTOM_SALES_STATUS', status_val)
            _add('DATE',                normalize_excel_date(record.get('DATE')) or date.today().strftime('%Y-%m-%d'))
            _add('COUNTRY',             str(record.get('COUNTRY', 'CHILE')).strip())
            _add('CUSTOMER',            str(record.get('CUSTOMER', customer)).strip() or customer)
            _add('DIV',                 str(record.get('DIV', '')).strip())
            _add('CONTACT',             contact_name_raw)
            _add('EMAIL_CUSTOMER',      email_customer if email_customer else str(record.get('EMAIL_CUSTOMER', '')).strip())
            _add('CUSTOMER_REF',        str(record.get('CUSTOMER_REF', '')).strip())
            _add('DUE_DATE',            normalize_excel_date(record.get('DUE_DATE')))

            # --- DUE_TIME safety (Time field must always be clean HH:MM:SS) ---
            raw_due_time = record.get('DUE_TIME')
            normalised_due_time = normalize_excel_time(raw_due_time)
            if raw_due_time not in (None, '') and normalised_due_time is None:
                ref_or_sap = record.get('REF') or record.get('SAP') or '(unknown)'
                frappe.throw(
                    f"Invalid time value '{raw_due_time}' in column DUE_TIME "
                    f"(REF/SAP = {ref_or_sap}).\n"
                    f"Accepted formats: HH:MM:SS, H:MM:SS AM/PM, Excel time serial, etc. "
                    f"The value could not be converted to a clean HH:MM:SS."
                )
            _add('DUE_TIME', normalised_due_time)
            # ------------------------------------------------------------------

            _add('SAP',                 str(record.get('SAP', '')).strip())
            _add('ITEM',                str(record.get('ITEM', '')).strip())
            _add('QTY',                 qty)
            _add('UNIT',                str(record.get('UNIT', 'EA')).strip())
            _add('PART_NUMBER',         str(record.get('PART_NUMBER', '')).strip())
            _add('BRAND',               brand)
            _add('DESCRIPTION',         str(record.get('DESCRIPTION', '')).strip())
            _add('INCOTERM',            str(record.get('INCOTERM', 'EXW')).strip())
            _add('ATTACHMENT',          str(record.get('ATTACHMENT', '')).strip())
            _add('NOTE',                str(record.get('NOTE', '')).strip())
            _add('SALE-PRICE',          record.get('SALE-PRICE') or record.get('SALE_PRICE') or None)
            _add('REFERENCE_PRICE',     record.get('REFERENCE_PRICE') or None)
            _add('DATE REQ',            str(record.get('DATE REQ', '') or record.get('DATE_REQ', '')).strip())
            _add('RP',                  str(record.get('RP', '')).strip())
            _add('ST',                  st)

            # These two are always useful for the UI / logging even in update mode
            item_data['CURRENT_DATE'] = current_date
            item_data['CURRENT_TIME'] = current_time
            item_data['IS_BANNED']    = is_banned
            item_data['contact_match_warn'] = contact_match_warn

            if email_customer is None and 'CONTACT' in item_data:
                item_data['contact_not_found'] = True
                item_data['contact_data'] = contact_data
            
            
            if not has_id_column:
                due_date_val = str(item_data.get('DUE_DATE') or '').strip()
                due_time_val = str(item_data.get('DUE_TIME') or '').strip()
                if not due_date_val or not due_time_val:
                    ref_or_sap = item_data.get('REF') or item_data.get('SAP') or '(unknown)'
                    frappe.throw(
                        f"DUE_DATE and DUE_TIME are required for every Excel row "
                        f"(REF/SAP = {ref_or_sap})."
                    )
            if has_id_column:
                if 'DUE_DATE' in item_data and not str(item_data.get('DUE_DATE') or '').strip():
                    frappe.throw("DUE_DATE is required when the DUE_DATE column is present.")
                if 'DUE_TIME' in item_data and not str(item_data.get('DUE_TIME') or '').strip():
                    frappe.throw("DUE_TIME is required when the DUE_TIME column is present.")
            parsed_data.append(item_data)
            
            
        sap_note_cache = {}
        pn_cross_cache = {}          # key = (part_number, sap, customer)

        for item in parsed_data:
            sap = (item.get("SAP") or "").strip()
            if sap and sap not in sap_note_cache:
                sap_note_cache[sap] = get_note_for_sap(sap)

            pn = (item.get("PART_NUMBER") or "").strip()
            cust = (item.get("CUSTOMER") or "").strip()
            cache_key = (pn, sap, cust)
            if pn and cache_key not in pn_cross_cache:
                pn_cross_cache[cache_key] = part_number_cross_search(
                    part_number=pn,
                    current_sap=sap or None,
                    current_customer=cust or None
                )

        # Apply the pre-computed results
        # IMPORTANT: only enrich fields that the Excel actually contained.
        # This protects existing records when the file is used in UPDATE mode.
        for item in parsed_data:
            sap = (item.get("SAP") or "").strip()
            if sap and "NOTE" in item:                     # ← only if NOTE column was present
                sap_note = sap_note_cache.get(sap, "-.-||QU:-.-")
                item["NOTE"] = f"{sap_note} - {item['NOTE']}" if item.get("NOTE") else sap_note

            pn = (item.get("PART_NUMBER") or "").strip()
            cust = (item.get("CUSTOMER") or "").strip()
            cache_key = (pn, sap, cust)
            pn_cross_result = pn_cross_cache.get(cache_key)

            if (pn_cross_result
                    and pn_cross_result.get("matches")
                    and "DESCRIPTION" in item):            # ← only if DESCRIPTION column was present
                item["PN_CROSS_SEARCH"] = pn_cross_result
                cross_note = build_cross_search_description_note(pn_cross_result)
                if cross_note:
                    current_desc = item.get("DESCRIPTION") or ""
                    item["DESCRIPTION"] = f"{cross_note} {current_desc}".strip()
                        

        if save:
            conn = _get_db_connection()
            cursor = conn.cursor()
            saved_ids = []
            try:
                for item in parsed_data:
                    fields = [k for k in item.keys() if item[k] is not None and k not in ['contact_not_found', 'contact_data', 'CURRENT_DATE', 'CURRENT_TIME', 'IS_BANNED']]
                    values = [item[k] for k in fields]
                    placeholders = ", ".join(["%s"] * len(values))
                    query = f"INSERT INTO `request for quote` ({', '.join(fields)}) VALUES ({placeholders})"
                    cursor.execute(query, tuple(values))
                    saved_ids.append(cursor.lastrowid)
                conn.commit()
            except Exception as e:
                conn.rollback()
                frappe.throw(f"Error saving Excel data: {str(e)}")
            finally:
                cursor.close()
                conn.close()
            return saved_ids
        return parsed_data
    except Exception as e:
        frappe.throw(f"Error processing Excel file: {str(e)}")

def resolve_user_st_code(user_link):
    """
    Convert a Contact.st / Address.st value (User Link or e-mail)
    into the 3-digit code stored in User.st.
    Returns '' when the code cannot be determined.
    """
    if not user_link:
        return ''

    val = str(user_link).strip()

    # Already a clean 3-digit code
    if val.isdigit() and len(val) == 3:
        return val

    # 1) Try as User.name (normal Link field behaviour)
    if frappe.db.exists("User", val):
        code = frappe.db.get_value("User", val, "st")
        if code and str(code).isdigit() and len(str(code)) == 3:
            return str(code)

    # 2) Try as User.email (same pattern used in get_st_for_customer_div)
    try:
        user = frappe.get_doc("User", {"email": val})
        code = user.get("st")
        if code and str(code).isdigit() and len(str(code)) == 3:
            return str(code)
    except Exception:
        pass

    return ''   