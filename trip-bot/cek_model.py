import google.generativeai as genai

genai.configure(api_key="AQ.Ab8RN6KI8z5RPXFNfnXBrSpBgTlPv_9AEDGvYNt71VNebWi_eQ")

for m in genai.list_models():
    if "generateContent" in m.supported_generation_methods:
        print(m.name)