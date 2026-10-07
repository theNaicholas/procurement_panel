import frappe
from frappe import get_doc, log_error, throw
import json
import mysql.connector
from .process_vendor_quote import process_ifm_pdf
import requests
import datetime
from io import BytesIO
from openpyxl import Workbook
from frappe.utils.response import build_response
from .process_request_test import (
    process_html_file, process_pdf_file, process_excel_file,
    resolve_user_st_code, get_st_code_for_ref, get_logged_in_user_st_code
)
import holidays
from datetime import date, timedelta, time, datetime
from frappe.model.document import get_doc   # keep this if you were using it, or use frappe.get_doc



"""
Request And Quote Doctype - Field Layout
========================================

SECTION: Request For Quote Details
Fieldname: request_for_quote_details_section (Section Break)

  1.  ID                        → legacy_id                  (Data)
  2.  REF                       → ref                        (Data)
  3.  DATE                      → date                       (Date)
  4.  COUNTRY                   → country                    (Data)
  5.  CUSTOMER                  → customer                   (Data)
  6.  DIV                       → div                        (Data)
  7.  CONTACT                   → contact                    (Data)
  8.  EMAIL_CUSTOMER            → email_customr              (Data)
  9.  CUSTOMER_REF              → customer_ref               (Data)
 10.  DUE_DATE                  → due_date                   (Data)
 11.  Due Time                  → due_time                   (Time)
 12.  SAP                       → sap                        (Data)
 13.  ITEM                      → item                       (Data)
 14.  QTY                       → qty                        (Data)
 15.  UNIT                      → unit                       (Data)
 16.  PART_NUMBER               → part_number                (Data)
 17.  BRAND                     → brand                      (Data)
 18.  DESCRIPTION               → description                (Long Text)
 19.  INCOTERM                  → incoterm                   (Data)
 20.  ATTACHMENT                → attachment                 (Long Text)
 21.  NOTE                      → note                       (Text)
 22.  SALE-PRICE                → sale_price                 (Data)
 23.  REFERENCE_PRICE           → reference_price            (Data)
 24.  DATE REQ                  → date_req                   (Data)
 25.  RP                        → rp                         (Text)
 26.  ST                        → st                         (Text)

SECTION: Quote Details
Fieldname: quote_details_section (Section Break)

COLUMN: original request
Fieldname: original_request_column (Column Break)

 27.  REP                       → rep                        (Data)
 28.  Quotation Item            → quotation_item             (Data)
 29.  Quotation QTY             → quotation_qty              (Data)
 30.  Quotation UNIT            → quotation_unit             (Data)
 31.  Quotation Part Number     → quotation_part_number      (Data)
 32.  Quotation Brand           → quotation_brand            (Data)
 33.  Quotation Description     → quotation_description      (Long Text)
 34.  Quotation CO              → quotation_co               (Data)
 35.  Quotation Sales Price     → quotation_sales_price      (Data)
 36.  Quotation Delivery        → quotation_delivery         (Data)
 37.  Quotation Aprox Weight    → quotation_aprox_weight     (Data)
 38.  Quotation Incoterm        → quotation_incoterm         (Data)
 39.  Quotation Attachment      → quotation_attachment       (Long Text)
 40.  Quotation Note            → quotation_note             (Text)
 41.  Quotation RFQDATE         → quotation_rfqdate          (Data)
 42.  Feedback                  → feedback                   (Data)
 43.  test                      → test                       (Data)
 44.  Procurement Status        → procurement_status         (Select)
"""

# --- New Imports for Template Mapping ---
import pandas as pd
import re
import io
import signal
# ----------------------------------------

pe_holidays = holidays.Peru()
cl_holidays = holidays.Chile()
us_holidays = holidays.UnitedStates()

# --- New Import for PDF processing ---
try:
    import pdfplumber
except ImportError:
    pdfplumber = None
# -----------------------------------


# ==============================================================================
# HELPER FUNCTIONS FOR NEW TEMPLATE MAPPING
# (Copied from upload_map_erp.py)
# ==============================================================================

# --- Timeout logic for Regex ---
class RegexTimeoutError(Exception):
    pass

def timeout_handler(signum, frame):
    """This function is called when the alarm signal is received."""
    raise RegexTimeoutError("Regex processing timed out after 1 second.")
# --- END ---

def _apply_cleanup_rule(value, rule):
    """Applies the selected cleanup rule to the extracted value with timeout protection."""
    if value is None:
        return ''
    cleanup_method = rule.get('cleanup_method')
    source_format = rule.get('source_format')
    regex = rule.get('regex')
    value_str = str(value).strip()

    try:
        if cleanup_method == 'convert_date' and source_format:
            value_str_cleaned = re.sub(r'(\d+)(st|nd|rd|th)', r'\1', value_str)
            dt_object = datetime.strptime(value_str_cleaned, source_format)
            # Match the upload.html format mm/dd/yyyy
            return dt_object.strftime('%m/%d/%Y')
        elif cleanup_method == 'convert_time' and source_format:
            dt_object = datetime.strptime(value_str, source_format)
            return dt_object.strftime('%H:%M:%S')
        elif regex:
            signal.signal(signal.SIGALRM, timeout_handler)
            signal.alarm(1)  # Set a 1-second alarm
            try:
                match = re.search(regex, value_str, re.DOTALL)
                signal.alarm(0)  # Disable the alarm
                return match.group(1).strip() if match and match.groups() else ''
            finally:
                signal.alarm(0)  # Ensure alarm is always disabled
    except (ValueError, RegexTimeoutError) as e:
        frappe.log_error(f"Cleanup failed for value '{value_str}'. Error: {e}", "Template Apply Error")
        return f"CLEANUP_ERROR: {e}" # Return error instead of raising

    return value_str

def _apply_pdf_rules(rules, file_content, filename):
    """
    Applies PDF mapping rules and returns extracted data.
    (Based on _run_pdf_test from upload_map_erp.py)
    """
    if not pdfplumber:
        frappe.throw("The 'pdfplumber' library is not installed. Please run 'bench pip install pdfplumber'.")

    header_data = {}
    # PDF Item extraction is not supported by the mapping tool, so we only process headers.

    with pdfplumber.open(io.BytesIO(file_content)) as pdf:
        for rule in rules.get('header_rules', []):
            field_name = rule.get('field', 'Unknown')
            try:
                value = None
                if rule['method'] == 'filename_regex':
                    value = filename

                elif rule['method'] == 'pdf_find_by_label':
                    page_num = int(rule.get('page_num', 0))
                    if page_num >= len(pdf.pages):
                        raise IndexError(f"Page number {page_num} is out of bounds.")

                    page = pdf.pages[page_num]
                    label_text = rule['source_label']
                    direction = rule.get('search_direction', 'right')

                    words = page.extract_words(x_tolerance=3, y_tolerance=3, keep_blank_chars=False, use_text_flow=True)

                    label_bbox = None
                    for i, word in enumerate(words):
                        if label_text.lower().startswith(word["text"].lower()):
                            temp_match = [word]
                            remaining_label = label_text.lower().replace(word["text"].lower(), "", 1).strip()
                            if not remaining_label:
                                label_bbox = (word["x0"], word["top"], word["x1"], word["bottom"])
                                break
                            for next_word in words[i+1:]:
                                if remaining_label.startswith(next_word["text"].lower()):
                                    temp_match.append(next_word)
                                    remaining_label = remaining_label.replace(next_word["text"].lower(), "", 1).strip()
                                    if not remaining_label:
                                        label_bbox = (temp_match[0]["x0"], temp_match[0]["top"], temp_match[-1]["x1"], temp_match[-1]["bottom"])
                                        break
                                else:
                                    break
                        if label_bbox:
                            break

                    if not label_bbox:
                        raise ValueError(f"Label '{label_text}' not found.")

                    found_words = []
                    if direction == 'right':
                        search_x0, search_top, search_bottom = label_bbox[2], label_bbox[1] - 5, label_bbox[3] + 5
                        for word in words:
                            if word["x0"] > search_x0 and word["top"] >= search_top and word["bottom"] <= search_bottom:
                                found_words.append(word)
                        found_words.sort(key=lambda w: w['x0'])

                    value = " ".join([w['text'] for w in found_words])

                elif rule['method'] == 'static_value':
                    value = rule.get('static_value', '')

                cleaned_value = _apply_cleanup_rule(value, rule)
                header_data[field_name] = cleaned_value

            except Exception as e:
                frappe.log_error(f"Error applying PDF rule for {field_name}: {e}", "Template Apply Error")
                header_data[field_name] = f"EXTRACT_ERROR: {e}"

    # Since PDF item rules aren't supported, return a single record with header data
    return [header_data]

