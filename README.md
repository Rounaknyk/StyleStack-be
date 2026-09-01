# StyleStack Backend — Modal Cloud GPU APIs

## Overview
StyleStack's AI backend runs entirely on **Modal.com** cloud GPUs. No local GPU or disk space required.

**Live Endpoint Base**: `https://rounaknaik1904--tryon-api-{method}.modal.run`

---

## 🔧 Current Endpoints

### 1. Wardrobe Digitizer ✅ (Working)
Segments individual clothing items from a full-body photo and returns clean cutouts on white backgrounds.

**Endpoint**: `POST https://rounaknaik1904--tryon-api-digitize.modal.run`

**Request**:
```json
{
  "person_image": "<base64_encoded_image>"
}
```

**Response**:
```json
{
  "status": "success",
  "items": {
    "upper": "<base64_png>",
    "lower": "<base64_png>",
    "shoes": "<base64_png>"
  }
}
```

**Tech Stack**: GroundingDINO + SAM + numpy compositing on NVIDIA A10G GPU  
**Latency**: ~10s warm / ~90s cold boot  
**Test**: `python3 test_digitize.py` (opens file browser)

---

### 2. Virtual Try-On 🚧 (Needs CatVTON upgrade)
Current IP-Adapter approach produces poor results. Needs migration to **CatVTON** model for accurate garment fitting.

**Endpoint**: `POST https://rounaknaik1904--tryon-api-run.modal.run`

---

## 🗺️ Future Implementation: CatVTON Virtual Try-On

**Model**: [CatVTON](https://github.com/Zheng-Chong/CatVTON) — purpose-built virtual try-on using diffusion inpainting  
**Base**: `stable-diffusion-inpainting`  
**Weights**: `zhengchong/CatVTON` (HuggingFace)  
**Auto-Masking**: DensePose + SCHP for automatic clothing region detection  

### Architecture
```
Person Image + Garment Image
        ↓
  AutoMasker (DensePose + SCHP)
        ↓
  CatVTONPipeline (SD Inpainting + CatVTON attention)
        ↓
  Try-On Result Image
```

### Deployment Plan
- Deploy CatVTON as a Modal class with `@modal.enter()` for model preloading
- Download all weights during `modal.Image` build phase (no local storage needed)
- Single POST endpoint: person_image + garment_image → result_image
- Target latency: ~15-20s warm

---

## 💰 Modal Credits
- Free tier: $1/month
- GPU: NVIDIA A10G ($0.000575/sec ≈ $2.07/hr)
- **Important**: `max_containers=1` is set to prevent runaway costs
- `scaledown_window=60` shuts down idle containers quickly

## ⚙️ Feature Flags & Admin Controls
We use Supabase `app_config` to control runtime feature flags (e.g., hiding Google Calendar Sync during store review).

### Toggling Features
There are two ways to toggle features:
1. **In-App Admin UI (Recommended)**: If you log in with your owner email (`rounakpro18@gmail.com` or `rondevelops1904@gmail.com`), you can navigate to **Profile > Preferences > Admin tools** and toggle features directly. This uses the `/api/v1/config/owner/features` endpoint securely with your Firebase token.
2. **Backend Script**: You can run `./scripts/toggle_calendar_sync.sh` from this directory. This script hits the `/api/v1/config/features` endpoint using the `ADMIN_API_KEY`.

## 📁 Project Structure
```
app/
  api/
    routes/
      config.py       # Feature flag endpoints
  modal_tryon.py      # Main Modal deployment (all endpoints)
scripts/
  toggle_calendar_sync.sh # Bash script to toggle features via Admin API Key
test_digitize.py      # Interactive wardrobe digitizer test
test_api.py           # Virtual try-on test
test_curl.py          # Quick API test
```
