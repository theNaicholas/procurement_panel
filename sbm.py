# Copyright (c) 2026, GL General Industries Corp
# License: MIT

"""
Supplier-Brand Management backend.

Extracted from my_custom_app.procurement_panel.
These are the functions required by sbm.html.

Install:
    apps/my_custom_app/my_custom_app/sbm.py

sbm.html currently calls my_custom_app.procurement_panel.<fn>.
That still works if you leave the originals in procurement_panel.py.
To point the page at this module, change those calls to
my_custom_app.sbm.<fn> and run: bench restart
"""

import frappe
import json


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
                unique_sups[sup_name] = {'name': sup_name, 'supplier_name': human_name, 'contact': contact_name, 'email': email, 'default_cc': s.get('default_cc'), 'brands': brand_map.get(sup_name, []), 'score': score}
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

        return {"status": "success", "data": list(unique_sups.values())}
    except Exception as e:
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
        if "supplier_display_name" in data and data.get("supplier_display_name"):
            sup.supplier_name = data.get("supplier_display_name")
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
def search_sbm_contacts(query, supplier_name=None):
    """
    Search existing Contacts by first name, last name, or email.
    Used by the SBM Add Contact modal dropdown.
    """
    try:
        query = (query or "").strip()
        supplier_name = (supplier_name or "").strip()
        if len(query) < 2:
            return {"status": "success", "data": []}

        like = "%" + query.replace("%", "\\%").replace("_", "\\_") + "%"
        rows = frappe.db.sql(
            """
            SELECT
                c.name,
                c.first_name,
                c.last_name,
                IFNULL(
                    c.email_id,
                    (
                        SELECT email_id
                        FROM `tabContact Email`
                        WHERE parent = c.name
                        ORDER BY is_primary DESC
                        LIMIT 1
                    )
                ) as email_id
            FROM `tabContact` c
            WHERE
                IFNULL(c.first_name, '') LIKE %(q)s
                OR IFNULL(c.last_name, '') LIKE %(q)s
                OR CONCAT(IFNULL(c.first_name, ''), ' ', IFNULL(c.last_name, '')) LIKE %(q)s
                OR IFNULL(c.email_id, '') LIKE %(q)s
                OR EXISTS (
                    SELECT 1
                    FROM `tabContact Email` ce
                    WHERE ce.parent = c.name
                      AND IFNULL(ce.email_id, '') LIKE %(q)s
                )
            ORDER BY c.first_name ASC, c.last_name ASC
            LIMIT 20
            """,
            {"q": like},
            as_dict=True,
        )

        already = set()
        if supplier_name:
            linked = frappe.db.sql(
                """
                SELECT parent
                FROM `tabDynamic Link`
                WHERE parenttype = 'Contact'
                  AND link_doctype = 'Supplier'
                  AND link_name = %s
                """,
                (supplier_name,),
                as_list=True,
            )
            already = {row[0] for row in linked if row and row[0]}

        data = []
        for row in rows:
            data.append({
                "name": row.name,
                "first_name": row.first_name or "",
                "last_name": row.last_name or "",
                "email_id": row.email_id or "",
                "already_linked": row.name in already,
            })

        return {"status": "success", "data": data}
    except Exception as e:
        frappe.log_error("Search SBM Contacts Error", str(e))
        return {"status": "error", "error": str(e)}


