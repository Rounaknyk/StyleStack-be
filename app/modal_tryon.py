import modal
import os
import json
import urllib.request
import urllib.parse
import time

# ---------------------------------------------------------
# Modal Setup & Environment Definition
# ---------------------------------------------------------
# Everything defined in this Image runs entirely on Modal's cloud servers.
# It does NOT download these gigabytes of models to your local Mac.
app = modal.App("tryon")

# Persistent volume for model weights — survives across container restarts
model_volume = modal.Volume.from_name("tryon-model-cache", create_if_missing=True)
MODEL_DIR = "/root/model_cache"

comfyui_image = (
    modal.Image.debian_slim(python_version="3.10")
    .apt_install("git", "wget", "libgl1-mesa-glx", "libglib2.0-0")
    .pip_install(
        "torch", "torchvision", "torchaudio", 
        "requests", "fastapi[standard]", "onnxruntime", "rembg[cpu]", 
        "transformers==4.39.3", "accelerate>=0.31.0", "diffusers==0.27.2",
        "scipy", "scikit-image", "av", "huggingface_hub==0.23.5",
        "fvcore", "cloudpickle", "omegaconf", "pycocotools", "peft==0.10.0",
        "matplotlib", "opencv-python"
    )
    .pip_install("git+https://github.com/facebookresearch/detectron2.git")
    .pip_install("git+https://github.com/facebookresearch/detectron2.git#subdirectory=projects/DensePose")
    .run_commands("python -c \"from transformers import BertModel, BertTokenizer; BertModel.from_pretrained('bert-base-uncased'); BertTokenizer.from_pretrained('bert-base-uncased')\"")
    # Clone ComfyUI
    .run_commands("git clone https://github.com/comfyanonymous/ComfyUI.git /root/ComfyUI")
    .run_commands("cd /root/ComfyUI && pip install -r requirements.txt")
    # Clone Custom Nodes
    .run_commands(
        "cd /root/ComfyUI/custom_nodes && "
        "git clone https://github.com/cubiq/ComfyUI_IPAdapter_plus.git && "
        "git clone https://github.com/storyicon/comfyui_segment_anything.git && "
        "git clone https://github.com/Jcd1230/rembg-comfyui-node.git"
    )
    .run_commands("cd /root/ComfyUI/custom_nodes/comfyui_segment_anything && pip install -r requirements.txt")
    .run_commands("cd /root/ComfyUI/custom_nodes/rembg-comfyui-node && pip install -r requirements.txt")
    .run_commands("pip install transformers==4.39.3 diffusers==0.27.2 huggingface_hub==0.23.5 peft==0.10.0")
    # NO model downloads during build — they go to the Volume at startup
    .add_local_dir(os.path.join(os.path.dirname(__file__), "catvton"), remote_path="/root/catvton")
)


