# 📊 EFFR vs 75th Percentile Analysis

## 🧠 Overview
This project explores the relationship between the **Effective Federal Funds Rate (EFFR)** and the **75th percentile rate**, focusing on patterns, correlations, and lag effects over time.

---

## 📂 Dataset
- **Time Period:** January 2025 – February 2026  
- **Total Changes in 75th Percentile:** 54  
  - 26 downward movements  
  - 28 upward movements  

---

## 🔍 Key Insights
- No strong correlation observed during early 2025  
- A noticeable correlation emerges after the **September 2025 rate cut cycle**  
- A **lag effect (~10–15 days)** is observed, where changes in the 75th percentile precede EFFR adjustments  

---

## ⚙️ Methodology
- Data cleaning and preprocessing using **Pandas**  
- Time series visualization for trend analysis  
- Event-based analysis to track rate change triggers  

---

## 📈 Results
- Identified a **delayed response pattern** in EFFR movements  
- Highlighted periods of **strong vs weak correlation**  
- Observed that sustained changes in the 75th percentile can signal upcoming EFFR shifts  

---

## 🛠️ Tech Stack
- Python  
- Pandas  
- Matplotlib  

---

## ▶️ How to Run

```bash
# Install dependencies
pip install -r requirements.txt

# Launch Jupyter Notebook
jupyter notebook
```

---

## 📌 Future Improvements
- Add rolling correlation metrics  
- Build predictive models for EFFR movement  
- Automate lag detection using time-series techniques  

---

## 🤝 Contributions
Feel free to fork this repository, raise issues, or submit pull requests!