@frappe.whitelist()
def link_sbm_contact(supplier_name, contact_name):
    """
    Link an existing Contact to a Supplier via Dynamic Link.
    Does not create a new Contact.
    """
    try:
        supplier_name = (supplier_name or "").strip()
        contact_name = (contact_name or "").strip()
        if not supplier_name or not contact_name:
            return {"status": "error", "error": "Supplier and Contact are required."}
        if not frappe.db.exists("Supplier", supplier_name):
            return {"status": "error", "error": "Supplier not found."}
        if not frappe.db.exists("Contact", contact_name):
            return {"status": "error", "error": "Contact not found."}

        already = frappe.db.exists(
            "Dynamic Link",
            {
                "parent": contact_name,
                "parenttype": "Contact",
                "link_doctype": "Supplier",
                "link_name": supplier_name,
            },
        )
        if already:
            return {"status": "error", "error": "This contact is already linked to the selected supplier."}

        contact = frappe.get_doc("Contact", contact_name)
        contact.append("links", {"link_doctype": "Supplier", "link_name": supplier_name})
        contact.save(ignore_permissions=True)

        sup = frappe.get_doc("Supplier", supplier_name)
        if not sup.supplier_primary_contact:
            sup.supplier_primary_contact = contact.name
            sup.save(ignore_permissions=True)

        frappe.db.commit()
        return {"status": "success"}
    except Exception as e:
        frappe.log_error("Link SBM Contact Error", str(e))
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
def delete_sbm_address(supplier_name, address_name):
    """
    Permanently delete an Address that is linked to the given Supplier.
    Procurement T3 / Administrator / System Manager only.
    """
    try:
        supplier_name = (supplier_name or "").strip()
        address_name = (address_name or "").strip()
        if not supplier_name or not address_name:
            return {"status": "error", "error": "Supplier and Address are required."}

        roles = frappe.get_roles(frappe.session.user)
        is_admin = frappe.session.user == "Administrator" or "System Manager" in roles
        if not is_admin and "Procurement T3" not in roles:
            return {"status": "error", "error": "Only Procurement T3 can delete supplier addresses."}

        if not frappe.db.exists("Address", address_name):
            return {"status": "error", "error": "Address not found."}

        linked = frappe.db.exists(
            "Dynamic Link",
            {
                "parent": address_name,
                "parenttype": "Address",
                "link_doctype": "Supplier",
                "link_name": supplier_name,
            },
        )
        if not linked:
            return {"status": "error", "error": "This address is not linked to the selected supplier."}

        sup = frappe.get_doc("Supplier", supplier_name)
        if sup.supplier_primary_address == address_name:
            sup.supplier_primary_address = None
            sup.save(ignore_permissions=True)

        frappe.delete_doc("Address", address_name, ignore_permissions=True)
        frappe.db.commit()
        return {"status": "success"}
    except Exception as e:
        frappe.log_error("Delete SBM Address Error", str(e))
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

        brand_files = frappe.get_all(
            "File",
            filters={"attached_to_doctype": "Brand", "attached_to_name": brand_name},
            fields=["name", "file_name", "file_url"],
            ignore_permissions=True
        ) or []

        div_names = [d.name for d in divisions if d.name]
        div_files_by_div = {}
        if div_names:
            div_files = frappe.get_all(
                "File",
                filters={"attached_to_doctype": "brand_division_glgnet", "attached_to_name": ["in", div_names]},
                fields=["name", "file_name", "file_url", "attached_to_name"],
                ignore_permissions=True
            ) or []
            for f in div_files:
                div_files_by_div.setdefault(f.attached_to_name, []).append({
                    "name": f.name,
                    "file_name": f.file_name,
                    "file_url": f.file_url
                })

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
                    ) as email
                FROM `tabSupplier` s
                LEFT JOIN `tabContact` c ON c.name = s.supplier_primary_contact
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
                "attachments": div_files_by_div.get(d.name, []),
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
                "attachments": [],
                "suppliers": orphan_rels
            })

        return {
            "status": "success",
            "data": {
                "brand": brand_name,
                "attachments": brand_files,
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
            # frappe.rename_doc() wrapper in this Frappe version does not accept
            # ignore_permissions. Call the inner rename implementation instead.
            from frappe.model.rename_doc import rename_doc as _rename_doc
            _rename_doc(
                "Brand",
                old_name,
                new_name,
                force=True,
                ignore_permissions=True,
            )
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

        if frappe.db.exists("DocType", "Supplier Brand Relationship"):
            rels = frappe.get_all(
                "Supplier Brand Relationship",
                filters={"brand": brand_name},
                pluck="name"
            ) or []
            for rel_name in rels:
                try:
                    frappe.delete_doc(
                        "Supplier Brand Relationship",
                        rel_name,
                        ignore_permissions=True,
                        force=True
                    )
                except Exception:
                    frappe.db.sql(
                        "DELETE FROM `tabSupplier Brand Relationship` WHERE name = %s",
                        (rel_name,),
                    )

        divs = []
        if frappe.db.exists("DocType", "brand_division_glgnet"):
            divs = frappe.get_all(
                "brand_division_glgnet",
                filters={"brand": brand_name},
                pluck="name"
            ) or []

        for div_name in divs:
            if frappe.db.exists("DocType", "Brand Division Responsible"):
                resp_docs = frappe.get_all(
                    "Brand Division Responsible",
                    filters={"brand_division": div_name},
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
            else:
                # DocType missing: still drop any leftover rows so Brand delete
                # does not later trip on a dangling link check.
                try:
                    frappe.db.sql(
                        "DELETE FROM `tabBrand Division Responsible` WHERE brand_division = %s",
                        (div_name,),
                    )
                except Exception:
                    pass

            try:
                frappe.delete_doc(
                    "brand_division_glgnet",
                    div_name,
                    ignore_permissions=True,
                    force=True
                )
            except Exception:
                frappe.db.sql(
                    "DELETE FROM `tabbrand_division_glgnet` WHERE name = %s",
                    (div_name,),
                )

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
def get_sbm_supplier_activity(supplier_name):
    """
    Supplier Activity feed:
    - Version rows for the Supplier
    - Version rows for this supplier's Supplier Brand Relationship docs
    Newest first.
    """
    try:
        supplier_name = (supplier_name or "").strip()
        if not supplier_name:
            return {"status": "error", "error": "Supplier is required."}

        events = []

        supplier_versions = frappe.get_all(
            "Version",
            filters={"ref_doctype": "Supplier", "docname": supplier_name},
            fields=["name", "creation", "owner", "data"],
            order_by="creation desc",
            limit=40,
        ) or []

        for v in supplier_versions:
            details = ""
            action = "Supplier updated"
            try:
                payload = json.loads(v.data) if v.data else {}
                changed = payload.get("changed") or []
                added = payload.get("added") or []
                if isinstance(changed, list) and changed:
                    fields = []
                    for row in changed:
                        if isinstance(row, (list, tuple)) and row:
                            fields.append(str(row[0]))
                        elif isinstance(row, str):
                            fields.append(row)
                    if fields:
                        details = "Changed: " + ", ".join(fields[:12])
                        action = "Supplier fields changed"
                elif added:
                    action = "Supplier rows added"
            except Exception:
                details = ""
            events.append({
                "title": action,
                "action": action,
                "creation": v.creation,
                "owner": v.owner,
                "ref_doctype": "Supplier",
                "docname": supplier_name,
                "details": details,
            })

        rel_names = frappe.get_all(
            "Supplier Brand Relationship",
            filters={"supplier": supplier_name},
            pluck="name",
        ) or []

        if rel_names:
            rel_versions = frappe.get_all(
                "Version",
                filters={
                    "ref_doctype": "Supplier Brand Relationship",
                    "docname": ["in", rel_names],
                },
                fields=["name", "creation", "owner", "data", "docname"],
                order_by="creation desc",
                limit=40,
            ) or []
            for v in rel_versions:
                details = ""
                action = "Brand relationship updated"
                try:
                    payload = json.loads(v.data) if v.data else {}
                    changed = payload.get("changed") or []
                    if isinstance(changed, list) and changed:
                        fields = []
                        for row in changed:
                            if isinstance(row, (list, tuple)) and row:
                                fields.append(str(row[0]))
                        if fields:
                            details = "Changed: " + ", ".join(fields[:12])
                except Exception:
                    details = ""
                events.append({
                    "title": action,
                    "action": action,
                    "creation": v.creation,
                    "owner": v.owner,
                    "ref_doctype": "Supplier Brand Relationship",
                    "docname": v.docname,
                    "details": details,
                })

        events.sort(key=lambda ev: ev.get("creation") or "", reverse=True)
        return {"status": "success", "data": events[:60]}
    except Exception as e:
        frappe.log_error("Get SBM Supplier Activity Error", str(e))
        return {"status": "error", "error": str(e)}


@frappe.whitelist()
def get_sbm_auto_assign_for_brand(brand_name):
    """
    Auto Assign rows that belong to one Brand, grouped by brand_division_glgnet.
    Used by the SBM Auto Assign tab and the per-division summary on Brand View.
    """
    try:
        from frappe.utils import cint
        from my_custom_app.auto_assign import BRAND_CERTIFY_POINTS, BRAND_CELL_BONUS

        brand_name = (brand_name or "").strip()
        if not brand_name:
            return {"status": "error", "error": "Brand is required."}

        divisions = frappe.get_all(
            "brand_division_glgnet",
            filters={"brand": brand_name},
            fields=["name", "brand", "div_name", "notes", "disabled"],
            order_by="div_name asc",
            limit_page_length=500,
        ) or []
        div_ids = [d.name for d in divisions if d.name]

        users = []
        if div_ids:
            users = frappe.get_all(
                "Brand Division User",
                filters={"division": ["in", div_ids]},
                fields=["name", "brand", "division", "user", "role", "status", "notes", "priority"],
                order_by="priority asc, user asc",
                limit_page_length=500,
            ) or []

        extra_users = frappe.get_all(
            "Brand Division User",
            filters={"brand": brand_name},
            fields=["name", "brand", "division", "user", "role", "status", "notes", "priority"],
            limit_page_length=500,
        ) or []
        seen_users = {u.name for u in users}
        for u in extra_users:
            if u.name not in seen_users:
                users.append(u)
                seen_users.add(u.name)

        rules = []
        if div_ids:
            rules = frappe.get_all(
                "Brand Division Match Condition",
                filters={"brand_division": ["in", div_ids], "disabled": 0},
                fields=["name", "brand_division", "field_to_match", "match_condition",
                        "example_value", "min_length", "priority", "disabled", "notes"],
                order_by="priority desc",
                limit_page_length=1000,
            ) or []

        users_by_div = {}
        for u in users:
            users_by_div.setdefault(u.division or "", []).append(u)

        rules_by_div = {}
        for r in rules:
            rules_by_div.setdefault(r.brand_division or "", []).append(r)

        def certify_for(div_rules):
            rules_sorted = sorted(
                [
                    {
                        "name": r.name,
                        "field_to_match": r.field_to_match,
                        "match_condition": r.match_condition,
                        "example_value": r.example_value,
                        "priority": cint(r.priority) or 0,
                    }
                    for r in (div_rules or [])
                ],
                key=lambda x: -x["priority"],
            )
            if not rules_sorted:
                return {
                    "status": "insufficient",
                    "status_label": "No rules",
                    "rule_sum": 0,
                    "max_with_brand": BRAND_CELL_BONUS,
                    "threshold": BRAND_CERTIFY_POINTS,
                    "alone": [],
                    "pairs": [],
                    "min_rules": 0,
                }

            rule_sum = sum(x["priority"] for x in rules_sorted)
            alone = [x for x in rules_sorted if x["priority"] >= BRAND_CERTIFY_POINTS]
            with_bonus = [x for x in rules_sorted if x["priority"] + BRAND_CELL_BONUS >= BRAND_CERTIFY_POINTS]

            running = 0
            min_rules = 0
            for x in rules_sorted:
                running += x["priority"]
                min_rules += 1
                if running >= BRAND_CERTIFY_POINTS:
                    break
            if running < BRAND_CERTIFY_POINTS:
                min_rules = 0

            if alone:
                status = "one_rule"
                status_label = "One rule is enough"
            elif min_rules:
                status = "combo"
                status_label = "Needs {0} matching rules".format(min_rules)
            elif with_bonus or rule_sum + BRAND_CELL_BONUS >= BRAND_CERTIFY_POINTS:
                status = "needs_brand"
                status_label = "Needs listed Brand/alias (+50) plus rule(s)"
            else:
                status = "insufficient"
                status_label = "Cannot reach 100 even with every rule + brand bonus"

            return {
                "status": status,
                "status_label": status_label,
                "rule_sum": rule_sum,
                "max_with_brand": rule_sum + BRAND_CELL_BONUS,
                "threshold": BRAND_CERTIFY_POINTS,
                "alone": [
                    "{0} {1} ({2})".format(x["match_condition"], x["example_value"], x["priority"])
                    for x in alone
                ],
                "min_rules": min_rules,
            }

        out_divs = []
        for d in divisions:
            d_rules = rules_by_div.get(d.name, [])
            out_divs.append({
                "name": d.name,
                "brand": d.brand,
                "div_name": d.div_name or d.name,
                "notes": d.notes or "",
                "disabled": cint(d.disabled),
                "users": users_by_div.get(d.name, []),
                "rules": d_rules,
                "certify": certify_for(d_rules),
                "_expanded": False,
            })

        aliases = []
        try:
            aliases = frappe.get_all(
                "Brand Alias",
                filters={"brand": brand_name, "disabled": 0},
                fields=["name", "alias", "brand", "disabled"],
                order_by="alias asc",
                limit_page_length=200,
            ) or []
        except Exception:
            aliases = []

        overrides = []
        try:
            overrides = frappe.get_all(
                "Auto Assign Override",
                filters={"brand": brand_name},
                fields=["name", "brand", "part_number", "corrected_division", "rep",
                        "change_type", "user", "modified"],
                order_by="modified desc",
                limit_page_length=100,
            ) or []
        except Exception:
            overrides = []

        return {
            "status": "success",
            "data": {
                "brand": brand_name,
                "divisions": out_divs,
                "aliases": aliases,
                "overrides": overrides,
                "threshold": BRAND_CERTIFY_POINTS,
                "brand_bonus": BRAND_CELL_BONUS,
            },
        }
    except Exception as e:
        frappe.log_error("Get SBM Auto Assign For Brand Error", str(e))
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
def get_current_user_groups():
    """Return the list of User Groups for the currently logged-in user, bypassing standard API permission limits."""
    try:
        groups = frappe.db.get_all("User Group Member", filters={"user": frappe.session.user}, pluck="parent")
        return {"status": "success", "groups": groups}
    except Exception as e:
        frappe.log_error("Get Current User Groups Error", str(e))
        return {"status": "error", "error": str(e), "groups": []}


@frappe.whitelist()
def session_ping():
    """
    Extremely lightweight keep-alive.
    Called by the Procurement/Sales Panel every few minutes so the
    Frappe session never expires while a user is filling a long form.
    """
    return {
        "status": "success",
        "user": frappe.session.user,
        "ts": frappe.utils.now_datetime().isoformat()
    }