def _apply_html_rules(rules, file_content, filename):
    """
    Applies HTML mapping rules and returns extracted data.
    (Based on _run_html_test from upload_map_erp.py)
    """
    try:
        list_of_dfs = pd.read_html(io.BytesIO(file_content))
    except Exception as e:
        frappe.throw(f"Pandas could not parse the HTML file. It may be malformed. Error: {e}")

    header_data = {}
    items_data = []

    # --- Extract Header Data ---
    for rule in rules.get('header_rules', []):
        field_name = rule.get('field', 'Unknown')
        try:
            value = None
            if rule['method'] == 'filename_regex':
                value = filename

            elif rule['method'] in ['find_by_label', 'fixed_position']:
                df_index = int(rule.get('table_index', 0))
                if df_index >= len(list_of_dfs):
                    raise IndexError(f"Table index '{df_index}' out of bounds.")

                df = list_of_dfs[df_index].fillna('')

                if rule['method'] == 'find_by_label':
                    label_text, label_col, value_col = rule['source_label'], int(rule['label_col']), int(rule['value_col'])
                    found = False
                    for _, row in df.iterrows():
                        if label_col < len(row) and str(row[label_col]).strip().lower() == label_text.lower():
                            if value_col < len(row):
                                value = str(row[value_col])
                                found = True
                                break
                    if not found:
                        raise ValueError(f"Label '{label_text}' not found.")

                elif rule['method'] == 'fixed_position':
                    fixed_row, fixed_col = int(rule['fixed_row']), int(rule['fixed_col'])
                    if fixed_row >= len(df) or fixed_col >= len(df.columns):
                        raise IndexError(f"Position out of bounds.")
                    value = str(df.iloc[fixed_row, fixed_col])

            elif rule['method'] == 'static_value':
                value = rule.get('static_value', '')

            cleaned_value = _apply_cleanup_rule(value, rule)
            header_data[field_name] = cleaned_value

        except Exception as e:
            frappe.log_error(f"Error applying HTML header rule for {field_name}: {e}", "Template Apply Error")
            header_data[field_name] = f"EXTRACT_ERROR: {e}"

    # --- Extract Item Data ---
    item_rules = rules.get('item_rules', {})
    if item_rules:
        try:
            df_index = int(item_rules.get('table_index', 0))
            if df_index >= len(list_of_dfs):
                raise IndexError(f"Item table index '{df_index}' out of bounds.")

            df = list_of_dfs[df_index].fillna('')
            start_row = int(item_rules.get('start_row', 0))
            id_col = int(item_rules.get('identifier_col', 0))
            id_regex = item_rules.get('identifier_regex', '')

            item_start_indices = [
                index for index, row in df.iterrows()
                if index >= start_row and id_col < len(row) and id_regex and re.search(id_regex, str(row[id_col]))
            ]

            if not item_start_indices:
                raise ValueError("No items found based on identification rules.")

            for start_index in item_start_indices:
                item_row_data = {}
                for rule in item_rules.get('field_rules', []):
                    field_name = rule.get('field', 'Unknown')
                    try:
                        row_offset, col_index = int(rule.get('row_offset', 0)), int(rule.get('col_index', 0))
                        target_row_index = start_index + row_offset

                        if not (target_row_index < len(df) and col_index < len(df.iloc[target_row_index])):
                            raise IndexError("Position is out of bounds.")

                        value = str(df.iloc[target_row_index, col_index])
                        cleaned_value = _apply_cleanup_rule(value, rule)
                        item_row_data[field_name] = cleaned_value

                    except Exception as e:
                         frappe.log_error(f"Error applying HTML item rule for {field_name}: {e}", "Template Apply Error")
                         item_row_data[field_name] = f"EXTRACT_ERROR: {e}"

                items_data.append(item_row_data)

        except Exception as e:
             frappe.log_error(f"Failed to process items: {e}", "Template Apply Error")
             # Add a dummy item to show the error
             items_data.append({"DESCRIPTION": f"ITEM_PROCESS_ERROR: {e}"})

    if not items_data:
        # If no items were found or rules, return just the header data
        return [header_data]
    else:
        # Combine header data with each item row
        return [{**header_data, **item} for item in items_data]


def get_template_for_file(customer, file_content, file_ext, filename):
    """
    Finds the correct 'Upload Mapping Template' to use based on 'Upload Mapping Group' rules.
    """
    group_name = frappe.db.exists("Upload Mapping Group", {"customer": customer})
    if not group_name:
        frappe.throw(f"No auto-selection rules found for customer: {customer}")

    group = frappe.get_doc("Upload Mapping Group", group_name)
    if not group.rules:
        frappe.throw(f"Rule group for {customer} is empty.")

    rules = sorted(group.rules, key=lambda x: int(x.priority or 99))

    # Parse file content once for checking all rules
    list_of_dfs = []
    pdf_pages = []

    try:
        if file_ext in ['html', 'htm']:
            list_of_dfs = pd.read_html(io.BytesIO(file_content))
        elif file_ext == 'pdf':
            if not pdfplumber:
                 frappe.throw("PDFPlumber library not installed.")
            with pdfplumber.open(io.BytesIO(file_content)) as pdf:
                pdf_pages = [page.extract_text() for page in pdf.pages] # Extract text for searching
    except Exception as e:
        frappe.throw(f"Could not parse file {filename} for auto-selection. Error: {e}")

    for rule in rules:
        try:
            check_text = rule.check_text
            if not check_text:
                continue

            cell_value = ""
            if file_ext in ['html', 'htm']:
                table_idx = int(rule.check_table_index or 0)
                row_idx = int(rule.check_row or 0)
                col_idx = int(rule.check_column or 0)

                if table_idx < len(list_of_dfs):
                    df = list_of_dfs[table_idx].fillna('')
                    if row_idx < len(df) and col_idx < len(df.columns):
                        cell_value = str(df.iloc[row_idx, col_idx])

            elif file_ext == 'pdf':
                page_idx = int(rule.check_table_index or 0) # Use table_index as page index for PDF
                if page_idx < len(pdf_pages):
                    cell_value = pdf_pages[page_idx] # Check against the entire page text

            if check_text.lower() in cell_value.lower():
                return rule.template_to_use # Found a match

        except Exception as e:
            frappe.log_error(f"Error checking auto-select rule {rule.name}: {e}", "Auto-Select Error")
            continue # Try the next rule

    frappe.throw(f"File {filename} did not match any auto-selection rules for {customer}.")


def apply_mapping_rules(customer, file_content, file_ext, filename):
    """
    Main function to find and apply mapping rules for the new template system.
    """
    # 1. Find the correct template name
    template_name = get_template_for_file(customer, file_content, file_ext, filename)

    if not template_name:
        frappe.throw("Could not determine which template to use.")

    # 2. Load the template
    try:
        doc = frappe.get_doc("Upload Mapping Template", template_name)
        mapping_rules = json.loads(doc.mapping_json or '{}')
        file_type = doc.file_type
    except Exception as e:
        frappe.throw(f"Could not load mapping template '{template_name}'. Error: {e}")

    # 3. Apply the rules based on file type
    parsed_data = []
    if file_type == 'PDF':
        parsed_data = _apply_pdf_rules(mapping_rules, file_content, filename)
    elif file_type == 'HTML':
        parsed_data = _apply_html_rules(mapping_rules, file_content, filename)
    else:
        frappe.throw(f"Template '{template_name}' has an unsupported file type: {file_type}")

    # 4. Add debug info and file_name
    # The 'upload.html' page expects this structure
    if parsed_data:
        parsed_data[0]['debug_log'] = [
            f"Applied template: {template_name}",
            f"File type: {file_type}"
        ]

    return parsed_data

# ==============================================================================
# END OF NEW HELPER FUNCTIONS
# ==============================================================================




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

def translate_text(text, source_lang='es', target_lang='en'):
    api_key = frappe.conf.get('deepl_api_key')
    if not api_key:
        frappe.log_error(message="DeepL API key not found in Site Config", title="Translation Error (Test)")
        return text

    url = "https://api-free.deepl.com/v2/translate"
    params = {
        "auth_key": api_key,
        "text": text,
        "source_lang": source_lang.upper(),
        "target_lang": target_lang.upper()
    }

    try:
        response = requests.post(url, data=params)
        response.raise_for_status()
        return response.json()["translations"][0]["text"]
    except Exception as e:
        frappe.log_error(message=f"Translation API error: {str(e)}", title="Translation Error (Test)")
        return text


