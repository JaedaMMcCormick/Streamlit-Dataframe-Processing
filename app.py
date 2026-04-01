import streamlit as st
from fpdf import FPDF
import tempfile
import os
import json
import base64
from PIL import Image, ImageDraw
import pandas as pd
import matplotlib.pyplot as plt
from openai import OpenAI

# ------------------ OPENAI CLIENT ------------------
# --- OpenAI API Key setup ---
# 1. Try Streamlit secrets first (for Streamlit Cloud)
# 2. Fallback to environment variable (for local testing)
OPENAI_API_KEY = st.secrets.get("OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY")

if not OPENAI_API_KEY:
    st.error("OpenAI API key is not set! Please add it to Streamlit secrets or your environment variables.")
    st.stop()  # Stop the app if no key

# Create the OpenAI client
client = OpenAI(api_key=OPENAI_API_KEY)

# ------------------ CONSTANTS ------------------
STENCIL_PATH = "assets/compactor_stencil.png"

FILL_COLORS = {
    "Trash": (120, 120, 120, 180),
    "Recycling": (0, 120, 255, 180),
    "Cardboard": (165, 100, 42, 180),
}

# ------------------ SESSION STATE ------------------
if "images" not in st.session_state:
    st.session_state["images"] = [None] * 4

if "metadata" not in st.session_state:
    st.session_state["metadata"] = {}

if "history" not in st.session_state:
    st.session_state["history"] = [{"date": "", "tonnage": ""} for _ in range(12)]

if "ai_analysis" not in st.session_state:
    st.session_state["ai_analysis"] = None

# ------------------ HELPERS ------------------
def pil_to_base64(img: Image.Image) -> str:
    buffer = tempfile.SpooledTemporaryFile()
    img.save(buffer, format="PNG")
    buffer.seek(0)
    return base64.b64encode(buffer.read()).decode()

def analyze_waste_images(images):
    """
    Uses OpenAI Vision to estimate divertible materials.
    """

    image_payload = []
    for img in images:
        image_payload.append({
            "type": "image_url",
            "image_url": {
                "url": f"data:image/png;base64,{pil_to_base64(img)}"
            }
        })

    prompt = """
You are a professional waste audit expert.

Analyze the provided photos of trash and waste.
Estimate the percentage of divertible materials
(recyclables, cardboard, metals, paper).

Respond ONLY in valid JSON using this schema:
{
  "divertible_percentage": number,
  "materials_detected": {
    "cardboard": number,
    "plastic": number,
    "metal": number,
    "paper": number
  },
  "confidence": number
}
"""

    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    *image_payload
                ]
            }
        ],
        temperature=0
    )

    return json.loads(response.choices[0].message.content)

def generate_compactor_fill(stencil_img, fill_pct, item_type):
    stencil = stencil_img.convert("RGBA")
    w, h = stencil.size

    overlay = Image.new("RGBA", stencil.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    fill_left = int(w * 0.25)
    fill_right = int(w * 0.75)
    fill_bottom = int(h * 0.85)
    fill_top_max = int(h * 0.15)

    fill_height = int((fill_bottom - fill_top_max) * (fill_pct / 100))
    fill_top = fill_bottom - fill_height

    draw.rectangle(
        [fill_left, fill_top, fill_right, fill_bottom],
        fill=FILL_COLORS[item_type],
    )

    return Image.alpha_composite(overlay, stencil)

# ------------------ UI ------------------
st.set_page_config(page_title="Waste Audit Report")
st.title("Visual Waste Assessment Report")

# ------------------ IMAGE UPLOAD ------------------
st.subheader("Upload Photos (4)")
for i in range(4):
    uploaded = st.file_uploader(
        f"Photo {i + 1}", type=["png", "jpg", "jpeg"], key=f"file_{i}"
    )
    if uploaded:
        st.session_state["images"][i] = Image.open(uploaded)

images_ready = all(img is not None for img in st.session_state["images"])

# ------------------ AI ANALYSIS ------------------
if images_ready:
    if st.button("Analyze Photos with AI"):
        with st.spinner("Analyzing waste composition..."):
            st.session_state["ai_analysis"] = analyze_waste_images(
                st.session_state["images"]
            )

if st.session_state["ai_analysis"]:
    ai = st.session_state["ai_analysis"]
    st.metric(
        "Estimated Divertible Materials",
        f"{ai['divertible_percentage']}%"
    )
    st.progress(ai["divertible_percentage"] / 100)
    st.caption(f"AI confidence: {int(ai['confidence'] * 100)}%")

# ------------------ COMPACTOR VISUAL ------------------
st.subheader("Compactor Visualization")

item_type = st.selectbox(
    "Material Type",
    ["Trash", "Recycling", "Cardboard"]
)

fill_percent = (
    st.session_state["ai_analysis"]["divertible_percentage"]
    if st.session_state["ai_analysis"]
    else 50
)

stencil_img = Image.open(STENCIL_PATH)
compactor_preview = generate_compactor_fill(
    stencil_img,
    fill_percent,
    item_type
)

st.image(compactor_preview, use_container_width=True)

# ------------------ METADATA ------------------
st.subheader("Assessment Metadata")
with st.form("metadata_form"):
    location = st.text_input("Location")
    service_type = st.text_input("Service Type")
    audit_date = st.date_input("Audit Date")
    tonnage = st.text_input("Current Tonnage")
    submitted = st.form_submit_button("Save Metadata")

    if submitted:
        st.session_state["metadata"] = {
            "Location": location,
            "Service Type": service_type,
            "Audit Date": str(audit_date),
            "Tonnage": tonnage,
            "AI Divertible %": f"{fill_percent}%"
        }
        st.success("Metadata saved")

# ------------------ PDF CLASS ------------------
class PDF(FPDF):
    def header(self):
        self.set_font("Helvetica", "B", 16)
        self.cell(0, 10, "Visual Waste Assessment Report", ln=True, align="C")
        self.ln(5)

    def add_metadata(self, metadata):
        self.set_font("Helvetica", size=11)
        for k, v in metadata.items():
            self.cell(0, 8, f"{k}: {v}", ln=True)

# ------------------ GENERATE PDF ------------------
if images_ready and st.session_state["metadata"]:
    if st.button("Generate PDF Report"):
        with tempfile.TemporaryDirectory() as tmp:

            compactor_path = os.path.join(tmp, "compactor.png")
            compactor_preview.save(compactor_path)

            pdf = PDF()
            pdf.add_page()
            pdf.add_metadata(st.session_state["metadata"])
            pdf.image(compactor_path, x=10, y=80, w=80)

            pdf_path = os.path.join(tmp, "Waste_Report.pdf")
            pdf.output(pdf_path)

            with open(pdf_path, "rb") as f:
                st.download_button(
                    "Download PDF Report",
                    f.read(),
                    file_name="Waste_Report.pdf",
                    mime="application/pdf",
                )
