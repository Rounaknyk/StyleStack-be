import tkinter as tk
from tkinter import filedialog
import base64
import requests
import json
import os
import platform
import subprocess

def select_file(title):
    root = tk.Tk()
    root.withdraw() # Hide the main tkinter window
    file_path = filedialog.askopenfilename(
        title=title,
        filetypes=[("Image files", "*.jpg *.jpeg *.png")]
    )
    return file_path

def image_to_base64(filepath):
    with open(filepath, "rb") as image_file:
        return base64.b64encode(image_file.read()).decode('utf-8')

def main():
    print("👗 Welcome to the StyleStack Virtual Try-On Tester!\n")
    
    # Hardcode images for testing
    person_path = "/Users/rounaknaik/Downloads/input image.jpg"
    garment_path = "/Users/rounaknaik/Downloads/shirt.png"
    
    print(f"✅ Selected person: {person_path}")
    print(f"✅ Selected garment: {garment_path}")

    # 3. Encode to Base64
    print("\nEncoding images...")
    person_b64 = image_to_base64(person_path)
    garment_b64 = image_to_base64(garment_path)

    # The URL of your Modal endpoint
    url = "https://rondevelops1904--tryon-api-run.modal.run"
    payload = {
        "person_image": person_b64,
        "garment_image": garment_b64,
        "cloth_type": "upper"
    }
    
    print("\n🚀 Sending to Modal cloud GPU (this may take 10-15 seconds if cold booting)...")
    try:
        response = requests.post(url, json=payload)
        response.raise_for_status()
        
        data = response.json()
        
        if "image_base64" in data:
            print("\n🎉 Success! Try-on image generated.")
            output_path = "output_tryon.jpg"
            
            # Save the image
            with open(output_path, "wb") as f:
                f.write(base64.b64decode(data["image_base64"]))
                
            print(f"✅ Saved result to: {os.path.abspath(output_path)}")
            
            # Open the image automatically based on the OS
            if platform.system() == 'Darwin':       # macOS
                subprocess.call(('open', output_path))
            elif platform.system() == 'Windows':    # Windows
                os.startfile(output_path)
            else:                                   # linux variants
                subprocess.call(('xdg-open', output_path))
        else:
            print("\n❌ Error: API did not return an image.")
            print(data)
            
    except requests.exceptions.RequestException as e:
        print(f"\n❌ Request failed: {e}")
        if e.response is not None:
            print(f"Response: {e.response.text}")

if __name__ == "__main__":
    main()
