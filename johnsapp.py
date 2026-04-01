#Johns App



"""
MLS (FlexMLS/Spark RESO) -> AirDNA STR Estimator
One-file Streamlit app with per-run filters and Excel export.

HOW IT WORKS (once you add keys):
1) Query MLS (Spark RESO Web API / OData) for active listings <= max price in a radius.
2) For each listing, query AirDNA for comps/valuation within comp radius.
3) Filter comps by beds/baths/sqft tolerances.
4) Estimate ADR, occupancy, monthly revenue (ADR * Occ * 30.4).
5) Export to Excel with two sheets: estimates + comps.

NOTE:
- This file is runnable now, but MLS/AirDNA functions are STUBS until you add keys + endpoint details.
- You should NOT scrape the MLS UI login. Use approved Spark/RESO Web API access.
"""

import os
import math
import time
import json
import hashlib
import asyncio
from dataclasses import dataclass, asdict
from typing import Optional, List, Dict, Any, Tuple

import pandas as pd
import streamlit as st
import httpx # type: ignore

# ----------------------------
# Data Models
# ----------------------------
@dataclass
class Listing:
    mls_id: str
    address: str
    city: str
    state: str
    zip: str
    price: int
    beds: Optional[int]
    baths: Optional[float]
    sqft: Optional[int]
    lat: float
    lng: float
    url: Optional[str] = None

@dataclass
class StrComp:
    comp_id: str
    lat: float
    lng: float
    beds: Optional[int]
    baths: Optional[float]
    sqft: Optional[int]
    adr: Optional[float]
    occupancy: Optional[float]


# ----------------------------
# Utilities
# ----------------------------
def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 3958.7613  # miles
    p = math.pi / 180.0
    dlat = (lat2 - lat1) * p
    dlon = (lon2 - lon1) * p
    a = (math.sin(dlat/2)**2 +
         math.cos(lat1*p)*math.cos(lat2*p)*math.sin(dlon/2)**2)
    return 2 * R * math.asin(math.sqrt(a))

def bounding_box(center_lat: float, center_lng: float, radius_miles: float) -> Tuple[float, float, float, float]:
    """
    Returns (min_lat, max_lat, min_lng, max_lng) as an approximation.
    Used when RESO circle geo filters aren't available.
    """
    lat_delta = radius_miles / 69.0
    lng_delta = radius_miles / (69.0 * math.cos(math.radians(center_lat)) + 1e-9)
    return (center_lat - lat_delta, center_lat + lat_delta, center_lng - lng_delta, center_lng + lng_delta)

def safe_int(x) -> Optional[int]:
    try:
        if x is None:
            return None
        return int(float(x))
    except Exception:
        return None

def safe_float(x) -> Optional[float]:
    try:
        if x is None:
            return None
        return float(x)
    except Exception:
        return None

# ----------------------------
# Tiny disk cache (for AirDNA calls)
# ----------------------------
class SimpleDiskCache:
    def __init__(self, folder: str = ".cache"):
        self.folder = folder
        os.makedirs(self.folder, exist_ok=True)

    def _path(self, key: str) -> str:
        h = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return os.path.join(self.folder, f"{h}.json")

    def get(self, key: str) -> Optional[Any]:
        p = self._path(key)
        if not os.path.exists(p):
            return None
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)

    def set(self, key: str, val: Any) -> None:
        p = self._path(key)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(val, f)

# ----------------------------
# Async QPS limiter
# ----------------------------
class AsyncQpsLimiter:
    def __init__(self, qps: float):
        self.qps = max(0.1, float(qps))
        self.min_interval = 1.0 / self.qps
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def wait(self):
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last
            sleep_for = self.min_interval - elapsed
            if sleep_for > 0:
                await asyncio.sleep(sleep_for)
            self._last = time.monotonic()


# ----------------------------
# Matching + Estimation
# ----------------------------
def comp_match_ok(listing: Listing, comp: StrComp, beds_tol: int, baths_tol: float, sqft_tol_pct: float) -> bool:
    if listing.beds is not None and comp.beds is not None:
        if abs(comp.beds - listing.beds) > beds_tol:
            return False
    if listing.baths is not None and comp.baths is not None:
        if abs(comp.baths - listing.baths) > baths_tol:
            return False
    if listing.sqft is not None and comp.sqft is not None and listing.sqft > 0:
        if abs(comp.sqft - listing.sqft) / listing.sqft > sqft_tol_pct:
            return False
    return True

