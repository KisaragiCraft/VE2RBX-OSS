# VE2RBX OSS

**Reuse your VoxEdit / The Sandbox assets in Roblox.** VE2RBX OSS is a local tool that converts VoxEdit projects into clean, Roblox-ready `FBX` / `GLB` / `OBJ` — with colors, glow, and animation kept intact. It runs entirely on your PC: no account, no payment, and nothing is ever uploaded.

*Other languages: [日本語 (Japanese)](README.ja.md)*

<!-- HERO IMAGE — save the side-by-side as assets/hero.png, then delete this comment and uncomment the line below:
![A VoxEdit project (left) converted into Roblox Studio (right)](assets/hero.png)
-->

## Why VE2RBX?

Made models in VoxEdit and want to use them in Roblox? The usual export path tends to break colors and textures, leaving you with hours of cleanup. VE2RBX is built around how Roblox Studio actually imports models — not just plain file conversion.

**Crisp colors, no bleeding.**
VoxEdit models are pixel-art-like: every face is a flat color with hard edges. Ordinary texture baking blurs those boundaries. VE2RBX instead builds a separate face for each color and fills it with a single palette color, so edges stay sharp and colors never mix.
<!-- ![Standard export (blurred edges) vs VE2RBX (sharp edges)](assets/compare_color.png) -->

**Glow (emissive) preserved.**
Emissive colors are separated into their own parts so they can actually glow in Roblox. (Split glow-only parts in VoxEdit first, since glow is applied per part.)
<!-- ![Emissive parts glowing in Roblox](assets/compare_glow.png) -->

**Animations carry over.**
VoxEdit animations transfer straight into Roblox.
<!-- ![A VoxEdit animation playing in Roblox](assets/anim.gif) -->

**Roblox-aware geometry.**
Roblox caps each mesh at 20,000 vertices. VE2RBX merges VoxEdit's many small parts so you don't import a pile of separate models, and automatically splits into new parts before hitting the limit. For animated models, moving parts are kept separate while static parts are merged.

**Fully local.**
No account, no billing, and your project never leaves your computer.

> Output formats: `.fbx`, `.glb`, `.obj`

## Quick Start

1. Download `VE2RBX.exe` from the [Releases page](https://github.com/KisaragiCraft/VE2RBX-OSS/releases).
2. Run it — a converter page opens in your browser.
3. Drag in a VoxEdit project **folder or `.zip`**.
4. Press **Convert**.
5. Your files appear in `Documents\VE2RBXoutput`.

**About the input:** the folder or ZIP must be a *complete* VoxEdit project containing both a `.vxr` and a `.vxa` file. A lone `.vxm` mesh file is not enough. Use English letters and numbers for the project folder name — the name is reused for export filenames, and non-English names can break Roblox texture references.

## Output Files

Each conversion creates a folder named after your project inside `Documents\VE2RBXoutput`:

| File | What it is |
| --- | --- |
| `<Name>.fbx` | Static model for Roblox Studio (exported at 0.01 scale) |
| `<Name>.glb` | Static GLB at normal scale |
| `<Name>_obj/` | OBJ + MTL + texture output at normal scale |
| `<Name>_anim.fbx` | Animation rig/model (when animation export is available) |
| `<Name>_animclip_<Clip>.fbx` | One FBX per animation clip |
| `<Name>_roblox_post_import.luau` | Helper script for imported visual models |

## Using the Output in Roblox Studio

**Static models:** import `<Name>.fbx`. After import, run `<Name>_roblox_post_import.luau` from the command bar if you want the imported MeshParts anchored and visual-only.

**Animations:** import `<Name>_anim.fbx` first as the animation rig/model, then assign each `<Name>_animclip_<Clip>.fbx` through Roblox Studio's animation clip editor.

## Run From Source

For everyday use, prefer the EXE above. To run from source instead:

**Requirements**
- Windows
- Python 3.11 or newer
- Blender installed, or `blender` / `blender.exe` available on your PATH

```powershell
python app\launcher\local_launcher.py
```

This opens the same local browser UI and writes outputs to `Documents\VE2RBXoutput`. For a no-browser smoke test:

```powershell
python app\launcher\local_launcher.py --no-browser --port 8765
```

### Build your own EXE

```powershell
python -m pip install pyinstaller
.\build_win.bat
```

The result is `dist\VE2RBX.exe`.

## License

VE2RBX OSS is free software under the **GNU General Public License v3.0 or later (`GPL-3.0-or-later`)**. See [LICENSE](LICENSE) for the full text.

- **Files you create with VE2RBX OSS (`.fbx`, `.glb`, `.obj`) are yours.** They are tool output and are not restricted by this license — use them in commercial Roblox games or anywhere else.
- The converter runs inside Blender through its Python API (`bpy`), so VE2RBX OSS itself is distributed under the GPL to stay compatible with Blender's license. If you modify and redistribute VE2RBX OSS, keep it open under the same terms.

Copyright (C) 2026 KisaragiCraft