# ---------------------------------------------------------
# The Serverless Web Endpoint
# ---------------------------------------------------------
@app.cls(
    image=comfyui_image, 
    gpu="A10G", 
    timeout=600,
    scaledown_window=60,
    max_containers=1,
    volumes={MODEL_DIR: model_volume},
)
class API:
    @modal.enter()
    def load_catvton(self):
        import sys
        import os
        import subprocess
        import torch
        from huggingface_hub import snapshot_download
        from diffusers.image_processor import VaeImageProcessor
        
        # Point HF cache to the persistent volume so downloads survive restarts
        hf_cache = os.path.join(MODEL_DIR, "huggingface")
        os.environ["HF_HOME"] = hf_cache
        os.environ["HUGGINGFACE_HUB_CACHE"] = hf_cache
        os.makedirs(hf_cache, exist_ok=True)
        
        # Download ComfyUI models to the volume (only if not already cached)
        comfy_cache = os.path.join(MODEL_DIR, "comfyui")
        os.makedirs(comfy_cache, exist_ok=True)
        
        wget_flags = ["wget", "-q", "--show-progress", "--retry-connrefused", "--waitretry=1", "--read-timeout=20", "--timeout=15", "-t", "5"]
        
        def ensure_model(url, rel_path):
            volume_dest = os.path.join(MODEL_DIR, "comfyui", rel_path)
            container_dest = os.path.join("/root/ComfyUI/models", rel_path)
            
            if not os.path.exists(volume_dest):
                os.makedirs(os.path.dirname(volume_dest), exist_ok=True)
                print(f"  Downloading {os.path.basename(volume_dest)} to volume cache...")
                subprocess.run(wget_flags + ["-O", volume_dest, url], check=True)
                model_volume.commit()
            else:
                print(f"  Cached in volume: {os.path.basename(volume_dest)}")
                
            os.makedirs(os.path.dirname(container_dest), exist_ok=True)
            if not os.path.exists(container_dest):
                os.symlink(volume_dest, container_dest)
        
        print("Checking ComfyUI model cache...")
        ensure_model("https://huggingface.co/runwayml/stable-diffusion-v1-5/resolve/main/v1-5-pruned-emaonly.ckpt",
                     "checkpoints/v1-5-pruned-emaonly.ckpt")
        ensure_model("https://huggingface.co/h94/IP-Adapter/resolve/main/models/ip-adapter_sd15.bin",
                     "ipadapter/ip-adapter_sd15.bin")
        ensure_model("https://huggingface.co/h94/IP-Adapter/resolve/main/models/image_encoder/model.safetensors",
                     "clip_vision/model.safetensors")
        ensure_model("https://dl.fbaipublicfiles.com/segment_anything/sam_vit_h_4b8939.pth",
                     "sams/sam_vit_h_4b8939.pth")
        ensure_model("https://huggingface.co/ShilongLiu/GroundingDINO/resolve/main/groundingdino_swint_ogc.pth",
                     "grounding-dino/groundingdino_swint_ogc.pth")
        ensure_model("https://huggingface.co/ShilongLiu/GroundingDINO/resolve/main/GroundingDINO_SwinT_OGC.cfg.py",
                     "grounding-dino/GroundingDINO_SwinT_OGC.cfg.py")
        
        print("Downloading CatVTON models (cached in volume)...")
        subprocess.run(["huggingface-cli", "download", "booksforcharlie/stable-diffusion-inpainting", "--exclude", "*.fp16.*"], check=True)
        subprocess.run(["huggingface-cli", "download", "zhengchong/CatVTON"], check=True)
        
        # Commit the volume so downloads persist
        model_volume.commit()
        
        # Add the catvton directory to sys.path so its modules can import each other
        sys.path.append(os.path.join(os.path.dirname(__file__), "catvton"))
        
        from model.cloth_masker import AutoMasker
        from model.pipeline import CatVTONPipeline
        from utils import init_weight_dtype
        
        print("Loading CatVTON models into VRAM...")
        repo_path = os.path.join(hf_cache, "hub/models--zhengchong--CatVTON/snapshots/2969fcf85fe62f2036605716f0b56f0b81d01d79")
        if not os.path.exists(repo_path):
            repo_path = snapshot_download(repo_id="zhengchong/CatVTON")
        
        base_ckpt = os.path.join(hf_cache, "hub/models--booksforcharlie--stable-diffusion-inpainting/snapshots/d260cf3646fe94c6d5d70820b77512f836d7cce2")
        if not os.path.exists(base_ckpt):
            base_ckpt = "booksforcharlie/stable-diffusion-inpainting"
            
        self.catvton_pipeline = CatVTONPipeline(
            base_ckpt=base_ckpt,
            attn_ckpt=repo_path,
            attn_ckpt_version="mix",
            weight_dtype=init_weight_dtype("bf16"),
            use_tf32=True,
            device='cuda'
        )
        
        self.mask_processor = VaeImageProcessor(vae_scale_factor=8, do_normalize=False, do_binarize=True, do_convert_grayscale=True)
        self.automasker = AutoMasker(
            densepose_ckpt=os.path.join(repo_path, "DensePose"),
            schp_ckpt=os.path.join(repo_path, "SCHP"),
            device='cuda', 
        )
        print("CatVTON is ready.")

    @modal.enter()
    def start_comfyui(self):
        import subprocess
        import urllib.request
        import time
        import os
        print("Starting ComfyUI daemon...")
        
        # Start the ComfyUI server in the background inside this Modal container
        self.server_process = subprocess.Popen(
            ["python", "-u", "main.py", "--port", "8188"],
            cwd="/root/ComfyUI"
        )
        
        # Wait for ComfyUI to boot up by polling the API
        server_ready = False
        for _ in range(120): # Wait up to 120 seconds for models to load into VRAM
            if self.server_process.poll() is not None:
                print("ComfyUI process crashed!")
                out, _ = self.server_process.communicate()
                print("Crash Logs:")
                print(out.decode())
                break
            try:
                urllib.request.urlopen("http://127.0.0.1:8188/history")
                server_ready = True
                break
            except Exception:
                time.sleep(1)
                
        if not server_ready:
            self.server_process.terminate()
            raise Exception("ComfyUI failed to start within the timeout.")
        print("ComfyUI is ready to accept requests.")

    @modal.exit()
    def stop_comfyui(self):
        if hasattr(self, 'server_process'):
            self.server_process.kill()

    @modal.fastapi_endpoint(method="POST")
    def run(self, item: dict):
        """
        Expects a JSON payload: 
        {
            "person_image": "<base64_string>",
            "garment_image": "<base64_string>"
        }
        """
        import base64
        import urllib.request
        import json
        import time
        import os
        import io
        from PIL import Image
        
        def decode_image(b64_str):
            img_data = base64.b64decode(b64_str)
            return Image.open(io.BytesIO(img_data)).convert("RGB")
        
        try:
            person_img = decode_image(item["person_image"])
            garment_img = decode_image(item["garment_image"])
            cloth_type = item.get("cloth_type", "upper")
            
            # CatVTON expects 768x1024 or 384x512
            # We use 768x1024 for high quality
            import sys
            import os
            sys.path.append(os.path.join(os.path.dirname(__file__), "catvton"))
            from utils import resize_and_crop, resize_and_padding
            
            person_img = resize_and_crop(person_img, (768, 1024))
            garment_img = resize_and_padding(garment_img, (768, 1024))
            
            # 1. Generate mask automatically
            mask = self.automasker(person_img, cloth_type)['mask']
            mask = self.mask_processor.blur(mask, blur_factor=9)
            
            # 2. Run Inference
            import torch
            generator = torch.Generator(device='cuda').manual_seed(42)
            
            result_image = self.catvton_pipeline(
                image=person_img,
                condition_image=garment_img,
                mask=mask,
                num_inference_steps=50,
                guidance_scale=2.5,
                generator=generator
            )[0]
            
            # Encode result
            buf = io.BytesIO()
            result_image.save(buf, format="PNG")
            img_b64 = base64.b64encode(buf.getvalue()).decode()
            
            return {"status": "success", "image_base64": img_b64}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def _run_comfyui_workflow(self, workflow):
        """Helper: send a workflow to ComfyUI and wait for completion. Returns the history outputs."""
        import urllib.request
        import json
        import time
        
        p = {"prompt": workflow}
        data = json.dumps(p).encode('utf-8')
        req = urllib.request.Request("http://127.0.0.1:8188/prompt", data=data)
        response = urllib.request.urlopen(req)
        prompt_id = json.loads(response.read())['prompt_id']
        
        for _ in range(300):  # 5 min max
            try:
                req = urllib.request.Request(f"http://127.0.0.1:8188/history/{prompt_id}")
                response = urllib.request.urlopen(req)
                history = json.loads(response.read())
                if prompt_id in history:
                    return history[prompt_id].get("outputs", {})
            except Exception:
                pass
            time.sleep(1)
        return None

    @modal.fastapi_endpoint(method="POST")
    def digitize(self, item: dict):
        """
        Wardrobe Digitizer: Segments individual clothing items from a person photo.
        
        Input:  {"person_image": "<base64>"}
        Output: {"status": "success", "items": {"upper": "<base64>", "lower": "<base64>", "shoes": "<base64>"}}
        """
        import base64
        import urllib.request
        import json
        import time
        import os
        import io
        import glob
        import numpy as np
        from PIL import Image
        
        def resize_image(b64_str):
            img_data = base64.b64decode(b64_str)
            img = Image.open(io.BytesIO(img_data)).convert("RGB")
            img.thumbnail((768, 1024), Image.Resampling.LANCZOS)
            return img
        
        # Save person image
        person_img = resize_image(item["person_image"])
        input_dir = "/root/ComfyUI/input"
        os.makedirs(input_dir, exist_ok=True)
        person_img.save(os.path.join(input_dir, "person_digitize.jpg"))
        
        # Clean old outputs
        output_dir = "/root/ComfyUI/output"
        for f in glob.glob(os.path.join(output_dir, "digi_*")):
            os.remove(f)
        
        # Narrower, more specific prompts + higher threshold
        segments = [
            ("upper", "shirt", 0.35),
            ("lower", "pants", 0.35),
            ("shoes", "shoes", 0.3),
        ]
        
        # PHASE 1: Collect all raw masks
        raw_masks = {}
        
        for item_name, prompt_text, threshold in segments:
            # Workflow: LoadImage → GroundingDINO+SAM → MaskToImage → SaveImage
            workflow = {
                "1": {
                    "inputs": {"image": "person_digitize.jpg", "upload": "image"},
                    "class_type": "LoadImage"
                },
                "2": {
                    "inputs": {"model_name": "GroundingDINO_SwinT_OGC (694MB)"},
                    "class_type": "GroundingDinoModelLoader (segment anything)"
                },
                "3": {
                    "inputs": {"model_name": "sam_vit_h (2.56GB)"},
                    "class_type": "SAMModelLoader (segment anything)"
                },
                "4": {
                    "inputs": {
                        "prompt": prompt_text,
                        "threshold": threshold,
                        "sam_model": ["3", 0],
                        "grounding_dino_model": ["2", 0],
                        "image": ["1", 0]
                    },
                    "class_type": "GroundingDinoSAMSegment (segment anything)"
                },
                "5": {
                    "inputs": {"mask": ["4", 1]},
                    "class_type": "MaskToImage"
                },
                "6": {
                    "inputs": {
                        "filename_prefix": f"digi_{item_name}",
                        "images": ["5", 0]
                    },
                    "class_type": "SaveImage"
                }
            }
            
            outputs = self._run_comfyui_workflow(workflow)
            if outputs is None:
                continue
            
            # Find the saved mask image
            for node_id, node_output in outputs.items():
                if "images" in node_output:
                    for img_info in node_output["images"]:
                        filename = img_info["filename"]
                        subfolder = img_info.get("subfolder", "")
                        mask_path = os.path.join(output_dir, subfolder, filename)
                        if os.path.exists(mask_path):
                            mask_img = Image.open(mask_path).convert("L").resize(person_img.size, Image.NEAREST)
                            raw_masks[item_name] = np.array(mask_img)
                            break
                    break
        
        # PHASE 2: Subtract overlaps — each mask removes pixels claimed by others
        clean_masks = {}
        for name, mask in raw_masks.items():
            clean = mask.copy()
            for other_name, other_mask in raw_masks.items():
                if other_name != name:
                    overlap = (clean > 128) & (other_mask > 128)
                    clean[overlap] = 0
            clean_masks[name] = clean
        
        # PHASE 3: Create cutouts from clean masks
        results = {}
        person_np = np.array(person_img)
        
        for item_name, mask_np in clean_masks.items():
            if not np.any(mask_np > 128):
                continue
            
            white_bg = np.full_like(person_np, 255)
            cutout = np.where(mask_np[:, :, None] > 128, person_np, white_bg)
            
            # Crop to bounding box with padding
            rows = np.any(mask_np > 128, axis=1)
            cols = np.any(mask_np > 128, axis=0)
            if rows.any() and cols.any():
                rmin, rmax = np.where(rows)[0][[0, -1]]
                cmin, cmax = np.where(cols)[0][[0, -1]]
                pad = 15
                rmin = max(0, rmin - pad)
                rmax = min(cutout.shape[0], rmax + pad)
                cmin = max(0, cmin - pad)
                cmax = min(cutout.shape[1], cmax + pad)
                cutout = cutout[rmin:rmax, cmin:cmax]
            
            cutout_img = Image.fromarray(cutout.astype(np.uint8))
            buf = io.BytesIO()
            cutout_img.save(buf, format="PNG")
            results[item_name] = base64.b64encode(buf.getvalue()).decode()
        
        if not results:
            return {"status": "error", "message": "No clothing items detected."}
        
        return {"status": "success", "items": results}