def estimate_from_comps(listing: Listing, comps: List[StrComp]) -> Dict[str, Any]:
    comps = [c for c in comps if c.adr is not None and c.occupancy is not None]
    if not comps:
        return dict(comp_count=0, adr_est=None, occ_est=None, monthly_rev_est=None, confidence=0.0)

    weights, adrs, occs = [], [], []
    for c in comps:
        d = haversine_miles(listing.lat, listing.lng, c.lat, c.lng)
        w_dist = 1.0 / (d + 0.25)  # closer = higher
        w_feat = 1.0
        if listing.beds is not None and c.beds is not None and c.beds == listing.beds:
            w_feat *= 1.25
        if listing.baths is not None and c.baths is not None and abs(c.baths - listing.baths) <= 0.5:
            w_feat *= 1.10
        w = w_dist * w_feat
        weights.append(w)
        adrs.append(c.adr)
        occs.append(c.occupancy)

    wsum = sum(weights)
    adr_est = sum(w*v for w, v in zip(weights, adrs)) / wsum
    occ_est = sum(w*v for w, v in zip(weights, occs)) / wsum
    monthly_rev_est = adr_est * occ_est * 30.4

    confidence = min(1.0, len(comps) / 15.0)
    return dict(
        comp_count=len(comps),
        adr_est=round(adr_est, 2),
        occ_est=round(occ_est, 3),
        monthly_rev_est=round(monthly_rev_est, 2),
        confidence=round(confidence, 2),
    )

# ----------------------------
# TODO #1: MLS via Spark/RESO Web API (FlexMLS)
# ----------------------------
def spark_search_listings(
    *,
    base_url: str,
    bearer_token: str,
    center_lat: float,
    center_lng: float,
    radius_miles: float,
    max_price: int,
    max_results: int,
) -> List[Listing]:
    """
    STUB IMPLEMENTATION.
    Replace this with Spark/RESO OData calls once you have credentials + field names.

    Typical approach:
      - Build bounding box filter on Latitude/Longitude fields, plus StandardStatus='Active' and ListPrice <= max_price.
      - Use $select to minimize payload.
      - Page using @odata.nextLink.

    Returns empty list until you implement.
    """
    # If you accidentally click Run without keys, keep app stable.
    if not bearer_token or not base_url:
        return []

    # ---- Example scaffold (NOT guaranteed field names) ----
    # Uncomment and adapt once you confirm your MLS RESO field names in metadata.
    #
    # min_lat, max_lat, min_lng, max_lng = bounding_box(center_lat, center_lng, radius_miles)
    # url = f"{base_url.rstrip('/')}/Property"
    # params = {
    #   "$top": 200,
    #   "$select": "ListingId,ListPrice,BedroomsTotal,BathroomsTotalInteger,LivingArea,Latitude,Longitude,UnparsedAddress,City,StateOrProvince,PostalCode",
    #   "$filter": f"StandardStatus eq 'Active' and ListPrice le {max_price} and Latitude ge {min_lat} and Latitude le {max_lat} and Longitude ge {min_lng} and Longitude le {max_lng}"
    # }
    # headers = {"Authorization": f"Bearer {bearer_token}"}
    # listings = []
    # with httpx.Client(timeout=30, headers=headers) as client:
    #   next_url = url
    #   next_params = params
    #   while next_url and len(listings) < max_results:
    #       r = client.get(next_url, params=next_params)
    #       r.raise_for_status()
    #       data = r.json()
    #       for row in data.get("value", []):
    #           listings.append(map_row_to_listing(row))
    #       next_url = data.get("@odata.nextLink")
    #       next_params = None  # nextLink already has params
    #
    # return listings[:max_results]

    return []


