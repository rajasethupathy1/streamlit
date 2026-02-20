import streamlit as st
import pandas as pd
import json
import re
import time
import requests
from io import BytesIO
from openai import OpenAI

# ========================= CONFIG =========================
st.set_page_config(page_title="BOM AI + DigiKey", layout="wide")

client = OpenAI(api_key='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')


# DigiKey Config
DIGIKEY_CLIENT_ID = "G5DqcrJ1M0pudM1lliawJMGr5kxNBx8LlsdVaPA39NY1jVPT"
DIGIKEY_CLIENT_SECRET = "kCPAZPSgGmmWp3iA5zhAVFnAloVmJpREijG5cm1wEy3DTbGAWk87Rf4xU3dKLtNT"
USE_SANDBOX = True  # ← Change to False for production






USE_SANDBOX           = False  # ← Set to False for real data (production keys required)

BASE_URL   = "https://sandbox-api.digikey.com" if USE_SANDBOX else "https://api.digikey.com"
AUTH_URL   = f"{BASE_URL}/v1/oauth2/token"
KEYWORD_SEARCH_URL = f"{BASE_URL}/products/v4/search/keyword"
EXACT_PRODUCT_URL  = f"{BASE_URL}/products/v4/products"  # exact MPN lookup

# Supplier fallback
SUPPLIER_MAPPING = {
    "hantek":  ["Robu.in", "Amazon India", "Evelta", "kitsguru", "DigiKey"],
    "owon":    ["Amazon India", "Robokits", "DigiKey", "Mouser"],
    "soldron": ["Evelta", "Amazon India", "Robu.in"],
    "default": ["DigiKey", "Mouser", "Element14", "Amazon India", "Robu.in", "Evelta"]
}

# =============================================================================
# PROMPT
# =============================================================================

SYSTEM_PROMPT = """
You are an expert in electronics test equipment, rework tools, and BOM normalization.

You receive the full raw text of one BOM row (values separated by | ).

Tasks:
1. Extract or infer the real Manufacturer Part Number / Model Number (MPN)
   - Short alphanumeric code (4–20 chars), e.g. DSO4104C, SPE6103, 858D, 120-8552-000
   - Never use the full product description as MPN
2. Identify the manufacturer (brand)
3. Create a short clean description
4. Use domain knowledge if data is ambiguous

Return ONLY valid JSON:

{
  "results": [
    {
      "clean_mpn": "MPN or empty string",
      "manufacturer": "brand or empty",
      "clean_description": "short description",
      "valid_mpn": true/false,
      "confidence": 0.xx,
      "reasoning": "brief explanation"
    }
  ]
}
"""

# =============================================================================
# HELPERS
# =============================================================================

def clean_text(v):
    if pd.isna(v): return ""
    return str(v).strip()

def build_row_text(row):
    parts = [clean_text(v) for v in row.values if clean_text(v)]
    return " | ".join(parts)

@st.cache_data(ttl=3500)
def get_digikey_token():
    if not all([DIGIKEY_CLIENT_ID, DIGIKEY_CLIENT_SECRET]):
        st.warning("DigiKey credentials missing → pricing/manufacturer skipped")
        return None
    try:
        r = requests.post(AUTH_URL, data={
            "client_id": DIGIKEY_CLIENT_ID,
            "client_secret": DIGIKEY_CLIENT_SECRET,
            "grant_type": "client_credentials"
        }, headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=10)
        r.raise_for_status()
        return r.json()["access_token"]
    except Exception as e:
        st.error(f"DigiKey auth failed: {str(e)}")
        return None