@frappe.whitelist(allow_guest=False)
def upload_file():
    # Safely check both form_dict and request.form to ensure variables are never lost
    customer = frappe.form_dict.get('customer') or (frappe.request.form.get('customer') if hasattr(frappe.request, 'form') else None)
    is_new_template_raw = frappe.form_dict.get('is_new_template') or (frappe.request.form.get('is_new_template') if hasattr(frappe.request, 'form') else None)
    is_new_template = is_new_template_raw == '1'
    
    # Check if files were passed correctly
    files = []
    if hasattr(frappe.request, 'files'):
        files = frappe.request.files.getlist('file')

    if not customer or not files:
        frappe.throw(f"Upload blocked. Customer provided: '{customer}'. Number of files attached: {len(files)}.")

    allowed_extensions = ['html', 'htm', 'pdf', 'xlsx']
    for file in files:
        file_ext = file.filename.lower().split('.')[-1]
        if file_ext not in allowed_extensions:
            throw(f"Unsupported file type: {file.filename}. Only .html, .htm, .pdf, and .xlsx files are supported.")

    frappe.log_error(message=f"Uploaded files: {[file.filename for file in files]}, Customer: {customer}", title="Upload File Debug (Test)")

    all_parsed_data = []

    for file in files:
        file_ext = file.filename.lower().split('.')[-1]

        # --- NEW LOGIC BRANCH ---
        if is_new_template:
            # Use the new mapping system
            try:
                # We need to read the file content here to pass to the parsers
                file_content = file.read()
                # Reset file pointer if we need to use it again (e.g., for saving)
                file.seek(0)

                parsed_data = apply_mapping_rules(customer, file_content, file_ext, file.filename)

                # Check for contact_not_found logic (which old parser does)
                # The new parser extracts 'CONTACT' and 'EMAIL_CUSTOMER'
                # We need to replicate the contact check
                if parsed_data:
                    first_record = parsed_data[0]
                    contact_name = first_record.get('CONTACT')
                    email = first_record.get('EMAIL_CUSTOMER')

                    if contact_name and not email:
                         # Match contact specifically to the customer
                        contact_query = """
                            SELECT c.name, ce.email_id 
                            FROM `tabContact` c
                            INNER JOIN `tabDynamic Link` dl ON dl.parent = c.name
                            LEFT JOIN `tabContact Email` ce ON ce.parent = c.name AND ce.is_primary = 1
                            WHERE dl.link_doctype = 'Customer' AND dl.link_name = %s
                            AND (c.first_name LIKE %s OR c.last_name LIKE %s)
                            LIMIT 1
                        """
                        contact_match = frappe.db.sql(contact_query, (customer, f'%{contact_name}%', f'%{contact_name}%'), as_dict=True)
                        
                        if contact_match and contact_match[0].get('email_id'):
                             email = contact_match[0].get('email_id')
                             first_record['EMAIL_CUSTOMER'] = email

                        if not email:
                             # Can't find email, flag for popup
                             first_record['contact_not_found'] = True
                             first_record['contact_data'] = {
                                 'customer': customer,
                                 'first_name': contact_name,
                                 'email': ''
                             }

                    # Add other fields the old parser adds
                    first_record['CUSTOMER'] = customer # Ensure customer is set

                all_parsed_data.append({
                    "file_name": file.filename,
                    "parsed_data": parsed_data
                })

            except Exception as e:
                # Log the error and add a dummy record to show the error on the frontend
                frappe.log_error(f"Error applying new template for {file.filename}: {e}", "Upload File Error")
                all_parsed_data.append({
                    "file_name": file.filename,
                    "parsed_data": [{
                        "DESCRIPTION": f"ERROR: {str(e)}",
                        "debug_log": [f"Failed to apply template for {customer}", f"{traceback.format_exc()}"]
                    }]
                })

        else:
            # --- ORIGINAL LOGIC ---
            file_doc = get_doc({
                "doctype": "File",
                "file_name": file.filename,
                "content": file.read(),
                "is_private": 1
            })
            file_doc.insert()

            if file_ext == 'xlsx':
                parsed_data = process_excel_file(file_doc.file_url, customer=customer, save=False)

                # ============================================================
                # NEW: Group Excel rows by REF so each unique REF becomes
                # its own “file” card in the UI (identical behaviour to
                # uploading multiple HTML/PDF files).
                # Rows that share the same REF keep the same header values
                # and are shown together; different REFs are treated as
                # completely separate imports.
                # ============================================================
                from collections import OrderedDict
                groups = OrderedDict()
                for row in (parsed_data or []):
                    ref_val = str(row.get('REF') or '').strip()
                    # Empty REF rows stay together under a single synthetic key
                    key = ref_val if ref_val else '__EMPTY_REF__'
                    if key not in groups:
                        groups[key] = []
                    groups[key].append(row)

                # Pull timing info once (from the first row of the original list)
                timing_report = None
                total_sec = None
                if parsed_data and isinstance(parsed_data, list) and parsed_data[0].get("_timings"):
                    timing_report = parsed_data[0].pop("_timings", None)
                    total_sec = parsed_data[0].pop("_total_seconds", None)

                first_key = next(iter(groups), None)
                for key, group_rows in groups.items():
                    # Clean residual timing keys so they never appear in the UI
                    for r in group_rows:
                        r.pop("_timings", None)
                        r.pop("_total_seconds", None)
                        r.pop("_detailed_sap_pn", None)

                    if key == '__EMPTY_REF__':
                        display_name = f"{file.filename} [No REF]"
                    else:
                        display_name = f"{file.filename} [REF: {key}]"

                    all_parsed_data.append({
                        "file_name": display_name,
                        "parsed_data": group_rows,
                        # Attach timing only to the first group to avoid duplication
                        "_timings": timing_report if key == first_key else None,
                        "_total_seconds": total_sec if key == first_key else None
                    })

            elif file_ext in ['html', 'htm']:
                parsed_data = process_html_file(file_doc.file_url, customer=customer, save=False)

                # surface the timing report that process_* attached
                timing_report = None
                total_sec = None
                if parsed_data and isinstance(parsed_data, list) and parsed_data[0].get("_timings"):
                    timing_report = parsed_data[0].pop("_timings", None)
                    total_sec = parsed_data[0].pop("_total_seconds", None)

                all_parsed_data.append({
                    "file_name": file.filename,
                    "parsed_data": parsed_data,
                    "_timings": timing_report,
                    "_total_seconds": total_sec
                })

            elif file_ext == 'pdf':
                parsed_data = process_pdf_file(file_doc.file_url, customer=customer, save=False)

                # surface the timing report that process_* attached
                timing_report = None
                total_sec = None
                if parsed_data and isinstance(parsed_data, list) and parsed_data[0].get("_timings"):
                    timing_report = parsed_data[0].pop("_timings", None)
                    total_sec = parsed_data[0].pop("_total_seconds", None)

                all_parsed_data.append({
                    "file_name": file.filename,
                    "parsed_data": parsed_data,
                    "_timings": timing_report,
                    "_total_seconds": total_sec
                })
            else:
                throw(f"Unsupported file type: {file.filename}")
        # --- END OF LOGIC BRANCH ---

    return all_parsed_data


@frappe.whitelist(allow_guest=False)
def download_template():
    wb = Workbook()

    # Use the default active sheet instead of creating new ones
    ws = wb.active
    ws.title = "Upload Template"

    # Exact columns accepted by the Sales Excel import (ID is deliberately omitted)
    all_fields = [
        'REF', 'CUSTOM_SALES_STATUS', 'DATE', 'COUNTRY', 'CUSTOMER', 'DIV',
        'CONTACT', 'EMAIL_CUSTOMER', 'CUSTOMER_REF', 'DUE_DATE', 'DUE_TIME',
        'SAP', 'ITEM', 'QTY', 'UNIT', 'PART_NUMBER', 'BRAND', 'DESCRIPTION',
        'INCOTERM', 'NOTE', 'SALE-PRICE', 'REFERENCE_PRICE', 'DATE REQ', 'ST'
    ]

    # Write the headers across Row 1
    for i, field in enumerate(all_fields, start=1):
        ws.cell(row=1, column=i, value=field)

    output = BytesIO()
    wb.save(output)
    output.seek(0)

    return build_response("application/vnd.openxmlformats-officedocument.spreadsheet.sheet", output.getvalue(), "upload_template.xlsx")


def _normalize_part_number(value):
    text = str(value or "").strip().upper()
    return text.replace(" ", "").replace("-", "")


def _item_part_number_fieldname():
    try:
        meta = frappe.get_meta("Item")
        for fieldname in ("custom_part_number", "part_number", "manufacturer_part_no"):
            if meta.has_field(fieldname):
                return fieldname
    except Exception:
        pass
    return None