# ----------------------------
# TODO #2: AirDNA comps/valuation
# ----------------------------
async def airdna_get_comps(
    *,
    client: httpx.AsyncClient,
    limiter: AsyncQpsLimiter,
    cache: SimpleDiskCache,
    base_url: str,
    api_key: str,
    listing: Listing,
    comp_radius_miles: float,
) -> List[StrComp]:
    """
    STUB IMPLEMENTATION.
    Replace with AirDNA endpoint(s) available in your contract.
    Must return comps with adr + occupancy + beds/baths/sqft if available.

    Uses caching to reduce repeat calls.
    """
    if not api_key or not base_url:
        return []

    key = f"airdna:comps:{round(listing.lat,5)}:{round(listing.lng,5)}:{comp_radius_miles}:{listing.beds}:{listing.baths}:{listing.sqft}"
    cached = cache.get(key)
    if cached is not None:
        return [StrComp(**c) for c in cached]

    # ---- Example scaffold (endpoint will vary by contract) ----
    # await limiter.wait()
    # url = f"{base_url.rstrip('/')}/<YOUR_ENDPOINT_PATH>"
    # params = {
    #   "lat": listing.lat,
    #   "lng": listing.lng,
    #   "radius_miles": comp_radius_miles,
    #   # other fields as required...
    # }
    # r = await client.get(url, params=params)
    # r.raise_for_status()
    # data = r.json()
    # comps = []
    # for row in data["..."]:
    #   comps.append(StrComp(
    #       comp_id=str(row["id"]),
    #       lat=float(row["lat"]),
    #       lng=float(row["lng"]),
    #       beds=safe_int(row.get("bedrooms")),
    #       baths=safe_float(row.get("bathrooms")),
    #       sqft=safe_int(row.get("sqft")),
    #       adr=safe_float(row.get("adr")),
    #       occupancy=safe_float(row.get("occupancy")),
    #   ))

    comps: List[StrComp] = []
    cache.set(key, [c.__dict__ for c in comps])
    return comps


