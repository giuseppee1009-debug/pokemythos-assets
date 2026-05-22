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

def get_shiny_url(sid, variant, pkdex):
    entry = pkdex.get(str(sid))
    if not entry: return None
    
    if not variant:
        return entry.get('gif_url_shiny')
    
    if variant == 'female':
        f_url = entry.get('gif_url_shiny_female')
        if not f_url:
            base_url = entry.get('gif_url_shiny')
            if base_url:
                f_url = base_url.replace('.gif', '-f.gif')
        return f_url
        
    if variant == 'gmax':
        return entry.get('gmax', {}).get('gif_url_shiny')
        
    if variant == 'mega': return entry.get('mega_shiny_gif')
    if variant == 'megax': return entry.get('mega_x_shiny_gif')
    if variant == 'megay': return entry.get('mega_y_shiny_gif')
        
    forms = entry.get('forms', {})
    for fk, fv in forms.items():
        norm_fk = fk.replace('-', '_').replace(' ', '_').lower()
        if norm_fk == variant:
            return fv.get('gif_url_shiny')
            
    return None

def process_file_worker(f, pkdex, sparkle_frames, sparkle_durations, print_lock):
    match = re.match(r"^(\d{3})(?:_([a-z0-9_]+))?\.gif$", f)
    if not match: return
    
    sid = int(match.group(1))
    variant = match.group(2)
    
    shiny_filename = f.replace(".gif", "_shiny.gif")
    shiny_url = get_shiny_url(sid, variant, pkdex)
    
    if not shiny_url:
        with print_lock:
            print(f"[{f}] URL shiny non trovato nel DB per il tipo di file specificato.")
        return
        
    try:
        urllib_session = requests.Session()
        r = urllib_session.get(shiny_url, timeout=15)
        if r.status_code != 200:
            with print_lock:
                print(f"[{f}] Errore download dall'URL: {shiny_url} (HTTP {r.status_code})")
            return
            
        process_shiny(r.content, sparkle_frames, sparkle_durations, shiny_filename)
        with print_lock:
            print(f"[\u2714] {f} -> {shiny_filename}")
    except Exception as e:
        with print_lock:
            print(f"[\u2718] Errore elaborazione {f}: {e}")

def get_variant_category(filename):
    match = re.match(r"^\d{3}(?:_([a-z0-9_]+))?\.gif$", filename)
    if not match: return "ignora"
    variant = match.group(1)
    if not variant: return "base"
    if variant == "female": return "femmina"
    if "mega" in variant: return "mega"
    return "forme-alternative"

def main():
    print("=" * 50)
    print("GENERATORE SHINY MULTI-THREAD")
    print("=" * 50)
    print("Scegli su quali pokemon eseguire l'operazione (verranno sovrascritti i corrispettivi _shiny.gif):")
    print("1: Solo Base (ex. 001.gif)")
    print("2: Solo Femmine (ex. 003_female.gif)")
    print("3: Solo Mega (ex. 006_megax.gif)")
    print("4: Forme alternative / Altro (ex. gmax, hisui, ecc)")
    print("5: Tutti assieme")
    
    scelta = input("Digita il numero (1-5): ").strip()
    
    if scelta not in ["1", "2", "3", "4", "5"]:
        print("Scelta non valida.")
        return

    species_path = os.path.abspath(os.path.join("..", "..", "pokemythosbuono", "data", "species.json"))
    
    if not os.path.exists(species_path):
        print(f"File species.json non trovato in: {species_path}")
        print("Assicurati di lanciare lo script dalla cartella Desktop/pokemythos/Assets/assets/")
        return
        
    with open(species_path, "r", encoding="utf-8") as f:
        pkdex = json.load(f)
        
    sparkles_url = "https://giuseppee1009-debug.github.io/pokemythos-assets/assets/sparkles_effect.gif"
    print("Scaricando sparkles_effect.gif...")
    r_sp = requests.get(sparkles_url)
    if r_sp.status_code != 200:
        print("Errore nel download delle stelle.")
        return
    with Image.open(io.BytesIO(r_sp.content)) as sp_im:
        sparkle_frames = []
        sparkle_durations = []
        for f in ImageSequence.Iterator(sp_im):
            sparkle_frames.append(f.copy().convert("RGBA"))
            sparkle_durations.append(max(20, f.info.get('duration', 80)))
            
    tutti_files = [f for f in os.listdir(".") if f.endswith(".gif") and not f.endswith("_shiny.gif")]
    
    files_da_processare = []
    for f in tutti_files:
        cat = get_variant_category(f)
        if cat == "ignora": continue
        
        if scelta == "1" and cat == "base": files_da_processare.append(f)
        elif scelta == "2" and cat == "femmina": files_da_processare.append(f)
        elif scelta == "3" and cat == "mega": files_da_processare.append(f)
        elif scelta == "4" and cat == "forme-alternative": files_da_processare.append(f)
        elif scelta == "5": files_da_processare.append(f)
        
    print_lock = Lock()
    workers = min(32, (os.cpu_count() or 4) * 2)
    print(f"Trovati {len(files_da_processare)} pokemon. Inizio elaborazione multi-thread con {workers} esecutori in parallelo...")
    
    if not files_da_processare:
        print("Non ci sono pokemon corrispondenti alla tua scelta in questa cartella.")
        return
        
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(process_file_worker, f, pkdex, sparkle_frames, sparkle_durations, print_lock): f
            for f in files_da_processare
        }
        concurrent.futures.wait(futures)

    print("\nFatto! File aggiornati in locale.")

if __name__ == "__main__":
    main()
