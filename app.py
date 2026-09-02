def create_lexoffice_invoice(api_key, contact_id, line_items, remark):
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }
    
    now_utc_iso = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    
    payload = {
        "archived": False,
        "voucherDate": now_utc_iso,
        "address": {"contactId": contact_id},
        "lineItems": line_items,
        "totalPrice": {"currency": "EUR"},
        "taxConditions": {"taxType": "net"},
        "shippingConditions": {
            "shippingDate": now_utc_iso,
            "shippingEndDate": now_utc_iso,
            "shippingType": "serviceperiod"
        },
        "remark": remark
    }
    res = requests.post("https://api.lexoffice.io/v1/invoices?finalize=false", headers=headers, json=payload)
    if res.status_code in [200, 201]:
        return res.json()['id']
    else:
        st.error(f"Fehler beim Erstellen der Rechnung in Lexoffice: {res.text}")
        return None
