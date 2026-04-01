import streamlit as st
from fpdf import FPDF
from PIL import Image
import pandas as pd
import matplotlib.pyplot as plt
import tempfile
import os

# ------------------ SESSION STATE ------------------
if "images" not in st.session_state:
    st.session_state["images"] = [None] * 4  # 4 photos

if "metadata" not in st.session_state:
    st.session_state["metadata"] = {}

if "history" not in st.session_state:
    st.session_state["history"] = [{"date": "", "tonnage": ""} for _ in range(12)]

# ------------------ PAGE SETUP ------------------
st.set_page_config(page_title="Waste Audit Report")
st.title("Visual Waste Assessment Report Generator")

# ------------------ IMAGE UPLOAD ------------------
st.subheader("Upload Photos")
for i in range(4):
    uploaded_file = st.file_uploader(f"Photo {i+1}", type=["png", "jpg", "jpeg"], key=f"img_{i}")
    if uploaded_file:
        st.session_state["images"][i] = Image.open(uploaded_file)

# ------------------ METADATA FORM ------------------
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
            "Tonnage": tonnage
        }
        st.success("Metadata saved!")

# ------------------ HISTORICAL TONNAGE ------------------
st.subheader("Historical Tonnage (up to 12 entries)")
for i in range(12):
    date = st.text_input(f"Date {i+1}", value=st.session_state["history"][i]["date"], key=f"hist_date_{i}", placeholder="YYYY-MM-DD")
    ton = st.text_input(f"Tonnage {i+1}", value=st.session_state["history"][i]["tonnage"], key=f"hist_ton_{i}")
    st.session_state["history"][i] = {"date": date, "tonnage": ton}

# ------------------ VALIDATION ------------------
images_ready = all(img is not None for img in st.session_state["images"])
metadata_ready = bool(st.session_state["metadata"]) and all(st.session_state["metadata"].values())

# ------------------ PDF CLASS ------------------
class PDF(FPDF):
    def header(self):
        self.set_font("Arial", "B", 16)
        self.cell(0, 10, "Visual Waste Assessment Report", ln=True, align="C")
        self.ln(5)

    def add_metadata(self, metadata):
        self.set_font("Arial", "", 12)
        for k, v in metadata.items():
            self.cell(0, 8, f"{k}: {v}", ln=True)
        self.ln(5)

    def add_graph(self, path):
        self.image(path, x=10, y=60, w=80)

    def add_images(self, paths):
        x = 120
        y = 40
        w = 70
        h = 45
        pad = 7
        for p in paths:
            self.image(p, x=x, y=y, w=w, h=h)
            y += h + pad

# ------------------ GENERATE PDF ------------------
if images_ready and metadata_ready:
    if st.button("Generate PDF Report"):
        with tempfile.TemporaryDirectory() as tmp:
            # Save images
            image_paths = []
            for i, img in enumerate(st.session_state["images"]):
                path = os.path.join(tmp, f"image_{i}.png")
                img.save(path)
                image_paths.append(path)

            # Prepare historical data
            data = [(r["date"], float(r["tonnage"])) for r in st.session_state["history"] if r["date"] and r["tonnage"].replace('.', '', 1).isdigit()]
            graph_path = None
            if data:
                df = pd.DataFrame(data, columns=["Date", "Tonnage"])
                df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
                df = df.dropna()
                if not df.empty:
                    df["Month"] = df["Date"].dt.strftime("%b %Y")
                    plt.figure(figsize=(6, 2.5))
                    plt.plot(df["Month"], df["Tonnage"], marker='o')
                    plt.title("Historical Tonnage")
                    plt.xticks(rotation=45)
                    plt.tight_layout()
                    graph_path = os.path.join(tmp, "trend.png")
                    plt.savefig(graph_path)
                    plt.close()

            # Create PDF
            pdf = PDF()
            pdf.add_page()
            pdf.add_metadata(st.session_state["metadata"])
            if graph_path:
                pdf.add_graph(graph_path)
            pdf.add_images(image_paths)

            pdf_file_path = os.path.join(tmp, "Waste_Report.pdf")
            pdf.output(pdf_file_path)

            # Download button
            with open(pdf_file_path, "rb") as f:
                st.download_button("Download PDF", f.read(), file_name="Waste_Report.pdf", mime="application/pdf")