def _admin_find_item(customer, sap, brand, part_number):
    """
    Administrator test match.
    One Item: return it.
    Several Items: return nothing. Do not guess.
    """
    customer = str(customer or "").strip()
    sap = str(sap or "").strip()
    brand = str(brand or "").strip()
    part_number = str(part_number or "").strip()
    normalized = _normalize_part_number(part_number)
    found = []

    def _add(item_code, method):
        item_code = str(item_code or "").strip()
        if not item_code or not frappe.db.exists("Item", item_code):
            return
        if any(row["item_code"] == item_code for row in found):
            return
        found.append({"item_code": item_code, "match_method": method})

    if sap and customer:
        rows = frappe.db.sql(
            """
            SELECT icd.parent AS item_code
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
            _add(row.item_code, "sap_customer")

    pn_field = _item_part_number_fieldname()
    if normalized and pn_field:
        rows = frappe.db.sql(
            f"""
            SELECT i.name AS item_code
            FROM `tabItem` i
            WHERE IFNULL(i.disabled, 0) = 0
              AND IFNULL(i.`{pn_field}`, '') != ''
              AND REPLACE(REPLACE(UPPER(i.`{pn_field}`), ' ', ''), '-', '') = %s
              AND (%s = '' OR UPPER(IFNULL(i.brand, '')) = UPPER(%s))
            """,
            (normalized, brand, brand),
            as_dict=True,
        ) or []
        for row in rows:
            _add(row.item_code, "part_number")

    if len(found) == 1:
        return found[0]
    return None


def _admin_create_item(customer, brand, part_number, description, unit):
    part_number = str(part_number or "").strip()
    brand = str(brand or "").strip()
    description = str(description or "").strip()
    if not part_number:
        return None

    normalized = _normalize_part_number(part_number)
    item_code = normalized[:140] or None
    if not item_code:
        return None
    if frappe.db.exists("Item", item_code):
        item_code = f"{item_code}-{customer[:20]}".strip("-")[:140]
    if frappe.db.exists("Item", item_code):
        return None

    item_group = None
    try:
        item_group = frappe.db.get_single_value("Stock Settings", "item_group")
    except Exception:
        item_group = None
    if item_group and frappe.db.get_value("Item Group", item_group, "is_group"):
        item_group = None
    if not item_group or not frappe.db.exists("Item Group", item_group):
        item_group = frappe.db.get_value("Item Group", {"is_group": 0}, "name")
    if not item_group:
        frappe.throw("No leaf Item Group found. Create one before uploading.")

    stock_uom = str(unit or "").strip()
    if not stock_uom or not frappe.db.exists("UOM", stock_uom):
        stock_uom = frappe.db.get_single_value("Stock Settings", "stock_uom") or "Nos"

    if brand and not frappe.db.exists("Brand", brand):
        try:
            frappe.get_doc({"doctype": "Brand", "brand": brand}).insert(ignore_permissions=True)
        except Exception:
            brand = ""

    doc = frappe.get_doc({
        "doctype": "Item",
        "item_code": item_code,
        "item_name": (description or part_number)[:140],
        "item_group": item_group,
        "stock_uom": stock_uom,
        "is_stock_item": 0,
        "include_item_in_manufacturing": 0,
        "description": description or part_number,
        "brand": brand or None,
    })
    pn_field = _item_part_number_fieldname()
    if pn_field:
        setattr(doc, pn_field, part_number)
    doc.flags.ignore_permissions = True
    doc.insert(ignore_permissions=True)
    return doc.name


def _admin_upsert_customer_detail(item_code, customer, sap, brand, part_number, description):
    if not item_code or not customer or not frappe.db.exists("Customer", customer):
        return
    item = frappe.get_doc("Item", item_code)
    child_meta = frappe.get_meta("Item Customer Detail")
    target = None
    for row in item.get("customer_items") or []:
        same_customer = str(row.customer_name or "").strip() == customer
        same_sap = str(row.ref_code or "").strip() == str(sap or "").strip()
        same_pn = str(row.get("customer_part_number") or "").strip().upper() == str(part_number or "").strip().upper()
        if same_customer and (same_sap or same_pn):
            target = row
            break
    if target is None:
        target = item.append("customer_items", {})
        target.customer_name = customer
        target.customer_group = frappe.db.get_value("Customer", customer, "customer_group") or ""
    if sap:
        target.ref_code = sap
    if child_meta.has_field("customer_part_number"):
        target.customer_part_number = part_number or ""
    if child_meta.has_field("customer_brand"):
        target.customer_brand = brand or ""
    if child_meta.has_field("customer_description"):
        target.customer_description = description or ""
    item.flags.ignore_permissions = True
    item.save(ignore_permissions=True)


def _admin_link_uploaded_item(raq_doc, source_row):
    """
    Test path. Caller must already have confirmed the real login is Administrator.
    Does not overwrite the Item description.
    Does not split an item because the description mentions another part.
    """
    customer = str(raq_doc.customer or "").strip()
    sap = str(raq_doc.sap or "").strip()
    brand = str(raq_doc.brand or "").strip()
    part_number = str(raq_doc.part_number or "").strip()
    description = str(raq_doc.description or "").strip()
    unit = str(raq_doc.unit or "").strip()

    match = _admin_find_item(customer, sap, brand, part_number)
    created = False
    if match:
        item_code = match["item_code"]
        method = match["match_method"]
    else:
        item_code = _admin_create_item(customer, brand, part_number, description, unit)
        method = "manual"
        created = bool(item_code)
    if not item_code:
        return {"linked": False, "reason": "no unique item and no part number to create"}

    _admin_upsert_customer_detail(item_code, customer, sap, brand, part_number, description)

    from my_custom_app.item_management import save_raq_item_match
    link = save_raq_item_match(
        raq_name=raq_doc.name,
        item_code=item_code,
        match_method=method,
        match_status="confirmed",
    )
    return {
        "linked": link.get("status") == "success",
        "item_code": item_code,
        "match_method": method,
        "created": created,
        "error": link.get("error") or "",
    }


def _customer_details_prefix(customer_name):
    name = str(customer_name or "").strip()
    if not name or not frappe.db.exists("Customer", name):
        return ""
    details = frappe.db.get_value("Customer", name, "customer_details") or ""
    return str(details).strip()

def _prepend_customer_details(customer_name, note):
    prefix = _customer_details_prefix(customer_name)
    body = str(note or "")
    if not prefix:
        return body
    if body.startswith(prefix):
        return body
    if not body.strip():
        return prefix
    return prefix + "\n" + body
Python


@frappe.whitelist(allow_guest=False)
def save_data(data):
    # ============================================================
    # CRITICAL SAFETY: Capture the real user FIRST and never allow
    # a None / empty / "None" value to be restored later.
    # This is what caused the "User None is disabled" crash when
    # the upload modal stayed open for a long time.
    # ============================================================
    current_user = frappe.session.user
    if not current_user or current_user in (None, "None", "", "Guest"):
        # Last-resort fallback – never write None back into the session
        current_user = "Guest"
    
    # NEW early exit – the incoming session is already unusable
    if current_user == "Guest" and frappe.session.user in (None, "None", ""):
        return {
            "status": "error",
            "error": "Session expired or invalid (User None). Please refresh the page, log in again, and retry the upload."
        }

    try:
        # ============================================================
        # TEMPORARILY RUN AS ADMINISTRATOR TO BYPASS ALL PERMISSIONS
        # This is required because the user is a Website User with no
        # role permissions on Request And Quote
        # ============================================================
        frappe.set_user("Administrator")

        frappe.log_error(message=f"Received data type: {type(data)}", title="Save Data Debug (Test)")
        frappe.log_error(message=f"Raw data: {str(data)[:100]}", title="Save Data Raw (Test)")

        if isinstance(data, str):
            data = json.loads(data)

        frappe.log_error(message=f"Parsed data type: {type(data)}", title="Save Data Parsed Type (Test)")
        frappe.log_error(message=f"Parsed data sample: {str(data)[:100]}", title="Save Data Parsed Sample (Test)")

        if not isinstance(data, list):
            raise ValueError("Expected data to be a list after parsing")

        for item in data:
            item['DIV'] = item.get('DIV', '').upper()
            item['UNIT'] = item.get('UNIT', '').upper()
            item['CUSTOMER'] = item.get('CUSTOMER', '').upper()
            frappe.log_error(message=f"Item before saving: {item}", title="Item Before Saving (Test)")

        # ============================================================
        # OPTIMIZED ID CALCULATION
        # Fetch the latest ID instantly using the 'creation' index.
        # Executed ONCE before the loop to prevent 504 Timeouts.
        # ============================================================
        frappe.log_error(message="Step 1: Starting ID calculation", title="Save Data Tracker")
        
        max_id_result = frappe.db.sql("""
            SELECT MAX(CAST(name AS UNSIGNED)) as max_id 
            FROM `tabRequest And Quote`
        """, as_dict=True)

        current_max = max_id_result[0].max_id if max_id_result and max_id_result[0].max_id else 0
        next_id = current_max + 1
        
        frappe.log_error(message=f"Step 2: ID calculation finished. Next ID: {next_id}", title="Save Data Tracker")

        saved_ids = []
        updated_ids = []
        for index, item in enumerate(data):
            if not isinstance(item, dict):
                frappe.log_error(message=f"Invalid item type: {type(item)}, item: {item}", title="Invalid Item (Test)")
                raise ValueError(f"Expected item to be a dictionary, got {type(item)}: {item}")

            # ---------------------------------------------------------------
            # Decide Create vs Update for this row
            # ---------------------------------------------------------------
            row_id = str(item.get('ID', '') or '').strip()

            if row_id:
                # ===================== UPDATE PATH =====================
                frappe.log_error(
                    message=f"Step 3.{index}: UPDATE mode for existing ID {row_id}",
                    title="Save Data Tracker"
                )

                if not frappe.db.exists("Request And Quote", row_id):
                    error_msg = f"Row {index}: ID '{row_id}' does not exist in Request And Quote. Cannot update."
                    frappe.log_error(message=error_msg, title="Save Data Update Error")
                    frappe.local.response['http_status_code'] = 400
                    return {
                        "status": "error",
                        "message": error_msg,
                        "saved_ids_before_failure": saved_ids,
                        "updated_ids_before_failure": updated_ids
                    }

                try:
                    # Build a dict of ONLY the fields that the Excel actually supplied.
                    # Keys here are the real doctype fieldnames.
                    update_fields = {}

                    # ------------------------------------------------------------------
                    # NEW REF LOGIC (only when the Excel actually contains a REF column)
                    # [Z] + custom_customer_category + ST(Customer preferred, else Address matching DIV) + "-"
                    # The part after the last "-" (if any) is treated as the user-typed sequential number.
                    # ------------------------------------------------------------------
                    if 'REF' in item:
                        original_ref = str(item.get('REF', '') or '').strip()
                        z_checked = bool(item.get('Z_CHECK') or item.get('z_check') or item.get('is_z'))
                        customer_name = str(item.get('CUSTOMER', '') or '').strip()
                        div_val = str(item.get('DIV', '') or '').strip()
                        st_for_ref = ""
                        cat = ""
                        if customer_name:
                            st_for_ref = get_st_code_for_ref(customer_name, div_val)
                            cat = (frappe.db.get_value("Customer", customer_name, "custom_customer_category") or "").strip()

                        auto_prefix = ""
                        if z_checked:
                            auto_prefix += "Z"
                        if st_for_ref:
                            auto_prefix += f"{cat}{st_for_ref}"

                        if original_ref:
                            # User-typed REF wins. Do not rebuild category/ST around it.
                            final_ref = original_ref
                            if z_checked and final_ref.upper()[:1] != "Z":
                                final_ref = "Z" + final_ref
                        elif auto_prefix:
                            final_ref = f"{auto_prefix}-"
                        else:
                            final_ref = original_ref

                        update_fields['ref'] = final_ref

                    # Simple 1-to-1 mappings (only if the Excel key is present)
                    simple_map = {
                        'CUSTOM_SALES_STATUS': 'custom_sales_status',
                        'DATE':                'date',
                        'COUNTRY':             'country',
                        'CUSTOMER':            'customer',
                        'DIV':                 'div',
                        'CONTACT':             'contact',
                        'EMAIL_CUSTOMER':      'email_customr',
                        'CUSTOMER_REF':        'customer_ref',
                        'DUE_DATE':            'due_date',
                        'DUE_TIME':            'due_time',
                        'SAP':                 'sap',
                        'ITEM':                'item',
                        'QTY':                 'qty',
                        'UNIT':                'unit',
                        'PART_NUMBER':         'part_number',
                        'BRAND':               'brand',
                        'DESCRIPTION':         'description',
                        'INCOTERM':            'incoterm',
                        'ATTACHMENT':          'attachment',
                        'NOTE':                'note',
                        'SALE-PRICE':          'sale_price',
                        'REFERENCE_PRICE':     'reference_price',
                        'DATE REQ':            'date_req',
                        'RP':                  'rp',
                        'ST':                  'st',
                        # Quotation-side fields (same names the create path uses)
                        'REP':                 'rep',
                        'CO':                  'quotation_co',
                        'DELIVERY':            'quotation_delivery',
                        'APROX WEIGHT':        'quotation_aprox_weight',
                        'APROX_WEIGHT':        'quotation_aprox_weight',
                    }

                    for excel_key, fieldname in simple_map.items():
                        if excel_key in item:
                            val = item[excel_key]
                            # Keep empty strings as empty (user intentionally cleared the cell)
                            if val is None:
                                val = ''
                            update_fields[fieldname] = val

                    # On UPDATE we still honour the ST value that came from the Excel
                    # (we do NOT force the logged-in user’s ST on updates)
                    if 'ST' in item:
                        update_fields['st'] = resolve_user_st_code(str(item.get('ST', '')))

                    # When the Excel also supplies the quotation-side mirrors we keep them in sync
                    if 'ITEM' in item:
                        update_fields['quotation_item'] = str(item.get('ITEM', ''))
                    if 'QTY' in item:
                        update_fields['quotation_qty'] = str(item.get('QTY', '')) if item.get('QTY') else '0'
                    if 'UNIT' in item:
                        update_fields['quotation_unit'] = str(item.get('UNIT', ''))
                    if 'PART_NUMBER' in item:
                        update_fields['quotation_part_number'] = str(item.get('PART_NUMBER', ''))   
                    if 'BRAND' in item:
                        update_fields['quotation_brand'] = str(item.get('BRAND', ''))
                    if 'DESCRIPTION' in item:
                        # Cross-search notes belong ONLY on description (request side).
                        # Strip a leading /*Cross … */ block so quotation_description
                        # keeps the original product text and never stores the cross data.
                        raw_desc = str(item.get('DESCRIPTION', '') or '')
                        clean_desc = re.sub(
                            r'^\s*/\*.*?Cross.*?\*/\s*',
                            '',
                            raw_desc,
                            flags=re.DOTALL | re.IGNORECASE
                        ).strip()
                        update_fields['quotation_description'] = clean_desc
                    if 'INCOTERM' in item:
                        update_fields['quotation_incoterm'] = str(item.get('INCOTERM', ''))  
                    if 'ATTACHMENT' in item:
                        update_fields['quotation_attachment'] = str(item.get('ATTACHMENT', ''))
                    if 'NOTE' in item:
                        update_fields['quotation_note'] = str(item.get('NOTE', ''))
                    if 'DATE' in item:
                        # Keep the same convention used on create
                        update_fields['quotation_rfqdate'] = str(item.get('DATE', ''))

                    # Never force quotation_sales_price on an update unless the Excel explicitly sent a sale-price column
                    # (the create path intentionally leaves it None)

                    if not update_fields:
                        frappe.log_error(
                            message=f"Row {index} (ID {row_id}): Excel contained only the ID column – nothing to update.",
                            title="Save Data Update Warning"
                        )
                        updated_ids.append(row_id)
                        continue

                    if 'CUSTOM_SALES_STATUS' in item and str(item.get('CUSTOM_SALES_STATUS') or '').strip() == 'S-SUBMITTED':
                        previous_status = frappe.db.get_value("Request And Quote", row_id, "custom_sales_status") or ""
                        if str(previous_status).strip() != "S-SUBMITTED":
                            update_fields["custom_requested_by"] = get_logged_in_user_st_code(current_user) or ""

                    # Perform the update
                    frappe.db.set_value(
                        "Request And Quote",
                        row_id,
                        update_fields,
                        update_modified=True
                    )
                    updated_ids.append(row_id)

                    gl_item_code = str(item.get('GL_ITEM_CODE') or '').strip()
                    if gl_item_code:
                        try:
                            from my_custom_app.item_management import save_raq_item_match
                            save_raq_item_match(
                                raq_name=str(row_id),
                                item_code=gl_item_code,
                                match_method=str(item.get('GL_MATCH_METHOD') or 'manual'),
                                match_status='confirmed'
                            )
                        except Exception as gl_err:
                            frappe.log_error(
                                message=f"RAQ {row_id} updated, but GL Item link to {gl_item_code} failed: {gl_err}",
                                title="Save Data GL Item Match Warning"
                            )

                    frappe.log_error(
                        message=f"Step 4.{index}: Successfully UPDATED record {row_id}. Fields written: {list(update_fields.keys())}",
                        title="Save Data Tracker"
                    )

                except Exception as doc_error:
                    error_msg = f"Failed updating record at index {index} (ID: {row_id}): {str(doc_error)}"
                    frappe.log_error(message=error_msg, title="Save Data Update Error")
                    frappe.local.response['http_status_code'] = 400
                    return {
                        "status": "error",
                        "message": error_msg,
                        "saved_ids_before_failure": saved_ids,
                        "updated_ids_before_failure": updated_ids
                    }

            else:
                # ===================== CREATE PATH =====================
                frappe.log_error(
                    message=f"Step 3.{index}: Preparing to insert record with intended ID {next_id}",
                    title="Save Data Tracker"
                )

                try:
                    # ------------------------------------------------------------------
                    # NEW REF LOGIC (Aug 2026)
                    # [Z] + custom_customer_category + ST(Customer preferred, else Address matching DIV) + "-"
                    # The part after the last "-" (if any) is treated as the user-typed sequential number.
                    # ------------------------------------------------------------------
                    original_ref = str(item.get('REF', '') or '').strip()
                    z_checked = bool(item.get('Z_CHECK') or item.get('z_check') or item.get('is_z'))
                    customer_name = str(item.get('CUSTOMER', '') or '').strip()
                    div_val = str(item.get('DIV', '') or '').strip()
                    st_for_ref = ""
                    cat = ""
                    if customer_name:
                        st_for_ref = get_st_code_for_ref(customer_name, div_val)
                        cat = (frappe.db.get_value("Customer", customer_name, "custom_customer_category") or "").strip()

                    auto_prefix = ""
                    if z_checked:
                        auto_prefix += "Z"
                    if st_for_ref:
                        auto_prefix += f"{cat}{st_for_ref}"

                    if original_ref:
                        # User-typed REF wins. Do not rebuild category/ST around it.
                        final_ref = original_ref
                        if z_checked and final_ref.upper()[:1] != "Z":
                            final_ref = "Z" + final_ref
                    elif auto_prefix:
                        final_ref = f"{auto_prefix}-"
                    else:
                        final_ref = original_ref

                    due_date_val = str(item.get('DUE_DATE') or '').strip()
                    due_time_val = str(item.get('DUE_TIME') or '').strip()
                    if not due_date_val or not due_time_val:
                        frappe.throw(
                            "DUE_DATE and DUE_TIME are required for every uploaded record "
                            f"(REF/SAP = {item.get('REF') or item.get('SAP') or '(unknown)'})."
                        )

                    uploader_st = get_logged_in_user_st_code(current_user) or ""
                    created_status = str(item.get('CUSTOM_SALES_STATUS') or '').strip()

                    mapped_item = {
                        # === Base fields ===
                        'ref': final_ref,
                        'custom_sales_status': str(item.get('CUSTOM_SALES_STATUS', '')),
                        'date': str(item.get('DATE', '')),
                        'country': str(item.get('COUNTRY', '')),
                        'customer': str(item.get('CUSTOMER', '')),
                        'div': str(item.get('DIV', '')),
                        'contact': str(item.get('CONTACT', '')),
                        'email_customr': str(item.get('EMAIL_CUSTOMER', '')),
                        'customer_ref': str(item.get('CUSTOMER_REF', '')),
                        'due_date': str(item.get('DUE_DATE', '')) if item.get('DUE_DATE') else None,
                        'due_time': str(item.get('DUE_TIME', '')) if item.get('DUE_TIME') else None,
                        'sap': str(item.get('SAP', '')),
                        'item': str(item.get('ITEM', '')),
                        'qty': str(item.get('QTY', '')) if item.get('QTY') else '0',
                        'unit': str(item.get('UNIT', '')),
                        'part_number': str(item.get('PART_NUMBER', '')),
                        'brand': str(item.get('BRAND', '')),
                        'description': str(item.get('DESCRIPTION', '')),
                        'incoterm': str(item.get('INCOTERM', '')),
                        'attachment': str(item.get('ATTACHMENT', '')),
                        'note': _prepend_customer_details(item.get('CUSTOMER') or item.get('customer'), item.get('NOTE', '')),
                        'sale_price': item.get('SALE-PRICE') or item.get('SALE_PRICE') or None,
                        'reference_price': item.get('REFERENCE_PRICE') or None,
                        'date_req': str(item.get('DATE REQ', '') or item.get('DATE_REQ', '')) or None,
                        'rp': str(item.get('RP', '')),
                        # NEW RULE: ST always comes from the logged-in user on create
                        'st': uploader_st,
                        'custom_uploaded_by': uploader_st,
                        'custom_requested_by': uploader_st if created_status == 'S-SUBMITTED' else '',

                        # === Quotation-side fields ===
                        'rep': str(item.get('REP', '') or item.get('rp', '')),
                        'quotation_item': str(item.get('ITEM', '')),
                        'quotation_qty': str(item.get('QTY', '')) if item.get('QTY') else '0',
                        'quotation_unit': str(item.get('UNIT', '')),
                        'quotation_part_number': str(item.get('PART_NUMBER', '')),
                        'quotation_brand': str(item.get('BRAND', '')),
                        # Cross-search notes stay on description only.
                        # quotation_description gets the same product text with the
                        # leading /*Cross … */ block removed.
                        'quotation_description': re.sub(
                            r'^\s*/\*.*?Cross.*?\*/\s*',
                            '',
                            str(item.get('DESCRIPTION', '') or ''),
                            flags=re.DOTALL | re.IGNORECASE
                        ).strip(),
                        'quotation_co': str(item.get('CO', '')),
                        'quotation_sales_price': None,          # NEVER copy SALE-PRICE on create
                        'quotation_delivery': str(item.get('DELIVERY', '')),
                        'quotation_aprox_weight': str(item.get('APROX WEIGHT', '') or item.get('APROX_WEIGHT', '')),
                        'quotation_incoterm': str(item.get('INCOTERM', '')),
                        'quotation_attachment': str(item.get('ATTACHMENT', '')),
                        'quotation_note': str(item.get('NOTE', '')),
                        'quotation_rfqdate': str(item.get('DATE', ''))
                    }

                    doc = frappe.get_doc({
                        "doctype": "Request And Quote",
                        **mapped_item
                    })

                    doc.name = str(next_id)
                    doc.creation = frappe.utils.now_datetime()
                    doc.modified = frappe.utils.now_datetime()
                    doc.owner = "Administrator"
                    doc.modified_by = "Administrator"
                    doc.docstatus = 0

                    doc.db_insert()
                    saved_ids.append(doc.name)

                    gl_item_code = str(item.get('GL_ITEM_CODE') or '').strip()
                    if gl_item_code:
                        try:
                            from my_custom_app.item_management import save_raq_item_match
                            save_raq_item_match(
                                raq_name=doc.name,
                                item_code=gl_item_code,
                                match_method=str(item.get('GL_MATCH_METHOD') or 'manual'),
                                match_status='confirmed'
                            )
                        except Exception as gl_err:
                            frappe.log_error(
                                message=f"RAQ {doc.name} saved, but GL Item link to {gl_item_code} failed: {gl_err}",
                                title="Save Data GL Item Match Warning"
                            )
                    elif current_user == "Administrator":
                        try:
                            gl_result = _admin_link_uploaded_item(doc, item)
                        except Exception as gl_err:
                            gl_result = {"linked": False, "reason": str(gl_err)}
                            frappe.log_error(
                                message=f"RAQ {doc.name} saved, but Administrator item link failed: {gl_err}",
                                title="Save Data Admin GL Item Warning"
                            )
                        if not hasattr(frappe.local, "_admin_gl_links"):
                            frappe.local._admin_gl_links = []
                        frappe.local._admin_gl_links.append({
                            "raq": doc.name,
                            "result": gl_result,
                        })
                    frappe.log_error(
                        message=f"Step 4.{index}: Successfully inserted record {doc.name}",
                        title="Save Data Tracker"
                    )
                    next_id += 1

                except Exception as doc_error:
                    error_msg = f"Failed inserting record at index {index} (Intended ID: {next_id}): {str(doc_error)}"
                    frappe.log_error(message=error_msg, title="Save Data Insert Error")
                    frappe.local.response['http_status_code'] = 400
                    return {
                        "status": "error",
                        "message": error_msg,
                        "saved_ids_before_failure": saved_ids,
                        "updated_ids_before_failure": updated_ids
                    }

        # After the loop
        frappe.db.commit()

        return {
            "status": "success",
            "ids": saved_ids,          # newly created
            "updated_ids": updated_ids, # existing records that were overwritten
            "admin_gl_links": getattr(frappe.local, "_admin_gl_links", []) if current_user == "Administrator" else []
        }

    except Exception as e:
        frappe.log_error(message=f"Error saving data: {str(e)}", title="Save Data Error (Test)")
        raise

    finally:
        # ============================================================
        # CRITICAL: never write None / empty / "None" back into the session
        # ============================================================
        if current_user and current_user not in (None, "None", ""):
            frappe.set_user(current_user)
        else:
            # Absolute last-resort fallback – keep the session alive as Guest
            frappe.set_user("Guest")

@frappe.whitelist(allow_guest=False)
def get_records(ids):
    try:
        if isinstance(ids, str):
            ids = json.loads(ids)
        if not isinstance(ids, list):
            throw("Invalid IDs format: Expected a list of IDs")

        records = []
        for id in ids:
            id = str(id).strip()
            doc = frappe.get_doc("Request For Quote", id)
            record = {
                "ID": doc.name,
                "SAP": doc.sap,
                "REF": doc.ref,
                "ST": doc.st,
                "CUSTOMER": doc.customer,
                "CUSTOMER_REF": doc.customer_ref,
                "DUE_DATE": doc.due_date,
                "DUE_TIME": doc.due_time,
                "ITEM": doc.item,
                "QTY": doc.qty,
                "UNIT": doc.unit,
                "PART_NUMBER": doc.part_number,
                "BRAND": doc.brand,
                "DESCRIPTION": doc.description,
                "NOTE": doc.note,
                "ATTACHMENT": doc.attachment
            }
            frappe.log_error(message=f"Fetched record: {record}", title="Record Fetched (Test)")
            records.append(record)

        return records
    except json.JSONDecodeError:
        throw("Invalid JSON format for IDs")
    except Exception as e:
        frappe.log_error(message=f"Error fetching records: {str(e)}", title="Fetch Records Error (Test)")
        throw(f"Error fetching records: {str(e)}")


@frappe.whitelist(allow_guest=False)
def get_banned_dates():
    try:
        banned_dates = frappe.get_all("banned_dates_upload", fields=["name", "date", "reason"], order_by="date asc")
        return banned_dates
    except Exception as e:
        frappe.log_error(message=f"Error fetching banned dates: {str(e)}", title="Fetch Banned Dates Error")
        throw(f"Error fetching banned dates: {str(e)}")


@frappe.whitelist(allow_guest=False)
def add_banned_date(date, reason):
    try:
        doc = frappe.get_doc({
            "doctype": "banned_dates_upload",
            "date": date,
            "reason": reason,
            "added_on": frappe.utils.nowdate(),
            "added_by": "" # Left blank as per requirement
        })
        doc.insert()
        return {"name": doc.name, "date": doc.date, "reason": doc.reason}
    except Exception as e:
        frappe.log_error(message=f"Error adding banned date: {str(e)}", title="Add Banned Date Error")
        throw(f"Error adding banned date: {str(e)}")


@frappe.whitelist(allow_guest=False)
def delete_banned_date(name):
    try:
        frappe.delete_doc("banned_dates_upload", name)
        return {"status": "success"}
    except Exception as e:
        frappe.log_error(message=f"Error deleting banned date: {str(e)}", title="Delete Banned Date Error")
        throw(f"Error deleting banned date: {str(e)}")


@frappe.whitelist(allow_guest=False)
def fetch_quote_data(view='quote', rep='', filterType=''):
    conn = mysql.connector.connect(
        host="glgeneralindustries.com",
        user="glgenera_uploadapp",
        password="kKh$8#-g7_[9",
        database="glgenera_intranet",
        port=3306
    )
    cursor = conn.cursor(dictionary=True)

    if view == 'quote':
        query = """
            SELECT q.*, rfq.SAP, rfq.DUE_TIME, rfq.ATTACHMENT
            FROM `quote` q
            LEFT JOIN `request for quote` rfq ON q.ID = rfq.ID
            WHERE (q.SALES_PRICE IS NULL OR q.SALES_PRICE = '')
            AND q.RFQDATE >= CURDATE()
        """
        if filterType == 'today_empty':
            query = query.replace("q.RFQDATE >= CURDATE()", "q.RFQDATE = CURDATE()")

        if rep:
            query += " AND q.REP = %s"
            cursor.execute(query, (rep,))
        else:
            cursor.execute(query)

    else: # view == 'assign'
        query = """
            SELECT *
            FROM `request for quote`
            WHERE date >= '2024-01-01' AND (rp IS NULL OR TRIM(rp) = '')
        """
        cursor.execute(query)

    records = cursor.fetchall()
    cursor.close()
    conn.close()

    prefixed_records = []
    for r in records:
        prefixed = {}
        for key, value in r.items():
            if view == 'quote' and key in ['SAP', 'DUE_TIME', 'ATTACHMENT']:
                prefixed[f'request for quote.{key}'] = value
            elif view == 'quote':
                prefixed[f'quote.{key}'] = value
            else:
                prefixed[key] = value
        prefixed_records.append(prefixed)

    return {'records': prefixed_records}


@frappe.whitelist(allow_guest=False)
def save_quote_data(data, view):
    conn = mysql.connector.connect(
        host="glgeneralindustries.com",
        user="glgenera_uploadapp",
        password="kKh$8#-g7_[9",
        database="glgenera_intranet",
        port=3306
    )
    cursor = conn.cursor()

    table = 'quote' if view == 'quote' else 'request for quote'

    for item in data:
        fields = ', '.join([f"`{k}` = %s" for k in item.keys() if k != 'ID'])
        values = [item[k] for k in item.keys() if k != 'ID'] + [item['ID']]
        query = f"UPDATE `{table}` SET {fields} WHERE ID = %s"
        cursor.execute(query, values)

    conn.commit()
    cursor.close()
    conn.close()

    return {'status': 'success'}


@frappe.whitelist(allow_guest=False)
def get_indicators():
    assign_count = frappe.db.sql("""
        SELECT COUNT(*)
        FROM `tabRequest For Quote`
        WHERE date >= '2024-01-01' AND (rp IS NULL OR TRIM(rp) = '')
    """)[0][0]

    daily_xs = frappe.db.sql("""
        SELECT COUNT(*) 
        FROM `tabQuote` 
        WHERE rfqdate = CURDATE() AND sales_price REGEXP '[xX]'
    """)[0][0]

    empties_today = frappe.db.sql("""
        SELECT COUNT(*) 
        FROM `tabQuote` 
        WHERE rfqdate = CURDATE() AND (sales_price IS NULL OR sales_price = '')
    """)[0][0]

    today_empty = frappe.db.sql("""
        SELECT COUNT(*) 
        FROM `tabQuote` 
        WHERE rfqdate = CURDATE() AND (sales_price IS NULL OR TRIM(sales_price) = '')
    """)[0][0]

    return {'assign': assign_count, 'dailyXs': daily_xs, 'emptiesToday': empties_today, 'todayEmpty': today_empty}


@frappe.whitelist(allow_guest=False)
def process_ifm_quote():
    ref_number = frappe.form_dict.get('ref_number')
    pdf_file = frappe.request.files.get('pdf_file')

    if not ref_number or not pdf_file:
        throw("Reference number and PDF file are required.")

    file_doc = get_doc({
        "doctype": "File",
        "file_name": pdf_file.filename,
        "content": pdf_file.read(),
        "is_private": 1
    })
    file_doc.insert()

    pdf_path = file_doc.get_full_path()
    processed_data = process_ifm_pdf(pdf_path)

    return processed_data


@frappe.whitelist(allow_guest=False)
def create_contact(contact_data):
    try:
        frappe.log_error(message=f"Received contact_data type: {type(contact_data)}", title="Contact Data Type (Test)")
        frappe.log_error(message=f"Received contact_data: {contact_data}", title="Contact Data Received (Test)")

        if isinstance(contact_data, str):
            contact_data = json.loads(contact_data)

        if not isinstance(contact_data, dict):
            raise ValueError(f"Expected contact_data to be a dictionary, got {type(contact_data)}")

        customer = contact_data['customer']
        first_name = contact_data['first_name']
        middle_name = contact_data.get('middle_name', '')
        last_name = contact_data.get('last_name', '')
        second_last_name = contact_data.get('second_last_name', '')
        email = contact_data['email']

        if not frappe.db.exists("Customer", customer):
            return {"status": "error", "error": f"Customer '{customer}' not found in ERPNext"}

        contact = frappe.get_doc({
            "doctype": "Contact",
            "first_name": first_name,
            "middle_name": middle_name if middle_name else None,
            "last_name": last_name if last_name else None,
            "second_last_name": second_last_name if second_last_name else None,
            "email_ids": [{
                "email_id": email,
                "is_primary": 1
            }],
            "links": [{
                "link_doctype": "Customer",
                "link_name": customer
            }]
        })
        contact.insert()

        frappe.log_error(message=f"Contact created successfully for email: {email}", title="Contact Created (Test)")
        return {"status": "success", "email": email}

    except Exception as e:
        frappe.log_error(message=f"Error in create_contact: {str(e)}", title="Contact Creation Error (Test)")
        return {"status": "error", "error": str(e)}


@frappe.whitelist(allow_guest=False)
def get_customers():
    """
    Fetches a list of all customers, plus 'new template' entries for
    customers who have at least one mapping template.
    """
    try:
        # 1. Get all existing customers and their auto_upload status (Filtered by auto_upload = 1)
        existing_customers = frappe.get_all("Customer", filters={"auto_upload": 1}, fields=["name", "auto_upload"], order_by="name asc")

        # 2. Get all customers who have at least one NEW mapping template
        new_template_customers_raw = frappe.db.sql("""
            SELECT DISTINCT customer
            FROM `tabUpload Mapping Template`
            WHERE is_active = 1
            AND customer IS NOT NULL
            AND customer != ''
        """, as_dict=True)

        new_template_customer_set = {d.customer for d in new_template_customers_raw}

        # 3. Build the final list
        final_customer_list = []

        # Add existing customers
        for cust in existing_customers:
            final_customer_list.append({
                "name": cust.name,
                "auto_upload": cust.auto_upload or 0,
                "is_new_template": 0
            })

        # Add 'new template' entries for customers who have them
        for customer_name in new_template_customer_set:
            final_customer_list.append({
                "name": customer_name,
                "auto_upload": 0,
                "is_new_template": 1
            })

        return final_customer_list

    except Exception as e:
        frappe.log_error(f"Error fetching customers: {e}", "API Error")
        return []


@frappe.whitelist(allow_guest=False)
def get_customer_details(customer_name):
    """
    Fetches detailed information for a specific customer for auto-population.
    """
    try:
        if not frappe.db.exists("Customer", customer_name):
            frappe.throw(f"Customer {customer_name} not found")

        customer_doc = frappe.get_doc("Customer", customer_name)

        st_code = ''
        st_user_email = customer_doc.get("st")
        if st_user_email:
            if frappe.db.exists("User", st_user_email):
                user_doc = frappe.get_doc("User", st_user_email)
                st_code_from_user = user_doc.get("st")
                if st_code_from_user and st_code_from_user.isdigit() and len(st_code_from_user) == 3:
                        st_code = str(st_code_from_user)

        # --- FIX: Replaced frappe.get_all with frappe.db.sql for correct link filtering ---
        addresses = frappe.db.sql("""
            SELECT address_title, country, address_type
            FROM `tabAddress`
            WHERE name IN (
                SELECT parent
                FROM `tabDynamic Link`
                WHERE link_doctype = 'Customer'
                AND link_name = %s
            )
        """, customer_name, as_dict=True)

        divisions = [addr.get("address_title") for addr in addresses if addr.get("address_title")]
        billing_address = next((addr for addr in addresses if addr.get("address_type") == "Billing"), None)
        country = billing_address.get("country") if billing_address else ""

        # --- FIX: Replaced frappe.get_all with frappe.db.sql for correct link filtering ---
        contacts_raw = frappe.db.sql("""
            SELECT name, first_name, last_name
            FROM `tabContact`
            WHERE name IN (
                SELECT parent
                FROM `tabDynamic Link`
                WHERE link_doctype = 'Customer'
                AND link_name = %s
            )
        """, customer_name, as_dict=True)

        contacts = []
        for contact in contacts_raw:
            email_id = frappe.db.get_value("Contact Email", {"parent": contact.name, "is_primary": 1}, "email_id")
            contact['email'] = email_id if email_id else ""
            contacts.append(contact)

        return {
            "st": st_code,
            "divisions": sorted(list(set(divisions))),
            "country": country,
            "contacts": contacts
        }
    except Exception as e:
        frappe.log_error(f"Error fetching details for customer {customer_name}: {e}", "API Error")
        frappe.throw(str(e))


@frappe.whitelist(allow_guest=False)
def search_sap_in_remote_db(sap):
    if not sap:
        return {"status": "error", "message": "SAP number is required"}

    try:
        results = frappe.db.sql("""
            SELECT sap AS SAP, part_number AS PART_NUMBER, brand AS BRAND, 
                   description AS DESCRIPTION, date AS DATE, customer AS CUSTOMER
            FROM `tabRequest For Quote`
            WHERE sap = %s
        """, (sap,), as_dict=True)

        if not results:
            return {"status": "not_found"}

        for r in results:
            if isinstance(r.get('DATE'), date):
                r['DATE'] = r['DATE'].strftime('%Y-%m-%d')

        if len(results) == 1:
            return {"status": "found_single", "data": results[0]}
        else:
            distinct_records = []
            seen_descriptions = set()
            for record in results:
                description = record.get('DESCRIPTION')
                if description not in seen_descriptions:
                    distinct_records.append(record)
                    seen_descriptions.add(description)

            return {"status": "found_multiple", "data": distinct_records}

    except Exception as e:
        frappe.log_error(f"Error searching remote DB for SAP {sap}: {e}", "Remote DB Error")
        return {"status": "error", "message": str(e)}
    finally:
        if conn and conn.is_connected():
            cursor.close()
            conn.close()


# The rest of the file remains the same. The following is the original content from that point.

field_to_column = {
    'ID': 'q.name',
    'REP': 'q.rep',
    'REF': 'q.ref',
    'RFQDATE': 'q.rfqdate',
    'ITEM': 'q.item',
    'QTY': 'q.qty',
    'UNIT': 'q.unit',
    'PART_NUMBER': 'q.part_number',
    'BRAND': 'q.brand',
    'DESCRIPTION': 'q.description',
    'CO': 'q.co',
    'SALES_PRICE': 'q.sales_price',
    'DELIVERY': 'q.delivery',
    'APROX WEIGHT': 'q.aprox_weight',
    'INCOTERM': 'q.incoterm',
    'NOTE': 'q.note',
    'CUSTOMER_REF': 'q.customer_ref',
    'SAP': 'rfq.sap',
    'COUNTRY': 'rfq.country',
    'RFQ_NOTE': 'rfq.note',
    'DUE_TIME': 'rfq.due_time',
    'SUPPLIER': 's.supplier',
    'CONTACT': 's.contact',
    'EMAIL_CONTACT': 's.email_contact',
    'COST_EA': 's.cost_ea',
    'SUPPLIER_DELIVERY': 's.delivery',
    'SUPPLIER_NOTE': 's.note',
    'REP COUNTRY': 's.rep_country',
    'ATTACHMENT': 'rfq.attachment'
}
field_to_table = {
    'ID': 'q',
    'REP': 'q',
    'REF': 'q',
    'RFQDATE': 'q',
    'ITEM': 'q',
    'QTY': 'q',
    'UNIT': 'q',
    'PART_NUMBER': 'q',
    'BRAND': 'q',
    'DESCRIPTION': 'q',
    'CO': 'q',
    'SALES_PRICE': 'q',
    'DELIVERY': 'q',
    'APROX WEIGHT': 'q',
    'INCOTERM': 'q',
    'NOTE': 'q',
    'CUSTOMER_REF': 'q',
    'SAP': 'rfq',
    'COUNTRY': 'rfq',
    'RFQ_NOTE': 'rfq',
    'DUE_TIME': 'rfq',
    'SUPPLIER': 's',
    'CONTACT': 's',
    'EMAIL_CONTACT': 's',
    'COST_EA': 's',
    'SUPPLIER_DELIVERY': 's',
    'SUPPLIER_NOTE': 's',
    'REP COUNTRY': 's',
    'ATTACHMENT': 'rfq'
}
number_fields = ['ID', 'QTY', 'APROX WEIGHT', 'COST_EA']

def get_condition(field, criteria, value):
    table = field_to_table.get(field, 'q')
    column = f"{table}.`{field}`" if ' ' in field else f"{table}.{field}"

    if criteria == 'equals':
        if field in number_fields:
            return f"CAST({column} AS DECIMAL) = %s", value
        else:
            return f"{column} = %s", value
    elif criteria == 'contains':
        return f"{column} LIKE %s", f"%{value}%"
    elif criteria == 'starts with':
        return f"{column} LIKE %s", f"{value}%"
    elif criteria == 'ends with':
        return f"{column} LIKE %s", f"%{value}"
    elif criteria == 'greater than':
        if field in number_fields:
            return f"CAST({column} AS DECIMAL) > %s", value
        else:
            return f"{column} > %s", value
    elif criteria == 'less than':
        if field in number_fields:
            return f"CAST({column} AS DECIMAL) < %s", value
        else:
            return f"{column} < %s", value
    elif criteria == 'before':
        return f"{column} < %s", value
    elif criteria == 'after':
        return f"{column} > %s", value
    elif criteria == 'is empty':
        if field in number_fields:
            return f"{column} IS NULL", None
        else:
            return f"({column} IS NULL OR {column} = '')", None
    else:
        return None, None


@frappe.whitelist(allow_guest=False)
def fetch_master_quote_data(page=1, limit=10, sort_field='ID', sort_direction='ASC', filters=None):
    if filters is None:
        filters = []
    elif isinstance(filters, str):
        try:
            filters = json.loads(filters)
        except json.JSONDecodeError:
            filters = []

    if not isinstance(filters, list):
        filters = []

    query_base = """
    SELECT
        q.name AS ID, q.ref AS REF, q.country AS quote_COUNTRY, q.rep AS REP, q.item AS ITEM, q.qty AS QTY, q.unit AS UNIT,
        q.part_number AS PART_NUMBER, q.brand AS BRAND, q.description AS DESCRIPTION, q.co AS CO, q.sales_price AS SALES_PRICE, q.delivery AS DELIVERY,
        q.aprox_weight AS `APROX WEIGHT`, q.incoterm AS INCOTERM, q.customer_ref AS CUSTOMER_REF, q.rfqdate AS RFQDATE, q.note AS NOTE,
        rfq.sap AS SAP, rfq.country AS rfq_COUNTRY, rfq.note AS RFQ_NOTE, rfq.due_time AS DUE_TIME, rfq.attachment AS ATTACHMENT,
        s.supplier AS SUPPLIER, s.contact AS CONTACT, s.email_contact AS EMAIL_CONTACT, s.cost_ea AS COST_EA, s.delivery AS SUPPLIER_DELIVERY,
        s.note AS SUPPLIER_NOTE, s.rep_country AS `REP COUNTRY`
    FROM `tabQuote` q
    LEFT JOIN `tabRequest For Quote` rfq ON q.name = rfq.name
    LEFT JOIN `tabSupplier` s ON q.name = s.name
    """

    where_clauses = []
    params = []

    for filter in filters:
        if not isinstance(filter, dict):
            continue
        field = filter.get('field')
        criteria = filter.get('criteria')
        value = filter.get('value')

        if field and criteria:
            condition, param = get_condition(field, criteria, value)
            if condition:
                where_clauses.append(condition)
                if param is not None:
                    params.append(param)

    count_query = """
        SELECT COUNT(*)
        FROM `tabQuote` q
        LEFT JOIN `tabRequest For Quote` rfq ON q.name = rfq.name
        LEFT JOIN `tabSupplier` s ON q.name = s.name
    """

    if where_clauses:
        count_query += " WHERE " + " AND ".join(where_clauses)
        total_records = frappe.db.sql(count_query, tuple(params))[0][0]
    else:
        total_records = frappe.db.sql(count_query)[0][0]

    if where_clauses:
        query_base += " WHERE " + " AND ".join(where_clauses)

    sort_column = field_to_column.get(sort_field, 'q.name')
    sort_direction = sort_direction.upper() if sort_direction.upper() in ['ASC', 'DESC'] else 'ASC'

    query_base += f" ORDER BY {sort_column} {sort_direction}"

    offset = (int(page) - 1) * int(limit)

    if limit == 'ALL':
        records = frappe.db.sql(query_base, tuple(params), as_dict=True)
    else:
        query = query_base + " LIMIT %s OFFSET %s"
        params.extend([int(limit), offset])
        records = frappe.db.sql(query, tuple(params), as_dict=True)

    return {'records': records, 'total_records': total_records}




@frappe.whitelist(allow_guest=False)
def fetch_master_quote_count(filters=None):
    if filters is None:
        filters = []
    elif isinstance(filters, str):
        try:
            filters = json.loads(filters)
        except json.JSONDecodeError:
            filters = []

    if not isinstance(filters, list):
        filters = []

    query_base = """
        SELECT COUNT(*) as count
        FROM `tabQuote` q
        LEFT JOIN `tabRequest For Quote` rfq ON q.name = rfq.name
        LEFT JOIN `tabSupplier` s ON q.name = s.name
    """

    where_clauses = []
    params = []

    for filter in filters:
        if not isinstance(filter, dict):
            continue
        field = filter.get('field')
        criteria = filter.get('criteria')
        value = filter.get('value')

        if field and criteria:
            condition, param = get_condition(field, criteria, value)
            if condition:
                where_clauses.append(condition)
                if param is not None:
                    params.append(param)

    if where_clauses:
        query_base += " WHERE " + " AND ".join(where_clauses)

    result = frappe.db.sql(query_base, tuple(params), as_dict=True)
    count = result[0]['count'] if result else 0

    return {'count': count}


@frappe.whitelist(allow_guest=False)
def save_master_quote_data(data):
    conn = mysql.connector.connect(
        host="glgeneralindustries.com",
        user="glgenera_uploadapp",
        password="kKh$8#-g7_[9",
        database="glgenera_intranet",
        port=3306
    )
    cursor = conn.cursor()

    if isinstance(data, str):
        data = json.loads(data)

    for item in data:
        id_value = item['ID']

        quote_fields = [
            'REF', 'REP', 'ITEM', 'QTY', 'UNIT', 'PART_NUMBER', 'BRAND',
            'DESCRIPTION', 'CO', 'SALES_PRICE', 'DELIVERY', 'APROX WEIGHT',
            'INCOTERM', 'CUSTOMER_REF', 'RFQDATE', 'NOTE'
        ]
        quote_fields_available = [field for field in quote_fields if field in item]

        if quote_fields_available:
            quote_set_clause = ', '.join([f"`{field}` = %s" for field in quote_fields_available])
            quote_values = [item[field] for field in quote_fields_available] + [id_value]
            quote_query = f"UPDATE `quote` SET {quote_set_clause} WHERE ID = %s"
            cursor.execute(quote_query, quote_values)

        rfq_field_map = {
            'SAP': 'SAP',
            'rfq_COUNTRY': 'COUNTRY',
            'RFQ_NOTE': 'NOTE',
            'DUE_TIME': 'DUE_TIME'
        }
        rfq_fields_available = [field for field in rfq_field_map.keys() if field in item]

        if rfq_fields_available:
            rfq_db_fields = [rfq_field_map[field] for field in rfq_fields_available]
            rfq_set_clause = ', '.join([f"`{field}` = %s" for field in rfq_db_fields])
            rfq_values = [item[field] for field in rfq_fields_available] + [id_value]
            rfq_query = f"UPDATE `request for quote` SET {rfq_set_clause} WHERE ID = %s"
            cursor.execute(rfq_query, rfq_values)

        supplier_field_map = {
            'SUPPLIER': 'SUPPLIER',
            'CONTACT': 'CONTACT',
            'EMAIL_CONTACT': 'EMAIL_CONTACT',
            'COST_EA': 'COST_EA',
            'SUPPLIER_DELIVERY': 'DELIVERY',
            'SUPPLIER_NOTE': 'NOTE',
            'REP COUNTRY': 'REP COUNTRY'
        }
        supplier_fields_available = [field for field in supplier_field_map.keys() if field in item]

        if supplier_fields_available:
            cursor.execute("SELECT COUNT(*) FROM supplier WHERE ID = %s", (id_value,))
            exists = cursor.fetchone()[0] > 0

            if exists:
                set_clause = ', '.join([f"`{supplier_field_map[field]}` = %s" for field in supplier_fields_available])
                values = [item[field] for field in supplier_fields_available] + [id_value]
                query = f"UPDATE supplier SET {set_clause} WHERE ID = %s"
                cursor.execute(query, values)
            else:
                db_fields = [supplier_field_map[field] for field in supplier_fields_available]
                fields = ['ID'] + db_fields
                placeholders = ', '.join(['%s'] * len(fields))
                query = f"INSERT INTO supplier ({', '.join([f'`{field}`' for field in fields])}) VALUES ({placeholders})"
                values = [id_value] + [item[field] for field in supplier_fields_available]
                cursor.execute(query, values)

    conn.commit()
    cursor.close()
    conn.close()

    return {'status': 'success'}


@frappe.whitelist(allow_guest=False)
def create_bid_project(selected_records):
    if isinstance(selected_records, str):
        selected_records = json.loads(selected_records)

    if not isinstance(selected_records, list) or not selected_records:
        frappe.throw("Selected records must be a non-empty list")

    selected_ids = [record['id'] for record in selected_records]
    selected_records_dict = {rec['id']: rec['division'] for rec in selected_records}

    records = frappe.db.sql("""
        SELECT q.name AS ID, q.ref AS REF, q.item AS ITEM, q.qty AS QTY, q.unit AS UNIT, q.part_number AS PART_NUMBER, q.brand AS BRAND,
               q.description AS DESCRIPTION, q.delivery AS DELIVERY, rfq.due_time AS DUE_TIME
        FROM `tabQuote` q
        LEFT JOIN `tabRequest For Quote` rfq ON q.name = rfq.name
        WHERE q.name IN %s
    """, (tuple(selected_ids),), as_dict=True)

    if not records:
        frappe.throw("No records found for the selected IDs")

    for record in records:
        record['DIVISION'] = selected_records_dict.get(record['ID'], '')

    bid_project = frappe.get_doc({
        "doctype": "Bid Project",
        "bid_ref": records[0]["REF"],
        "status": "Sent",
        "due_date": records[0]["DUE_TIME"] or datetime.date.today().strftime('%Y-%m-%d'),
        "bid_project_items": []
    })

    for record in records:
        item_code = record["PART_NUMBER"] or f"ITEM-{record['ID']}"
        item_exists = frappe.db.exists("Item", {"part_number": record["PART_NUMBER"]})

        if not item_exists:
            item = frappe.get_doc({
                "doctype": "Item",
                "item_code": item_code,
                "item_name": "",
                "division": record["DIVISION"] or "",
                "part_number": record["PART_NUMBER"] or "",
                "brand": record["BRAND"] or "",
                "description": record["DESCRIPTION"] or "",
                "item_group": "All Item Groups"
            })
            item.insert()
        else:
            item = frappe.get_doc("Item", {"part_number": record["PART_NUMBER"]})

        bid_project.append("bid_project_items", {
            "bid_id": record["ID"],
            "item_code": item.name,
            "brand": record["BRAND"] or "",
            "division": record["DIVISION"] or "",
            "due_date": record["DUE_TIME"] or datetime.date.today().strftime('%Y-%m-%d'),
            "gl_reference": record["REF"],
            "req_delivery_date": record["DELIVERY"] or ""
        })

    bid_project.insert()

    return {"name": bid_project.name}


@frappe.whitelist(allow_guest=False)
def get_saved_templates():
    try:
        templates = frappe.get_all("Upload Saved Templates", fields=["template_name"], order_by="template_name asc")
        return templates
    except Exception as e:
        frappe.log_error(message=f"Error fetching saved templates: {str(e)}", title="Fetch Saved Templates Error")
        throw(f"Error fetching saved templates: {str(e)}")


@frappe.whitelist(allow_guest=False)
def save_template(template_name, template_data):
    try:
        if not template_name:
            throw("Template name is required")

        if isinstance(template_data, str):
            template_data = json.loads(template_data)

        if not isinstance(template_data, dict):
            throw(f"Expected template_data to be a dictionary, got {type(template_data)}")

        doc = frappe.get_doc({
            "doctype": "Upload Saved Templates",
            "template_name": template_name,
            "template_data": template_data
        })
        doc.insert()
        return {"status": "success", "template_name": doc.template_name}

    except Exception as e:
        frappe.log_error(message=f"Error saving template: {str(e)}", title="Save Template Error")
        throw(f"Error saving template: {str(e)}")


@frappe.whitelist(allow_guest=False)
def get_template(template_name):
    try:
        doc = frappe.get_doc("Upload Saved Templates", template_name)
        # The template_data field in Doctype is Text, so it will be a string.
        # It needs to be parsed back into a dictionary/object before returning.
        template_data_str = doc.template_data
        template_data_obj = json.loads(template_data_str) if template_data_str else {}

        return {
            "template_name": doc.template_name,
            "parsed_data": template_data_obj.get('parsed_data'),
            "file_name": template_data_obj.get('file_name')
        }
    except Exception as e:
        frappe.log_error(message=f"Error fetching template {template_name}: {str(e)}", title="Fetch Template Error")
        throw(f"Error fetching template {template_name}: {str(e)}")