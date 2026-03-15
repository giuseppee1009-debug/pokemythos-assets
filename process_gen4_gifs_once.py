import argparse
import io
import json
import math
import os
from pathlib import Path

import requests
from PIL import Image, ImageFile, ImageSequence

ImageFile.LOAD_TRUNCATED_IMAGES = True


def _normalize_variant_token(variant):
    if variant is None:
        return None
    token = str(variant).strip().lower()
    if not token:
        return None
    token = token.replace("-", "_").replace(" ", "_")
    alias_map = {
        "mega_x": "megax",
        "mega_y": "megay",
        "gigantamax": "gmax",
        "g_max": "gmax",
    }
    token = alias_map.get(token, token)
    token = "".join(ch for ch in token if ch.isalnum() or ch == "_")
    return token or None


def build_asset_name(species_id, variant=None, shiny=False):
    base = f"{int(species_id):03d}"
    variant_token = _normalize_variant_token(variant)
    if variant_token:
        base += f"_{variant_token}"
    if shiny:
        base += "_shiny"
    return f"{base}.gif"

def get_global_bbox(frames):
    if not frames:
        return (0, 0, 1, 1)
    left, upper, right, lower = frames[0].width, frames[0].height, 0, 0
    found = False
    for fr in frames:
        bbox = fr.getbbox()
        if not bbox:
            continue
        found = True
        l, u, r, b = bbox
        left = min(left, l)
        upper = min(upper, u)
        right = max(right, r)
        lower = max(lower, b)
    if not found:
        return (0, 0, frames[0].width, frames[0].height)
    return (
        max(0, left - 2),
        max(0, upper - 2),
        min(frames[0].width, right + 2),
        min(frames[0].height, lower + 2),
    )


def load_sparkle_frames(path):
    if not path or not path.exists():
        return []
    try:
        with Image.open(path) as im:
            return [f.copy().convert("RGBA") for f in ImageSequence.Iterator(im)]
    except Exception:
        return []


def resize_gif_bytes(image_data, target_size, is_shiny=False, loop_ms=3000, sparkles=None):
    try:
        with Image.open(io.BytesIO(image_data)) as im:
            frames = []
            durations = []
            for frame in ImageSequence.Iterator(im):
                frames.append(frame.copy().convert("RGBA"))
                durations.append(max(80, int(frame.info.get("duration", 100))))

        if not frames:
            return None

        bbox = get_global_bbox(frames)
        crop_w = max(1, bbox[2] - bbox[0])
        crop_h = max(1, bbox[3] - bbox[1])

        ratio = target_size / max(crop_w, crop_h)
        if ratio > 10:
            ratio = 10

        new_w = max(1, int(crop_w * ratio))
        new_h = max(1, int(crop_h * ratio))
        new_size = (new_w, new_h)

        resized_frames = []
        for fr in frames:
            cropped = fr.crop(bbox)
            resized_frames.append(cropped.resize(new_size, Image.Resampling.NEAREST))

        total_duration = sum(durations)
        if total_duration <= 0:
            total_duration = 100

        sparkle_frames = sparkles if (is_shiny and sparkles) else []
        sparkle_duration = len(sparkle_frames) * 80 if sparkle_frames else 0

        target_duration = max(loop_ms, sparkle_duration + 500) if loop_ms > 0 else total_duration
        loops_needed = max(1, int(math.ceil(target_duration / total_duration)))

        resized_sparkles = []
        sp_x = sp_y = 0
        if sparkle_frames:
            sp_w, sp_h = sparkle_frames[0].size
            sp_ratio = min(new_w / sp_w, new_h / sp_h)
            sp_new_w = max(1, int(sp_w * sp_ratio))
            sp_new_h = max(1, int(sp_h * sp_ratio))
            sp_x = (new_w - sp_new_w) // 2
            sp_y = (new_h - sp_new_h) // 2
            for sf in sparkle_frames:
                resized_sparkles.append(sf.resize((sp_new_w, sp_new_h), Image.Resampling.NEAREST))

        processed_frames = []
        final_durations = []
        sparkle_idx = 0

        for _ in range(loops_needed):
            for i, frame in enumerate(resized_frames):
                canvas = Image.new("RGBA", new_size, (0, 0, 0, 0))
                canvas.paste(frame, (0, 0), frame)
                if sparkle_idx < len(resized_sparkles):
                    sp = resized_sparkles[sparkle_idx]
                    canvas.paste(sp, (sp_x, sp_y), sp)
                    sparkle_idx += 1
                processed_frames.append(canvas)
                final_durations.append(durations[i])

        if not processed_frames:
            return None

        out_buffer = io.BytesIO()
        processed_frames[0].save(
            out_buffer,
            format="GIF",
            save_all=True,
            append_images=processed_frames[1:],
            duration=final_durations,
            loop=0,
            disposal=2,
            optimize=True,
            transparency=0,
        )
        out_buffer.seek(0)
        return out_buffer.getvalue()
    except Exception:
        return None


def fetch_bytes(url, root_dir, session, cache):
    if not url:
        return None

    cached = cache.get(url)
    if cached is not None:
        return cached

    if url.startswith("http://") or url.startswith("https://"):
        try:
            resp = session.get(url, timeout=20)
            if resp.status_code == 200:
                cache[url] = resp.content
                return resp.content
            return None
        except Exception:
            return None

    local_path = (root_dir / url).resolve()
    if local_path.exists():
        try:
            data = local_path.read_bytes()
            cache[url] = data
            return data
        except Exception:
            return None
    return None


