#!/usr/bin/env python3
"""Write the Subha girl icon into the generated native projects.
Usage: python3 scripts/apply_icons.py android|ios   (run from mobile/)"""
import os, sys
from PIL import Image, ImageDraw

A = "assets"
only = Image.open(f"{A}/icon-only.png").convert("RGBA")
fg = Image.open(f"{A}/icon-foreground.png").convert("RGBA")
bg = Image.open(f"{A}/icon-background.png").convert("RGBA")

def save(im, path, size):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    im.resize((size, size), Image.LANCZOS).save(path)

def android():
    res = "android/app/src/main/res"
    assert os.path.isdir(res), "android project missing - run `npx cap add android` first"
    dens = {"mdpi": (48, 108), "hdpi": (72, 162), "xhdpi": (96, 216),
            "xxhdpi": (144, 324), "xxxhdpi": (192, 432)}
    for d, (legacy, adaptive) in dens.items():
        m = f"{res}/mipmap-{d}"
        save(only, f"{m}/ic_launcher.png", legacy)
        # round legacy icon
        big = only.resize((legacy * 4, legacy * 4), Image.LANCZOS)
        mask = Image.new("L", big.size, 0)
        ImageDraw.Draw(mask).ellipse((0, 0, big.size[0] - 1, big.size[1] - 1), fill=255)
        big.putalpha(mask)
        big.resize((legacy, legacy), Image.LANCZOS).save(f"{m}/ic_launcher_round.png")
        save(fg, f"{m}/ic_launcher_foreground.png", adaptive)
        save(bg, f"{m}/ic_launcher_background.png", adaptive)
    xml = ('<?xml version="1.0" encoding="utf-8"?>\n'
           '<adaptive-icon xmlns:android="http://schemas.android.com/apk/res/android">\n'
           '    <background android:drawable="@mipmap/ic_launcher_background"/>\n'
           '    <foreground android:drawable="@mipmap/ic_launcher_foreground"/>\n'
           '</adaptive-icon>\n')
    os.makedirs(f"{res}/mipmap-anydpi-v26", exist_ok=True)
    for n in ("ic_launcher.xml", "ic_launcher_round.xml"):
        open(f"{res}/mipmap-anydpi-v26/{n}", "w").write(xml)
    print("Android icons written")

def ios():
    d = "ios/App/App/Assets.xcassets/AppIcon.appiconset"
    assert os.path.isdir(os.path.dirname(d)), "ios project missing - run `npx cap add ios` first"
    os.makedirs(d, exist_ok=True)
    for f in os.listdir(d):
        os.remove(os.path.join(d, f))
    flat = Image.new("RGB", only.size, (154, 107, 255))
    flat.paste(only, mask=only.split()[3])          # App Store rejects alpha
    flat.resize((1024, 1024), Image.LANCZOS).save(f"{d}/AppIcon-512@2x.png")
    open(f"{d}/Contents.json", "w").write(
        '{\n  "images": [\n    {\n      "filename": "AppIcon-512@2x.png",\n'
        '      "idiom": "universal",\n      "platform": "ios",\n      "size": "1024x1024"\n    }\n  ],\n'
        '  "info": {\n    "author": "xcode",\n    "version": 1\n  }\n}\n')
    print("iOS icon written")

{"android": android, "ios": ios}[sys.argv[1]]()
