##LinkedIN

import streamlit as st
import pandas as pd
import matplotlib.pyplot as plt


st.title("Data Processing")

upload_file = st.file_uploader("Chose a CSV File to Upload", type = 'csv')
if upload_file is not None:
    df = pd.read_csv(upload_file)
    st.subheader('Data Preview:')
    st.write(df.head())

    st.subheader('Data Summary:')
    st.write(df.describe())