def digikey_lookup(mpn: str, token: str):
    if not token or not mpn.strip():
        return {}

    headers = {
        "Authorization": f"Bearer {token}",
        "X-DIGIKEY-Client-Id": DIGIKEY_CLIENT_ID,
        "X-DIGIKEY-Locale-Site": "US",
        "X-DIGIKEY-Locale-Language": "en",
        "X-DIGIKEY-Locale-Currency": "USD"
    }

    # Step 1: Exact part lookup (highest success rate for your MPNs)
    exact_url = f"{EXACT_PRODUCT_URL}/{mpn.strip()}"
    try:
        r = requests.get(exact_url, headers=headers, timeout=12)
        if r.status_code == 200:
            product = r.json()
            price = None
            if product.get("StandardPricing"):
                price = product["StandardPricing"][0].get("UnitPrice", None)
            return {
                "digi_manufacturer": product.get("Manufacturer", {}).get("Name", ""),
                "digi_description": product.get("Description", ""),
                "digi_stock": product.get("QuantityAvailable", 0),
                "digi_price_1": price,
                "digi_match": True,
                "source": "exact"
            }
        elif r.status_code == 404:
            st.info(f"Exact MPN '{mpn}' not found – trying keyword fallback")
        else:
            st.warning(f"Exact lookup failed for {mpn}: {r.status_code}")
    except Exception as e:
        st.warning(f"Exact lookup error for {mpn}: {str(e)}")

    # Step 2: Keyword fallback
    try:
        r = requests.get(
            KEYWORD_SEARCH_URL,
            params={"keywords": mpn.strip(), "recordCount": 3},
            headers=headers,
            timeout=12
        )
        r.raise_for_status()
        products = r.json().get("Products", [])
        if not products:
            return {}
        best = products[0]
        price = None
        if best.get("ProductPrice"):
            price = best["ProductPrice"][0].get("UnitPrice", None)
        return {
            "digi_manufacturer": best.get("Manufacturer",{}).get("Name",""),
            "digi_description": best.get("Description",""),
            "digi_stock": best.get("QuantityAvailable",0),
            "digi_price_1": price,
            "digi_match": True,
            "source": "keyword"
        }
    except Exception as e:
        st.warning(f"DigiKey keyword lookup failed for {mpn}: {str(e)}")
        return {}

def enrich_manufacturer_and_suppliers(row):
    manuf = str(row.get("ai_manufacturer", "")).strip()
    desc = str(row.get("ai_clean_description", "")).lower()
    mpn  = str(row.get("ai_clean_mpn", "")).strip().upper()

    conf_raw = row.get("ai_confidence")
    confidence = 0.0
    if pd.notna(conf_raw):
        try:
            confidence = float(conf_raw)
        except:
            pass

    if not manuf or confidence < 0.5:
        if "oscilloscope" in desc or "dso" in mpn:
            manuf = "Hantek"
        elif "power supply" in desc or "spe" in mpn or "pps" in mpn:
            manuf = "OWON"
        elif "hot air" in desc or "858" in mpn:
            manuf = "Soldron"
        elif "probe" in desc or "voltage" in desc:
            manuf = "Generic / Hantek"
        else:
            manuf = "—"

    key = "default"
    if "hantek" in manuf.lower(): key = "hantek"
    elif "owon" in manuf.lower(): key = "owon"
    elif "soldron" in manuf.lower(): key = "soldron"

    suppliers = SUPPLIER_MAPPING.get(key, SUPPLIER_MAPPING["default"])
    return manuf, ", ".join(suppliers[:6])

# =============================================================================
# MAIN PROCESSING
# =============================================================================

def process_bom(df):
    records = []
    for idx, row in df.iterrows():
        text = build_row_text(row)
        records.append({
            "row_index": int(idx),
            "row_text": text[:2200]
        })

    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0.0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": "\n\n".join(
                    [f"Row {r['row_index']}:\n{r['row_text']}" for r in records]
                )}
            ]
        )
        parsed = json.loads(resp.choices[0].message.content)
        results = parsed.get("results", [])

        ai_df = pd.DataFrame(results)
        if "row_index" not in ai_df.columns:
            ai_df["row_index"] = range(len(ai_df))

        ai_df = ai_df.set_index("row_index").reindex(range(len(df))).reset_index(drop=True)
        ai_df = ai_df.add_prefix("ai_")
        return ai_df

    except Exception as e:
        st.error(f"AI processing failed: {str(e)}")
        empty = pd.DataFrame(index=range(len(df)))
        for c in ["clean_mpn", "manufacturer", "clean_description", "reasoning"]:
            empty[f"ai_{c}"] = ""
        empty["ai_valid_mpn"] = False
        empty["ai_confidence"] = 0.0
        return empty

