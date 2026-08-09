#!/usr/bin/env python3
"""Generate one avatar per Kokoro voice via Cloudflare Workers AI (FLUX.1
[schnell], Apache-2.0).

Kokoro ships no artwork for its voices, so we make some. This is a build-time
tool: run it once, commit the results, and nobody cloning the repo needs an
image model or a Cloudflare account. The UI falls back to a generated SVG
monogram for any voice without a file here.

  ./tools/generate-avatars.py            # only missing ones
  ./tools/generate-avatars.py --force    # regenerate everything

Auth comes from CLOUDFLARE_API_TOKEN + CLOUDFLARE_ACCOUNT_ID, or is read from
an existing `wrangler login` session. The token needs Workers AI write.

Style is deliberately flat vector illustration, not photoreal: it stays
legible at 30-40px, holds together across 28 generations, and avoids putting
synthetic photographs of "people" next to accent and gender labels.
"""
import argparse
import base64
import json
import os
import pathlib
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "avatars"
MODEL = "@cf/black-forest-labs/flux-1-schnell"
SIZE = 256                     # displayed at 40-80px; 256 covers 3x screens

STYLE = ("flat vector avatar illustration, head and shoulders portrait, "
         "centered, simple bold geometric shapes, clean flat colour fills, "
         "soft pastel background, modern editorial style, friendly, "
         "no text, no letters, no words")

# Deliberately varied by age, hair and bearing so 28 avatars stay tellable
# apart at thumbnail size. Accent is carried by the voice id, not by looks.
PEOPLE = {
    # American female
    "af_heart":    "a warm smiling woman with dark curly shoulder-length hair",
    "af_bella":    "a young woman with long auburn hair and round glasses",
    "af_nicole":   "a calm woman with a short platinum blonde bob",
    "af_aoede":    "a woman with braided black hair and gold hoop earrings",
    "af_kore":     "a woman with straight dark hair and a high ponytail",
    "af_sarah":    "a woman in her thirties with wavy brown hair",
    "af_nova":     "a woman with a short pixie cut and bright blue jacket",
    "af_sky":      "a young woman with light freckles and a loose topknot",
    "af_alloy":    "a woman with sleek silver-grey hair and a turtleneck",
    "af_jessica":  "a woman with shoulder-length red hair and a denim collar",
    "af_river":    "a woman with long dark waves and a green scarf",
    # American male
    "am_michael":  "a man in his forties with short brown hair, clean shaven",
    "am_fenrir":   "a broad man with a full dark beard and tied-back hair",
    "am_puck":     "a young man with messy blond hair and a mischievous grin",
    "am_onyx":     "a man with a shaved head and a dark polo shirt",
    "am_eric":     "a man with short black hair and thick rectangular glasses",
    "am_liam":     "a young man with tousled brown hair and a hooded top",
    "am_echo":     "a man with a neat dark fade and a grey crew neck",
    "am_adam":     "an older man with greying temples and a plaid shirt",
    "am_santa":    "a cheerful older man with a white beard and red sweater",
    # British female
    "bf_emma":     "a poised woman with a dark chin-length bob",
    "bf_isabella": "a woman with long dark hair and a crisp white collar",
    "bf_lily":     "a young woman with light blonde hair and a pale cardigan",
    "bf_alice":    "a woman with strawberry blonde curls and a knitted jumper",
    # British male
    "bm_george":   "a distinguished older man with grey hair and a tweed jacket",
    "bm_fable":    "a man with dark hair, a trimmed beard and a scarf",
    "bm_lewis":    "a man in his thirties with short ginger hair",
    "bm_daniel":   "a young man with neat dark hair and a navy jumper",
}


def credentials():
    token = os.environ.get("CLOUDFLARE_API_TOKEN")
    account = os.environ.get("CLOUDFLARE_ACCOUNT_ID")
    if token and account:
        return token, account

    cfg = pathlib.Path("~/.wrangler/config/default.toml").expanduser()
    if not token and cfg.exists():
        m = re.search(r'oauth_token\s*=\s*"([^"]+)"', cfg.read_text())
        if m:
            token = m.group(1)
    if not account:
        try:                                   # ask wrangler rather than guess
            out = subprocess.run(["wrangler", "whoami"], capture_output=True,
                                 text=True, timeout=60).stdout
            ids = re.findall(r"\b([0-9a-f]{32})\b", out)
            account = ids[0] if ids else None
        except (OSError, subprocess.SubprocessError):
            pass
    if not (token and account):
        sys.exit("need CLOUDFLARE_API_TOKEN and CLOUDFLARE_ACCOUNT_ID, "
                 "or a current `wrangler login`")
    return token, account


def generate(voice, token, account, attempt=0):
    who = PEOPLE[voice]
    body = json.dumps({
        "prompt": f"{who}. {STYLE}",
        "steps": 8,
        # Seed derived from the name, not random: regenerating reproduces the
        # same face. (PYTHONHASHSEED would make hash() useless for this.)
        "seed": sum(ord(c) * (i + 1) for i, c in enumerate(voice)),
    }).encode()
    req = urllib.request.Request(
        f"https://api.cloudflare.com/client/v4/accounts/{account}/ai/run/{MODEL}",
        data=body, method="POST",
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            payload = json.load(r)
    except urllib.error.HTTPError as e:
        detail = e.read().decode()[:300]
        if e.code in (429, 500, 502, 503) and attempt < 3:
            time.sleep(4 * (attempt + 1))
            return generate(voice, token, account, attempt + 1)
        raise SystemExit(f"{voice}: HTTP {e.code} {detail}")
    if not payload.get("success", True):
        raise SystemExit(f"{voice}: {payload.get('errors')}")
    return base64.b64decode(payload["result"]["image"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--only", help="comma-separated voice ids")
    args = ap.parse_args()

    OUT.mkdir(exist_ok=True)
    token, account = credentials()
    wanted = args.only.split(",") if args.only else list(PEOPLE)

    made = 0
    for voice in wanted:
        dest = OUT / f"{voice}.webp"
        if dest.exists() and not args.force:
            continue
        png = OUT / f"{voice}.png"
        png.write_bytes(generate(voice, token, account))
        # Square-crop to the centre, downscale, and convert. cwebp at q80 puts
        # a 256px avatar around 5 KB, so all 28 cost ~140 KB in the repo.
        subprocess.run(["convert", str(png), "-gravity", "center",
                        "-crop", "1:1", "+repage",
                        "-resize", f"{SIZE}x{SIZE}", str(png)], check=True)
        subprocess.run(["cwebp", "-quiet", "-q", "80", str(png),
                        "-o", str(dest)], check=True)
        png.unlink()
        made += 1
        print(f"  {voice:<13} {dest.stat().st_size/1024:5.1f} KB")

    total = sum(f.stat().st_size for f in OUT.glob("*.webp"))
    print(f"\n{made} generated, {len(list(OUT.glob('*.webp')))} on disk, "
          f"{total/1024:.0f} KB total")


if __name__ == "__main__":
    main()
