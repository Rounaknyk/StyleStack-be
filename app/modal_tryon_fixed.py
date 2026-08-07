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
app = modal.App("stylestack-tryon-backend")

def download_models():
    """Downloads weights to the Modal container during the cloud build phase."""
    import subprocess
    # Use robust wget flags for Hugging Face downloads to avoid transient drops
    wget_flags = ["wget", "-q", "--show-progress", "--retry-connrefused", "--waitretry=1", "--read-timeout=20", "--timeout=15", "-t", "5", "-O"]
    
    # Base SD 1.5 Model
    subprocess.run(wget_flags + ["/root/ComfyUI/models/checkpoints/v1-5-pruned-emaonly.ckpt", 
                    "https://huggingface.co/runwayml/stable-diffusion-v1-5/resolve/main/v1-5-pruned-emaonly.ckpt"], check=True)
    
    # Create directories for IP-Adapter and CLIP Vision
    os.makedirs("/root/ComfyUI/models/ipadapter", exist_ok=True)
    os.makedirs("/root/ComfyUI/models/clip_vision", exist_ok=True)
    
    # IP-Adapter Models
    subprocess.run(wget_flags + ["/root/ComfyUI/models/ipadapter/ip-adapter_sd15.bin", 
                    "https://huggingface.co/h94/IP-Adapter/resolve/main/models/ip-adapter_sd15.bin"], check=True)
    subprocess.run(wget_flags + ["/root/ComfyUI/models/clip_vision/model.safetensors", 
                    "https://huggingface.co/h94/IP-Adapter/resolve/main/models/image_encoder/model.safetensors"], check=True)
                    
    # Segment Anything (SAM) Model
    os.makedirs("/root/ComfyUI/models/sams", exist_ok=True)
    subprocess.run(wget_flags + ["/root/ComfyUI/models/sams/sam_vit_h_4b8939.pth", 
                    "https://dl.fbaipublicfiles.com/segment_anything/sam_vit_h_4b8939.pth"], check=True)
                    
    # GroundingDINO Model (Must match exact lowercase filename expected by the node)
    os.makedirs("/root/ComfyUI/models/grounding-dino", exist_ok=True)
    subprocess.run(wget_flags + ["/root/ComfyUI/models/grounding-dino/groundingdino_swint_ogc.pth", 
                    "https://huggingface.co/ShilongLiu/GroundingDINO/resolve/main/groundingdino_swint_ogc.pth"], check=True)
    subprocess.run(wget_flags + ["/root/ComfyUI/models/grounding-dino/GroundingDINO_SwinT_OGC.cfg.py", 
                    "https://huggingface.co/ShilongLiu/GroundingDINO/resolve/main/GroundingDINO_SwinT_OGC.cfg.py"], check=True)

comfyui_image = (
    modal.Image.debian_slim(python_version="3.10")
    .apt_install("git", "wget", "libgl1-mesa-glx", "libglib2.0-0")
    .pip_install("torch", "torchvision", "torchaudio", "requests", "fastapi[standard]", "onnxruntime", "rembg[cpu]", "transformers<4.40.0")
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
    .run_commands("pip install transformers==4.39.3")
    # Download weights to the cloud container
    .run_function(download_models)
)

