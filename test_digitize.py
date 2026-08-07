"""
StyleStack Wardrobe Digitizer — Interactive Test Script
Browse and select a full-body photo, then get clean cutouts of each clothing item.
"""
import base64
import requests
import os
import subprocess
import tkinter as tk
from tkinter import filedialog

# Hide the root Tk window
root = tk.Tk()
root.withdraw()

print("👔 StyleStack Wardrobe Digitizer")
print("=" * 40)
print("📂 Select a full-body photo...")

file_path = filedialog.askopenfilename(
    title="Select a full-body photo",
    filetypes=[
        ("Image files", "*.jpg *.jpeg *.png *.webp *.heic"),
        ("All files", "*.*")
    ]
)

if not file_path:
    print("❌ No file selected. Exiting.")
    exit()

print(f"✅ Selected: {file_path}")

# Encode image
with open(file_path, "rb") as f:
    person_b64 = base64.b64encode(f.read()).decode("utf-8")

url = "https://rounaknaik1904--tryon-api-digitize.modal.run"
print(f"\n🚀 Sending to cloud GPU (may take ~60-90s on cold boot)...")

response = requests.post(url, json={"person_image": person_b64}, timeout=300)

print(f"📥 Response: {response.status_code}")

if response.status_code == 200:
    data = response.json()
    if data["status"] == "success":
        items = data["items"]
        print(f"\n🎉 Detected {len(items)} clothing items!\n")

        output_dir = os.path.join(os.path.dirname(file_path), "digitized_output")
        os.makedirs(output_dir, exist_ok=True)

        saved = []
        for name, img_b64 in items.items():
            path = os.path.join(output_dir, f"{name}.png")
            with open(path, "wb") as f:
                f.write(base64.b64decode(img_b64))
            print(f"   ✅ {name}: {path}")
            saved.append(path)

        # Open all in Preview
        subprocess.run(["open"] + saved)
        print(f"\n📁 All outputs saved to: {output_dir}")
    else:
        print(f"❌ {data.get('message', 'Unknown error')}")
else:
    print(f"❌ HTTP {response.status_code}: {response.text[:300]}")
