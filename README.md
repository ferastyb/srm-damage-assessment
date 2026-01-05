# SRM Damage Assessment (Prototype)

A semi-automated **structural damage assessment tool** designed to support aircraft maintenance and structures engineers when evaluating **fuselage dents**.

This project combines:

- 🎯 **Deterministic engineering rules** (coded from SRM-style logic)
- 🧠 A framework ready for future AI assistance
- 💻 A simple Streamlit UI for day-to-day use
- 📑 Traceable outputs that explain every decision

> ⚠️ **Important:**  
> This tool is **advisory only**.  
> All assessments must be verified against the **latest SRM revision** and your organization’s approved procedures.

---

## ✈️ What this tool does (v1)

- Accepts structured inputs for a dent (size, depth, location, distances, etc.)
- Applies configurable SRM-style limits:
  - depth vs thickness  
  - dent diameter  
  - distance to frame  
  - distance to stringer
- Returns:
  - Whether the damage is **within / outside** configured limits
  - A **clear explanation** for each check
  - A ready-to-paste **engineering summary**

This version focuses on:

> **Pressurized / unpressurized fuselage dents**

The architecture is built so that **scratches, cracks, corrosion, and more aircraft types** can be added later.

---

## 📂 Project structure