# =============================================================================
# STREAMLIT APP
# =============================================================================

st.title("BOM Analyzer – Real MPN + DigiKey Manufacturer & Pricing")
st.caption("AI reads full row → extracts MPN → DigiKey returns real manufacturer + price")

uploaded_file = st.file_uploader("Upload BOM Excel", type=["xlsx"])

if uploaded_file:
    df = pd.read_excel(uploaded_file)
    df.columns = df.columns.str.strip().str.lower()

    st.success(f"Loaded {len(df)} rows")
    st.dataframe(df.head(10), width="stretch")

    if st.button("🚀 Process BOM + DigiKey Lookup", type="primary"):
        with st.spinner("AI extracting real MPNs..."):
            ai_df = process_bom(df)

        final_df = pd.concat([df.reset_index(drop=True), ai_df.reset_index(drop=True)], axis=1)

        # Force string conversion – prevents ArrowTypeError
        final_df = final_df.astype(str).replace(['nan', 'None', 'NaN'], '')

        # Enrich fallback manufacturer & suppliers
        manuf_list = []
        supp_list = []
        for _, row in final_df.iterrows():
            m, s = enrich_manufacturer_and_suppliers(row)
            manuf_list.append(m)
            supp_list.append(s)

        final_df["Manufacturer (fallback)"] = manuf_list
        final_df["Possible Suppliers"] = supp_list

        # Real-time DigiKey lookup (exact first, keyword fallback)
        token = get_digikey_token()
        if token:
            with st.spinner("Fetching real-time DigiKey manufacturer, stock & pricing..."):
                for i in range(len(final_df)):
                    mpn = str(final_df.at[i, "ai_clean_mpn"]).strip()
                    if len(mpn) >= 4:
                        enrich = digikey_lookup(mpn, token)
                        if enrich.get("digi_match"):
                            final_df.at[i, "DigiKey Manufacturer"] = enrich.get("digi_manufacturer", "—")
                            final_df.at[i, "DigiKey Stock"] = enrich.get("digi_stock", "—")
                            final_df.at[i, "DigiKey Price (1+ USD)"] = enrich.get("digi_price_1", None)

        # Summary table
        summary = pd.DataFrame({
            "Product Name": final_df.get("part", "—"),
            "Manufacturer (AI + fallback)": final_df["Manufacturer (fallback)"],
            "MPN / Part #": final_df["ai_clean_mpn"].replace({"": "—"}),
            "Clean Description": final_df["ai_clean_description"].replace({"": "—"}),
            "DigiKey Manufacturer": final_df.get("DigiKey Manufacturer", "—"),
            "DigiKey Stock": final_df.get("DigiKey Stock", "—"),
            "DigiKey Price (1+ USD)": final_df.get("DigiKey Price (1+ USD)", pd.NA),
            "Possible Suppliers": final_df["Possible Suppliers"],
            "Confidence": pd.to_numeric(final_df["ai_confidence"], errors="coerce").round(2)
        })

        # Format price column safely
        summary["DigiKey Price (1+ USD)"] = pd.to_numeric(
            summary["DigiKey Price (1+ USD)"], errors='coerce'
        ).apply(
            lambda x: f"${x:.2f}" if pd.notna(x) and x > 0 else "—"
        )

        # Final safety
        summary = summary.astype(str).replace(['nan', 'None', 'NaN'], '')

        st.subheader("Final BOM with DigiKey Manufacturer & Pricing")
        st.dataframe(summary, width="stretch", hide_index=True)

        # Downloads
        col1, col2 = st.columns(2)

        with col1:
            buf = BytesIO()
            with pd.ExcelWriter(buf, engine="openpyxl") as w:
                summary.to_excel(w, index=False, sheet_name="Summary")
            buf.seek(0)
            st.download_button("⬇ Download Summary", buf, "bom_summary.xlsx")

        with col2:
            buf_full = BytesIO()
            with pd.ExcelWriter(buf_full, engine="openpyxl") as w:
                final_df.to_excel(w, index=False, sheet_name="Full")
            buf_full.seek(0)
            st.download_button("⬇ Download Full Data", buf_full, "full_bom.xlsx")

        st.success("Completed successfully!")