# ----------------------------
# Pipeline runner
# ----------------------------
async def run_pipeline(
    *,
    spark_base_url: str,
    spark_token: str,
    airdna_base_url: str,
    airdna_key: str,
    market_label: str,
    center_lat: float,
    center_lng: float,
    search_radius_miles: float,
    max_price: int,
    max_listings: int,
    comp_radius_miles: float,
    beds_tol: int,
    baths_tol: float,
    sqft_tol_pct: float,
    airdna_concurrency: int,
    airdna_qps: int,
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    # 1) MLS listings
    listings = spark_search_listings(
        base_url=spark_base_url,
        bearer_token=spark_token,
        center_lat=center_lat,
        center_lng=center_lng,
        radius_miles=search_radius_miles,
        max_price=max_price,
        max_results=max_listings,
    )

    # Post-filter to true circle radius (even if MLS query uses bbox later)
    listings = [
        l for l in listings
        if haversine_miles(center_lat, center_lng, l.lat, l.lng) <= search_radius_miles
    ][:max_listings]

    # 2) AirDNA comps for each listing (async)
    sem = asyncio.Semaphore(airdna_concurrency)
    limiter = AsyncQpsLimiter(qps=float(airdna_qps))
    cache = SimpleDiskCache(folder=".cache")

    estimate_rows: List[Dict[str, Any]] = []
    comps_rows: List[Dict[str, Any]] = []

    headers = {"Authorization": f"Bearer {airdna_key}"}
    async with httpx.AsyncClient(timeout=30, headers=headers) as client:

        async def process_one(l: Listing):
            async with sem:
                comps = await airdna_get_comps(
                    client=client,
                    limiter=limiter,
                    cache=cache,
                    base_url=airdna_base_url,
                    api_key=airdna_key,
                    listing=l,
                    comp_radius_miles=comp_radius_miles,
                )
                # ensure comp radius
                comps = [c for c in comps if haversine_miles(l.lat, l.lng, c.lat, c.lng) <= comp_radius_miles]
                # filter similarity
                comps = [c for c in comps if comp_match_ok(l, c, beds_tol, baths_tol, sqft_tol_pct)]
                est = estimate_from_comps(l, comps)

                estimate_rows.append({**asdict(l), **est, "market": market_label})

                for c in comps:
                    comps_rows.append({
                        "market": market_label,
                        "mls_id": l.mls_id,
                        "address": l.address,
                        "comp_id": c.comp_id,
                        "distance_mi": round(haversine_miles(l.lat, l.lng, c.lat, c.lng), 2),
                        "beds": c.beds,
                        "baths": c.baths,
                        "sqft": c.sqft,
                        "adr": c.adr,
                        "occupancy": c.occupancy,
                    })

        await asyncio.gather(*(process_one(l) for l in listings))

    est_df = pd.DataFrame(estimate_rows)
    comps_df = pd.DataFrame(comps_rows)
    return est_df, comps_df


def export_excel(est_df: pd.DataFrame, comps_df: pd.DataFrame, out_path: str) -> str:
    preferred = [
        "market","mls_id","address","city","state","zip","price","beds","baths","sqft","lat","lng","url",
        "comp_count","adr_est","occ_est","monthly_rev_est","confidence",
    ]
    if not est_df.empty:
        cols = [c for c in preferred if c in est_df.columns] + [c for c in est_df.columns if c not in preferred]
        est_df = est_df[cols]

    with pd.ExcelWriter(out_path, engine="openpyxl") as writer:
        est_df.to_excel(writer, sheet_name="estimates", index=False)
        comps_df.to_excel(writer, sheet_name="comps", index=False)
    return out_path


# ----------------------------
# Streamlit UI
# ----------------------------
st.set_page_config(page_title="MLS ? AirDNA STR Estimator", layout="wide")
st.title("MLS (FlexMLS/Spark) ? AirDNA STR Rent Estimator (Excel)")

with st.sidebar:
    st.header("Per-run filters")

    market_label = st.text_input("Market label", value="Phoenix / Scottsdale, AZ")
    center_lat = st.number_input("Center latitude", value=33.4942, format="%.6f")
    center_lng = st.number_input("Center longitude", value=-111.9261, format="%.6f")
    search_radius_miles = st.slider("MLS search radius (miles)", 1, 50, 10)
    max_price = st.number_input("Max list price ($)", value=900000, step=5000)
    max_listings = st.slider("Max listings to process", 10, 5000, 500)

    st.divider()
    comp_radius_miles = st.slider("AirDNA comp radius (miles)", 1, 10, 2)
    beds_tol = st.slider("Beds tolerance", 0, 3, 1)
    baths_tol = st.slider("Baths tolerance", 0.0, 3.0, 1.0, step=0.5)
    sqft_tol_pct = st.slider("Sqft tolerance (%)", 5, 50, 20) / 100.0

    st.divider()
    out_name = st.text_input("Output Excel filename", value="str_estimates.xlsx")

    st.subheader("Keys (paste later)")
    spark_base_url = st.text_input("Spark/RESO base URL", value=os.getenv("SPARK_RESO_BASE_URL", ""))
    spark_token = st.text_input("Spark bearer token", value=os.getenv("SPARK_BEARER_TOKEN", ""), type="password")
    airdna_base_url = st.text_input("AirDNA base URL", value=os.getenv("AIRDNA_BASE_URL", "https://api.airdna.co"))
    airdna_key = st.text_input("AirDNA API key", value=os.getenv("AIRDNA_API_KEY", ""), type="password")

    st.divider()
    airdna_concurrency = st.slider("AirDNA concurrency", 1, 30, int(os.getenv("AIRDNA_MAX_CONCURRENCY", "8")))
    airdna_qps = st.slider("AirDNA QPS limit", 1, 20, int(os.getenv("AIRDNA_QPS", "4")))

run_btn = st.button("Run ? Export Excel", type="primary")

if run_btn:
    if not spark_token or not airdna_key or not spark_base_url:
        st.warning("App UI is ready, but MLS + AirDNA calls are stubs until you paste keys and implement the two TODO functions.")
        st.stop()

    with st.status("Running pipeline...", expanded=True):
        est_df, comps_df = asyncio.run(run_pipeline(
            spark_base_url=spark_base_url,
            spark_token=spark_token,
            airdna_base_url=airdna_base_url,
            airdna_key=airdna_key,
            market_label=market_label,
            center_lat=float(center_lat),
            center_lng=float(center_lng),
            search_radius_miles=float(search_radius_miles),
            max_price=int(max_price),
            max_listings=int(max_listings),
            comp_radius_miles=float(comp_radius_miles),
            beds_tol=int(beds_tol),
            baths_tol=float(baths_tol),
            sqft_tol_pct=float(sqft_tol_pct),
            airdna_concurrency=int(airdna_concurrency),
            airdna_qps=int(airdna_qps),
        ))

        out_path = export_excel(est_df, comps_df, out_name)
        st.success(f"Exported: {out_path}")

    st.subheader("Estimates preview")
    st.dataframe(est_df.head(200), use_container_width=True)

    st.subheader("Comps preview")
    st.dataframe(comps_df.head(200), use_container_width=True)

    try:
        with open(out_name, "rb") as f:
            st.download_button("Download Excel", data=f, file_name=out_name)
    except Exception as e:
        st.info(f"Excel created at {out_name}, but download failed: {e}")