def select_url(info, gif_key, img_key):
    return info.get(gif_key) or info.get(img_key)


def collect_entries(species_id, info):
    entries = []

    base = [
        (None, False, select_url(info, "gif_url", "image_url")),
        (None, True, select_url(info, "gif_url_shiny", "image_url_shiny")),
        ("female", False, select_url(info, "gif_url_female", "image_url_female")),
        ("female", True, select_url(info, "gif_url_shiny_female", "image_url_shiny_female")),
    ]
    entries.extend(base)

    mega_map = [
        ("mega", "mega_gif", "mega"),
        ("mega", "mega_shiny_gif", "mega_shiny"),
        ("megax", "mega_x_gif", "mega_x"),
        ("megax", "mega_x_shiny_gif", "mega_x_shiny"),
        ("megay", "mega_y_gif", "mega_y"),
        ("megay", "mega_y_shiny_gif", "mega_y_shiny"),
    ]
    for variant, gif_key, img_key in mega_map:
        shiny = "shiny" in gif_key or "shiny" in img_key
        url = select_url(info, gif_key, img_key)
        entries.append((variant, shiny, url))

    gmax = info.get("gmax")
    if isinstance(gmax, dict):
        entries.append(("gmax", False, select_url(gmax, "gif_url", "image_url")))
        entries.append(("gmax", True, select_url(gmax, "gif_url_shiny", "image_url_shiny")))

    forms = info.get("forms")
    if isinstance(forms, dict):
        for form_key, form_info in forms.items():
            if not isinstance(form_info, dict):
                continue
            variant = _normalize_variant_token(form_key)
            if not variant:
                continue
            entries.append((variant, False, select_url(form_info, "gif_url", "image_url")))
            entries.append((variant, True, select_url(form_info, "gif_url_shiny", "image_url_shiny")))

    final_entries = []
    for variant, shiny, url in entries:
        if not url:
            continue
        final_entries.append((variant, bool(shiny), url))
    return final_entries


def resolve_default_species_path(root_dir):
    candidate = root_dir / "data" / "species.json"
    if candidate.exists():
        return candidate
    candidate = root_dir / "setups" / "species.json"
    if candidate.exists():
        return candidate
    raise FileNotFoundError("species.json not found in data/ or setups/")


def resolve_default_output_dir(root_dir):
    candidate = root_dir / "pokemythos-assets" / "assets"
    if candidate.exists():
        return candidate
    return root_dir / "assets_generated"


def main():
    parser = argparse.ArgumentParser(description="Download and resize Showdown assets into numbered GIFs.")
    parser.add_argument("--species", type=str, default=None, help="Path to species.json")
    parser.add_argument("--out", type=str, default=None, help="Output directory")
    parser.add_argument("--min-id", type=int, default=1, help="Minimum species id")
    parser.add_argument("--max-id", type=int, default=721, help="Maximum species id")
    parser.add_argument("--size", type=int, default=280, help="Target size (pixels)")
    parser.add_argument("--loop-ms", type=int, default=3000, help="Minimum GIF duration in ms (0 to disable)")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing files")
    parser.add_argument("--sparkles", action="store_true", help="Overlay sparkles on shiny using assets/sparkles_effect.gif")
    args = parser.parse_args()

    root_dir = Path(__file__).resolve().parent
    species_path = Path(args.species) if args.species else resolve_default_species_path(root_dir)
    out_dir = Path(args.out) if args.out else resolve_default_output_dir(root_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    with species_path.open("r", encoding="utf-8") as f:
        pokedex = json.load(f)

    sparkle_frames = []
    if args.sparkles:
        sparkle_frames = load_sparkle_frames(root_dir / "assets" / "sparkles_effect.gif")

    total_saved = 0
    total_skipped = 0
    total_failed = 0
    failed_entries = []

    cache = {}
    session = requests.Session()

    for sid in range(args.min_id, args.max_id + 1):
        info = pokedex.get(str(sid))
        if not isinstance(info, dict):
            continue

        entries = collect_entries(sid, info)
        for variant, shiny, url in entries:
            filename = build_asset_name(sid, variant, shiny)
            out_path = out_dir / filename

            if out_path.exists() and not args.overwrite:
                total_skipped += 1
                continue

            data = fetch_bytes(url, root_dir, session, cache)
            if not data:
                total_failed += 1
                failed_entries.append(f"{filename} | fetch_failed | {url}")
                continue

            resized = resize_gif_bytes(
                data,
                target_size=args.size,
                is_shiny=shiny,
                loop_ms=args.loop_ms,
                sparkles=sparkle_frames,
            )
            if not resized:
                total_failed += 1
                failed_entries.append(f"{filename} | resize_failed | {url}")
                continue

            try:
                out_path.write_bytes(resized)
                total_saved += 1
            except Exception as exc:
                total_failed += 1
                failed_entries.append(f"{filename} | write_failed | {exc}")

    print(f"Saved: {total_saved} | Skipped: {total_skipped} | Failed: {total_failed}")
    if failed_entries:
        print("Failed entries:")
        for entry in failed_entries:
            print(f"- {entry}")


if __name__ == "__main__":
    main()


