import os
import io
import re
import math
import json
import requests
import concurrent.futures
from threading import Lock
from PIL import Image, ImageSequence

def get_global_bbox(im):
    left, upper, right, lower = im.width, im.height, 0, 0
    found_content = False
    for frame in ImageSequence.Iterator(im):
        bbox = frame.getbbox()
        if bbox:
            found_content = True
            l, u, r, l_ = bbox
            left = min(left, l)
            upper = min(upper, u)
            right = max(right, r)
            lower = max(lower, l_)
            
    if not found_content:
        return (0, 0, im.width, im.height)
    return (max(0, left-2), max(0, upper-2), min(im.width, right+2), min(im.height, lower+2))

def get_frame_at_time(frames, durations, t, loop=True):
    total = sum(durations)
    if total == 0: return frames[0]
    if loop: t = t % total
    else:
        if t >= total: return None
    curr = 0
    for i, d in enumerate(durations):
        curr += d
        if t < curr: return frames[i]
    return frames[-1]

def process_shiny(shiny_bytes, sparkles_frames, sparkle_durations, output_path, target_size=280):
    with Image.open(io.BytesIO(shiny_bytes)) as im:
        bbox = get_global_bbox(im)
        crop_w = bbox[2] - bbox[0]
        crop_h = bbox[3] - bbox[1]
        
        ratio = target_size / max(crop_w, crop_h)
        if ratio > 10: ratio = 10 
        
        new_w = int(crop_w * ratio)
        new_h = int(crop_h * ratio)
        new_size = (new_w, new_h)
        
        base_frames = []
        base_durations = []
        for f in ImageSequence.Iterator(im):
            base_frames.append(f.copy().convert("RGBA").crop(bbox).resize(new_size, Image.Resampling.NEAREST))
            base_durations.append(max(20, f.info.get('duration', 100)))
            
    sp_im_w, sp_im_h = sparkles_frames[0].size
    sp_ratio = min(new_w / sp_im_w, new_h / sp_im_h)
    sp_new_w = int(sp_im_w * sp_ratio)
    sp_new_h = int(sp_im_h * sp_ratio)
    sp_resize = (sp_new_w, sp_new_h)
    sp_x = (new_w - sp_new_w) // 2
    sp_y = (new_h - sp_new_h) // 2
    
    resized_sparkles = [sf.resize(sp_resize, Image.Resampling.NEAREST) for sf in sparkles_frames]
        
    total_pkmn_duration = sum(base_durations)
    total_sparkle_duration = sum(sparkle_durations)
    
    min_required = max(3000, total_sparkle_duration + 500)
    if total_pkmn_duration <= 0: total_pkmn_duration = 100
    loops_needed = math.ceil(min_required / total_pkmn_duration)
    target_duration = loops_needed * total_pkmn_duration
    
    step_ms = 40 
    num_frames = max(1, target_duration // step_ms)
    
    processed_frames = []
    final_durations = []
    
    for i in range(num_frames):
        t = i * step_ms
        base_f = get_frame_at_time(base_frames, base_durations, t, loop=True)
        sp_f = get_frame_at_time(resized_sparkles, sparkle_durations, t, loop=False)
        
        canvas = Image.new("RGBA", new_size, (0, 0, 0, 0))
        canvas.paste(base_f, (0, 0), base_f)
        
        if sp_f:
            canvas.paste(sp_f, (sp_x, sp_y), sp_f)
            
        processed_frames.append(canvas)
        final_durations.append(step_ms)
        
    processed_frames[0].save(
        output_path, 
        format='GIF', 
        save_all=True, 
        append_images=processed_frames[1:], 
        duration=final_durations, 
        loop=0, 
        disposal=2, 
        optimize=False,
        transparency=0
    )

def process_local_gmax(f, name_to_id, sparkle_frames, sparkle_durations, print_lock):
    basename = f.replace("_shiny_gmax.gif", "").replace("-", "_").lower()
    
    sid = name_to_id.get(basename)
    if not sid:
        with print_lock:
            print(f"[{f}] Impossibile determinare l'ID del Pokedex per '{basename}'.")
        return
        
    out_name = f"{sid}_gmax_shiny.gif"
    
    try:
        with open(f, "rb") as bf:
            gif_bytes = bf.read()
            
        process_shiny(gif_bytes, sparkle_frames, sparkle_durations, out_name)
        with print_lock:
            print(f"[\u2714] Convertito e rinominato: {f} -> {out_name}")
    except Exception as e:
        with print_lock:
            print(f"[\u2718] Errore su {f}: {e}")

def main():
    print("=" * 50)
    print("CONVERTITORE GMAX SHINY (LOCAL FILES)")
    print("=" * 50)
    
    species_path = r"C:\Users\pesci\Desktop\pokemythos\pokemythosbuono\data\species.json"
    if not os.path.exists(species_path):
        print("Errore critico: species.json non trovato!")
        return
        
    with open(species_path, "r", encoding="utf-8") as file_db:
        pkdex = json.load(file_db)
        
    name_to_id = {}
    for k, v in pkdex.items():
        n = v.get("name", "").lower().replace(" ", "_").replace("-", "_")
        name_to_id[n] = k.zfill(3)

    sparkles_url = "https://giuseppee1009-debug.github.io/pokemythos-assets/assets/sparkles_effect.gif"
    print("Scaricando sparkles_effect.gif...")
    r_sp = requests.get(sparkles_url)
    if r_sp.status_code != 200:
        print("Errore nel download delle stelle.")
        return
        
    with Image.open(io.BytesIO(r_sp.content)) as sp_im:
        sparkle_frames = []
        sparkle_durations = []
        for frame in ImageSequence.Iterator(sp_im):
            sparkle_frames.append(frame.copy().convert("RGBA"))
            sparkle_durations.append(max(20, frame.info.get('duration', 80)))
            
    files_to_process = [f for f in os.listdir(".") if f.endswith("_shiny_gmax.gif")]
    
    if not files_to_process:
        print("Nessun file *_shiny_gmax.gif trovato in questa cartella.")
        return
        
    print_lock = Lock()
    workers = min(16, (os.cpu_count() or 4))
    print(f"Trovati {len(files_to_process)} G-Max. Inizio elaborazione ({workers} threads)...")
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(process_local_gmax, f, name_to_id, sparkle_frames, sparkle_durations, print_lock): f
            for f in files_to_process
        }
        concurrent.futures.wait(futures)

    print("\nFatto! GIF Processate e Rinominate!")

if __name__ == "__main__":
    main()
